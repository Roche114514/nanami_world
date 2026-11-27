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
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from . import episodic_memory, persona_bridge, schedule, sprite_pack, venue_semantics, weather
from .build_history import BuildEntry, BuildHistory, build_action, capture_build_state, diff_build_state
from .schema import (
    AWAKE_REST_RECOVERY_TARGET,
    DEFAULT_ACTOR_PIXELS_16X24,
    DEFAULT_ACTOR_SLOT_COLORS,
    MAP_STYLES,
    MAX_BRIEF_CHARS,
    MAX_DETAIL_CHARS,
    MAX_ELEVATION,
    MAX_SPACE_HEIGHT,
    MAX_SPACE_WIDTH,
    MAX_SPACE_ZONES,
    MIN_SPACE_HEIGHT,
    MIN_SPACE_WIDTH,
    SPACE_RELIEF_FIELDS,
    TILE_CATALOG,
    VALID_ACTOR_STATUSES,
    VALID_CLIFF_STYLES,
    VALID_SPACE_KINDS,
    VALID_STAIR_STYLES,
    _normalize_expression,
    _normalize_vitals,
    clamp_float,
    clamp_int,
    clip_text,
    default_appearance,
    infer_consumable_names,
    infer_rest_mode,
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
    normalize_space_cliff_styles,
    normalize_space_elevations,
    normalize_space_stair_styles,
    normalize_space_stairs,
    normalize_space_tiles,
    normalize_sprite,
    normalize_traits,
    normalize_zones,
    slugify,
)

logger = logging.getLogger(__name__)

class _UnspecifiedReliefStyle(str):
    """An omitted brush material keeps the existing override."""


