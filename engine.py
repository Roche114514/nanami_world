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
from uuid import uuid4

from . import persona_bridge
from .schema import (
    DEFAULT_ACTOR_PIXELS_16X24,
    DEFAULT_ACTOR_SLOT_COLORS,
    MAP_STYLES,
    MAX_BRIEF_CHARS,
    MAX_DETAIL_CHARS,
    MAX_SPACE_HEIGHT,
    MAX_SPACE_WIDTH,
    MIN_SPACE_HEIGHT,
    MIN_SPACE_WIDTH,
    TILE_CATALOG,
    VALID_SPACE_KINDS,
    _normalize_expression,
    _normalize_vitals,
    clamp_float,
    clamp_int,
    clip_text,
    infer_consumable_names,
    inscription_is_new,
    normalize_actor,
    normalize_appearance,
    normalize_autonomy,
    normalize_blueprint,
    normalize_buildings,
    normalize_combine_rules,
    normalize_effects,
    normalize_footprint,
    normalize_palette,
    normalize_space,
    normalize_space_tiles,
    normalize_sprite,
    normalize_traits,
    normalize_zones,
    slugify,
)

logger = logging.getLogger(__name__)

_MODULATION_DOMAINS = frozenset({
    "rest", "food", "game", "reading", "social", "notice", "explore",
    "expression", "comfort", "curiosity", "hobby", "music",
})

_STAGE_DIR_RE = re.compile(r"[（(][^）)\n]{1,40}[）)]")
_MAX_UPLOAD_BYTES = 512 * 1024
PLAYER_INVENTORY_SIZE = 32
NPC_INVENTORY_SIZE = 8
INVENTORY_USE_VERBS = ("consume", "interact", "read", "turn_on", "turn_off")
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


