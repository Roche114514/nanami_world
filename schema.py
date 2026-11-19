"""文字世界本体规范与数据结构校验（Space / Actor / AssetBlueprint / Entity / Palette）。

全部数据保持纯 JSON-safe dict/dataclass 互转，确保确定性落盘与前后兼容。
"""

from __future__ import annotations

import re
import time
from collections.abc import Mapping, Sequence
from typing import Any

VALID_TRAITS: tuple[str, ...] = (
    "portable",
    "surface",
    "container",
    "consumable",
    "thermal",
    "switchable",
    "restable",
    "activity",
    "inscribable",
    "portal",
)

VALID_PLACEMENTS: frozenset[str] = frozenset({"floor", "wall", "surface_only", "any"})
VALID_LIGHT_MODES: frozenset[str] = frozenset({"auto", "dawn", "day", "dusk", "night"})
PIXEL_SLOTS: frozenset[str] = frozenset({".", "o", "1", "2", "3", "a", "b"})

MAX_BRIEF_CHARS = 25
MAX_DETAIL_CHARS = 60
MAX_NAME_CHARS = 12
MAX_SPACE_WIDTH = 128
MIN_SPACE_WIDTH = 8
MAX_SPACE_HEIGHT = 128
MIN_SPACE_HEIGHT = 6

# 16 种 Minecraft 式地形与墙体方块定义（用于自由铺地、筑墙与地图生成）
TILE_CATALOG: dict[str, dict[str, Any]] = {
    "wood_floor": {
        "tile_id": "wood_floor",
        "name": "暖木地板",
        "category": "floor",
        "walkable": True,
        "is_wall": False,
        "texture": "planks",
        "main": "#8b5e3c",
        "alt": "#7d5333",
        "accent": "#a3704c",
    },
    "dark_wood": {
        "tile_id": "dark_wood",
        "name": "胡桃木板",
        "category": "floor",
        "walkable": True,
        "is_wall": False,
        "texture": "planks",
        "main": "#543828",
        "alt": "#472e20",
        "accent": "#6e4a35",
    },
    "tatami": {
        "tile_id": "tatami",
        "name": "日式榻榻米",
        "category": "floor",
        "walkable": True,
        "is_wall": False,
        "texture": "tatami",
        "main": "#8fa365",
        "alt": "#829659",
        "accent": "#596b38",
    },
    "carpet_pink": {
        "tile_id": "carpet_pink",
        "name": "樱粉软毯",
        "category": "floor",
        "walkable": True,
        "is_wall": False,
        "texture": "carpet",
        "main": "#c4748e",
        "alt": "#b5657f",
        "accent": "#e39ab1",
    },
    "carpet_blue": {
        "tile_id": "carpet_blue",
        "name": "星夜地毯",
        "category": "floor",
        "walkable": True,
        "is_wall": False,
        "texture": "carpet",
        "main": "#465c85",
        "alt": "#3d5178",
        "accent": "#7089b8",
    },
    "stone_tile": {
        "tile_id": "stone_tile",
        "name": "复古石砖",
        "category": "floor",
        "walkable": True,
        "is_wall": False,
        "texture": "stone",
        "main": "#737882",
        "alt": "#666b75",
        "accent": "#949aa6",
    },
    "cobble": {
        "tile_id": "cobble",
        "name": "鹅卵石道",
        "category": "outdoor",
        "walkable": True,
        "is_wall": False,
        "texture": "cobble",
        "main": "#827e78",
        "alt": "#736f69",
        "accent": "#9e9a93",
    },
    "grass": {
        "tile_id": "grass",
        "name": "青翠草地",
        "category": "outdoor",
        "walkable": True,
        "is_wall": False,
        "texture": "grass",
        "main": "#4e8c46",
        "alt": "#457d3e",
        "accent": "#6eb364",
    },
    "flower_grass": {
        "tile_id": "flower_grass",
        "name": "繁花草坪",
        "category": "outdoor",
        "walkable": True,
        "is_wall": False,
        "texture": "flowers",
        "main": "#529148",
        "alt": "#48823f",
        "accent": "#ff9ebb",
    },
    "sand": {
        "tile_id": "sand",
        "name": "暖金沙地",
        "category": "outdoor",
        "walkable": True,
        "is_wall": False,
        "texture": "sand",
        "main": "#d4b475",
        "alt": "#c7a767",
        "accent": "#ebcf96",
    },
    "water": {
        "tile_id": "water",
        "name": "清澈池水",
        "category": "outdoor",
        "walkable": False,
        "is_wall": False,
        "texture": "water",
        "main": "#3b7ea1",
        "alt": "#326f91",
        "accent": "#7cc7e8",
    },
    "hedge": {
        "tile_id": "hedge",
        "name": "灌木篱笆",
        "category": "outdoor",
        "walkable": False,
        "is_wall": False,
        "texture": "leaves",
        "main": "#2e5c29",
        "alt": "#244a20",
        "accent": "#488241",
    },
    "brick_wall": {
        "tile_id": "brick_wall",
        "name": "红砖墙体",
        "category": "wall",
        "walkable": False,
        "is_wall": True,
        "texture": "brick",
        "main": "#8c463b",
        "alt": "#6e332a",
        "accent": "#b5695d",
    },
    "wood_wall": {
        "tile_id": "wood_wall",
        "name": "木质隔断墙",
        "category": "wall",
        "walkable": False,
        "is_wall": True,
        "texture": "wood_wall",
        "main": "#5c4233",
        "alt": "#422e22",
        "accent": "#7d5b47",
    },
    "white_wall": {
        "tile_id": "white_wall",
        "name": "奶油白墙",
        "category": "wall",
        "walkable": False,
        "is_wall": True,
        "texture": "plaster",
        "main": "#cfc7bc",
        "alt": "#9e9589",
        "accent": "#ebe5dd",
    },
    "glass_wall": {
        "tile_id": "glass_wall",
        "name": "玻璃隔断",
        "category": "wall",
        "walkable": False,
        "is_wall": True,
        "texture": "glass",
        "main": "#658f9c",
        "alt": "#43646e",
        "accent": "#b8e6f2",
    },
}