_UNSPECIFIED_RELIEF_STYLE = _UnspecifiedReliefStyle("")


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
        "hour": dt.hour,
        "minute": dt.minute,
        "hour_float": round(dt.hour + dt.minute / 60.0, 3),
        "sim_day": dt.toordinal(),
        "light_mode": mode,
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
        self._isolated_active_actors: dict[str, str] = {}
        self._defer_save_depth: int = 0
        self._save_pending: bool = False
        self._build_history = BuildHistory()
        self._build_events: list[dict[str, Any]] | None = None

        self._stamps_path = self._data_root / "stamps.json"
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
        self._stamps: dict[str, dict[str, Any]] = {}
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
                        if not bp["ephemeral_realm_id"]:
                            self._custom_blueprints[str(bid)] = bp

            raw_pals = _read_json_file(self._palettes_path, {})
            if isinstance(raw_pals, Mapping):
                for pid, item in raw_pals.items():
                    if isinstance(item, Mapping):
                        pal = normalize_palette(item, builtin=False)
                        self._custom_palettes[str(pid)] = pal

            raw_stamps = _read_json_file(self._stamps_path, {})
            if isinstance(raw_stamps, Mapping):
                self._stamps = {
                    str(stamp_id): self._normalize_build_stamp(stamp, str(stamp_id))
                    for stamp_id, stamp in raw_stamps.items() if isinstance(stamp, Mapping)
                }

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
            "weather_by_realm": {},
            "shop_restock_day_by_realm": {},
            "next_entity_num": 1,
            "spaces": {space["space_id"]: space},
            "actors": {},
            "entities": {},
            "pending_diffs": [],
            "looked_entities": {},
            "external_actor_state": {},
            "external_overlays": {},
            "ephemeral_blueprints": {},
            "character_appearances": {},
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

        sim_min = max(0.0, float(raw.get("simulation_minutes") or 0.0))
        weather_by_realm: dict[str, dict[str, Any]] = {}
        raw_weather = raw.get("weather_by_realm")
        if isinstance(raw_weather, Mapping):
            for r_sid, w_raw in raw_weather.items():
                clean_r = str(r_sid or "").strip()
                if clean_r in spaces:
                    weather_by_realm[clean_r] = weather.normalize_weather_state(w_raw, sim_minutes=sim_min)
        shop_restock_day_by_realm: dict[str, int] = {}
        raw_restock = raw.get("shop_restock_day_by_realm")
        if isinstance(raw_restock, Mapping):
            for r_sid, day_val in raw_restock.items():
                clean_r = str(r_sid or "").strip()
                if clean_r in spaces:
                    try:
                        shop_restock_day_by_realm[clean_r] = int(day_val)
                    except (TypeError, ValueError):
                        continue

        external_actor_state: dict[str, dict[str, dict[str, Any]]] = {}
        raw_ext_state = raw.get("external_actor_state")
        if isinstance(raw_ext_state, Mapping):
            for r_sid, by_actor in raw_ext_state.items():
                clean_r = str(r_sid or "").strip()
                if clean_r not in spaces or not isinstance(by_actor, Mapping):
                    continue
                clean_map: dict[str, dict[str, Any]] = {}
                for aid_x, rec in by_actor.items():
                    clean_aid = str(aid_x or "").strip()
                    if clean_aid not in actors or not isinstance(rec, Mapping):
                        continue
                    cd_raw = rec.get("covert_directive")
                    cd_norm = self._normalize_covert_directive(cd_raw, sim_now=sim_min) if isinstance(cd_raw, Mapping) else None
                    clean_map[clean_aid] = {
                        "private_brief": clip_text(rec.get("private_brief"), 480, ""),
                        "cover_goal": clip_text(rec.get("cover_goal"), 36, ""),
                        "covert_directive": cd_norm,
                        "updated_ts": float(rec.get("updated_ts") or 0.0),
                    }
                if clean_map:
                    external_actor_state[clean_r] = clean_map

        external_overlays: dict[str, dict[str, Any]] = {}
        raw_overlays = raw.get("external_overlays")
        if isinstance(raw_overlays, Mapping):
            for r_sid, ov in raw_overlays.items():
                clean_r = str(r_sid or "").strip()
                if clean_r in spaces and isinstance(ov, Mapping):
                    external_overlays[clean_r] = self._normalize_overlay(ov)

        ephemeral_blueprints = {}
        saved_blueprints = raw.get("ephemeral_blueprints")
        for bid, item in (saved_blueprints.items() if isinstance(saved_blueprints, Mapping) else ()):
            if isinstance(item, Mapping) and item.get("ephemeral_realm_id") in spaces:
                ephemeral_blueprints[bid] = normalize_blueprint(item)

        character_appearances: dict[str, dict[str, Any]] = {}
        raw_char_apps = raw.get("character_appearances")
        if isinstance(raw_char_apps, Mapping):
            for c_key, rec in raw_char_apps.items():
                clean_ck = str(c_key or "").strip()
                if not clean_ck or not isinstance(rec, Mapping):
                    continue
                ms = str(rec.get("map_style") or "doll").strip().lower()
                if ms not in MAP_STYLES:
                    ms = "doll"
                character_appearances[clean_ck] = {
                    "appearance": normalize_appearance(rec.get("appearance"), seed=clean_ck),
                    "map_style": ms,
                }

        return {
            "version": 1,
            "active_space_id": active_sid,
            "active_actor_id": active_aid,
            "last_tick_ts": float(raw.get("last_tick_ts") or time.time()),
            "clock_offset_minutes": float(raw.get("clock_offset_minutes") or 0.0),
            "simulation_minutes": sim_min,
            "next_seq": max(1, int(raw.get("next_seq") or 1)),
            "event_source": str(raw.get("event_source") or uuid4().hex),
            "persona_events": dict(raw.get("persona_events") or {}),
            "puzzle_memories": dict(raw.get("puzzle_memories") or {}),
            "key_loans": dict(raw.get("key_loans") or {}),
            "gift_offers": dict(raw.get("gift_offers") or {}),
            "weather_by_realm": weather_by_realm,
            "shop_restock_day_by_realm": shop_restock_day_by_realm,
            "next_entity_num": max(1, int(raw.get("next_entity_num") or 1)),
            "spaces": spaces,
            "actors": actors,
            "entities": entities,
            "pending_diffs": list(raw.get("pending_diffs") or [])[-30:],
            "looked_entities": dict(raw.get("looked_entities") or {}),
            "external_actor_state": external_actor_state,
            "external_overlays": external_overlays,
            "ephemeral_blueprints": ephemeral_blueprints,
            "character_appearances": character_appearances,
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
        realm = str(raw.get("ephemeral_realm_id") or "").strip()
        tags = [clip_text(t, 32 if realm else 16).lower() for t in (raw.get("tags") or []) if clip_text(t, 16)][:6]
        traits = normalize_traits(
            raw.get("traits") if isinstance(raw.get("traits"), Mapping) else {},
            name=name,
            tags=tags,
        )
        solid = bool(raw.get("solid", False))
        if "restable" in traits:
            solid = False
        if "portal" in traits:
            placement = "any"
            solid = not bool(traits["portal"].get("is_open", False))
        return {
            "entity_id": eid,
            "blueprint_id": str(raw.get("blueprint_id") or "").strip(),
            "ephemeral_realm_id": realm,
            **({"examined_by": [str(aid) for aid in (raw.get("examined_by") or ())][:64]} if realm else {}),
            "name": name,
            "brief": clip_text(raw.get("brief"), 60 if realm else MAX_BRIEF_CHARS, name),
            "detail": clip_text(raw.get("detail"), 180 if realm else MAX_DETAIL_CHARS, f"一件{name}。"),
            "tags": tags,
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
        if self._defer_save_depth > 0:
            self._save_pending = True
            return
        # 物理更新与交互保存共用世界锁，紧凑编码使用 JSON 快速路径。
        _atomic_write_json(self._world_path, self._world, indent=None)

    @contextmanager
    def batch_mutations(self):
        """将成批世界构建操作合成为单次原子落盘，显著减少重复 JSON 序列化耗时。"""
        with self._lock:
            self._defer_save_depth += 1
        try:
            yield
        finally:
            with self._lock:
                self._defer_save_depth = max(0, self._defer_save_depth - 1)
                if self._defer_save_depth == 0 and self._save_pending:
                    self._save_pending = False
                    self._save_world_locked()

    def _lookup_blueprint_locked(self, blueprint_id: str, space_id: str | None = None) -> dict[str, Any] | None:
        realm = self._realm_space_id_locked(space_id or self._world["active_space_id"])
        ephem = self._world["ephemeral_blueprints"].get(blueprint_id)
        if ephem and ephem.get("ephemeral_realm_id") == realm:
            return copy.deepcopy(ephem)
        raw = self._custom_blueprints.get(blueprint_id) or self._builtin_blueprints.get(blueprint_id)
        return copy.deepcopy(raw) if raw is not None else None

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
        if self._build_events is not None:
            self._build_events.append(event)
        else:
            self._write_event_locked(event)
        return event

    def _write_event_locked(self, event: Mapping[str, Any]) -> None:
        try:
            self._events_path.parent.mkdir(parents=True, exist_ok=True)
            with self._events_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, ensure_ascii=False) + "\n")
        except OSError:
            logger.warning("nanami_world: 写入 events.jsonl 失败", exc_info=True)

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

    def poll_events(
        self,
        *,
        after_seq: int = 0,
        limit: int = 100,
        realm_id: str = "",
        kinds: Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        """按游标读取不可篡改世界事件流，供外部插件订阅状态变更。"""
        with self._lock:
            if not self._events_path.is_file():
                return []
            try:
                lines = self._events_path.read_text(encoding="utf-8").splitlines()
            except OSError:
                return []
            kind_set = {str(k).strip() for k in (kinds or ()) if str(k).strip()} or None
            target_realm = self._realm_space_id_locked(realm_id) if realm_id else ""
            out: list[dict[str, Any]] = []
            max_items = max(1, min(500, int(limit or 100)))
            for raw in lines:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    ev = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(ev, dict) or int(ev.get("seq") or 0) <= int(after_seq):
                    continue
                if kind_set and str(ev.get("kind") or "") not in kind_set:
                    continue
                if target_realm:
                    ev_sid = str(ev.get("space_id") or "").strip()
                    if ev_sid in self._world["spaces"]:
                        ev_realm = self._realm_space_id_locked(ev_sid)
                    elif "__" in ev_sid:
                        ev_realm = ev_sid.split("__", 1)[0]
                    else:
                        ev_realm = ev_sid
                    if ev_realm != target_realm:
                        continue
                out.append(ev)
                if len(out) >= max_items:
                    break
            return out

    # ── 只读工具与目录查询 ──────────────────────────────────────────────
    def all_blueprints(self, space_id: str | None = None, *, include_ephemeral: bool = True) -> dict[str, dict[str, Any]]:
        with self._lock:
            merged = {**self._builtin_blueprints, **self._custom_blueprints}
            if include_ephemeral:
                realm = self._realm_space_id_locked(space_id or self._world["active_space_id"])
                merged.update({
                    bid: bp for bid, bp in self._world["ephemeral_blueprints"].items()
                    if bp["ephemeral_realm_id"] == realm
                })
            return copy.deepcopy(merged)

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

    def get_cell_elevation(self, space: Mapping[str, Any], x: int, y: int) -> int:
        elevs = space.get("elevations")
        if not isinstance(elevs, Mapping) or not elevs:
            return 0
        return clamp_int(elevs.get(f"{int(x)},{int(y)}"), 0, MAX_ELEVATION, 0)

    def is_stair_cell(self, space: Mapping[str, Any], x: int, y: int) -> bool:
        stairs = space.get("stairs")
        if not isinstance(stairs, Mapping) or not stairs:
            return False
        return f"{int(x)},{int(y)}" in stairs

    def can_step_elevation(
        self, space: Mapping[str, Any], x0: int, y0: int, x1: int, y1: int
    ) -> bool:
        elevs = space.get("elevations")
        if not isinstance(elevs, Mapping) or not elevs:
            return True
        k0 = f"{int(x0)},{int(y0)}"
        k1 = f"{int(x1)},{int(y1)}"
        e0 = self.get_cell_elevation(space, x0, y0)
        e1 = self.get_cell_elevation(space, x1, y1)
        if e0 == e1:
            return True
        if abs(e0 - e1) > 1:
            return False
        stairs = space.get("stairs")
        if not isinstance(stairs, Mapping) or not stairs:
            return False
        need_orient = "ns" if int(x0) == int(x1) else "ew"
        st0 = stairs.get(k0)
        st1 = stairs.get(k1)
        return st0 in (need_orient, "both") or st1 in (need_orient, "both")

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

    def _is_unlocked_interior_door_locked(self, ent: Mapping[str, Any]) -> bool:
        pt = (ent.get("traits") or {}).get("portal")
        if not isinstance(pt, Mapping):
            return False
        if str(pt.get("target_space_id") or "").strip():
            return False
        if bool(pt.get("is_locked", False)):
            return False
        return "interior_door" in (ent.get("tags") or ())

    def _open_interior_doors_on_path_locked(
        self,
        space_id: str,
        cells: Sequence[tuple[int, int]],
        actor: Mapping[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        if not cells:
            return []
        cell_set = {(int(x), int(y)) for x, y in cells}
        opened: list[dict[str, Any]] = []
        actor_id = str((actor or {}).get("actor_id") or "")
        for ent in self._world["entities"].values():
            if ent.get("space_id") != space_id or int(ent.get("z", 0)) != 0:
                continue
            if not self._is_unlocked_interior_door_locked(ent):
                continue
            pt = ent["traits"]["portal"]
            if bool(pt.get("is_open", False)) and not ent.get("solid"):
                continue
            if not (cell_set & set(self._entity_cells(ent))):
                continue
            pt["is_open"] = True
            ent["solid"] = False
            self._append_event_locked(
                "entity_state_changed",
                actor_id=actor_id,
                space_id=space_id,
                payload={"entity_id": ent["entity_id"], "is_open": True, "solid": False},
            )
            opened.append(ent)
        return opened

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

    def _realm_buildings_locked(self, realm_id: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        seen_int: set[str] = set()
        ordered_sids = [
            realm_id,
            *(
                sid
                for sid in self._world["spaces"]
                if sid != realm_id and self._realm_space_id_locked(sid) == realm_id
            ),
        ]
        for sp_id in ordered_sids:
            sp = self._world["spaces"].get(sp_id)
            if not sp:
                continue
            for raw_b in sp.get("buildings") or ():
                if not isinstance(raw_b, Mapping):
                    continue
                int_sid = str(raw_b.get("interior_space_id") or "").strip()
                if int_sid and int_sid in seen_int:
                    continue
                if int_sid:
                    seen_int.add(int_sid)
                b_copy = copy.deepcopy(dict(raw_b))
                if not b_copy.get("parent_space_id"):
                    b_copy["parent_space_id"] = sp_id
                out.append(b_copy)
        return out

    def _realm_portals_locked(self, realm_id: str) -> dict[str, dict[str, Any]]:
        portals: dict[str, dict[str, Any]] = {}
        spaces = self._world["spaces"]
        for ent in self._world["entities"].values():
            src_sid = str(ent.get("space_id") or "")
            if not src_sid or src_sid not in spaces or self._realm_space_id_locked(src_sid) != realm_id:
                continue
            p_meta = (ent.get("traits") or {}).get("portal")
            if not isinstance(p_meta, Mapping):
                continue
            tgt_sid = str(p_meta.get("target_space_id") or "").strip()
            if not tgt_sid or tgt_sid not in spaces:
                continue
            portals[f"{src_sid}:{tgt_sid}"] = {
                "space": src_sid,
                "target": tgt_sid,
                "name": str(ent.get("name") or spaces[tgt_sid].get("name") or tgt_sid),
                "x": int(ent.get("x") or 0),
                "y": int(ent.get("y") or 0),
                "entity_id": str(ent.get("entity_id") or ""),
            }
        return portals

    def list_realm_places(self, space_id: str) -> dict[str, str]:
        with self._lock:
            sid = str(space_id or self._world["active_space_id"]).strip()
            if sid not in self._world["spaces"]:
                return {}
            _realm, places = self._realm_places_locked(sid)
            return places

    def list_realm_buildings(self, space_id: str) -> list[dict[str, Any]]:
        with self._lock:
            realm = self._realm_space_id_locked(space_id)
            return self._realm_buildings_locked(realm)

    def list_realms_summary(self) -> list[dict[str, Any]]:
        """列出全部顶层平行域摘要，供外部插件发现并挂载现有常驻小镇。"""
        with self._lock:
            spaces = self._world.get("spaces") or {}
            active_sid = str(self._world.get("active_space_id") or "")
            active_realm = self._realm_space_id_locked(active_sid) if active_sid in spaces else ""
            sim_min = float(self._world.get("simulation_minutes") or 0.0)
            out: list[dict[str, Any]] = []
            for sp_id, sp in spaces.items():
                if self._realm_space_id_locked(sp_id) != sp_id:
                    continue
                realm_sids = {sid for sid in spaces if self._realm_space_id_locked(sid) == sp_id}
                actors = [
                    {
                        "actor_id": str(a.get("actor_id") or aid),
                        "name": str(a.get("name") or aid),
                        "host_character": str(a.get("host_character") or ""),
                        "is_player": bool(a.get("is_player", False)),
                        "is_native_npc": bool(a.get("is_native_npc", False)),
                        "space_id": str(a.get("space_id") or ""),
                        "actor_status": str(a.get("actor_status") or "active"),
                    }
                    for aid, a in (self._world.get("actors") or {}).items()
                    if a.get("space_id") in realm_sids
                ]
                buildings = self._realm_buildings_locked(sp_id)
                out.append({
                    "realm_space_id": sp_id,
                    "name": str(sp.get("name") or sp_id),
                    "space_kind": str(sp.get("space_kind") or "room"),
                    "is_active": sp_id == active_realm,
                    "isolated_persona": self._is_isolated_persona_realm_locked(sp_id),
                    "opaque_minds": self._is_opaque_realm_locked(sp_id),
                    "minutes": round(sim_min, 2),
                    "space_count": len(realm_sids),
                    "building_count": len(buildings),
                    "actors": actors,
                })
            out.sort(key=lambda row: (0 if row["is_active"] else 1, row["name"]))
            return out

    def _weather_state_locked(self, realm_id: str) -> dict[str, Any]:
        sim_min = float(self._world.get("simulation_minutes") or 0.0)
        w_map = self._world.setdefault("weather_by_realm", {})
        raw = w_map.get(realm_id)
        if not isinstance(raw, Mapping):
            st = weather.default_weather_state(sim_min)
            w_map[realm_id] = st
            return st
        st = weather.normalize_weather_state(raw, sim_minutes=sim_min)
        w_map[realm_id] = st
        return st

    def _weather_view_locked(self, space_id: str) -> dict[str, Any]:
        sid = str(space_id or self._world.get("active_space_id") or "").strip()
        sp = self._world["spaces"].get(sid) or {}
        realm = self._realm_space_id_locked(sid) if sid else ""
        st = self._weather_state_locked(realm) if realm else weather.default_weather_state()
        is_outdoor = sp.get("space_kind") == "town"
        return weather.weather_view(st, is_outdoor=is_outdoor)

    def get_weather(self, space_id: str = "") -> dict[str, Any]:
        with self._lock:
            sid = str(space_id or self._world.get("active_space_id") or "").strip()
            return self._weather_view_locked(sid)

    def _realm_clock_offset_locked(self, space_id: str) -> float:
        base = float(self._world.get("clock_offset_minutes") or 0.0)
        sid = str(space_id or "").strip()
        if not sid or sid not in self._world["spaces"]:
            return base
        realm = self._realm_space_id_locked(sid)
        realm_off = float((self._world["spaces"].get(realm) or {}).get("clock_offset_minutes") or 0.0)
        return base + realm_off

    def set_realm_initial_hour(self, space_id: str, hour_float: float) -> float:
        """为指定平行域设置独立初始时刻偏移，不影响主世界及默认房间时钟。"""
        with self._lock:
            sid = str(space_id or "").strip()
            if sid not in self._world["spaces"]:
                raise ValueError(f"空间不存在：{sid}")
            realm = self._realm_space_id_locked(sid)
            global_off = float(self._world.get("clock_offset_minutes") or 0.0)
            base_dt = datetime.fromtimestamp(time.time() + global_off * 60.0)
            cur_h = base_dt.hour + base_dt.minute / 60.0 + base_dt.second / 3600.0
            delta_hours = ((float(hour_float) % 24.0) - cur_h) % 24.0
            delta_min = round(delta_hours * 60.0, 2)
            self._world["spaces"][realm]["clock_offset_minutes"] = delta_min
            self._save_world_locked()
            return delta_min

    def get_lighting(self, space_id: str = "") -> dict[str, Any]:
        with self._lock:
            sid = str(space_id or self._world.get("active_space_id") or "").strip()
            if sid not in self._world["spaces"]:
                sid = next(iter(self._world["spaces"]))
            space = self._world["spaces"][sid]
            return compute_lighting(
                space,
                self._world["entities"],
                time.time(),
                self._realm_clock_offset_locked(sid),
            )

    def _record_weather_transition_locked(
        self,
        realm_id: str,
        prev_code: str,
        next_state: Mapping[str, Any],
    ) -> None:
        new_code = str(next_state.get("current") or "clear")
        meta = weather.WEATHER_META.get(new_code, weather.WEATHER_META["clear"])
        self._append_event_locked(
            "weather_changed",
            space_id=realm_id,
            payload={
                "from": prev_code,
                "to": new_code,
                "intensity": float(next_state.get("intensity") or 0.0),
                "mode": str(next_state.get("mode") or "auto"),
            },
        )
        self._push_diff_locked(realm_id, f"天气转变为{meta['label']}（{meta['outdoor_desc']}）")
        seen_child_sids: set[str] = set()
        for act in self._world["actors"].values():
            if act.get("is_player"):
                continue
            asid = str(act.get("space_id") or "")
            if self._realm_space_id_locked(asid) != realm_id:
                continue
            sp = self._world["spaces"].get(asid) or {}
            is_outdoor = sp.get("space_kind") == "town"
            if not is_outdoor and asid not in seen_child_sids:
                seen_child_sids.add(asid)
                self._push_diff_locked(asid, f"{meta['indoor_desc']}。")
            spec = weather.weather_persona_event_spec(
                new_code,
                float(next_state.get("intensity") or 0.0),
                is_outdoor=is_outdoor,
                prev_code=prev_code,
            )
            if spec:
                self._queue_persona_event_locked(
                    act,
                    spec["kind"],
                    None,
                    "weather",
                    spec["label"],
                    epa=spec["epa"],
                    need=spec["need"],
                )

    def set_weather(
        self,
        space_id: str = "",
        mode: str = "auto",
        *,
        intensity: float | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            sid = str(space_id or self._world.get("active_space_id") or "").strip()
            if sid not in self._world["spaces"]:
                raise ValueError(f"空间不存在：{sid}")
            clean_mode = str(mode or "auto").strip().lower()
            if clean_mode not in weather.VALID_WEATHER_MODES:
                raise ValueError(f"不支持的天气模式：{mode}")
            realm = self._realm_space_id_locked(sid)
            prev = self._weather_state_locked(realm)
            prev_code = prev["current"]
            sim_min = float(self._world.get("simulation_minutes") or 0.0)

            if clean_mode == "auto":
                nxt = {
                    **prev,
                    "mode": "auto",
                    "next_change_sim_min": round(sim_min + 180.0, 3),
                }
                if intensity is not None and prev_code != "clear":
                    nxt["intensity"] = round(clamp_float(intensity, 0.15, 1.0, prev["intensity"]), 3)
            else:
                def_int = 0.0 if clean_mode == "clear" else (0.6 if clean_mode in ("rain", "snow") else 0.5)
                eff_int = 0.0 if clean_mode == "clear" else clamp_float(
                    intensity if intensity is not None else def_int, 0.15, 1.0, def_int,
                )
                nxt = {
                    **prev,
                    "mode": clean_mode,
                    "current": clean_mode,
                    "intensity": round(eff_int, 3),
                    "changed_sim_min": round(sim_min, 3),
                    "next_change_sim_min": round(sim_min + 240.0, 3),
                    "transition_seq": int(prev.get("transition_seq") or 0) + (1 if clean_mode != prev_code else 0),
                }

            self._world.setdefault("weather_by_realm", {})[realm] = nxt
            if nxt["current"] != prev_code:
                self._record_weather_transition_locked(realm, prev_code, nxt)
            self._save_world_locked()
            return self._weather_view_locked(sid)

    def _resolve_actor_home_locked(self, actor: Mapping[str, Any], realm_id: str) -> str:
        hs_map = actor.get("home_spaces") if isinstance(actor.get("home_spaces"), Mapping) else {}
        explicit = str(hs_map.get(realm_id) or "").strip()
        realm_buildings = self._realm_buildings_locked(realm_id)
        valid_sids = {
            sid for sid in self._world["spaces"]
            if sid != realm_id and self._realm_space_id_locked(sid) == realm_id
        }
        return schedule.default_home_space_id(
            str(actor.get("name") or ""),
            str(actor.get("host_character") or ""),
            realm_buildings,
            explicit_home=explicit,
            valid_space_ids=valid_sids,
            actor_id=str(actor.get("actor_id") or ""),
        )

    def get_actor_home_space(self, actor_id: str, space_id: str = "") -> str:
        with self._lock:
            actor = self._world["actors"].get(actor_id)
            if not actor:
                raise ValueError(f"角色不存在：{actor_id}")
            sid = str(space_id or actor.get("space_id") or "").strip()
            realm = self._realm_space_id_locked(sid) if sid in self._world["spaces"] else sid
            return self._resolve_actor_home_locked(actor, realm)

    def set_actor_home_space(
        self,
        actor_id: str,
        home_space_id: str,
        *,
        realm_space_id: str = "",
    ) -> str:
        with self._lock:
            actor = self._world["actors"].get(actor_id)
            if not actor:
                raise ValueError(f"角色不存在：{actor_id}")
            base_sid = str(realm_space_id or actor.get("space_id") or self._world.get("active_space_id") or "").strip()
            realm = self._realm_space_id_locked(base_sid)
            clean_home = str(home_space_id or "").strip()
            hs_map = actor.setdefault("home_spaces", {})
            if not clean_home:
                hs_map.pop(realm, None)
            else:
                if (
                    clean_home not in self._world["spaces"]
                    or self._realm_space_id_locked(clean_home) != realm
                    or clean_home == realm
                ):
                    raise ValueError("指派的住处必须是同一城镇域内的建筑室内空间。")
                hs_map[realm] = clean_home
            resolved = self._resolve_actor_home_locked(actor, realm)
            self._append_event_locked(
                "actor_home_updated",
                actor_id=actor_id,
                space_id=realm,
                payload={"home_space_id": resolved, "explicit": bool(clean_home)},
            )
            self._save_world_locked()
            return resolved

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
        pass_interior_doors: bool = False,
    ) -> tuple[set[tuple[int, int]], set[tuple[int, int]]]:
        blocked: set[tuple[int, int]] = set()
        open_doors: set[tuple[int, int]] = set()
        for ent in self._world["entities"].values():
            if ent.get("space_id") != space_id or int(ent.get("z", 0)) != 0:
                continue
            traits = ent.get("traits") or {}
            cells = self._entity_cells(ent)
            if "portal" in traits and (
                bool(traits["portal"].get("is_open", False))
                or (pass_interior_doors and self._is_unlocked_interior_door_locked(ent))
            ):
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
        pass_interior_doors: bool = False,
    ) -> list[tuple[int, int]] | None:
        space = self._world["spaces"].get(space_id)
        if not space or not goal_cells:
            return None
        if start in goal_cells:
            return [start]
        w = int(space["width"])
        h = int(space["height"])
        blocked, open_doors = self._build_walk_masks_locked(
            space_id,
            ignore_actor_id=ignore_actor_id,
            ignore_distant_actors_from=ignore_distant_actors_from,
            pass_interior_doors=pass_interior_doors,
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
                if not self.can_step_elevation(space, cx, cy, nx, ny):
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
                if not self.can_step_elevation(space, cx, cy, nx, ny):
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
        if any(self.get_cell_tile(space, cx, cy).get("tile_id") == "void" for cx, cy in req_cells):
            raise ValueError("该位置为虚空（无地块），请先铺设地面或墙体。")
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

        if len(req_cells) > 1:
            elev_set = {self.get_cell_elevation(space, cx, cy) for cx, cy in req_cells}
            if len(elev_set) > 1:
                raise ValueError("该位置横跨不同高度的台地边缘。")

        if (solid or placement == "floor") and any(self.is_stair_cell(space, cx, cy) for cx, cy in req_cells):
            raise ValueError("台阶通道上不能放置阻挡物或地面家具。")

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
    def _available_space_id_locked(self, name: str) -> str:
        base = slugify(name, prefix="room")
        sid = base
        suffix = 2
        while sid in self._world["spaces"]:
            sid = f"{base}_{suffix}"
            suffix += 1
        return sid

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
        opaque_minds: bool = False,
        isolated_persona: bool = False,
        clock_offset_minutes: float = 0.0,
        hub_location_id: str = "",
        floor_index: int = 1,
        floor_label: str = "",
        building_group_id: str = "",
    ) -> dict[str, Any]:
        with self._lock:
            clean_name = clip_text(name, 16, "新空间")
            explicit_sid = str(fixed_space_id or "").strip()
            if explicit_sid:
                if explicit_sid in self._world["spaces"]:
                    raise ValueError(f"空间已存在：{explicit_sid}")
                sid = explicit_sid
            else:
                sid = self._available_space_id_locked(clean_name)
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
                "floor_index": floor_index,
                "floor_label": floor_label,
                "building_group_id": building_group_id,
                "hub_location_id": str(hub_location_id or "").strip(),
                "district": district,
                "ambient_phrase": ambient_phrase or f"一间叫作「{clean_name}」的小空间。",
                "palette_id": palette_id,
                "opaque_minds": bool(opaque_minds),
                "isolated_persona": bool(isolated_persona),
                "clock_offset_minutes": float(clock_offset_minutes or 0.0),
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

    def delete_space(self, space_id: str, *, owned_actor_ids: tuple[str, ...] = ()) -> dict[str, Any]:
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

            isolated = self._is_isolated_persona_realm_locked(sid)
            removed_actor_ids = {
                aid for aid, act in self._world["actors"].items()
                if (act.get("is_native_npc") and str(act.get("native_realm_id") or "") in delete_ids)
                or act.get("ephemeral_realm_id") in delete_ids
                or (aid in owned_actor_ids and isolated
                    and self._realm_space_id_locked(act["space_id"]) == sid)
                or (act.get("is_player") and self._realm_space_id_locked(act["space_id"]) in delete_ids
                    and self._is_isolated_persona_realm_locked(act["space_id"]))
            }
            held_ids: set[str] = set()
            for aid, act in list(self._world["actors"].items()):
                if aid in removed_actor_ids:
                    self._clear_actor_rest_locked(act)
                    continue
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
                hs = act.get("home_spaces")
                if isinstance(hs, dict):
                    for hk in [k for k, v in hs.items() if k in delete_ids or v in delete_ids]:
                        hs.pop(hk, None)

            removed_actor_parents = {f"{kind}:{aid}" for aid in removed_actor_ids for kind in ("actor", "inventory")}
            removed_ids: list[str] = []
            for eid, ent in list(self._world["entities"].items()):
                if ent.get("ephemeral_realm_id") in delete_ids or ent.get("parent_id") in removed_actor_parents:
                    removed_ids.append(eid)
                    continue
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
                    pt.pop("paired_entity_id", None)

            for eid in removed_ids:
                self._world["entities"].pop(eid, None)
            self._world["ephemeral_blueprints"] = {
                bid: bp for bid, bp in self._world["ephemeral_blueprints"].items()
                if bp["ephemeral_realm_id"] not in delete_ids
            }
            self._cancel_key_loans_locked(removed_ids)
            for aid in removed_actor_ids:
                self._cleanup_removed_actor_locked(aid, drop_carried=False)
                self._world["actors"].pop(aid, None)
            if self._world.get("active_actor_id") in removed_actor_ids:
                self._world["active_actor_id"] = next(iter(self._world["actors"]), "")

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
                self._isolated_active_actors.pop(dsid, None)
                self._world["puzzle_memories"].pop(dsid, None)
                self._world.get("weather_by_realm", {}).pop(dsid, None)
                self._world.get("shop_restock_day_by_realm", {}).pop(dsid, None)
                self._world.get("external_actor_state", {}).pop(dsid, None)
                self._world.get("external_overlays", {}).pop(dsid, None)
                persona_bridge.delete_world_relations(self._data_root, dsid)
                persona_bridge.delete_isolated_persona_sandbox(self._data_root, dsid)
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
            self._build_history.discard_spaces(delete_ids)
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

    def generate_town(
        self,
        town_spec: Mapping[str, Any],
        *,
        town_id: str = "",
        move_actor_id: str = "",
        opaque_minds: bool = False,
        isolated_persona: bool = False,
        switch_active: bool = True,
        initial_hour: float | None = None,
        hub_location_id: str = "",
    ) -> dict[str, Any]:
        from . import town_builder
        return town_builder.build_generated_town(
            self,
            town_spec,
            town_id=town_id,
            move_actor_id=move_actor_id,
            opaque_minds=opaque_minds,
            isolated_persona=isolated_persona,
            switch_active=switch_active,
            initial_hour=initial_hour,
            hub_location_id=hub_location_id,
        )

    def _is_multi_space_realm_locked(self, space_id: str) -> bool:
        realm = self._realm_space_id_locked(space_id)
        sp = self._world["spaces"].get(realm) or {}
        if sp.get("space_kind") == "town" or bool(sp.get("buildings")):
            return True
        return any(str(other.get("parent_space_id") or "").strip() == realm for other in self._world["spaces"].values())

    def _is_opaque_realm_locked(self, space_id: str) -> bool:
        realm = self._realm_space_id_locked(space_id)
        return bool((self._world["spaces"].get(realm) or {}).get("opaque_minds", False))

    def is_opaque_realm(self, space_id: str) -> bool:
        with self._lock:
            return self._is_opaque_realm_locked(space_id)

    def set_realm_opaque_minds(self, space_id: str, enabled: bool) -> bool:
        with self._lock:
            sid = str(space_id or "").strip()
            if sid not in self._world["spaces"]:
                raise ValueError(f"空间不存在：{sid}")
            realm = self._realm_space_id_locked(sid)
            self._world["spaces"][realm]["opaque_minds"] = bool(enabled)
            self._save_world_locked()
            return bool(enabled)

    def _is_isolated_persona_realm_locked(self, space_id: str) -> bool:
        realm = self._realm_space_id_locked(space_id)
        return bool((self._world["spaces"].get(realm) or {}).get("isolated_persona", False))

    def is_isolated_persona_realm(self, space_id: str) -> bool:
        with self._lock:
            return self._is_isolated_persona_realm_locked(space_id)

    def set_realm_isolated_persona(self, space_id: str, enabled: bool) -> bool:
        with self._lock:
            sid = str(space_id or "").strip()
            if sid not in self._world["spaces"]:
                raise ValueError(f"空间不存在：{sid}")
            realm = self._realm_space_id_locked(sid)
            self._world["spaces"][realm]["isolated_persona"] = bool(enabled)
            self._save_world_locked()
            return bool(enabled)

    def add_town_building(self, town_space_id: str, **kwargs: Any) -> dict[str, Any]:
        from . import town_builder
        return town_builder.add_building_to_town(self, town_space_id, **kwargs)

    def validate_town(self, town_space_id: str) -> dict[str, Any]:
        from . import town_builder
        return town_builder.validate_town_layout(self, town_space_id)

    # ── 建造事务与地形编辑 ──────────────────────────────────────────────
    def _inbound_portal_spaces_locked(self, space_id: str) -> set[str]:
        return {
            ent["space_id"] for ent in self._world["entities"].values()
            if (ent.get("traits", {}).get("portal") or {}).get("target_space_id") == space_id
        }

    @contextmanager
    def _build_mutation_locked(
        self, label: str, space_id: str, extra_spaces: set[str] | None = None,
    ) -> Iterator[None]:
        if self._build_events is not None:
            yield
            return
        spaces = {space_id} | (extra_spaces or set())
        before = capture_build_state(self._world, spaces)
        old_pending = self._save_pending
        old_sequence = self._world["next_seq"]
        old_diffs = list(self._world["pending_diffs"])
        self._build_events = []
        self._defer_save_depth += 1
        try:
            yield
            after = capture_build_state(self._world, spaces, set(before["actors"]))
            edits = list(diff_build_state(before, after))
            self._defer_save_depth -= 1
            try:
                if self._defer_save_depth == 0 and self._save_pending:
                    self._save_world_locked()
                    self._save_pending = False
            finally:
                self._defer_save_depth += 1
        except BaseException:
            current = capture_build_state(self._world, spaces, set(before["actors"]))
            BuildEntry(label, space_id, spaces, list(diff_build_state(before, current))).apply(self._world, undo=True)
            self._world["next_seq"] = old_sequence
            self._world["pending_diffs"] = old_diffs
            self._save_pending = old_pending
            raise
        else:
            if edits:
                touched = {space_id}
                for edit in edits:
                    category, key, *_ = edit.path
                    if category == "spaces":
                        touched.add(key)
                    elif category in ("entities", "actors"):
                        for state in (before, after):
                            if key in state[category]:
                                touched.add(state[category][key]["space_id"])
                self._build_history.record(BuildEntry(label, space_id, touched, edits))
            for event in self._build_events:
                self._write_event_locked(event)
        finally:
            self._defer_save_depth -= 1
            self._build_events = None

    def _build_history_status_locked(self, sid: str) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for action, undo in (("undo", True), ("redo", False)):
            entry = self._build_history.head(sid, undo=undo)
            ready = entry is not None and entry.applicable(self._world, undo=undo)
            result[f"can_{action}"] = ready
            result[f"{action}_label"] = entry.label if ready else ""
        return result

    def _apply_build_history_locked(self, space_id: str, *, undo: bool) -> dict[str, Any]:
        sid = str(space_id or self._world["active_space_id"]).strip()
        entry = self._build_history.head(sid, undo=undo)
        if entry is None or not entry.applicable(self._world, undo=undo):
            raise ValueError("当前空间没有可撤销的建造操作。" if undo else "当前空间没有可重做的建造操作。")
        active = self._world["active_space_id"]
        previous = capture_build_state(self._world, entry.spaces)
        entry.apply(self._world, undo=undo)
        if active not in self._world["spaces"]:
            self._world["active_space_id"] = entry.primary
        try:
            self._save_world_locked()
        except BaseException:
            current = capture_build_state(self._world, entry.spaces, set(previous["actors"]))
            rollback = BuildEntry(entry.label, entry.primary, entry.spaces, list(diff_build_state(previous, current)))
            rollback.apply(self._world, undo=True)
            self._world["active_space_id"] = active
            raise
        self._build_history.advance(entry, undo=undo)
        return {"space_id": sid if sid in self._world["spaces"] else entry.primary, "label": entry.label}

    def undo_build(self, space_id: str) -> dict[str, Any]:
        with self._lock:
            return self._apply_build_history_locked(space_id, undo=True)

    def redo_build(self, space_id: str) -> dict[str, Any]:
        with self._lock:
            return self._apply_build_history_locked(space_id, undo=False)

    def _build_space_locked(self, space_id: str) -> dict[str, Any]:
        sid = str(space_id or self._world["active_space_id"]).strip()
        space = self._world["spaces"].get(sid)
        if space is None:
            raise ValueError(f"空间不存在：{sid}")
        return space

    def _relief_occupancy_locked(self, sid: str) -> tuple[set[tuple[int, int]], set[tuple[int, int]]]:
        blocked, footprints = set(), set()
        for ent in self._world["entities"].values():
            if ent["space_id"] != sid or ent["z"] != 0:
                continue
            cells = self._entity_cells(ent)
            if ent["solid"] or ent["placement"] == "floor":
                blocked.update(cells)
            if len(cells) > 1:
                footprints.update(cells)
        return blocked, footprints

    @staticmethod
    def _set_relief_cell(values: dict[str, Any], key: str, value: Any) -> bool:
        if value:
            if values.get(key) == value:
                return False
            values[key] = value
            return True
        return values.pop(key, None) is not None

    @build_action("涂刷台地与石阶")
    def paint_terrain_relief(
        self, space_id: str, cells: Sequence[Mapping[str, Any]] | None = None, *,
        mode: str = "raise", stair_orient: str = "ns", level: int | None = None,
        cliff_style: str = _UNSPECIFIED_RELIEF_STYLE, stair_style: str = _UNSPECIFIED_RELIEF_STYLE,
    ) -> dict[str, Any]:
        with self._lock:
            space = self._build_space_locked(space_id)
            sid, w, h = space["space_id"], space["width"], space["height"]
            if mode == "stair":
                mode = f"stair_{stair_orient}"
            stair_modes = {"stair_ns": "ns", "stair_ew": "ew", "stair_both": "both", "clear_stair": ""}
            elevation_sets = {"set_0": 0, "set_1": 1, "set_2": 2}
            if mode == "set_level":
                target_level = clamp_int(level, 0, MAX_ELEVATION, -1)
                if target_level < 0:
                    raise ValueError("请选择台地高度。")
                elevation_sets[mode] = target_level
            if mode not in stair_modes and mode not in elevation_sets and mode not in ("raise", "lower", "set_cliff"):
                raise ValueError(f"不支持的台地笔刷：{mode}")
            cliff_material = None if cliff_style is _UNSPECIFIED_RELIEF_STYLE else str(cliff_style or "").strip().lower()
            stair_material = None if stair_style is _UNSPECIFIED_RELIEF_STYLE else str(stair_style or "").strip().lower()
            blocked, footprints = self._relief_occupancy_locked(sid)
            points: dict[tuple[int, int], None] = {}
            for cell in cells or ():
                if isinstance(cell, Mapping):
                    try:
                        points[int(cell.get("x")), int(cell.get("y"))] = None
                    except (TypeError, ValueError, OverflowError):
                        continue

            elevations, stairs, cliffs, stair_materials = (space[field] for field in SPACE_RELIEF_FIELDS)
            skipped: list[list[int]] = []
            painted = 0
            for x, y in points:
                if (not (0 <= x < w and 0 <= y < h) or self.is_wall_cell(space, x, y)
                        or self.get_cell_tile(space, x, y)["tile_id"] == "void"):
                    skipped.append([x, y])
                    continue
                key = f"{x},{y}"
                if mode in stair_modes:
                    orient = stair_modes[mode]
                    if orient and (x, y) in blocked:
                        skipped.append([x, y])
                        continue
                    changed = self._set_relief_cell(stairs, key, orient)
                    if not orient or stair_material in ("", "auto", *VALID_STAIR_STYLES):
                        style = stair_material if orient and stair_material in VALID_STAIR_STYLES else ""
                        changed |= self._set_relief_cell(stair_materials, key, style)
                else:
                    previous = self.get_cell_elevation(space, x, y)
                    new_level = previous
                    if mode == "raise":
                        new_level = min(MAX_ELEVATION, previous + 1)
                    elif mode == "lower":
                        new_level = max(0, previous - 1)
                    elif mode in elevation_sets:
                        new_level = elevation_sets[mode]
                    if ((mode == "set_cliff" and previous == 0)
                            or (new_level != previous and (x, y) in footprints)):
                        skipped.append([x, y])
                        continue
                    changed = self._set_relief_cell(elevations, key, new_level)
                    if not new_level or cliff_material in ("", "auto", *VALID_CLIFF_STYLES):
                        style = cliff_material if new_level and cliff_material in VALID_CLIFF_STYLES else ""
                        changed |= self._set_relief_cell(cliffs, key, style)
                painted += int(changed)
            if painted:
                self._save_world_locked()
            return {"space_id": sid, "painted": painted, "skipped_cells": skipped}

    @build_action("保存区域")
    def upsert_space_zone(self, space_id: str, zone: Mapping[str, Any]) -> dict[str, Any]:
        with self._lock:
            space = self._build_space_locked(space_id)
            zones = list(space["zones"])
            zid = str(zone.get("zone_id") or "").strip()
            for index, existing in enumerate(zones):
                if existing["zone_id"] == zid:
                    zones[index] = {**existing, **zone, "zone_id": zid}
                    break
            else:
                if len(zones) >= MAX_SPACE_ZONES:
                    raise ValueError(f"每个空间最多可划定 {MAX_SPACE_ZONES} 个区域。")
                index = len(zones)
                if not zid:
                    base = slugify(clip_text(zone.get("name"), 16, "区域"), prefix="zone")
                    used = {z["zone_id"] for z in zones}
                    zid = base
                    suffix = 2
                    while zid in used:
                        zid = f"{base}_{suffix}"
                        suffix += 1
                zones.append({**zone, "zone_id": zid})
            space["zones"] = normalize_zones(zones, space["width"], space["height"])
            self._save_world_locked()
            return {"space_id": space["space_id"], "zone": copy.deepcopy(space["zones"][index])}

    @build_action("删除区域")
    def delete_space_zone(self, space_id: str, zone_id: str) -> dict[str, Any]:
        with self._lock:
            space = self._build_space_locked(space_id)
            zones = [zone for zone in space["zones"] if zone["zone_id"] != zone_id]
            if len(zones) == len(space["zones"]):
                raise ValueError(f"区域不存在：{zone_id}")
            space["zones"] = normalize_zones(zones, space["width"], space["height"])
            self._save_world_locked()
            return {"space_id": space["space_id"], "zone_id": zone_id}

    def _shift_space_contents_locked(self, sid: str, dx: int, dy: int, w: int, h: int) -> None:
        space = self._world["spaces"][sid]
        for ent in self._world["entities"].values():
            if ent["space_id"] == sid:
                ent["x"] += dx
                ent["y"] += dy
            portal = ent["traits"].get("portal")
            if portal and portal.get("target_space_id") == sid:
                portal["target_x"] = clamp_int(portal["target_x"] + dx, 0, w - 1, 0)
                portal["target_y"] = clamp_int(portal["target_y"] + dy, 0, h - 1, 0)
        for actor in self._world["actors"].values():
            if actor["space_id"] == sid:
                actor["x"] += dx
                actor["y"] += dy
        for area in (*space["zones"], *space["buildings"]):
            area["x"] += dx
            area["y"] += dy
            if "door_x" in area:
                area["door_x"] += dx
                area["door_y"] += dy

    @staticmethod
    def _trim_axis(start: int, end: int, limit: int, minimum: int, margin: int) -> tuple[int, int]:
        start, end = max(0, start - margin), min(limit, end + margin)
        size = max(minimum, end - start)
        start = max(0, min(limit - size, start - (size - (end - start)) // 2))
        return start, size

    @build_action("裁切外围虚空", inbound=True)
    def trim_space_void(self, space_id: str, margin: int = 0) -> dict[str, Any]:
        with self._lock:
            space = self._build_space_locked(space_id)
            sid, old_w, old_h = space["space_id"], space["width"], space["height"]
            tiles = space["tiles"]
            points = [
                (x, y) for y in range(old_h) for x in range(old_w)
                if tiles.get(f"{x},{y}") != "void"
            ]
            for ent in self._world["entities"].values():
                if ent["space_id"] == sid:
                    points.extend(self._entity_cells(ent) if ent["z"] >= 0 else [(ent["x"], ent["y"])])
            points.extend((a["x"], a["y"]) for a in self._world["actors"].values() if a["space_id"] == sid)
            for area in (*space["zones"], *space["buildings"]):
                x, y = area["x"], area["y"]
                points.extend(((x, y), (x + area["w"] - 1, y + area["h"] - 1)))
                if "door_x" in area:
                    points.append((area["door_x"], area["door_y"]))
            points = [(x, y) for x, y in points if 0 <= x < old_w and 0 <= y < old_h]
            xs, ys = zip(*(points or [(old_w // 2, old_h // 2)]))
            pad = clamp_int(margin, 0, max(old_w, old_h), 0)
            ox, w = self._trim_axis(min(xs), max(xs) + 1, old_w, MIN_SPACE_WIDTH, pad)
            oy, h = self._trim_axis(min(ys), max(ys) + 1, old_h, MIN_SPACE_HEIGHT, pad)
            trimmed = (w, h) != (old_w, old_h)
            if trimmed:
                for field in ("tiles", *SPACE_RELIEF_FIELDS):
                    shifted = {}
                    for key, value in space[field].items():
                        x, y = map(int, key.split(","))
                        if ox <= x < ox + w and oy <= y < oy + h:
                            shifted[f"{x - ox},{y - oy}"] = value
                    space[field] = shifted
                implicit_walls = max(0, min(h, space["wall_rows"] - oy))
                wall_rows = min(implicit_walls, max(1, h // 3))
                for y in range(wall_rows, implicit_walls):
                    for x in range(w):
                        space["tiles"].setdefault(f"{x},{y}", "wood_wall")
                self._shift_space_contents_locked(sid, -ox, -oy, w, h)
                space.update(width=w, height=h, wall_rows=wall_rows)
                self._save_world_locked()
            return {"space_id": sid, "trimmed": trimmed, "width": w, "height": h, "offset": [ox, oy]}

    def _portal_landing_locked(
        self, space: Mapping[str, Any], portal: Mapping[str, Any], *, allow_actor: bool = False,
    ) -> tuple[int, int] | None:
        footprint = set(self._entity_cells(portal))
        actors = {
            (a["x"], a["y"]): aid for aid, a in self._world["actors"].items()
            if a["space_id"] == space["space_id"]
        } if allow_actor else {}
        fallback = None
        for x, y in sorted(footprint):
            for dx, dy in ((0, 1), (0, -1), (1, 0), (-1, 0)):
                nx, ny = x + dx, y + dy
                if (nx, ny) in footprint or not self.can_step_elevation(space, x, y, nx, ny):
                    continue
                occupant = actors.get((nx, ny))
                if self._is_floor_walkable_locked(space, nx, ny, ignore_actor_id=occupant):
                    if not occupant:
                        return nx, ny
                    fallback = fallback or (nx, ny)
        return fallback

    def _portal_blueprint_locked(self, blueprint_id: str, sid: str) -> dict[str, Any] | None:
        bp = self._lookup_blueprint_locked(blueprint_id, sid)
        if bp is None:
            fallback = {
                "wood_door": "default_door", "interior_door": "default_door",
                "staircase_up": "default_staircase", "staircase_down": "default_staircase",
            }.get(blueprint_id, "")
            bp = self._lookup_blueprint_locked(fallback, sid) if fallback else None
        return bp if bp and "portal" in bp["traits"] else None

    def _portal_placement_locked(self, bp: Mapping[str, Any], sid: str, x: int, y: int) -> tuple[str, int, int, int]:
        placement = "floor" if "stair" in bp["tags"] else bp["placement"]
        result = self._resolve_placement_locked(sid, x, y, bp["footprint"], placement, bp["solid"])
        if result[3] != 0:
            raise ValueError("请将门或楼梯放在地面或墙面。")
        return result

    def _find_return_portal_cell_locked(
        self, space: Mapping[str, Any], bp: Mapping[str, Any], x: int, y: int,
    ) -> tuple[tuple[str, int, int, int], tuple[int, int]]:
        sid, w, h = space["space_id"], space["width"], space["height"]

        def score(cell: tuple[int, int]) -> tuple[int, int, int, int]:
            cx, cy = cell
            return abs(cx - x) + abs(cy - y), int(self.is_wall_cell(space, cx, cy)), cy, cx

        cells = sorted(((cx, cy) for cy in range(h) for cx in range(w)), key=score)
        for cx, cy in cells:
            if not self.get_cell_tile(space, cx, cy)["walkable"] and not self.is_wall_cell(space, cx, cy):
                continue
            try:
                location = self._portal_placement_locked(bp, sid, cx, cy)
            except ValueError:
                continue
            landing = self._portal_landing_locked(space, {**bp, "x": cx, "y": cy})
            if landing is not None:
                return location, landing
        raise ValueError("目标空间没有可摆放回程门或楼梯的位置。")

    def link_portal_bidirectional(
        self, space_id: str = "", *, entity_id: str = "", blueprint_id: str = "", x: int = 0, y: int = 1,
        mode: str = "existing", target_space_id: str = "", new_space_name: str = "",
        width: int = 12, height: int = 9, palette_id: str = "", floor_index: int = 2,
        target_x: int | None = None, target_y: int | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            entities, spaces = self._world["entities"], self._world["spaces"]
            origin = entities.get(entity_id) if entity_id else None
            if entity_id and (not origin or origin["z"] != 0 or (space_id and origin["space_id"] != space_id)
                              or "portal" not in origin["traits"]):
                raise ValueError("请选中当前空间内可连接的门或楼梯。")
            source = self._build_space_locked(origin["space_id"] if origin else space_id)
            sid = source["space_id"]
            sx, sy = (origin["x"], origin["y"]) if origin else (int(x), int(y))
            if mode not in ("existing", "new_room", "new_floor"):
                raise ValueError(f"不支持的空间连接方式：{mode}")
            group = source["building_group_id"] or sid
            floor = clamp_int(floor_index, -2, 5, 2)
            parent_sid = sid
            floor_label = ""
            if mode == "new_floor":
                if source["space_kind"] not in ("room", "building") or floor in (0, source["floor_index"]):
                    raise ValueError("请选择房间或建筑中与当前楼层不同的楼层。")
                grouped = [sp for sp in spaces.values() if sp["building_group_id"] == group]
                if any(sp["floor_index"] == floor for sp in grouped):
                    raise ValueError("此楼层已经存在，请选择连接已有空间。")
                parent_sid = next((sp["space_id"] for sp in grouped if sp["floor_index"] == 1), sid)
                floor_label = f"B{-floor}" if floor < 0 else f"{floor}F"

            create_target = mode != "existing"
            name = clip_text(new_space_name, 16, (f"{source['name']} {floor_label}" if floor_label else "新房间"))
            tid = self._available_space_id_locked(name) if create_target else str(target_space_id or "").strip()
            if tid == sid or (not create_target and tid not in spaces):
                raise ValueError("请选择另一个可连接的空间。")

            default_bp = "default_staircase" if mode == "new_floor" else "default_door"
            bp = self._portal_blueprint_locked(blueprint_id or default_bp, sid) if not origin else None
            if not origin and bp is None:
                raise ValueError("请选择带传送功能的门或楼梯蓝图。")
            source_portal = origin if origin else {**bp, "x": sx, "y": sy}
            source_landing = self._portal_landing_locked(source, source_portal, allow_actor=True)
            if source_landing is None:
                raise ValueError("来程门或楼梯旁需要留一格可通行的落脚点。")
            source_location = self._portal_placement_locked(bp, sid, sx, sy) if bp else None
            original_traits = source_portal["traits"]["portal"]
            prior = entities.get(original_traits.get("paired_entity_id", "")) if origin else None
            prior_traits = (prior or {}).get("traits", {}).get("portal", {})
            if origin and prior_traits.get("paired_entity_id") != entity_id:
                prior = None
            affected = {tid} | ({prior["space_id"]} if prior else set())

            with self._build_mutation_locked("连接双向门或楼层", sid, affected):
                if create_target:
                    pal = palette_id or source["palette_id"]
                    target = normalize_space({
                        "space_id": tid, "name": name, "width": width, "height": height,
                        "palette_id": pal if pal in self.all_palettes() else source["palette_id"],
                        "parent_space_id": parent_sid, "district": source["district"],
                        "space_kind": "building" if floor_label else "room",
                        "floor_index": floor if floor_label else 1, "floor_label": floor_label,
                        "building_group_id": group if floor_label else "",
                        "ambient_phrase": f"一间叫作「{name}」的小空间。",
                    })
                    spaces[tid] = target
                    if floor_label:
                        source.update(space_kind="building", building_group_id=group)
                else:
                    target = spaces[tid]
                if ((self._is_isolated_persona_realm_locked(sid) or self._is_isolated_persona_realm_locked(tid))
                        and self._realm_space_id_locked(sid) != self._realm_space_id_locked(tid)):
                    raise ValueError("这扇门无法通往其他平行世界。")

                return_ent = prior if prior and prior["space_id"] == tid else None
                if return_ent is not None:
                    landing = self._portal_landing_locked(target, return_ent, allow_actor=True)
                    if landing is None:
                        raise ValueError("回程门或楼梯旁需要留一格可通行的落脚点。")
                else:
                    fallback = "default_staircase" if "stair" in source_portal["tags"] else "default_door"
                    return_bp = (
                        self._portal_blueprint_locked(source_portal["blueprint_id"], tid)
                        or self._portal_blueprint_locked(fallback, tid)
                    )
                    if return_bp is None:
                        raise ValueError("找不到可用的回程门或楼梯蓝图。")
                    location, landing = self._find_return_portal_cell_locked(
                        target, return_bp, sx if target_x is None else int(target_x), sy if target_y is None else int(target_y),
                    )
                    return_ent = self._instantiate_blueprint_locked(return_bp, tid, *location)
                    entities[return_ent["entity_id"]] = return_ent
                if origin is None:
                    origin = self._instantiate_blueprint_locked(bp, sid, *source_location)
                    entities[origin["entity_id"]] = origin
                if prior is not None and prior is not return_ent:
                    prior_traits.pop("paired_entity_id", None)
                    prior_traits["target_space_id"] = ""

                for ent, peer, destination, (tx, ty) in (
                    (origin, return_ent, tid, landing), (return_ent, origin, sid, source_landing),
                ):
                    ent["traits"]["portal"].update(
                        target_space_id=destination, target_x=tx, target_y=ty, paired_entity_id=peer["entity_id"],
                    )
                self._append_event_locked(
                    "portal_linked", space_id=sid,
                    payload={"entity_id": origin["entity_id"], "target_space_id": tid, "return_entity_id": return_ent["entity_id"]},
                )
                self._save_world_locked()
                return {
                    "space_id": sid, "target_space_id": tid, "created_space": create_target,
                    "entity_id": origin["entity_id"], "return_entity_id": return_ent["entity_id"],
                }

    @staticmethod
    def _normalize_build_stamp(raw: Mapping[str, Any], stamp_id: str = "") -> dict[str, Any]:
        w = clamp_int(raw.get("width", raw.get("w")), 1, MAX_SPACE_WIDTH, MAX_SPACE_WIDTH)
        h = clamp_int(raw.get("height", raw.get("h")), 1, MAX_SPACE_HEIGHT, MAX_SPACE_HEIGHT)
        tiles = normalize_space_tiles(raw.get("tiles"), w, h)
        relief = {
            "elevations": normalize_space_elevations(raw.get("elevations"), w, h),
            "stairs": normalize_space_stairs(raw.get("stairs"), w, h),
            "cliff_styles": normalize_space_cliff_styles(raw.get("cliff_styles"), w, h),
            "stair_styles": normalize_space_stair_styles(raw.get("stair_styles"), w, h),
        }
        entries: list[dict[str, Any]] = []
        raw_entities = raw.get("entities")
        if isinstance(raw_entities, Sequence):
            for item in raw_entities[:w * h * 2]:
                if not isinstance(item, Mapping):
                    continue
                dx = clamp_int(item.get("x", item.get("dx")), -1, w, -1)
                dy = clamp_int(item.get("y", item.get("dy")), -1, h, -1)
                bid = str(item.get("blueprint_id") or "").strip()
                if bid and 0 <= dx < w and 0 <= dy < h:
                    entry = {"blueprint_id": bid, "x": dx, "y": dy, "footprint": normalize_footprint(item.get("footprint"))}
                    if "z" in item:
                        entry["z"] = clamp_int(item["z"], 0, 1, 0)
                    entries.append(entry)
        keys = set(tiles).union(*relief.values())
        coordinates = [tuple(map(int, key.split(","))) for key in keys]
        if "width" not in raw and "w" not in raw:
            w = max([1, *(x + 1 for x, _ in coordinates), *(e["x"] + e["footprint"][0] for e in entries)])
        if "height" not in raw and "h" not in raw:
            h = max([1, *(y + 1 for _, y in coordinates), *(e["y"] + e["footprint"][1] for e in entries)])
        return {
            "stamp_id": stamp_id, "name": clip_text(raw.get("name"), 32, "未命名印章"),
            "width": min(w, MAX_SPACE_WIDTH), "height": min(h, MAX_SPACE_HEIGHT),
            "tiles": tiles, **relief, "entities": entries,
        }

    def save_build_stamp(self, raw_stamp: Mapping[str, Any]) -> dict[str, Any]:
        with self._lock:
            stamp_id = str(raw_stamp.get("stamp_id") or "").strip() or f"stamp_{uuid4().hex[:12]}"
            stamp = self._normalize_build_stamp(raw_stamp, stamp_id)
            if not any(stamp[field] for field in ("tiles", *SPACE_RELIEF_FIELDS, "entities")):
                raise ValueError("印章里没有可保存的地形或物品。")
            updated = {**self._stamps, stamp_id: stamp}
            _atomic_write_json(self._stamps_path, updated)
            self._stamps = updated
            return copy.deepcopy(stamp)

    def delete_build_stamp(self, stamp_id: str) -> dict[str, Any]:
        with self._lock:
            if stamp_id not in self._stamps:
                raise ValueError(f"印章不存在：{stamp_id}")
            updated = {sid: stamp for sid, stamp in self._stamps.items() if sid != stamp_id}
            _atomic_write_json(self._stamps_path, updated)
            self._stamps = updated
            return {"stamp_id": stamp_id}

    @build_action("粘贴组合印章")
    def paste_build_stamp(self, space_id: str, x: int, y: int, stamp: Mapping[str, Any] | str) -> dict[str, Any]:
        with self._lock:
            space = self._build_space_locked(space_id)
            sid, w, h = space["space_id"], space["width"], space["height"]
            if isinstance(stamp, str):
                saved = self._stamps.get(stamp)
                if saved is None:
                    raise ValueError(f"印章不存在：{stamp}")
                stamp = saved
            stamp = self._normalize_build_stamp(stamp)
            x, y = int(x), int(y)
            actors = {(a["x"], a["y"]) for a in self._world["actors"].values() if a["space_id"] == sid}
            occupied = {
                point for ent in self._world["entities"].values() if ent["space_id"] == sid and ent["z"] >= 0
                for point in self._entity_cells(ent)
            }
            blocked, footprints = self._relief_occupancy_locked(sid)
            painted = 0
            tiles = space["tiles"]
            elevations, stairs, cliffs, stair_materials = (space[field] for field in SPACE_RELIEF_FIELDS)
            stamp_tiles = stamp["tiles"]
            stamp_levels, stamp_stairs, stamp_cliffs, stamp_stair_materials = (stamp[field] for field in SPACE_RELIEF_FIELDS)
            for key in dict.fromkeys(key for field in ("tiles", *SPACE_RELIEF_FIELDS) for key in stamp[field]):
                dx, dy = map(int, key.split(","))
                cx, cy = x + dx, y + dy
                if not (0 <= cx < w and 0 <= cy < h):
                    continue
                dest = f"{cx},{cy}"
                point = (cx, cy)
                tid = stamp_tiles.get(key)
                if tid:
                    tile = TILE_CATALOG[tid]
                    if (not tile["walkable"] and point in actors) or (
                        point in occupied and (tid == "void" or tile["is_wall"] != self.is_wall_cell(space, cx, cy)
                                               or (not tile["walkable"] and not tile["is_wall"] and point in blocked))
                    ):
                        continue
                    if tiles.get(dest) != tid:
                        tiles[dest] = tid
                        painted += 1
                if self.is_wall_cell(space, cx, cy) or tiles.get(dest) == "void":
                    if tid:
                        for field in SPACE_RELIEF_FIELDS:
                            space[field].pop(dest, None)
                    continue
                if point not in footprints and (key in stamp_levels or tid):
                    self._set_relief_cell(elevations, dest, stamp_levels.get(key))
                if tid or key in stamp_levels or key in stamp_cliffs:
                    self._set_relief_cell(cliffs, dest, stamp_cliffs.get(key) if dest in elevations else "")
                if key in stamp_stairs and point not in blocked:
                    stairs[dest] = stamp_stairs[key]
                elif tid:
                    stairs.pop(dest, None)
                if tid or key in stamp_stairs or key in stamp_stair_materials:
                    self._set_relief_cell(stair_materials, dest, stamp_stair_materials.get(key) if dest in stairs else "")

            placed = 0
            skipped = 0
            for entry in sorted(stamp["entities"], key=lambda item: item.get("z", 0)):
                bp = self._lookup_blueprint_locked(entry["blueprint_id"], sid)
                try:
                    if not bp:
                        raise ValueError("蓝图已不存在。")
                    location = self._resolve_placement_locked(
                        sid, x + entry["x"], y + entry["y"], bp["footprint"], bp["placement"], bp["solid"],
                    )
                    if "z" in entry and entry["z"] != location[3]:
                        raise ValueError("该笔刷物品的摆放层不匹配。")
                except ValueError:
                    skipped += 1
                    continue
                ent = self._instantiate_blueprint_locked(bp, sid, *location)
                self._world["entities"][ent["entity_id"]] = ent
                placed += 1
            self._save_world_locked()
            return {"space_id": sid, "pasted_tiles": painted, "pasted_entities": placed, "skipped_entities": skipped}

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

        if add_w or add_n:
            for field in SPACE_RELIEF_FIELDS:
                shifted = {}
                for key, value in space[field].items():
                    x, y = map(int, key.split(","))
                    shifted[f"{x + add_w},{y + add_n}"] = value
                space[field] = shifted

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

        if add_w or add_n:
            self._shift_space_contents_locked(sid, add_w, add_n, new_w, new_h)

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

    @build_action("扩展地图", inbound=True)
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

    def set_space_terrain_relief(
        self,
        space_id: str,
        elevations: Mapping[str, int] | None = None,
        stairs: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """替换空间的台地高度与台阶方向，并保留仍有效的材质。"""
        with self._lock:
            sid = str(space_id or self._world["active_space_id"]).strip()
            space = self._world["spaces"].get(sid)
            if not space:
                raise ValueError(f"空间不存在：{sid}")
            w = int(space["width"])
            h = int(space["height"])
            if elevations is not None:
                space["elevations"] = normalize_space_elevations(elevations, w, h)
                space["cliff_styles"] = {key: style for key, style in space["cliff_styles"].items()
                                         if key in space["elevations"]}
            if stairs is not None:
                space["stairs"] = normalize_space_stairs(stairs, w, h)
                space["stair_styles"] = {key: style for key, style in space["stair_styles"].items()
                                         if key in space["stairs"]}
            self._save_world_locked()
            return {
                "space_id": sid,
                "elevated_cells": len(space.get("elevations") or {}),
                "stair_cells": len(space.get("stairs") or {}),
            }

    @build_action("涂刷地形", inbound=True)
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
                return {"space_id": sid, "painted": 0, "removed": [], "expanded": [], "skipped_actor_cells": []}

            expanded_dirs: list[str] = []
            removed_eids: list[str] = []
            skipped_actor_cells: list[list[int]] = []
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
                        if key in tiles and tiles[key] != "void":
                            tiles.pop(key, None)
                            reset_count += 1
                        elif key not in tiles and ty < wall_rows:
                            tiles[key] = "wood_floor"
                            reset_count += 1
                self._save_world_locked()
                return {
                    "space_id": sid,
                    "painted": reset_count,
                    "removed": removed_eids,
                    "expanded": expanded_dirs,
                    "skipped_actor_cells": skipped_actor_cells,
                }

            expand_ops = [(tx, ty, tid) for tx, ty, tid in raw_ops if tid not in ("", "erase", "default", "void")]
            if auto_expand and expand_ops:
                if any(tx <= 0 for tx, _, _ in expand_ops) and int(space["width"]) < MAX_SPACE_WIDTH:
                    _, dw, _ = self._expand_space_locked(sid, "west", 4)
                    raw_ops = [(tx + dw, ty, tid) for tx, ty, tid in raw_ops]
                    expand_ops = [(tx + dw, ty, tid) for tx, ty, tid in expand_ops]
                    expanded_dirs.append("west")
                if any(tx >= int(space["width"]) - 1 for tx, _, _ in expand_ops) and int(space["width"]) < MAX_SPACE_WIDTH:
                    self._expand_space_locked(sid, "east", 4)
                    expanded_dirs.append("east")
                if any(ty <= 0 for _, ty, _ in expand_ops) and int(space["height"]) < MAX_SPACE_HEIGHT:
                    _, _, dn = self._expand_space_locked(sid, "north", 4)
                    raw_ops = [(tx, ty + dn, tid) for tx, ty, tid in raw_ops]
                    expand_ops = [(tx, ty + dn, tid) for tx, ty, tid in expand_ops]
                    expanded_dirs.append("north")
                if any(ty >= int(space["height"]) - 1 for _, ty, _ in expand_ops) and int(space["height"]) < MAX_SPACE_HEIGHT:
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
                if tid == "void":
                    actor_here = any(
                        act.get("space_id") == sid
                        and int(act.get("x", -1)) == tx
                        and int(act.get("y", -1)) == ty
                        for act in self._world["actors"].values()
                    )
                    if actor_here:
                        skipped_actor_cells.append([tx, ty])
                        continue
                    hit_ids = [
                        ent["entity_id"]
                        for ent in list(self._world["entities"].values())
                        if ent.get("space_id") == sid
                        and int(ent.get("z", 0)) >= 0
                        and (tx, ty) in self._entity_cells(ent)
                    ]
                    for eid in hit_ids:
                        if eid in self._world["entities"]:
                            res = self.remove_entity(eid)
                            removed_eids.extend(res.get("removed") or [])
                    for field in SPACE_RELIEF_FIELDS:
                        space[field].pop(key, None)
                    if tiles.get(key) != "void":
                        tiles[key] = "void"
                        changed += 1
                elif tid in ("", "erase", "default"):
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
                "skipped_actor_cells": skipped_actor_cells,
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

            all_bps = self.all_blueprints(include_ephemeral=False)

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
            if bp["ephemeral_realm_id"]:
                raise ValueError("临时蓝图须在所属空间内生成。")
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
        if posture == "resting":
            desired_bucket = "sleep_deep"
            hint_raw = ""
        elif hint_raw:
            lowered = hint_raw.lower()
            if lowered in _EMOTION_HINT_ALIASES:
                desired_bucket = _EMOTION_HINT_ALIASES[lowered]
            else:
                desired_bucket = classify_emotion_bucket(hint_raw)
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

        if posture != "resting":
            pack = [item for item in pack if item.get("bucket") != "sleep_deep"]
            if desired_bucket == "sleep_deep":
                desired_bucket = "sleepy"
                hint_raw = ""
        if not pack:
            return desired_bucket, None

        if hint_raw:
            for item in pack:
                if hint_raw in str(item.get("tag") or ""):
                    return desired_bucket, item

        fallback_order = {
            "sleep_deep": ("sleep_deep", "sleepy", "sad", "neutral"),
            "sleepy": ("sleepy", "neutral"),
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
                "tag": bucket_labels[bucket] if bucket in ("sleep_deep", "sleepy") else str(emotion_hint or bucket_labels.get(bucket, "平静")),
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
                    if not same_realm and item.get("ephemeral_realm_id"):
                        item.update(parent_id=f"space:{old_sid}", x=actor["x"], y=actor["y"], z=0)
                        item.pop("inventory_slot", None)
                        if actor.get("holding_id") == item["entity_id"]:
                            actor["holding_id"] = None
                    else:
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

    def _seed_familiar_realm_locked(self, actor: dict[str, Any], realm_sid: str) -> None:
        """为本镇常驻居民预填对小镇各建筑与通路的熟悉认知，避免表现为刚入镇的访客。"""
        if not realm_sid:
            return
        cog = actor.setdefault("autonomy", {}).setdefault("cognition", {})
        if not isinstance(cog, dict):
            return
        places = cog.setdefault("places", {})
        portals = cog.setdefault("portals", {})
        realm_sids = {
            sid for sid in self._world["spaces"]
            if self._realm_space_id_locked(sid) == realm_sid
        }
        for sid in realm_sids:
            sp = self._world["spaces"][sid]
            cur = places.get(sid) if isinstance(places.get(sid), dict) else {}
            visits = max(3, int(cur.get("visits") or 0))
            places[sid] = {
                **cur,
                "name": str(sp.get("name") or sid),
                "kind": str(sp.get("space_kind") or "building"),
                "source": "direct",
                "visits": visits,
                "samples": list(cur.get("samples") or [0.0]),
            }
        for ent in self._world["entities"].values():
            sp_id = str(ent.get("space_id") or "")
            if sp_id not in realm_sids:
                continue
            pt = (ent.get("traits") or {}).get("portal")
            if not isinstance(pt, Mapping):
                continue
            tgt = str(pt.get("target_space_id") or "").strip()
            if not tgt or tgt not in realm_sids:
                continue
            tgt_sp = self._world["spaces"].get(tgt) or {}
            portals[f"{sp_id}:{tgt}"] = {
                "space": sp_id,
                "target": tgt,
                "name": str(tgt_sp.get("name") or tgt),
                "x": int(ent.get("x") or 0),
                "y": int(ent.get("y") or 0),
                "source": "direct",
                "confidence": 1.0,
                "samples": [0.0],
            }
        cog["last_space"] = str(actor.get("space_id") or realm_sid)

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
        is_native_npc: bool = False,
        native_realm_id: str = "",
        ephemeral_realm_id: str = "",
        actor_status: str = "active",
        status_note: str = "",
        persona_brief: str = "",
        home_space_id: str = "",
        seed_familiar_realm: bool = False,
    ) -> dict[str, Any]:
        with self._lock:
            clean_name = clip_text(name or host_character, 20, "七海千秋")
            if not space_id or space_id not in self._world["spaces"]:
                space_id = self._world["active_space_id"]
            explicit_aid = str(actor_id or "").strip()
            aid = explicit_aid or self._alloc_actor_id_locked(clean_name, host_character, space_id=space_id)
            if ephemeral_realm_id and aid in self._world["actors"] and (
                self._world["actors"][aid].get("ephemeral_realm_id") != ephemeral_realm_id
            ):
                aid = f"{aid[:39]}_{uuid4().hex[:8]}"
            space = self._world["spaces"][space_id]
            tx, ty = self._find_open_floor_cell_locked(space, preferred_x=x, preferred_y=y, ignore_actor_id=aid)

            clean_status = str(actor_status or "active").strip().lower()
            if clean_status not in VALID_ACTOR_STATUSES:
                raise ValueError(f"无效角色状态：{actor_status}")
            resolved_native_realm = (
                str(native_realm_id or "").strip() or self._realm_space_id_locked(space_id)
                if is_native_npc else ""
            )

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
            if ephemeral_realm_id and self._realm_space_id_locked(space_id) != ephemeral_realm_id:
                raise ValueError("临时角色只能创建或更新于所属平行域。")
            if existing is not None:
                existing["name"] = clean_name
                if host_character:
                    existing["host_character"] = host_character
                if is_native_npc:
                    existing["is_native_npc"] = True
                    existing["native_realm_id"] = resolved_native_realm
                if actor_status:
                    existing["actor_status"] = clean_status
                if status_note:
                    existing["status_note"] = clip_text(status_note, 60, "")
                if persona_brief:
                    existing["persona_brief"] = clip_text(persona_brief, 360, "")
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
                init_appearance = dict(appearance) if appearance is not None else None
                init_map_style = map_style
                if not is_native_npc:
                    char_key = str(host_character or clean_name).strip()
                    if init_appearance is None:
                        saved_app, saved_style = self._resolve_saved_character_appearance_locked(char_key)
                        if saved_app is not None:
                            init_appearance = saved_app
                        if not init_map_style and saved_style in MAP_STYLES:
                            init_map_style = saved_style
                    elif not init_map_style:
                        _saved_app, saved_style = self._resolve_saved_character_appearance_locked(char_key)
                        if saved_style in MAP_STYLES:
                            init_map_style = saved_style
                actor = normalize_actor({
                    "actor_id": aid,
                    "host_character": host_character or clean_name,
                    "name": clean_name,
                    "is_native_npc": bool(is_native_npc),
                    "native_realm_id": resolved_native_realm,
                    "ephemeral_realm_id": ephemeral_realm_id,
                    "actor_status": clean_status,
                    "status_note": status_note,
                    "persona_brief": clip_text(persona_brief, 360, ""),
                    "space_id": space_id,
                    "x": tx,
                    "y": ty,
                    "appearance": init_appearance,
                    "map_style": init_map_style or "doll",
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
            if seed_familiar_realm:
                self._seed_familiar_realm_locked(actor, self._realm_space_id_locked(space_id))
            self._sync_actor_world_state_locked(actor)
            clean_home = str(home_space_id or "").strip()
            if clean_home and clean_home in self._world["spaces"]:
                realm_sid = self._realm_space_id_locked(space_id)
                actor.setdefault("home_spaces", {})[realm_sid] = clean_home
            if self._is_isolated_persona_realm_locked(space_id):
                self._isolated_active_actors.setdefault(self._realm_space_id_locked(space_id), aid)
            elif not is_native_npc or self._world.get("active_actor_id") not in self._world["actors"]:
                self._world["active_actor_id"] = aid
            self._append_event_locked("actor_imported", actor_id=aid, space_id=space_id, payload={"name": clean_name, "x": actor["x"], "y": actor["y"], "mode": actor["sprite"]["mode"], "is_native_npc": bool(is_native_npc)})
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

        sid = str(space.get("space_id") or "")
        cx, cy = w // 2, max(wall_rows, h // 2)
        actor_pts = [
            (int(a["x"]), int(a["y"]))
            for aid_a, a in self._world["actors"].items()
            if aid_a != ignore_actor_id and a.get("space_id") == sid and a.get("actor_status", "active") != "missing"
        ]
        portal_pts = [
            cell
            for e in self._world["entities"].values()
            if e.get("space_id") == sid and "portal" in (e.get("traits") or {})
            for cell in self._entity_cells(e)
        ]
        entity_pts = {
            cell
            for e in self._world["entities"].values()
            if e.get("space_id") == sid and int(e.get("z", 0)) == 0 and "portal" not in (e.get("traits") or {})
            for cell in self._entity_cells(e)
        }
        if not actor_pts and not portal_pts and (cx, cy) not in entity_pts and self._is_floor_walkable_locked(space, cx, cy, ignore_actor_id=ignore_actor_id):
            return cx, cy

        target_elev = self.get_cell_elevation(space, cx, cy)
        candidates: list[tuple[tuple[float, int, int, int], tuple[int, int]]] = []
        for yy in range(wall_rows, h):
            for xx in range(w):
                if not self._is_floor_walkable_locked(space, xx, yy, ignore_actor_id=ignore_actor_id):
                    continue
                d_act = min(10, min((abs(xx - ax) + abs(yy - ay) for ax, ay in actor_pts), default=10))
                d_door = min(6, min((max(abs(xx - px), abs(yy - py)) for px, py in portal_pts), default=6))
                door_pen = 30.0 if d_door <= 1 else (8.0 if d_door <= 2 else 0.0)
                entity_pen = 45.0 if (xx, yy) in entity_pts else 0.0
                elev_pen = 22.0 if self.get_cell_elevation(space, xx, yy) != target_elev else 0.0
                stair_pen = 18.0 if self.is_stair_cell(space, xx, yy) else 0.0
                dist_center = abs(xx - cx) + abs(yy - cy)
                score = d_act * 4.0 + d_door * 1.5 - door_pen - entity_pen - elev_pen - stair_pen - dist_center * 0.25
                candidates.append(((-round(score, 3), dist_center, yy, xx), (xx, yy)))
        if candidates:
            candidates.sort(key=lambda row: row[0])
            return candidates[0][1]

        for yy in list(range(wall_rows, h)) + list(range(0, wall_rows)):
            for xx in range(w):
                if self._is_floor_walkable_locked(space, xx, yy, ignore_actor_id=ignore_actor_id):
                    return xx, yy
        return cx, cy

    def _find_placement_cell_locked(
        self,
        space_id: str,
        blueprint: Mapping[str, Any],
        *,
        strategy: str = "spread",
        zone_id: str = "",
    ) -> tuple[int, int]:
        """为无显式坐标的蓝图放置寻找按策略分散、可选限定隔间且避开门洞的可落位格子。"""
        space = self._world["spaces"].get(space_id)
        if space is None:
            raise ValueError(f"空间不存在：{space_id}")
        w = int(space["width"])
        h = int(space["height"])
        wall_rows = int(space["wall_rows"])
        fp = blueprint["footprint"]
        placement = str(blueprint.get("placement") or "any")
        solid = bool(blueprint.get("solid", False))
        strat = str(strategy or "spread").strip().lower()
        center_x = w // 2
        center_y = max(wall_rows, h // 2)

        existing_pts: set[tuple[int, int]] = set()
        surface_top_pts: set[tuple[int, int]] = set()
        surface_cells: list[tuple[int, int]] = []
        solid_cells: set[tuple[int, int]] = set()
        portal_pts: list[tuple[int, int]] = []
        for ent in self._world["entities"].values():
            if ent.get("space_id") != space_id or int(ent.get("z", 0)) < 0:
                continue
            traits = ent.get("traits") or {}
            cells = self._entity_cells(ent)
            if "portal" in traits:
                portal_pts.extend(cells)
                continue
            if int(ent.get("z", 0)) >= 1:
                surface_top_pts.update(cells)
            else:
                existing_pts.update(cells)
            if ent.get("solid"):
                solid_cells.update(cells)
            if "surface" in traits and int(ent.get("z", 0)) == 0:
                surface_cells.extend(cells)
        actor_pts = [
            (int(a["x"]), int(a["y"]))
            for a in self._world["actors"].values()
            if a.get("space_id") == space_id and a.get("actor_status", "active") != "missing"
        ]

        def _door_penalty(cx: int, cy: int) -> float:
            dd = min((max(abs(cx - px), abs(cy - py)) for px, py in portal_pts), default=99)
            return 120.0 if dd <= 1 else (40.0 if dd <= 2 else 0.0)

        def _adj_blocked(cx: int, cy: int) -> int:
            cnt = 0
            for ox, oy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nx, ny = cx + ox, cy + oy
                if not (0 <= nx < w and 0 <= ny < h) or self.is_wall_cell(space, nx, ny) or (nx, ny) in solid_cells:
                    cnt += 1
            return cnt

        is_town = space.get("space_kind") == "town"

        def _score_floor(cx: int, cy: int) -> float:
            d_item = min(14, min((abs(cx - ex) + abs(cy - ey) for ex, ey in existing_pts), default=14))
            d_act = min(10, min((abs(cx - ax) + abs(cy - ay) for ax, ay in actor_pts), default=10))
            dist_center = abs(cx - center_x) + abs(cy - center_y)
            d_center = min(8, dist_center)
            adj = _adj_blocked(cx, cy)
            d_pen = _door_penalty(cx, cy) + (60.0 if self.is_stair_cell(space, cx, cy) else 0.0)
            if is_town:
                d_item_t = min(5, d_item)
                d_act_t = min(5, d_act)
                if dist_center < 3:
                    ring_score = -16.0
                elif dist_center <= 9:
                    ring_score = 24.0 - abs(dist_center - 6) * 2.0
                else:
                    ring_score = 12.0 - (dist_center - 9) * 5.0
                edge_hint = 3.0 if (strat in ("corner", "perimeter") and adj >= 1) else 0.0
                return d_item_t * 2.4 + d_act_t * 0.9 + ring_score + edge_hint - d_pen
            if strat == "corner":
                return (24.0 if adj >= 2 else (8.0 if adj == 1 else 0.0)) + d_item * 3.0 + d_act * 1.5 + d_center * 1.2 - d_pen
            if strat == "perimeter":
                return (18.0 if adj >= 1 else 0.0) + d_item * 3.0 + d_act * 1.5 + d_center * 0.9 - d_pen
            return d_item * 4.0 + d_act * 1.6 + d_center * 0.8 + (1.5 if adj == 1 else 0.0) - d_pen

        wall_cells = sorted(
            ((xx, yy) for yy in range(min(wall_rows, h)) for xx in range(w)),
            key=lambda p: (
                -min(12, min((abs(p[0] - ex) + abs(p[1] - ey) for ex, ey in existing_pts), default=12)),
                _door_penalty(p[0], p[1]),
                p[1],
                p[0],
            ),
        )
        floor_cells = sorted(
            (
                (xx, yy)
                for yy in range(wall_rows, h)
                for xx in range(w)
                if (xx, yy) not in solid_cells
                and not self.is_wall_cell(space, xx, yy)
                and self.get_cell_tile(space, xx, yy).get("walkable", True)
            ),
            key=lambda p: (-round(_score_floor(p[0], p[1]), 3), p[1], p[0]),
        )
        scored_surfaces = sorted(
            dict.fromkeys(surface_cells),
            key=lambda p: (
                p in surface_top_pts,
                -min(10, min((abs(p[0] - sx) + abs(p[1] - sy) for sx, sy in surface_top_pts), default=10)),
                _door_penalty(p[0], p[1]),
                p[1],
                p[0],
            ),
        )

        raw_candidates: list[tuple[int, int]] = []
        if placement == "wall":
            raw_candidates.extend(wall_cells)
        elif placement == "surface_only" or (strat == "surface" and placement == "any" and scored_surfaces):
            raw_candidates.extend(scored_surfaces)
            raw_candidates.extend(floor_cells)
            raw_candidates.extend(wall_cells)
        else:
            raw_candidates.extend(floor_cells)
            if placement == "any":
                raw_candidates.extend(wall_cells)

        clean_zid = str(zone_id or "").strip()
        if clean_zid:
            target_zone = next(
                (
                    z for z in (space.get("zones") or ())
                    if isinstance(z, Mapping) and (str(z.get("zone_id") or "") == clean_zid or str(z.get("name") or "") == clean_zid)
                ),
                None,
            )
            if target_zone is not None:
                zx, zy = int(target_zone.get("x") or 0), int(target_zone.get("y") or 0)
                zw, zh = int(target_zone.get("w") or 0), int(target_zone.get("h") or 0)
                zone_candidates = [pt for pt in raw_candidates if zx <= pt[0] < zx + zw and zy <= pt[1] < zy + zh]
                if zone_candidates:
                    raw_candidates = [*zone_candidates, *raw_candidates]

        seen: set[tuple[int, int]] = set()
        for cx, cy in raw_candidates:
            if (cx, cy) in seen:
                continue
            seen.add((cx, cy))
            try:
                self._resolve_placement_locked(space_id, cx, cy, fp, placement, solid)
                return cx, cy
            except ValueError:
                continue
        raise ValueError(f"空间「{space['name']}」没有可放置「{blueprint['name']}」的空位。")

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

    def _resolve_saved_character_appearance_locked(
        self,
        char_key: str,
    ) -> tuple[dict[str, Any] | None, str]:
        """解析角色已保存的皮肤外观与地图呈现风格（显式预设 -> 同名现有角色 -> 本地像素图包）。"""
        key = str(char_key or "").strip()
        if not key:
            return None, ""
        resolved_app: dict[str, Any] | None = None
        resolved_style = ""

        presets = self._world.get("character_appearances") or {}
        rec = presets.get(key)
        if isinstance(rec, Mapping) and isinstance(rec.get("appearance"), Mapping):
            resolved_app = copy.deepcopy(dict(rec["appearance"]))
            ms = str(rec.get("map_style") or "").strip().lower()
            resolved_style = ms if ms in MAP_STYLES else "doll"

        if resolved_app is None:
            candidates = [
                a for a in (self._world.get("actors") or {}).values()
                if not a.get("is_player")
                and not a.get("is_native_npc")
                and str(a.get("host_character") or a.get("name") or "").strip() == key
            ]
            if candidates:
                best = max(
                    candidates,
                    key=lambda a: (
                        0 if a.get("ephemeral_realm_id") else 1,
                        1 if ((a.get("appearance") or {}).get("custom_frames") or {}).get("down") else 0,
                    ),
                )
                if isinstance(best.get("appearance"), Mapping):
                    resolved_app = copy.deepcopy(dict(best["appearance"]))
                ms = str(best.get("map_style") or "").strip().lower()
                if ms in MAP_STYLES:
                    resolved_style = ms

        has_custom = bool(
            isinstance(resolved_app, Mapping)
            and isinstance(resolved_app.get("custom_frames"), Mapping)
            and resolved_app["custom_frames"].get("down")
        )
        if not has_custom:
            cached_frames = sprite_pack.extract_cached_sprite_frames(self._data_root, key)
            if cached_frames:
                base = copy.deepcopy(resolved_app) if isinstance(resolved_app, Mapping) else default_appearance(key)
                base["custom_frames"] = cached_frames
                resolved_app = base

        return resolved_app, resolved_style

    def has_saved_character_appearance(self, char_key: str) -> bool:
        """检查角色是否已有保存的自定义皮肤、同角色外观或本地像素缓存。"""
        key = str(char_key or "").strip()
        if not key:
            return False
        with self._lock:
            if key in (self._world.get("character_appearances") or {}):
                return True
            for a in (self._world.get("actors") or {}).values():
                if (
                    not a.get("is_player")
                    and not a.get("is_native_npc")
                    and not a.get("ephemeral_realm_id")
                    and str(a.get("host_character") or a.get("name") or "").strip() == key
                    and ((a.get("appearance") or {}).get("custom_frames") or {}).get("down")
                ):
                    return True
        return sprite_pack.get_cached_sprite(self._data_root, key) is not None

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
            if not actor.get("is_player") and not actor.get("is_native_npc"):
                char_key = str(actor.get("host_character") or actor.get("name") or "").strip()
                if char_key:
                    self._world.setdefault("character_appearances", {})[char_key] = {
                        "appearance": copy.deepcopy(actor["appearance"]),
                        "map_style": actor["map_style"],
                    }
                    for sib_id, sib in self._world["actors"].items():
                        if (
                            sib_id == actor_id
                            or sib.get("is_player")
                            or sib.get("is_native_npc")
                        ):
                            continue
                        sib_key = str(sib.get("host_character") or sib.get("name") or "").strip()
                        if sib_key == char_key:
                            sib["appearance"] = copy.deepcopy(actor["appearance"])
                            sib["map_style"] = actor["map_style"]
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
            realm = self._realm_space_id_locked(actor["space_id"])
            if self._is_isolated_persona_realm_locked(realm):
                self._isolated_active_actors[realm] = actor_id
            else:
                self._world["active_actor_id"] = actor_id
                self._save_world_locked()
            return copy.deepcopy(actor)

    def _cleanup_removed_actor_locked(self, aid: str, *, drop_carried: bool = True) -> None:
        act = self._world["actors"].get(aid)
        sid = str((act or {}).get("space_id") or self._world["active_space_id"])
        for item in self._world["entities"].values():
            if drop_carried and act and self._carries_item(act, item):
                item.update(parent_id=f"space:{sid}", space_id=sid, x=act["x"], y=act["y"], z=0)
                item.pop("inventory_slot", None)
            if item["traits"].get("container", {}).get("owner_actor_id") == aid:
                item["traits"]["container"].pop("owner_actor_id")
        self._world["key_loans"] = {
            loan_id: loan for loan_id, loan in self._world["key_loans"].items()
            if aid not in (loan["borrower_id"], loan["lender_id"])
        }
        self._world["gift_offers"] = {
            oid: offer for oid, offer in (self._world.get("gift_offers") or {}).items()
            if aid not in (offer.get("from_id"), offer.get("to_id"))
        }
        for history in self._world["puzzle_memories"].values():
            history[:] = [memory for memory in history if aid not in memory["witness_ids"]]
        for event_id, event in list(self._world["persona_events"].items()):
            if aid in event["pending"]:
                event["pending"].remove(aid)
                if not event["pending"]:
                    del self._world["persona_events"][event_id]
        for by_actor in (self._world.get("external_actor_state") or {}).values():
            if isinstance(by_actor, dict):
                by_actor.pop(aid, None)

    def set_actor_status(
        self,
        actor_id: str,
        status: str,
        *,
        status_note: str = "",
    ) -> dict[str, Any]:
        """更新角色物理生命周期状态：active | incapacitated | missing。"""
        with self._lock:
            aid = str(actor_id or "").strip()
            actor = self._world["actors"].get(aid)
            if actor is None:
                raise ValueError(f"角色不存在：{aid}")
            clean_status = str(status or "").strip().lower()
            if clean_status not in VALID_ACTOR_STATUSES:
                raise ValueError(f"无效角色状态：{status}")
            if actor.get("is_player") and clean_status != "active":
                raise ValueError("主控角色不能设为退场或失踪状态。")
            old_status = str(actor.get("actor_status") or "active")
            actor["actor_status"] = clean_status
            if status_note or clean_status != old_status:
                actor["status_note"] = clip_text(status_note, 60, "")
            if clean_status != "active":
                self._clear_actor_rest_locked(actor)
                visit = self._joint_visit_locked(actor)
                if visit:
                    self._finish_joint_visit_locked(aid, visit["id"], together=True)
            self._sync_actor_world_state_locked(actor)
            self._append_event_locked(
                "actor_status_changed",
                actor_id=aid,
                space_id=str(actor.get("space_id") or ""),
                payload={"old_status": old_status, "status": clean_status, "status_note": actor["status_note"]},
            )
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
            self._cleanup_removed_actor_locked(aid, drop_carried=True)
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
        outcome: str = "", epa: Mapping[str, float] | None = None, need: str = "",
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
        if isinstance(epa, Mapping):
            clean_epa = {k: float(v) for k, v in epa.items() if k in ("e", "p", "a")}
            if clean_epa:
                payload["epa"] = clean_epa
        if need:
            payload["need"] = str(need)
        cognition = actor.get("autonomy", {}).get("cognition") or {}
        impression = cognition.get("semantic", {}).get(payload["entity_id"], {})
        if ok and "epa" not in payload and impression.get("epa"):
            payload["epa"] = impression["epa"]
        event = self._append_event_locked(
            "actor_action_outcome", actor_id=actor["actor_id"], space_id=actor["space_id"], payload=payload,
        )
        if kind:
            self._world["persona_events"][event_id] = {**event, "pending": list(payload["witness_ids"])}
        if ok and kind in ("action_success", "consumed", "discovered", "cooperative_solve", "gift_received"):
            summary = clip_text(message, 80)
            if target.get("name") and action in ("interact", "consume", "unlock"):
                verb = {"interact": "体验了", "consume": "享用了", "unlock": "打开了"}[action]
                summary = f"{verb}「{target['name']}」。"
            self._record_episode_locked(actor, "activity", summary, subject=target)
        return event

    def _record_episode_locked(
        self, actor: Mapping[str, Any], kind: str, summary: str, *, subject: Mapping[str, Any] | None = None,
    ) -> None:
        if actor.get("is_player"):
            return
        memory = actor["autonomy"].setdefault("cognition", {}).setdefault("episodic_memory", {})
        subject = subject or {}
        episodic_memory.record(
            memory, self._world["simulation_minutes"], self._world["spaces"][actor["space_id"]], kind, summary,
            subject_id=subject.get("actor_id") or subject.get("entity_id") or subject.get("zone_id") or "",
            subject_name=subject.get("name") or "",
        )

    def record_player_conversation(self, actor_id: str, space_id: str, text: str) -> None:
        with self._lock:
            actor = self._world["actors"].get(actor_id)
            if actor and not actor.get("is_player") and actor["space_id"] == space_id:
                self._record_episode_locked(
                    actor, "social", text, subject={"actor_id": episodic_memory.PLAYER_SUBJECT_ID, "name": "玩家"},
                )
                self._sync_actor_world_state_locked(actor)
                self._save_world_locked()

    def record_social_feedback(
        self, actor_id: str, space_id: str, message: str, *, peer_id: str = "", mood_delta: float = 0.0,
        spoken: bool = True, speech_act: Mapping[str, Any] | None = None, decision_interval: float = 5.0,
    ) -> bool:
        """Capture the exchange and report whether a new rendezvous is pending."""
        with self._lock:
            actors = self._world["actors"]
            actor = actors.get(actor_id)
            if actor is None or actor["space_id"] != space_id:
                return False
            joint_waiting = False
            if peer_id:
                peer = actors.get(peer_id)
                if peer is None or peer_id == actor_id or peer["space_id"] != space_id:
                    return False
                event = self._queue_persona_event_locked(actor, "social_gesture", None, "talk", message, peer=peer,
                                                         mood_delta=mood_delta)
                realm = self._realm_space_id_locked(space_id)
                has_sa = isinstance(speech_act, Mapping) and bool(speech_act)
                split_sa = has_sa and ("a" in speech_act or "b" in speech_act)
                raw_sides = (speech_act.get("a"), speech_act.get("b")) if split_sa else (speech_act, speech_act)
                sides = [copy.deepcopy(dict(raw)) if isinstance(raw, Mapping) else {} for raw in raw_sides]
                joint_id = self._agree_joint_visit_locked(actor, peer, realm, sides, event["seq"], decision_interval)
                joint_waiting = bool(joint_id and not actor["autonomy"]["cognition"]["joint_visit"].get("met"))
                for (receiver, partner), side_sa in zip(((actor, peer), (peer, actor)), sides):
                    if self._is_multi_space_realm_locked(realm) or "cognition" in receiver["autonomy"] or side_sa:
                        cognition = receiver["autonomy"].setdefault("cognition", {})
                        sig: dict[str, Any] = {
                            "seq": event["seq"], "minute": self._world["simulation_minutes"],
                            "actor": partner["actor_id"], "name": partner["name"], "mood_delta": mood_delta, "spoken": spoken,
                        }
                        if side_sa:
                            sig["speech_act"] = side_sa
                            if joint_id:
                                sig["joint_visit_id"] = joint_id
                            if joint_id or side_sa.get("shared_place_id") or side_sa.get("personal_goal") or side_sa.get("tone") == "tense":
                                cognition["preempt_seq"] = int(cognition.get("preempt_seq", 0)) + 1
                        cognition["social_signal"] = sig
                    topic = (side_sa or {}).get("summary")
                    summary = f"和{partner['name']}聊了聊：{topic}" if topic else message
                    self._record_episode_locked(receiver, "social", summary, subject=partner)
                    self._sync_actor_world_state_locked(receiver)
            else:
                if any(person["space_id"] == space_id and aid != actor_id for aid, person in actors.items()):
                    return False
                self._queue_persona_event_locked(actor, "alone_tick", None, "idle", message)
            self._save_world_locked()
            return joint_waiting

    def _agree_joint_visit_locked(
        self, actor: dict[str, Any], peer: dict[str, Any], realm: str,
        sides: list[dict[str, Any]], seq: int, decision_interval: float,
    ) -> str:
        if actor.get("is_player") or peer.get("is_player"):
            return ""
        for person in (actor, peer):
            cognition = person["autonomy"].get("cognition") or {}
            if any((cognition.get(key) or {}).get("source") == "persona_choice"
                   for key in ("active_intention", "suspended_intention", "pending_modulation")):
                return ""
        entity_id = str(sides[0].get("shared_entity_id") or "")
        entity = self._joint_entity_locked(entity_id, actor["space_id"])
        if entity_id and (not entity or sides[1].get("shared_entity_id") != entity_id):
            return ""
        target = actor["space_id"] if entity else str(sides[0].get("shared_place_id") or "")
        space = self._world["spaces"].get(target)
        if not entity and (
            not space or sides[1].get("shared_place_id") != target or self._realm_space_id_locked(target) != realm
            or not self._is_multi_space_realm_locked(realm)
        ):
            return ""
        building = next(
            (b for b in self._world["spaces"][realm]["buildings"] if b["interior_space_id"] == target), None,
        )
        for person, side in zip((actor, peer), sides):
            goal = side.get("personal_goal")
            if isinstance(goal, Mapping) and goal.get("kind") == "visit" and (
                goal.get("target") != target or goal.get("action") == "escort"
                or (goal.get("drive") == "rest" and (
                    self._resolve_actor_home_locked(person, realm) == target
                    or venue_semantics.is_lodging_building(building, name=space["name"])
                ))
            ):
                return ""
        current = self._joint_visit_locked(actor)
        if (current and current["peer_id"] == peer["actor_id"] and current["target"] == target
                and current.get("entity_id", "") == entity_id):
            return current["id"]
        previous = (actor["autonomy"].get("cognition") or {}).get("joint_visit") or {}
        counterpart = (peer["autonomy"].get("cognition") or {}).get("joint_visit") or {}
        if (
            previous.get("met") and previous.get("target") == actor["space_id"] == target
            and previous["id"] == counterpart.get("id")
            and previous.get("entity_id", "") == entity_id
            and self._world["simulation_minutes"] < previous["expires_at"]
        ):
            return previous["id"]

        drive = str(sides[0].get("shared_drive") or "")
        if drive not in _MODULATION_DOMAINS or sides[1].get("shared_drive") != drive:
            drive = "social"
        agreement = {
            "id": f"sa:{seq}", "target": target, "drive": drive,
            "expires_at": self._world["simulation_minutes"] + max(10.0, 10 * decision_interval),
        }
        if entity:
            agreement["entity_id"] = entity_id
            agreement["entity_name"] = entity["name"]
            if "restable" in entity["traits"] and all(side.get("shared_action") == "rest" for side in sides):
                sitter = actor if sides[0].get("seated") else peer
                agreement.update(drive="rest", seated_id=sitter["actor_id"])
        for person, partner in ((actor, peer), (peer, actor)):
            cognition = person["autonomy"].setdefault("cognition", {})
            cognition["joint_visit"] = {**agreement, "peer_id": partner["actor_id"], "peer_name": partner["name"]}
        return agreement["id"]

    def _joint_entity_locked(self, entity_id: str, space_id: str) -> dict[str, Any] | None:
        entity = self._world["entities"].get(entity_id)
        if (entity and entity["space_id"] == space_id and entity["z"] >= 0
                and entity["parent_id"].startswith(("space:", "entity:")) and "portal" not in entity["traits"]):
            return entity
        return None

    def _joint_visit_locked(self, actor: Mapping[str, Any]) -> dict[str, Any] | None:
        visit = (actor["autonomy"].get("cognition") or {}).get("joint_visit")
        if not visit or self._world["simulation_minutes"] >= visit["expires_at"]:
            return None
        peer = self._world["actors"].get(visit["peer_id"])
        peer_visit = ((peer["autonomy"].get("cognition") or {}).get("joint_visit") or {}) if peer else {}
        realm = self._realm_space_id_locked(actor["space_id"])
        if (peer_visit.get("id") != visit["id"] or (visit.get("done") and peer_visit.get("done"))
                or visit["target"] not in self._world["spaces"] or self._realm_space_id_locked(visit["target"]) != realm
                or self._realm_space_id_locked(peer["space_id"]) != realm):
            return None
        if visit.get("entity_id") and not self._joint_entity_locked(visit["entity_id"], visit["target"]):
            return None
        return visit

    def recent_joint_entity(self, actor_id: str, peer_id: str) -> str:
        with self._lock:
            actors = self._world["actors"]
            actor, peer = actors.get(actor_id), actors.get(peer_id)
            if not actor or not peer or actor["space_id"] != peer["space_id"]:
                return ""
            joint = (actor["autonomy"].get("cognition") or {}).get("joint_visit") or {}
            other = (peer["autonomy"].get("cognition") or {}).get("joint_visit") or {}
            entity_id = joint.get("entity_id") or ""
            if (not entity_id or joint.get("id") != other.get("id") or joint["target"] != actor["space_id"]
                    or not self._joint_entity_locked(entity_id, joint["target"])):
                return ""
            if joint.get("met") and self._world["simulation_minutes"] < joint["expires_at"] + 2:
                return entity_id
            if self._joint_visit_locked(actor):
                return entity_id
            return ""

    def confirm_joint_visit(self, actor_id: str, visit_id: str, *, activity_minutes: float) -> bool:
        """Confirm physical proximity and release both participants to their activity."""
        with self._lock:
            actor = self._world["actors"].get(actor_id)
            visit = self._joint_visit_locked(actor) if actor else None
            if not visit or visit.get("done") or visit["id"] != visit_id or actor["space_id"] != visit["target"]:
                return False
            if visit.get("met"):
                return True
            peer = self._world["actors"][visit["peer_id"]]
            entity = self._joint_entity_locked(visit.get("entity_id", ""), visit["target"])
            participants = (actor, peer)
            sitter_id = visit.get("seated_id")
            if any(person["posture"] == "resting" or person["vitals"]["disengaged"]
                   or (person["actor_id"] != sitter_id and
                       (person["vitals"]["energy"] < 0.25 or person["vitals"]["sleep_pressure"] >= 0.85))
                   for person in participants):
                return False
            if entity:
                if any(self.chebyshev_distance(person, entity) > 1 for person in participants):
                    return False
                if (actor["x"], actor["y"]) == (peer["x"], peer["y"]):
                    return False
                if sitter_id and any(person["actor_id"] != sitter_id
                                     and (person["x"], person["y"]) == (entity["x"], entity["y"])
                                     for person in participants):
                    return False
            elif self.chebyshev_distance(actor, peer) > 1 or peer["actor_id"] not in self._visible_peer_ids_locked(actor):
                return False
            place = f"{entity['name']}旁" if entity else self._world["spaces"][visit["target"]]["name"]
            for person, partner in ((actor, peer), (peer, actor)):
                cognition = person["autonomy"]["cognition"]
                cognition["joint_visit"].update(
                    met=True, expires_at=self._world["simulation_minutes"] + activity_minutes,
                )
                self._record_episode_locked(person, "social", f"和{partner['name']}在{place}汇合了。", subject=partner)
                self._sync_actor_world_state_locked(person)
            self._append_event_locked(
                "actors_met_for_visit", actor_id=actor_id, space_id=visit["target"],
                payload={"visit_id": visit_id, "peer_id": peer["actor_id"], "entity_id": visit.get("entity_id", "")},
            )
            seat_is_clear = entity and not any(
                person["space_id"] == visit["target"] and person["actor_id"] != sitter_id
                and (person["x"], person["y"]) == (entity["x"], entity["y"])
                for person in self._world["actors"].values()
            )
            if sitter_id and seat_is_clear:
                self.cognitive_act(sitter_id, visit["target"], entity["entity_id"], "rest")
            self._save_world_locked()
            return True

    def _finish_joint_visit_locked(self, actor_id: str, visit_id: str, *, together: bool = False) -> bool:
        actor = self._world["actors"].get(actor_id)
        visit = self._joint_visit_locked(actor) if actor else None
        if not visit or visit["id"] != visit_id:
            return False
        participants = [actor]
        if together or not visit.get("met"):
            peer = self._world["actors"].get(visit["peer_id"])
            if peer is not None:
                participants.append(peer)
        for person in participants:
            person["autonomy"]["cognition"]["joint_visit"]["done"] = True
            self._sync_actor_world_state_locked(person)
        return True

    def finish_joint_visit(self, actor_id: str, visit_id: str, *, together: bool = False) -> None:
        with self._lock:
            if self._finish_joint_visit_locked(actor_id, visit_id, together=together):
                self._save_world_locked()

    @staticmethod
    def _normalize_covert_directive(raw: Mapping[str, Any], *, sim_now: float = 0.0) -> dict[str, Any]:
        kind = str(raw.get("kind") or "entity").strip().lower()
        if kind not in ("entity", "visit"):
            kind = "entity"
        status = str(raw.get("status") or "pending").strip().lower()
        if status not in ("pending", "in_progress", "completed", "witnessed", "cancelled"):
            status = "pending"
        allowed: list[str] = []
        for item in (raw.get("allowed_witness_ids") or ()):
            cid = str(item or "").strip()
            if cid and cid not in allowed:
                allowed.append(cid)
                if len(allowed) >= 16:
                    break
        witnessed: list[str] = []
        for item in (raw.get("witnessed_by") or ()):
            wid = str(item or "").strip()
            if wid and wid not in witnessed:
                witnessed.append(wid)
                if len(witnessed) >= 16:
                    break
        exp_raw = raw.get("expires_at_min")
        expires_at: float | None = None
        if exp_raw not in (None, ""):
            try:
                expires_at = float(exp_raw)
            except (TypeError, ValueError):
                expires_at = None
        try:
            defer_count = max(0, int(raw.get("defer_count") or 0))
        except (TypeError, ValueError):
            defer_count = 0
        return {
            "directive_id": clip_text(raw.get("directive_id"), 64, "") or f"cd:{uuid.uuid4().hex[:8]}",
            "kind": kind,
            "target": str(raw.get("target") or "").strip(),
            "expected_space_id": str(raw.get("expected_space_id") or "").strip(),
            "cancel_on_blocked": bool(raw.get("cancel_on_blocked", False)),
            "action": str(raw.get("action") or "").strip().lower(),
            "note_text": clip_text(raw.get("note_text"), 120, ""),
            "require_privacy": bool(raw.get("require_privacy", True)),
            "allowed_witness_ids": allowed,
            "cover_goal": clip_text(raw.get("cover_goal"), 36, ""),
            "reason": clip_text(raw.get("reason"), 48, ""),
            "status": status,
            "witnessed_by": witnessed,
            "defer_count": defer_count,
            "deferred_witness_ids": sorted({
                str(wid) for wid in (raw.get("deferred_witness_ids") or ()) if isinstance(wid, str) and wid
            })[:16],
            "created_at_min": float(raw.get("created_at_min") or sim_now),
            "expires_at_min": expires_at,
            "updated_ts": float(raw.get("updated_ts") or time.time()),
        }

    @staticmethod
    def _normalize_overlay(raw: Mapping[str, Any]) -> dict[str, Any]:
        def normalize_cards(rows: Any) -> list[dict[str, Any]]:
            cards = []
            for card in (rows if isinstance(rows, list) else [])[:160]:
                if not isinstance(card, Mapping) or not card.get("id"):
                    continue
                icon = str(card.get("icon_data_url") or "")
                cards.append({
                    "id": clip_text(card["id"], 96),
                    "title": clip_text(card.get("title"), 180),
                    "subtitle": clip_text(card.get("subtitle"), 180),
                    "tag": clip_text(card.get("tag"), 48),
                    "items": [clip_text(line, 180) for line in card["items"][:160] if isinstance(line, str)]
                    if isinstance(card.get("items"), list) else [],
                    "selected": card.get("selected") is True,
                    "icon_data_url": icon if re.fullmatch(r"data:image/png;base64,[a-zA-Z0-9+/=]{1,16384}", icon) else "",
                })
            return cards

        sections_out: list[dict[str, Any]] = []
        for row in (raw.get("sections") or ())[:8]:
            if not isinstance(row, Mapping):
                continue
            items = [
                clip_text(it, 180, "")
                for it in (row.get("items") or ())[:32]
                if clip_text(it, 180, "")
            ]
            sections_out.append({
                "heading": clip_text(row.get("heading"), 32, ""),
                "body": clip_text(row.get("body"), 400, ""),
                "items": items,
                "cards": normalize_cards(row.get("cards")),
            })
        actions_out: list[dict[str, Any]] = []
        for row in (raw.get("actions") or ())[:6]:
            if not isinstance(row, Mapping):
                continue
            aid_act = clip_text(row.get("action_id"), 48, "")
            lbl = clip_text(row.get("label"), 24, "")
            if aid_act and lbl:
                actions_out.append({
                    "action_id": aid_act,
                    "label": lbl,
                    "style": "primary" if row.get("style") == "primary" else "secondary",
                    "required_selection": clamp_int(row.get("required_selection"), 0, 16, 0),
                })
        return {
            "view_only": bool(raw.get("view_only")),
            "live_autonomy": bool(raw.get("live_autonomy")),
            "title": clip_text(raw.get("title"), 32, ""),
            "badge": clip_text(raw.get("badge"), 40, ""),
            "summary": clip_text(raw.get("summary"), 240, ""),
            "sections": sections_out,
            "actions": actions_out,
            "cards": normalize_cards(raw.get("cards")),
            "select_limit": clamp_int(raw.get("select_limit"), 0, 16, 0),
            "context_id": clip_text(raw.get("context_id"), 96, ""),
            "updated_ts": float(raw.get("updated_ts") or time.time()),
        }

    def _external_realm_map_locked(self, space_id: str, *, create: bool = False) -> dict[str, dict[str, Any]]:
        sid = str(space_id or self._world.get("active_space_id") or "").strip()
        if sid not in self._world["spaces"]:
            raise ValueError(f"空间不存在：{sid}")
        realm = self._realm_space_id_locked(sid)
        root = self._world.setdefault("external_actor_state", {})
        if create:
            return root.setdefault(realm, {})
        return root.get(realm) or {}

    def set_actor_private_brief(
        self,
        actor_id: str,
        brief: str,
        *,
        cover_goal: str = "",
        space_id: str = "",
        night_active: bool | None = None,
    ) -> dict[str, Any]:
        """为指定角色注入外置私有背景简报（按角色及所属世界隔离）。"""
        with self._lock:
            aid = str(actor_id or "").strip()
            actor = self._world["actors"].get(aid)
            if actor is None:
                raise ValueError(f"角色不存在：{aid}")
            realm_map = self._external_realm_map_locked(space_id or str(actor["space_id"]), create=True)
            rec = realm_map.setdefault(
                aid,
                {"private_brief": "", "cover_goal": "", "covert_directive": None, "night_active": False, "updated_ts": time.time()},
            )
            rec["private_brief"] = clip_text(brief, 480, "")
            if cover_goal:
                rec["cover_goal"] = clip_text(cover_goal, 36, "")
            if night_active is not None:
                rec["night_active"] = bool(night_active)
            rec["updated_ts"] = time.time()
            self._save_world_locked()
            return copy.deepcopy(rec)

    def get_actor_private_brief(self, actor_id: str, *, space_id: str = "") -> str:
        with self._lock:
            aid = str(actor_id or "").strip()
            actor = self._world["actors"].get(aid)
            if actor is None:
                return ""
            eff_sid = space_id or str(actor.get("space_id") or "")
            if eff_sid not in self._world["spaces"]:
                return ""
            rec = self._external_realm_map_locked(eff_sid, create=False).get(aid) or {}
            return str(rec.get("private_brief") or "")

    def clear_actor_private_brief(self, actor_id: str, *, space_id: str = "") -> bool:
        with self._lock:
            aid = str(actor_id or "").strip()
            actor = self._world["actors"].get(aid)
            if actor is None:
                return False
            eff_sid = space_id or str(actor.get("space_id") or "")
            if eff_sid not in self._world["spaces"]:
                return False
            realm_map = self._external_realm_map_locked(eff_sid, create=False)
            rec = realm_map.get(aid)
            if not rec:
                return False
            rec["private_brief"] = ""
            rec["cover_goal"] = ""
            if rec.get("covert_directive") is None:
                realm_map.pop(aid, None)
            else:
                rec["updated_ts"] = time.time()
            self._save_world_locked()
            return True

    def set_covert_directive(
        self,
        actor_id: str,
        directive: Mapping[str, Any],
        *,
        space_id: str = "",
    ) -> dict[str, Any]:
        """下达带隐私门控的隐蔽行为指令（由认知引擎独自接收）。"""
        with self._lock:
            aid = str(actor_id or "").strip()
            actor = self._world["actors"].get(aid)
            if actor is None:
                raise ValueError(f"角色不存在：{aid}")
            if actor.get("is_player"):
                raise ValueError("不能向主控角色下达隐蔽指令。")
            eff_sid = space_id or str(actor["space_id"])
            cd = self._normalize_covert_directive(
                directive,
                sim_now=float(self._world.get("simulation_minutes") or 0.0),
            )
            if not cd["target"]:
                raise ValueError("隐蔽指令缺少 target。")
            realm_map = self._external_realm_map_locked(eff_sid, create=True)
            rec = realm_map.setdefault(
                aid,
                {"private_brief": "", "cover_goal": "", "covert_directive": None, "updated_ts": time.time()},
            )
            rec["covert_directive"] = cd
            if cd.get("cover_goal"):
                rec["cover_goal"] = cd["cover_goal"]
            rec["updated_ts"] = time.time()
            if self._realm_space_id_locked(actor["space_id"]) == self._realm_space_id_locked(eff_sid):
                cognition = actor["autonomy"].setdefault("cognition", {})
                cognition["preempt_seq"] = int(cognition.get("preempt_seq") or 0) + 1
                self._sync_actor_world_state_locked(actor)
            self._append_event_locked(
                "covert_directive_assigned",
                actor_id=aid,
                space_id=str(actor["space_id"]),
                payload={
                    "directive_id": cd["directive_id"],
                    "kind": cd["kind"],
                    "target": cd["target"],
                    "action": cd["action"],
                    "require_privacy": cd["require_privacy"],
                },
            )
            self._save_world_locked()
            return copy.deepcopy(cd)

    def get_covert_directive(self, actor_id: str, *, space_id: str = "") -> dict[str, Any] | None:
        with self._lock:
            aid = str(actor_id or "").strip()
            actor = self._world["actors"].get(aid)
            if actor is None:
                return None
            eff_sid = space_id or str(actor.get("space_id") or "")
            if eff_sid not in self._world["spaces"]:
                return None
            rec = self._external_realm_map_locked(eff_sid, create=False).get(aid) or {}
            cd = rec.get("covert_directive")
            return copy.deepcopy(cd) if isinstance(cd, Mapping) else None

    def update_covert_directive_state(
        self,
        actor_id: str,
        directive_id: str,
        *,
        status: str = "",
        deferred_witnesses: Sequence[str] | None = None,
        witnessed_by: Sequence[str] | None = None,
        completed_detail: str = "",
        space_id: str = "",
    ) -> dict[str, Any] | None:
        with self._lock:
            aid = str(actor_id or "").strip()
            actor = self._world["actors"].get(aid)
            if actor is None:
                return None
            eff_sid = space_id or str(actor.get("space_id") or "")
            if eff_sid not in self._world["spaces"]:
                return None
            rec = self._external_realm_map_locked(eff_sid, create=False).get(aid)
            cd = rec.get("covert_directive") if isinstance(rec, dict) else None
            if not isinstance(cd, dict) or (directive_id and cd.get("directive_id") != directive_id):
                return None
            sid = str(actor.get("space_id") or eff_sid)
            if cd.get("status") in ("completed", "witnessed", "cancelled"):
                return copy.deepcopy(cd)
            if deferred_witnesses is not None:
                wids = sorted({str(w).strip() for w in deferred_witnesses if str(w).strip()})[:16]
                if cd.get("deferred_witness_ids") == wids:
                    return copy.deepcopy(cd)
                cd["status"] = "pending"
                cd["deferred_witness_ids"] = wids
                cd["defer_count"] = int(cd.get("defer_count") or 0) + 1
                cd["updated_ts"] = time.time()
                self._append_event_locked(
                    "covert_act_deferred",
                    actor_id=aid,
                    space_id=sid,
                    payload={
                        "directive_id": cd["directive_id"],
                        "kind": cd["kind"],
                        "target": cd["target"],
                        "action": cd["action"],
                        "witness_ids": wids,
                    },
                )
            else:
                if witnessed_by:
                    cur_w = list(cd.get("witnessed_by") or ())
                    for wid in witnessed_by:
                        cwid = str(wid or "").strip()
                        if cwid and cwid not in cur_w:
                            cur_w.append(cwid)
                    cd["witnessed_by"] = cur_w[:16]
                clean_status = str(status or "").strip().lower()
                if clean_status in ("pending", "in_progress", "completed", "witnessed", "cancelled"):
                    cd["status"] = clean_status
                    cd["deferred_witness_ids"] = []
                cd["updated_ts"] = time.time()
                if clean_status in ("completed", "cancelled"):
                    self._append_event_locked(
                        "covert_act_completed" if clean_status == "completed" else "covert_act_cancelled",
                        actor_id=aid,
                        space_id=sid,
                        payload={
                            "directive_id": cd["directive_id"],
                            "kind": cd["kind"],
                            "target": cd["target"],
                            "action": cd["action"],
                            "detail": clip_text(completed_detail, 120, ""),
                        },
                    )
                elif clean_status == "witnessed":
                    self._append_event_locked(
                        "covert_act_witnessed",
                        actor_id=aid,
                        space_id=sid,
                        payload={
                            "directive_id": cd["directive_id"],
                            "kind": cd["kind"],
                            "target": cd["target"],
                            "action": cd["action"],
                            "witness_ids": list(cd.get("witnessed_by") or ()),
                        },
                    )
            self._save_world_locked()
            return copy.deepcopy(cd)

    def clear_covert_directive(self, actor_id: str, *, directive_id: str = "", space_id: str = "") -> bool:
        with self._lock:
            aid = str(actor_id or "").strip()
            actor = self._world["actors"].get(aid)
            if actor is None:
                return False
            eff_sid = space_id or str(actor.get("space_id") or "")
            if eff_sid not in self._world["spaces"]:
                return False
            realm_map = self._external_realm_map_locked(eff_sid, create=False)
            rec = realm_map.get(aid)
            cd = rec.get("covert_directive") if isinstance(rec, dict) else None
            if not isinstance(cd, Mapping):
                return False
            if directive_id and cd.get("directive_id") != directive_id:
                return False
            rec["covert_directive"] = None
            if not rec.get("private_brief"):
                realm_map.pop(aid, None)
            else:
                rec["updated_ts"] = time.time()
            self._save_world_locked()
            return True

    def _can_notice_covert_locked(self, witness: Mapping[str, Any], actor: Mapping[str, Any]) -> bool:
        if witness.get("actor_status", "active") != "active" or witness.get("posture") == "resting":
            return False
        space = self._world["spaces"][actor["space_id"]]
        if space["space_kind"] == "town" and self.chebyshev_distance(witness, actor) > 4:
            return False
        sight = self._sight_context_locked(witness)
        return not sight[0] or self._point_seen_locked(sight, (actor["x"], actor["y"]))

    def _check_covert_witnesses_locked(self, mover: Mapping[str, Any]) -> list[dict[str, Any]]:
        """当任意清醒角色走入/切入某空间视线范围时，检测是否撞见正在进行的隐蔽行动。"""
        mover_id = str(mover.get("actor_id") or "")
        sid = str(mover.get("space_id") or "")
        if not mover_id or not sid or mover.get("actor_status", "active") != "active":
            return []
        if sid not in self._world["spaces"]:
            return []
        realm = self._realm_space_id_locked(sid)
        realm_map = (self._world.get("external_actor_state") or {}).get(realm) or {}
        if not realm_map:
            return []
        caught: list[dict[str, Any]] = []
        for other_id, other in self._world["actors"].items():
            if other_id == mover_id or other.get("space_id") != sid or other.get("actor_status", "active") != "active":
                continue
            rec = realm_map.get(other_id)
            cd = rec.get("covert_directive") if isinstance(rec, dict) else None
            if not isinstance(cd, dict) or cd.get("status") != "in_progress":
                continue
            if not cd.get("require_privacy", True):
                continue
            if mover_id in (cd.get("allowed_witness_ids") or ()):
                continue
            if not self._can_notice_covert_locked(mover, other):
                continue
            cd["status"] = "witnessed"
            wids = list(cd.get("witnessed_by") or ())
            if mover_id not in wids:
                wids.append(mover_id)
            cd["witnessed_by"] = wids[:16]
            cd["updated_ts"] = time.time()
            cog = (other.get("autonomy") or {}).get("cognition")
            if isinstance(cog, dict):
                active_int = cog.get("active_intention") or {}
                if active_int.get("covert_directive_id") == cd["directive_id"]:
                    cog.pop("active_intention", None)
                other["autonomy"]["last_thought"] = ""
            self._sync_actor_world_state_locked(other)
            self._append_event_locked(
                "covert_act_witnessed",
                actor_id=other_id,
                space_id=sid,
                payload={
                    "directive_id": cd["directive_id"],
                    "kind": cd["kind"],
                    "target": cd["target"],
                    "action": cd["action"],
                    "witness_ids": list(cd["witnessed_by"]),
                    "caught_by": mover_id,
                },
            )
            caught.append({"actor_id": other_id, "directive_id": cd["directive_id"], "witness_id": mover_id})
        if caught:
            self._save_world_locked()
        return caught

    def set_external_overlay(self, space_id: str, overlay: Mapping[str, Any] | None) -> dict[str, Any] | None:
        """设置或清除面向当前世界的只读 HUD 面板内容（仅由外部宿主推入）。"""
        with self._lock:
            sid = str(space_id or self._world.get("active_space_id") or "").strip()
            if sid not in self._world["spaces"]:
                raise ValueError(f"空间不存在：{sid}")
            realm = self._realm_space_id_locked(sid)
            overlays = self._world.setdefault("external_overlays", {})
            if overlay is None:
                if realm not in overlays:
                    return None
                overlays.pop(realm, None)
                self._append_event_locked("external_overlay_updated", space_id=realm, payload={"cleared": True})
                self._save_world_locked()
                return None
            norm = self._normalize_overlay(overlay)
            current = overlays.get(realm)
            if current and all(current.get(key) == value for key, value in norm.items() if key != "updated_ts"):
                return copy.deepcopy(current)
            overlays[realm] = norm
            self._append_event_locked(
                "external_overlay_updated",
                space_id=realm,
                payload={"title": norm["title"], "badge": norm["badge"]},
            )
            self._save_world_locked()
            return copy.deepcopy(norm)

    def get_external_overlay(self, space_id: str = "") -> dict[str, Any] | None:
        with self._lock:
            sid = str(space_id or self._world.get("active_space_id") or "").strip()
            if sid not in self._world["spaces"]:
                return None
            realm = self._realm_space_id_locked(sid)
            ov = (self._world.get("external_overlays") or {}).get(realm)
            return copy.deepcopy(ov) if isinstance(ov, Mapping) else None

    @staticmethod
    def _redact_actor_dict_for_opaque(actor_dict: dict[str, Any], *, keep_self: bool = False) -> dict[str, Any]:
        if keep_self or actor_dict.get("is_player"):
            return actor_dict
        aut = actor_dict.get("autonomy")
        if isinstance(aut, dict):
            aut["last_thought"] = ""
            aut["current_goal"] = ""
            aut["current_drive"] = ""
            aut["last_action_summary"] = ""
            aut["agenda"] = ""
            aut["cognition"] = {}
        if "cognition" in actor_dict:
            actor_dict["cognition"] = {}
        if "last_thought" in actor_dict:
            actor_dict["last_thought"] = ""
        if "agenda" in actor_dict:
            actor_dict["agenda"] = ""
        if isinstance(actor_dict.get("reflection_state"), dict):
            actor_dict["reflection_state"] = {}
        actor_dict.pop("external_private_brief", None)
        actor_dict.pop("covert_directive", None)
        return actor_dict

    def _redact_view_node(self, node: dict[str, Any]) -> None:
        if isinstance(node.get("actor"), dict):
            self._redact_actor_dict_for_opaque(node["actor"])
        elif node.get("actor_id") and isinstance(node.get("autonomy"), dict):
            self._redact_actor_dict_for_opaque(node)
        if isinstance(node.get("actors"), list):
            for item in node["actors"]:
                if isinstance(item, dict):
                    self._redact_actor_dict_for_opaque(item)
        if isinstance(node.get("peers"), list):
            for item in node["peers"]:
                if isinstance(item, dict):
                    self._redact_view_node(item)
        if "cognition" in node and not (isinstance(node.get("actor"), dict) and node["actor"].get("is_player")):
            node["cognition"] = {}
        for k in ("thought", "goal", "drive", "agenda", "reflection"):
            if k in node:
                node[k] = [] if isinstance(node[k], list) else ""

    def redact_live_view_if_opaque(self, view: dict[str, Any], space_id: str) -> dict[str, Any]:
        if not isinstance(view, dict) or not space_id:
            return view
        if not self.is_opaque_realm(space_id):
            return view
        out = copy.deepcopy(view)
        self._redact_view_node(out)
        return out

    def configure_placed_entity(
        self,
        entity_id: str,
        *,
        custom_name: str | None = None,
        brief: str | None = None,
        detail: str | None = None,
        locked: bool | None = None,
        key_tag: str | None = None,
        writable_by_ai: bool | None = None,
        container_entity_id: str = "",
    ) -> dict[str, Any]:
        """线程安全地一次性配置放置物品的名称、描述、锁与容器归属。"""
        with self._lock:
            eid = str(entity_id or "").strip()
            ent = self._world["entities"].get(eid)
            if not ent:
                raise ValueError(f"物品不存在：{eid}")
            if custom_name is not None:
                ent["name"] = clip_text(custom_name, 12, ent["name"])
            if brief is not None:
                ent["brief"] = clip_text(brief, MAX_BRIEF_CHARS, ent["brief"])
            if detail is not None:
                ent["detail"] = clip_text(detail, MAX_DETAIL_CHARS, ent["detail"])
            traits = ent.setdefault("traits", {})
            if key_tag is not None:
                clean_tag = clip_text(key_tag, 32, "").lower()
                if "container" in traits and isinstance(traits["container"], dict):
                    traits["container"]["key_tag"] = clean_tag
                elif clean_tag:
                    tags = [str(t).lower() for t in (ent.get("tags") or []) if str(t).strip()]
                    if clean_tag not in tags:
                        tags = [*tags[-5:], clean_tag]
                    ent["tags"] = tags
            if locked is not None and "container" in traits and isinstance(traits["container"], dict):
                traits["container"]["is_locked"] = bool(locked)
                if bool(locked):
                    traits["container"]["is_open"] = False
            if writable_by_ai is not None and "inscribable" in traits and isinstance(traits["inscribable"], dict):
                traits["inscribable"]["writable_by_ai"] = bool(writable_by_ai)
            cid = str(container_entity_id or "").strip()
            if cid:
                if cid == eid:
                    raise ValueError("物品不能放入自身。")
                container = self._world["entities"].get(cid)
                if not container or "container" not in (container.get("traits") or {}):
                    raise ValueError(f"目标容器不存在或不可容纳物品：{cid}")
                ent["space_id"] = str(container["space_id"])
                ent["parent_id"] = f"entity:{cid}"
                ent["x"] = int(container["x"])
                ent["y"] = int(container["y"])
                ent["z"] = -1
            self._save_world_locked()
            return copy.deepcopy(ent)

    def place_ephemeral_entity(
        self, space_id: str, raw_blueprint: Mapping[str, Any], *,
        x: int | None = None, y: int | None = None, container_entity_id: str = "",
        placement_strategy: str = "spread", zone_id: str = "",
    ) -> dict[str, Any]:
        """放置只在目标平行域持久化的生成物。"""
        with self._lock:
            if space_id not in self._world["spaces"]:
                raise ValueError(f"空间不存在：{space_id}")
            realm = self._realm_space_id_locked(space_id)
            if container_entity_id:
                container = self._world["entities"].get(container_entity_id)
                if (not container or "container" not in container["traits"]
                        or self._realm_space_id_locked(container["space_id"]) != realm):
                    raise ValueError("目标容器须位于同一平行域。")
                space_id = container["space_id"]
                parent, px, py, pz = f"entity:{container_entity_id}", container["x"], container["y"], -1
            bid = f"ephemeral_{uuid4().hex}"
            bp = normalize_blueprint({**raw_blueprint, "blueprint_id": bid, "ephemeral_realm_id": realm})
            if not container_entity_id:
                if x is None or y is None:
                    try:
                        x, y = self._find_placement_cell_locked(space_id, bp, strategy=placement_strategy, zone_id=zone_id)
                    except ValueError:
                        if bp["placement"] not in ("wall", "surface_only"):
                            raise
                        # 生成物缺少墙面或承载面时，改放在同一空间的可通行地面上。
                        bp.update(placement="floor", solid=False)
                        x, y = self._find_placement_cell_locked(space_id, bp, strategy=placement_strategy, zone_id=zone_id)
                parent, px, py, pz = self._resolve_placement_locked(
                    space_id, x, y, bp["footprint"], bp["placement"], bp["solid"],
                )
            ent = self._instantiate_blueprint_locked(bp, space_id, parent, px, py, pz)
            eid = ent["entity_id"]
            self._world["ephemeral_blueprints"][bid] = bp
            self._world["entities"][eid] = ent
            self._append_event_locked("entity_placed", space_id=space_id, payload={
                "entity_id": eid, "name": ent["name"], "x": px, "y": py, "z": pz,
            })
            self._save_world_locked()
            return copy.deepcopy(ent)

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

    def dispatch_actor_to_player(
        self,
        actor_id: str,
        *,
        player_id: str = "",
        reason: str = "",
        space_id: str = "",
        preempt: bool = True,
    ) -> dict[str, Any]:
        """安排指定活跃 NPC 跨房间/跨建筑寻访同域主控并当面传达消息（无主控时静默跳过）。"""
        with self._lock:
            aid = str(actor_id or "").strip()
            actor = self._world["actors"].get(aid)
            if actor is None:
                raise ValueError(f"角色不存在：{aid}")
            if actor.get("is_player"):
                raise ValueError("不能派遣主控角色去找主控。")
            if actor.get("actor_status", "active") != "active":
                return {"ok": True, "dispatched": False, "reason": "actor_not_active"}

            eff_sid = str(space_id or actor.get("space_id") or "").strip()
            if eff_sid not in self._world["spaces"]:
                eff_sid = str(actor["space_id"])
            realm = self._realm_space_id_locked(eff_sid)

            req_pid = str(player_id or "").strip()
            target_player: dict[str, Any] | None = None
            for cand_id, cand in self._world["actors"].items():
                if not cand.get("is_player") or cand.get("actor_status", "active") == "missing":
                    continue
                if cand.get("space_id") not in self._world["spaces"]:
                    continue
                if self._realm_space_id_locked(cand["space_id"]) != realm:
                    continue
                if req_pid and cand_id != req_pid:
                    continue
                target_player = cand
                break

            if target_player is None:
                return {"ok": True, "dispatched": False, "reason": "no_player_avatar"}

            pid = str(target_player["actor_id"])
            clean_reason = clip_text(reason, 80, "") or "有事想当面告诉对方"
            mod_id = time.monotonic_ns()
            commitment = {
                "id": f"ext_seek:{mod_id}",
                "kind": "seek_player",
                "target": pid,
                "drive": "social",
                "reason": clean_reason,
                "priority": "high",
                "source": "external",
            }
            packet: dict[str, Any] = {
                "id": mod_id,
                "realm": realm,
                "at": self._world["simulation_minutes"],
                "source": "external",
                "strength": 1.0,
                "agenda": clean_reason,
                "half_life": 25.0,
                "domains": {"social": 0.45},
                "places": {},
                "preferred_zone": "",
                "semantic": {},
                "experience": "",
                "thought": "",
                "diary": "",
                "relax": 0.0,
                "persistence": 0.0,
                "persist_think": False,
                "beliefs": [],
                "peer_beliefs": [],
                "commitment": commitment,
            }
            cognition = actor["autonomy"].setdefault("cognition", {})
            cognition["pending_modulation"] = packet
            if preempt:
                cognition["preempt_seq"] = int(cognition.get("preempt_seq", 0)) + 1
            self._sync_actor_world_state_locked(actor)
            self._append_event_locked(
                "npc_dispatched_to_player",
                actor_id=aid,
                space_id=str(actor["space_id"]),
                payload={
                    "actor_id": aid,
                    "player_id": pid,
                    "player_space_id": str(target_player["space_id"]),
                    "reason": clean_reason,
                },
            )
            self._save_world_locked()
            return {
                "ok": True,
                "dispatched": True,
                "actor_id": aid,
                "player_id": pid,
                "player_space_id": str(target_player["space_id"]),
                "preempt_seq": int(cognition.get("preempt_seq", 0)),
            }

    def propose_actor_commitment(
        self,
        actor_id: str,
        commitment: Mapping[str, Any],
        *,
        space_id: str = "",
        preempt: bool = False,
    ) -> dict[str, Any]:
        """以非强制方式为角色注入自主选择生成的世界意图（source='persona_choice'）。"""
        with self._lock:
            aid = str(actor_id or "").strip()
            actor = self._world["actors"].get(aid)
            if actor is None:
                raise ValueError(f"角色不存在：{aid}")
            if actor.get("is_player"):
                return {"ok": True, "proposed": False, "reason": "is_player"}
            if actor.get("actor_status", "active") != "active":
                return {"ok": True, "proposed": False, "reason": "actor_not_active"}

            eff_sid = str(space_id or actor.get("space_id") or "").strip()
            if eff_sid not in self._world["spaces"]:
                eff_sid = str(actor["space_id"])
            realm = self._realm_space_id_locked(eff_sid)

            kind = str(commitment.get("kind") or "").strip()
            target = str(commitment.get("target") or "").strip()
            if kind not in ("visit", "entity", "social", "seek_player") or not target:
                raise ValueError("propose_actor_commitment 需要有效的 kind 与 target。")
            mod_id = time.monotonic_ns()
            cid = str(commitment.get("commitment_id") or f"pc:{mod_id}")
            drive = str(commitment.get("drive") or "").strip()
            if drive not in _MODULATION_DOMAINS:
                drive = "social" if kind in ("social", "seek_player") else "curiosity"
            reason = clip_text(commitment.get("reason"), 48, "") or "按照自己的想法去看看"
            priority = "high" if str(commitment.get("priority") or "").lower() == "high" else "normal"
            norm_comm: dict[str, Any] = {
                "id": cid,
                "kind": kind,
                "target": target,
                "drive": drive,
                "reason": reason,
                "priority": priority,
                "source": "persona_choice",
            }
            if commitment.get("action"):
                norm_comm["action"] = str(commitment["action"]).strip().lower()
            if commitment.get("note_text"):
                norm_comm["note_text"] = clip_text(commitment["note_text"], 120, "")

            packet: dict[str, Any] = {
                "id": mod_id,
                "realm": realm,
                "at": self._world["simulation_minutes"],
                "source": "persona_choice",
                "strength": 0.9,
                "agenda": reason,
                "half_life": 20.0,
                "domains": {drive: 0.35},
                "places": {},
                "preferred_zone": "",
                "semantic": {},
                "experience": "",
                "thought": "",
                "diary": "",
                "relax": 0.0,
                "persistence": 0.1,
                "persist_think": False,
                "beliefs": [],
                "peer_beliefs": [],
                "commitment": norm_comm,
            }
            cognition = actor["autonomy"].setdefault("cognition", {})
            visit = self._joint_visit_locked(actor)
            if visit:
                self._finish_joint_visit_locked(aid, visit["id"], together=True)
            cognition["pending_modulation"] = packet
            if preempt:
                cognition["preempt_seq"] = int(cognition.get("preempt_seq", 0)) + 1
            self._sync_actor_world_state_locked(actor)
            self._append_event_locked(
                "actor_commitment_proposed",
                actor_id=aid,
                space_id=str(actor["space_id"]),
                payload={
                    "commitment_id": cid,
                    "kind": kind,
                    "target": target,
                    "action": norm_comm.get("action", ""),
                    "reason": reason,
                },
            )
            self._save_world_locked()
            return {"ok": True, "proposed": True, "commitment_id": cid, "actor_id": aid}

    def emit_ambient_cue(
        self,
        space_id: str,
        text: str,
        *,
        zone_id: str = "",
        audible_adjacent_zones: bool = True,
        cue_kind: str = "sound",
    ) -> dict[str, Any]:
        """向指定空间或隔间发出客观环境异常/声光线索，进入见证者与同层耳闻者的事件流。"""
        with self._lock:
            sid = str(space_id or "").strip()
            space = self._world["spaces"].get(sid)
            if space is None:
                raise ValueError(f"空间不存在：{sid}")
            clean_text = clip_text(text, 100, "")
            if not clean_text:
                raise ValueError("emit_ambient_cue 需要非空的 text。")
            clean_kind = clip_text(cue_kind, 24, "sound") or "sound"
            clean_zid = str(zone_id or "").strip()
            matched_zid = ""
            matched_zname = ""
            if clean_zid:
                zone = next(
                    (
                        z for z in (space.get("zones") or ())
                        if isinstance(z, Mapping) and (str(z.get("zone_id") or "") == clean_zid or str(z.get("name") or "") == clean_zid)
                    ),
                    None,
                )
                if zone is not None:
                    matched_zid = str(zone.get("zone_id") or "")
                    matched_zname = str(zone.get("name") or "")

            witness_ids: list[str] = []
            audible_ids: list[str] = []
            for aid, actor in self._world["actors"].items():
                if actor.get("space_id") != sid or actor.get("actor_status", "active") != "active":
                    continue
                if not matched_zid:
                    witness_ids.append(aid)
                    continue
                az = self.get_zone_at(space, actor["x"], actor["y"])
                if az and str(az.get("zone_id") or "") == matched_zid:
                    witness_ids.append(aid)
                elif audible_adjacent_zones:
                    audible_ids.append(aid)

            self._push_diff_locked(sid, clean_text)
            ev = self._append_event_locked(
                "ambient_cue",
                space_id=sid,
                payload={
                    "space_id": sid,
                    "space_name": str(space.get("name") or sid),
                    "floor_label": str(space.get("floor_label") or ""),
                    "zone_id": matched_zid,
                    "zone_name": matched_zname,
                    "text": clean_text,
                    "cue_kind": clean_kind,
                    "witness_actor_ids": witness_ids,
                    "audible_actor_ids": audible_ids,
                },
            )
            self._save_world_locked()
            return {"ok": True, "event": ev, "witness_actor_ids": witness_ids, "audible_actor_ids": audible_ids}

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
            if mood is not None and actor_id in self._world["actors"] and event["payload"]["kind"] != "memory_consolidated":
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
            if actor is None or actor.get("actor_status", "active") != "active":
                return None
            sid = actor["space_id"]
            space = self._world["spaces"][sid]
            realm, realm_places = self._realm_places_locked(sid)
            ext_rec = self._external_realm_map_locked(realm, create=False).get(actor_id) or {}
            private_brief = str(ext_rec.get("private_brief") or "")
            raw_cd = ext_rec.get("covert_directive")
            covert_directive = (
                copy.deepcopy(raw_cd)
                if isinstance(raw_cd, Mapping) and raw_cd.get("status") in ("pending", "in_progress")
                else None
            )
            covert_target_space = ""
            covert_entity = None
            covert_target_unavailable = False
            if covert_directive and covert_directive["kind"] == "entity":
                eid = covert_directive["target"]
                target = self._world["entities"].get(eid)
                expected = covert_directive["expected_space_id"]
                carried_by_other = bool(target and target["parent_id"].startswith(("actor:", "inventory:"))
                                        and not self._carries_item(actor, target))
                if expected and self._realm_space_id_locked(expected) == realm and (
                    not target or carried_by_other or target["space_id"] != expected
                ):
                    covert_target_space = expected
                    covert_target_unavailable = True
                elif target and self._realm_space_id_locked(target["space_id"]) == realm:
                    covert_target_space = target["space_id"]
                    if covert_target_space == sid:
                        covert_entity = next(iter(self.visible_entities(actor_id, spatial_filter=False, entity_id=eid)), None)
            joint_visit = self._joint_visit_locked(actor)
            cognition = actor["autonomy"].get("cognition") or {}
            active = cognition.get("active_intention") or {}
            pending_mod = cognition.get("pending_modulation") or {}
            pending_comm = pending_mod.get("commitment") if isinstance(pending_mod, Mapping) else {}
            signal = cognition.get("social_signal") or {}
            personal = (signal.get("speech_act") or {}).get("personal_goal") or {}
            entity_intent = personal.get("kind") == "entity" or (
                active.get("kind") == "entity" and active.get("source") == "speech_act"
            )
            seek_player_intent = (active.get("kind") == "seek_player") or (
                isinstance(pending_comm, Mapping) and pending_comm.get("kind") == "seek_player"
            )
            persona_intent = (active.get("source") == "persona_choice" or pending_mod.get("source") == "persona_choice")
            isolated_persona = self._is_isolated_persona_realm_locked(realm)
            if (not include_rooms and not (joint_visit or entity_intent or covert_directive or seek_player_intent or persona_intent)
                    and not (isolated_persona and private_brief) and not self._is_multi_space_realm_locked(realm)):
                return None
            joint_entity = self._joint_entity_locked(joint_visit.get("entity_id", ""), joint_visit["target"]) if joint_visit else None
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
                    "actor_status": str(a_x.get("actor_status") or "active"),
                }
                for aid_x, a_x in self._world["actors"].items()
                if aid_x != actor_id and not a_x.get("is_player")
                and a_x.get("actor_status", "active") != "missing"
                and self._realm_space_id_locked(a_x["space_id"]) == realm
            ]
            realm_players = [
                {
                    "actor_id": aid_p, "name": a_p["name"],
                    "space_id": a_p["space_id"], "x": a_p["x"], "y": a_p["y"], "posture": a_p["posture"],
                }
                for aid_p, a_p in self._world["actors"].items()
                if a_p.get("is_player") and a_p.get("actor_status", "active") != "missing"
                and a_p.get("space_id") in self._world["spaces"]
                and self._realm_space_id_locked(a_p["space_id"]) == realm
            ]
            lighting = compute_lighting(space, self._world["entities"], time.time(), self._realm_clock_offset_locked(sid))
            hour = lighting["hour_float"]
            return {
                "actor": copy.deepcopy(actor),
                "actors": copy.deepcopy([
                    a for a in self._world["actors"].values()
                    if a["space_id"] == sid and a.get("actor_status", "active") != "missing"
                ]),
                "space": copy.deepcopy(space),
                "realm": realm,
                "realm_places": realm_places,
                "realm_buildings": self._realm_buildings_locked(realm),
                "realm_portals": self._realm_portals_locked(realm),
                "realm_actors": realm_actors,
                "realm_players": realm_players,
                "joint_visit": copy.deepcopy(joint_visit),
                "joint_entity": (copy.deepcopy({key: joint_entity[key] for key in
                                  ("entity_id", "name", "space_id", "parent_id", "x", "y", "footprint")})
                                 if joint_entity else None),
                "player_ids": {aid for aid, a in self._world["actors"].items() if a.get("is_player")},
                "minutes": self._world["simulation_minutes"],
                "visible": visible,
                "visible_peer_ids": visible_peer_ids,
                "destinations": destinations,
                "lighting": lighting,
                "weather": self._weather_view_locked(sid),
                "home_space_id": self._resolve_actor_home_locked(actor, realm),
                "external_private_brief": private_brief,
                "nocturnal_active": bool(ext_rec.get("night_active") and (hour >= 21.0 or hour < 6.5)
                                         or covert_directive is not None),
                "covert_directive": covert_directive,
                "covert_target_space": covert_target_space,
                "covert_entity": covert_entity,
                "covert_target_unavailable": covert_target_unavailable,
                "covert_witness_ids": [
                    aid for aid, other in self._world["actors"].items()
                    if aid != actor_id and other["space_id"] == sid and aid not in covert_directive["allowed_witness_ids"]
                    and self._can_notice_covert_locked(other, actor)
                ] if covert_directive and covert_directive["require_privacy"] else [],
                "opaque_minds": self._is_opaque_realm_locked(realm),
                "isolated_persona": isolated_persona,
            }

    def live_actor_frame(self, actor_id: str) -> dict[str, Any] | None:
        with self._lock:
            actor = self._world["actors"].get(actor_id)
            if not actor:
                return None
            fields = ("actor_id", "name", "space_id", "x", "y", "facing", "move_seq", "last_path",
                      "last_act", "posture", "resting_on_id", "holding_id", "vitals", "expression",
                      "actor_status", "status_note")
            frame = {key: copy.deepcopy(actor.get(key)) for key in fields}
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
            joint_visit = self._joint_visit_locked(actor)
            frame["is_player"] = bool(actor.get("is_player", False))
            realm_sid = self._realm_space_id_locked(actor["space_id"])
            frame["home_space_id"] = "" if frame["is_player"] else self._resolve_actor_home_locked(actor, realm_sid)
            frame["reflection_state"] = {
                "id": packet.get("id"), "strength": packet.get("strength", 1.0),
                "preempt_seq": int(cog.get("preempt_seq", 0)),
                "seek_peer_target": str(active_int.get("target") or "") if active_int.get("kind") == "seek_peer" else "",
                "seek_player_target": str(active_int.get("target") or "") if active_int.get("kind") == "seek_player" else "",
                "joint_visit_id": joint_visit["id"] if joint_visit else "",
                "speech_action": active_int.get("kind") == "entity" and active_int.get("source") == "speech_act",
                "story_choice": active_int.get("source") == "persona_choice",
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
                and peer.get("actor_status", "active") == "active"
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
            viewer = self._entity_ui_viewer_locked(space_id)
            for entity in candidates:
                if entity is not None and entity["space_id"] == space_id and not entity["parent_id"].startswith("inventory:"):
                    item = self._entity_ui_payload_locked(entity, viewer)
                    if item is not None:
                        entities.append(item)
            result = {
                "space_id": space_id,
                "entities": entities,
                "lighting": compute_lighting(
                    space, world_entities, time.time(), self._realm_clock_offset_locked(space_id),
                ),
                "weather": self._weather_view_locked(space_id),
            }
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
            if aid == pid or actor["space_id"] != sid or actor.get("actor_status", "active") == "missing":
                continue
            pos = _score_pos(actor["x"], actor["y"])
            if pos[1] > 1:
                continue
            if pos[0] == 0:
                front_actor = aid
            act_status = str(actor.get("actor_status") or "active")
            status_note = str(actor.get("status_note") or "")
            label = (
                f"查探 {actor['name']}（{status_note or '无反应'}）"
                if act_status == "incapacitated"
                else f"和 {actor['name']} 对话"
            )
            ranked.append(((*pos, 3, 0), {
                "kind": "actor",
                "target_id": aid,
                "name": actor["name"],
                "actor_status": act_status,
                "status_note": status_note,
                "label": label,
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
            if portal is not None and not portal.get("is_open"):
                ranked.append(((*best, 0, 0), {
                    "kind": "entity",
                    "target_id": ent["entity_id"],
                    "name": ent["name"],
                    "action": "open",
                    "label": f"推开「{ent['name']}」",
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

    def player_actor_id(self, space_id: str = "") -> str:
        """Resolve the player assigned to an isolated realm or the sandbox."""
        with self._lock:
            sid = space_id or self._world["active_space_id"]
            realm = self._realm_space_id_locked(sid)
            base_id = episodic_memory.PLAYER_SUBJECT_ID
            actors = self._world["actors"]
            legacy = actors.get(base_id) or {}
            legacy_realm = self._realm_space_id_locked(legacy.get("space_id", ""))
            if self._is_isolated_persona_realm_locked(realm):
                scoped_id = f"{base_id}_{hashlib.sha256(realm.encode()).hexdigest()[:16]}"
                return scoped_id if scoped_id in actors or legacy_realm != realm else base_id
            sandbox_id = f"{base_id}_sandbox"
            if sandbox_id in actors or self._is_isolated_persona_realm_locked(legacy_realm):
                return sandbox_id
            return base_id

    def ensure_player_avatar(self, *, space_id: str = "", name: str = "你") -> dict[str, Any]:
        """在当前空间确保存在独立的主控「玩家」像素小人（不受 AI 自主心跳接管）。"""
        with self._lock:
            target_sid = str(space_id or self._world["active_space_id"]).strip()
            if target_sid not in self._world["spaces"]:
                raise ValueError(f"空间不存在：{target_sid}")
            pid = self.player_actor_id(target_sid)
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
                known_actors=[giver_name] if key != "user" else [],
                isolated_persona=self._is_isolated_persona_realm_locked(realm), **scope,
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
                blueprint = self.all_blueprints(include_ephemeral=False).get(blueprint_id)
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
                px0, py0 = int(player["x"]), int(player["y"])
                if (
                    self._is_floor_walkable_locked(space, nx, ny, ignore_actor_id=player_id)
                    and self.can_step_elevation(space, px0, py0, nx, ny)
                ):
                    self._finish_move_locked(player, [(px0, py0), (nx, ny)], record_event=False)
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
                target_id = str(prompt["target_id"])
                target_act = self._world["actors"].get(target_id)
                if target_act and target_act.get("actor_status", "active") == "incapacitated":
                    note = str(target_act.get("status_note") or "倒在地上没有反应")
                    self._append_event_locked(
                        "actor_inspected",
                        actor_id=player_id,
                        space_id=sid,
                        payload={
                            "target_actor_id": target_id,
                            "target_name": target_act["name"],
                            "status": "incapacitated",
                            "status_note": note,
                        },
                    )
                    self._push_diff_locked(sid, f"你走近查探了 {target_act['name']}：{note}")
                    self._save_world_locked()
                    return {
                        **result,
                        "kind": "inspect_actor",
                        "target_actor_id": target_id,
                        "name": prompt["name"],
                        "actor_status": "incapacitated",
                        "status_note": note,
                        "message": f"查探 {prompt['name']}：{note}",
                    }
                return {
                    **result,
                    "kind": "talk_actor",
                    "target_actor_id": target_id,
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
        cardinal: bool = False, region: tuple[int, int, int, int] | None = None, avoid_actors: bool = False,
    ) -> list[tuple[int, int]]:
        """沿物理寻路结果走有限步，返回实际经过的格子。"""
        with self._lock:
            walked = self.plan_actor_route(
                actor_id, space_id, x, y, reach=reach, stride=stride, cardinal=cardinal, region=region,
                avoid_actors=avoid_actors,
            )
            if len(walked) > 1:
                actor_obj = self._world["actors"][actor_id]
                self._open_interior_doors_on_path_locked(space_id, walked, actor_obj)
                self._finish_move_locked(actor_obj, walked)
                self._save_world_locked()
            return walked

    def plan_actor_route(
        self, actor_id: str, space_id: str, x: int, y: int, *, reach: int, stride: int,
        cardinal: bool = False, region: tuple[int, int, int, int] | None = None, avoid_actors: bool = False,
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
            ignore_from = (int(actor["x"]), int(actor["y"])) if distant and not avoid_actors else None
            path = self._bfs_locked(
                space_id, (actor["x"], actor["y"]), goals, ignore_actor_id=actor_id,
                ignore_distant_actors_from=ignore_from,
            )
            if not path and not actor.get("is_player"):
                path = self._bfs_locked(
                    space_id, (actor["x"], actor["y"]), goals, ignore_actor_id=actor_id,
                    ignore_distant_actors_from=ignore_from, pass_interior_doors=True,
                )
            return (path or [])[:stride + 1]

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
            if not actor.get("is_player"):
                self._open_interior_doors_on_path_locked(space_id, [cell], actor)
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
            message = self.execute_act(actor_id, entity_id, action)
            if action == "take":
                return self._carries_item(actor, entity), message
            if action == "place":
                return not self._carries_item(actor, entity), message
            if action == "unlock":
                return not entity["traits"]["container"]["is_locked"], message
            if action == "open":
                fixture = entity["traits"].get("container") or entity["traits"].get("portal") or {}
                return bool(fixture.get("is_open")), message
            return True, message

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
            actors = {
                aid: actor for aid, actor in self._world["actors"].items()
                if not actor.get("is_player") and actor.get("actor_status", "active") != "missing"
            }
            if not actors:
                return ""
            target = str(character_name or "").strip()
            sid = str(space_id or "").strip()
            active = str(self._world.get("active_actor_id") or "")

            if sid:
                if self._is_isolated_persona_realm_locked(sid):
                    active = self._isolated_active_actors.get(self._realm_space_id_locked(sid), "")
                if target:
                    for aid, act in actors.items():
                        if act.get("space_id") == sid and (act.get("host_character") == target or act.get("name") == target):
                            return aid
                if active in actors and actors[active].get("space_id") == sid:
                    return active
                for aid, act in actors.items():
                    if act.get("space_id") == sid:
                        return aid
                if self._is_isolated_persona_realm_locked(sid):
                    return ""
                actors = {
                    aid: act for aid, act in actors.items()
                    if not self._is_isolated_persona_realm_locked(act["space_id"])
                }

            if not actors:
                return ""
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

    @build_action("放置家具", space_arg=1)
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
            bp = self._lookup_blueprint_locked(blueprint_id, space_id)
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

    @build_action("移动物品", entity=True, inbound=True)
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
            peer = self._paired_portal_locked(entity_id, ent["traits"].get("portal", {}))
            if peer and pz != 0:
                raise ValueError("已连接的门或楼梯必须放在地面或墙面。")
            ent["parent_id"] = parent_id
            ent["x"] = px
            ent["y"] = py
            ent["z"] = pz

            if peer and (px, py) != (old_x, old_y):
                landing = self._portal_landing_locked(self._world["spaces"][sid], ent, allow_actor=True)
                if landing is None:
                    raise ValueError("移动后的入口旁需要留一格可通行的落脚点。")
                peer.update(target_x=landing[0], target_y=landing[1])

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

    def _paired_portal_locked(self, entity_id: str, portal: Mapping[str, Any]) -> dict[str, Any] | None:
        peer = self._world["entities"].get(portal.get("paired_entity_id", ""))
        peer_portal = (peer or {}).get("traits", {}).get("portal", {})
        return peer_portal if peer_portal.get("paired_entity_id") == entity_id else None

    def _disconnect_portal_peer_locked(self, entity_id: str, portal: Mapping[str, Any]) -> None:
        peer = self._paired_portal_locked(entity_id, portal)
        if peer:
            peer.pop("paired_entity_id")
            peer["target_space_id"] = ""

    @build_action("收走物品", entity=True, inbound=True)
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
                removed = self._world["entities"].pop(rid)
                self._disconnect_portal_peer_locked(rid, removed["traits"].get("portal", {}))
            self._cancel_key_loans_locked(to_remove)
            self._push_diff_locked(sid, f"「{ent['name']}」被收走了")
            self._append_event_locked("entity_removed", space_id=sid, payload={"entity_id": entity_id, "name": ent["name"], "cascade": to_remove})
            self._save_world_locked()
            return {"removed": to_remove}

    @build_action("编辑物品", entity=True, inbound=True)
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
            previous_portal = dict(ent["traits"].get("portal", {}))
            if traits is not None:
                ent["traits"] = normalize_traits(
                    traits,
                    name=str(ent.get("name") or ""),
                    tags=ent.get("tags") or (),
                )
            if portal_target_space_id is not None and "portal" in ent["traits"]:
                ent["traits"]["portal"]["target_space_id"] = str(portal_target_space_id).strip()
            portal = ent["traits"].get("portal", {})
            if previous_portal.get("paired_entity_id") and any(
                portal.get(field) != previous_portal.get(field)
                for field in ("target_space_id", "target_x", "target_y", "paired_entity_id")
            ):
                self._disconnect_portal_peer_locked(entity_id, previous_portal)
                portal.pop("paired_entity_id", None)
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
            affected = {sid} | self._inbound_portal_spaces_locked(sid)
            for eid in removed:
                ent = self._world["entities"].pop(eid)
                self._disconnect_portal_peer_locked(eid, ent["traits"].get("portal", {}))
            self._cancel_key_loans_locked(removed)
            for act in self._world["actors"].values():
                if act.get("space_id") == sid:
                    act["resting_on_id"] = None
                    act["posture"] = "standing"
            self._append_event_locked("space_cleared", space_id=sid, payload={"count": len(removed)})
            self._save_world_locked()
            self._build_history.discard_spaces(affected)
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
            if space.get("space_kind") == "town":
                w_view = self._weather_view_locked(sid)
                vision_radius = max(6, 12 + int(w_view.get("visibility_delta") or 0))
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
                self._realm_clock_offset_locked(space["space_id"]),
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
            if pid == aid or peer.get("space_id") != sid or peer.get("actor_status", "active") == "missing":
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
                    outer = self._open_container_path_locked(pid, actor_id)
                    if outer and _is_cell_seen(outer):
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
                rmode = self._entity_rest_mode(entity)
                is_sit = rmode == "sit" or (isinstance(occ_act, Mapping) and occ_act.get("posture") == "sitting")
                if is_sit:
                    parts.append(f"{occ_name}正坐在上面" if occ_name else "有人正坐在上面")
                else:
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
                rmode = self._entity_rest_mode(entity)
                verbs.append(("rest", "坐下" if rmode == "sit" else "坐下休息"))

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
                if not self._door_occupants_locked(entity):
                    verbs.append(("close", "关门"))
            else:
                verbs.append(("open", "开门"))
            if str(pt.get("target_space_id") or "") in self._world["spaces"]:
                verbs.append(("enter", "进入"))

        return verbs

    def _door_occupants_locked(self, entity: Mapping[str, Any]) -> list[dict[str, Any]]:
        sid = str(entity.get("space_id") or "")
        door_cells = set(self._entity_cells(entity))
        return [
            act for act in self._world["actors"].values()
            if act.get("space_id") == sid
            and act.get("actor_status", "active") != "missing"
            and (int(act["x"]), int(act["y"])) in door_cells
        ]

    def _door_occupant_refusal_locked(self, entity: Mapping[str, Any], actor_id: str) -> str:
        occupants = self._door_occupants_locked(entity)
        if not occupants:
            return ""
        if len(occupants) == 1 and occupants[0].get("actor_id") == actor_id:
            return f"你正站在「{entity['name']}」的门框里，先迈出一步再关门吧。"
        who = "、".join(str(a["name"]) for a in occupants)
        return f"{who}正站在「{entity['name']}」的门框里，挡住了门扇，暂时无法关门。"

    # ── 执行动作工具：act / move / write / combine ──────────────────────
    def resume_resting_group(self, space_id: str, actor_ids: Sequence[str]) -> dict[str, Any]:
        """结束隔离世界的一次集体休息，优先让有待办指令的角色恢复行动。"""
        with self._lock:
            realm = self._realm_space_id_locked(space_id)
            if not self._is_isolated_persona_realm_locked(realm):
                raise ValueError("集体休息快进仅适用于隔离世界。")
            cast = [
                actor for aid in dict.fromkeys(actor_ids)
                if (actor := self._world["actors"].get(aid)) and not actor.get("is_player")
                and actor.get("actor_status", "active") == "active"
                and self._realm_space_id_locked(actor["space_id"]) == realm
            ]
            if not cast or any(actor["posture"] != "resting" for actor in cast):
                return {"resumed": False}
            eligible = [
                actor for actor in cast
                if actor["autonomy"].get("enabled", True) and not actor["vitals"].get("disengaged")
            ]
            if not eligible:
                return {"resumed": False}
            external = self._external_realm_map_locked(realm)

            def priority(actor: Mapping[str, Any]) -> tuple[bool, float, str]:
                aid = actor["actor_id"]
                directive = (external.get(aid) or {}).get("covert_directive") or {}
                deadline = directive.get("expires_at_min")
                pending = directive.get("status") in ("pending", "in_progress") and (
                    deadline is None or self._world["simulation_minutes"] < deadline
                )
                vitals = actor["vitals"]
                return (not pending, vitals["sleep_pressure"] - vitals["energy"], aid)

            actor = min(eligible, key=priority)
            vitals = actor["vitals"]
            vitals["energy"] = max(vitals["energy"], 0.85)
            vitals["sleep_pressure"] = min(vitals["sleep_pressure"], 0.18)
            seat = self._world["entities"].get(actor.get("resting_on_id"))
            self._clear_actor_rest_locked(actor)
            cognition = actor["autonomy"].setdefault("cognition", {})
            cognition.pop("agreed_rest_until", None)
            for key in ("active_intention", "suspended_intention"):
                if (cognition.get(key) or {}).get("drive") == "rest":
                    cognition.pop(key, None)
            cognition["rest_resume_until"] = self._world["simulation_minutes"] + 1.0
            cognition["preempt_seq"] = int(cognition.get("preempt_seq") or 0) + 1
            self._record_act_locked(actor, "leave_rest", seat)
            self._refresh_actor_expression_locked(actor)
            self._sync_actor_world_state_locked(actor)
            self._save_world_locked()
            return {"resumed": True, "actor_id": actor["actor_id"], "name": actor["name"]}

    @staticmethod
    def _entity_rest_mode(entity: Mapping[str, Any] | None) -> str:
        if not isinstance(entity, Mapping):
            return "sleep"
        return infer_rest_mode(
            (entity.get("traits") or {}).get("restable"),
            name=str(entity.get("name") or ""),
            tags=entity.get("tags") or (),
        )

    def _clear_actor_rest_locked(self, actor: dict[str, Any]) -> None:
        rest_id = actor.get("resting_on_id")
        if rest_id and rest_id in self._world["entities"]:
            rs = (self._world["entities"][rest_id].get("traits") or {}).get("restable")
            if rs and rs.get("occupied_by") == actor.get("actor_id"):
                rs["occupied_by"] = None
        actor["resting_on_id"] = None
        actor["posture"] = "standing"
        cognition = actor["autonomy"].get("cognition")
        if cognition:
            cognition.pop("rest_started_at", None)
            cognition.pop("rest_started_ts", None)
            cognition.pop("rest_consolidated", None)

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

    @staticmethod
    def _infer_consumable_hunger_delta(entity: Mapping[str, Any]) -> float:
        eff = entity.get("effects") or {}
        if eff.get("hunger_delta") is not None:
            return float(eff["hunger_delta"])
        name = str(entity.get("name") or "")
        verb = str((entity.get("traits") or {}).get("consumable", {}).get("consume_verb") or "")
        tags = set(entity.get("tags") or ())
        if any(w in name for w in ("拉面", "猪扒饭", "猪排饭", "便当", "饭", "面", "正餐", "咖喱", "汉堡", "三明治")):
            return -0.55
        if any(w in name for w in ("蛋糕", "甜品", "派", "布丁", "松饼")):
            return -0.38
        if verb == "喝" or "drink" in tags or any(w in name for w in ("茶", "咖啡", "热饮", "奶茶", "苏打", "果汁", "饮料", "水")):
            return -0.18
        return -0.28

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
            delta = self._infer_consumable_hunger_delta(entity)
            aut["hunger"] = round(clamp_float(aut["hunger"] + delta, 0.0, 1.0, 0.2), 3)
        if "activity" in traits:
            aut["curiosity"] = round(clamp_float(aut["curiosity"] - 0.28, 0.0, 1.0, 0.3), 3)
            aut["hunger"] = round(clamp_float(aut["hunger"] + 0.01, 0.0, 1.0, 0.3), 3)
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
                # Event and physics updates can be newer than this cognitive snapshot.
                for key in ("episodic_memory", "rest_started_at", "rest_started_ts", "rest_consolidated", "joint_visit"):
                    if key in prev_cog:
                        cognition[key] = prev_cog[key]
                    else:
                        cognition.pop(key, None)
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
        is_portal = "portal" in (entity.get("traits") or {})
        allow_cells: set[tuple[int, int]] = (
            set(ent_cells) if (allow_inside_entity or (not entity.get("solid") and not is_portal)) else set()
        )

        if allow_inside_entity:
            for cx, cy in ent_cells:
                if self._is_floor_walkable_locked(space, cx, cy, allow_cells=allow_cells, ignore_actor_id=aid):
                    goal_cells.add((cx, cy))
        else:
            for cx, cy in ent_cells:
                for dx in (-1, 0, 1):
                    for dy in (-1, 0, 1):
                        nx, ny = cx + dx, cy + dy
                        if is_portal and (nx, ny) in ent_cells:
                            continue
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
            path = self._bfs_locked(
                sid,
                (int(actor["x"]), int(actor["y"])),
                goal_cells,
                allow_cells=allow_cells,
                ignore_actor_id=aid,
                pass_interior_doors=True,
            )
            if path:
                self._open_interior_doors_on_path_locked(sid, path, actor)
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
        space = self._world["spaces"][actor["space_id"]]
        if space["space_kind"] == "town" and space["zones"] and not actor.get("is_player"):
            previous = self.get_zone_at(space, actor["x"], actor["y"])
            for x, y in path[1:]:
                zone = self.get_zone_at(space, x, y)
                if zone is not None and zone != previous:
                    self._record_episode_locked(actor, "exploration", f"走过了{zone['name']}。", subject=zone)
                previous = zone
        self._clear_actor_rest_locked(actor)
        actor["autonomy"]["awake_rest"] = False
        self._record_move_locked(actor, path)
        actor["x"], actor["y"] = path[-1]
        self._refresh_actor_expression_locked(actor, action_kind="move")
        if record_event:
            self._append_event_locked("actor_moved", actor_id=actor["actor_id"], space_id=actor["space_id"],
                                      payload={"x": actor["x"], "y": actor["y"]})
        self._check_covert_witnesses_locked(actor)

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
                if canon == "close" and "portal" in (ent.get("traits") or {}) and "container" not in (ent.get("traits") or {}):
                    if refusal := self._door_occupant_refusal_locked(ent, actor_id):
                        return refusal
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
                if ent.get("ephemeral_realm_id") and actor_id not in ent["examined_by"]:
                    ent["examined_by"].append(actor_id)
                self._refresh_actor_expression_locked(actor, action_kind="look")
                self._append_event_locked("entity_inspected", actor_id=actor_id, space_id=sid, payload={"entity_id": eid, "name": ent["name"]})
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
                    if not new_open:
                        if refusal := self._door_occupant_refusal_locked(ent, actor_id):
                            return refusal
                    traits["portal"]["is_open"] = new_open
                    ent["solid"] = not new_open
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
                if left == 0 and not actor.get("is_player") and ent.get("parent_id") == f"inventory:{actor_id}":
                    if actor.get("holding_id") == eid:
                        actor["holding_id"] = None
                    self._world["entities"].pop(eid, None)
                    self._cancel_key_loans_locked((eid,))
                    self._refresh_actor_expression_locked(actor, action_kind="consume")
                    self._append_event_locked(
                        "entity_consumed",
                        actor_id=actor_id,
                        space_id=sid,
                        payload={"entity_id": eid, "verb": verb, "uses_left": 0, "vanished": True, "discarded_empty": True},
                    )
                    return report(f"{prefix}把随身的「{full_name}」{verb}完了，顺手收起了空器皿。", ok=True)
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
                rmode = self._entity_rest_mode(ent)
                occ = traits["restable"].get("occupied_by")
                if occ and occ != actor_id:
                    occ_act = (self._world.get("actors") or {}).get(str(occ))
                    if (
                        isinstance(occ_act, Mapping)
                        and occ_act.get("posture") in ("resting", "sitting")
                        and occ_act.get("resting_on_id") == eid
                    ):
                        occ_name = str(occ_act.get("name") or "别人").strip()
                        if rmode == "sit" or occ_act.get("posture") == "sitting":
                            return report(f"「{ent['name']}」上已经有{occ_name}坐着了，换个位置吧。")
                        return report(f"「{ent['name']}」上已经有{occ_name}在睡觉了，挤不下啦。")
                ok, prefix = self._walk_to_entity_locked(actor, ent, allow_inside_entity=True)
                if not ok:
                    return report(prefix)
                self._clear_actor_rest_locked(actor)
                traits["restable"]["occupied_by"] = actor_id
                actor["resting_on_id"] = eid
                actor["posture"] = "sitting" if rmode == "sit" else "resting"
                actor["x"] = int(ent["x"])
                actor["y"] = int(ent["y"])
                if not actor.get("is_player") and rmode != "sit":
                    cognition = actor["autonomy"].setdefault("cognition", {})
                    cognition["rest_started_at"] = self._world["simulation_minutes"]
                    cognition["rest_started_ts"] = time.time()
                self._refresh_actor_expression_locked(actor, action_kind="sit" if rmode == "sit" else "rest")
                self._append_event_locked(
                    "entity_state_changed",
                    actor_id=actor_id,
                    space_id=sid,
                    payload={
                        "entity_id": eid,
                        "resting": rmode != "sit",
                        "sitting": rmode == "sit",
                        "posture": actor["posture"],
                    },
                )
                if rmode == "sit":
                    return report(f"{prefix}在「{ent['name']}」上坐了下来。", ok=True)
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
                entries = traits["inscribable"].get("entries") or []
                if ent.get("ephemeral_realm_id") and actor_id not in ent["examined_by"]:
                    ent["examined_by"].append(actor_id)
                self._append_event_locked("entity_read", actor_id=actor_id, space_id=sid, payload={"entity_id": eid, "name": ent["name"], "entries_count": len(entries)})
                if entries:
                    self._record_episode_locked(actor, "activity", f"读了「{ent['name']}」上的文字。", subject=ent)
                self._save_world_locked()
                if not entries:
                    if ent.get("ephemeral_realm_id"):
                        return f"{prefix}{ent['name']}：{ent['detail']}"
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
        source_sid = str(ent["space_id"])
        if (self._is_isolated_persona_realm_locked(source_sid) or self._is_isolated_persona_realm_locked(target_sid)) and (
            self._realm_space_id_locked(source_sid) != self._realm_space_id_locked(target_sid)
        ):
            return False, "这扇门无法通往其他平行世界。", {}
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
            preferred_x=int(pt.get("target_x", 5)),
            preferred_y=int(pt.get("target_y", 2)),
            ignore_actor_id=actor_id,
        )
        self._switch_actor_world_state_locked(actor, target_sid)
        self._record_teleport_locked(actor)
        actor["x"] = tx
        actor["y"] = ty
        dest_kind = str(target_space.get("space_kind") or "room")
        if dest_kind != "town":
            self._record_episode_locked(actor, "exploration", f"走进了{target_space['name']}。")
            self._sync_actor_world_state_locked(actor)
        if dest_kind == "building":
            actor["facing"] = "up"
        elif dest_kind == "town":
            actor["facing"] = "down"
        self._refresh_actor_expression_locked(actor)
        if not self._is_isolated_persona_realm_locked(target_sid):
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
        self._check_covert_witnesses_locked(actor)
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

    def enter_portal(self, entity_id: str, *, actor_id: str = "", observe_only: bool = False) -> dict[str, Any]:
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
            isolated = self._is_isolated_persona_realm_locked(src_sid)
            src_realm = self._realm_space_id_locked(src_sid) if isolated else src_sid
            if (isolated or self._is_isolated_persona_realm_locked(target_sid)) and (
                self._realm_space_id_locked(src_sid) != self._realm_space_id_locked(target_sid)
            ):
                raise ValueError("这扇门无法通往其他平行世界。")
            overlay = (self._world.get("external_overlays") or {}).get(src_realm) if isolated else None
            is_view_only = bool(isinstance(overlay, Mapping) and overlay.get("view_only"))
            req_aid = str(actor_id or "").strip()
            if observe_only or (isolated and is_view_only):
                return {
                    "entered": True,
                    "mode": "observer",
                    "actor_id": "",
                    "from_space_id": src_sid,
                    "target_space_id": target_sid,
                    "target_space_name": str(target_space["name"]),
                    "feedback": f"切换视角进入「{target_space['name']}」。",
                }
            chosen_actor: dict[str, Any] | None = None
            if req_aid:
                cand = self._world["actors"].get(req_aid)
                if cand and cand.get("space_id") == src_sid:
                    chosen_actor = cand
                elif isolated:
                    # 平行故事域内若显式传入的演员（如尚未进入房间的玩家）不在门前，绝不强行瞬移无关 NPC
                    return {
                        "entered": True,
                        "mode": "observer",
                        "actor_id": "",
                        "from_space_id": src_sid,
                        "target_space_id": target_sid,
                        "target_space_name": str(target_space["name"]),
                        "feedback": f"切换视角进入「{target_space['name']}」。",
                    }
            if chosen_actor is None and not isolated:
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
            if not isolated:
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
    def _realm_business_day_locked(self, realm_space: Mapping[str, Any], cur_now: float) -> int:
        light = compute_lighting(
            realm_space,
            self._world["entities"],
            cur_now,
            self._realm_clock_offset_locked(str(realm_space.get("space_id") or "")),
        )
        return int(light["sim_day"]) if float(light["hour_float"]) >= 6.0 else int(light["sim_day"]) - 1

    def _restock_shops_locked(self, realm_sid: str, cur_now: float) -> list[str]:
        realm_space = self._world["spaces"].get(realm_sid)
        buildings = self._realm_buildings_locked(realm_sid) if realm_space else None
        if not realm_space or not isinstance(buildings, list) or not buildings:
            return []
        shops: list[tuple[Mapping[str, Any], str, str, str]] = []
        for b in buildings:
            if not isinstance(b, Mapping):
                continue
            shop_sid = str(b.get("interior_space_id") or "").strip()
            if not shop_sid or shop_sid not in self._world["spaces"]:
                continue
            profile = venue_semantics.restock_profile_for_building(b)
            if profile not in ("cafe", "shop"):
                continue
            shop_name = str(b.get("name") or self._world["spaces"][shop_sid].get("name") or shop_sid)
            shops.append((b, shop_sid, shop_name, profile))
        if not shops:
            return []

        business_day = self._realm_business_day_locked(realm_space, cur_now)
        restock_map = self._world.setdefault("shop_restock_day_by_realm", {})
        sim_now = float(self._world.get("simulation_minutes") or 0.0)
        if realm_sid not in restock_map:
            restock_map[realm_sid] = business_day
            if sim_now < 60.0:
                return []
            targets = [
                (b, s_sid, s_name, s_prof)
                for b, s_sid, s_name, s_prof in shops
                if not any(
                    e.get("space_id") == s_sid
                    and not str(e.get("parent_id") or "").startswith(("inventory:", "actor:"))
                    and int(((e.get("traits") or {}).get("consumable") or {}).get("uses_left") or 0) > 0
                    for e in self._world["entities"].values()
                )
            ]
        elif business_day > int(restock_map[realm_sid]):
            restock_map[realm_sid] = business_day
            targets = shops
        else:
            return []

        restocked: list[str] = []
        for bldg, shop_sid, shop_name, restock_profile in targets:
            shop_space = self._world["spaces"].get(shop_sid)
            if not shop_space:
                continue
            changed = False
            for ent in self._world["entities"].values():
                if ent.get("space_id") != shop_sid or str(ent.get("parent_id") or "").startswith(("inventory:", "actor:")):
                    continue
                traits = ent.get("traits") or {}
                cm = traits.get("consumable")
                th = traits.get("thermal")
                needs_refill = isinstance(cm, dict) and int(cm.get("uses_left") or 0) < int(cm.get("max_uses") or 1)
                needs_heat = isinstance(th, dict) and str(th.get("temp") or "cold") != "hot"
                if not (needs_refill or needs_heat):
                    continue
                if isinstance(cm, dict):
                    cm["uses_left"] = int(cm.get("max_uses") or 2)
                    bp_id = str(ent.get("blueprint_id") or "")
                    bp = self._custom_blueprints.get(bp_id) or self._builtin_blueprints.get(bp_id)
                    if bp:
                        ent["name"] = str(bp.get("name") or ent["name"])
                        ent["brief"] = str(bp.get("brief") or ent.get("brief") or "")
                    else:
                        full_name, _ = infer_consumable_names(ent["name"], str(cm.get("empty_name") or ""))
                        if full_name:
                            ent["name"] = full_name
                        if ent.get("brief") == cm.get("empty_brief"):
                            ent["brief"] = full_name or ent["name"]
                if isinstance(th, dict):
                    th["temp"] = "hot"
                    th["temp_updated_ts"] = cur_now
                changed = True

            staples = venue_semantics.restock_staples_for_profile(restock_profile)
            surfaces = [
                e for e in self._world["entities"].values()
                if e.get("space_id") == shop_sid
                and int(e.get("z", 0)) == 0
                and "surface" in (e.get("traits") or {})
            ]
            surface_cells: list[tuple[int, int]] = []
            for surf in surfaces:
                surface_cells.extend(self._entity_cells(surf))
            for bp_id in staples:
                if bp_id not in self._builtin_blueprints:
                    continue
                has_live = any(
                    e.get("space_id") == shop_sid
                    and not str(e.get("parent_id") or "").startswith(("inventory:", "actor:"))
                    and e.get("blueprint_id") == bp_id
                    and int(((e.get("traits") or {}).get("consumable") or {}).get("uses_left") or 0) > 0
                    for e in self._world["entities"].values()
                )
                if has_live:
                    continue
                bp = self._builtin_blueprints[bp_id]
                placed = False
                for cx, cy in surface_cells:
                    try:
                        p_id, px, py, pz = self._resolve_placement_locked(
                            shop_sid, cx, cy, bp["footprint"], bp["placement"], bp["solid"],
                        )
                        new_ent = self._instantiate_blueprint_locked(bp, shop_sid, p_id, px, py, pz)
                        self._world["entities"][new_ent["entity_id"]] = new_ent
                        changed = True
                        placed = True
                        break
                    except ValueError:
                        continue
                if not placed:
                    try:
                        fx, fy = self._find_open_floor_cell_locked(shop_space)
                        p_id, px, py, pz = self._resolve_placement_locked(
                            shop_sid, fx, fy, bp["footprint"], bp["placement"], bp["solid"],
                        )
                        new_ent = self._instantiate_blueprint_locked(bp, shop_sid, p_id, px, py, pz)
                        self._world["entities"][new_ent["entity_id"]] = new_ent
                        changed = True
                    except ValueError:
                        pass

            if changed:
                restocked.append(shop_name)
                self._push_diff_locked(shop_sid, f"清晨营业，「{shop_name}」补好了今日份的餐点与热饮。")
                for act in self._world["actors"].values():
                    if act.get("is_player") or act.get("actor_status", "active") != "active":
                        continue
                    cog = (act.get("autonomy") or {}).get("cognition")
                    if not isinstance(cog, dict):
                        continue
                    place = (cog.get("places") or {}).get(shop_sid)
                    if not isinstance(place, dict):
                        continue
                    aff = place.setdefault("affordances", {})
                    food_rec = aff.get("food")
                    if not isinstance(food_rec, dict) or float(food_rec.get("confidence", 0.0)) < 0.65:
                        aff["food"] = {
                            "source": "signboard",
                            "confidence": 0.65,
                            "samples": list((food_rec or {}).get("samples") or [sim_now])[-7:],
                        }
        return restocked

    def tick_physics(
        self, *, advance_minutes: float | None = None, now: float | None = None,
        minimum_elapsed_minutes: float = 0.0, time_scale: float = 1.0, consolidate_memory: bool = True,
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
                    return {"delta_min": 0.0, "cooled": [], "restocked": []}
                delta_min = elapsed * time_scale
                self._world["clock_offset_minutes"] += delta_min - elapsed
            self._world["last_tick_ts"] = cur_now
            self._world["simulation_minutes"] += delta_min

            cooled: list[str] = []
            restocked: list[str] = []
            precision = 5 if minimum_elapsed_minutes else 3
            if delta_min > 0:
                root_realms = [
                    r_sid for r_sid, sp in self._world["spaces"].items()
                    if not str(sp.get("parent_space_id") or "").strip()
                    or str(sp.get("parent_space_id") or "").strip() not in self._world["spaces"]
                ]
                sim_now = float(self._world.get("simulation_minutes") or 0.0)
                w_map = self._world.setdefault("weather_by_realm", {})
                for r_sid in root_realms:
                    prev_w = self._weather_state_locked(r_sid)
                    prev_code = prev_w["current"]
                    nxt_w, w_changed = weather.advance_weather(
                        prev_w, sim_now, realm_seed=f"{self._world['event_source']}:{r_sid}",
                    )
                    w_map[r_sid] = nxt_w
                    if w_changed:
                        self._record_weather_transition_locked(r_sid, prev_code, nxt_w)

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
                    ent_sid = str(ent.get("space_id") or "")
                    ent_sp = self._world["spaces"].get(ent_sid)
                    if ent_sp and ent_sp.get("space_kind") == "town" and not str(ent.get("parent_id") or "").startswith("inventory:"):
                        w_st = self._weather_state_locked(self._realm_space_id_locked(ent_sid))
                        rate = float(weather.WEATHER_META.get(w_st["current"], {}).get("outdoor_thermal_rate", 1.0))
                        elapsed *= rate
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

                for r_sid in root_realms:
                    restocked.extend(self._restock_shops_locked(r_sid, cur_now))

                for act in self._world["actors"].values():
                    if act.get("is_player") or act.get("actor_status", "active") != "active":
                        continue
                    vit = act["vitals"]
                    rest_id = act.get("resting_on_id")
                    posture = str(act.get("posture") or "standing")
                    has_rest_ent = bool(rest_id and rest_id in self._world["entities"])
                    is_resting = bool(posture == "resting" and has_rest_ent)
                    is_sitting = bool(posture == "sitting" and has_rest_ent)
                    cognition = act["autonomy"].setdefault("cognition", {})
                    if is_resting:
                        regen = float(((self._world["entities"][rest_id].get("traits") or {}).get("restable") or {}).get("energy_regen_per_min") or 0.012)
                        vit["energy"] = round(clamp_float(vit["energy"] + regen * delta_min, 0.0, 1.0, 0.7), precision)
                        vit["sleep_pressure"] = round(clamp_float(vit["sleep_pressure"] - regen * 0.9 * delta_min, 0.0, 1.0, 0.3), precision)
                        started = cognition.setdefault("rest_started_at", sim_now - delta_min)
                        started_ts = cognition.get("rest_started_ts", last_ts)
                        if advance_minutes is None and started_ts > last_ts:
                            rest_delta = max(0.0, (cur_now - started_ts) / 60.0) * time_scale
                            started = cognition["rest_started_at"] = sim_now - rest_delta
                        if consolidate_memory and not cognition.get("rest_consolidated") and sim_now - started >= episodic_memory.REST_MINUTES:
                            cognition["rest_consolidated"] = True
                            summary = episodic_memory.consolidate(cognition.get("episodic_memory") or {}, sim_now)
                            if summary:
                                self._queue_persona_event_locked(
                                    act, "memory_consolidated", None, "consolidate", summary,
                                    epa={"e": 0.0, "p": 0.0, "a": 0.0},
                                )
                    elif is_sitting:
                        regen = float(((self._world["entities"][rest_id].get("traits") or {}).get("restable") or {}).get("energy_regen_per_min") or 0.008)
                        vit["energy"] = round(clamp_float(vit["energy"] + regen * 0.45 * delta_min, 0.0, 1.0, 0.7), precision)
                    else:
                        if act["autonomy"].get("awake_rest") and vit["energy"] < AWAKE_REST_RECOVERY_TARGET:
                            vit["energy"] = round(min(AWAKE_REST_RECOVERY_TARGET, vit["energy"] + 0.01 * delta_min), precision)
                        else:
                            vit["energy"] = round(clamp_float(vit["energy"] - 0.0012 * delta_min, 0.05, 1.0, 0.7), precision)
                        vit["sleep_pressure"] = round(clamp_float(vit["sleep_pressure"] + 0.0015 * delta_min, 0.0, 1.0, 0.3), precision)
                    aut = normalize_autonomy(act.get("autonomy"))
                    hunger_rate = 0.0014 * 0.35 if is_resting else 0.0014
                    aut["hunger"] = round(clamp_float(aut["hunger"] + hunger_rate * delta_min, 0.0, 1.0, 0.3), precision)
                    aut["curiosity"] = round(clamp_float(aut["curiosity"] + 0.006 * delta_min, 0.0, 1.0, 0.55), precision)
                    act["autonomy"] = aut
                    self._refresh_actor_expression_locked(act)
                    self._sync_actor_world_state_locked(act)

                if delta_min >= 1.0 or cooled or restocked:
                    self._append_event_locked("world_tick", payload={"delta_min": round(delta_min, 2), "cooled": cooled, "restocked": restocked})
            self._save_world_locked()
            return {"delta_min": round(delta_min, 2), "cooled": cooled, "restocked": restocked}

    def has_space(self, space_id: str) -> bool:
        with self._lock:
            return str(space_id or "").strip() in self._world["spaces"]

    def latest_night_skip(self, space_id: str = "") -> dict[str, Any] | None:
        with self._lock:
            sid = str(space_id or self._world.get("active_space_id") or "").strip()
            if sid not in self._world["spaces"]:
                return None
            realm_sid = self._realm_space_id_locked(sid)
            realm_space = self._world["spaces"].get(realm_sid) or {}
            notice = realm_space.get("night_skip_notice")
            return copy.deepcopy(notice) if isinstance(notice, Mapping) else None

    def try_skip_night(
        self,
        space_id: str = "",
        *,
        chatting_ids: set[str] | None = None,
        consolidate_memory: bool = True,
    ) -> dict[str, Any] | None:
        """深夜全员默认上床且无重要夜间事务仍在活动时，自动将所属平行域时间跳至次日清晨 07:00。"""
        with self._lock:
            sid = str(space_id or self._world.get("active_space_id") or "").strip()
            if not sid or sid not in self._world["spaces"]:
                return None
            realm_sid = self._realm_space_id_locked(sid)
            realm_space = self._world["spaces"].get(realm_sid)
            if not isinstance(realm_space, dict):
                return None
            now_ts = time.time()
            realm_off = self._realm_clock_offset_locked(realm_sid)
            lt = compute_lighting(realm_space, self._world["entities"], now_ts, realm_off)
            hour = float(lt.get("hour_float") or 12.0)
            if not (hour >= 23.5 or hour < 6.5):
                return None

            local_ts = now_ts + realm_off * 60.0
            night_key = int((local_ts - 6.5 * 3600.0) // 86400)
            if realm_space.get("_last_night_skip_day") == night_key:
                return None

            realm_actors = [
                act for act in self._world["actors"].values()
                if act.get("actor_status", "active") == "active"
                and self._realm_space_id_locked(str(act.get("space_id") or "")) == realm_sid
            ]
            npcs = [act for act in realm_actors if not act.get("is_player")]
            players = [act for act in realm_actors if act.get("is_player")]
            if not npcs:
                return None
            chatting = chatting_ids or set()
            if any(str(act.get("actor_id") or "") in chatting for act in realm_actors):
                return None
            for act in npcs:
                rest_id = act.get("resting_on_id")
                if act.get("posture") != "resting" or not (rest_id and rest_id in self._world["entities"]):
                    return None
            overlay = (self._world.get("external_overlays") or {}).get(realm_sid)
            is_view_only = bool(isinstance(overlay, Mapping) and overlay.get("view_only"))
            if players and not is_view_only:
                if any(p.get("posture") != "resting" for p in players):
                    return None

            skip_hours = (24.0 - hour + 7.0) if hour >= 23.5 else (7.0 - hour)
            skip_min = round(skip_hours * 60.0, 2)
            from_clock = str(lt.get("clock_text") or f"{int(hour):02d}:{int((hour % 1) * 60):02d}")
            realm_space["clock_offset_minutes"] = round(float(realm_space.get("clock_offset_minutes") or 0.0) + skip_min, 2)
            realm_space["_last_night_skip_day"] = night_key
            sim_now = float(self._world.get("simulation_minutes") or 0.0)

            for act in (*npcs, *players):
                vit = act["vitals"]
                vit["energy"] = round(max(float(vit.get("energy", 0.7)), 0.94), 3)
                vit["sleep_pressure"] = round(min(float(vit.get("sleep_pressure", 0.2)), 0.08), 3)
                if not act.get("is_player"):
                    aut = normalize_autonomy(act.get("autonomy"))
                    aut["hunger"] = round(min(0.65, float(aut.get("hunger", 0.25)) + 0.18), 3)
                    cog = aut.setdefault("cognition", {})
                    if consolidate_memory and not cog.get("rest_consolidated"):
                        cog["rest_consolidated"] = True
                        summary = episodic_memory.consolidate(cog.get("episodic_memory") or {}, sim_now)
                        if summary:
                            self._queue_persona_event_locked(
                                act, "memory_consolidated", None, "consolidate", summary,
                                epa={"e": 0.0, "p": 0.0, "a": 0.0},
                            )
                    cog.pop("agreed_rest_until", None)
                    for k_int in ("active_intention", "suspended_intention"):
                        if isinstance(cog.get(k_int), Mapping) and cog[k_int].get("drive") == "rest":
                            cog.pop(k_int, None)
                    cog["rest_resume_until"] = sim_now + 1.0
                    act["autonomy"] = aut
                seat = self._world["entities"].get(act.get("resting_on_id"))
                self._clear_actor_rest_locked(act)
                self._record_act_locked(act, "leave_rest", seat)
                self._refresh_actor_expression_locked(act, action_kind="leave_rest")
                self._sync_actor_world_state_locked(act)

            self._restock_shops_locked(realm_sid, now_ts)
            prev_notice = realm_space.get("night_skip_notice") if isinstance(realm_space.get("night_skip_notice"), Mapping) else {}
            seq = int(prev_notice.get("seq") or 0) + 1
            notice = {
                "seq": seq,
                "realm_space_id": realm_sid,
                "from_clock": from_clock,
                "to_clock": "07:00",
                "skip_minutes": skip_min,
                "woken_actors": [str(act["actor_id"]) for act in npcs],
                "message": "🌙 夜深人静，全员已入睡，时间流逝至次日清晨 07:00",
            }
            realm_space["night_skip_notice"] = notice
            self._push_diff_locked(realm_sid, notice["message"])
            self._append_event_locked("night_skipped", space_id=realm_sid, payload=notice)
            self._save_world_locked()
            return copy.deepcopy(notice)

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

    def _open_container_path_locked(self, parent_id: str, viewer: str) -> dict[str, Any] | None:
        seen = set()
        parent = None
        while parent_id.startswith("entity:"):
            if parent_id in seen:
                return None
            seen.add(parent_id)
            parent = self._world["entities"].get(parent_id.split(":", 1)[1])
            container = (parent or {}).get("traits", {}).get("container") or {}
            if not container.get("is_open") or container.get("owner_actor_id") not in (None, "", viewer):
                return None
            if parent["z"] >= 0:
                return parent
            parent_id = parent["parent_id"]
        if parent_id.startswith(("actor:", "inventory:")) and parent_id.split(":", 1)[1] != viewer:
            return None
        return parent

    def _entity_ui_viewer_locked(self, space_id: str) -> str:
        player = self.player_actor_id(space_id)
        if player in self._world["actors"] or self._is_opaque_realm_locked(space_id):
            return player
        return self.resolve_actor_id(space_id=space_id)

    def _entity_ui_payload_locked(self, entity: Mapping[str, Any], viewer: str) -> dict[str, Any] | None:
        if entity.get("ephemeral_realm_id") and entity["z"] < 0:
            parent_id = entity["parent_id"]
            if parent_id.startswith("entity:"):
                if not self._open_container_path_locked(parent_id, viewer):
                    return None
            elif parent_id.startswith("actor:") and parent_id != f"actor:{viewer}":
                return None
        item = copy.deepcopy(entity)
        if item.get("ephemeral_realm_id") and viewer not in item.pop("examined_by", []):
            item["detail"] = ""
            if "inscribable" in item["traits"]:
                item["traits"]["inscribable"]["entries"] = []
        item["sprite"] = self._sprite_with_data_url(item["sprite"])
        item["state_tags"] = self.entity_state_tags(item)
        return item

    def snapshot_for_ui(self, space_id: str = "") -> dict[str, Any]:
        with self._lock:
            sid = str(space_id or self._world.get("active_space_id") or "")
            if sid not in self._world["spaces"]:
                sid = next(iter(self._world["spaces"]))
            space = copy.deepcopy(self._world["spaces"][sid])
            entities_in_space: list[dict[str, Any]] = []
            viewer = self._entity_ui_viewer_locked(sid)
            for ent in self._world["entities"].values():
                if ent.get("space_id") != sid or ent["parent_id"].startswith("inventory:"):
                    continue
                item = self._entity_ui_payload_locked(ent, viewer)
                if item is not None:
                    entities_in_space.append(item)

            realm_sid = self._realm_space_id_locked(sid)
            is_opaque = self._is_opaque_realm_locked(sid)
            actors_out: list[dict[str, Any]] = []
            for act in self._world["actors"].values():
                if act.get("actor_status", "active") == "missing":
                    continue
                a = copy.deepcopy(act)
                a["sprite"] = self._sprite_with_data_url(a["sprite"])
                a["active_sprite"] = self._actor_active_sprite_payload(a)
                if not a.get("is_player") and a.get("space_id") in self._world["spaces"]:
                    a_realm = self._realm_space_id_locked(a["space_id"])
                    a["resolved_home_space_id"] = self._resolve_actor_home_locked(a, a_realm)
                if is_opaque and not a.get("is_player"):
                    self._redact_actor_dict_for_opaque(a)
                actors_out.append(a)

            blueprints_out = []
            for bp in self.all_blueprints(include_ephemeral=False).values():
                b = copy.deepcopy(bp)
                b["sprite"] = self._sprite_with_data_url(b["sprite"])
                blueprints_out.append(b)

            palettes = self.all_palettes()
            active_palette = palettes.get(space["palette_id"]) or next(iter(palettes.values()))
            lighting = compute_lighting(
                space,
                self._world["entities"],
                time.time(),
                self._realm_clock_offset_locked(sid),
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
                        if act.get("space_id") == csid and act.get("actor_status", "active") != "missing"
                    ],
                })

            bgroup_id = str(space.get("building_group_id") or "").strip()
            building_floors: list[dict[str, Any]] = []
            if bgroup_id:
                grouped = [
                    fsp for fsp in self._world["spaces"].values()
                    if str(fsp.get("building_group_id") or "").strip() == bgroup_id
                ]
                grouped.sort(key=lambda fsp: (-int(fsp.get("floor_index", 1)), str(fsp.get("space_id") or "")))
                for fsp in grouped:
                    fsid = str(fsp.get("space_id") or "")
                    fidx = int(fsp.get("floor_index", 1))
                    flbl = str(fsp.get("floor_label") or "").strip() or (f"B{abs(fidx)}" if fidx < 0 else f"{fidx}F")
                    building_floors.append({
                        "space_id": fsid,
                        "name": str(fsp.get("name") or fsid),
                        "floor_index": fidx,
                        "floor_label": flbl,
                        "is_current": fsid == sid,
                        "stair_entity_id": door_by_target.get(fsid, ""),
                        "actor_count": 0 if is_opaque else sum(
                            1 for act in self._world["actors"].values()
                            if act.get("space_id") == fsid and act.get("actor_status", "active") != "missing"
                        ),
                    })

            player_id = self.player_actor_id(sid)
            viewer_ids = {player_id}
            if active_aid:
                viewer_ids.add(active_aid)
            events_out: list[dict[str, Any]] = []
            for ev in reversed(self.read_recent_events(40)):
                kind_ev = str(ev.get("kind") or "")
                if kind_ev in (
                    "covert_directive_assigned",
                    "covert_act_completed",
                    "covert_act_cancelled",
                    "covert_act_deferred",
                    "dialogue_exchange",
                    "dialogue_reunion_blocked",
                    "npc_dispatched_to_player",
                ):
                    continue
                if kind_ev == "covert_act_witnessed":
                    wids = {str(w) for w in ((ev.get("payload") or {}).get("witness_ids") or ()) if str(w)}
                    if not (wids & viewer_ids):
                        continue
                if is_opaque and kind_ev in ("reflection",):
                    continue
                events_out.append(ev)

            return {
                "active_space_id": sid,
                "active_actor_id": active_aid,
                "player_actor_id": player_id if self._world["actors"].get(player_id) else "",
                "realm_space_id": realm_sid,
                "opaque_minds": is_opaque,
                "isolated_persona": self._is_isolated_persona_realm_locked(sid),
                "external_overlay": copy.deepcopy((self._world.get("external_overlays") or {}).get(realm_sid)),
                "clock_offset_minutes": float(self._world.get("clock_offset_minutes") or 0.0),
                "looked_entities": dict(self._world.get("looked_entities") or {}),
                "spaces": [
                    {
                        **copy.deepcopy(sp),
                        "realm_space_id": self._realm_space_id_locked(sp["space_id"]),
                        "entity_count": sum(
                            1
                            for ent in self._world["entities"].values()
                            if ent.get("space_id") == sp.get("space_id")
                        ),
                        "actor_names": [
                            str(act.get("name") or act.get("actor_id") or "")
                            for act in self._world["actors"].values()
                            if act.get("space_id") == sp.get("space_id")
                            and act.get("actor_status", "active") != "missing"
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
                "build_history": self._build_history_status_locked(sid),
                "stamps": copy.deepcopy(list(self._stamps.values())),
                "parent_space": parent_space_summary,
                "child_spaces": child_spaces,
                "building_floors": building_floors,
                "current_zone": current_zone,
                "lighting": lighting,
                "night_skip": copy.deepcopy((self._world["spaces"].get(realm_sid) or {}).get("night_skip_notice"))
                if isinstance((self._world["spaces"].get(realm_sid) or {}).get("night_skip_notice"), Mapping)
                else None,
                "weather": self._weather_view_locked(sid),
                "palette": active_palette,
                "palettes": list(palettes.values()),
                "tile_catalog": [copy.deepcopy(t) for t in TILE_CATALOG.values()],
                "entities": entities_in_space,
                "actors": actors_out,
                "blueprints": blueprints_out,
                "host_characters": self.list_host_characters(),
                "events": events_out,
            }