def _atomic_write_json(path: Path, payload: Any, *, indent: int | None = 2) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=indent) + "\n", encoding="utf-8")
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
            "simulation_minutes": 0.0,
            "next_seq": 1,
            "event_source": uuid4().hex,
            "persona_events": {},
            "puzzle_memories": {},
            "key_loans": {},
            "gift_offers": {},
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
        host_map: dict[str, dict[str, Any]] | None = None
        for aid, act in (raw.get("actors") or {}).items():
            if isinstance(act, Mapping):
                norm_a = normalize_actor(act)
                if not isinstance(act.get("appearance"), Mapping):
                    from . import text_compiler
                    if host_map is None:
                        host_map = {str(h.get("name") or ""): h for h in self.list_host_characters()}
                    h_name = str(norm_a.get("host_character") or norm_a.get("name") or "")
                    h_info = host_map.get(h_name) or {}
                    desc = (str(h_info.get("brief") or "") + "。" + str(h_info.get("setting") or "")).strip("。")
                    norm_a["appearance"] = text_compiler.compile_actor_appearance(h_name, desc, use_llm=False)["appearance"]
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

        loaned_ids = {loan.get("item_id") for loan in (raw.get("key_loans") or {}).values()
                      if isinstance(loan, Mapping) and loan.get("status") == "open"}
        for aid, actor in actors.items():
            held = entities.get(actor.get("holding_id"))
            if not actor.get("is_player") or not held or held["parent_id"] != f"actor:{aid}" or held["entity_id"] in loaned_ids:
                continue
            slots = {e.get("inventory_slot") for e in entities.values() if e["parent_id"] == f"inventory:{aid}"}
            slot = next((i for i in range(PLAYER_INVENTORY_SIZE) if i not in slots), None)
            if slot is not None:
                held.update(parent_id=f"inventory:{aid}", inventory_slot=slot, space_id=actor["space_id"], z=-1)
                actor["holding_id"] = None

        return {
            "version": 1,
            "active_space_id": active_sid,
            "active_actor_id": active_aid,
            "last_tick_ts": float(raw.get("last_tick_ts") or time.time()),
            "clock_offset_minutes": float(raw.get("clock_offset_minutes") or 0.0),
            "simulation_minutes": max(0.0, float(raw.get("simulation_minutes") or 0.0)),
            "next_seq": max(1, int(raw.get("next_seq") or 1)),
            "event_source": str(raw.get("event_source") or uuid4().hex),
            "persona_events": dict(raw.get("persona_events") or {}),
            "puzzle_memories": dict(raw.get("puzzle_memories") or {}),
            "key_loans": dict(raw.get("key_loans") or {}),
            "gift_offers": dict(raw.get("gift_offers") or {}),
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
            "y": int(raw["y"]) if raw.get("y") is not None else 1,
            "z": int(raw.get("z") or 0),
            **({"inventory_slot": raw["inventory_slot"]} if type(raw.get("inventory_slot")) is int else {}),
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
        # 物理更新与交互保存共用世界锁，紧凑编码使用 JSON 快速路径。
        _atomic_write_json(self._world_path, self._world, indent=None)

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
        return [ent for ent in self._world["entities"].values() if ent.get("parent_id") == parent_tag and ent["z"] < 0]

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

    def _realm_space_id_locked(self, space_id: str) -> str:
        sid = str(space_id or "").strip()
        seen: set[str] = set()
        spaces = self._world.get("spaces") or {}
        cur = sid
        for _ in range(8):
            if cur in seen:
                break
            seen.add(cur)
            sp = spaces.get(cur)
            if not isinstance(sp, Mapping):
                break
            parent = str(sp.get("parent_space_id") or "").strip()
            if not parent or parent not in spaces:
                break
            cur = parent
        return cur or sid

    def realm_space_id(self, space_id: str) -> str:
        with self._lock:
            return self._realm_space_id_locked(space_id)

    def _realm_places_locked(self, space_id: str) -> tuple[str, dict[str, str]]:
        realm = self._realm_space_id_locked(space_id)
        places = {
            sp_id: str(sp.get("name") or sp_id)
            for sp_id, sp in self._world["spaces"].items()
            if self._realm_space_id_locked(sp_id) == realm
        }
        return realm, places

    def list_realm_places(self, space_id: str) -> dict[str, str]:
        with self._lock:
            sid = str(space_id or self._world["active_space_id"]).strip()
            if sid not in self._world["spaces"]:
                return {}
            _realm, places = self._realm_places_locked(sid)
            return places

    @staticmethod
    def get_zone_at(space: Mapping[str, Any], x: int, y: int) -> dict[str, Any] | None:
        ix, iy = int(x), int(y)
        best: dict[str, Any] | None = None
        best_area = 10**9
        for z in space.get("zones") or ():
            if not isinstance(z, Mapping):
                continue
            zx, zy = int(z.get("x") or 0), int(z.get("y") or 0)
            zw, zh = int(z.get("w") or 0), int(z.get("h") or 0)
            if zx <= ix < zx + zw and zy <= iy < zy + zh:
                area = zw * zh
                if area < best_area:
                    best_area = area
                    best = dict(z)
        return best

    def _build_walk_masks_locked(
        self,
        space_id: str,
        *,
        ignore_actor_id: str | None = None,
        ignore_distant_actors_from: tuple[int, int] | None = None,
    ) -> tuple[set[tuple[int, int]], set[tuple[int, int]]]:
        blocked: set[tuple[int, int]] = set()
        open_doors: set[tuple[int, int]] = set()
        for ent in self._world["entities"].values():
            if ent.get("space_id") != space_id or int(ent.get("z", 0)) != 0:
                continue
            traits = ent.get("traits") or {}
            cells = self._entity_cells(ent)
            if "portal" in traits and bool(traits["portal"].get("is_open", False)):
                open_doors.update(cells)
                continue
            if self._is_entity_blocking_locked(ent):
                blocked.update(cells)
        for aid, act in self._world["actors"].items():
            if aid == ignore_actor_id or act.get("space_id") != space_id:
                continue
            ax, ay = int(act.get("x", -1)), int(act.get("y", -1))
            if ignore_distant_actors_from is not None:
                ox, oy = ignore_distant_actors_from
                if max(abs(ax - ox), abs(ay - oy)) > 3:
                    continue
            blocked.add((ax, ay))
        return blocked, open_doors

    @staticmethod
    def _sight_line_clear_locked(
        start: tuple[int, int],
        end: tuple[int, int],
        opaque: set[tuple[int, int]],
    ) -> bool:
        """Grid supercover line-of-sight; opaque endpoint itself remains visible."""
        x, y = start
        tx, ty = end
        dx, dy = abs(tx - x), -abs(ty - y)
        sx = 1 if x < tx else -1
        sy = 1 if y < ty else -1
        err = dx + dy
        while (x, y) != end:
            prev = (x, y)
            twice = 2 * err
            if twice >= dy:
                err += dy
                x += sx
            if twice <= dx:
                err += dx
                y += sy
            if x != prev[0] and y != prev[1]:
                if (x, prev[1]) in opaque and (prev[0], y) in opaque:
                    return False
            if (x, y) != end and (x, y) in opaque:
                return False
        return True

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
        ignore_distant_actors_from: tuple[int, int] | None = None,
    ) -> list[tuple[int, int]] | None:
        space = self._world["spaces"].get(space_id)
        if not space or not goal_cells:
            return None
        if start in goal_cells:
            return [start]
        w = int(space["width"])
        h = int(space["height"])
        blocked, open_doors = self._build_walk_masks_locked(
            space_id, ignore_actor_id=ignore_actor_id, ignore_distant_actors_from=ignore_distant_actors_from,
        )
        allow = allow_cells or set()
        queue: deque[tuple[int, int]] = deque([start])
        parents: dict[tuple[int, int], tuple[int, int] | None] = {start: None}
        while queue:
            cx, cy = queue.popleft()
            for dx, dy in ((0, 1), (0, -1), (1, 0), (-1, 0)):
                nx, ny = cx + dx, cy + dy
                nxt = (nx, ny)
                if nxt in parents or not (0 <= nx < w and 0 <= ny < h):
                    continue
                if nxt not in allow:
                    if nxt in blocked:
                        continue
                    if nxt not in open_doors and not self.get_cell_tile(space, nx, ny).get("walkable", True):
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

    def _reachable_cells_locked(
        self,
        space_id: str,
        start: tuple[int, int],
        *,
        max_steps: int = 16,
        ignore_actor_id: str | None = None,
    ) -> dict[tuple[int, int], int]:
        space = self._world["spaces"].get(space_id)
        if not space:
            return {}
        w = int(space["width"])
        h = int(space["height"])
        blocked, open_doors = self._build_walk_masks_locked(space_id, ignore_actor_id=ignore_actor_id)
        dists: dict[tuple[int, int], int] = {start: 0}
        queue: deque[tuple[int, int]] = deque([start])
        while queue:
            cx, cy = queue.popleft()
            cd = dists[(cx, cy)]
            if cd >= max_steps:
                continue
            for dx, dy in ((0, 1), (0, -1), (1, 0), (-1, 0)):
                nx, ny = cx + dx, cy + dy
                nxt = (nx, ny)
                if nxt in dists or not (0 <= nx < w and 0 <= ny < h):
                    continue
                if nxt in blocked:
                    continue
                if nxt not in open_doors and not self.get_cell_tile(space, nx, ny).get("walkable", True):
                    continue
                dists[nxt] = cd + 1
                queue.append(nxt)
        return dists

    def chebyshev_distance(self, actor: Mapping[str, Any], entity: Mapping[str, Any]) -> int:
        if self._carries_item(actor, entity):
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
        wall_rows: int = 1,
        space_kind: str = "room",
        parent_space_id: str = "",
        district: str = "",
        zones: Sequence[Mapping[str, Any]] | None = None,
        buildings: Sequence[Mapping[str, Any]] | None = None,
        fixed_space_id: str = "",
        switch_active: bool = True,
    ) -> dict[str, Any]:
        with self._lock:
            clean_name = clip_text(name, 16, "新空间")
            explicit_sid = str(fixed_space_id or "").strip()
            if explicit_sid:
                if explicit_sid in self._world["spaces"]:
                    raise ValueError(f"空间已存在：{explicit_sid}")
                sid = explicit_sid
            else:
                base_id = slugify(clean_name, prefix="room")
                sid = base_id
                idx = 2
                while sid in self._world["spaces"]:
                    sid = f"{base_id}_{idx}"
                    idx += 1
            if palette_id not in self.all_palettes():
                palette_id = "warm_stardew"
            kind = str(space_kind or "room").strip().lower()
            if kind not in VALID_SPACE_KINDS:
                kind = "room"
            sp = normalize_space({
                "space_id": sid,
                "name": clean_name,
                "width": width,
                "height": height,
                "wall_rows": wall_rows,
                "space_kind": kind,
                "parent_space_id": str(parent_space_id or "").strip(),
                "district": district,
                "ambient_phrase": ambient_phrase or f"一间叫作「{clean_name}」的小空间。",
                "palette_id": palette_id,
                "zones": list(zones or ()),
                "buildings": list(buildings or ()),
            })
            self._world["spaces"][sid] = sp
            if switch_active:
                self._world["active_space_id"] = sid
            self._append_event_locked("space_created", space_id=sid, payload={"name": clean_name, "size": [sp["width"], sp["height"]], "space_kind": kind})
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

    def _descendant_space_ids_locked(self, root_sid: str) -> list[str]:
        spaces = self._world["spaces"]
        descendants: list[str] = []
        queue: deque[str] = deque([root_sid])
        seen: set[str] = {root_sid}
        while queue:
            cur = queue.popleft()
            for k, sp in spaces.items():
                if k in seen:
                    continue
                if str(sp.get("parent_space_id") or "").strip() == cur:
                    seen.add(k)
                    descendants.append(k)
                    queue.append(k)
        return descendants

    def delete_space(self, space_id: str) -> dict[str, Any]:
        with self._lock:
            sid = str(space_id or "").strip()
            spaces = self._world["spaces"]
            if sid not in spaces:
                raise ValueError(f"空间不存在：{sid}")
            descendants = self._descendant_space_ids_locked(sid)
            delete_ids = {sid, *descendants}
            remaining = [k for k in spaces.keys() if k not in delete_ids]
            if not remaining:
                raise ValueError("至少需要保留一个世界空间，无法删除最后一个世界。")

            active_sid = str(self._world.get("active_space_id") or "")
            if active_sid and active_sid not in delete_ids and active_sid in spaces:
                fallback_sid = active_sid
            else:
                fallback_sid = remaining[0]
            fallback_space = spaces[fallback_sid]

            held_ids: set[str] = set()
            for act in self._world["actors"].values():
                hid = act.get("holding_id")
                if hid and hid in self._world["entities"]:
                    held_ids.add(hid)
                if act.get("space_id") in delete_ids:
                    self._clear_actor_rest_locked(act)
                    nx, ny = self._find_open_floor_cell_locked(
                        fallback_space,
                        ignore_actor_id=act["actor_id"],
                    )
                    self._switch_actor_world_state_locked(act, fallback_sid)
                    act["x"] = nx
                    act["y"] = ny
                    act["posture"] = "standing"
                    self._refresh_actor_expression_locked(act)
                    if hid and hid in self._world["entities"]:
                        self._world["entities"][hid]["space_id"] = fallback_sid
                ws = act.get("world_states")
                if isinstance(ws, dict):
                    for dsid in delete_ids:
                        ws.pop(dsid, None)

            removed_ids: list[str] = []
            for eid, ent in list(self._world["entities"].items()):
                if eid in held_ids:
                    if ent.get("space_id") in delete_ids:
                        ent["space_id"] = fallback_sid
                    continue
                if ent.get("space_id") in delete_ids:
                    removed_ids.append(eid)
                    continue
                pt = (ent.get("traits") or {}).get("portal")
                if isinstance(pt, dict) and str(pt.get("target_space_id") or "") in delete_ids:
                    pt["target_space_id"] = ""

            for eid in removed_ids:
                self._world["entities"].pop(eid, None)
            self._cancel_key_loans_locked(removed_ids)

            deleted_space = spaces.pop(sid)
            for dsid in descendants:
                spaces.pop(dsid, None)

            parent_sid = str(deleted_space.get("parent_space_id") or "").strip()
            if parent_sid and parent_sid in spaces:
                parent_sp = spaces[parent_sid]
                parent_sp["buildings"] = [
                    b for b in (parent_sp.get("buildings") or [])
                    if str(b.get("interior_space_id") or "") != sid
                ]

            for dsid in delete_ids:
                self._world["puzzle_memories"].pop(dsid, None)
                persona_bridge.delete_world_relations(self._data_root, dsid)
            self._world["persona_events"] = {
                eid: event for eid, event in self._world["persona_events"].items() if event["space_id"] not in delete_ids
            }
            if self._world.get("active_space_id") in delete_ids:
                self._world["active_space_id"] = fallback_sid

            self._append_event_locked(
                "space_deleted",
                space_id=fallback_sid,
                payload={
                    "deleted_space_id": sid,
                    "deleted_descendants": descendants,
                    "deleted_name": deleted_space.get("name", sid),
                    "removed_entities": len(removed_ids),
                },
            )
            self._save_world_locked()
            return {
                "deleted_space_id": sid,
                "deleted_descendants": descendants,
                "deleted_name": str(deleted_space.get("name") or sid),
                "active_space_id": self._world["active_space_id"],
                "removed_entities": len(removed_ids),
            }

    def create_default_town(
        self,
        *,
        town_id: str = "town_sakura",
        replace: bool = False,
        move_actor_id: str = "",
    ) -> dict[str, Any]:
        from . import town_builder
        return town_builder.build_default_town(
            self,
            town_id=town_id,
            replace=replace,
            move_actor_id=move_actor_id,
        )

    def create_building_subworld(self, town_space_id: str, **kwargs: Any) -> dict[str, Any]:
        from . import town_builder
        return town_builder.build_building_subworld(self, town_space_id, **kwargs)

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
                self._switch_actor_world_state_locked(actor, sid)
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
        self._sync_actor_world_state_locked(actor)
        if changed and cur_expr:
            self._append_event_locked(
                "actor_expression_changed",
                actor_id=actor["actor_id"],
                space_id=actor["space_id"],
                payload={"index": new_expr["index"], "tag": new_expr["tag"], "bucket": new_expr["bucket"]},
            )
        return new_expr

    def _sync_actor_world_state_locked(self, actor: dict[str, Any], space_id: str = "") -> None:
        sid = str(space_id or actor.get("space_id") or "room_default").strip() or "room_default"
        states = actor.setdefault("world_states", {})
        states[sid] = {
            "vitals": _normalize_vitals(actor.get("vitals")),
            "autonomy": normalize_autonomy(actor.get("autonomy")),
            "expression": _normalize_expression(actor.get("expression")),
        }
        realm = self._realm_space_id_locked(sid)
        cognition = states[sid]["autonomy"].get("cognition")
        if realm != sid and cognition is not None:
            states[sid]["autonomy"].pop("cognition")
            if realm not in states:
                states[realm] = {
                    "vitals": _normalize_vitals({"preferred_domains": actor["vitals"]["preferred_domains"]}),
                    "autonomy": normalize_autonomy({}),
                    "expression": _normalize_expression({}),
                }
            states[realm]["autonomy"]["cognition"] = cognition

    def _switch_actor_world_state_locked(self, actor: dict[str, Any], new_space_id: str) -> None:
        old_sid = str(actor.get("space_id") or "room_default").strip() or "room_default"
        new_sid = str(new_space_id or old_sid).strip() or old_sid
        if old_sid != new_sid:
            self._sync_actor_world_state_locked(actor, old_sid)
            same_realm = self._realm_space_id_locked(old_sid) == self._realm_space_id_locked(new_sid)
            actor["space_id"] = new_sid
            for item in self._world["entities"].values():
                if self._carries_item(actor, item):
                    item["space_id"] = new_sid
            if not same_realm:
                states = actor.setdefault("world_states", {})
                saved = states.get(new_sid)
                if isinstance(saved, Mapping):
                    actor["vitals"] = _normalize_vitals(saved.get("vitals"))
                    actor["autonomy"] = normalize_autonomy(saved.get("autonomy"))
                    actor["expression"] = _normalize_expression(saved.get("expression"))
                else:
                    pref = list((actor.get("vitals") or {}).get("preferred_domains") or [])
                    actor["vitals"] = _normalize_vitals({"preferred_domains": pref, "source": "builtin"})
                    actor["autonomy"] = normalize_autonomy({})
                    actor["expression"] = _normalize_expression({})
                realm = self._realm_space_id_locked(new_sid)
                cognition = (states.get(realm, {}).get("autonomy") or {}).get("cognition")
                if cognition is not None:
                    actor["autonomy"]["cognition"] = cognition
        self._sync_actor_world_state_locked(actor, new_sid)

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
            self._sync_actor_world_state_locked(actor)
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
    def _alloc_actor_id_locked(self, clean_name: str, host_character: str = "", space_id: str = "") -> str:
        # 1. 同一个世界（space_id）内同名角色已存在时复用其 actor_id；不同世界彼此独立
        for eid_a, act in self._world["actors"].items():
            if act.get("name") == clean_name and (not space_id or act.get("space_id") == space_id):
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
            short_h = hashlib.md5(f"{clean_name}@{space_id}".encode("utf-8")).hexdigest()[:6]
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
        appearance: Mapping[str, Any] | None = None,
        map_style: str = "",
    ) -> dict[str, Any]:
        with self._lock:
            clean_name = clip_text(name or host_character, 20, "七海千秋")
            if not space_id or space_id not in self._world["spaces"]:
                space_id = self._world["active_space_id"]
            explicit_aid = str(actor_id or "").strip()
            aid = explicit_aid or self._alloc_actor_id_locked(clean_name, host_character, space_id=space_id)
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
                    self._switch_actor_world_state_locked(existing, space_id)
                    self._record_teleport_locked(existing)
                    existing["x"] = tx
                    existing["y"] = ty
                if resolved_sprite:
                    existing["sprite"] = normalize_actor({"name": clean_name, "sprite": resolved_sprite})["sprite"]
                if appearance:
                    existing["appearance"] = normalize_appearance(appearance, seed=str(existing.get("host_character") or clean_name))
                if map_style in MAP_STYLES:
                    existing["map_style"] = map_style
                actor = existing
            else:
                actor = normalize_actor({
                    "actor_id": aid,
                    "host_character": host_character or clean_name,
                    "name": clean_name,
                    "space_id": space_id,
                    "x": tx,
                    "y": ty,
                    "appearance": dict(appearance) if appearance else None,
                    "map_style": map_style or "doll",
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
            self._sync_actor_world_state_locked(actor)
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
            for radius in range(1, 7):
                for dy in range(-radius, radius + 1):
                    for dx in range(-radius, radius + 1):
                        if max(abs(dx), abs(dy)) != radius:
                            continue
                        nx, ny = px + dx, py + dy
                        if self._is_floor_walkable_locked(space, nx, ny, ignore_actor_id=ignore_actor_id):
                            return nx, ny
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
            self._switch_actor_world_state_locked(actor, space_id)
            self._record_teleport_locked(actor)
            actor["x"] = int(x)
            actor["y"] = int(y)
            self._refresh_actor_expression_locked(actor, action_kind="move")
            self._append_event_locked("actor_moved", actor_id=actor_id, space_id=space_id, payload={"x": int(x), "y": int(y), "by": "user"})
            self._save_world_locked()
            return copy.deepcopy(actor)

    def set_actor_appearance(
        self,
        actor_id: str,
        *,
        appearance: Mapping[str, Any] | None = None,
        map_style: str = "",
    ) -> dict[str, Any]:
        """保存角色的 Q 版像素小人外观参数与地图呈现风格（doll / portrait）。"""
        with self._lock:
            actor = self._world["actors"].get(actor_id)
            if not actor:
                raise ValueError(f"角色不存在：{actor_id}")
            if appearance is not None:
                actor["appearance"] = normalize_appearance(
                    appearance,
                    seed=str(actor.get("host_character") or actor.get("name") or ""),
                )
            if map_style:
                if map_style not in MAP_STYLES:
                    raise ValueError("map_style 只能是 doll 或 portrait。")
                actor["map_style"] = map_style
            self._append_event_locked(
                "actor_appearance_changed",
                actor_id=actor_id,
                space_id=actor["space_id"],
                payload={"map_style": actor["map_style"], "hair_style": actor["appearance"]["hair_style"]},
            )
            self._save_world_locked()
            return copy.deepcopy(actor)

    def set_active_actor(self, actor_id: str, *, expected_space_id: str = "") -> dict[str, Any]:
        with self._lock:
            actor = self._world["actors"].get(actor_id)
            if not actor or actor.get("is_player"):
                raise ValueError(f"角色不存在：{actor_id}")
            if expected_space_id and actor["space_id"] != expected_space_id:
                raise ValueError("角色已离开当前场景。")
            self._world["active_actor_id"] = actor_id
            self._save_world_locked()
            return copy.deepcopy(actor)

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
            for item in self._world["entities"].values():
                if self._carries_item(act, item):
                    item.update(parent_id=f"space:{sid}", space_id=sid, x=act["x"], y=act["y"], z=0)
                    item.pop("inventory_slot", None)
                if item["traits"].get("container", {}).get("owner_actor_id") == aid:
                    item["traits"]["container"].pop("owner_actor_id")

            removed = actors.pop(aid)
            self._world["key_loans"] = {
                loan_id: loan for loan_id, loan in self._world["key_loans"].items()
                if aid not in (loan["borrower_id"], loan["lender_id"])
            }
            for history in self._world["puzzle_memories"].values():
                history[:] = [memory for memory in history if aid not in memory["witness_ids"]]
            for event_id, event in list(self._world["persona_events"].items()):
                if aid in event["pending"]:
                    event["pending"].remove(aid)
                    if not event["pending"]:
                        del self._world["persona_events"][event_id]
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

    # ── 钥匙借用与归还 ─────────────────────────────────────────────────
    def _loan_time_locked(self) -> float:
        return time.time() + self._world["clock_offset_minutes"] * 60

    def _loan_participant_locked(self, loan_id: str, actor_id: str, role: str) -> dict[str, Any]:
        loan = self._world["key_loans"].get(loan_id)
        if not loan or loan[f"{role}_id"] != actor_id:
            raise ValueError("此角色没有这笔借物记录。")
        return loan

    def _cancel_key_loans_locked(self, item_ids: Sequence[str]) -> None:
        for loan in self._world["key_loans"].values():
            if loan["status"] == "open" and loan["item_id"] in item_ids:
                loan["status"] = "cancelled"

    def _record_loan_event_locked(
        self, kind: str, actor_id: str, loan: Mapping[str, Any], **facts: Any,
    ) -> None:
        actor = self._world["actors"].get(actor_id) or {}
        self._append_event_locked(kind, actor_id=actor_id, space_id=str(actor.get("space_id") or ""), payload={
            "loan_id": loan["id"], "item_id": loan["item_id"], "world_at": self._loan_time_locked(), **facts,
        })

    def _handoff_key_locked(self, giver: dict[str, Any], receiver: dict[str, Any], key: dict[str, Any]) -> str:
        sid = giver["space_id"]
        aid = giver["actor_id"]
        if giver.get("holding_id") != key["entity_id"] or key["parent_id"] != f"actor:{aid}":
            return "key_not_held"
        if receiver.get("holding_id"):
            return "receiver_hands_full"
        if sid != receiver["space_id"]:
            return "different_space"
        if receiver["posture"] == "resting":
            return "receiver_resting"
        x, y = receiver["x"], receiver["y"]
        reach = {(x + dx, y + dy) for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1))}
        path = self._bfs_locked(sid, (giver["x"], giver["y"]), reach, ignore_actor_id=aid)
        if not path:
            return "route_blocked"
        if len(path) > 1:
            self._finish_move_locked(giver, path)
        giver["holding_id"] = None
        receiver["holding_id"] = key["entity_id"]
        key.update({"parent_id": f"actor:{receiver['actor_id']}", "x": x, "y": y, "z": -1, "space_id": sid})
        return "transferred"

    def lend_key(self, lender_id: str, borrower_id: str, item_id: str, *, minutes: int = 30) -> dict[str, Any]:
        with self._lock:
            if not 1 <= minutes <= 1440:
                raise ValueError("借用时间须在 1 到 1440 分钟之间。")
            actors = self._world["actors"]
            lender, borrower = actors.get(lender_id), actors.get(borrower_id)
            key = self._world["entities"].get(item_id)
            if not lender or not borrower or lender_id == borrower_id or not key or "key" not in key["tags"]:
                raise ValueError("请选一把钥匙和另一位角色。")
            loans = self._world["key_loans"]
            if any(loan["item_id"] == item_id and loan["status"] == "open" for loan in loans.values()):
                raise ValueError("这把钥匙还在借用中。")
            outcome = self._handoff_key_locked(lender, borrower, key)
            if outcome != "transferred":
                raise ValueError({
                    "key_not_held": "请先让出借人拿起钥匙。", "receiver_hands_full": "借用人手上已有物品。",
                    "different_space": "两人要在同一空间交接。", "receiver_resting": "借用人正在休息。",
                    "route_blocked": "通往借用人的路被挡住了。",
                }[outcome])
            now = self._loan_time_locked()
            loan_id = f"loan_{self._world['next_seq']}"
            loan = {
                "id": loan_id, "item_id": item_id, "item_name": key["name"],
                "lender_id": lender_id, "borrower_id": borrower_id, "status": "open",
                "started_at": now, "due_at": now + minutes * 60, "original_due_at": now + minutes * 60,
            }
            loans[loan_id] = loan
            self._record_loan_event_locked("key_lent", lender_id, loan, borrower_id=borrower_id,
                                           due_at=loan["due_at"], witness_ids=[lender_id, borrower_id])
            self._save_world_locked()
            return copy.deepcopy(loan)

    def return_key(self, borrower_id: str, loan_id: str) -> dict[str, Any]:
        with self._lock:
            loan = self._loan_participant_locked(loan_id, borrower_id, "borrower")
            if loan["status"] == "returned":
                return {"outcome": "returned", "late": loan["late"]}
            if loan["status"] != "open":
                raise ValueError("这笔借用已结束。")
            actors = self._world["actors"]
            borrower, lender = actors.get(borrower_id), actors.get(loan["lender_id"])
            key = self._world["entities"].get(loan["item_id"])
            if not borrower or not lender or not key:
                raise ValueError("借用双方或钥匙已不在世界中。")
            outcome = self._handoff_key_locked(borrower, lender, key)
            if outcome != "transferred":
                self._record_loan_event_locked("key_return_blocked", borrower_id, loan, reason=outcome,
                                               witness_ids=[borrower_id])
                self._save_world_locked()
                return {"outcome": outcome}
            now = self._loan_time_locked()
            loan.update({"status": "returned", "returned_at": now, "late": now > loan["due_at"]})
            self._record_loan_event_locked("key_returned", borrower_id, loan, lender_id=lender["actor_id"],
                                           late=loan["late"], witness_ids=[borrower_id, lender["actor_id"]])
            self._save_world_locked()
            return {"outcome": "returned", "late": loan["late"]}

    def send_key_loan_extension(self, borrower_id: str, loan_id: str, *, minutes: int = 60) -> None:
        with self._lock:
            loan = self._loan_participant_locked(loan_id, borrower_id, "borrower")
            if loan["status"] != "open":
                raise ValueError("这笔借用已结束。")
            if loan.get("notice"):
                return
            if not 1 <= minutes <= 1440:
                raise ValueError("延期时长须在 1 到 1440 分钟之间。")
            now = self._loan_time_locked()
            requested_due = max(now, loan["due_at"]) + minutes * 60
            loan["notice"] = {"sent_at": now, "requested_due": requested_due}
            self._record_loan_event_locked("key_extension_sent", borrower_id, loan, requested_due=requested_due,
                                           witness_ids=[borrower_id])
            self._save_world_locked()

    def deliver_key_loan_extension(self, borrower_id: str, loan_id: str) -> None:
        with self._lock:
            loan = self._loan_participant_locked(loan_id, borrower_id, "borrower")
            notice = loan.get("notice")
            if loan["status"] != "open" or not notice:
                raise ValueError("当前没有待投递的延期请求。")
            if "received_at" in notice:
                return
            notice["received_at"] = self._loan_time_locked()
            self._record_loan_event_locked("key_extension_received", loan["lender_id"], loan,
                                           witness_ids=[borrower_id, loan["lender_id"]])
            self._save_world_locked()

    def key_loans_for(self, actor_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = []
            for loan in self._world["key_loans"].values():
                if actor_id not in (loan["borrower_id"], loan["lender_id"]):
                    continue
                row = copy.deepcopy(loan)
                row.pop("responded_to", None)
                row["overdue"] = row["status"] == "open" and self._loan_time_locked() > row["due_at"]
                if actor_id == row["lender_id"] and "received_at" not in (row.get("notice") or {}):
                    row.pop("notice", None)
                if actor_id == row["borrower_id"] and (row.get("reaction") or {}).get("choice") != "accept":
                    row.pop("reaction", None)
                rows.append(row)
            return rows

    def _loan_evidence_locked(self, loan: Mapping[str, Any]) -> str:
        if loan["status"] != "open":
            return ""
        notice = loan.get("notice") or {}
        now = self._loan_time_locked()
        if "received_at" in notice and "accepted_at" not in notice and notice["requested_due"] > now:
            evidence = "extension_received"
        else:
            evidence = "overdue" if now > loan["due_at"] else ""
        previous = (loan.get("responded_to") or {}).get(evidence) or {}
        if previous.get("due_at") == loan["due_at"] and previous.get("retry_at", float("inf")) > now:
            return ""
        return evidence

    def pending_key_loan_response(self, lender_id: str) -> dict[str, Any] | None:
        with self._lock:
            for loan in self._world["key_loans"].values():
                if loan["lender_id"] != lender_id:
                    continue
                evidence = self._loan_evidence_locked(loan)
                if evidence:
                    return {"loan_id": loan["id"], "item_name": loan["item_name"], "evidence": evidence}
            return None

    def respond_to_key_loan(self, lender_id: str, loan_id: str, evidence: str, choice: str) -> bool:
        with self._lock:
            loan = self._loan_participant_locked(loan_id, lender_id, "lender")
            if not evidence or evidence != self._loan_evidence_locked(loan):
                return False
            if choice not in ("wait", "ask", "accept") or (choice == "accept" and evidence != "extension_received"):
                raise ValueError("当前信息不足以接受延期。")
            checked = {"due_at": loan["due_at"]}
            if choice == "wait":
                checked["retry_at"] = self._loan_time_locked() + 10 * 60
            loan.setdefault("responded_to", {})[evidence] = checked
            loan["reaction"] = {"choice": choice, "evidence": evidence}
            if choice == "accept":
                loan["due_at"] = loan["notice"]["requested_due"]
                loan["notice"]["accepted_at"] = self._loan_time_locked()
            witnesses = [lender_id, loan["borrower_id"]] if choice == "accept" else [lender_id]
            self._record_loan_event_locked("key_loan_response", lender_id, loan, choice=choice, evidence=evidence,
                                           due_at=loan["due_at"], witness_ids=witnesses)
            self._save_world_locked()
            return True

    def _queue_persona_event_locked(
        self, actor: Mapping[str, Any], kind: str, entity: Mapping[str, Any] | None, action: str, message: str,
        *, ok: bool = True, peer: Mapping[str, Any] | None = None, mood_delta: float = 0.0,
        outcome: str = "",
    ) -> dict[str, Any]:
        participants = [{"actor_id": actor["actor_id"], "name": actor["name"], "role": "initiator"}]
        if peer is not None:
            role = "helper" if action == "invite_puzzle" else "peer"
            participants.append({"actor_id": peer["actor_id"], "name": peer["name"], "role": role})
        source = self._world["event_source"]
        event_id = f"{source}:{self._world['next_seq']}"
        target = entity or {}
        activity = (target.get("traits") or {}).get("activity") or {}
        payload = {
            "event_id": event_id,
            "source": source,
            "kind": kind,
            "action": action,
            "outcome": outcome or ("success" if ok else "blocked"),
            "entity_id": target.get("entity_id", ""),
            "entity_name": target.get("name", ""),
            "domain": str(activity.get("domain") or "nonGame"),
            "participants": participants,
            "witness_ids": [person["actor_id"] for person in participants],
            "label": clip_text(message, 80),
            "effect_mood_delta": mood_delta,
        }
        cognition = actor.get("autonomy", {}).get("cognition") or {}
        impression = cognition.get("semantic", {}).get(payload["entity_id"], {})
        if ok and impression.get("epa"):
            payload["epa"] = impression["epa"]
        event = self._append_event_locked(
            "actor_action_outcome", actor_id=actor["actor_id"], space_id=actor["space_id"], payload=payload,
        )
        if kind:
            self._world["persona_events"][event_id] = {**event, "pending": list(payload["witness_ids"])}
        return event

    def record_social_feedback(
        self, actor_id: str, space_id: str, message: str, *, peer_id: str = "", mood_delta: float = 0.0,
        spoken: bool = True, speech_act: Mapping[str, Any] | None = None,
    ) -> None:
        """Capture a completed exchange or an observed interval of solitude."""
        with self._lock:
            actors = self._world["actors"]
            actor = actors.get(actor_id)
            if actor is None or actor["space_id"] != space_id:
                return
            if peer_id:
                peer = actors.get(peer_id)
                if peer is None or peer_id == actor_id or peer["space_id"] != space_id:
                    return
                event = self._queue_persona_event_locked(actor, "social_gesture", None, "talk", message, peer=peer,
                                                         mood_delta=mood_delta)
                realm = self._realm_space_id_locked(space_id)
                has_sa = isinstance(speech_act, Mapping) and bool(speech_act)
                split_sa = has_sa and ("a" in speech_act or "b" in speech_act)
                for idx_r, (receiver, partner) in enumerate(((actor, peer), (peer, actor))):
                    side_raw = (speech_act.get("a" if idx_r == 0 else "b") if split_sa else speech_act) if has_sa else None
                    side_sa = copy.deepcopy(dict(side_raw)) if isinstance(side_raw, Mapping) and side_raw else None
                    if self._world["spaces"][realm].get("space_kind") == "town" or "cognition" in receiver["autonomy"] or side_sa:
                        cognition = receiver["autonomy"].setdefault("cognition", {})
                        sig: dict[str, Any] = {
                            "seq": event["seq"], "minute": self._world["simulation_minutes"],
                            "actor": partner["actor_id"], "name": partner["name"], "mood_delta": mood_delta, "spoken": spoken,
                        }
                        if side_sa:
                            sig["speech_act"] = side_sa
                            if side_sa.get("shared_place_id") or side_sa.get("personal_goal") or side_sa.get("tone") == "tense":
                                cognition["preempt_seq"] = int(cognition.get("preempt_seq", 0)) + 1
                        cognition["social_signal"] = sig
                        self._sync_actor_world_state_locked(receiver)
            else:
                if any(person["space_id"] == space_id and aid != actor_id for aid, person in actors.items()):
                    return
                self._queue_persona_event_locked(actor, "alone_tick", None, "idle", message)
            self._save_world_locked()

    def inject_language_modulation(
        self, actor_id: str, payload: Mapping[str, Any], *, preempt: bool = False,
    ) -> bool:
        """将语言交互产出的认知调节（议程偏置/信念/行动承诺）注入角色认知状态。"""
        if not isinstance(payload, Mapping) or not payload:
            return False
        with self._lock:
            actor = self._world["actors"].get(actor_id)
            if actor is None:
                return False
            sid = actor["space_id"]
            realm, realm_places = self._realm_places_locked(sid)
            realm_peers = {
                aid: a for aid, a in self._world["actors"].items()
                if aid != actor_id and not a.get("is_player") and self._realm_space_id_locked(a["space_id"]) == realm
            }
            realm_sp = self._world["spaces"].get(realm) or {}
            cur_sp = self._world["spaces"].get(sid) or {}
            valid_zones = {
                str(z.get("zone_id") or "")
                for z in (*(realm_sp.get("zones") or ()), *(cur_sp.get("zones") or ()))
                if isinstance(z, Mapping) and z.get("zone_id")
            }

            agenda = clip_text(payload.get("agenda"), 64)
            half_life = clamp_float(payload.get("half_life_min"), 5, 45, 25)

            raw_dom = payload.get("domain_bias")
            domains = {
                str(k): clamp_float(v, -0.6, 0.6, 0)
                for k, v in raw_dom.items() if str(k) in _MODULATION_DOMAINS
            } if isinstance(raw_dom, Mapping) else {}

            raw_plc = payload.get("place_bias")
            places = {
                str(k): clamp_float(v, -0.6, 0.6, 0)
                for k, v in raw_plc.items() if str(k) in realm_places
            } if isinstance(raw_plc, Mapping) else {}

            pref_zone = str(payload.get("preferred_zone") or "").strip()
            if pref_zone not in valid_zones:
                pref_zone = ""

            beliefs: list[dict[str, str]] = []
            raw_beliefs = payload.get("beliefs")
            if isinstance(raw_beliefs, list):
                for item in raw_beliefs[:6]:
                    if not isinstance(item, Mapping):
                        continue
                    pid = str(item.get("place_id") or "").strip()
                    dom = str(item.get("domain") or "").strip()
                    if pid in realm_places and dom in _MODULATION_DOMAINS:
                        beliefs.append({"place_id": pid, "domain": dom, "note": clip_text(item.get("note"), 48)})

            peer_beliefs: list[dict[str, str]] = []
            raw_peer_beliefs = payload.get("peer_beliefs")
            if isinstance(raw_peer_beliefs, list):
                for item in raw_peer_beliefs[:6]:
                    if not isinstance(item, Mapping):
                        continue
                    peer_id = str(item.get("actor_id") or "").strip()
                    pid = str(item.get("place_id") or "").strip()
                    if peer_id in realm_peers and pid in realm_places:
                        peer_beliefs.append({"actor_id": peer_id, "place_id": pid})

            commitment: dict[str, Any] | None = None
            commit_pid = str(payload.get("commit_place_id") or "").strip()
            commit_peer = str(payload.get("commit_peer_id") or "").strip()
            raw_commit = payload.get("commitment")
            if commit_pid in realm_places:
                cdrive = str(payload.get("commit_drive") or "").strip()
                if cdrive not in _MODULATION_DOMAINS:
                    cdrive = "explore"
                commitment = {
                    "kind": "visit", "target": commit_pid, "drive": cdrive,
                    "reason": agenda or f"去「{realm_places[commit_pid]}」",
                    "priority": "high", "source": "language",
                }
            elif commit_peer in realm_peers:
                p_name = str(realm_peers[commit_peer].get("name") or commit_peer)
                commitment = {
                    "kind": "social", "target": commit_peer, "drive": "social",
                    "reason": agenda or f"去找{p_name}",
                    "priority": "high", "source": "language",
                }
            elif isinstance(raw_commit, Mapping):
                ckind = str(raw_commit.get("kind") or "").strip()
                ctgt = str(raw_commit.get("target") or "").strip()
                cdrive = str(raw_commit.get("drive") or "").strip()
                if ckind == "visit" and ctgt in realm_places:
                    commitment = {
                        "kind": "visit", "target": ctgt,
                        "drive": cdrive if cdrive in _MODULATION_DOMAINS else "explore",
                        "reason": clip_text(raw_commit.get("reason"), 48) or agenda or f"去「{realm_places[ctgt]}」",
                        "priority": "high" if str(raw_commit.get("priority") or "").lower() == "high" else "normal",
                        "source": "language",
                    }
                elif ckind == "social" and ctgt in realm_peers:
                    p_name = str(realm_peers[ctgt].get("name") or ctgt)
                    commitment = {
                        "kind": "social", "target": ctgt, "drive": "social",
                        "reason": clip_text(raw_commit.get("reason"), 48) or agenda or f"去找{p_name}",
                        "priority": "high" if str(raw_commit.get("priority") or "").lower() == "high" else "normal",
                        "source": "language",
                    }
                elif ckind == "entity" and ctgt in self._world["entities"] and self._world["entities"][ctgt].get("space_id") == sid:
                    ent_name = str(self._world["entities"][ctgt].get("name") or ctgt)
                    commitment = {
                        "kind": "entity", "target": ctgt,
                        "drive": cdrive if cdrive in _MODULATION_DOMAINS else "curiosity",
                        "reason": clip_text(raw_commit.get("reason"), 48) or agenda or f"看看「{ent_name}」",
                        "priority": "high" if str(raw_commit.get("priority") or "").lower() == "high" else "normal",
                        "source": "language",
                    }

            if not any((agenda, domains, places, pref_zone, beliefs, peer_beliefs, commitment)):
                return False

            mod_id = time.monotonic_ns()
            if commitment is not None:
                commitment["id"] = f"lang:{mod_id}"
            packet: dict[str, Any] = {
                "id": mod_id, "realm": realm, "at": self._world["simulation_minutes"],
                "source": "language", "strength": 1.0,
                "agenda": agenda, "half_life": half_life,
                "domains": domains, "places": places, "preferred_zone": pref_zone,
                "semantic": {}, "experience": "", "thought": "", "diary": "",
                "relax": 0.0, "persistence": 0.0, "persist_think": False,
                "beliefs": beliefs,
                "peer_beliefs": peer_beliefs,
            }
            if commitment is not None:
                packet["commitment"] = commitment

            cognition = actor["autonomy"].setdefault("cognition", {})
            cognition["pending_modulation"] = packet
            if preempt or commitment is not None:
                cognition["preempt_seq"] = int(cognition.get("preempt_seq", 0)) + 1
            self._sync_actor_world_state_locked(actor)
            self._save_world_locked()
            return True

    def pending_persona_events(self) -> list[dict[str, Any]]:
        with self._lock:
            return copy.deepcopy(sorted(self._world["persona_events"].values(), key=lambda event: event["seq"]))

    def acknowledge_persona_event(
        self, event_id: str, actor_id: str, mood: float | None,
        appraisal: Mapping[str, Any] | None = None,
    ) -> None:
        with self._lock:
            outbox = self._world["persona_events"]
            event = outbox.get(event_id)
            if not event or actor_id not in event["pending"]:
                return
            if mood is not None and actor_id in self._world["actors"]:
                actor = self._world["actors"][actor_id]
                same_realm = self._realm_space_id_locked(actor["space_id"]) == self._realm_space_id_locked(event["space_id"])
                self.set_actor_world_mood(
                    actor_id, space_id=actor["space_id"] if same_realm else event["space_id"],
                    vitals_patch={"mood_v": mood, "source": "nanami_persona"},
                    record_event=False, persist=False,
                )
                if same_realm and appraisal and "cognition" in actor["autonomy"]:
                    actor["autonomy"]["cognition"]["last_appraisal"] = {
                        "seq": event["seq"],
                        "delta_mood": clamp_float(appraisal.get("deltaMood"), -1, 1, 0),
                        "deflection": clamp_float(appraisal.get("deflection"), 0, 5, 0),
                    }
                    self._sync_actor_world_state_locked(actor)
            if appraisal and event["payload"]["kind"] == "cooperative_solve":
                for memory in self._world["puzzle_memories"].get(event["space_id"], []):
                    if memory["event_id"] == event_id:
                        memory.setdefault("appraisals", {})[actor_id] = dict(appraisal)
                        break
            event["pending"].remove(actor_id)
            if not event["pending"]:
                del outbox[event_id]
            self._save_world_locked()

    def puzzle_memories(self, actor_id: str, space_id: str) -> list[dict[str, Any]]:
        with self._lock:
            history = self._world["puzzle_memories"].get(space_id, [])
            return copy.deepcopy([
                {
                    **{key: value for key, value in memory.items() if key != "appraisals"},
                    "appraisal": memory.get("appraisals", {}).get(actor_id),
                }
                for memory in reversed(history) if actor_id in memory["witness_ids"]
            ])

    def puzzle_peers(self, actor_id: str, entity_id: str) -> list[str]:
        with self._lock:
            actor = self._world["actors"].get(actor_id)
            entity = self._world["entities"].get(entity_id)
            if actor is None or entity is None or entity["space_id"] != actor["space_id"]:
                return []
            lock = entity["traits"].get("container") or {}
            tag = lock.get("key_tag")
            if not lock.get("is_locked") or not tag:
                return []
            return [
                peer["actor_id"] for peer in self._world["actors"].values()
                if peer["actor_id"] != actor_id and peer["space_id"] == actor["space_id"]
                and tag in (self._world["entities"].get(peer.get("holding_id")) or {}).get("tags", [])
            ]

    def invite_to_puzzle(self, actor_id: str, peer_id: str, entity_id: str, *, willing: bool) -> str:
        """Settle a request to use a companion's held key against the current world state."""
        with self._lock:
            if peer_id not in self.puzzle_peers(actor_id, entity_id):
                return "这位同伴现在没有能打开这个箱子的钥匙。"
            actor = self._world["actors"][actor_id]
            peer = self._world["actors"][peer_id]
            entity = self._world["entities"][entity_id]
            if any(entity_id not in {e["entity_id"] for e in self.visible_entities(person["actor_id"])}
                   for person in (actor, peer)):
                return "两人需要都能看到这个箱子。"
            sid = actor["space_id"]
            name = entity["name"]
            kind = ""
            vitals = peer["vitals"]
            if (not willing or peer["posture"] == "resting" or vitals["disengaged"]
                    or vitals["energy"] < 0.35 or vitals["sleep_pressure"] >= 0.68 or vitals["mood_v"] <= -0.35):
                message = f"{peer['name']}暂时不想帮忙打开「{name}」。"
                outcome = "declined"
            else:
                leader_ready, _ = self._walk_to_entity_locked(actor, entity)
                helper_ready, _ = self._walk_to_entity_locked(peer, entity) if leader_ready else (False, "")
                if not helper_ready:
                    message = f"通往「{name}」的路过不去，这次没能一起开锁。"
                    outcome = "blocked"
                else:
                    entity["traits"]["container"].update({"is_locked": False, "is_open": True})
                    self._record_act_locked(peer, "unlock", entity)
                    kind = "cooperative_solve"
                    message = f"{actor['name']}找到「{name}」，{peer['name']}用手里的钥匙打开了它。"
                    outcome = "success"
            event = self._queue_persona_event_locked(
                actor, kind, entity, "invite_puzzle", message, peer=peer, outcome=outcome,
            )
            if kind:
                history = self._world["puzzle_memories"].setdefault(sid, [])
                history.append({**event["payload"], "key_tag": entity["traits"]["container"]["key_tag"]})
                del history[:-80]
            self._save_world_locked()
            return message

    def list_actors(self) -> list[dict[str, Any]]:
        with self._lock:
            return [copy.deepcopy(a) for a in self._world["actors"].values()]

    def cognitive_scene(self, actor_id: str, *, include_rooms: bool = False) -> dict[str, Any] | None:
        """取自主执行需要的场景数据，省去界面快照中的素材与事件日志。"""
        with self._lock:
            actor = self._world["actors"].get(actor_id)
            if actor is None:
                return None
            sid = actor["space_id"]
            space = self._world["spaces"][sid]
            realm, realm_places = self._realm_places_locked(sid)
            if not include_rooms and self._world["spaces"][realm].get("space_kind") != "town":
                return None
            visible = self.visible_entities(actor_id, spatial_filter=True if space["space_kind"] != "room" else None)
            visible_peer_ids = sorted(self._visible_peer_ids_locked(actor))
            destinations = {}
            for entity in visible:
                target = entity.get("traits", {}).get("portal", {}).get("target_space_id")
                if target in self._world["spaces"] and self._realm_space_id_locked(target) == realm:
                    destinations[target] = self._world["spaces"][target]["name"]
            realm_space = self._world["spaces"].get(realm) or {}
            realm_actors = [
                {
                    "actor_id": aid_x, "name": a_x["name"],
                    "host_character": a_x.get("host_character") or a_x["name"],
                    "space_id": a_x["space_id"], "x": a_x["x"], "y": a_x["y"], "posture": a_x["posture"],
                }
                for aid_x, a_x in self._world["actors"].items()
                if aid_x != actor_id and not a_x.get("is_player") and self._realm_space_id_locked(a_x["space_id"]) == realm
            ]
            return {
                "actor": copy.deepcopy(actor),
                "actors": copy.deepcopy([a for a in self._world["actors"].values() if a["space_id"] == sid]),
                "space": copy.deepcopy(space),
                "realm": realm,
                "realm_places": realm_places,
                "realm_buildings": copy.deepcopy(realm_space.get("buildings") or []),
                "realm_actors": realm_actors,
                "player_ids": {aid for aid, a in self._world["actors"].items() if a.get("is_player")},
                "minutes": self._world["simulation_minutes"],
                "visible": visible,
                "visible_peer_ids": visible_peer_ids,
                "destinations": destinations,
                "lighting": compute_lighting(
                    space, self._world["entities"], time.time(), self._world["clock_offset_minutes"],
                ),
            }

    def live_actor_frame(self, actor_id: str) -> dict[str, Any] | None:
        with self._lock:
            actor = self._world["actors"].get(actor_id)
            if not actor:
                return None
            fields = ("actor_id", "name", "space_id", "x", "y", "facing", "move_seq", "last_path",
                      "last_act", "posture", "resting_on_id", "holding_id", "vitals", "expression")
            frame = {key: copy.deepcopy(actor[key]) for key in fields}
            frame["autonomy"] = {
                key: actor["autonomy"].get(key) for key in (
                    "enabled", "current_goal", "current_drive", "last_thought", "hunger", "curiosity",
                )
            }
            zone = self.get_zone_at(self._world["spaces"][actor["space_id"]], actor["x"], actor["y"])
            frame["zone"] = copy.deepcopy(zone) if zone else None
            cog = actor["autonomy"].get("cognition") or {}
            packet = cog.get("modulation") or {}
            active_int = cog.get("active_intention") or {}
            frame["is_player"] = bool(actor.get("is_player", False))
            frame["reflection_state"] = {
                "id": packet.get("id"), "strength": packet.get("strength", 1.0),
                "preempt_seq": int(cog.get("preempt_seq", 0)),
                "seek_peer_target": str(active_int.get("target") or "") if active_int.get("kind") == "seek_peer" else "",
            }
            return frame

    def pending_preempt_seq(self, actor_id: str) -> int:
        with self._lock:
            actor = self._world["actors"].get(actor_id)
            if not actor:
                return 0
            return int(((actor.get("autonomy") or {}).get("cognition") or {}).get("preempt_seq", 0))

    def live_realm_peers(self, actor_id: str) -> list[str]:
        with self._lock:
            actor = self._world["actors"].get(actor_id)
            if not actor:
                return []
            realm = self._realm_space_id_locked(actor["space_id"])
            return [
                aid for aid, peer in self._world["actors"].items()
                if aid != actor_id and not peer.get("is_player") and peer["autonomy"]["enabled"]
                and self._realm_space_id_locked(peer["space_id"]) == realm
            ]

    def live_space_changes(self, space_id: str, *, entity_ids: list[str] | None = None) -> dict[str, Any]:
        with self._lock:
            space = self._world["spaces"].get(space_id)
            if not space:
                return {}
            world_entities = self._world["entities"]
            candidates = world_entities.values() if entity_ids is None else (world_entities.get(eid) for eid in entity_ids)
            entities = []
            for entity in candidates:
                if entity is not None and entity["space_id"] == space_id and not entity["parent_id"].startswith("inventory:"):
                    item = copy.deepcopy(entity)
                    item["sprite"] = self._sprite_with_data_url(item["sprite"])
                    item["state_tags"] = self.entity_state_tags(item)
                    entities.append(item)
            result = {"space_id": space_id, "entities": entities, "lighting": compute_lighting(
                space, world_entities, time.time(), self._world["clock_offset_minutes"],
            )}
            if entity_ids is not None:
                present = {ent["entity_id"] for ent in entities}
                result["removed_entity_ids"] = [eid for eid in entity_ids if eid not in present]
            return result

    def _player_near_prompt_locked(self, player: Mapping[str, Any]) -> dict[str, Any]:
        pid = str(player["actor_id"])
        sid = str(player["space_id"])
        px, py = int(player.get("x") or 0), int(player.get("y") or 0)
        facing = str(player.get("facing") or "down")
        dx, dy = {"up": (0, -1), "down": (0, 1), "left": (-1, 0), "right": (1, 0)}.get(facing, (0, 1))
        fx, fy = px + dx, py + dy

        def _score_pos(tx: int, ty: int) -> tuple[int, int]:
            return (0 if (tx, ty) == (fx, fy) else 1, max(abs(tx - px), abs(ty - py)))

        ranked: list[tuple[tuple[int, int, int, int], dict[str, Any]]] = []
        front_actor = None
        for aid, actor in self._world["actors"].items():
            if aid == pid or actor["space_id"] != sid:
                continue
            pos = _score_pos(actor["x"], actor["y"])
            if pos[1] > 1:
                continue
            if pos[0] == 0:
                front_actor = aid
            ranked.append(((*pos, 3, 0), {
                "kind": "actor",
                "target_id": aid,
                "name": actor["name"],
                "label": f"和 {actor['name']} 对话",
            }))

        space = self._world["spaces"][sid]
        through_actor = front_actor is not None and self._is_floor_walkable_locked(
            space, fx, fy, ignore_actor_id=front_actor,
        )
        priority = (
            "leave_rest", "rest", "consume", "interact", "read",
            "turn_on", "turn_off", "open", "unlock", "close", "take", "look",
        )
        sight = None
        for ent in self._world["entities"].values():
            if ent.get("space_id") != sid or int(ent.get("z", 0)) < 0:
                continue
            cells = self._entity_cells(ent)
            best = min((_score_pos(cx, cy) for cx, cy in cells), default=(9, 9))
            portal = ent["traits"].get("portal")
            is_door = portal is not None and portal.get("target_space_id") in self._world["spaces"]
            blocked_approach = bool(through_actor and is_door and (fx + dx, fy + dy) in cells and best[1] > 1)
            if best[1] > 1 and not blocked_approach:
                continue
            if sight is None:
                sight = self._sight_context_locked(player)
            if not any(self._point_seen_locked(sight, cell) for cell in cells):
                continue
            if is_door:
                rank = (0, 1, 2, 0) if blocked_approach else (*best, 0, 0)
                ranked.append((rank, {
                    "kind": "portal",
                    "target_id": ent["entity_id"],
                    "name": ent["name"],
                    "label": f"推开「{ent['name']}」",
                    "blocked_approach": blocked_approach,
                }))
                continue
            verbs = dict(self.legal_verbs(ent, player))
            action = next(key for key in priority if key in verbs)
            ranked.append(((*best, 4 if action == "look" else 1, -int(ent.get("z", 0))), {
                "kind": "entity",
                "target_id": ent["entity_id"],
                "name": ent["name"],
                "action": action,
                "label": f"{verbs[action]}「{ent['name']}」",
            }))
        if ranked:
            return min(ranked, key=lambda item: item[0])[1]
        return {"kind": "", "target_id": "", "name": "", "label": ""}

    def ensure_player_avatar(self, *, space_id: str = "", name: str = "你") -> dict[str, Any]:
        """在当前空间确保存在独立的主控「玩家」像素小人（不受 AI 自主心跳接管）。"""
        with self._lock:
            pid = "player_avatar"
            target_sid = str(space_id or self._world["active_space_id"]).strip()
            if target_sid not in self._world["spaces"]:
                target_sid = self._world["active_space_id"]
            existing = self._world["actors"].get(pid)
            if existing is not None:
                if space_id and existing.get("space_id") != target_sid:
                    space = self._world["spaces"][target_sid]
                    tx, ty = self._find_open_floor_cell_locked(space, preferred_x=space["width"] // 2, preferred_y=space["height"] // 2, ignore_actor_id=pid)
                    self._clear_actor_rest_locked(existing)
                    self._switch_actor_world_state_locked(existing, target_sid)
                    existing["x"] = tx
                    existing["y"] = ty
                    self._record_teleport_locked(existing)
                    self._save_world_locked()
                return copy.deepcopy(existing)

            space = self._world["spaces"][target_sid]
            tx, ty = self._find_open_floor_cell_locked(
                space,
                preferred_x=max(1, space["width"] // 2 - 1),
                preferred_y=max(space.get("wall_rows", 0), space["height"] // 2),
                ignore_actor_id=pid,
            )
            player = normalize_actor({
                "actor_id": pid,
                "name": clip_text(name, 20, "你"),
                "is_player": True,
                "space_id": target_sid,
                "x": tx,
                "y": ty,
                "facing": "down",
                "appearance": {
                    "hair_style": "short",
                    "hair_color": "#4f6d9a",
                    "outfit_style": "jacket",
                    "outfit_color": "#3d85c6",
                    "skin_color": "#ffe2cf",
                    "eye_color": "#2f5d8a",
                    "accessory": "badge",
                },
                "sprite": {
                    "mode": "pixel_grid",
                    "accent_color": "#7cd4ff",
                },
                "autonomy": {"enabled": False, "current_goal": "自由漫游"},
            })
            self._world["actors"][pid] = player
            self._sync_actor_world_state_locked(player)
            self._append_event_locked(
                "actor_imported",
                actor_id=pid,
                space_id=target_sid,
                payload={"name": player["name"], "is_player": True, "x": tx, "y": ty},
            )
            self._save_world_locked()
            return copy.deepcopy(player)

    @staticmethod
    def _carries_item(actor: Mapping[str, Any], item: Mapping[str, Any]) -> bool:
        aid = actor.get("actor_id")
        return bool(aid and item.get("parent_id") in (f"actor:{aid}", f"inventory:{aid}"))

    @staticmethod
    def _pack_size(actor: Mapping[str, Any]) -> int:
        return PLAYER_INVENTORY_SIZE if actor.get("is_player") else NPC_INVENTORY_SIZE

    def _pack_items_locked(self, actor_id: str) -> list[dict[str, Any]]:
        return [item for item in self._world["entities"].values() if item["parent_id"] == f"inventory:{actor_id}"]

    def npc_pack(self, actor_id: str) -> dict[str, Any]:
        with self._lock:
            actor = self._world["actors"][actor_id]
            items = self._pack_items_locked(actor_id)
            if not actor.get("inventory_started"):
                actor["inventory_started"] = True
                if not items:
                    snack = self._instantiate_blueprint_locked(
                        self._builtin_blueprints["default_snack"], actor["space_id"], f"inventory:{actor_id}",
                        actor["x"], actor["y"], -1,
                    )
                    snack["inventory_slot"] = 0
                    self._world["entities"][snack["entity_id"]] = snack
                    items.append(snack)
                self._save_world_locked()
            return {"items": copy.deepcopy(items), "capacity": self._pack_size(actor)}

    def can_stow(self, entity_id: str) -> bool:
        with self._lock:
            item = self._world["entities"].get(entity_id)
            return item is not None and self._packable_locked(item) and not self._is_open_loan_item_locked(entity_id)

    def _is_open_loan_item_locked(self, entity_id: str) -> bool:
        return any(loan["item_id"] == entity_id and loan["status"] == "open" for loan in self._world["key_loans"].values())

    @staticmethod
    def _inventory_slots(items: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
        slots = {}
        unassigned = []
        for item in items:
            slot = item.get("inventory_slot")
            if isinstance(slot, int) and 0 <= slot < PLAYER_INVENTORY_SIZE and slot not in slots:
                slots[slot] = item
            else:
                unassigned.append(item)
        for item in unassigned:
            slot = next(i for i in range(len(slots) + 1) if i not in slots)
            slots[slot] = item
        return slots

    def _packable_locked(self, item: Mapping[str, Any]) -> bool:
        traits = item["traits"]
        return (
            traits.get("portable", {}).get("weight") == "light" and "portal" not in traits
            and not traits.get("restable", {}).get("occupied_by")
            and not any(e["parent_id"] == f"entity:{item['entity_id']}" for e in self._world["entities"].values())
        )

    def _free_pack_slot_locked(self, actor: Mapping[str, Any], preferred: int = -1) -> int:
        slots = self._inventory_slots(self._pack_items_locked(actor["actor_id"]))
        if preferred >= 0:
            if preferred >= self._pack_size(actor) or preferred in slots:
                raise ValueError("目标背包格不可用。")
            return preferred
        slot = next((i for i in range(self._pack_size(actor)) if i not in slots), None)
        if slot is None:
            raise ValueError(f"「{actor['name']}」的背包已满。")
        return slot

    def _store_in_pack_locked(self, actor: dict[str, Any], item: dict[str, Any], slot: int) -> None:
        aid = actor["actor_id"]
        if actor.get("holding_id") == item["entity_id"]:
            actor["holding_id"] = None
        item.update(parent_id=f"inventory:{aid}", space_id=actor["space_id"], x=actor["x"], y=actor["y"],
                    z=-1, inventory_slot=slot)

    def _accessible_chest_locked(self, actor: Mapping[str, Any], chest_id: str, *, require_open: bool = True) -> dict[str, Any]:
        chest = self._world["entities"].get(chest_id)
        traits = chest["traits"].get("container") if chest else None
        if traits is None or chest["z"] < 0 or self.chebyshev_distance(actor, chest) > 1:
            raise ValueError("请靠近要使用的箱子。")
        if traits.get("owner_actor_id") not in (None, "", actor["actor_id"]):
            raise ValueError(f"「{chest['name']}」是其他角色的私有箱。")
        if require_open and (traits["is_locked"] or not traits["is_open"]):
            raise ValueError(f"请先打开「{chest['name']}」。")
        return chest

    def _inventory_item_view_locked(self, actor: Mapping[str, Any], item: Mapping[str, Any], slot: int) -> dict[str, Any]:
        verbs = dict(self.legal_verbs(item, actor))
        blueprint_id = item["blueprint_id"]
        blueprint = self._custom_blueprints.get(blueprint_id) or self._builtin_blueprints.get(blueprint_id) or {}
        sprite = item["sprite"]
        if sprite.get("image_path") != (blueprint.get("sprite") or {}).get("image_path"):
            sprite = self._sprite_with_data_url(sprite)
        return {
            "slot": slot, "entity_id": item["entity_id"], "name": item["name"],
            "blueprint_id": blueprint_id,
            "brief": self.entity_display_brief(item), "tags": self.entity_state_tags(item),
            "sprite": copy.deepcopy(sprite), "traits": copy.deepcopy(item["traits"]),
            "actions": [{"verb": verb, "label": verbs[verb]} for verb in INVENTORY_USE_VERBS if verb in verbs],
        }

    def _gift_is_near_locked(self, giver: Mapping[str, Any], recipient: Mapping[str, Any]) -> bool:
        return (
            giver["space_id"] == recipient["space_id"] and giver["actor_id"] != recipient["actor_id"]
            and max(abs(giver["x"] - recipient["x"]), abs(giver["y"] - recipient["y"])) <= 1
            and giver["posture"] != "resting" and recipient["posture"] != "resting"
        )

    def _gift_offer_is_live_locked(self, offer: Mapping[str, Any]) -> bool:
        actors = self._world["actors"]
        giver, recipient = actors.get(offer.get("giver_id")), actors.get(offer.get("recipient_id"))
        item = self._world["entities"].get(offer.get("entity_id"))
        return bool(
            giver and recipient and item and time.time() < offer.get("expires_at", 0)
            and self._gift_is_near_locked(giver, recipient) and self._carries_item(giver, item)
        )

    def gift_offers_for(self, actor_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {**offer, "giver_name": self._world["actors"][offer["giver_id"]]["name"],
                 "item_name": self._world["entities"][offer["entity_id"]]["name"]}
                for offer in self._world["gift_offers"].values()
                if offer.get("recipient_id") == actor_id and self._gift_offer_is_live_locked(offer)
            ]

    def awaiting_gift_reply(self, actor_id: str) -> bool:
        with self._lock:
            return any(
                offer.get("giver_id") == actor_id and self._gift_offer_is_live_locked(offer)
                for offer in self._world["gift_offers"].values()
            )

    def give_item(
        self, giver_id: str, recipient_id: str, entity_id: str, *, custom_persona_dir: str = "",
    ) -> dict[str, Any]:
        with self._lock:
            actors = self._world["actors"]
            giver, recipient = actors.get(giver_id), actors.get(recipient_id)
            item = self._world["entities"].get(entity_id)
            if not giver or not recipient or not self._gift_is_near_locked(giver, recipient):
                raise ValueError("需要双方清醒并站在附近才能赠礼。")
            if not item or not self._carries_item(giver, item) or not self._packable_locked(item):
                raise ValueError("只能赠送自己携带的物品。")
            if self._is_open_loan_item_locked(entity_id):
                raise ValueError("正在借用的物品不能转赠。")
            self._free_pack_slot_locked(recipient)
            if recipient.get("is_player"):
                offers = self._world["gift_offers"]
                for offer_id, offer in list(offers.items()):
                    if not self._gift_offer_is_live_locked(offer):
                        del offers[offer_id]
                    elif offer["giver_id"] == giver_id:
                        raise ValueError("请先等对方回应上一份礼物。")
                offer_id = uuid4().hex
                offers[offer_id] = {
                    "offer_id": offer_id, "giver_id": giver_id, "recipient_id": recipient_id,
                    "entity_id": entity_id, "expires_at": time.time() + 30,
                }
                self._save_world_locked()
                return {"status": "offered", "message": f"{giver['name']}想把「{item['name']}」送给你。"}

            realm = self._realm_space_id_locked(recipient["space_id"])
            recipient_name = recipient["host_character"] or recipient["name"]
            giver_name = giver["host_character"] or giver["name"]
            key = "user" if giver.get("is_player") else f"char:{giver_name}"
            scope = {"custom_persona_dir": custom_persona_dir}
            relations = persona_bridge.read_actor_relations(
                self._project_root, recipient_name, space_id=realm, world_data_root=self._data_root,
                known_actors=[giver_name] if key != "user" else [], **scope,
            )
            profile = persona_bridge.read_persona_behavior_profile(self._project_root, recipient_name, **scope)
            relation = relations.get(key, {})
            accepted, preference = persona_bridge.evaluate_gift(profile, relation, recipient, item)
            if not accepted:
                return {"status": "declined", "message": f"{recipient['name']}婉拒了「{item['name']}」。"}

            now = self._world["simulation_minutes"]
            history = recipient.setdefault("gift_history", {})
            history_key = f"{giver_id}:{item['blueprint_id'] or item['name']}"
            recent = now - float(history.pop(history_key, -1e9)) < 10
            history[history_key] = now
            recipient["gift_history"] = dict(list(history.items())[-48:])
            impact = 0.10 * (preference - 0.20) * (0.2 if recent else 1.0)
            summary = self._complete_gift_locked(giver, recipient, item, preference)
            self._queue_persona_event_locked(recipient, "gift_received", item, "receive_gift", summary,
                                              mood_delta=impact)
            self._save_world_locked()
            if not recent:
                persona_bridge.record_world_gift(self._data_root, realm, recipient_name, key, giver_name,
                                                relation, summary, impact)
            return {"status": "accepted", "preference": preference, "message": summary}

    def respond_to_gift(self, player_id: str, offer_id: str, *, accept: bool) -> dict[str, Any]:
        with self._lock:
            offers = self._world["gift_offers"]
            offer = offers.get(offer_id)
            if not offer or offer.get("recipient_id") != player_id or not self._gift_offer_is_live_locked(offer):
                raise ValueError("这份礼物已不在交接范围内。")
            player = self._world["actors"][player_id]
            if not player.get("is_player"):
                raise ValueError("该礼物需要主控确认。")
            giver = self._world["actors"][offer["giver_id"]]
            item = self._world["entities"][offer["entity_id"]]
            if accept:
                message = self._complete_gift_locked(giver, player, item)
            else:
                message = f"你婉拒了{giver['name']}送来的「{item['name']}」。"
            del offers[offer_id]
            self._save_world_locked()
            return {"status": "accepted" if accept else "declined", "message": message}

    def _complete_gift_locked(
        self, giver: dict[str, Any], recipient: dict[str, Any], item: dict[str, Any], preference: float | None = None,
    ) -> str:
        slot = self._free_pack_slot_locked(recipient)
        if giver.get("holding_id") == item["entity_id"]:
            giver["holding_id"] = None
        self._store_in_pack_locked(recipient, item, slot)
        self._append_event_locked("gift_transferred", space_id=recipient["space_id"], actor_id=giver["actor_id"],
                                  payload={"entity_id": item["entity_id"], "item_name": item["name"],
                                           "recipient_id": recipient["actor_id"], "preference": preference})
        message = f"{recipient['name']}收下了{giver['name']}送来的「{item['name']}」。"
        if preference is not None and preference >= 0.72:
            message += "这份礼物很合心意。"
        return message

    def inventory_state(self, actor_id: str, chest_id: str = "") -> dict[str, Any]:
        with self._lock:
            actor = self._world["actors"].get(actor_id)
            if not actor:
                raise ValueError("角色不存在。")
            items = self._inventory_slots(self._pack_items_locked(actor_id))
            out = {
                "actor_id": actor_id, "capacity": self._pack_size(actor),
                "items": [self._inventory_item_view_locked(actor, item, slot) for slot, item in sorted(items.items())],
                "offers": self.gift_offers_for(actor_id),
            }
            if chest_id:
                chest = self._accessible_chest_locked(actor, chest_id)
                traits = chest["traits"]["container"]
                slots = self._inventory_slots(self._container_children_locked(chest_id))
                out["chest"] = {
                    "entity_id": chest_id, "name": chest["name"], "owner_actor_id": traits.get("owner_actor_id", ""),
                    "capacity": max(traits["capacity"], max(slots, default=-1) + 1),
                    "items": [self._inventory_item_view_locked(actor, item, slot) for slot, item in sorted(slots.items())],
                }
            return out

    def inventory_action(
        self, actor_id: str, operation: str, *, entity_id: str = "", chest_id: str = "",
        blueprint_id: str = "", slot: int = -1, verb: str = "", owner_id: str = "",
        facing: str = "",
    ) -> str:
        with self._lock:
            actor = self._world["actors"].get(actor_id)
            if not actor:
                raise ValueError("角色不存在。")
            sid = actor["space_id"]
            pack = self._pack_items_locked(actor_id)
            item = self._world["entities"].get(entity_id)
            chest = self._accessible_chest_locked(actor, chest_id) if chest_id else None
            if operation == "assign_chest":
                if not actor.get("is_player") or not chest:
                    raise ValueError("请先打开箱子。")
                owner = self._world["actors"].get(owner_id)
                if owner_id and (not owner or owner["space_id"] != sid):
                    raise ValueError("只能指派给当前场景的角色。")
                chest["traits"]["container"]["owner_actor_id"] = owner_id
                self._save_world_locked()
                return f"已设置「{chest['name']}」的使用权限。"
            if operation == "claim":
                blueprint = self.all_blueprints().get(blueprint_id)
                if not actor.get("is_player") or not blueprint or not self._packable_locked({**blueprint, "entity_id": ""}):
                    raise ValueError("这件素材无法领取到背包。")
                free = self._free_pack_slot_locked(actor)
                item = self._instantiate_blueprint_locked(blueprint, sid, f"inventory:{actor_id}", actor["x"], actor["y"], -1)
                item["inventory_slot"] = free
                self._world["entities"][item["entity_id"]] = item
            elif operation in ("pickup", "stow", "withdraw"):
                if not item or not self._packable_locked(item):
                    raise ValueError("这件物品无法收进背包。")
                if operation == "pickup" and (
                    item["z"] < 0 or self.chebyshev_distance(actor, item) > 1
                    or not self.visible_entities(actor_id, entity_id=entity_id)
                ):
                    raise ValueError("请靠近要拾取的地图物品。")
                if operation == "stow" and actor.get("holding_id") != entity_id:
                    raise ValueError("手里没有这件物品。")
                if operation == "stow" and self._is_open_loan_item_locked(entity_id):
                    raise ValueError("借用的钥匙需要留在手里。")
                if operation == "withdraw" and (not chest or item not in self._container_children_locked(chest_id)):
                    raise ValueError("物品已不在这个箱子里。")
                self._store_in_pack_locked(actor, item, self._free_pack_slot_locked(actor, slot))
            else:
                if not item or item not in pack:
                    raise ValueError("物品已不在背包里。")
                if operation == "swap":
                    if not 0 <= slot < self._pack_size(actor):
                        raise ValueError("背包格无效。")
                    slots = self._inventory_slots(pack)
                    other = slots.get(slot)
                    if other is not None:
                        other["inventory_slot"] = next(i for i, entry in slots.items() if entry is item)
                    item["inventory_slot"] = slot
                elif operation == "use":
                    if verb not in INVENTORY_USE_VERBS or verb not in dict(self.legal_verbs(item, actor)):
                        raise ValueError("这件物品现在无法这样使用。")
                    return self.execute_act(actor_id, entity_id, verb)
                elif operation == "place":
                    direction = facing or actor["facing"]
                    dx, dy = {"up": (0, -1), "down": (0, 1), "left": (-1, 0), "right": (1, 0)}.get(direction, (0, 1))
                    dest = (actor["x"] + dx, actor["y"] + dy)
                    parent, x, y, z = self._resolve_placement_locked(
                        sid, *dest, item["footprint"], item["placement"], item["solid"], ignore_id=entity_id,
                    )
                    if item["solid"] and any(
                        peer["space_id"] == sid and x <= peer["x"] < x + item["footprint"][0]
                        and y <= peer["y"] < y + item["footprint"][1] for peer in self._world["actors"].values()
                    ):
                        raise ValueError("有人站在要放置的位置。")
                    item.update(parent_id=parent, x=x, y=y, z=z)
                    item.pop("inventory_slot", None)
                elif operation == "deposit":
                    if not chest or not self._packable_locked(item):
                        raise ValueError("请先打开要存放物品的箱子。")
                    slots = self._inventory_slots(self._container_children_locked(chest_id))
                    capacity = chest["traits"]["container"]["capacity"]
                    if len(slots) >= capacity:
                        raise ValueError(f"「{chest['name']}」已经装满了。")
                    if slot >= 0 and (slot >= capacity or slot in slots):
                        raise ValueError("目标箱子格不可用。")
                    free = slot if slot >= 0 else next(i for i in range(len(slots) + 1) if i not in slots)
                    item.update(parent_id=f"entity:{chest_id}", x=chest["x"], y=chest["y"], z=-1, inventory_slot=free)
                else:
                    raise ValueError("未知背包操作。")
            self._append_event_locked("inventory_changed", actor_id=actor_id, space_id=sid, payload={
                "entity_id": item["entity_id"], "operation": operation, "parent_id": item["parent_id"],
            })
            self._save_world_locked()
            return {
                "claim": "已领取", "pickup": "已拾取", "stow": "已收起", "withdraw": "已取出",
                "swap": "已移动", "place": "已放置", "deposit": "已存入",
            }[operation] + f"「{item['name']}」。"

    def step_player_move(
        self, player_id: str, dx: int, dy: int, *, space_id: str = "", include_prompt: bool = True,
    ) -> dict[str, Any]:
        """主控小人单步方向移动（WASD/方向键），返回最新坐标、脚下地块材质与眼前交互提示。"""
        with self._lock:
            player = self._world["actors"].get(player_id)
            if not player or not player.get("is_player"):
                raise ValueError("主控角色不存在。")
            step_dx = max(-1, min(1, int(dx)))
            step_dy = max(-1, min(1, int(dy)))
            if abs(step_dx) + abs(step_dy) != 1:
                raise ValueError("单步移动仅支持上下左右四向一格。")
            facing = {(0, -1): "up", (0, 1): "down", (-1, 0): "left", (1, 0): "right"}[(step_dx, step_dy)]
            sid = str(player["space_id"])
            space = self._world["spaces"][sid]
            nx, ny = int(player["x"]) + step_dx, int(player["y"]) + step_dy
            moved = False
            if not space_id or space_id == sid:
                player["facing"] = facing
                if self._is_floor_walkable_locked(space, nx, ny, ignore_actor_id=player_id):
                    self._finish_move_locked(player, [(int(player["x"]), int(player["y"])), (nx, ny)], record_event=False)
                    moved = True
            tile = self.get_cell_tile(space, int(player["x"]), int(player["y"]))
            result = {
                "moved": moved,
                "actor_id": player_id,
                "space_id": sid,
                "x": int(player["x"]),
                "y": int(player["y"]),
                "facing": player["facing"],
                "move_seq": int(player.get("move_seq") or 0),
                "tile_id": str(tile.get("tile_id") or "wood_floor"),
            }
            if include_prompt:
                result["prompt"] = self._player_near_prompt_locked(player)
            return result

    def player_interact_front(
        self,
        player_id: str,
        *,
        x: int | None = None,
        y: int | None = None,
        facing: str = "",
        space_id: str = "",
        target_kind: str = "",
        target_id: str = "",
    ) -> dict[str, Any]:
        """主控小人按 E 与眼前人物/门洞/物件交互。"""
        with self._lock:
            player = self._world["actors"].get(player_id)
            if not player or not player.get("is_player"):
                raise ValueError("主控角色不存在。")
            sid = player["space_id"]
            if space_id and sid != space_id:
                return {"kind": "none", "space_id": sid}
            changed: list[str] = []
            previous_rest = player.get("resting_on_id")
            if x is not None and y is not None:
                origin, target = (player["x"], player["y"]), (int(x), int(y))
                if origin != target:
                    path = None
                    if max(abs(target[0] - origin[0]), abs(target[1] - origin[1])) <= 8:
                        path = self._bfs_locked(sid, origin, {target}, ignore_actor_id=player_id)
                    if not path or len(path) > 17:
                        return {"kind": "none", "space_id": sid}
                    self._finish_move_locked(player, path, record_event=False)
                    if previous_rest:
                        changed.append(previous_rest)
            if facing in ("up", "down", "left", "right"):
                player["facing"] = facing
            prompt = self._player_near_prompt_locked(player)
            kind = prompt.get("kind") or ""
            result = {"space_id": sid, "changed_entity_ids": changed}
            if not kind or (target_id and (kind, prompt["target_id"]) != (target_kind, target_id)):
                return {**result, "kind": "none", "message": "眼前的互动目标已变化。"}
            if kind == "actor":
                return {
                    **result,
                    "kind": "talk_actor",
                    "target_actor_id": prompt["target_id"],
                    "name": prompt["name"],
                    "message": f"走近 {prompt['name']}，准备面对面说话。",
                }
            if kind == "portal":
                door = self._world["entities"][prompt["target_id"]]
                self._record_act_locked(player, "enter", door)
                entered, msg, _ = self._perform_portal_enter_locked(
                    player, door, blocked_approach=prompt["blocked_approach"],
                )
                return {
                    **result,
                    "kind": "entered_space" if entered else "action",
                    "space_id": player["space_id"],
                    "message": msg,
                }
            action = str(prompt.get("action") or "look")
            entity_id = prompt["target_id"]
            entity = self._world["entities"][entity_id]
            container = entity["traits"].get("container")
            if container and action in ("open", "close", "unlock"):
                msg = self.execute_act(player_id, entity_id, action) if not container["is_open"] else ""
                if container["is_open"] and not container["is_locked"]:
                    changed.append(entity_id)
                    return {**result, "kind": "container", "entity_id": entity_id}
                return {**result, "kind": "action", "message": msg}
            msg = self.execute_act(player_id, entity_id, action)
            for eid in (entity_id, previous_rest):
                if eid and eid not in changed:
                    changed.append(eid)
            return {
                **result,
                "kind": "entity_acted",
                "entity_id": entity_id,
                "action": action,
                "message": msg,
            }

    def move_actor_toward(
        self, actor_id: str, space_id: str, x: int, y: int, *, reach: int, stride: int,
        cardinal: bool = False, region: tuple[int, int, int, int] | None = None,
    ) -> list[tuple[int, int]]:
        """沿物理寻路结果走有限步，返回实际经过的格子。"""
        with self._lock:
            walked = self.plan_actor_route(
                actor_id, space_id, x, y, reach=reach, stride=stride, cardinal=cardinal, region=region,
            )
            if len(walked) > 1:
                self._finish_move_locked(self._world["actors"][actor_id], walked)
                self._save_world_locked()
            return walked

    def plan_actor_route(
        self, actor_id: str, space_id: str, x: int, y: int, *, reach: int, stride: int,
        cardinal: bool = False, region: tuple[int, int, int, int] | None = None,
    ) -> list[tuple[int, int]]:
        with self._lock:
            actor = self._world["actors"].get(actor_id)
            if actor is None or actor["space_id"] != space_id:
                return []
            goals = {
                (x + dx, y + dy)
                for dx in range(-reach, reach + 1)
                for dy in range(-reach, reach + 1)
                if not cardinal or abs(dx) + abs(dy) <= reach
            }
            if region is not None:
                left, top, right, bottom = region
                goals = {(gx, gy) for gx, gy in goals if left <= gx <= right and top <= gy <= bottom}
            distant = stride > 1 or max(abs(x - int(actor["x"])), abs(y - int(actor["y"]))) > 3
            path = self._bfs_locked(
                space_id, (actor["x"], actor["y"]), goals, ignore_actor_id=actor_id,
                ignore_distant_actors_from=(int(actor["x"]), int(actor["y"])) if distant else None,
            ) or []
            return path[:stride + 1]

    def advance_live_route(
        self, actor_id: str, space_id: str, move_seq: int, cell: tuple[int, int], *, persist: bool,
    ) -> dict[str, Any] | None:
        with self._lock:
            actor = self._world["actors"].get(actor_id)
            if actor is None or actor["space_id"] != space_id or actor["move_seq"] != move_seq:
                return None
            start = (actor["x"], actor["y"])
            if abs(start[0] - cell[0]) + abs(start[1] - cell[1]) != 1:
                return None
            if not self._is_floor_walkable_locked(self._world["spaces"][space_id], *cell, ignore_actor_id=actor_id):
                return None
            self._finish_move_locked(actor, [start, cell], record_event=persist)
            return self.live_actor_frame(actor_id)

    def cognitive_act(
        self, actor_id: str, space_id: str, entity_id: str, action: str, *, text: str = "",
    ) -> tuple[bool, str]:
        """执行近身动作，将成功状态与供界面显示的反馈分开。"""
        with self._lock:
            actor = self._world["actors"].get(actor_id)
            entity = self._world["entities"].get(entity_id)
            if not actor or not entity or actor["space_id"] != space_id or entity["space_id"] != space_id:
                return False, ""
            spatial = True if self._world["spaces"][space_id]["space_kind"] != "room" else None
            if not any(e["entity_id"] == entity_id for e in self.visible_entities(actor_id, spatial_filter=spatial)):
                return False, ""
            if entity["parent_id"] != f"actor:{actor_id}" and self.chebyshev_distance(actor, entity) > 1:
                return False, ""
            if action == "write":
                inscription = entity["traits"].get("inscribable") or {}
                if not inscription.get("writable_by_ai") or not inscription_is_new(text, inscription["entries"]):
                    return False, ""
                return True, self.execute_write(actor_id, entity_id, text)
            if action not in dict(self.legal_verbs(entity, actor)):
                return False, ""
            if action == "enter":
                ok, message, _info = self._perform_portal_enter_locked(actor, entity)
                return ok, message
            return True, self.execute_act(actor_id, entity_id, action)

    def set_actor_vitals(self, actor_id: str, vitals: Mapping[str, Any]) -> None:
        with self._lock:
            target = self._world["actors"].get(actor_id)
            if not target:
                return
            target["vitals"] = _normalize_vitals(vitals)
            self._refresh_actor_expression_locked(target)
            self._sync_actor_world_state_locked(target)
            self._save_world_locked()

    def set_actor_world_mood(
        self,
        actor_id: str,
        *,
        space_id: str = "",
        vitals_patch: Mapping[str, Any] | None = None,
        autonomy_patch: Mapping[str, Any] | None = None,
        record_event: bool = True,
        persist: bool = True,
    ) -> dict[str, Any]:
        """针对指定世界（space_id）更新角色的心情参数与心愿状态，与其他世界完全隔离。"""
        with self._lock:
            actor = self._world["actors"].get(actor_id)
            if not actor:
                raise ValueError(f"角色不存在：{actor_id}")
            target_sid = str(space_id or actor.get("space_id") or "room_default").strip() or "room_default"
            cur_sid = str(actor.get("space_id") or "room_default").strip() or "room_default"

            if target_sid == cur_sid:
                vit = dict(actor.get("vitals") or {})
                if isinstance(vitals_patch, Mapping):
                    vit.update(dict(vitals_patch))
                actor["vitals"] = _normalize_vitals(vit)

                aut = normalize_autonomy(actor.get("autonomy"))
                if isinstance(autonomy_patch, Mapping):
                    aut.update(dict(autonomy_patch))
                actor["autonomy"] = normalize_autonomy(aut)

                self._refresh_actor_expression_locked(actor, force_auto=record_event)
                self._sync_actor_world_state_locked(actor, target_sid)
                result_state = actor["world_states"][target_sid]
            else:
                states = actor.setdefault("world_states", {})
                bucket = copy.deepcopy(states.get(target_sid) or {})
                vit = dict(bucket.get("vitals") or {"preferred_domains": list((actor.get("vitals") or {}).get("preferred_domains") or []), "source": "builtin"})
                if isinstance(vitals_patch, Mapping):
                    vit.update(dict(vitals_patch))
                aut = normalize_autonomy(bucket.get("autonomy"))
                if isinstance(autonomy_patch, Mapping):
                    aut.update(dict(autonomy_patch))
                bucket["vitals"] = _normalize_vitals(vit)
                bucket["autonomy"] = normalize_autonomy(aut)
                bucket["expression"] = _normalize_expression(bucket.get("expression"))
                states[target_sid] = bucket
                result_state = bucket

            if record_event:
                self._append_event_locked(
                    "actor_world_mood_updated",
                    actor_id=actor_id,
                    space_id=target_sid,
                    payload={
                        "energy": result_state["vitals"]["energy"],
                        "sleep_pressure": result_state["vitals"]["sleep_pressure"],
                        "mood_v": result_state["vitals"]["mood_v"],
                    },
                )
            if persist:
                self._save_world_locked()
            return copy.deepcopy(result_state)

    def update_actor_sprite(self, actor_id: str, sprite: Mapping[str, Any]) -> dict[str, Any]:
        with self._lock:
            actor = self._world["actors"].get(actor_id)
            if not actor:
                raise ValueError(f"角色不存在：{actor_id}")
            norm = normalize_actor({"name": actor["name"], "sprite": sprite})["sprite"]
            actor["sprite"] = norm
            self._save_world_locked()
            return copy.deepcopy(actor)

    def resolve_actor_id(self, character_name: str = "", *, space_id: str = "") -> str:
        with self._lock:
            actors = {aid: actor for aid, actor in self._world["actors"].items() if not actor.get("is_player")}
            if not actors:
                return ""
            target = str(character_name or "").strip()
            sid = str(space_id or "").strip()
            active = str(self._world.get("active_actor_id") or "")

            if sid:
                if target:
                    for aid, act in actors.items():
                        if act.get("space_id") == sid and (act.get("host_character") == target or act.get("name") == target):
                            return aid
                if active in actors and actors[active].get("space_id") == sid:
                    return active
                for aid, act in actors.items():
                    if act.get("space_id") == sid:
                        return aid

            if target:
                for aid, act in actors.items():
                    if act.get("host_character") == target or act.get("name") == target:
                        return aid
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

    def _instantiate_blueprint_locked(
        self, blueprint: Mapping[str, Any], space_id: str, parent_id: str, x: int, y: int, z: int,
        *, delivery_mode: str = "silent", note_text: str = "", portal_target_space_id: str = "",
    ) -> dict[str, Any]:
        traits = copy.deepcopy(blueprint["traits"])
        if "thermal" in traits:
            traits["thermal"]["temp_updated_ts"] = time.time()
        if "portal" in traits and portal_target_space_id:
            traits["portal"]["target_space_id"] = portal_target_space_id
        if "inscribable" in traits and note_text.strip():
            traits["inscribable"]["entries"].append({"by": "user", "ts": time.time(), "text": clip_text(note_text, 120, "")})
        return self._normalize_entity_dict({
            **blueprint, "entity_id": self._next_entity_id_locked(blueprint),
            "parent_id": parent_id, "space_id": space_id, "x": x, "y": y, "z": z, "traits": traits,
            "traces": [{"ts": time.time(), "text": "由你直接递来" if delivery_mode == "give" else "从素材蓝图生成"}],
        })

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
            entity = self._instantiate_blueprint_locked(
                bp, sid, parent_id, px, py, pz, delivery_mode=delivery_mode,
                note_text=note_text, portal_target_space_id=portal_target_space_id,
            )
            eid = entity["entity_id"]
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
            self._cancel_key_loans_locked(to_remove)
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
            removed = [eid for eid, ent in self._world["entities"].items()
                       if ent.get("space_id") == sid and not ent["parent_id"].startswith(("inventory:", "actor:"))]
            for eid in removed:
                self._world["entities"].pop(eid, None)
            self._cancel_key_loans_locked(removed)
            for act in self._world["actors"].values():
                if act.get("space_id") == sid:
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
    def _sight_context_locked(
        self,
        actor: Mapping[str, Any],
        *,
        spatial_filter: bool | None = None,
    ) -> tuple[bool, tuple[int, int], int, set[tuple[int, int]], bool, list[tuple[int, int]]]:
        sid = actor["space_id"]
        space = self._world["spaces"].get(sid)
        entities = self._world["entities"]
        use_spatial = (
            bool(spatial_filter)
            if spatial_filter is not None
            else bool(
                space
                and (
                    space.get("space_kind") == "town"
                    or max(int(space.get("width") or 0), int(space.get("height") or 0)) >= 24
                )
            )
        )
        opaque: set[tuple[int, int]] = set()
        origin = (int(actor.get("x") or 0), int(actor.get("y") or 0))
        lamps: list[tuple[int, int]] = []
        is_night = False
        vision_radius = 12
        if use_spatial and space:
            w = int(space.get("width") or 0)
            h = int(space.get("height") or 0)
            open_doors: set[tuple[int, int]] = set()
            closed_doors: set[tuple[int, int]] = set()
            for cand in entities.values():
                if cand.get("space_id") != sid or int(cand.get("z", 0)) != 0:
                    continue
                pt = (cand.get("traits") or {}).get("portal")
                if pt is None:
                    continue
                cells = self._entity_cells(cand)
                if bool(pt.get("is_open", False)):
                    open_doors.update(cells)
                else:
                    closed_doors.update(cells)
            x0 = max(0, origin[0] - vision_radius - 5)
            x1 = min(w, origin[0] + vision_radius + 6)
            y0 = max(0, origin[1] - vision_radius - 5)
            y1 = min(h, origin[1] + vision_radius + 6)
            for yy in range(y0, y1):
                for xx in range(x0, x1):
                    if (xx, yy) not in open_doors and self.is_wall_cell(space, xx, yy):
                        opaque.add((xx, yy))
            opaque.update(closed_doors)
            lighting = compute_lighting(
                space,
                entities,
                time.time(),
                float(self._world.get("clock_offset_minutes") or 0.0),
            )
            is_night = lighting["period"] == "night"
            lamps = [(int(lp["x"]), int(lp["y"])) for lp in (lighting.get("lamp_positions") or [])]
        return use_spatial, origin, vision_radius, opaque, is_night, lamps

    def _point_seen_locked(
        self,
        ctx: tuple[bool, tuple[int, int], int, set[tuple[int, int]], bool, list[tuple[int, int]]],
        point: tuple[int, int],
    ) -> bool:
        use_spatial, origin, vision_radius, opaque, is_night, lamps = ctx
        if not use_spatial:
            return True
        cx, cy = point
        dist = max(abs(cx - origin[0]), abs(cy - origin[1]))
        if dist > vision_radius:
            return False
        lit = (
            not is_night
            or dist <= 3
            or any(
                max(abs(lx - cx), abs(ly - cy)) <= 4
                and self._sight_line_clear_locked((lx, ly), (cx, cy), opaque)
                for lx, ly in lamps
            )
        )
        return lit and self._sight_line_clear_locked(origin, (cx, cy), opaque)

    def _visible_peer_ids_locked(
        self,
        actor: Mapping[str, Any],
        *,
        spatial_filter: bool | None = None,
    ) -> set[str]:
        aid = actor["actor_id"]
        sid = actor["space_id"]
        ctx = self._sight_context_locked(actor, spatial_filter=spatial_filter)
        use_spatial = ctx[0]
        seen: set[str] = set()
        for pid, peer in self._world["actors"].items():
            if pid == aid or peer.get("space_id") != sid:
                continue
            if not use_spatial or self._point_seen_locked(ctx, (int(peer.get("x") or 0), int(peer.get("y") or 0))):
                seen.add(pid)
        return seen

    def visible_peer_ids(
        self,
        actor_id: str,
        *,
        spatial_filter: bool | None = None,
    ) -> set[str]:
        """返回角色当前视线内可见的同空间同伴 ID 集合。"""
        with self._lock:
            actor = self._world["actors"].get(actor_id)
            if not actor:
                return set()
            return self._visible_peer_ids_locked(actor, spatial_filter=spatial_filter)

    def visible_entities(
        self,
        actor_id: str,
        *,
        spatial_filter: bool | None = None,
        entity_id: str = "",
    ) -> list[dict[str, Any]]:
        with self._lock:
            actor = self._world["actors"].get(actor_id)
            if not actor:
                return []
            sid = actor["space_id"]
            entities = self._world["entities"]
            if entity_id and entity_id not in entities:
                return []
            ctx = self._sight_context_locked(actor, spatial_filter=spatial_filter)

            def _is_cell_seen(target_ent: Mapping[str, Any]) -> bool:
                if not ctx[0]:
                    return True
                return any(self._point_seen_locked(ctx, cell) for cell in self._entity_cells(target_ent))

            out: list[dict[str, Any]] = []
            candidates = (entities[entity_id],) if entity_id else entities.values()
            for ent in candidates:
                if ent.get("space_id") != sid:
                    continue
                pid = str(ent.get("parent_id") or "")
                z = int(ent.get("z") or 0)
                if pid == f"actor:{actor_id}" or (entity_id and pid == f"inventory:{actor_id}"):
                    out.append(copy.deepcopy(ent))
                    continue
                if z == -1 and pid.startswith("entity:"):
                    parent_ent = entities.get(pid.split(":", 1)[1])
                    if not parent_ent:
                        continue
                    c_trait = (parent_ent.get("traits") or {}).get("container")
                    if not c_trait or not bool(c_trait.get("is_open")) or c_trait.get("owner_actor_id") not in (None, "", actor_id):
                        continue
                    if _is_cell_seen(parent_ent):
                        out.append(copy.deepcopy(ent))
                    continue
                if z >= 0 and _is_cell_seen(ent):
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
            if occ:
                occ_act = (self._world.get("actors") or {}).get(str(occ))
                occ_name = str(occ_act.get("name") or "").strip() if isinstance(occ_act, Mapping) else ""
                parts.append(f"{occ_name}在上面睡觉" if occ_name else "有人在上面睡觉")
            else:
                parts.append("空着")
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

        if (traits.get("container") or {}).get("owner_actor_id") not in (None, "", aid):
            return verbs

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

    # ── 小人动画元数据：走位路径 / 朝向 / 最近一次交互动作 ───────────────
    @staticmethod
    def _facing_from_delta(dx: int, dy: int, fallback: str = "down") -> str:
        if dx == 0 and dy == 0:
            return fallback
        if abs(dx) >= abs(dy):
            return "right" if dx > 0 else "left"
        return "down" if dy > 0 else "up"

    def _record_move_locked(self, actor: dict[str, Any], path: Sequence[tuple[int, int]] | None) -> None:
        cells = [[int(c[0]), int(c[1])] for c in (path or [])][-96:]
        actor["last_path"] = cells
        actor["move_seq"] = int(actor.get("move_seq") or 0) + 1
        if len(cells) >= 2:
            actor["facing"] = self._facing_from_delta(
                cells[-1][0] - cells[-2][0],
                cells[-1][1] - cells[-2][1],
                str(actor.get("facing") or "down"),
            )

    def _record_teleport_locked(self, actor: dict[str, Any]) -> None:
        actor["last_path"] = []
        actor["move_seq"] = int(actor.get("move_seq") or 0) + 1

    def _face_entity_locked(self, actor: dict[str, Any], entity: Mapping[str, Any]) -> None:
        cells = self._entity_cells(entity)
        if not cells:
            return
        ax, ay = int(actor.get("x") or 0), int(actor.get("y") or 0)
        best = min(cells, key=lambda c: abs(c[0] - ax) + abs(c[1] - ay))
        if int(best[0]) == ax and int(best[1]) == ay:
            return
        actor["facing"] = self._facing_from_delta(
            int(best[0]) - ax,
            int(best[1]) - ay,
            str(actor.get("facing") or "down"),
        )

    def _record_act_locked(self, actor: dict[str, Any], verb: str, entity: Mapping[str, Any] | None) -> None:
        prev = actor.get("last_act") if isinstance(actor.get("last_act"), dict) else {}
        actor["last_act"] = {
            "seq": int(prev.get("seq") or 0) + 1,
            "verb": str(verb or ""),
            "entity_id": str((entity or {}).get("entity_id") or ""),
            "ts": round(time.time(), 3),
        }
        if entity is not None:
            self._face_entity_locked(actor, entity)

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
        expected_realm: str = "",
        persist: bool = True,
    ) -> dict[str, Any]:
        with self._lock:
            actor = self._world["actors"].get(actor_id)
            if not actor:
                raise ValueError(f"角色不存在：{actor_id}")
            cur = normalize_autonomy(actor.get("autonomy"))
            if expected_realm and self._realm_space_id_locked(actor["space_id"]) != expected_realm:
                return cur
            prev_cog = cur.get("cognition") or {}
            signal = prev_cog.get("social_signal")
            pending_mod = prev_cog.get("pending_modulation")
            prev_preempt = int(prev_cog.get("preempt_seq", 0))
            cur.update(dict(patch or {}))
            cognition = cur.get("cognition")
            if cognition is not None:
                if signal and signal["seq"] > cognition.get("last_social_signal", 0):
                    cognition["social_signal"] = signal
                if pending_mod and int(pending_mod.get("id", 0)) > int(cognition.get("last_language_mod_id", 0)):
                    cognition["pending_modulation"] = pending_mod
                if prev_preempt > int(cognition.get("preempt_seq", 0)):
                    cognition["preempt_seq"] = prev_preempt
            actor["autonomy"] = normalize_autonomy(cur)
            if emotion_hint:
                self._refresh_actor_expression_locked(actor, emotion_hint=emotion_hint)
            self._sync_actor_world_state_locked(actor)
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
            if persist:
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
        self._record_move_locked(actor, path)
        actor["x"] = dest_x
        actor["y"] = dest_y
        if not allow_inside_entity:
            self._face_entity_locked(actor, entity)
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
            self._finish_move_locked(actor, path)
            self._save_world_locked()
            return f"你走到了房间坐标 ({tx}, {ty})。"

    def _finish_move_locked(
        self, actor: dict[str, Any], path: list[tuple[int, int]], *, record_event: bool = True,
    ) -> None:
        self._clear_actor_rest_locked(actor)
        self._record_move_locked(actor, path)
        actor["x"], actor["y"] = path[-1]
        self._refresh_actor_expression_locked(actor, action_kind="move")
        if record_event:
            self._append_event_locked("actor_moved", actor_id=actor["actor_id"], space_id=actor["space_id"],
                                      payload={"x": actor["x"], "y": actor["y"]})

    def execute_act(self, actor_id: str, entity_id: str, action: str, target_id: str = "") -> str:
        with self._lock:
            actor = self._world["actors"].get(actor_id)
            if not actor:
                return "当前世界里还没有导入角色。"
            eid = str(entity_id or "").strip()
            ent = self._world["entities"].get(eid)
            if not ent:
                return f"这个世界里没有编号为「{eid}」的东西。"
            if not self.visible_entities(actor_id, entity_id=eid):
                return f"你此刻看不到「{ent['name']}」。"

            canon = _VERB_ALIASES.get(str(action or "").strip().lower()) or _VERB_ALIASES.get(str(action or "").strip(), "")
            event_kind = {
                "unlock": "discovered", "consume": "consumed", "rest": "rested",
                "interact": "action_success",
            }.get(canon)
            if canon == "turn_on" and (ent["traits"].get("switchable") or {}).get("provides_light"):
                event_kind = "light_on"

            def report(message: str, *, ok: bool = False) -> str:
                if event_kind:
                    self._queue_persona_event_locked(
                        actor, event_kind if ok else "action_blocked", ent, canon, message, ok=ok,
                        mood_delta=float(ent["effects"].get("mood_v_delta") or 0) if ok and canon in ("consume", "interact") else 0.0,
                    )
                    self._save_world_locked()
                return message

            legal = dict(self.legal_verbs(ent, actor))
            if canon not in legal:
                if canon == "consume" and "consumable" in (ent.get("traits") or {}):
                    return report(f"「{ent['name']}」已经空了，没有可以再用的份量。")
                if canon == "take" and "portable" not in (ent.get("traits") or {}):
                    return f"「{ent['name']}」太沉或固定在原地，拿不起来。"
                avail = "、".join(legal.values())
                return report(f"现在不能对「{ent['name']}」做这个动作（当前可行：{avail}）。")

            traits = ent["traits"]
            sid = actor["space_id"]
            self._record_act_locked(actor, canon, ent)

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
                if actor.get("is_player"):
                    try:
                        return self.inventory_action(actor_id, "pickup", entity_id=eid)
                    except ValueError as exc:
                        return str(exc)
                if actor.get("holding_id"):
                    held = self._world["entities"].get(actor["holding_id"])
                    held_name = held["name"] if held else "别的东西"
                    return f"你手里已经拿着「{held_name}」，得先把它放下才能拿别的。"
                ok, prefix = self._walk_to_entity_locked(actor, ent)
                if not ok:
                    return prefix
                ent["parent_id"] = f"actor:{actor_id}"
                ent.pop("inventory_slot", None)
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
                        if t_ent["z"] < 0:
                            return f"请先把「{t_ent['name']}」放到地面上。"
                        ok, _ = self._walk_to_entity_locked(actor, t_ent)
                        if not ok:
                            return _
                        t_traits = t_ent.get("traits") or {}
                        if "container" in t_traits:
                            if t_traits["container"].get("is_locked") or not t_traits["container"].get("is_open"):
                                return f"「{t_ent['name']}」还是关着的，放不进去。"
                            if t_traits["container"].get("owner_actor_id") not in (None, "", actor_id):
                                return f"「{t_ent['name']}」是其他角色的私有箱。"
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
                ent.pop("inventory_slot", None)
                if actor.get("holding_id") == eid:
                    actor["holding_id"] = None
                self._append_event_locked("entity_moved", actor_id=actor_id, space_id=sid, payload={"entity_id": eid, "x": px, "y": py, "z": pz, "parent_id": pid})
                self._save_world_locked()
                return f"你把「{ent['name']}」放到了 ({px}, {py})。"

            if canon == "unlock":
                ok, prefix = self._walk_to_entity_locked(actor, ent)
                if not ok:
                    return report(prefix)
                need_tag = str(traits["container"].get("key_tag") or "").strip()
                held_id = actor.get("holding_id")
                has_key = False
                for cand in [*self.visible_entities(actor_id), *self._pack_items_locked(actor_id)]:
                    if cand["entity_id"] == held_id or self.chebyshev_distance(actor, cand) <= 1:
                        if not need_tag or need_tag in (cand.get("tags") or []):
                            if cand["entity_id"] != eid:
                                has_key = True
                                break
                if need_tag and not has_key:
                    return report(f"「{ent['name']}」锁着，需要带有「{need_tag}」标签的钥匙在手边才能打开。")
                traits["container"]["is_locked"] = False
                traits["container"]["is_open"] = True
                self._append_event_locked("entity_state_changed", actor_id=actor_id, space_id=sid, payload={"entity_id": eid, "unlocked": True, "is_open": True})
                return report(f"{prefix}解开了「{ent['name']}」的锁并打开了它。", ok=True)

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
                    return report(prefix)
                cm = traits["consumable"]
                full_name, empty_name = infer_consumable_names(ent["name"], str(cm.get("empty_name") or ""))
                if not cm.get("empty_name") and empty_name != full_name:
                    cm["empty_name"] = empty_name
                cm["uses_left"] = max(0, int(cm["uses_left"]) - 1)
                self._apply_effects_locked(actor, ent)
                verb = cm.get("consume_verb") or "使用"
                left = int(cm["uses_left"])
                if left == 0 and cm.get("on_empty") == "vanish":
                    if actor.get("holding_id") == eid:
                        actor["holding_id"] = None
                    self._world["entities"].pop(eid, None)
                    self._cancel_key_loans_locked((eid,))
                    self._refresh_actor_expression_locked(actor, action_kind="consume")
                    self._append_event_locked("entity_consumed", actor_id=actor_id, space_id=sid, payload={"entity_id": eid, "verb": verb, "uses_left": 0, "vanished": True})
                    return report(f"{prefix}把「{full_name}」{verb}光了，现在彻底没了。", ok=True)
                if left == 0:
                    if empty_name:
                        ent["name"] = empty_name
                    ent["brief"] = clip_text(cm.get("empty_brief"), MAX_BRIEF_CHARS, f"空掉的{empty_name or ent['name']}")
                elif full_name and ent["name"] != full_name:
                    ent["name"] = full_name
                ent["traces"].append({"ts": time.time(), "text": f"{actor['name']}{verb}了一次"})
                self._refresh_actor_expression_locked(actor, action_kind="consume")
                self._append_event_locked("entity_consumed", actor_id=actor_id, space_id=sid, payload={"entity_id": eid, "verb": verb, "uses_left": left})
                return report(f"{prefix}{verb}了「{full_name}」，" + (f"还剩 {left} 次。" if left > 0 else "已经空了。"), ok=True)

            if canon in ("turn_on", "turn_off"):
                ok, prefix = self._walk_to_entity_locked(actor, ent)
                if not ok:
                    return report(prefix)
                new_on = canon == "turn_on"
                traits["switchable"]["is_on"] = new_on
                self._refresh_actor_expression_locked(actor, action_kind=canon)
                self._append_event_locked("entity_state_changed", actor_id=actor_id, space_id=sid, payload={"entity_id": eid, "is_on": new_on})
                if not event_kind:
                    self._save_world_locked()
                label = "打开了" if new_on else "关掉了"
                return report(f"{prefix}{label}「{ent['name']}」。", ok=True)

            if canon == "rest":
                occ = traits["restable"].get("occupied_by")
                if occ and occ != actor_id:
                    occ_act = (self._world.get("actors") or {}).get(str(occ))
                    if isinstance(occ_act, Mapping) and occ_act.get("posture") == "resting" and occ_act.get("resting_on_id") == eid:
                        occ_name = str(occ_act.get("name") or "别人").strip()
                        return report(f"「{ent['name']}」上已经有{occ_name}在睡觉了，挤不下啦。")
                ok, prefix = self._walk_to_entity_locked(actor, ent, allow_inside_entity=True)
                if not ok:
                    return report(prefix)
                self._clear_actor_rest_locked(actor)
                traits["restable"]["occupied_by"] = actor_id
                actor["resting_on_id"] = eid
                actor["posture"] = "resting"
                actor["x"] = int(ent["x"])
                actor["y"] = int(ent["y"])
                self._refresh_actor_expression_locked(actor, action_kind="rest")
                self._append_event_locked("entity_state_changed", actor_id=actor_id, space_id=sid, payload={"entity_id": eid, "resting": True})
                return report(f"{prefix}在「{ent['name']}」上歇了下来。", ok=True)

            if canon == "leave_rest":
                self._clear_actor_rest_locked(actor)
                self._refresh_actor_expression_locked(actor, action_kind="leave_rest")
                self._append_event_locked("entity_state_changed", actor_id=actor_id, space_id=sid, payload={"entity_id": eid, "resting": False})
                self._save_world_locked()
                return f"你从「{ent['name']}」上起身站好了。"

            if canon == "interact":
                ok, prefix = self._walk_to_entity_locked(actor, ent)
                if not ok:
                    return report(prefix)
                ac = traits["activity"]
                if "switchable" in traits and not traits["switchable"].get("is_on"):
                    traits["switchable"]["is_on"] = True
                if ac.get("battery") is not None:
                    cost = int(ac.get("battery_cost") or 10)
                    if int(ac["battery"]) <= 0:
                        return report(f"「{ent['name']}」没电了，打不开。")
                    ac["battery"] = max(0, int(ac["battery"]) - cost)
                ac["progress"] = int(ac.get("progress") or 0) + 1
                self._apply_effects_locked(actor, ent)
                ent["traces"].append({"ts": time.time(), "text": f"{actor['name']}玩/用了第 {ac['progress']} {ac['progress_unit']}"})
                self._refresh_actor_expression_locked(actor, action_kind="interact")
                self._append_event_locked("entity_state_changed", actor_id=actor_id, space_id=sid, payload={"entity_id": eid, "progress": ac["progress"], "battery": ac.get("battery")})
                bat_msg = f"，剩余电量 {ac['battery']}%" if ac.get("battery") is not None else ""
                return report(f"{prefix}用「{ent['name']}」推进到了第 {ac['progress']} {ac['progress_unit']}{bat_msg}。", ok=True)

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
                ok, msg, _info = self._perform_portal_enter_locked(actor, ent)
                return msg

            return f"暂不支持对「{ent['name']}」执行 {action}。"

    def _perform_portal_enter_locked(
        self,
        actor: dict[str, Any],
        ent: dict[str, Any],
        *,
        blocked_approach: bool = False,
    ) -> tuple[bool, str, dict[str, Any]]:
        eid = str(ent["entity_id"])
        pt = (ent.get("traits") or {}).get("portal")
        if not isinstance(pt, dict):
            return False, f"「{ent['name']}」不是可通行的门。", {}
        target_sid = str(pt.get("target_space_id") or "").strip()
        target_space = self._world["spaces"].get(target_sid)
        if not target_space:
            return False, f"「{ent['name']}」那边还没有接通别的空间。", {}
        prefix = ""
        if not blocked_approach:
            ok, prefix = self._walk_to_entity_locked(actor, ent, allow_inside_entity=True)
            if not ok:
                return False, prefix, {}

        actor_id = str(actor["actor_id"])
        from_sid = str(actor.get("space_id") or "")
        pt["is_open"] = True
        ent["solid"] = False
        self._clear_actor_rest_locked(actor)
        tx, ty = self._find_open_floor_cell_locked(
            target_space,
            preferred_x=int(pt.get("target_x") or 5),
            preferred_y=int(pt.get("target_y") or 2),
            ignore_actor_id=actor_id,
        )
        self._switch_actor_world_state_locked(actor, target_sid)
        self._record_teleport_locked(actor)
        actor["x"] = tx
        actor["y"] = ty
        dest_kind = str(target_space.get("space_kind") or "room")
        if dest_kind == "building":
            actor["facing"] = "up"
        elif dest_kind == "town":
            actor["facing"] = "down"
        self._refresh_actor_expression_locked(actor)
        self._world["active_space_id"] = target_sid
        self._append_event_locked(
            "actor_moved",
            actor_id=actor_id,
            space_id=target_sid,
            payload={
                "via_portal": eid,
                "door_name": ent["name"],
                "from_space_id": from_sid,
                "to_space_id": target_sid,
                "to_space_name": target_space["name"],
                "x": tx,
                "y": ty,
            },
        )
        self._save_world_locked()
        msg = f"{prefix}推开「{ent['name']}」，来到了「{target_space['name']}」({tx}, {ty})。"
        return True, msg, {
            "entered": True,
            "mode": "actor",
            "actor_id": actor_id,
            "from_space_id": from_sid,
            "target_space_id": target_sid,
            "target_space_name": str(target_space["name"]),
            "x": tx,
            "y": ty,
            "feedback": msg,
        }

    def enter_portal(self, entity_id: str, *, actor_id: str = "") -> dict[str, Any]:
        with self._lock:
            eid = str(entity_id or "").strip()
            ent = self._world["entities"].get(eid)
            if not ent:
                raise ValueError(f"门或传送点不存在：{eid}")
            pt = (ent.get("traits") or {}).get("portal")
            if not isinstance(pt, dict):
                raise ValueError(f"「{ent['name']}」不是可以进入的门。")
            target_sid = str(pt.get("target_space_id") or "").strip()
            target_space = self._world["spaces"].get(target_sid)
            if not target_space:
                raise ValueError(f"「{ent['name']}」还没有连接目标空间。")

            src_sid = str(ent.get("space_id") or "")
            chosen_actor: dict[str, Any] | None = None
            req_aid = str(actor_id or "").strip()
            if req_aid:
                cand = self._world["actors"].get(req_aid)
                if cand and cand.get("space_id") == src_sid:
                    chosen_actor = cand
            if chosen_actor is None:
                active_aid = str(self._world.get("active_actor_id") or "")
                active_cand = self._world["actors"].get(active_aid) if active_aid else None
                if active_cand and active_cand.get("space_id") == src_sid:
                    chosen_actor = active_cand
                else:
                    chosen_actor = next(
                        (a for a in self._world["actors"].values() if a.get("space_id") == src_sid),
                        None,
                    )

            if chosen_actor is not None:
                ok, msg, info = self._perform_portal_enter_locked(chosen_actor, ent)
                if not ok:
                    raise ValueError(msg)
                return info

            pt["is_open"] = True
            ent["solid"] = False
            self._world["active_space_id"] = target_sid
            self._save_world_locked()
            msg = f"推开「{ent['name']}」，进入「{target_space['name']}」查看。"
            return {
                "entered": True,
                "mode": "observer",
                "actor_id": "",
                "from_space_id": src_sid,
                "target_space_id": target_sid,
                "target_space_name": str(target_space["name"]),
                "feedback": msg,
            }

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
            self._record_act_locked(actor, "write", ent)
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
                self._cancel_key_loans_locked((sid_src,))

            fb = str(matched_rule.get("feedback") or "两件东西产生了一点变化。")
            self._refresh_actor_expression_locked(actor, action_kind="combine")
            self._append_event_locked("entity_state_changed", actor_id=actor_id, space_id=actor["space_id"], payload={"combine": [sid_src, sid_tgt], "feedback": fb})
            self._save_world_locked()
            return f"{prefix}{fb}"

    # ── L1 物理时钟推进（0 Token）────────────────────────────────────────
    def tick_physics(
        self, *, advance_minutes: float | None = None, now: float | None = None,
        minimum_elapsed_minutes: float = 0.0, time_scale: float = 1.0,
    ) -> dict[str, Any]:
        with self._lock:
            cur_now = float(now if now is not None else time.time())
            last_ts = float(self._world.get("last_tick_ts") or cur_now)
            if advance_minutes is not None:
                delta_min = max(0.0, float(advance_minutes))
                self._world["clock_offset_minutes"] = float(self._world.get("clock_offset_minutes") or 0.0) + delta_min
            else:
                elapsed = max(0.0, (cur_now - last_ts) / 60.0)
                if elapsed < minimum_elapsed_minutes:
                    return {"delta_min": 0.0, "cooled": []}
                delta_min = elapsed * time_scale
                self._world["clock_offset_minutes"] += delta_min - elapsed
            self._world["last_tick_ts"] = cur_now
            self._world["simulation_minutes"] += delta_min

            cooled: list[str] = []
            precision = 5 if minimum_elapsed_minutes else 3
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
                        vit["energy"] = round(clamp_float(vit["energy"] + regen * delta_min, 0.0, 1.0, 0.7), precision)
                        vit["sleep_pressure"] = round(clamp_float(vit["sleep_pressure"] - regen * 0.9 * delta_min, 0.0, 1.0, 0.3), precision)
                    else:
                        vit["energy"] = round(clamp_float(vit["energy"] - 0.0012 * delta_min, 0.05, 1.0, 0.7), precision)
                        vit["sleep_pressure"] = round(clamp_float(vit["sleep_pressure"] + 0.0015 * delta_min, 0.0, 1.0, 0.3), precision)
                    aut = normalize_autonomy(act.get("autonomy"))
                    aut["hunger"] = round(clamp_float(aut["hunger"] + 0.005 * delta_min, 0.0, 1.0, 0.3), precision)
                    aut["curiosity"] = round(clamp_float(aut["curiosity"] + 0.006 * delta_min, 0.0, 1.0, 0.55), precision)
                    act["autonomy"] = aut
                    self._refresh_actor_expression_locked(act)
                    self._sync_actor_world_state_locked(act)

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
    def canvas_summary(self) -> dict[str, Any]:
        with self._lock:
            spaces = self._world["spaces"]
            sid = self._world.get("active_space_id")
            if sid not in spaces:
                sid = next(iter(spaces))
            return {
                "active_space_id": sid,
                "active_actor_id": self.resolve_actor_id(space_id=sid),
                "space_count": len(spaces),
                "actor_count": len(self._world["actors"]),
                "entity_count": sum(ent["space_id"] == sid and not ent["parent_id"].startswith("inventory:")
                                    for ent in self._world["entities"].values()),
            }

    def snapshot_for_ui(self, space_id: str = "") -> dict[str, Any]:
        with self._lock:
            sid = str(space_id or self._world.get("active_space_id") or "")
            if sid not in self._world["spaces"]:
                sid = next(iter(self._world["spaces"]))
            space = copy.deepcopy(self._world["spaces"][sid])
            entities_in_space: list[dict[str, Any]] = []
            for ent in self._world["entities"].values():
                if ent.get("space_id") != sid or ent["parent_id"].startswith("inventory:"):
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
            active_aid = self.resolve_actor_id(space_id=sid)
            active_act = self._world["actors"].get(active_aid) if active_aid else None
            current_zone = (
                self.get_zone_at(space, int(active_act.get("x") or 0), int(active_act.get("y") or 0))
                if active_act and active_act.get("space_id") == sid
                else None
            )
            parent_sid = str(space.get("parent_space_id") or "").strip()
            parent_sp = self._world["spaces"].get(parent_sid) if parent_sid else None
            parent_space_summary = (
                {
                    "space_id": str(parent_sp["space_id"]),
                    "name": str(parent_sp["name"]),
                    "space_kind": str(parent_sp.get("space_kind") or "room"),
                }
                if parent_sp
                else None
            )

            door_by_target: dict[str, str] = {}
            for ent in entities_in_space:
                pt = (ent.get("traits") or {}).get("portal")
                if isinstance(pt, Mapping) and str(pt.get("target_space_id") or "").strip():
                    door_by_target.setdefault(str(pt["target_space_id"]).strip(), str(ent["entity_id"]))
            building_by_interior = {
                str(b.get("interior_space_id") or "").strip(): b
                for b in (space.get("buildings") or [])
                if str(b.get("interior_space_id") or "").strip()
            }
            child_spaces: list[dict[str, Any]] = []
            for csp in self._world["spaces"].values():
                csid = str(csp.get("space_id") or "")
                if str(csp.get("parent_space_id") or "").strip() != sid:
                    continue
                bmeta = building_by_interior.get(csid) or {}
                child_spaces.append({
                    "space_id": csid,
                    "name": str(csp.get("name") or csid),
                    "district": str(csp.get("district") or ""),
                    "icon": str(bmeta.get("icon") or "🏠"),
                    "category": str(bmeta.get("category") or "house"),
                    "door_x": bmeta.get("door_x"),
                    "door_y": bmeta.get("door_y"),
                    "door_entity_id": door_by_target.get(csid, ""),
                    "entity_count": sum(
                        1 for ent in self._world["entities"].values() if ent.get("space_id") == csid
                    ),
                    "actor_names": [
                        str(act.get("name") or act.get("actor_id") or "")
                        for act in self._world["actors"].values()
                        if act.get("space_id") == csid
                    ],
                })

            return {
                "active_space_id": sid,
                "active_actor_id": active_aid,
                "realm_space_id": self._realm_space_id_locked(sid),
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
                        "child_count": sum(
                            1
                            for sp2 in self._world["spaces"].values()
                            if str(sp2.get("parent_space_id") or "").strip() == str(sp.get("space_id") or "")
                        ),
                    }
                    for sp in self._world["spaces"].values()
                ],
                "space": space,
                "parent_space": parent_space_summary,
                "child_spaces": child_spaces,
                "current_zone": current_zone,
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