DEFAULT_SLOT_COLORS: dict[str, str] = {
    "o": "#21181b",
    "1": "#c97a4b",
    "2": "#f2be8d",
    "3": "#7c472c",
    "a": "#ffd166",
    "b": "#88d4f5",
}

# 默认静态 16x24 像素小人（单帧，不做骨骼/逐帧动画）
DEFAULT_ACTOR_PIXELS_16X24: tuple[str, ...] = (
    "................",
    ".....oooooo.....",
    "....o111111o....",
    "...o11221111o...",
    "...o1111111ao...",
    "...o11bbbb11o...",
    "...o1boooob1o...",
    "....obboobbo....",
    "....obbbbbbo....",
    ".....oooooo.....",
    "....o11aa11o....",
    "...o111aa111o...",
    "...o11111111o...",
    "...ob111111bo...",
    "....o113311o....",
    "....o113311o....",
    "....o333333o....",
    ".....o3333o.....",
    ".....oo..oo.....",
    ".....oboobo.....",
    ".....ob..bo.....",
    ".....oo..oo.....",
    "....o3o..o3o....",
    "................",
)

DEFAULT_ACTOR_SLOT_COLORS: dict[str, str] = {
    "o": "#201721",
    "1": "#c9a8b8",
    "2": "#ecd5e0",
    "3": "#566375",
    "a": "#84b89a",
    "b": "#ffe2cf",
}


def clamp_int(value: Any, low: int, high: int, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, parsed))


def clamp_float(value: Any, low: float, high: float, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if parsed != parsed:
        return default
    return max(low, min(high, parsed))


def slugify(text: str, prefix: str = "item") -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_]+", "_", str(text or "").strip().lower()).strip("_")
    if not cleaned:
        cleaned = prefix
    if cleaned[0].isdigit():
        cleaned = f"{prefix}_{cleaned}"
    return cleaned[:40]


def clip_text(text: Any, limit: int, fallback: str = "") -> str:
    cleaned = re.sub(r"\s+", " ", str(text or "").strip())
    if not cleaned:
        return fallback
    return cleaned[:limit].strip()


def _sanitize_pixel_char(ch: str) -> str:
    if ch in PIXEL_SLOTS:
        return ch
    mapping = {
        " ": ".",
        "_": ".",
        "-": ".",
        "#": "o",
        "0": ".",
        "x": "o",
        "X": "o",
        "A": "a",
        "B": "b",
    }
    return mapping.get(ch, "1" if ch.isalnum() else ".")


