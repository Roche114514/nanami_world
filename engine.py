"""`nanami_world` 确定性物理、离散坐标与物质守恒内核。

职责：
- 管理唯一事实账本 `world.json`、蓝图库 `blueprints.json`、调色板 `palettes.json` 与不可篡改事件流 `events.jsonl`；
- 强校验物质守恒（唯一 `parent_id`）、坐标碰撞、墙面/地面/桌面分层与 BFS 连通寻路；
- 驱动 10 个正交能力组件（Traits）的确定性状态机与时钟推进（0 Token）。
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import logging
import os
import re
import threading
import time
from collections import deque
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from .schema import (
    DEFAULT_ACTOR_PIXELS_16X24,
    DEFAULT_ACTOR_SLOT_COLORS,
    MAX_BRIEF_CHARS,
    MAX_DETAIL_CHARS,
    MAX_SPACE_HEIGHT,
    MAX_SPACE_WIDTH,
    MIN_SPACE_HEIGHT,
    MIN_SPACE_WIDTH,
    TILE_CATALOG,
    clamp_float,
    clamp_int,
    clip_text,
    normalize_actor,
    normalize_autonomy,
    normalize_blueprint,
    normalize_combine_rules,
    normalize_effects,
    normalize_footprint,
    normalize_palette,
    normalize_space,
    normalize_space_tiles,
    normalize_sprite,
    normalize_traits,
    slugify,
)

logger = logging.getLogger(__name__)

_STAGE_DIR_RE = re.compile(r"[（(][^）)\n]{1,40}[）)]")
_MAX_UPLOAD_BYTES = 512 * 1024
_EMOTION_TAG_LINE_RE = re.compile(r"(?:sprite|立绘|表情)\s*(\d+)\s*[:：]\s*(.+)", re.IGNORECASE)

_EMOTION_BUCKET_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("sleep_deep", ("筋疲力尽", "安睡", "打瞌睡", "流口水", "熟睡", "睡着")),
    ("sleepy", ("困倦", "疲倦", "倦怠", "疲惫", "打哈欠", "困", "犯困")),
    ("shy", ("害羞", "脸红", "羞涩", "腼腆")),
    ("excited", ("兴奋", "激动", "激動", "喘气", "自信", "得意", "狡黠")),
    ("happy", ("开心", "微笑", "温柔", "喜悦", "喜悅", "欢快", "歡快", "高兴", "温和", "溫和")),
    ("angry", ("生气", "鼓起脸颊", "埋怨", "嘟嘴", "斥责", "斥責", "讨厌", "討厭", "恼火")),
    ("sad", ("难过", "沮丧", "不满", "悲伤", "悲傷", "自闭", "委屈", "失落")),
    ("cold", ("冷漠", "警告", "阻止", "制止", "冷淡", "疏离")),
    ("worried", ("担心", "不安", "皱眉", "苦恼", "苦惱", "严肃", "惊恐", "慌张", "驚恐")),
    ("curious", ("好奇", "沉思", "思考", "仔细查看", "眺望", "疑惑", "歪头", "惊讶", "驚訝", "吃惊", "震惊", "震驚", "困惑", "无奈", "無奈")),
    ("explain", ("指点", "引导", "解释", "果断", "陈述", "从容", "從容")),
    ("neutral", ("中性", "平静", "宁静", "寧靜", "普通", "默认")),
)

_EMOTION_HINT_ALIASES: dict[str, str] = {
    "happy": "happy", "开心": "happy", "高兴": "happy", "微笑": "happy", "温柔": "happy", "喜欢": "happy", "松快": "happy",
    "excited": "excited", "兴奋": "excited", "激动": "excited", "来劲": "excited", "得意": "excited",
    "shy": "shy", "害羞": "shy", "脸红": "shy",
    "sleepy": "sleepy", "困": "sleepy", "困倦": "sleepy", "疲倦": "sleepy", "累": "sleepy",
    "sleep_deep": "sleep_deep", "睡着": "sleep_deep", "休息": "sleep_deep", "筋疲力尽": "sleep_deep",
    "angry": "angry", "生气": "angry", "不高兴": "angry", "嘟嘴": "angry", "委屈": "angry", "不满": "angry",
    "sad": "sad", "难过": "sad", "沮丧": "sad", "悲伤": "sad", "失落": "sad",
    "cold": "cold", "冷漠": "cold", "严肃": "cold", "沉默": "cold",
    "worried": "worried", "担心": "worried", "不安": "worried", "皱眉": "worried",
    "curious": "curious", "好奇": "curious", "思考": "curious", "疑惑": "curious", "沉思": "curious", "惊讶": "curious",
    "explain": "explain", "解释": "explain", "指点": "explain", "认真": "explain",
    "neutral": "neutral", "平静": "neutral", "中性": "neutral", "正常": "neutral",
}


def classify_emotion_bucket(tag_text: str) -> str:
    txt = str(tag_text or "").strip().lower()
    if not txt:
        return "neutral"
    for bucket, keywords in _EMOTION_BUCKET_RULES:
        if any(kw in txt for kw in keywords):
            return bucket
    return "neutral"


def parse_emotion_tags(raw_tags: str) -> dict[int, str]:
    out: dict[int, str] = {}
    for line in str(raw_tags or "").splitlines():
        m = _EMOTION_TAG_LINE_RE.search(line.strip())
        if not m:
            continue
        idx = int(m.group(1))
        tag = clip_text(m.group(2), 40, "")
        if idx >= 1 and tag:
            out[idx] = tag
    return out


def _build_pack_sprites(raw_sprites: Any, emotion_tags_raw: str) -> list[dict[str, Any]]:
    if not isinstance(raw_sprites, list):
        return []
    tag_map = parse_emotion_tags(emotion_tags_raw)
    out: list[dict[str, Any]] = []
    for pos, item in enumerate(raw_sprites, start=1):
        path = str(item.get("path") if isinstance(item, Mapping) else item or "").strip()
        if not path:
            continue
        tag = tag_map.get(pos) or ("中性，平静" if pos == 1 else f"立绘 {pos}")
        out.append({
            "index": pos,
            "path": path,
            "tag": tag,
            "bucket": classify_emotion_bucket(tag),
        })
    return out

_VERB_ALIASES: dict[str, str] = {
    "look": "look",
    "凑近看": "look",
    "查看": "look",
    "观察": "look",
    "take": "take",
    "拿起": "take",
    "拿": "take",
    "place": "place",
    "放下": "place",
    "放置": "place",
    "open": "open",
    "打开": "open",
    "开门": "open",
    "推开门": "open",
    "把门打开": "open",
    "close": "close",
    "合上": "close",
    "关上": "close",
    "关门": "close",
    "把门关上": "close",
    "unlock": "unlock",
    "开锁": "unlock",
    "解锁": "unlock",
    "consume": "consume",
    "use": "consume",
    "使用": "consume",
    "喝": "consume",
    "drink": "consume",
    "吃": "consume",
    "eat": "consume",
    "turn_on": "turn_on",
    "开灯": "turn_on",
    "开机": "turn_on",
    "打开开关": "turn_on",
    "开启": "turn_on",
    "turn_off": "turn_off",
    "关灯": "turn_off",
    "关机": "turn_off",
    "关掉": "turn_off",
    "关闭": "turn_off",
    "rest": "rest",
    "休息": "rest",
    "坐下休息": "rest",
    "躺下": "rest",
    "leave_rest": "leave_rest",
    "起身": "leave_rest",
    "起来": "leave_rest",
    "stand": "leave_rest",
    "interact": "interact",
    "互动": "interact",
    "玩": "interact",
    "玩一会": "interact",
    "玩游戏": "interact",
    "翻阅": "interact",
    "read": "read",
    "读": "read",
    "阅读": "read",
    "enter": "enter",
    "进入": "enter",
    "推门": "enter",
}


def _atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _read_json_file(path: Path, fallback: Any) -> Any:
    if not path.is_file():
        return fallback
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("nanami_world: 读取 JSON 失败: %s", path, exc_info=True)
        return fallback


def _parse_characters_yaml_fallback(raw_text: str) -> list[dict[str, Any]]:
    """无 PyYAML 时只读解析 data/config/characters.yaml 的基础字段与 sprites/emotion_tags。"""
    items: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    multiline_key = ""
    multiline_quote = ""
    multiline_buf: list[str] = []
    in_sprites = False

    def _flush_multiline() -> None:
        nonlocal multiline_key, multiline_quote, multiline_buf
        if cur is not None and multiline_key:
            joined = "\n".join(multiline_buf).strip()
            if multiline_quote and joined.endswith(multiline_quote):
                joined = joined[:-1].strip()
            cur[multiline_key] = joined
        multiline_key = ""
        multiline_quote = ""
        multiline_buf = []

    for raw_line in raw_text.splitlines():
        if not raw_line.strip() and not multiline_key:
            continue
        if multiline_key:
            if multiline_quote:
                stripped = raw_line.strip()
                if stripped.endswith(multiline_quote) and not stripped.endswith("''"):
                    multiline_buf.append(stripped[:-1])
                    _flush_multiline()
                else:
                    multiline_buf.append(stripped)
                continue
            if raw_line.startswith("    "):
                multiline_buf.append(raw_line[4:])
                continue
            _flush_multiline()

        stripped = raw_line.strip()
        if stripped.startswith("#"):
            continue
        if raw_line.startswith("- "):
            if cur and cur.get("name"):
                items.append(cur)
            cur = {"name": "", "color": "", "character_brief": "", "character_setting": "", "sprites": [], "emotion_tags": ""}
            in_sprites = False
            stripped = raw_line[2:].strip()
        if cur is None:
            continue
        if raw_line.startswith("  ") and not raw_line.startswith("    "):
            in_sprites = stripped.startswith("sprites:")
        if in_sprites and (stripped.startswith("- path:") or stripped.startswith("path:")):
            p = stripped.split(":", 1)[1].strip().strip("'\"")
            if p:
                cur["sprites"].append({"path": p})
            continue
        for key in ("name", "color", "character_brief", "character_setting", "emotion_tags"):
            if stripped.startswith(f"{key}:"):
                val = stripped[len(key) + 1:].strip()
                if val in ("|", "|-", ">", ">-"):
                    multiline_key = key
                    multiline_quote = ""
                    multiline_buf = []
                elif len(val) >= 1 and val[0] in ("'", '"') and (len(val) == 1 or not val.endswith(val[0])):
                    multiline_key = key
                    multiline_quote = val[0]
                    multiline_buf = [val[1:]]
                else:
                    cur[key] = val.strip("'\"")
                break
    _flush_multiline()
    if cur and cur.get("name"):
        items.append(cur)
    return items


def _detect_image_ext(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return ""


def compute_lighting(space: Mapping[str, Any], entities: Mapping[str, Mapping[str, Any]], now: float, offset_minutes: float) -> dict[str, Any]:
    """根据时间窗（或手动预设）与灯具开关计算当前空间光照与时段。"""
    mode = str(space.get("ambient_light") or "auto")
    dt = datetime.fromtimestamp(now + offset_minutes * 60.0)
    clock_str = dt.strftime("%H:%M")
    if mode == "auto":
        hour = dt.hour
        if 5 <= hour < 8:
            period = "dawn"
        elif 8 <= hour < 17:
            period = "day"
        elif 17 <= hour < 20:
            period = "dusk"
        else:
            period = "night"
    else:
        period = mode

    sid = str(space.get("space_id") or "")
    lit_lamps = [
        ent for ent in entities.values()
        if ent.get("space_id") == sid
        and int(ent.get("z", 0)) >= 0
        and bool(((ent.get("traits") or {}).get("switchable") or {}).get("provides_light"))
        and bool(((ent.get("traits") or {}).get("switchable") or {}).get("is_on"))
    ]
    is_dark = period == "night" and not lit_lamps
    return {
        "period": period,
        "clock": clock_str,
        "has_lamp_on": bool(lit_lamps),
        "is_dark": is_dark,
        "lamp_positions": [{"x": int(e.get("x", 0)), "y": int(e.get("y", 0))} for e in lit_lamps],
    }


class WorldEngine:
    """线程安全文字世界物理引擎与状态仓储。"""

    def __init__(
        self,
        *,
        code_dir: Path,
        data_root: Path,
        project_root: Path | None = None,
    ) -> None:
        self._code_dir = Path(code_dir)
        self._data_root = Path(data_root)
        self._project_root = Path(project_root) if project_root else Path.cwd()
        self._lock = threading.RLock()

        self._world_path = self._data_root / "world.json"
        self._blueprints_path = self._data_root / "blueprints.json"
        self._palettes_path = self._data_root / "palettes.json"
        self._events_path = self._data_root / "events.jsonl"
        self._sprites_dir = self._data_root / "sprites"
        self._bridge_dir = self._data_root / "bridge"

        self._builtin_room_cfg = self._load_builtin_room_cfg()
        self._builtin_blueprints = self._load_builtin_blueprints()
        self._builtin_palettes = self._load_builtin_palettes()

        self._custom_blueprints: dict[str, dict[str, Any]] = {}
        self._custom_palettes: dict[str, dict[str, Any]] = {}
        self._host_sprite_cache: dict[str, str] = {}
        self._world: dict[str, Any] = {}
        self._load_all()

    @property
    def data_root(self) -> Path:
        return self._data_root

    @property
    def project_root(self) -> Path:
        return self._project_root

    @property
    def bridge_dir(self) -> Path:
        return self._bridge_dir

    # ── 初始化加载 ──────────────────────────────────────────────────────
    def _load_builtin_room_cfg(self) -> dict[str, Any]:
        raw = _read_json_file(self._code_dir / "defaults" / "room_default.json", {})
        return raw if isinstance(raw, dict) else {}

    def _load_builtin_blueprints(self) -> dict[str, dict[str, Any]]:
        raw = _read_json_file(self._code_dir / "defaults" / "blueprints.json", [])
        out: dict[str, dict[str, Any]] = {}
        if isinstance(raw, list):
            for item in raw:
                if isinstance(item, Mapping):
                    bp = normalize_blueprint(item, builtin=True)
                    out[bp["blueprint_id"]] = bp
        return out

    def _load_builtin_palettes(self) -> dict[str, dict[str, Any]]:
        raw = _read_json_file(self._code_dir / "defaults" / "palettes.json", [])
        out: dict[str, dict[str, Any]] = {}
        if isinstance(raw, list):
            for item in raw:
                if isinstance(item, Mapping):
                    pal = normalize_palette(item, builtin=True)
                    out[pal["palette_id"]] = pal
        return out

    def _load_all(self) -> None:
        with self._lock:
            raw_bps = _read_json_file(self._blueprints_path, {})
            if isinstance(raw_bps, Mapping):
                for bid, item in raw_bps.items():
                    if isinstance(item, Mapping):
                        bp = normalize_blueprint(item, builtin=False)
                        self._custom_blueprints[str(bid)] = bp

            raw_pals = _read_json_file(self._palettes_path, {})
            if isinstance(raw_pals, Mapping):
                for pid, item in raw_pals.items():
                    if isinstance(item, Mapping):
                        pal = normalize_palette(item, builtin=False)
                        self._custom_palettes[str(pid)] = pal

            raw_world = _read_json_file(self._world_path, None)
            if isinstance(raw_world, Mapping) and isinstance(raw_world.get("spaces"), Mapping) and raw_world["spaces"]:
                self._world = self._normalize_world_dict(raw_world)
            else:
                self._world = self._default_world()
                self._save_world_locked()

    def _default_world(self) -> dict[str, Any]:
        sp_raw = self._builtin_room_cfg.get("default_space") or {"space_id": "room_default", "name": "默认房间"}
        space = normalize_space(sp_raw)
        return {
            "version": 1,
            "active_space_id": space["space_id"],
            "active_actor_id": "",
            "last_tick_ts": time.time(),
            "clock_offset_minutes": 0.0,
            "next_seq": 1,
            "next_entity_num": 1,
            "spaces": {space["space_id"]: space},
            "actors": {},
            "entities": {},
            "pending_diffs": [],
            "looked_entities": {},
        }

    def _normalize_world_dict(self, raw: Mapping[str, Any]) -> dict[str, Any]:
        spaces = {}
        for sid, sp in (raw.get("spaces") or {}).items():
            if isinstance(sp, Mapping):
                norm = normalize_space(sp)
                spaces[str(sid)] = norm
        if not spaces:
            sp = normalize_space({"space_id": "room_default", "name": "默认房间"})
            spaces[sp["space_id"]] = sp

        active_sid = str(raw.get("active_space_id") or "")
        if active_sid not in spaces:
            active_sid = next(iter(spaces))

        actors: dict[str, dict[str, Any]] = {}
        for aid, act in (raw.get("actors") or {}).items():
            if isinstance(act, Mapping):
                norm_a = normalize_actor(act)
                if norm_a["space_id"] not in spaces:
                    norm_a["space_id"] = active_sid
                actors[str(aid)] = norm_a

        active_aid = str(raw.get("active_actor_id") or "")
        if active_aid not in actors:
            active_aid = next(iter(actors), "")

        entities: dict[str, dict[str, Any]] = {}
        for eid, ent in (raw.get("entities") or {}).items():
            if isinstance(ent, Mapping):
                norm_e = self._normalize_entity_dict(ent)
                if norm_e["space_id"] in spaces:
                    entities[str(eid)] = norm_e

        return {
            "version": 1,
            "active_space_id": active_sid,
            "active_actor_id": active_aid,
            "last_tick_ts": float(raw.get("last_tick_ts") or time.time()),
            "clock_offset_minutes": float(raw.get("clock_offset_minutes") or 0.0),
            "next_seq": max(1, int(raw.get("next_seq") or 1)),
            "next_entity_num": max(1, int(raw.get("next_entity_num") or 1)),
            "spaces": spaces,
            "actors": actors,
            "entities": entities,
            "pending_diffs": list(raw.get("pending_diffs") or [])[-30:],
            "looked_entities": dict(raw.get("looked_entities") or {}),
        }

    def _normalize_entity_dict(self, raw: Mapping[str, Any]) -> dict[str, Any]:
        name = clip_text(raw.get("name"), 12, "物件")
        eid = str(raw.get("entity_id") or "").strip() or slugify(name, prefix="ent")
        footprint = normalize_footprint(raw.get("footprint"))
        placement = str(raw.get("placement") or "any").strip().lower()
        if placement not in ("floor", "wall", "surface_only", "any"):
            placement = "any"
        traces_raw = raw.get("traces") if isinstance(raw.get("traces"), list) else []
        traces = [
            {"ts": float(t.get("ts") or time.time()), "text": clip_text(t.get("text"), 80, "")}
            for t in traces_raw[-20:]
            if isinstance(t, Mapping) and clip_text(t.get("text"), 80, "")
        ]
        traits = normalize_traits(raw.get("traits") if isinstance(raw.get("traits"), Mapping) else {})
        solid = bool(raw.get("solid", False))
        if "restable" in traits:
            solid = False
        if "portal" in traits:
            placement = "any"
            solid = not bool(traits["portal"].get("is_open", False))
        return {
            "entity_id": eid,
            "blueprint_id": str(raw.get("blueprint_id") or "").strip(),
            "name": name,
            "brief": clip_text(raw.get("brief"), MAX_BRIEF_CHARS, name),
            "detail": clip_text(raw.get("detail"), MAX_DETAIL_CHARS, f"一件{name}。"),
            "tags": [clip_text(t, 16, "").lower() for t in (raw.get("tags") or []) if clip_text(t, 16, "")][:6],
            "parent_id": str(raw.get("parent_id") or f"space:{raw.get('space_id') or 'room_default'}"),
            "space_id": str(raw.get("space_id") or "room_default"),
            "x": int(raw.get("x") or 0),
            "y": int(raw.get("y") or 1),
            "z": int(raw.get("z") or 0),
            "footprint": footprint,
            "placement": placement,
            "solid": solid,
            "traits": traits,
            "effects": normalize_effects(raw.get("effects") if isinstance(raw.get("effects"), Mapping) else {}),
            "combine_rules": normalize_combine_rules(raw.get("combine_rules")),
            "sprite": normalize_sprite(
                raw.get("sprite") if isinstance(raw.get("sprite"), Mapping) else None,
                default_w=max(16, footprint[0] * 16),
                default_h=max(16, footprint[1] * 16),
            ),
            "traces": traces,
        }

    def _save_world_locked(self) -> None:
        _atomic_write_json(self._world_path, self._world)

    def _save_blueprints_locked(self) -> None:
        _atomic_write_json(self._blueprints_path, self._custom_blueprints)

    def _save_palettes_locked(self) -> None:
        _atomic_write_json(self._palettes_path, self._custom_palettes)

    def _append_event_locked(
        self,
        kind: str,
        *,
        actor_id: str = "",
        space_id: str = "",
        payload: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        seq = int(self._world.get("next_seq") or 1)
        self._world["next_seq"] = seq + 1
        event = {
            "seq": seq,
            "ts": round(time.time(), 3),
            "kind": str(kind),
            "actor_id": str(actor_id or ""),
            "space_id": str(space_id or self._world.get("active_space_id") or ""),
            "payload": dict(payload or {}),
        }
        try:
            self._events_path.parent.mkdir(parents=True, exist_ok=True)
            with self._events_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, ensure_ascii=False) + "\n")
        except OSError:
            logger.warning("nanami_world: 写入 events.jsonl 失败", exc_info=True)
        return event

    def _push_diff_locked(self, space_id: str, text: str) -> None:
        cleaned = clip_text(text, 100, "")
        if not cleaned:
            return
        diffs = self._world.setdefault("pending_diffs", [])
        diffs.append({"space_id": space_id, "ts": time.time(), "text": cleaned})
        if len(diffs) > 30:
            self._world["pending_diffs"] = diffs[-30:]

    def consume_diffs(self, space_id: str) -> list[str]:
        with self._lock:
            diffs = self._world.get("pending_diffs") or []
            matched = [str(d.get("text") or "") for d in diffs if d.get("space_id") == space_id and d.get("text")]
            if matched:
                self._world["pending_diffs"] = [d for d in diffs if d.get("space_id") != space_id]
                self._save_world_locked()
            return matched

    def peek_diffs(self, space_id: str) -> list[str]:
        with self._lock:
            diffs = self._world.get("pending_diffs") or []
            return [str(d.get("text") or "") for d in diffs if d.get("space_id") == space_id and d.get("text")]

    def read_recent_events(self, limit: int = 40) -> list[dict[str, Any]]:
        with self._lock:
            if not self._events_path.is_file():
                return []
            try:
                lines = self._events_path.read_text(encoding="utf-8").splitlines()
            except OSError:
                return []
            out: list[dict[str, Any]] = []
            for raw in lines[-max(1, limit):]:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    item = json.loads(raw)
                    if isinstance(item, dict):
                        out.append(item)
                except ValueError:
                    continue
            return out

    # ── 只读工具与目录查询 ──────────────────────────────────────────────
    def all_blueprints(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            merged: dict[str, dict[str, Any]] = {}
            for k, v in self._builtin_blueprints.items():
                merged[k] = copy.deepcopy(v)
            for k, v in self._custom_blueprints.items():
                merged[k] = copy.deepcopy(v)
            return merged

    def all_palettes(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            merged: dict[str, dict[str, Any]] = {}
            for k, v in self._builtin_palettes.items():
                merged[k] = copy.deepcopy(v)
            for k, v in self._custom_palettes.items():
                merged[k] = copy.deepcopy(v)
            return merged

    def list_host_characters(self) -> list[dict[str, Any]]:
        """只读读取宿主 data/config/characters.yaml（含立绘路径与情绪标签）。"""
        candidates = [
            self._project_root / "data" / "config" / "characters.yaml",
            Path("data/config/characters.yaml"),
        ]
        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        for cfg_path in candidates:
            if not cfg_path.is_file():
                continue
            try:
                raw_text = cfg_path.read_text(encoding="utf-8")
            except OSError:
                continue
            parsed: Any = None
            try:
                import yaml  # type: ignore

                parsed = yaml.safe_load(raw_text)
            except Exception:
                parsed = _parse_characters_yaml_fallback(raw_text)
            if isinstance(parsed, list):
                for entry in parsed:
                    if not isinstance(entry, Mapping):
                        continue
                    name = str(entry.get("name") or "").strip()
                    if not name or name in seen:
                        continue
                    seen.add(name)
                    pack_sprites = _build_pack_sprites(
                        entry.get("sprites"),
                        str(entry.get("emotion_tags") or ""),
                    )
                    crop_raw = entry.get("portrait_crop") if isinstance(entry.get("portrait_crop"), Mapping) else {}
                    items.append({
                        "name": name,
                        "color": str(entry.get("color") or "#e8a2b8").strip(),
                        "brief": str(entry.get("character_brief") or entry.get("brief") or "").strip(),
                        "setting": str(entry.get("character_setting") or entry.get("setting") or "").strip()[:1600],
                        "emotion_tags": str(entry.get("emotion_tags") or "").strip(),
                        "portrait_crop": {
                            "x": clamp_float(crop_raw.get("x"), 0.0, 1.0, 0.5),
                            "y": clamp_float(crop_raw.get("y"), 0.0, 1.0, 0.2),
                            "zoom": clamp_float(crop_raw.get("zoom"), 0.5, 3.0, 1.0),
                        },
                        "pack_sprites": pack_sprites,
                        "default_sprite_path": pack_sprites[0]["path"] if pack_sprites else "",
                    })
            if items:
                return items
        return items

    def _host_sprite_data_url(self, rel_path: str) -> str:
        rel = str(rel_path or "").strip().lstrip("/\\")
        if not rel:
            return ""
        cached = self._host_sprite_cache.get(rel)
        if cached is not None:
            return cached
        roots = [
            (self._project_root / "data").resolve(),
            self._project_root.resolve(),
            (Path.cwd() / "data").resolve(),
            Path.cwd().resolve(),
        ]
        for root in roots:
            candidate = (root / rel).resolve()
            if root not in candidate.parents and candidate != root:
                continue
            if not candidate.is_file():
                continue
            try:
                blob = candidate.read_bytes()
            except OSError:
                continue
            ext = candidate.suffix.lower().lstrip(".")
            if ext == "jpg":
                ext = "jpeg"
            if ext not in ("png", "jpeg", "webp", "gif"):
                ext = "png"
            data_url = f"data:image/{ext};base64,{base64.b64encode(blob).decode('ascii')}"
            if len(self._host_sprite_cache) < 128:
                self._host_sprite_cache[rel] = data_url
            return data_url
        return ""

    # ── 上传图片管理 ────────────────────────────────────────────────────
    def save_uploaded_sprite(self, data_url_or_b64: str) -> str:
        raw_str = str(data_url_or_b64 or "").strip()
        if not raw_str:
            raise ValueError("图片数据不能为空。")
        if "," in raw_str and raw_str.lower().startswith("data:"):
            raw_str = raw_str.split(",", 1)[1]
        try:
            blob = base64.b64decode(raw_str, validate=True)
        except Exception as exc:
            raise ValueError("无效的 Base64 图片数据。") from exc
        if len(blob) > _MAX_UPLOAD_BYTES:
            raise ValueError(f"静态贴图不能超过 {_MAX_UPLOAD_BYTES // 1024} KB。")
        ext = _detect_image_ext(blob)
        if not ext:
            raise ValueError("仅支持 PNG / JPG / WEBP 静态图片。")
        digest = hashlib.sha256(blob).hexdigest()[:16]
        rel = f"sprites/{digest}{ext}"
        dest = self._data_root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.exists():
            tmp = dest.with_name(f"{dest.name}.tmp")
            tmp.write_bytes(blob)
            os.replace(tmp, dest)
        return rel

    def _sprite_with_data_url(self, sprite: Mapping[str, Any]) -> dict[str, Any]:
        out = copy.deepcopy(dict(sprite))
        rel = str(out.get("image_path") or "").strip()
        if rel:
            candidate = (self._data_root / rel).resolve()
            root_resolved = self._data_root.resolve()
            if root_resolved in candidate.parents and candidate.is_file():
                try:
                    raw = candidate.read_bytes()
                    ext = candidate.suffix.lower().lstrip(".") or "png"
                    if ext == "jpg":
                        ext = "jpeg"
                    out["image_data_url"] = f"data:image/{ext};base64,{base64.b64encode(raw).decode('ascii')}"
                except OSError:
                    pass
        return out

    # ── 地形方块、坐标、碰撞与寻路 ───────────────────────────────────────
    def get_cell_tile(self, space: Mapping[str, Any], x: int, y: int) -> dict[str, Any]:
        tiles = space.get("tiles") or {}
        tid = tiles.get(f"{int(x)},{int(y)}")
        if isinstance(tid, str) and tid in TILE_CATALOG:
            return TILE_CATALOG[tid]
        wall_rows = int(space.get("wall_rows") or 0)
        return TILE_CATALOG["wood_wall"] if 0 <= int(y) < wall_rows else TILE_CATALOG["wood_floor"]

    def is_wall_cell(self, space: Mapping[str, Any], x: int, y: int) -> bool:
        return bool(self.get_cell_tile(space, x, y).get("is_wall", False))

    def _entity_cells(self, entity: Mapping[str, Any]) -> list[tuple[int, int]]:
        x0 = int(entity.get("x") or 0)
        y0 = int(entity.get("y") or 0)
        fw, fh = normalize_footprint(entity.get("footprint"))
        return [(x0 + dx, y0 + dy) for dx in range(fw) for dy in range(fh)]

    def _surface_covering_locked(self, space_id: str, x: int, y: int, footprint: list[int], ignore_id: str | None = None) -> dict[str, Any] | None:
        fw, fh = footprint
        need_cells = {(x + dx, y + dy) for dx in range(fw) for dy in range(fh)}
        for eid, ent in self._world["entities"].items():
            if eid == ignore_id or ent.get("space_id") != space_id or int(ent.get("z", 0)) != 0:
                continue
            if "surface" not in (ent.get("traits") or {}):
                continue
            surf_cells = set(self._entity_cells(ent))
            if need_cells.issubset(surf_cells):
                return ent
        return None

    def _surface_children_locked(self, surface_id: str) -> list[dict[str, Any]]:
        parent_tag = f"entity:{surface_id}"
        return [ent for ent in self._world["entities"].values() if ent.get("parent_id") == parent_tag]

    def _container_children_locked(self, container_id: str) -> list[dict[str, Any]]:
        parent_tag = f"entity:{container_id}"
        return [ent for ent in self._world["entities"].values() if ent.get("parent_id") == parent_tag]

    def _is_entity_blocking_locked(self, ent: Mapping[str, Any]) -> bool:
        traits = ent.get("traits") or {}
        if "restable" in traits:
            return False
        if "portal" in traits:
            return not bool(traits["portal"].get("is_open", False))
        return bool(ent.get("solid", False))

    def _has_open_door_at_locked(self, space_id: str, x: int, y: int) -> bool:
        for ent in self._world["entities"].values():
            if ent.get("space_id") != space_id or int(ent.get("z", 0)) != 0:
                continue
            traits = ent.get("traits") or {}
            if "portal" not in traits or not bool(traits["portal"].get("is_open", False)):
                continue
            if (x, y) in self._entity_cells(ent):
                return True
        return False

    def _is_floor_walkable_locked(
        self,
        space: Mapping[str, Any],
        x: int,
        y: int,
        *,
        allow_cells: set[tuple[int, int]] | None = None,
        ignore_actor_id: str | None = None,
    ) -> bool:
        w = int(space["width"])
        h = int(space["height"])
        if not (0 <= x < w and 0 <= y < h):
            return False
        if allow_cells and (x, y) in allow_cells:
            return True
        sid = space["space_id"]
        has_open_door = self._has_open_door_at_locked(sid, x, y)
        if not has_open_door and not self.get_cell_tile(space, x, y).get("walkable", True):
            return False
        for ent in self._world["entities"].values():
            if ent.get("space_id") != sid or int(ent.get("z", 0)) != 0:
                continue
            if not self._is_entity_blocking_locked(ent):
                continue
            if (x, y) in self._entity_cells(ent):
                return False
        for aid, act in self._world["actors"].items():
            if aid == ignore_actor_id or act.get("space_id") != sid:
                continue
            if int(act.get("x", -1)) == x and int(act.get("y", -1)) == y:
                return False
        return True

    def _bfs_locked(
        self,
        space_id: str,
        start: tuple[int, int],
        goal_cells: set[tuple[int, int]],
        *,
        allow_cells: set[tuple[int, int]] | None = None,
        ignore_actor_id: str | None = None,
    ) -> list[tuple[int, int]] | None:
        space = self._world["spaces"].get(space_id)
        if not space or not goal_cells:
            return None
        if start in goal_cells:
            return [start]
        queue: deque[tuple[int, int]] = deque([start])
        parents: dict[tuple[int, int], tuple[int, int] | None] = {start: None}
        while queue:
            cx, cy = queue.popleft()
            for dx, dy in ((0, 1), (0, -1), (1, 0), (-1, 0)):
                nx, ny = cx + dx, cy + dy
                nxt = (nx, ny)
                if nxt in parents:
                    continue
                if not self._is_floor_walkable_locked(
                    space, nx, ny, allow_cells=allow_cells, ignore_actor_id=ignore_actor_id
                ):
                    continue
                parents[nxt] = (cx, cy)
                if nxt in goal_cells:
                    path = [nxt]
                    cur = (cx, cy)
                    while cur is not None:
                        path.append(cur)
                        cur = parents[cur]
                    path.reverse()
                    return path
                queue.append(nxt)
        return None

    def chebyshev_distance(self, actor: Mapping[str, Any], entity: Mapping[str, Any]) -> int:
        if entity.get("parent_id") == f"actor:{actor.get('actor_id')}":
            return 0
        if entity.get("space_id") != actor.get("space_id"):
            return 999
        ax, ay = int(actor.get("x") or 0), int(actor.get("y") or 0)
        cells = self._entity_cells(entity)
        if not cells:
            return 999
        return min(max(abs(ax - cx), abs(ay - cy)) for cx, cy in cells)

    def _resolve_placement_locked(
        self,
        space_id: str,
        x: int,
        y: int,
        footprint: list[int],
        placement: str,
        solid: bool,
        *,
        target_parent_id: str = "",
        ignore_id: str | None = None,
    ) -> tuple[str, int, int, int]:
        space = self._world["spaces"].get(space_id)
        if not space:
            raise ValueError(f"空间不存在：{space_id}")
        w = int(space["width"])
        h = int(space["height"])
        wall_rows = int(space["wall_rows"])
        fw, fh = footprint

        # 显式放入某个容器
        if target_parent_id.startswith("entity:"):
            pid = target_parent_id.split(":", 1)[1]
            parent_ent = self._world["entities"].get(pid)
            if not parent_ent:
                raise ValueError(f"目标物体不存在：{pid}")
            traits = parent_ent.get("traits") or {}
            if "container" in traits:
                cap = int(traits["container"].get("capacity") or 6)
                children = [c for c in self._container_children_locked(pid) if c["entity_id"] != ignore_id]
                if len(children) >= cap:
                    raise ValueError(f"「{parent_ent['name']}」已经装满了。")
                if placement not in ("any", "surface_only"):
                    raise ValueError("这件家具不能塞进储物箱里。")
                return f"entity:{pid}", int(parent_ent["x"]), int(parent_ent["y"]), -1

        if not (0 <= x and x + fw <= w and 0 <= y and y + fh <= h):
            raise ValueError(f"坐标 ({x}, {y}) 超出空间边界。")

        req_cells = {(x + dx, y + dy) for dx in range(fw) for dy in range(fh)}
        wall_flags = [self.is_wall_cell(space, cx, cy) for cx, cy in req_cells]

        # 墙面区域（含默认顶部墙带与用户涂刷的 MC 墙体方块）
        if all(wall_flags):
            if placement not in ("wall", "any"):
                raise ValueError("这个位置在墙面上，不能放地面家具。")
            for eid, ent in self._world["entities"].items():
                if eid == ignore_id or ent.get("space_id") != space_id or int(ent.get("z", 0)) != 0:
                    continue
                if req_cells & set(self._entity_cells(ent)):
                    raise ValueError(f"墙面 ({x}, {y}) 已经被「{ent['name']}」占用了。")
            return f"space:{space_id}", x, y, 0

        if any(wall_flags):
            raise ValueError("这个位置横跨墙面与地面，请调整摆放位置。")

        # 地面或承载面区域
        if placement == "wall":
            raise ValueError("这类挂墙物件只能放在墙面格上。")

        surf = self._surface_covering_locked(space_id, x, y, footprint, ignore_id=ignore_id)
        if surf is not None:
            if placement not in ("any", "surface_only"):
                raise ValueError(f"该物品不能摆在「{surf['name']}」桌面上。")
            cap = int((surf.get("traits") or {}).get("surface", {}).get("capacity") or 4)
            children = [c for c in self._surface_children_locked(surf["entity_id"]) if c["entity_id"] != ignore_id]
            if len(children) >= cap:
                raise ValueError(f"「{surf['name']}」上已经摆满（{cap}/{cap}）了。")
            for ch in children:
                if req_cells & set(self._entity_cells(ch)):
                    raise ValueError(f"桌面这一格已经摆着「{ch['name']}」了。")
            return f"entity:{surf['entity_id']}", x, y, 1

        if placement == "surface_only":
            raise ValueError("该物品只能摆在桌子等承载面上。")

        blocked_tiles = [
            self.get_cell_tile(space, cx, cy)
            for cx, cy in req_cells
            if not self.get_cell_tile(space, cx, cy).get("walkable", True)
        ]
        if blocked_tiles and (placement == "floor" or solid):
            raise ValueError(f"位置 ({x}, {y}) 是「{blocked_tiles[0]['name']}」，不能放地面家具。")

        for eid, ent in self._world["entities"].items():
            if eid == ignore_id or ent.get("space_id") != space_id or int(ent.get("z", 0)) != 0:
                continue
            if req_cells & set(self._entity_cells(ent)):
                raise ValueError(f"位置 ({x}, {y}) 已经有「{ent['name']}」了。")
        if solid:
            for act in self._world["actors"].values():
                if act.get("space_id") == space_id and (int(act["x"]), int(act["y"])) in req_cells:
                    raise ValueError(f"角色「{act['name']}」正站在这个位置。")
        return f"space:{space_id}", x, y, 0

    # ── 空间与蓝图管理 API ──────────────────────────────────────────────
    def create_space(
        self,
        *,
        name: str,
        width: int = 12,
        height: int = 9,
        ambient_phrase: str = "",
        palette_id: str = "warm_stardew",
    ) -> dict[str, Any]:
        with self._lock:
            clean_name = clip_text(name, 16, "新空间")
            base_id = slugify(clean_name, prefix="room")
            sid = base_id
            idx = 2
            while sid in self._world["spaces"]:
                sid = f"{base_id}_{idx}"
                idx += 1
            if palette_id not in self.all_palettes():
                palette_id = "warm_stardew"
            sp = normalize_space({
                "space_id": sid,
                "name": clean_name,
                "width": width,
                "height": height,
                "ambient_phrase": ambient_phrase or f"一间叫作「{clean_name}」的小空间。",
                "palette_id": palette_id,
            })
            self._world["spaces"][sid] = sp
            self._world["active_space_id"] = sid
            self._append_event_locked("space_created", space_id=sid, payload={"name": clean_name, "size": [sp["width"], sp["height"]]})
            self._save_world_locked()
            return copy.deepcopy(sp)

    def switch_space(self, space_id: str) -> dict[str, Any]:
        with self._lock:
            sid = str(space_id or "").strip()
            if sid not in self._world["spaces"]:
                raise ValueError(f"空间不存在：{sid}")
            self._world["active_space_id"] = sid
            self._save_world_locked()
            return copy.deepcopy(self._world["spaces"][sid])

    def delete_space(self, space_id: str) -> dict[str, Any]:
        with self._lock:
            sid = str(space_id or "").strip()
            spaces = self._world["spaces"]
            if sid not in spaces:
                raise ValueError(f"空间不存在：{sid}")
            if len(spaces) <= 1:
                raise ValueError("至少需要保留一个世界空间，无法删除最后一个世界。")

            active_sid = str(self._world.get("active_space_id") or "")
            if active_sid and active_sid != sid and active_sid in spaces:
                fallback_sid = active_sid
            else:
                fallback_sid = next(k for k in spaces.keys() if k != sid)
            fallback_space = spaces[fallback_sid]

            held_ids: set[str] = set()
            for act in self._world["actors"].values():
                hid = act.get("holding_id")
                if hid and hid in self._world["entities"]:
                    held_ids.add(hid)
                if act.get("space_id") == sid:
                    self._clear_actor_rest_locked(act)
                    nx, ny = self._find_open_floor_cell_locked(
                        fallback_space,
                        ignore_actor_id=act["actor_id"],
                    )
                    act["space_id"] = fallback_sid
                    act["x"] = nx
                    act["y"] = ny
                    act["posture"] = "standing"
                    if hid and hid in self._world["entities"]:
                        self._world["entities"][hid]["space_id"] = fallback_sid

            removed_ids: list[str] = []
            for eid, ent in list(self._world["entities"].items()):
                if eid in held_ids:
                    if ent.get("space_id") == sid:
                        ent["space_id"] = fallback_sid
                    continue
                if ent.get("space_id") == sid:
                    removed_ids.append(eid)
                    continue
                pt = (ent.get("traits") or {}).get("portal")
                if isinstance(pt, dict) and str(pt.get("target_space_id") or "") == sid:
                    pt["target_space_id"] = ""

            for eid in removed_ids:
                self._world["entities"].pop(eid, None)

            deleted_space = spaces.pop(sid)
            if self._world.get("active_space_id") == sid:
                self._world["active_space_id"] = fallback_sid

            self._append_event_locked(
                "space_deleted",
                space_id=fallback_sid,
                payload={
                    "deleted_space_id": sid,
                    "deleted_name": deleted_space.get("name", sid),
                    "removed_entities": len(removed_ids),
                },
            )
            self._save_world_locked()
            return {
                "deleted_space_id": sid,
                "deleted_name": str(deleted_space.get("name") or sid),
                "active_space_id": self._world["active_space_id"],
                "removed_entities": len(removed_ids),
            }

    def _expand_space_locked(
        self,
        space_id: str,
        direction: str,
        amount: int = 4,
        *,
        fill_tile_id: str = "",
    ) -> tuple[dict[str, Any], int, int]:
        sid = str(space_id or self._world["active_space_id"]).strip()
        space = self._world["spaces"].get(sid)
        if not space:
            raise ValueError(f"空间不存在：{sid}")
        d = str(direction or "east").strip().lower()
        if d not in ("north", "south", "east", "west", "all"):
            raise ValueError(f"不支持的扩图方向：{direction}")
        step = clamp_int(amount, 1, 32, 4)
        old_w = int(space["width"])
        old_h = int(space["height"])
        wall_rows = int(space.get("wall_rows") or 0)
        room_w = MAX_SPACE_WIDTH - old_w
        room_h = MAX_SPACE_HEIGHT - old_h

        add_w = add_e = add_n = add_s = 0
        if d == "west":
            add_w = min(step, room_w)
        elif d == "east":
            add_e = min(step, room_w)
        elif d == "north":
            add_n = min(step, room_h)
        elif d == "south":
            add_s = min(step, room_h)
        else:
            add_w = min(step, room_w)
            add_e = min(step, room_w - add_w)
            add_n = min(step, room_h)
            add_s = min(step, room_h - add_n)

        if add_w == 0 and add_e == 0 and add_n == 0 and add_s == 0:
            raise ValueError(f"当前空间已达到最大尺寸限制（{MAX_SPACE_WIDTH}×{MAX_SPACE_HEIGHT}）。")

        old_tiles = dict(space.get("tiles") or {})
        if add_n > 0 and wall_rows > 0:
            for yy in range(wall_rows):
                for xx in range(old_w):
                    old_tiles.setdefault(f"{xx},{yy}", "wood_wall")

        new_tiles: dict[str, str] = {}
        for k, tid in old_tiles.items():
            try:
                sx, sy = (int(p) for p in str(k).split(",", 1))
            except ValueError:
                continue
            new_tiles[f"{sx + add_w},{sy + add_n}"] = tid

        new_w = old_w + add_w + add_e
        new_h = old_h + add_n + add_s
        has_explicit_fill = fill_tile_id in TILE_CATALOG
        if has_explicit_fill or add_n > 0:
            eff_fill = fill_tile_id if has_explicit_fill else "wood_floor"
            for yy in range(new_h):
                for xx in range(new_w):
                    in_old = (add_w <= xx < add_w + old_w) and (add_n <= yy < add_n + old_h)
                    if in_old:
                        continue
                    if has_explicit_fill or yy < add_n:
                        new_tiles[f"{xx},{yy}"] = eff_fill

        if add_w != 0 or add_n != 0:
            for ent in self._world["entities"].values():
                if ent.get("space_id") == sid:
                    ent["x"] = int(ent.get("x") or 0) + add_w
                    ent["y"] = int(ent.get("y") or 0) + add_n
                pt = (ent.get("traits") or {}).get("portal")
                if isinstance(pt, dict) and pt.get("target_space_id") == sid:
                    pt["target_x"] = clamp_int(int(pt.get("target_x") or 0) + add_w, 0, new_w - 1, 0)
                    pt["target_y"] = clamp_int(int(pt.get("target_y") or 0) + add_n, 0, new_h - 1, 0)
            for act in self._world["actors"].values():
                if act.get("space_id") == sid:
                    act["x"] = int(act.get("x") or 0) + add_w
                    act["y"] = int(act.get("y") or 0) + add_n

        space["width"] = new_w
        space["height"] = new_h
        space["tiles"] = new_tiles
        if add_n > 0:
            space["wall_rows"] = 0
        self._append_event_locked(
            "space_expanded",
            space_id=sid,
            payload={"direction": d, "size": [new_w, new_h], "shift": [add_w, add_n]},
        )
        self._push_diff_locked(sid, f"空间向外拓展了一片，现在是 {new_w}×{new_h} 格")
        return space, add_w, add_n

    def expand_space(
        self,
        space_id: str,
        direction: str,
        amount: int = 4,
        *,
        fill_tile_id: str = "",
    ) -> dict[str, Any]:
        with self._lock:
            space, _, _ = self._expand_space_locked(space_id, direction, amount, fill_tile_id=fill_tile_id)
            self._save_world_locked()
            return copy.deepcopy(space)

    def paint_tiles(
        self,
        space_id: str,
        cells: Sequence[Mapping[str, Any]] | None = None,
        *,
        rect: Mapping[str, Any] | None = None,
        auto_expand: bool = False,
        demolish: bool = False,
    ) -> dict[str, Any]:
        with self._lock:
            sid = str(space_id or self._world["active_space_id"]).strip()
            space = self._world["spaces"].get(sid)
            if not space:
                raise ValueError(f"空间不存在：{sid}")

            raw_ops: list[tuple[int, int, str]] = []
            for item in cells or ():
                if not isinstance(item, Mapping):
                    continue
                try:
                    cx = int(item.get("x"))
                    cy = int(item.get("y"))
                except (TypeError, ValueError):
                    continue
                raw_ops.append((cx, cy, str(item.get("tile_id") or "").strip()))

            if isinstance(rect, Mapping):
                try:
                    rx0 = int(rect.get("x0"))
                    ry0 = int(rect.get("y0"))
                    rx1 = int(rect.get("x1"))
                    ry1 = int(rect.get("y1"))
                except (TypeError, ValueError) as exc:
                    raise ValueError("矩形坐标必须是整数。") from exc
                x0, x1 = min(rx0, rx1), max(rx0, rx1)
                y0, y1 = min(ry0, ry1), max(ry0, ry1)
                tid = str(rect.get("tile_id") or "").strip()
                hollow = bool(rect.get("hollow", False))
                for yy in range(y0, y1 + 1):
                    for xx in range(x0, x1 + 1):
                        if hollow and x0 < xx < x1 and y0 < yy < y1:
                            continue
                        raw_ops.append((xx, yy, tid))

            if not raw_ops:
                return {"space_id": sid, "painted": 0, "removed": [], "expanded": []}

            expanded_dirs: list[str] = []
            removed_eids: list[str] = []
            w = int(space["width"])
            h = int(space["height"])

            if demolish:
                wall_rows = int(space.get("wall_rows") or 0)
                tiles = space.setdefault("tiles", {})
                reset_count = 0
                for tx, ty, _ in raw_ops:
                    if not (0 <= tx < w and 0 <= ty < h):
                        continue
                    hits = [
                        ent for ent in self._world["entities"].values()
                        if ent.get("space_id") == sid
                        and int(ent.get("z", 0)) >= 0
                        and (tx, ty) in self._entity_cells(ent)
                    ]
                    if hits:
                        hits.sort(key=lambda e: int(e.get("z", 0)), reverse=True)
                        res = self.remove_entity(hits[0]["entity_id"])
                        removed_eids.extend(res.get("removed") or [])
                    else:
                        key = f"{tx},{ty}"
                        if key in tiles:
                            tiles.pop(key, None)
                            reset_count += 1
                        elif ty < wall_rows:
                            tiles[key] = "wood_floor"
                            reset_count += 1
                self._save_world_locked()
                return {
                    "space_id": sid,
                    "painted": reset_count,
                    "removed": removed_eids,
                    "expanded": expanded_dirs,
                }

            if auto_expand:
                if any(tx <= 0 for tx, _, _ in raw_ops) and int(space["width"]) < MAX_SPACE_WIDTH:
                    _, dw, _ = self._expand_space_locked(sid, "west", 4)
                    raw_ops = [(tx + dw, ty, tid) for tx, ty, tid in raw_ops]
                    expanded_dirs.append("west")
                if any(tx >= int(space["width"]) - 1 for tx, _, _ in raw_ops) and int(space["width"]) < MAX_SPACE_WIDTH:
                    self._expand_space_locked(sid, "east", 4)
                    expanded_dirs.append("east")
                if any(ty <= 0 for _, ty, _ in raw_ops) and int(space["height"]) < MAX_SPACE_HEIGHT:
                    _, _, dn = self._expand_space_locked(sid, "north", 4)
                    raw_ops = [(tx, ty + dn, tid) for tx, ty, tid in raw_ops]
                    expanded_dirs.append("north")
                if any(ty >= int(space["height"]) - 1 for _, ty, _ in raw_ops) and int(space["height"]) < MAX_SPACE_HEIGHT:
                    self._expand_space_locked(sid, "south", 4)
                    expanded_dirs.append("south")

            w = int(space["width"])
            h = int(space["height"])
            wall_rows = int(space.get("wall_rows") or 0)
            tiles = space.setdefault("tiles", {})
            changed = 0
            for tx, ty, tid in raw_ops:
                if not (0 <= tx < w and 0 <= ty < h):
                    continue
                key = f"{tx},{ty}"
                if tid in ("", "erase", "default"):
                    if key in tiles:
                        tiles.pop(key, None)
                        changed += 1
                    elif ty < wall_rows:
                        tiles[key] = "wood_floor"
                        changed += 1
                elif tid in TILE_CATALOG:
                    if tiles.get(key) != tid:
                        tiles[key] = tid
                        changed += 1

            # 若新画出的墙体或水池覆盖了角色当前位置，温和移开至邻近可站格子
            for aid, act in self._world["actors"].items():
                if act.get("space_id") != sid:
                    continue
                if not self._is_floor_walkable_locked(space, int(act["x"]), int(act["y"]), ignore_actor_id=aid):
                    nx, ny = self._find_open_floor_cell_locked(
                        space,
                        preferred_x=int(act["x"]),
                        preferred_y=int(act["y"]),
                        ignore_actor_id=aid,
                    )
                    self._clear_actor_rest_locked(act)
                    act["x"] = nx
                    act["y"] = ny
                    self._refresh_actor_expression_locked(act)

            if changed > 0:
                self._append_event_locked("tiles_painted", space_id=sid, payload={"count": changed, "expanded": expanded_dirs})
            self._save_world_locked()
            return {
                "space_id": sid,
                "painted": changed,
                "removed": removed_eids,
                "expanded": expanded_dirs,
            }

    def apply_generated_map(
        self,
        map_spec: Mapping[str, Any],
        *,
        mode: str = "new_space",
        target_space_id: str = "",
    ) -> dict[str, Any]:
        with self._lock:
            id_alias: dict[str, str] = {}
            for raw_bp in map_spec.get("custom_blueprints") or ():
                if not isinstance(raw_bp, Mapping):
                    continue
                orig_id = str(raw_bp.get("blueprint_id") or "").strip()
                saved_bp = self.save_blueprint(raw_bp)
                if orig_id:
                    id_alias[orig_id] = saved_bp["blueprint_id"]

            w = clamp_int(map_spec.get("width"), MIN_SPACE_WIDTH, MAX_SPACE_WIDTH, 16)
            h = clamp_int(map_spec.get("height"), MIN_SPACE_HEIGHT, MAX_SPACE_HEIGHT, 12)
            name = clip_text(map_spec.get("name"), 16, "灵感空间")
            pal_id = str(map_spec.get("palette_id") or "warm_stardew").strip()
            if pal_id not in self.all_palettes():
                pal_id = "warm_stardew"
            ambient = clip_text(map_spec.get("ambient_phrase"), 60, f"一处叫作「{name}」的空间。")
            wall_rows = clamp_int(map_spec.get("wall_rows"), 0, max(1, h // 3), 0)

            target_sid = str(target_space_id or self._world["active_space_id"]).strip()
            if str(mode or "").strip() == "replace_current" and target_sid in self._world["spaces"]:
                sid = target_sid
                self.clear_space(sid)
                space = self._world["spaces"][sid]
                space["name"] = name
                space["width"] = w
                space["height"] = h
                space["wall_rows"] = wall_rows
                space["ambient_phrase"] = ambient
                space["palette_id"] = pal_id
                space["tiles"] = {}
            else:
                created = self.create_space(
                    name=name,
                    width=w,
                    height=h,
                    ambient_phrase=ambient,
                    palette_id=pal_id,
                )
                sid = created["space_id"]
                space = self._world["spaces"][sid]
                space["wall_rows"] = wall_rows
                space["tiles"] = {}

            tiles_map: dict[str, str] = {}
            base_tile = str(map_spec.get("base_tile") or "").strip()
            if base_tile in TILE_CATALOG:
                for yy in range(h):
                    for xx in range(w):
                        tiles_map[f"{xx},{yy}"] = base_tile

            for reg in map_spec.get("tile_regions") or ():
                if not isinstance(reg, Mapping):
                    continue
                tid = str(reg.get("tile_id") or "").strip()
                if tid not in TILE_CATALOG:
                    continue
                rx = clamp_int(reg.get("x"), 0, w - 1, 0)
                ry = clamp_int(reg.get("y"), 0, h - 1, 0)
                rw = clamp_int(reg.get("w"), 1, w - rx, 1)
                rh = clamp_int(reg.get("h"), 1, h - ry, 1)
                hollow = bool(reg.get("hollow", False))
                for yy in range(ry, ry + rh):
                    for xx in range(rx, rx + rw):
                        if hollow and rx < xx < rx + rw - 1 and ry < yy < ry + rh - 1:
                            continue
                        tiles_map[f"{xx},{yy}"] = tid
                if hollow and TILE_CATALOG[tid].get("is_wall") and rw >= 4 and rh >= 4:
                    bot_y = ry + rh - 1
                    has_gap = any(
                        TILE_CATALOG.get(tiles_map.get(f"{xx},{bot_y}", "wood_floor"), TILE_CATALOG["wood_floor"]).get("walkable", True)
                        for xx in range(rx + 1, rx + rw - 1)
                    )
                    if not has_gap:
                        mid_x = rx + rw // 2
                        open_tid = base_tile if base_tile in TILE_CATALOG and TILE_CATALOG[base_tile].get("walkable", True) else "wood_floor"
                        tiles_map[f"{mid_x},{bot_y}"] = open_tid
                        if rw >= 5 and mid_x - 1 > rx:
                            tiles_map[f"{mid_x - 1},{bot_y}"] = open_tid

            explicit_tiles = normalize_space_tiles(map_spec.get("tiles"), w, h)
            tiles_map.update(explicit_tiles)
            space["tiles"] = tiles_map

            all_bps = self.all_blueprints()

            def _place_priority(item: Mapping[str, Any]) -> int:
                raw_bid = str(item.get("blueprint_id") or "").strip()
                bp = all_bps.get(id_alias.get(raw_bid, raw_bid))
                if not bp:
                    return 9
                if "surface" in (bp.get("traits") or {}):
                    return 0
                if bp.get("placement") == "wall":
                    return 1
                if bp.get("placement") == "floor" and bp.get("solid"):
                    return 2
                return 3

            raw_placements = [p for p in (map_spec.get("placements") or ()) if isinstance(p, Mapping)]
            raw_placements.sort(key=_place_priority)
            placed: list[dict[str, Any]] = []
            skipped = 0
            offsets = [(dx, dy) for dx in range(-2, 3) for dy in range(-2, 3)]
            offsets.sort(key=lambda d: abs(d[0]) + abs(d[1]))

            for item in raw_placements:
                raw_bid = str(item.get("blueprint_id") or "").strip()
                bid = id_alias.get(raw_bid, raw_bid)
                if bid not in all_bps:
                    skipped += 1
                    continue
                tx = clamp_int(item.get("x"), 0, w - 1, w // 2)
                ty = clamp_int(item.get("y"), 0, h - 1, max(1, h // 2))
                note = str(item.get("note_text") or "")
                portal_tgt = str(item.get("portal_target_space_id") or "")
                done_item = None
                for dx, dy in offsets:
                    cx, cy = tx + dx, ty + dy
                    if not (0 <= cx < w and 0 <= cy < h):
                        continue
                    try:
                        done_item = self.place_from_blueprint(
                            bid,
                            sid,
                            cx,
                            cy,
                            delivery_mode="silent",
                            note_text=note,
                            portal_target_space_id=portal_tgt,
                        )
                        break
                    except ValueError:
                        continue
                if done_item is not None:
                    placed.append(done_item)
                else:
                    skipped += 1

            aid = self.resolve_actor_id()
            if aid and aid in self._world["actors"]:
                actor = self._world["actors"][aid]
                self._clear_actor_rest_locked(actor)
                ax, ay = self._find_open_floor_cell_locked(space, preferred_x=w // 2, preferred_y=h // 2, ignore_actor_id=aid)
                actor["space_id"] = sid
                actor["x"] = ax
                actor["y"] = ay
                self._refresh_actor_expression_locked(actor)

            self._world["active_space_id"] = sid
            self._append_event_locked(
                "map_generated",
                space_id=sid,
                payload={"name": name, "size": [w, h], "placed": len(placed), "skipped": skipped},
            )
            self._push_diff_locked(sid, f"整片空间变成了「{name}」（{w}×{h}），放下了 {len(placed)} 件物品")
            self._save_world_locked()
            return {
                "space": copy.deepcopy(space),
                "placed_count": len(placed),
                "skipped_count": skipped,
            }

    def set_space_palette(self, space_id: str, palette_id: str) -> dict[str, Any]:
        with self._lock:
            sid = str(space_id or self._world["active_space_id"]).strip()
            if sid not in self._world["spaces"]:
                raise ValueError(f"空间不存在：{sid}")
            if palette_id not in self.all_palettes():
                raise ValueError(f"调色板不存在：{palette_id}")
            self._world["spaces"][sid]["palette_id"] = palette_id
            self._save_world_locked()
            return copy.deepcopy(self._world["spaces"][sid])

    def save_palette(self, raw_palette: Mapping[str, Any]) -> dict[str, Any]:
        with self._lock:
            pal = normalize_palette(raw_palette, builtin=False)
            pid = pal["palette_id"]
            if pid in self._builtin_palettes:
                pid = f"{pid}_custom"
                pal["palette_id"] = pid
            self._custom_palettes[pid] = pal
            self._save_palettes_locked()
            return copy.deepcopy(pal)

    def save_blueprint(self, raw_blueprint: Mapping[str, Any]) -> dict[str, Any]:
        with self._lock:
            bp = normalize_blueprint(raw_blueprint, builtin=False)
            bid = bp["blueprint_id"]
            if bid in self._builtin_blueprints:
                bid = f"{bid}_custom"
                bp["blueprint_id"] = bid
            self._custom_blueprints[bid] = bp
            self._save_blueprints_locked()
            return copy.deepcopy(bp)

    def delete_blueprint(self, blueprint_id: str) -> bool:
        with self._lock:
            bid = str(blueprint_id or "").strip()
            if bid in self._custom_blueprints:
                del self._custom_blueprints[bid]
                self._save_blueprints_locked()
                return True
            return False

    def _find_host_pack(self, host_name: str) -> dict[str, Any] | None:
        target = str(host_name or "").strip()
        if not target:
            return None
        for row in self.list_host_characters():
            if row.get("name") == target:
                return row
        return None

    def _pick_pack_sprite_locked(
        self,
        actor: Mapping[str, Any],
        *,
        emotion_hint: str = "",
        action_kind: str = "",
    ) -> tuple[str, dict[str, Any] | None]:
        pack = list((actor.get("sprite") or {}).get("pack_sprites") or [])
        vit = actor.get("vitals") or {}
        energy = float(vit.get("energy", 0.72))
        sleep_p = float(vit.get("sleep_pressure", 0.30))
        mood_v = float(vit.get("mood_v", 0.15))
        agenda = str(vit.get("agenda") or "")
        disengaged = bool(vit.get("disengaged", False))
        posture = str(actor.get("posture") or "standing")

        desired_bucket = "neutral"
        hint_raw = str(emotion_hint or "").strip()
        if hint_raw:
            lowered = hint_raw.lower()
            if lowered in _EMOTION_HINT_ALIASES:
                desired_bucket = _EMOTION_HINT_ALIASES[lowered]
            else:
                desired_bucket = classify_emotion_bucket(hint_raw)
        elif posture == "resting" or energy < 0.22 or sleep_p >= 0.80:
            desired_bucket = "sleep_deep"
        elif energy < 0.45 or sleep_p >= 0.58:
            desired_bucket = "sleepy"
        elif disengaged:
            desired_bucket = "cold"
        elif mood_v <= -0.32:
            desired_bucket = "sad"
        elif mood_v <= -0.12:
            desired_bucket = "angry"
        elif action_kind == "interact" or (mood_v >= 0.40 and energy >= 0.55):
            desired_bucket = "excited"
        elif action_kind in ("look", "read", "write") or agenda == "want_understand":
            desired_bucket = "curious"
        elif mood_v >= 0.18:
            desired_bucket = "happy"
        elif energy >= 0.60 and mood_v >= 0.05:
            desired_bucket = "curious"

        if not pack:
            return desired_bucket, None

        if hint_raw:
            for item in pack:
                if hint_raw in str(item.get("tag") or ""):
                    return desired_bucket, item

        fallback_order = {
            "sleep_deep": ("sleep_deep", "sleepy", "sad", "neutral"),
            "sleepy": ("sleepy", "sleep_deep", "neutral"),
            "shy": ("shy", "happy", "neutral"),
            "excited": ("excited", "happy", "curious", "neutral"),
            "happy": ("happy", "excited", "neutral"),
            "angry": ("angry", "cold", "sad", "neutral"),
            "sad": ("sad", "worried", "cold", "neutral"),
            "cold": ("cold", "angry", "sad", "neutral"),
            "worried": ("worried", "sad", "curious", "neutral"),
            "curious": ("curious", "explain", "neutral"),
            "explain": ("explain", "curious", "neutral"),
            "neutral": ("neutral", "happy"),
        }.get(desired_bucket, (desired_bucket, "neutral"))

        for bucket in fallback_order:
            for item in pack:
                if item.get("bucket") == bucket:
                    return desired_bucket, item
        return desired_bucket, pack[0]

    def _refresh_actor_expression_locked(
        self,
        actor: dict[str, Any],
        *,
        emotion_hint: str = "",
        action_kind: str = "",
        force_auto: bool = False,
    ) -> dict[str, Any]:
        cur_expr = dict(actor.get("expression") or {})
        if action_kind:
            actor["last_action_kind"] = action_kind
        effective_action = action_kind or str(actor.get("last_action_kind") or "")
        if cur_expr.get("manual") and not force_auto and not emotion_hint:
            return cur_expr

        bucket, picked = self._pick_pack_sprite_locked(
            actor,
            emotion_hint=emotion_hint,
            action_kind=effective_action,
        )
        if picked is not None:
            new_expr = {
                "index": int(picked["index"]),
                "tag": str(picked["tag"]),
                "bucket": str(picked.get("bucket") or bucket),
                "path": str(picked["path"]),
                "manual": False,
            }
        else:
            bucket_labels = {
                "sleep_deep": "熟睡", "sleepy": "困倦", "shy": "害羞", "excited": "兴奋",
                "happy": "开心", "angry": "生气", "sad": "失落", "cold": "冷淡",
                "worried": "担心", "curious": "好奇", "explain": "认真", "neutral": "平静",
            }
            new_expr = {
                "index": 1,
                "tag": str(emotion_hint or bucket_labels.get(bucket, "平静")),
                "bucket": bucket,
                "path": "",
                "manual": False,
            }
        changed = (
            int(cur_expr.get("index") or 1) != new_expr["index"]
            or str(cur_expr.get("bucket") or "") != new_expr["bucket"]
            or str(cur_expr.get("path") or "") != new_expr["path"]
        )
        actor["expression"] = new_expr
        if changed and cur_expr:
            self._append_event_locked(
                "actor_expression_changed",
                actor_id=actor["actor_id"],
                space_id=actor["space_id"],
                payload={"index": new_expr["index"], "tag": new_expr["tag"], "bucket": new_expr["bucket"]},
            )
        return new_expr

    def refresh_actor_expression(
        self,
        actor_id: str,
        *,
        emotion_hint: str = "",
        action_kind: str = "",
        force_auto: bool = False,
    ) -> dict[str, Any] | None:
        with self._lock:
            actor = self._world["actors"].get(actor_id)
            if not actor:
                return None
            expr = self._refresh_actor_expression_locked(
                actor,
                emotion_hint=emotion_hint,
                action_kind=action_kind,
                force_auto=force_auto,
            )
            self._save_world_locked()
            return copy.deepcopy(expr)

    def set_actor_expression(
        self,
        actor_id: str,
        *,
        index: int | None = None,
        mode: str = "manual",
    ) -> dict[str, Any]:
        with self._lock:
            actor = self._world["actors"].get(actor_id)
            if not actor:
                raise ValueError(f"角色不存在：{actor_id}")
            if mode == "auto" or index is None or int(index) <= 0:
                expr = self._refresh_actor_expression_locked(actor, force_auto=True)
                self._save_world_locked()
                return copy.deepcopy(expr)
            pack = list((actor.get("sprite") or {}).get("pack_sprites") or [])
            picked = next((p for p in pack if int(p.get("index") or 0) == int(index)), None)
            if picked is None:
                raise ValueError(f"角色包中没有编号为 {index} 的立绘。")
            actor["expression"] = {
                "index": int(picked["index"]),
                "tag": str(picked["tag"]),
                "bucket": str(picked["bucket"]),
                "path": str(picked["path"]),
                "manual": True,
            }
            self._append_event_locked(
                "actor_expression_changed",
                actor_id=actor_id,
                space_id=actor["space_id"],
                payload={"index": int(picked["index"]), "tag": picked["tag"], "manual": True},
            )
            self._save_world_locked()
            return copy.deepcopy(actor["expression"])

    def get_actor_expression_gallery(self, actor_id: str) -> list[dict[str, Any]]:
        with self._lock:
            actor = self._world["actors"].get(actor_id)
            if not actor:
                return []
            pack = list((actor.get("sprite") or {}).get("pack_sprites") or [])
            out: list[dict[str, Any]] = []
            for item in pack:
                out.append({
                    **item,
                    "data_url": self._host_sprite_data_url(str(item.get("path") or "")),
                })
            return out

    # ── 角色导入与坐标落格 ──────────────────────────────────────────────
    def _alloc_actor_id_locked(self, clean_name: str, host_character: str = "") -> str:
        # 1. 同名（或明确绑定同一宿主角色且同显示名）已存在时复用其 actor_id，支持召唤切换空间
        for eid_a, act in self._world["actors"].items():
            if act.get("name") == clean_name:
                return eid_a
        # 2. 新建角色时保证 ID 唯一（中文名不可退化覆盖既有 `"actor"`）
        base_ascii = re.sub(r"[^a-zA-Z0-9_]+", "_", str(clean_name or "").strip().lower()).strip("_")
        if not base_ascii and host_character:
            base_ascii = re.sub(r"[^a-zA-Z0-9_]+", "_", str(host_character).strip().lower()).strip("_")
        if base_ascii:
            cand_base = f"actor_{base_ascii}"[:36]
        elif not self._world["actors"]:
            cand_base = "actor"
        else:
            short_h = hashlib.md5(clean_name.encode("utf-8")).hexdigest()[:6]
            cand_base = f"actor_{short_h}"
        aid = cand_base
        idx = 2
        while aid in self._world["actors"]:
            aid = f"{cand_base[:32]}_{idx}"
            idx += 1
        return aid

    def import_actor(
        self,
        *,
        name: str,
        host_character: str = "",
        actor_id: str = "",
        space_id: str = "",
        x: int | None = None,
        y: int | None = None,
        sprite: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            clean_name = clip_text(name or host_character, 20, "七海千秋")
            explicit_aid = str(actor_id or "").strip()
            aid = explicit_aid or self._alloc_actor_id_locked(clean_name, host_character)
            if not space_id or space_id not in self._world["spaces"]:
                space_id = self._world["active_space_id"]
            space = self._world["spaces"][space_id]
            tx, ty = self._find_open_floor_cell_locked(space, preferred_x=x, preferred_y=y, ignore_actor_id=aid)

            pack_info = self._find_host_pack(host_character or clean_name)
            resolved_sprite: dict[str, Any] | None = dict(sprite) if sprite else None
            if (resolved_sprite is None or resolved_sprite.get("mode") == "host_pack") and pack_info and pack_info.get("pack_sprites"):
                resolved_sprite = {
                    "mode": "host_pack",
                    "w": 16,
                    "h": 24,
                    "pixels": list(DEFAULT_ACTOR_PIXELS_16X24),
                    "slot_colors": dict(DEFAULT_ACTOR_SLOT_COLORS),
                    "accent_color": pack_info.get("color") or "#e8a2b8",
                    "pack_sprites": pack_info["pack_sprites"],
                    "portrait_crop": pack_info.get("portrait_crop") or {"x": 0.5, "y": 0.2, "zoom": 1.0},
                }

            existing = self._world["actors"].get(aid)
            if existing is not None:
                existing["name"] = clean_name
                if host_character:
                    existing["host_character"] = host_character
                if existing.get("space_id") != space_id or x is not None or y is not None:
                    self._clear_actor_rest_locked(existing)
                    existing["space_id"] = space_id
                    existing["x"] = tx
                    existing["y"] = ty
                if resolved_sprite:
                    existing["sprite"] = normalize_actor({"name": clean_name, "sprite": resolved_sprite})["sprite"]
                actor = existing
            else:
                actor = normalize_actor({
                    "actor_id": aid,
                    "host_character": host_character or clean_name,
                    "name": clean_name,
                    "space_id": space_id,
                    "x": tx,
                    "y": ty,
                    "sprite": resolved_sprite if resolved_sprite else {
                        "mode": "pixel_grid",
                        "w": 16,
                        "h": 24,
                        "pixels": list(DEFAULT_ACTOR_PIXELS_16X24),
                        "slot_colors": dict(DEFAULT_ACTOR_SLOT_COLORS),
                    },
                })
                self._world["actors"][aid] = actor

            self._refresh_actor_expression_locked(actor, force_auto=True)
            self._world["active_actor_id"] = aid
            self._append_event_locked("actor_imported", actor_id=aid, space_id=space_id, payload={"name": clean_name, "x": actor["x"], "y": actor["y"], "mode": actor["sprite"]["mode"]})
            self._save_world_locked()
            return copy.deepcopy(actor)

    def _find_open_floor_cell_locked(
        self,
        space: Mapping[str, Any],
        *,
        preferred_x: int | None = None,
        preferred_y: int | None = None,
        ignore_actor_id: str | None = None,
    ) -> tuple[int, int]:
        w = int(space["width"])
        h = int(space["height"])
        wall_rows = int(space["wall_rows"])
        if preferred_x is not None and preferred_y is not None:
            px = clamp_int(preferred_x, 0, w - 1, w // 2)
            py = clamp_int(preferred_y, 0, h - 1, max(wall_rows, h // 2))
            if self._is_floor_walkable_locked(space, px, py, ignore_actor_id=ignore_actor_id):
                return px, py
        cx, cy = w // 2, max(wall_rows, h // 2)
        if self._is_floor_walkable_locked(space, cx, cy, ignore_actor_id=ignore_actor_id):
            return cx, cy
        for yy in list(range(wall_rows, h)) + list(range(0, wall_rows)):
            for xx in range(w):
                if self._is_floor_walkable_locked(space, xx, yy, ignore_actor_id=ignore_actor_id):
                    return xx, yy
        return cx, cy

    def place_actor(self, actor_id: str, space_id: str, x: int, y: int) -> dict[str, Any]:
        with self._lock:
            actor = self._world["actors"].get(actor_id)
            if not actor:
                raise ValueError(f"角色不存在：{actor_id}")
            space = self._world["spaces"].get(space_id)
            if not space:
                raise ValueError(f"空间不存在：{space_id}")
            if not self._is_floor_walkable_locked(space, int(x), int(y), ignore_actor_id=actor_id):
                raise ValueError(f"坐标 ({x}, {y}) 不可落脚（墙面或被实心家具占用）。")
            self._clear_actor_rest_locked(actor)
            actor["space_id"] = space_id
            actor["x"] = int(x)
            actor["y"] = int(y)
            self._refresh_actor_expression_locked(actor, action_kind="move")
            self._append_event_locked("actor_moved", actor_id=actor_id, space_id=space_id, payload={"x": int(x), "y": int(y), "by": "user"})
            self._save_world_locked()
            return copy.deepcopy(actor)

    def set_active_actor(self, actor_id: str) -> dict[str, Any]:
        with self._lock:
            if actor_id not in self._world["actors"]:
                raise ValueError(f"角色不存在：{actor_id}")
            self._world["active_actor_id"] = actor_id
            self._save_world_locked()
            return copy.deepcopy(self._world["actors"][actor_id])

    def remove_actor(self, actor_id: str) -> dict[str, Any]:
        with self._lock:
            aid = str(actor_id or "").strip()
            actors = self._world["actors"]
            if aid not in actors:
                raise ValueError(f"角色不存在：{aid}")
            if len(actors) <= 1:
                raise ValueError("至少需要保留一名角色，无法移除最后一位角色。")

            act = actors[aid]
            sid = str(act.get("space_id") or self._world["active_space_id"])
            self._clear_actor_rest_locked(act)
            hid = act.get("holding_id")
            if hid and hid in self._world["entities"]:
                held = self._world["entities"][hid]
                held["parent_id"] = f"space:{sid}"
                held["space_id"] = sid
                held["x"] = int(act.get("x") or 0)
                held["y"] = int(act.get("y") or 1)
                held["z"] = 0
                act["holding_id"] = None

            removed = actors.pop(aid)
            if self._world.get("active_actor_id") == aid:
                self._world["active_actor_id"] = next(iter(actors.keys()))
            self._append_event_locked(
                "actor_removed",
                actor_id=aid,
                space_id=sid,
                payload={"name": removed.get("name") or aid},
            )
            self._save_world_locked()
            return {
                "removed_actor_id": aid,
                "name": str(removed.get("name") or aid),
                "active_actor_id": self._world["active_actor_id"],
            }

    def record_custom_event(
        self,
        kind: str,
        *,
        actor_id: str = "",
        space_id: str = "",
        payload: Mapping[str, Any] | None = None,
    ) -> None:
        with self._lock:
            sid = str(space_id or self._world.get("active_space_id") or "")
            self._append_event_locked(kind, actor_id=actor_id, space_id=sid, payload=dict(payload or {}))
            self._save_world_locked()

    def list_actors(self) -> list[dict[str, Any]]:
        with self._lock:
            return [copy.deepcopy(a) for a in self._world["actors"].values()]

    def set_actor_vitals(self, actor_id: str, vitals: Mapping[str, Any]) -> None:
        with self._lock:
            target = self._world["actors"].get(actor_id)
            if not target:
                return
            target["vitals"] = dict(vitals)
            self._refresh_actor_expression_locked(target)
            self._save_world_locked()

    def update_actor_sprite(self, actor_id: str, sprite: Mapping[str, Any]) -> dict[str, Any]:
        with self._lock:
            actor = self._world["actors"].get(actor_id)
            if not actor:
                raise ValueError(f"角色不存在：{actor_id}")
            norm = normalize_actor({"name": actor["name"], "sprite": sprite})["sprite"]
            actor["sprite"] = norm
            self._save_world_locked()
            return copy.deepcopy(actor)

    def resolve_actor_id(self, character_name: str = "") -> str:
        with self._lock:
            actors = self._world["actors"]
            if not actors:
                return ""
            target = str(character_name or "").strip()
            if target:
                for aid, act in actors.items():
                    if act.get("host_character") == target or act.get("name") == target:
                        return aid
            active = str(self._world.get("active_actor_id") or "")
            if active in actors:
                return active
            return next(iter(actors))

    # ── 物质放置、移动、编辑与移除 ──────────────────────────────────────
    def _next_entity_id_locked(self, blueprint: Mapping[str, Any]) -> str:
        prefix = slugify(blueprint.get("name") or blueprint.get("blueprint_id") or "item", prefix="ent")
        if len(prefix) > 12:
            prefix = prefix[:12].rstrip("_")
        # 优先使用短英文前缀（按 blueprint_id 尾部）
        bp_id = str(blueprint.get("blueprint_id") or "")
        tail = bp_id.replace("default_", "").replace("custom_", "")
        short = slugify(tail or prefix, prefix="ent")[:16]
        num = int(self._world.get("next_entity_num") or 1)
        while True:
            cand = f"{short}_{num}"
            num += 1
            if cand not in self._world["entities"]:
                self._world["next_entity_num"] = num
                return cand

    def place_from_blueprint(
        self,
        blueprint_id: str,
        space_id: str,
        x: int,
        y: int,
        *,
        delivery_mode: str = "silent",
        note_text: str = "",
        portal_target_space_id: str = "",
    ) -> dict[str, Any]:
        with self._lock:
            bp = self.all_blueprints().get(blueprint_id)
            if not bp:
                raise ValueError(f"素材蓝图不存在：{blueprint_id}")
            sid = str(space_id or self._world["active_space_id"])
            parent_id, px, py, pz = self._resolve_placement_locked(
                sid,
                int(x),
                int(y),
                bp["footprint"],
                bp["placement"],
                bp["solid"],
            )
            eid = self._next_entity_id_locked(bp)
            traits = copy.deepcopy(bp["traits"])
            if "thermal" in traits:
                traits["thermal"]["temp_updated_ts"] = time.time()
            if "portal" in traits and portal_target_space_id:
                traits["portal"]["target_space_id"] = portal_target_space_id
            if "inscribable" in traits and note_text.strip():
                traits["inscribable"]["entries"].append({
                    "by": "user",
                    "ts": time.time(),
                    "text": clip_text(note_text, 120, ""),
                })
            entity = self._normalize_entity_dict({
                "entity_id": eid,
                "blueprint_id": bp["blueprint_id"],
                "name": bp["name"],
                "brief": bp["brief"],
                "detail": bp["detail"],
                "tags": bp["tags"],
                "parent_id": parent_id,
                "space_id": sid,
                "x": px,
                "y": py,
                "z": pz,
                "footprint": bp["footprint"],
                "placement": bp["placement"],
                "solid": bp["solid"],
                "traits": traits,
                "effects": bp["effects"],
                "combine_rules": bp["combine_rules"],
                "sprite": bp["sprite"],
                "traces": [{
                    "ts": time.time(),
                    "text": "由你直接递来" if delivery_mode == "give" else "由你放在空间里",
                }],
            })
            self._world["entities"][eid] = entity

            where_label = "空间里"
            if parent_id.startswith("entity:"):
                parent_ent = self._world["entities"].get(parent_id.split(":", 1)[1])
                if parent_ent:
                    where_label = f"「{parent_ent['name']}」上"
            diff_msg = f"{where_label}多了一件「{entity['name']}」（{entity['brief']}）"
            if note_text.strip():
                diff_msg += f"，上面写着「{clip_text(note_text, 40, '')}」"
            self._push_diff_locked(sid, diff_msg)
            self._append_event_locked(
                "entity_placed",
                space_id=sid,
                payload={
                    "entity_id": eid,
                    "name": entity["name"],
                    "x": px,
                    "y": py,
                    "z": pz,
                    "delivery_mode": delivery_mode,
                },
            )
            self._save_world_locked()
            return copy.deepcopy(entity)

    def move_entity_ui(self, entity_id: str, x: int, y: int, target_parent_id: str = "") -> dict[str, Any]:
        with self._lock:
            ent = self._world["entities"].get(entity_id)
            if not ent:
                raise ValueError(f"物体不存在：{entity_id}")
            sid = ent["space_id"]
            if target_parent_id == f"entity:{entity_id}":
                raise ValueError("物体不能放进自己里面。")
            # 若物体自身正承载着 z=1 子物品，先检查暂不允许整桌压入其他物品
            if self._surface_children_locked(entity_id) and target_parent_id:
                raise ValueError("桌面上还摆着东西，请先移开桌面上的物品。")

            old_x, old_y = int(ent["x"]), int(ent["y"])
            parent_id, px, py, pz = self._resolve_placement_locked(
                sid,
                int(x),
                int(y),
                ent["footprint"],
                ent["placement"],
                bool(ent["solid"]),
                target_parent_id=target_parent_id,
                ignore_id=entity_id,
            )
            ent["parent_id"] = parent_id
            ent["x"] = px
            ent["y"] = py
            ent["z"] = pz

            # 若移动的是承载面，同步带着其 z=1 子物品平移
            dx, dy = px - old_x, py - old_y
            if dx != 0 or dy != 0:
                for ch in self._surface_children_locked(entity_id):
                    ch["x"] = int(ch["x"]) + dx
                    ch["y"] = int(ch["y"]) + dy
                for ch in self._container_children_locked(entity_id):
                    ch["x"] = px
                    ch["y"] = py

            self._append_event_locked(
                "entity_moved",
                space_id=sid,
                payload={"entity_id": entity_id, "x": px, "y": py, "z": pz, "parent_id": parent_id},
            )
            self._save_world_locked()
            return copy.deepcopy(ent)

    def remove_entity(self, entity_id: str) -> dict[str, Any]:
        with self._lock:
            ent = self._world["entities"].get(entity_id)
            if not ent:
                raise ValueError(f"物体不存在：{entity_id}")
            sid = ent["space_id"]
            # 解绑正在此物上休息的角色或手持
            for act in self._world["actors"].values():
                if act.get("holding_id") == entity_id:
                    act["holding_id"] = None
                if act.get("resting_on_id") == entity_id:
                    act["resting_on_id"] = None
                    act["posture"] = "standing"
            # 连带清理内部子物品（若为容器/承载面）
            to_remove = [entity_id]
            parent_tag = f"entity:{entity_id}"
            for cid, ch in self._world["entities"].items():
                if ch.get("parent_id") == parent_tag:
                    to_remove.append(cid)
            for rid in to_remove:
                self._world["entities"].pop(rid, None)
            self._push_diff_locked(sid, f"「{ent['name']}」被收走了")
            self._append_event_locked("entity_removed", space_id=sid, payload={"entity_id": entity_id, "name": ent["name"], "cascade": to_remove})
            self._save_world_locked()
            return {"removed": to_remove}

    def update_entity_spec(
        self,
        entity_id: str,
        *,
        name: str | None = None,
        brief: str | None = None,
        detail: str | None = None,
        traits: Mapping[str, Any] | None = None,
        portal_target_space_id: str | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            ent = self._world["entities"].get(entity_id)
            if not ent:
                raise ValueError(f"物体不存在：{entity_id}")
            if name is not None:
                ent["name"] = clip_text(name, 12, ent["name"])
            if brief is not None:
                ent["brief"] = clip_text(brief, MAX_BRIEF_CHARS, ent["brief"])
            if detail is not None:
                ent["detail"] = clip_text(detail, MAX_DETAIL_CHARS, ent["detail"])
            if traits is not None:
                ent["traits"] = normalize_traits(traits)
            if portal_target_space_id is not None and "portal" in ent["traits"]:
                ent["traits"]["portal"]["target_space_id"] = str(portal_target_space_id).strip()
            if "restable" in ent["traits"]:
                ent["solid"] = False
            elif "portal" in ent["traits"]:
                ent["placement"] = "any"
                ent["solid"] = not bool(ent["traits"]["portal"].get("is_open", False))
            self._save_world_locked()
            return copy.deepcopy(ent)

    def clear_space(self, space_id: str) -> dict[str, Any]:
        with self._lock:
            sid = str(space_id or self._world["active_space_id"])
            if sid not in self._world["spaces"]:
                raise ValueError(f"空间不存在：{sid}")
            removed = [eid for eid, ent in self._world["entities"].items() if ent.get("space_id") == sid]
            for eid in removed:
                self._world["entities"].pop(eid, None)
            for act in self._world["actors"].values():
                if act.get("space_id") == sid:
                    act["holding_id"] = None
                    act["resting_on_id"] = None
                    act["posture"] = "standing"
            self._append_event_locked("space_cleared", space_id=sid, payload={"count": len(removed)})
            self._save_world_locked()
            return {"space_id": sid, "removed_count": len(removed)}

    def apply_starter_pack(self, space_id: str = "") -> list[dict[str, Any]]:
        with self._lock:
            sid = str(space_id or self._world["active_space_id"])
            self.clear_space(sid)
            placed: list[dict[str, Any]] = []
            for spec in self._builtin_room_cfg.get("starter_pack") or []:
                if not isinstance(spec, Mapping):
                    continue
                try:
                    ent = self.place_from_blueprint(
                        str(spec.get("blueprint_id") or ""),
                        sid,
                        int(spec.get("x") or 1),
                        int(spec.get("y") or 1),
                        delivery_mode=str(spec.get("delivery_mode") or "silent"),
                    )
                    placed.append(ent)
                except ValueError:
                    logger.debug("nanami_world: starter_pack 放置失败: %s", spec, exc_info=True)
            return placed

    # ── 视野过滤与动态合法动词 ──────────────────────────────────────────
    def visible_entities(self, actor_id: str) -> list[dict[str, Any]]:
        with self._lock:
            actor = self._world["actors"].get(actor_id)
            if not actor:
                return []
            sid = actor["space_id"]
            entities = self._world["entities"]
            out: list[dict[str, Any]] = []
            for ent in entities.values():
                if ent.get("space_id") != sid:
                    continue
                pid = str(ent.get("parent_id") or "")
                z = int(ent.get("z") or 0)
                if pid == f"actor:{actor_id}":
                    out.append(copy.deepcopy(ent))
                    continue
                if z == -1 and pid.startswith("entity:"):
                    parent_ent = entities.get(pid.split(":", 1)[1])
                    if not parent_ent:
                        continue
                    c_trait = (parent_ent.get("traits") or {}).get("container")
                    if not c_trait or not bool(c_trait.get("is_open")):
                        continue
                    out.append(copy.deepcopy(ent))
                    continue
                if z >= 0:
                    out.append(copy.deepcopy(ent))
            return out

    def entity_display_brief(self, entity: Mapping[str, Any]) -> str:
        traits = entity.get("traits") or {}
        if "consumable" in traits and int(traits["consumable"].get("uses_left", 0)) <= 0:
            base = str(traits["consumable"].get("empty_brief") or entity.get("brief") or entity.get("name"))
        elif "switchable" in traits:
            sw = traits["switchable"]
            cand = sw.get("on_brief") if sw.get("is_on") else sw.get("off_brief")
            base = str(cand or entity.get("brief") or entity.get("name"))
        else:
            base = str(entity.get("brief") or entity.get("name"))
        return base

    def entity_state_tags(self, entity: Mapping[str, Any]) -> list[str]:
        traits = entity.get("traits") or {}
        parts: list[str] = []
        if "thermal" in traits:
            tmap = {"hot": "热的", "warm": "微温", "cold": "凉了"}
            parts.append(tmap.get(str(traits["thermal"].get("temp")), "凉了"))
        if "consumable" in traits:
            cm = traits["consumable"]
            left = int(cm.get("uses_left") or 0)
            parts.append(f"剩 {left} 次" if left > 0 else "已空")
        if "switchable" in traits:
            sw = traits["switchable"]
            if sw.get("provides_light"):
                parts.append("亮着" if sw.get("is_on") else "灭着")
            elif "activity" in traits:
                parts.append("开屏" if sw.get("is_on") else "关屏")
            else:
                parts.append("开启" if sw.get("is_on") else "关闭")
        if "container" in traits:
            ct = traits["container"]
            if ct.get("is_locked"):
                kt = ct.get("key_tag") or "钥匙"
                parts.append(f"上锁（需要 {kt}）")
            else:
                parts.append("开着" if ct.get("is_open") else "合着")
        if "restable" in traits:
            occ = traits["restable"].get("occupied_by")
            parts.append("有人休息中" if occ else "空着")
        if "activity" in traits:
            ac = traits["activity"]
            if ac.get("battery") is not None:
                parts.append(f"电量 {int(ac['battery'])}%")
            parts.append(f"进度 {int(ac.get('progress') or 0)} {ac.get('progress_unit') or '次'}")
        if "inscribable" in traits:
            entries = traits["inscribable"].get("entries") or []
            parts.append(f"有 {len(entries)} 条字迹" if entries else "空白")
        if "portal" in traits:
            pt = traits["portal"]
            parts.append("开着（可通行）" if pt.get("is_open") else "关着（阻挡）")
            target_sid = str(pt.get("target_space_id") or "")
            sp = self._world["spaces"].get(target_sid)
            if sp:
                parts.append(f"通往「{sp['name']}」")
            else:
                parts.append("未连接目标空间")
        return parts

    def legal_verbs(self, entity: Mapping[str, Any], actor: Mapping[str, Any]) -> list[tuple[str, str]]:
        """返回 [(canonical_action, display_label), ...]。"""
        traits = entity.get("traits") or {}
        aid = str(actor.get("actor_id") or "")
        pid = str(entity.get("parent_id") or "")
        is_held = pid == f"actor:{aid}"
        verbs: list[tuple[str, str]] = [("look", "凑近看")]

        if "portable" in traits and traits["portable"].get("weight", "light") == "light":
            if is_held:
                verbs.append(("place", "放下"))
            elif not (traits.get("restable") and traits["restable"].get("occupied_by") == aid):
                verbs.append(("take", "拿起"))

        if "container" in traits:
            ct = traits["container"]
            if ct.get("is_locked"):
                verbs.append(("unlock", "开锁"))
            elif ct.get("is_open"):
                verbs.append(("close", "合上"))
            else:
                verbs.append(("open", "打开"))

        if "consumable" in traits:
            cm = traits["consumable"]
            if int(cm.get("uses_left") or 0) > 0:
                verbs.append(("consume", str(cm.get("consume_verb") or "使用")))

        if "switchable" in traits:
            sw = traits["switchable"]
            is_on = bool(sw.get("is_on"))
            if sw.get("provides_light"):
                verbs.append(("turn_off" if is_on else "turn_on", "关灯" if is_on else "开灯"))
            elif "activity" in traits:
                verbs.append(("turn_off" if is_on else "turn_on", "关机" if is_on else "开机"))
            else:
                verbs.append(("turn_off" if is_on else "turn_on", "关掉" if is_on else "打开开关"))

        if "restable" in traits:
            occ = traits["restable"].get("occupied_by")
            if occ == aid:
                verbs.append(("leave_rest", "起身"))
            elif occ is None:
                verbs.append(("rest", "坐下休息"))

        if "activity" in traits:
            ac = traits["activity"]
            bat = ac.get("battery")
            if bat is None or int(bat) > 0:
                dom = str(ac.get("domain") or "")
                label = "玩一会" if dom == "game" else ("翻阅" if dom == "reading" else "互动")
                verbs.append(("interact", label))

        if "inscribable" in traits:
            verbs.append(("read", "读"))

        if "portal" in traits:
            pt = traits["portal"]
            if pt.get("is_open"):
                verbs.append(("close", "关门"))
            else:
                verbs.append(("open", "开门"))
            if str(pt.get("target_space_id") or "") in self._world["spaces"]:
                verbs.append(("enter", "进入"))

        return verbs

    # ── 执行动作工具：act / move / write / combine ──────────────────────
    def _clear_actor_rest_locked(self, actor: dict[str, Any]) -> None:
        rest_id = actor.get("resting_on_id")
        if rest_id and rest_id in self._world["entities"]:
            rs = (self._world["entities"][rest_id].get("traits") or {}).get("restable")
            if rs and rs.get("occupied_by") == actor.get("actor_id"):
                rs["occupied_by"] = None
        actor["resting_on_id"] = None
        actor["posture"] = "standing"

    def _apply_effects_locked(self, actor: dict[str, Any], entity: Mapping[str, Any]) -> None:
        eff = entity.get("effects") or {}
        vit = actor["vitals"]
        vit["energy"] = round(clamp_float(vit.get("energy", 0.7) + float(eff.get("energy_delta") or 0.0), 0.0, 1.0, 0.7), 3)
        vit["sleep_pressure"] = round(
            clamp_float(vit.get("sleep_pressure", 0.3) + float(eff.get("sleep_pressure_delta") or 0.0), 0.0, 1.0, 0.3),
            3,
        )
        vit["mood_v"] = round(clamp_float(vit.get("mood_v", 0.1) + float(eff.get("mood_v_delta") or 0.0), -1.0, 1.0, 0.1), 3)
        aut = normalize_autonomy(actor.get("autonomy"))
        traits = entity.get("traits") or {}
        if "consumable" in traits:
            aut["hunger"] = round(clamp_float(aut["hunger"] - 0.38, 0.0, 1.0, 0.2), 3)
        if "activity" in traits:
            aut["curiosity"] = round(clamp_float(aut["curiosity"] - 0.28, 0.0, 1.0, 0.3), 3)
            aut["hunger"] = round(clamp_float(aut["hunger"] + 0.05, 0.0, 1.0, 0.3), 3)
        actor["autonomy"] = aut

    def update_actor_autonomy(
        self,
        actor_id: str,
        patch: Mapping[str, Any],
        *,
        emotion_hint: str = "",
    ) -> dict[str, Any]:
        with self._lock:
            actor = self._world["actors"].get(actor_id)
            if not actor:
                raise ValueError(f"角色不存在：{actor_id}")
            cur = normalize_autonomy(actor.get("autonomy"))
            cur.update(dict(patch or {}))
            actor["autonomy"] = normalize_autonomy(cur)
            if emotion_hint:
                self._refresh_actor_expression_locked(actor, emotion_hint=emotion_hint)
            if patch.get("last_action_summary"):
                self._append_event_locked(
                    "actor_autonomous_step",
                    actor_id=actor_id,
                    space_id=actor["space_id"],
                    payload={
                        "goal": actor["autonomy"]["current_goal"],
                        "drive": actor["autonomy"]["current_drive"],
                        "summary": actor["autonomy"]["last_action_summary"],
                    },
                )
            self._save_world_locked()
            return copy.deepcopy(actor["autonomy"])

    def _walk_to_entity_locked(
        self,
        actor: dict[str, Any],
        entity: Mapping[str, Any],
        *,
        allow_inside_entity: bool = False,
    ) -> tuple[bool, str]:
        if self.chebyshev_distance(actor, entity) <= 1:
            return True, ""
        sid = actor["space_id"]
        space = self._world["spaces"].get(sid)
        if not space:
            return False, "当前空间无效。"
        aid = actor["actor_id"]
        ent_cells = set(self._entity_cells(entity))
        goal_cells: set[tuple[int, int]] = set()
        allow_cells: set[tuple[int, int]] = set(ent_cells) if (allow_inside_entity or not entity.get("solid")) else set()

        if allow_inside_entity:
            for cx, cy in ent_cells:
                if self._is_floor_walkable_locked(space, cx, cy, allow_cells=allow_cells, ignore_actor_id=aid):
                    goal_cells.add((cx, cy))
        else:
            for cx, cy in ent_cells:
                for dx in (-1, 0, 1):
                    for dy in (-1, 0, 1):
                        nx, ny = cx + dx, cy + dy
                        if self._is_floor_walkable_locked(space, nx, ny, allow_cells=allow_cells, ignore_actor_id=aid):
                            goal_cells.add((nx, ny))

        path = self._bfs_locked(
            sid,
            (int(actor["x"]), int(actor["y"])),
            goal_cells,
            allow_cells=allow_cells,
            ignore_actor_id=aid,
        )
        if not path:
            return False, f"通往「{entity['name']}」的路被挡住了，过不去。"
        self._clear_actor_rest_locked(actor)
        dest_x, dest_y = path[-1]
        actor["x"] = dest_x
        actor["y"] = dest_y
        self._append_event_locked("actor_moved", actor_id=aid, space_id=sid, payload={"x": dest_x, "y": dest_y, "reason": f"approach:{entity['entity_id']}"})
        return True, "你走过去，"

    def execute_move(self, actor_id: str, x: int, y: int) -> str:
        with self._lock:
            actor = self._world["actors"].get(actor_id)
            if not actor:
                return "当前世界里还没有导入角色。"
            sid = actor["space_id"]
            space = self._world["spaces"].get(sid)
            if not space:
                return "当前所在空间不存在。"
            tx, ty = int(x), int(y)
            if not (0 <= tx < int(space["width"]) and 0 <= ty < int(space["height"])):
                return f"坐标 ({tx}, {ty}) 不在房间地面范围内。"
            if self.is_wall_cell(space, tx, ty) and not self._has_open_door_at_locked(sid, tx, ty):
                return f"({tx}, {ty}) 是墙面，不在房间地面范围内。"
            if not self._is_floor_walkable_locked(space, tx, ty, ignore_actor_id=actor_id):
                return f"({tx}, {ty}) 那个位置放了实心东西，站不过去。"
            path = self._bfs_locked(sid, (int(actor["x"]), int(actor["y"])), {(tx, ty)}, ignore_actor_id=actor_id)
            if not path:
                return f"去 ({tx}, {ty}) 的路被家具挡住了，过不去。"
            self._clear_actor_rest_locked(actor)
            actor["x"] = tx
            actor["y"] = ty
            self._refresh_actor_expression_locked(actor, action_kind="move")
            self._append_event_locked("actor_moved", actor_id=actor_id, space_id=sid, payload={"x": tx, "y": ty})
            self._save_world_locked()
            return f"你走到了房间坐标 ({tx}, {ty})。"

    def execute_act(self, actor_id: str, entity_id: str, action: str, target_id: str = "") -> str:
        with self._lock:
            actor = self._world["actors"].get(actor_id)
            if not actor:
                return "当前世界里还没有导入角色。"
            eid = str(entity_id or "").strip()
            ent = self._world["entities"].get(eid)
            if not ent:
                return f"这个世界里没有编号为「{eid}」的东西。"
            vis_ids = {v["entity_id"] for v in self.visible_entities(actor_id)}
            if eid not in vis_ids:
                return f"你此刻看不到「{ent['name']}」。"

            canon = _VERB_ALIASES.get(str(action or "").strip().lower()) or _VERB_ALIASES.get(str(action or "").strip(), "")
            legal = dict(self.legal_verbs(ent, actor))
            if canon not in legal:
                if canon == "consume" and "consumable" in (ent.get("traits") or {}):
                    return f"「{ent['name']}」已经空了，没有可以再用的份量。"
                if canon == "take" and "portable" not in (ent.get("traits") or {}):
                    return f"「{ent['name']}」太沉或固定在原地，拿不起来。"
                avail = "、".join(legal.values())
                return f"现在不能对「{ent['name']}」做这个动作（当前可行：{avail}）。"

            traits = ent["traits"]
            sid = actor["space_id"]

            if canon == "look":
                ok, prefix = self._walk_to_entity_locked(actor, ent) if self.chebyshev_distance(actor, ent) > 2 else (True, "")
                if not ok:
                    return prefix
                self._world.setdefault("looked_entities", {})[eid] = time.time()
                self._refresh_actor_expression_locked(actor, action_kind="look")
                extra = ""
                if "inscribable" in traits:
                    entries = traits["inscribable"].get("entries") or []
                    if entries:
                        lines = [f"「{e['text']}」" for e in entries[-3:]]
                        extra = " 上面写着：" + "；".join(lines)
                self._save_world_locked()
                return f"{prefix}{ent['name']}：{ent['detail']}{extra}"

            if canon == "take":
                if actor.get("holding_id"):
                    held = self._world["entities"].get(actor["holding_id"])
                    held_name = held["name"] if held else "别的东西"
                    return f"你手里已经拿着「{held_name}」，得先把它放下才能拿别的。"
                ok, prefix = self._walk_to_entity_locked(actor, ent)
                if not ok:
                    return prefix
                ent["parent_id"] = f"actor:{actor_id}"
                ent["z"] = -1
                actor["holding_id"] = eid
                ent["traces"].append({"ts": time.time(), "text": f"被{actor['name']}拿了起来"})
                self._append_event_locked("entity_moved", actor_id=actor_id, space_id=sid, payload={"entity_id": eid, "to": f"actor:{actor_id}"})
                self._save_world_locked()
                return f"{prefix}把「{ent['name']}」拿在了手里。"

            if canon == "place":
                self._clear_actor_rest_locked(actor)
                tgt = str(target_id or "").strip()
                dest_x, dest_y = int(actor["x"]), int(actor["y"])
                explicit_parent = ""
                if tgt and tgt != "floor":
                    if "," in tgt:
                        parts = tgt.split(",", 1)
                        try:
                            dest_x, dest_y = int(parts[0].strip()), int(parts[1].strip())
                        except ValueError:
                            return "放置坐标格式不正确。"
                    elif tgt in self._world["entities"]:
                        t_ent = self._world["entities"][tgt]
                        if t_ent.get("space_id") != sid:
                            return f"「{t_ent['name']}」不在这个房间里。"
                        ok, _ = self._walk_to_entity_locked(actor, t_ent)
                        if not ok:
                            return _
                        t_traits = t_ent.get("traits") or {}
                        if "container" in t_traits:
                            if t_traits["container"].get("is_locked") or not t_traits["container"].get("is_open"):
                                return f"「{t_ent['name']}」还是关着的，放不进去。"
                            explicit_parent = f"entity:{tgt}"
                            dest_x, dest_y = int(t_ent["x"]), int(t_ent["y"])
                        elif "surface" in t_traits:
                            placed_cell = None
                            for cx, cy in self._entity_cells(t_ent):
                                occupied = any(
                                    (cx, cy) in self._entity_cells(ch)
                                    for ch in self._surface_children_locked(tgt)
                                )
                                if not occupied:
                                    placed_cell = (cx, cy)
                                    break
                            if placed_cell is None:
                                return f"「{t_ent['name']}」上已经摆满了。"
                            dest_x, dest_y = placed_cell
                try:
                    pid, px, py, pz = self._resolve_placement_locked(
                        sid,
                        dest_x,
                        dest_y,
                        ent["footprint"],
                        ent["placement"],
                        bool(ent["solid"]),
                        target_parent_id=explicit_parent,
                        ignore_id=eid,
                    )
                except ValueError as exc:
                    return str(exc)
                ent["parent_id"] = pid
                ent["space_id"] = sid
                ent["x"] = px
                ent["y"] = py
                ent["z"] = pz
                if actor.get("holding_id") == eid:
                    actor["holding_id"] = None
                self._append_event_locked("entity_moved", actor_id=actor_id, space_id=sid, payload={"entity_id": eid, "x": px, "y": py, "z": pz, "parent_id": pid})
                self._save_world_locked()
                return f"你把「{ent['name']}」放到了 ({px}, {py})。"

            if canon == "unlock":
                ok, prefix = self._walk_to_entity_locked(actor, ent)
                if not ok:
                    return prefix
                need_tag = str(traits["container"].get("key_tag") or "").strip()
                held_id = actor.get("holding_id")
                has_key = False
                for cand in self.visible_entities(actor_id):
                    if cand["entity_id"] == held_id or self.chebyshev_distance(actor, cand) <= 1:
                        if not need_tag or need_tag in (cand.get("tags") or []):
                            if cand["entity_id"] != eid:
                                has_key = True
                                break
                if need_tag and not has_key:
                    return f"「{ent['name']}」锁着，需要带有「{need_tag}」标签的钥匙在手边才能打开。"
                traits["container"]["is_locked"] = False
                traits["container"]["is_open"] = True
                self._append_event_locked("entity_state_changed", actor_id=actor_id, space_id=sid, payload={"entity_id": eid, "unlocked": True, "is_open": True})
                self._save_world_locked()
                return f"{prefix}解开了「{ent['name']}」的锁并打开了它。"

            if canon in ("open", "close"):
                ok, prefix = self._walk_to_entity_locked(actor, ent)
                if not ok:
                    return prefix
                new_open = canon == "open"
                if "portal" in traits and "container" not in traits:
                    traits["portal"]["is_open"] = new_open
                    ent["solid"] = not new_open
                    if not new_open:
                        door_cells = set(self._entity_cells(ent))
                        space = self._world["spaces"].get(sid)
                        for other_id, other_act in self._world["actors"].items():
                            if other_act.get("space_id") == sid and (int(other_act["x"]), int(other_act["y"])) in door_cells and space:
                                nx, ny = self._find_open_floor_cell_locked(
                                    space,
                                    preferred_x=int(other_act["x"]),
                                    preferred_y=int(other_act["y"]),
                                    ignore_actor_id=other_id,
                                )
                                self._clear_actor_rest_locked(other_act)
                                other_act["x"] = nx
                                other_act["y"] = ny
                    self._refresh_actor_expression_locked(actor, action_kind=canon)
                    self._append_event_locked("entity_state_changed", actor_id=actor_id, space_id=sid, payload={"entity_id": eid, "is_open": new_open, "solid": ent["solid"]})
                    self._push_diff_locked(sid, f"「{ent['name']}」{'被打开了，现在可直接通行' if new_open else '被关上了'}")
                    self._save_world_locked()
                    if new_open:
                        return f"{prefix}把「{ent['name']}」打开了，现在可以直接通行。"
                    return f"{prefix}把「{ent['name']}」关上了。"
                traits["container"]["is_open"] = new_open
                self._append_event_locked("entity_state_changed", actor_id=actor_id, space_id=sid, payload={"entity_id": eid, "is_open": new_open})
                self._save_world_locked()
                if new_open:
                    inside = [c["name"] for c in self._container_children_locked(eid)]
                    inside_note = f"里面放着：{'、'.join(inside)}。" if inside else "里面是空的。"
                    return f"{prefix}打开了「{ent['name']}」。{inside_note}"
                return f"{prefix}把「{ent['name']}」合上了。"

            if canon == "consume":
                ok, prefix = self._walk_to_entity_locked(actor, ent)
                if not ok:
                    return prefix
                cm = traits["consumable"]
                cm["uses_left"] = max(0, int(cm["uses_left"]) - 1)
                self._apply_effects_locked(actor, ent)
                verb = cm.get("consume_verb") or "使用"
                left = int(cm["uses_left"])
                if left == 0 and cm.get("on_empty") == "vanish":
                    if actor.get("holding_id") == eid:
                        actor["holding_id"] = None
                    self._world["entities"].pop(eid, None)
                    self._refresh_actor_expression_locked(actor, action_kind="consume")
                    self._append_event_locked("entity_consumed", actor_id=actor_id, space_id=sid, payload={"entity_id": eid, "verb": verb, "uses_left": 0, "vanished": True})
                    self._save_world_locked()
                    return f"{prefix}把「{ent['name']}」{verb}光了，现在彻底没了。"
                if left == 0:
                    ent["brief"] = clip_text(cm.get("empty_brief"), MAX_BRIEF_CHARS, f"空掉的{ent['name']}")
                ent["traces"].append({"ts": time.time(), "text": f"{actor['name']}{verb}了一次"})
                self._refresh_actor_expression_locked(actor, action_kind="consume")
                self._append_event_locked("entity_consumed", actor_id=actor_id, space_id=sid, payload={"entity_id": eid, "verb": verb, "uses_left": left})
                self._save_world_locked()
                return f"{prefix}对着「{ent['name']}」{verb}了一次，" + (f"还剩 {left} 次。" if left > 0 else "已经空了。")

            if canon in ("turn_on", "turn_off"):
                ok, prefix = self._walk_to_entity_locked(actor, ent)
                if not ok:
                    return prefix
                new_on = canon == "turn_on"
                traits["switchable"]["is_on"] = new_on
                self._refresh_actor_expression_locked(actor, action_kind=canon)
                self._append_event_locked("entity_state_changed", actor_id=actor_id, space_id=sid, payload={"entity_id": eid, "is_on": new_on})
                self._save_world_locked()
                label = "打开了" if new_on else "关掉了"
                return f"{prefix}{label}「{ent['name']}」。"

            if canon == "rest":
                ok, prefix = self._walk_to_entity_locked(actor, ent, allow_inside_entity=True)
                if not ok:
                    return prefix
                self._clear_actor_rest_locked(actor)
                traits["restable"]["occupied_by"] = actor_id
                actor["resting_on_id"] = eid
                actor["posture"] = "resting"
                actor["x"] = int(ent["x"])
                actor["y"] = int(ent["y"])
                self._refresh_actor_expression_locked(actor, action_kind="rest")
                self._append_event_locked("entity_state_changed", actor_id=actor_id, space_id=sid, payload={"entity_id": eid, "resting": True})
                self._save_world_locked()
                return f"{prefix}在「{ent['name']}」上歇了下来。"

            if canon == "leave_rest":
                self._clear_actor_rest_locked(actor)
                self._refresh_actor_expression_locked(actor, action_kind="leave_rest")
                self._append_event_locked("entity_state_changed", actor_id=actor_id, space_id=sid, payload={"entity_id": eid, "resting": False})
                self._save_world_locked()
                return f"你从「{ent['name']}」上起身站好了。"

            if canon == "interact":
                ok, prefix = self._walk_to_entity_locked(actor, ent)
                if not ok:
                    return prefix
                ac = traits["activity"]
                if "switchable" in traits and not traits["switchable"].get("is_on"):
                    traits["switchable"]["is_on"] = True
                if ac.get("battery") is not None:
                    cost = int(ac.get("battery_cost") or 10)
                    if int(ac["battery"]) <= 0:
                        return f"「{ent['name']}」没电了，打不开。"
                    ac["battery"] = max(0, int(ac["battery"]) - cost)
                ac["progress"] = int(ac.get("progress") or 0) + 1
                self._apply_effects_locked(actor, ent)
                ent["traces"].append({"ts": time.time(), "text": f"{actor['name']}玩/用了第 {ac['progress']} {ac['progress_unit']}"})
                self._refresh_actor_expression_locked(actor, action_kind="interact")
                self._append_event_locked("entity_state_changed", actor_id=actor_id, space_id=sid, payload={"entity_id": eid, "progress": ac["progress"], "battery": ac.get("battery")})
                self._save_world_locked()
                bat_msg = f"，剩余电量 {ac['battery']}%" if ac.get("battery") is not None else ""
                return f"{prefix}用「{ent['name']}」推进到了第 {ac['progress']} {ac['progress_unit']}{bat_msg}。"

            if canon == "read":
                ok, prefix = self._walk_to_entity_locked(actor, ent) if self.chebyshev_distance(actor, ent) > 2 else (True, "")
                if not ok:
                    return prefix
                self._refresh_actor_expression_locked(actor, action_kind="read")
                self._save_world_locked()
                entries = traits["inscribable"].get("entries") or []
                if not entries:
                    return f"{prefix}翻开「{ent['name']}」，里面还是空白的。"
                formatted = "；".join(f"[{e['by']}] {e['text']}" for e in entries[-5:])
                return f"{prefix}「{ent['name']}」上面写着：{formatted}"

            if canon == "enter":
                ok, prefix = self._walk_to_entity_locked(actor, ent, allow_inside_entity=True)
                if not ok:
                    return prefix
                pt = traits["portal"]
                pt["is_open"] = True
                ent["solid"] = False
                target_sid = str(pt.get("target_space_id") or "")
                target_space = self._world["spaces"].get(target_sid)
                if not target_space:
                    return f"「{ent['name']}」那边还没有接通别的空间。"
                self._clear_actor_rest_locked(actor)
                tx, ty = self._find_open_floor_cell_locked(
                    target_space,
                    preferred_x=int(pt.get("target_x") or 5),
                    preferred_y=int(pt.get("target_y") or 2),
                    ignore_actor_id=actor_id,
                )
                actor["space_id"] = target_sid
                actor["x"] = tx
                actor["y"] = ty
                # 手持物随角色同步迁入新空间
                if actor.get("holding_id") and actor["holding_id"] in self._world["entities"]:
                    self._world["entities"][actor["holding_id"]]["space_id"] = target_sid
                self._world["active_space_id"] = target_sid
                self._append_event_locked("actor_moved", actor_id=actor_id, space_id=target_sid, payload={"via_portal": eid, "x": tx, "y": ty})
                self._save_world_locked()
                return f"{prefix}推开「{ent['name']}」，来到了「{target_space['name']}」({tx}, {ty})。"

            return f"暂不支持对「{ent['name']}」执行 {action}。"

    def execute_write(self, actor_id: str, entity_id: str, text: str, *, author: str = "") -> str:
        with self._lock:
            actor = self._world["actors"].get(actor_id)
            if not actor:
                return "当前世界里还没有导入角色。"
            eid = str(entity_id or "").strip()
            ent = self._world["entities"].get(eid)
            if not ent:
                return f"这个世界里没有编号为「{eid}」的东西。"
            vis_ids = {v["entity_id"] for v in self.visible_entities(actor_id)}
            if eid not in vis_ids:
                return f"你此刻看不到「{ent['name']}」。"
            traits = ent.get("traits") or {}
            if "inscribable" not in traits:
                return f"「{ent['name']}」不是能写字的东西。"
            ins = traits["inscribable"]
            if not author and not ins.get("writable_by_ai"):
                return f"「{ent['name']}」不能由你改写。"
            cleaned = _STAGE_DIR_RE.sub("", str(text or "")).strip()
            cleaned = clip_text(cleaned, 120, "")
            if not cleaned:
                return "写下的内容不能为空。"
            ok, prefix = self._walk_to_entity_locked(actor, ent)
            if not ok:
                return prefix
            entries = ins.setdefault("entries", [])
            max_entries = int(ins.get("max_entries") or 12)
            if len(entries) >= max_entries:
                entries[:] = entries[-(max_entries - 1):]
            who = author or actor["name"]
            entries.append({"by": who, "ts": time.time(), "text": cleaned})
            ent["traces"].append({"ts": time.time(), "text": f"{who}写下了一行字"})
            self._refresh_actor_expression_locked(actor, action_kind="write")
            self._append_event_locked("entity_inscribed", actor_id=actor_id, space_id=actor["space_id"], payload={"entity_id": eid, "by": who, "text": cleaned})
            self._save_world_locked()
            return f"{prefix}在「{ent['name']}」上写下了：「{cleaned}」。"

    def execute_combine(self, actor_id: str, source_id: str, target_id: str) -> str:
        with self._lock:
            actor = self._world["actors"].get(actor_id)
            if not actor:
                return "当前世界里还没有导入角色。"
            sid_src = str(source_id or "").strip()
            sid_tgt = str(target_id or "").strip()
            src = self._world["entities"].get(sid_src)
            tgt = self._world["entities"].get(sid_tgt)
            if not src or not tgt:
                return "找不到你要一起使用的两件东西。"
            vis_ids = {v["entity_id"] for v in self.visible_entities(actor_id)}
            if sid_src not in vis_ids or sid_tgt not in vis_ids:
                return "这两件东西里至少有一件不在你视野里。"
            ok, prefix = self._walk_to_entity_locked(actor, tgt)
            if not ok:
                return prefix

            src_tags = set(src.get("tags") or [])
            matched_rule = None
            for rule in tgt.get("combine_rules") or []:
                with_id = str(rule.get("with_id") or "").strip()
                with_tag = str(rule.get("with_tag") or "").strip()
                if (with_id and with_id == sid_src) or (with_tag and with_tag in src_tags):
                    matched_rule = rule
                    break
            if not matched_rule:
                return f"把「{src['name']}」和「{tgt['name']}」放一起试了试，似乎没什么反应。"

            patch = matched_rule.get("set_target_state") or {}
            t_traits = tgt.get("traits") or {}
            for k, v in patch.items():
                if k == "uses_left" and "consumable" in t_traits:
                    t_traits["consumable"]["uses_left"] = int(v)
                elif k == "temp" and "thermal" in t_traits:
                    t_traits["thermal"]["temp"] = str(v)
                    t_traits["thermal"]["temp_updated_ts"] = time.time()
                elif k == "battery" and "activity" in t_traits:
                    t_traits["activity"]["battery"] = int(v)
                elif k == "is_on" and "switchable" in t_traits:
                    t_traits["switchable"]["is_on"] = bool(v)
                elif k == "is_locked" and "container" in t_traits:
                    t_traits["container"]["is_locked"] = bool(v)
                elif k == "is_open" and "container" in t_traits:
                    t_traits["container"]["is_open"] = bool(v)

            if matched_rule.get("consume_source"):
                if actor.get("holding_id") == sid_src:
                    actor["holding_id"] = None
                self._world["entities"].pop(sid_src, None)

            fb = str(matched_rule.get("feedback") or "两件东西产生了一点变化。")
            self._refresh_actor_expression_locked(actor, action_kind="combine")
            self._append_event_locked("entity_state_changed", actor_id=actor_id, space_id=actor["space_id"], payload={"combine": [sid_src, sid_tgt], "feedback": fb})
            self._save_world_locked()
            return f"{prefix}{fb}"

    # ── L1 物理时钟推进（0 Token）────────────────────────────────────────
    def tick_physics(self, *, advance_minutes: float | None = None, now: float | None = None) -> dict[str, Any]:
        with self._lock:
            cur_now = float(now if now is not None else time.time())
            last_ts = float(self._world.get("last_tick_ts") or cur_now)
            if advance_minutes is not None:
                delta_min = max(0.0, float(advance_minutes))
                self._world["clock_offset_minutes"] = float(self._world.get("clock_offset_minutes") or 0.0) + delta_min
            else:
                delta_min = max(0.0, (cur_now - last_ts) / 60.0)
            self._world["last_tick_ts"] = cur_now

            cooled: list[str] = []
            if delta_min > 0:
                for eid, ent in self._world["entities"].items():
                    th = (ent.get("traits") or {}).get("thermal")
                    if not th:
                        continue
                    cur_temp = str(th.get("temp") or "cold")
                    if cur_temp == "cold":
                        continue
                    cool_min = max(5.0, float(th.get("cool_minutes") or 45.0))
                    elapsed = delta_min
                    if advance_minutes is None:
                        elapsed = max(delta_min, (cur_now - float(th.get("temp_updated_ts") or cur_now)) / 60.0)
                    new_temp = cur_temp
                    if elapsed >= cool_min:
                        new_temp = "cold"
                    elif elapsed >= cool_min * 0.45 and cur_temp == "hot":
                        new_temp = "warm"
                    if new_temp != cur_temp:
                        th["temp"] = new_temp
                        th["temp_updated_ts"] = cur_now
                        cooled.append(f"{ent['name']}->{new_temp}")
                        self._push_diff_locked(ent["space_id"], f"「{ent['name']}」{'微温了' if new_temp == 'warm' else '已经凉透了'}")

                for act in self._world["actors"].values():
                    vit = act["vitals"]
                    rest_id = act.get("resting_on_id")
                    if act.get("posture") == "resting" and rest_id and rest_id in self._world["entities"]:
                        regen = float(((self._world["entities"][rest_id].get("traits") or {}).get("restable") or {}).get("energy_regen_per_min") or 0.012)
                        vit["energy"] = round(clamp_float(vit["energy"] + regen * delta_min, 0.0, 1.0, 0.7), 3)
                        vit["sleep_pressure"] = round(clamp_float(vit["sleep_pressure"] - regen * 0.9 * delta_min, 0.0, 1.0, 0.3), 3)
                    else:
                        vit["energy"] = round(clamp_float(vit["energy"] - 0.0012 * delta_min, 0.05, 1.0, 0.7), 3)
                        vit["sleep_pressure"] = round(clamp_float(vit["sleep_pressure"] + 0.0015 * delta_min, 0.0, 1.0, 0.3), 3)
                    aut = normalize_autonomy(act.get("autonomy"))
                    aut["hunger"] = round(clamp_float(aut["hunger"] + 0.005 * delta_min, 0.0, 1.0, 0.3), 3)
                    aut["curiosity"] = round(clamp_float(aut["curiosity"] + 0.006 * delta_min, 0.0, 1.0, 0.55), 3)
                    act["autonomy"] = aut
                    self._refresh_actor_expression_locked(act)

                if delta_min >= 1.0 or cooled:
                    self._append_event_locked("world_tick", payload={"delta_min": round(delta_min, 2), "cooled": cooled})
            self._save_world_locked()
            return {"delta_min": round(delta_min, 2), "cooled": cooled}

    def _actor_active_sprite_payload(self, actor: Mapping[str, Any]) -> dict[str, Any]:
        sp = actor.get("sprite") or {}
        expr = actor.get("expression") or {}
        mode = str(sp.get("mode") or "pixel_grid")
        path = str(expr.get("path") or "")
        data_url = ""
        if mode == "host_pack" and path:
            data_url = self._host_sprite_data_url(path)
        elif mode == "image" and sp.get("image_path"):
            data_url = self._sprite_with_data_url(sp).get("image_data_url", "")
        return {
            "mode": mode,
            "index": int(expr.get("index") or 1),
            "tag": str(expr.get("tag") or "平静"),
            "bucket": str(expr.get("bucket") or "neutral"),
            "manual": bool(expr.get("manual", False)),
            "path": path,
            "data_url": data_url,
            "portrait_crop": dict(sp.get("portrait_crop") or {"x": 0.5, "y": 0.2, "zoom": 1.0}),
        }

    # ── UI 快照拼装 ─────────────────────────────────────────────────────
    def snapshot_for_ui(self, space_id: str = "") -> dict[str, Any]:
        with self._lock:
            sid = str(space_id or self._world.get("active_space_id") or "")
            if sid not in self._world["spaces"]:
                sid = next(iter(self._world["spaces"]))
            space = copy.deepcopy(self._world["spaces"][sid])
            entities_in_space: list[dict[str, Any]] = []
            for ent in self._world["entities"].values():
                if ent.get("space_id") != sid:
                    continue
                item = copy.deepcopy(ent)
                item["sprite"] = self._sprite_with_data_url(item["sprite"])
                item["state_tags"] = self.entity_state_tags(item)
                entities_in_space.append(item)

            actors_out: list[dict[str, Any]] = []
            for act in self._world["actors"].values():
                a = copy.deepcopy(act)
                a["sprite"] = self._sprite_with_data_url(a["sprite"])
                a["active_sprite"] = self._actor_active_sprite_payload(a)
                actors_out.append(a)

            blueprints_out = []
            for bp in self.all_blueprints().values():
                b = copy.deepcopy(bp)
                b["sprite"] = self._sprite_with_data_url(b["sprite"])
                blueprints_out.append(b)

            palettes = self.all_palettes()
            active_palette = palettes.get(space["palette_id"]) or next(iter(palettes.values()))
            lighting = compute_lighting(
                space,
                self._world["entities"],
                time.time(),
                float(self._world.get("clock_offset_minutes") or 0.0),
            )
            return {
                "active_space_id": sid,
                "active_actor_id": self.resolve_actor_id(),
                "clock_offset_minutes": float(self._world.get("clock_offset_minutes") or 0.0),
                "looked_entities": dict(self._world.get("looked_entities") or {}),
                "spaces": [
                    {
                        **copy.deepcopy(sp),
                        "entity_count": sum(
                            1
                            for ent in self._world["entities"].values()
                            if ent.get("space_id") == sp.get("space_id")
                        ),
                        "actor_names": [
                            str(act.get("name") or act.get("actor_id") or "")
                            for act in self._world["actors"].values()
                            if act.get("space_id") == sp.get("space_id")
                        ],
                    }
                    for sp in self._world["spaces"].values()
                ],
                "space": space,
                "lighting": lighting,
                "palette": active_palette,
                "palettes": list(palettes.values()),
                "tile_catalog": [copy.deepcopy(t) for t in TILE_CATALOG.values()],
                "entities": entities_in_space,
                "actors": actors_out,
                "blueprints": blueprints_out,
                "host_characters": self.list_host_characters(),
                "events": list(reversed(self.read_recent_events(40))),
            }