def normalize_pixel_grid(
    rows: Sequence[str] | None,
    width: int = 16,
    height: int = 16,
    *,
    align_bottom: bool = True,
    auto_outline: bool = False,
) -> list[str]:
    """将像素色槽矩阵裁齐/补足到指定尺寸，执行居中落底与可选 1px 自动描边。"""
    w = clamp_int(width, 8, 48, 16)
    h = clamp_int(height, 8, 48, 16)
    raw_rows = [str(r or "") for r in (rows or ())]
    if not raw_rows:
        return ["." * w for _ in range(h)]

    grid: list[list[str]] = []
    for y in range(h):
        src = raw_rows[y] if y < len(raw_rows) else ""
        row = [_sanitize_pixel_char(src[x]) if x < len(src) else "." for x in range(w)]
        grid.append(row)

    # 检测边界框以执行居中与落底（跳过纯空图）
    non_empty = [(x, y) for y in range(h) for x in range(w) if grid[y][x] != "."]
    if not non_empty:
        return ["." * w for _ in range(h)]

    if align_bottom:
        min_x = min(x for x, _ in non_empty)
        max_x = max(x for x, _ in non_empty)
        min_y = min(y for _, y in non_empty)
        max_y = max(y for _, y in non_empty)
        box_w = max_x - min_x + 1
        target_x0 = max(0, (w - box_w) // 2)
        target_y_shift = (h - 1) - max_y
        if min_y + target_y_shift < 0:
            target_y_shift = -min_y
        # 保留底行留 1px 空间方便描边时仅偏移到倒数第 1 行
        shifted = [["." for _ in range(w)] for _ in range(h)]
        for y in range(min_y, max_y + 1):
            ny = y + target_y_shift
            if not (0 <= ny < h):
                continue
            for x in range(min_x, max_x + 1):
                nx = target_x0 + (x - min_x)
                if 0 <= nx < w:
                    shifted[ny][nx] = grid[y][x]
        grid = shifted

    if auto_outline:
        outlined = [list(r) for r in grid]
        for y in range(h):
            for x in range(w):
                if grid[y][x] != ".":
                    continue
                for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    nx, ny = x + dx, y + dy
                    if 0 <= nx < w and 0 <= ny < h and grid[ny][nx] not in (".", "o"):
                        outlined[y][x] = "o"
                        break
        grid = outlined

    return ["".join(r) for r in grid]


def normalize_slot_colors(raw: Mapping[str, Any] | None, *, fallback: Mapping[str, str] | None = None) -> dict[str, str]:
    base = dict(fallback or DEFAULT_SLOT_COLORS)
    if isinstance(raw, Mapping):
        for k in ("o", "1", "2", "3", "a", "b"):
            val = str(raw.get(k) or "").strip()
            if re.fullmatch(r"#[0-9a-fA-F]{6}", val):
                base[k] = val.lower()
    return base


_CONTAINER_SUFFIXES = ("碗", "杯", "盘", "碟", "壶", "瓶", "罐", "盒", "桶", "盅")


def infer_consumable_names(name: str, empty_name: str = "") -> tuple[str, str]:
    """根据容器后缀区分「没吃完时的食物名」与「吃完后的空容器名」（如 豚骨拉面碗 -> 豚骨拉面 / 拉面碗）。"""
    clean = str(name or "").strip()
    explicit_empty = str(empty_name or "").strip()[:MAX_NAME_CHARS]
    if len(clean) >= 3 and clean.endswith(_CONTAINER_SUFFIXES) and not clean.startswith("空"):
        full_name = clean[:-1]
        inferred_empty = explicit_empty or (clean[-3:] if len(clean) >= 4 else clean)
        return full_name, inferred_empty
    return clean, (explicit_empty or clean)


def normalize_footprint(raw: Any) -> list[int]:
    if isinstance(raw, (list, tuple)) and len(raw) >= 2:
        w = clamp_int(raw[0], 1, 4, 1)
        h = clamp_int(raw[1], 1, 4, 1)
        return [w, h]
    return [1, 1]


def normalize_traits(raw: Mapping[str, Any] | None) -> dict[str, dict[str, Any]]:
    src = raw if isinstance(raw, Mapping) else {}
    out: dict[str, dict[str, Any]] = {}

    if isinstance(src.get("portable"), Mapping):
        p = src["portable"]
        weight = str(p.get("weight") or "light").strip().lower()
        out["portable"] = {"weight": "heavy" if weight == "heavy" else "light"}

    if isinstance(src.get("surface"), Mapping):
        s = src["surface"]
        out["surface"] = {"capacity": clamp_int(s.get("capacity"), 1, 12, 4)}

    if isinstance(src.get("container"), Mapping):
        c = src["container"]
        out["container"] = {
            "capacity": clamp_int(c.get("capacity"), 1, 20, 6),
            "key_tag": str(c.get("key_tag") or "").strip()[:32],
            "is_open": bool(c.get("is_open", False)),
            "is_locked": bool(c.get("is_locked", False)),
        }

    if isinstance(src.get("consumable"), Mapping):
        cm = src["consumable"]
        max_uses = clamp_int(cm.get("max_uses"), 1, 20, 2)
        uses_left = clamp_int(cm.get("uses_left"), 0, max_uses, max_uses)
        on_empty = str(cm.get("on_empty") or "keep_empty").strip().lower()
        out["consumable"] = {
            "max_uses": max_uses,
            "uses_left": uses_left,
            "consume_verb": clip_text(cm.get("consume_verb"), 8, "使用"),
            "on_empty": "vanish" if on_empty == "vanish" else "keep_empty",
            "empty_name": clip_text(cm.get("empty_name"), MAX_NAME_CHARS, ""),
            "empty_brief": clip_text(cm.get("empty_brief"), MAX_BRIEF_CHARS, "已经空了"),
        }

    if isinstance(src.get("thermal"), Mapping):
        th = src["thermal"]
        temp = str(th.get("temp") or "hot").strip().lower()
        if temp not in ("hot", "warm", "cold"):
            temp = "hot"
        out["thermal"] = {
            "cool_minutes": clamp_int(th.get("cool_minutes"), 5, 720, 45),
            "temp": temp,
            "temp_updated_ts": float(th.get("temp_updated_ts") or time.time()),
        }

    if isinstance(src.get("switchable"), Mapping):
        sw = src["switchable"]
        out["switchable"] = {
            "provides_light": bool(sw.get("provides_light", False)),
            "is_on": bool(sw.get("is_on", False)),
            "on_brief": clip_text(sw.get("on_brief"), MAX_BRIEF_CHARS, ""),
            "off_brief": clip_text(sw.get("off_brief"), MAX_BRIEF_CHARS, ""),
        }

    if isinstance(src.get("restable"), Mapping):
        rs = src["restable"]
        occ = rs.get("occupied_by")
        out["restable"] = {
            "energy_regen_per_min": clamp_float(rs.get("energy_regen_per_min"), 0.001, 0.2, 0.012),
            "comfort": clamp_float(rs.get("comfort"), 0.0, 1.0, 0.7),
            "occupied_by": str(occ).strip() if occ else None,
        }

    if isinstance(src.get("activity"), Mapping):
        ac = src["activity"]
        bat_raw = ac.get("battery")
        battery = None if bat_raw is None else clamp_int(bat_raw, 0, 100, 100)
        out["activity"] = {
            "domain": clip_text(ac.get("domain"), 24, "hobby"),
            "progress_unit": clip_text(ac.get("progress_unit"), 8, "次"),
            "progress": clamp_int(ac.get("progress"), 0, 99999, 0),
            "battery": battery,
            "battery_cost": clamp_int(ac.get("battery_cost"), 0, 50, 10),
        }

    if isinstance(src.get("inscribable"), Mapping):
        ins = src["inscribable"]
        raw_entries = ins.get("entries") if isinstance(ins.get("entries"), list) else []
        entries: list[dict[str, Any]] = []
        for row in raw_entries[-20:]:
            if isinstance(row, Mapping):
                txt = clip_text(row.get("text"), 120, "")
                if txt:
                    entries.append({
                        "by": str(row.get("by") or "user")[:32],
                        "ts": float(row.get("ts") or time.time()),
                        "text": txt,
                    })
        out["inscribable"] = {
            "writable_by_ai": bool(ins.get("writable_by_ai", True)),
            "max_entries": clamp_int(ins.get("max_entries"), 1, 30, 12),
            "entries": entries,
        }

    if isinstance(src.get("portal"), Mapping):
        pt = src["portal"]
        out["portal"] = {
            "target_space_id": str(pt.get("target_space_id") or "").strip(),
            "target_x": clamp_int(pt.get("target_x"), 0, MAX_SPACE_WIDTH - 1, 5),
            "target_y": clamp_int(pt.get("target_y"), 0, MAX_SPACE_HEIGHT - 1, 2),
            "is_open": bool(pt.get("is_open", False)),
        }

    return out


def normalize_effects(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    src = raw if isinstance(raw, Mapping) else {}
    sat = [clip_text(x, 16, "") for x in (src.get("satisfies") or []) if clip_text(x, 16, "")]
    domain = src.get("domain")
    return {
        "energy_delta": clamp_float(src.get("energy_delta"), -0.5, 0.5, 0.0),
        "sleep_pressure_delta": clamp_float(src.get("sleep_pressure_delta"), -0.5, 0.5, 0.0),
        "mood_v_delta": clamp_float(src.get("mood_v_delta"), -0.5, 0.5, 0.02),
        "satisfies": sat[:6],
        "domain": clip_text(domain, 24, "") or None,
    }


def normalize_combine_rules(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for row in raw[:8]:
        if not isinstance(row, Mapping):
            continue
        with_tag = clip_text(row.get("with_tag"), 24, "")
        with_id = clip_text(row.get("with_id"), 40, "")
        if not with_tag and not with_id:
            continue
        set_state = dict(row.get("set_target_state")) if isinstance(row.get("set_target_state"), Mapping) else {}
        out.append({
            "with_tag": with_tag,
            "with_id": with_id,
            "action_label": clip_text(row.get("action_label"), 16, "组合"),
            "consume_source": bool(row.get("consume_source", False)),
            "set_target_state": set_state,
            "feedback": clip_text(row.get("feedback"), 80, "它们发生了互动。"),
        })
    return out


def normalize_sprite(raw: Mapping[str, Any] | None, *, default_w: int = 16, default_h: int = 16) -> dict[str, Any]:
    src = raw if isinstance(raw, Mapping) else {}
    mode = str(src.get("mode") or "pixel_grid").strip().lower()
    if mode not in ("host_pack", "pixel_grid", "image", "token"):
        mode = "pixel_grid"
    w = clamp_int(src.get("w"), 8, 48, default_w)
    h = clamp_int(src.get("h"), 8, 48, default_h)
    raw_frames = src.get("frames") if isinstance(src.get("frames"), Mapping) else {}
    frames: dict[str, list[str]] = {}
    for frame_name, rows in raw_frames.items():
        if isinstance(rows, Sequence) and not isinstance(rows, (str, bytes)):
            frames[str(frame_name)] = normalize_pixel_grid(rows, w, h, align_bottom=True)
    if not frames and isinstance(src.get("pixels"), Sequence) and not isinstance(src.get("pixels"), (str, bytes)):
        frames["default"] = normalize_pixel_grid(src.get("pixels"), w, h, align_bottom=True)
    if mode == "pixel_grid" and not frames:
        frames["default"] = normalize_pixel_grid(["." * w for _ in range(h)], w, h, align_bottom=False)
    return {
        "mode": mode,
        "w": w,
        "h": h,
        "frames": frames,
        "slot_colors": normalize_slot_colors(src.get("slot_colors") if isinstance(src.get("slot_colors"), Mapping) else None),
        "image_path": str(src.get("image_path") or "").strip(),
        "palette_snap": clamp_float(src.get("palette_snap"), 0.0, 1.0, 0.35),
    }


def normalize_blueprint(raw: Mapping[str, Any], *, builtin: bool = False) -> dict[str, Any]:
    name = clip_text(raw.get("name"), MAX_NAME_CHARS, "未命名物件")
    bp_id = str(raw.get("blueprint_id") or "").strip() or slugify(name, prefix="bp")
    footprint = normalize_footprint(raw.get("footprint"))
    placement = str(raw.get("placement") or "any").strip().lower()
    if placement not in VALID_PLACEMENTS:
        placement = "any"
    sprite_raw = raw.get("sprite") if isinstance(raw.get("sprite"), Mapping) else {}
    def_w = max(16, footprint[0] * 16)
    def_h = max(16, footprint[1] * 16)
    if def_w > 32:
        def_w = 32
    if def_h > 32:
        def_h = 32
    tags = [clip_text(t, 16, "").lower() for t in (raw.get("tags") or []) if clip_text(t, 16, "")]
    traits = normalize_traits(raw.get("traits") if isinstance(raw.get("traits"), Mapping) else {})
    solid = bool(raw.get("solid", footprint[0] * footprint[1] > 1 or placement == "floor"))
    if "restable" in traits:
        solid = False
    if "portal" in traits:
        placement = "any"
        solid = not bool(traits["portal"].get("is_open", False))
    return {
        "blueprint_id": bp_id,
        "name": name,
        "brief": clip_text(raw.get("brief"), MAX_BRIEF_CHARS, name),
        "detail": clip_text(raw.get("detail"), MAX_DETAIL_CHARS, f"一件安静待在这里的{name}。"),
        "tags": tags[:6],
        "footprint": footprint,
        "placement": placement,
        "solid": solid,
        "traits": traits,
        "effects": normalize_effects(raw.get("effects") if isinstance(raw.get("effects"), Mapping) else {}),
        "combine_rules": normalize_combine_rules(raw.get("combine_rules")),
        "sprite": normalize_sprite(sprite_raw, default_w=def_w, default_h=def_h),
        "builtin": bool(builtin or raw.get("builtin", False)),
    }


def normalize_space_tiles(raw: Any, width: int, height: int) -> dict[str, str]:
    if not isinstance(raw, Mapping):
        return {}
    out: dict[str, str] = {}
    for raw_k, raw_tid in raw.items():
        m = re.fullmatch(r"\s*(-?\d+)\s*,\s*(-?\d+)\s*", str(raw_k or ""))
        if not m:
            continue
        x = int(m.group(1))
        y = int(m.group(2))
        if not (0 <= x < width and 0 <= y < height):
            continue
        tid = str(raw_tid or "").strip()
        if tid in TILE_CATALOG:
            out[f"{x},{y}"] = tid
    return out


def normalize_space(raw: Mapping[str, Any]) -> dict[str, Any]:
    name = clip_text(raw.get("name"), 16, "默认房间")
    sid = str(raw.get("space_id") or "").strip() or slugify(name, prefix="space")
    width = clamp_int(raw.get("width"), MIN_SPACE_WIDTH, MAX_SPACE_WIDTH, 12)
    height = clamp_int(raw.get("height"), MIN_SPACE_HEIGHT, MAX_SPACE_HEIGHT, 9)
    wall_rows = clamp_int(raw.get("wall_rows"), 0, max(1, height // 3), 1)
    light = str(raw.get("ambient_light") or "auto").strip().lower()
    if light not in VALID_LIGHT_MODES:
        light = "auto"
    return {
        "space_id": sid,
        "name": name,
        "width": width,
        "height": height,
        "wall_rows": wall_rows,
        "ambient_phrase": clip_text(raw.get("ambient_phrase"), 60, "一间安静的小房间，墙边开着一扇窗。"),
        "palette_id": str(raw.get("palette_id") or "warm_stardew").strip() or "warm_stardew",
        "ambient_light": light,
        "tiles": normalize_space_tiles(raw.get("tiles"), width, height),
    }


def _normalize_pack_sprites(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for item in raw[:64]:
        if not isinstance(item, Mapping):
            continue
        path = str(item.get("path") or "").strip()
        if not path:
            continue
        idx = clamp_int(item.get("index"), 1, 99, len(out) + 1)
        out.append({
            "index": idx,
            "path": path,
            "tag": clip_text(item.get("tag"), 40, f"立绘 {idx}"),
            "bucket": str(item.get("bucket") or "neutral").strip() or "neutral",
        })
    return out


def _normalize_portrait_crop(raw: Any) -> dict[str, float]:
    src = raw if isinstance(raw, Mapping) else {}
    return {
        "x": clamp_float(src.get("x"), 0.0, 1.0, 0.5),
        "y": clamp_float(src.get("y"), 0.0, 1.0, 0.2),
        "zoom": clamp_float(src.get("zoom"), 0.5, 3.0, 1.0),
    }


def _normalize_expression(raw: Any) -> dict[str, Any]:
    src = raw if isinstance(raw, Mapping) else {}
    return {
        "index": clamp_int(src.get("index"), 1, 99, 1),
        "tag": clip_text(src.get("tag"), 40, "平静"),
        "bucket": str(src.get("bucket") or "neutral").strip() or "neutral",
        "path": str(src.get("path") or "").strip(),
        "manual": bool(src.get("manual", False)),
    }


def normalize_autonomy(raw: Any) -> dict[str, Any]:
    src = raw if isinstance(raw, Mapping) else {}
    try:
        step_ts = float(src.get("last_step_ts") or 0.0)
    except (TypeError, ValueError):
        step_ts = 0.0
    if step_ts != step_ts:
        step_ts = 0.0
    recent = [str(x).strip() for x in (src.get("recent_targets") or []) if str(x).strip()]
    return {
        "enabled": bool(src.get("enabled", True)),
        "current_goal": clip_text(src.get("current_goal"), 36, "刚来到空间，正四处打量"),
        "current_drive": clip_text(src.get("current_drive"), 24, "curiosity"),
        "last_thought": clip_text(src.get("last_thought"), 60, ""),
        "last_action_summary": clip_text(src.get("last_action_summary"), 80, ""),
        "last_step_ts": max(0.0, step_ts),
        "hunger": round(clamp_float(src.get("hunger"), 0.0, 1.0, 0.30), 3),
        "curiosity": round(clamp_float(src.get("curiosity"), 0.0, 1.0, 0.55), 3),
        "recent_targets": recent[:5],
    }


def _normalize_vitals(raw: Any) -> dict[str, Any]:
    vitals_raw = raw if isinstance(raw, Mapping) else {}
    return {
        "energy": clamp_float(vitals_raw.get("energy"), 0.0, 1.0, 0.72),
        "sleep_pressure": clamp_float(vitals_raw.get("sleep_pressure"), 0.0, 1.0, 0.30),
        "mood_v": clamp_float(vitals_raw.get("mood_v"), -1.0, 1.0, 0.15),
        "agenda": str(vitals_raw.get("agenda") or "").strip(),
        "disengaged": bool(vitals_raw.get("disengaged", False)),
        "preferred_domains": [
            str(d).strip() for d in (vitals_raw.get("preferred_domains") or []) if str(d).strip()
        ],
        "source": str(vitals_raw.get("source") or "builtin"),
    }


def normalize_world_states(
    raw: Any,
    *,
    current_space_id: str,
    current_vitals: Mapping[str, Any],
    current_autonomy: Mapping[str, Any],
    current_expression: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    if isinstance(raw, Mapping):
        for k, v in raw.items():
            wsid = str(k or "").strip()
            if not wsid or not isinstance(v, Mapping):
                continue
            out[wsid] = {
                "vitals": _normalize_vitals(v.get("vitals")),
                "autonomy": normalize_autonomy(v.get("autonomy")),
                "expression": _normalize_expression(v.get("expression")),
            }
    cur_sid = str(current_space_id or "room_default").strip() or "room_default"
    out[cur_sid] = {
        "vitals": _normalize_vitals(current_vitals),
        "autonomy": normalize_autonomy(current_autonomy),
        "expression": _normalize_expression(current_expression),
    }
    return out


APPEARANCE_HAIR_STYLES = ("spiky", "long", "bob", "twin", "ponytail", "short")
APPEARANCE_OUTFITS = ("jacket", "hoodie", "dress", "uniform")
APPEARANCE_ACCESSORIES = ("none", "bow", "glasses", "ears", "headband")
APPEARANCE_FACINGS = ("down", "up", "left", "right")
MAP_STYLES = ("doll", "portrait")

_APPEARANCE_COLOR_KEYS = (
    "hair_color",
    "hair_shade",
    "skin",
    "eye_color",
    "outfit_color",
    "outfit_accent",
    "outfit_inner",
    "legs_color",
    "shoes_color",
    "accessory_color",
)

_APPEARANCE_PALETTES: tuple[dict[str, str], ...] = (
    {"hair_color": "#f2f0f0", "hair_shade": "#f2c6d0", "outfit_color": "#4f6b3c", "outfit_accent": "#d83a34", "outfit_inner": "#f4f4f4", "legs_color": "#3b4a2f", "shoes_color": "#9a9aa0"},
    {"hair_color": "#f3e2cc", "hair_shade": "#e4c8a8", "outfit_color": "#2f5266", "outfit_accent": "#d94a4a", "outfit_inner": "#f6efe4", "legs_color": "#2a4254", "shoes_color": "#d9503f"},
    {"hair_color": "#3a3144", "hair_shade": "#58496a", "outfit_color": "#6b3f5e", "outfit_accent": "#f2b8c8", "outfit_inner": "#f4f0f0", "legs_color": "#3a2c3c", "shoes_color": "#2d2733"},
    {"hair_color": "#d98a4a", "hair_shade": "#f0b878", "outfit_color": "#d6c36a", "outfit_accent": "#8a5a2b", "outfit_inner": "#fff7e2", "legs_color": "#5b4a3a", "shoes_color": "#6d4a32"},
    {"hair_color": "#5e86c4", "hair_shade": "#9bbcea", "outfit_color": "#e8e6ee", "outfit_accent": "#5e86c4", "outfit_inner": "#ffffff", "legs_color": "#39455f", "shoes_color": "#d0d4e0"},
    {"hair_color": "#c85a78", "hair_shade": "#f08aa0", "outfit_color": "#f0b6c6", "outfit_accent": "#a63a58", "outfit_inner": "#fff2f5", "legs_color": "#5a3a4a", "shoes_color": "#a63a58"},
    {"hair_color": "#2f2a2e", "hair_shade": "#4b4348", "outfit_color": "#2c3e5a", "outfit_accent": "#d4b04a", "outfit_inner": "#f2f2f2", "legs_color": "#23304a", "shoes_color": "#1c1c20"},
)


def _hex_color(value: Any, fallback: str) -> str:
    text = str(value or "").strip()
    if re.fullmatch(r"#[0-9a-fA-F]{6}", text):
        return text.lower()
    return fallback


def default_appearance(seed: str = "") -> dict[str, Any]:
    """按名字确定性生成一套 Q 版像素小人外观参数（同名永远同形象）。"""
    digest = 0
    for ch in str(seed or "角色"):
        digest = (digest * 131 + ord(ch)) & 0xFFFFFFFF
    pal = _APPEARANCE_PALETTES[digest % len(_APPEARANCE_PALETTES)]
    hair_style = APPEARANCE_HAIR_STYLES[(digest >> 3) % len(APPEARANCE_HAIR_STYLES)]
    outfit = APPEARANCE_OUTFITS[(digest >> 7) % len(APPEARANCE_OUTFITS)]
    skin = ("#fde8d8", "#f6d9c4", "#f3cfb3")[(digest >> 11) % 3]
    return {
        "hair_style": hair_style,
        "hair_color": pal["hair_color"],
        "hair_shade": pal["hair_shade"],
        "skin": skin,
        "eye_color": "#2a2630",
        "outfit_style": outfit,
        "outfit_color": pal["outfit_color"],
        "outfit_accent": pal["outfit_accent"],
        "outfit_inner": pal["outfit_inner"],
        "legs_color": pal["legs_color"],
        "shoes_color": pal["shoes_color"],
        "accessory": "none",
        "accessory_color": pal["outfit_accent"],
        "custom_frames": {},
    }


def _normalize_custom_frame(raw_rows: Any, width: int = 23, height: int = 32) -> list[str]:
    if not isinstance(raw_rows, Sequence) or isinstance(raw_rows, (str, bytes)):
        return []
    rows_in = [str(r or "") for r in raw_rows][:height]
    out: list[str] = []
    non_empty = 0
    for y in range(height):
        src = rows_in[y] if y < len(rows_in) else ""
        chars: list[str] = []
        for x in range(width):
            ch = src[x] if x < len(src) else "."
            if ch in (".", " ", "_"):
                chars.append(".")
            elif ch.isalnum() and ch.isascii():
                chars.append(ch)
                non_empty += 1
            else:
                chars.append(".")
        out.append("".join(chars))
    return out if non_empty >= 12 else []


def normalize_custom_frames(raw: Any) -> dict[str, Any]:
    """规范化自定义像素小人帧（支持任意 12x16 ~ 64x64 尺寸，包含 down / up / right + palette）。"""
    if not isinstance(raw, Mapping):
        return {}
    raw_down = raw.get("down")
    if not isinstance(raw_down, Sequence) or isinstance(raw_down, (str, bytes)) or not raw_down:
        return {}
    in_h = len(raw_down)
    in_w = max((len(str(r or "")) for r in raw_down), default=16)
    w = clamp_int(raw.get("w") or in_w, 12, 64, 23)
    h = clamp_int(raw.get("h") or in_h, 16, 64, 32)
    down = _normalize_custom_frame(raw_down, w, h)
    if not down:
        return {}
    out: dict[str, Any] = {"w": w, "h": h, "down": down}
    for key in ("up", "right"):
        frame = _normalize_custom_frame(raw.get(key), w, h)
        if frame:
            out[key] = frame
    raw_pal = raw.get("palette") if isinstance(raw.get("palette"), Mapping) else {}
    pal: dict[str, str] = {}
    for k, v in raw_pal.items():
        ks = str(k or "").strip()
        if len(ks) == 1 and ks.isalnum() and ks.isascii():
            hx = _hex_color(v, "")
            if hx:
                pal[ks] = hx
    out["palette"] = pal
    return out


def normalize_appearance(raw: Any, seed: str = "") -> dict[str, Any]:
    """规范化 Q 版小人外观参数：枚举越界回退，颜色必须是 #rrggbb。"""
    base = default_appearance(seed)
    if not isinstance(raw, Mapping):
        return base
    out = dict(base)
    hs = str(raw.get("hair_style") or "").strip().lower()
    if hs in APPEARANCE_HAIR_STYLES:
        out["hair_style"] = hs
    os_ = str(raw.get("outfit_style") or "").strip().lower()
    if os_ in APPEARANCE_OUTFITS:
        out["outfit_style"] = os_
    acc = str(raw.get("accessory") or "").strip().lower()
    if acc in APPEARANCE_ACCESSORIES:
        out["accessory"] = acc
    for key in _APPEARANCE_COLOR_KEYS:
        out[key] = _hex_color(raw.get(key), base[key])
    out["custom_frames"] = normalize_custom_frames(raw.get("custom_frames"))
    return out


def normalize_last_path(raw: Any, limit: int = 96) -> list[list[int]]:
    cells: list[list[int]] = []
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        for item in raw:
            if isinstance(item, Sequence) and not isinstance(item, (str, bytes)) and len(item) >= 2:
                try:
                    cells.append([int(item[0]), int(item[1])])
                except (TypeError, ValueError):
                    continue
            if len(cells) >= limit:
                break
    return cells


def normalize_last_act(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        return {"seq": 0, "verb": "", "entity_id": "", "ts": 0.0}
    try:
        seq = int(raw.get("seq") or 0)
    except (TypeError, ValueError):
        seq = 0
    try:
        ts = float(raw.get("ts") or 0.0)
    except (TypeError, ValueError):
        ts = 0.0
    return {
        "seq": max(0, seq),
        "verb": clip_text(raw.get("verb"), 24, ""),
        "entity_id": clip_text(raw.get("entity_id"), 80, ""),
        "ts": ts,
    }


def normalize_actor(raw: Mapping[str, Any]) -> dict[str, Any]:
    name = clip_text(raw.get("name"), 20, "七海千秋")
    aid = str(raw.get("actor_id") or "").strip() or slugify(name, prefix="actor")
    posture = str(raw.get("posture") or "standing").strip().lower()
    if posture not in ("standing", "resting"):
        posture = "standing"
    cur_sid = str(raw.get("space_id") or "room_default").strip() or "room_default"
    vitals = _normalize_vitals(raw.get("vitals"))
    expression = _normalize_expression(raw.get("expression"))
    autonomy = normalize_autonomy(raw.get("autonomy"))
    sprite_raw = raw.get("sprite") if isinstance(raw.get("sprite"), Mapping) else {}
    sprite_mode = str(sprite_raw.get("mode") or "pixel_grid").strip().lower()
    if sprite_mode not in ("host_pack", "pixel_grid", "image", "token"):
        sprite_mode = "pixel_grid"
    raw_pixels = sprite_raw.get("pixels")
    if isinstance(raw_pixels, Sequence) and not isinstance(raw_pixels, (str, bytes)) and len(raw_pixels) > 0:
        pixels = normalize_pixel_grid(raw_pixels, 16, 24, align_bottom=True)
    else:
        pixels = list(DEFAULT_ACTOR_PIXELS_16X24)
    accent = str(sprite_raw.get("accent_color") or "#e8a2b8").strip()
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", accent):
        accent = "#e8a2b8"
    pack_sprites = _normalize_pack_sprites(sprite_raw.get("pack_sprites"))
    if sprite_mode == "host_pack" and not pack_sprites:
        sprite_mode = "pixel_grid"
    world_states = normalize_world_states(
        raw.get("world_states"),
        current_space_id=cur_sid,
        current_vitals=vitals,
        current_autonomy=autonomy,
        current_expression=expression,
    )
    return {
        "actor_id": aid,
        "host_character": str(raw.get("host_character") or "").strip(),
        "name": name,
        "space_id": cur_sid,
        "x": clamp_int(raw.get("x"), 0, MAX_SPACE_WIDTH - 1, 5),
        "y": clamp_int(raw.get("y"), 0, MAX_SPACE_HEIGHT - 1, 4),
        "facing": (lambda f: f if f in APPEARANCE_FACINGS else "down")(str(raw.get("facing") or "down").strip().lower()),
        "appearance": normalize_appearance(raw.get("appearance"), seed=str(raw.get("host_character") or name)),
        "map_style": (lambda m: m if m in MAP_STYLES else "doll")(str(raw.get("map_style") or "doll").strip().lower()),
        "last_path": normalize_last_path(raw.get("last_path")),
        "move_seq": clamp_int(raw.get("move_seq"), 0, 2_000_000_000, 0),
        "last_act": normalize_last_act(raw.get("last_act")),
        "posture": posture,
        "holding_id": str(raw.get("holding_id")).strip() if raw.get("holding_id") else None,
        "resting_on_id": str(raw.get("resting_on_id")).strip() if raw.get("resting_on_id") else None,
        "last_action_kind": str(raw.get("last_action_kind") or "").strip(),
        "expression": expression,
        "sprite": {
            "mode": sprite_mode,
            "w": 16,
            "h": 24,
            "pixels": pixels,
            "slot_colors": normalize_slot_colors(
                sprite_raw.get("slot_colors") if isinstance(sprite_raw.get("slot_colors"), Mapping) else None,
                fallback=DEFAULT_ACTOR_SLOT_COLORS,
            ),
            "image_path": str(sprite_raw.get("image_path") or "").strip(),
            "accent_color": accent,
            "pack_sprites": pack_sprites,
            "portrait_crop": _normalize_portrait_crop(sprite_raw.get("portrait_crop")),
        },
        "vitals": vitals,
        "autonomy": autonomy,
        "world_states": world_states,
    }


def normalize_palette(raw: Mapping[str, Any], *, builtin: bool = False) -> dict[str, Any]:
    name = clip_text(raw.get("name"), 20, "自定义调色板")
    pid = str(raw.get("palette_id") or "").strip() or slugify(name, prefix="pal")
    room_raw = raw.get("room") if isinstance(raw.get("room"), Mapping) else {}
    sb_raw = raw.get("sprite_base") if isinstance(raw.get("sprite_base"), Mapping) else {}
    term_raw = raw.get("terminal") if isinstance(raw.get("terminal"), Mapping) else {}
    return {
        "palette_id": pid,
        "name": name,
        "builtin": bool(builtin or raw.get("builtin", False)),
        "room": {
            "wall_base": str(room_raw.get("wall_base") or "#4a3b32"),
            "wall_trim": str(room_raw.get("wall_trim") or "#2e231d"),
            "floor_a": str(room_raw.get("floor_a") or "#8b5e3c"),
            "floor_b": str(room_raw.get("floor_b") or "#7d5333"),
            "night_mask": str(room_raw.get("night_mask") or "rgba(14, 18, 42, 0.48)"),
            "lamp_glow": str(room_raw.get("lamp_glow") or "rgba(255, 214, 130, 0.34)"),
        },
        "sprite_base": {
            "outline": str(sb_raw.get("outline") or "#21181b"),
            "wood_main": str(sb_raw.get("wood_main") or "#9e6b43"),
            "wood_hi": str(sb_raw.get("wood_hi") or "#c99364"),
            "wood_lo": str(sb_raw.get("wood_lo") or "#6d4528"),
        },
        "terminal": {
            "bg": str(term_raw.get("bg") or "#111418"),
            "fg": str(term_raw.get("fg") or "#d7e0ea"),
            "accent": str(term_raw.get("accent") or "#ff9dbb"),
            "muted": str(term_raw.get("muted") or "#728096"),
            "border": str(term_raw.get("border") or "#2d3542"),
        },
    }
