"""规范化地图规划与建筑内室生成器（map_generator.py）：支持单室套间、多房建筑群与可伸缩城镇。

保持纯 Python 标准库与零故事业务逻辑：由 LLM 负责语义化建筑布局规划（离线可无缝降级），
再由确定性几何/内室编译器保障门洞双向连通、分区隔断、家具可达性与按人数配足的床位。
"""

from __future__ import annotations

import logging
import random
import time
import zlib
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from . import text_compiler, venue_semantics, weather
from .schema import (
    MAX_SPACE_HEIGHT,
    MAX_SPACE_WIDTH,
    MIN_SPACE_HEIGHT,
    MIN_SPACE_WIDTH,
    TILE_CATALOG,
    clamp_int,
    clip_text,
    normalize_buildings,
    normalize_zones,
    slugify,
)

from . import building_architect, interior_designer

logger = logging.getLogger(__name__)

VALID_MAP_MODES: tuple[str, ...] = ("room", "compound", "town")
VALID_PARTITIONS: tuple[str, ...] = (
    "alcove", "wing_split", "pillar", "l_alcove", "pillar_colonnade",
    "center_screen", "corner_nook", "double_wing", "pantry", "divider", "none",
)

_FLOOR_TILES: frozenset[str] = frozenset({
    "wood_floor", "dark_wood", "tatami", "carpet_pink", "carpet_blue", "stone_tile",
})
_WALL_TILES: frozenset[str] = frozenset({"brick_wall", "wood_wall", "white_wall", "glass_wall"})
_PALETTES: tuple[str, ...] = ("warm_stardew", "midnight_handheld", "ink_terminal", "cyber_pastel")

_MAP_PLAN_SYSTEM_PROMPT = """你是像素风文字世界的空间总建筑师。根据用户的场景需求、地点清单（如有）与常住人数，
只输出一个合法 JSON 对象（不要 markdown 围栏、不要多余说明），规划空间形态与各房间/建筑的语义结构：
{
  "theme": "manor|academy|atelier|onsen_inn|cyber_loft|cozy_villa|seaside|snow_onsen|neon_market|forest_academy|sakura",
  "layout_style": "ring_plaza|river_bridges|coastal_pier|winding_avenue|courtyard_manor",
  "name": "主舞台名称(<=14字)",
  "ambient_phrase": "一句话环境白描(<=55字)",
  "palette_id": "warm_stardew|midnight_handheld|ink_terminal|cyber_pastel",
  "initial_hour": 15.0,
  "allowed_weathers": ["clear", "cloudy", "rain"],
  "initial_weather": "clear",
  "hub": {
    "name": "中枢大厅或单室名称(<=14字)",
    "role": "public|home|library|cafe|arcade|shop",
    "floor_tile": "wood_floor|dark_wood|tatami|carpet_pink|carpet_blue|stone_tile",
    "rug_tile": "wood_floor|dark_wood|tatami|carpet_pink|carpet_blue|stone_tile",
    "wall_tile": "brick_wall|wood_wall|white_wall|glass_wall",
    "partition_style": "alcove|wing_split|pillar|l_alcove|pillar_colonnade|center_screen|corner_nook|none",
    "bed_count": 0,
    "furniture_clusters": ["sofa_lounge", "desk_set", "bookshelf_wall", "coffee_bar", "atelier_corner", "bed_bay", "storage_nook"],
    "scene_items": [{"name": "定制家具名", "brief": "一句话描述", "motif": "fireplace|grand_piano|long_table|sofa_plush|hot_spring_tub|altar_pedestal|lab_bench|bar_counter|display_case|gear_console|folding_screen|wardrobe|tea_hearth|workbench|standing_mirror|statue_bust|globe_stand|candelabra|specimen_rack|flower_vase", "relation": "centerpiece|against_wall|in_corner|near|on_surface|wall_hang|open", "footprint": [1, 1]}]
  },
  "rooms": [
    {
      "location_id": "若输入提供了对应地点的 location_id 请原样保留，否则留空",
      "name": "子房或建筑名(<=14字)",
      "role": "home|cafe|shop|library|arcade|public",
      "connect_to": ["仅compound模式可选：填相邻房间名/hub，构造大厅->书房->密室等多跳结构"],
      "ext_w": 7,
      "ext_h": 5,
      "zone_slot": "nw|ne|sw|se",
      "floor_tile": "wood_floor|dark_wood|tatami|carpet_pink|carpet_blue|stone_tile",
      "rug_tile": "carpet_blue|carpet_pink|tatami|dark_wood|stone_tile|wood_floor",
      "wall_tile": "brick_wall|wood_wall|white_wall|glass_wall",
      "roof_color": "#c9647a",
      "partition_style": "alcove|wing_split|pillar|l_alcove|pillar_colonnade|center_screen|corner_nook|none",
      "bed_count": 0,
      "furniture_clusters": ["bed_bay", "desk_set", "sofa_lounge", "bookshelf_wall", "atelier_corner", "storage_nook", "coffee_bar", "shop_counter", "arcade_row"],
      "scene_items": [],
      "interior_ambient": "室内环境白描(<=55字)"
    }
  ]
}
原则：
1. 若输入给出了 places 清单，请让每一处场所都有贴合名字与氛围的对应房间/建筑并保留其 location_id。
2. 所有居住/住宿场所 role="home" 的 bed_count 总和应满足 resident_count，避免角色夜间无床可睡；除住宿房间外也可设置书房/画室/茶室/工坊/娱乐室提升空间层次。
3. 可结合主题选择非十字路口的 layout_style，或在 compound 模式中通过 connect_to 安排多跳里屋/密室；各房间尽量通过 scene_items 与多变 partition_style 避免千篇一律。"""

_INDOOR_PRESETS: dict[str, dict[str, Any]] = {
    "manor": {
        "name": "雪松林间公馆",
        "palette_id": "warm_stardew",
        "ambient_phrase": "木梁吊灯与深色胡桃木交织的双层公馆，长廊连着数间功能各异的厅室。",
        "initial_hour": 18.0,
        "allowed_weathers": ["cloudy", "rain", "clear", "snow"],
        "initial_weather": "cloudy",
        "hub": {
            "name": "公馆中央会客厅", "role": "public", "floor_tile": "dark_wood", "rug_tile": "carpet_pink",
            "wall_tile": "brick_wall", "partition_style": "pillar_colonnade", "bed_count": 0,
            "furniture_clusters": ["sofa_lounge", "desk_set", "bookshelf_wall", "storage_nook"],
        },
        "rooms": [
            {"name": "东翼客房", "role": "home", "floor_tile": "wood_floor", "rug_tile": "tatami", "wall_tile": "wood_wall", "roof_color": "#a85646", "partition_style": "l_alcove", "bed_count": 2, "furniture_clusters": ["bed_bay", "desk_set", "storage_nook"]},
            {"name": "西翼宿舍", "role": "home", "floor_tile": "tatami", "rug_tile": "carpet_blue", "wall_tile": "wood_wall", "roof_color": "#7b6c63", "partition_style": "corner_nook", "bed_count": 2, "furniture_clusters": ["bed_bay", "sofa_lounge", "storage_nook"]},
            {"name": "档案阅览室", "role": "library", "floor_tile": "dark_wood", "rug_tile": "carpet_blue", "wall_tile": "wood_wall", "roof_color": "#527d78", "partition_style": "wing_split", "bed_count": 0, "furniture_clusters": ["bookshelf_wall", "desk_set", "atelier_corner", "sofa_lounge"]},
            {"name": "暖灯茶歇室", "role": "cafe", "floor_tile": "wood_floor", "rug_tile": "carpet_pink", "wall_tile": "white_wall", "roof_color": "#c9885b", "partition_style": "center_screen", "bed_count": 0, "furniture_clusters": ["coffee_bar", "sofa_lounge", "desk_set"]},
            {"name": "器物贮藏间", "role": "shop", "connect_to": ["档案阅览室"], "floor_tile": "stone_tile", "rug_tile": "dark_wood", "wall_tile": "brick_wall", "roof_color": "#5e7ea6", "partition_style": "alcove", "bed_count": 0, "furniture_clusters": ["shop_counter", "storage_nook", "desk_set"]},
        ],
    },
    "atelier": {
        "name": "北廊艺术工坊",
        "palette_id": "warm_stardew",
        "ambient_phrase": "采光宽敞的画室工坊群，飘着松节油、烘焙茶香与旧书纸页味道。",
        "initial_hour": 15.0,
        "allowed_weathers": ["clear", "cloudy", "rain"],
        "initial_weather": "clear",
        "hub": {
            "name": "展陈长廊大厅", "role": "public", "floor_tile": "wood_floor", "rug_tile": "stone_tile",
            "wall_tile": "white_wall", "partition_style": "pillar", "bed_count": 0,
            "furniture_clusters": ["sofa_lounge", "atelier_corner", "desk_set", "storage_nook"],
        },
        "rooms": [
            {"name": "天光大画室", "role": "library", "floor_tile": "wood_floor", "rug_tile": "stone_tile", "wall_tile": "white_wall", "roof_color": "#7b8fa6", "partition_style": "wing_split", "bed_count": 0, "furniture_clusters": ["atelier_corner", "bookshelf_wall", "desk_set", "storage_nook"]},
            {"name": "画室茶歇角", "role": "cafe", "floor_tile": "dark_wood", "rug_tile": "carpet_pink", "wall_tile": "white_wall", "roof_color": "#c9885b", "partition_style": "none", "bed_count": 0, "furniture_clusters": ["coffee_bar", "sofa_lounge", "desk_set"]},
            {"name": "北侧休息间", "role": "home", "floor_tile": "wood_floor", "rug_tile": "tatami", "wall_tile": "wood_wall", "roof_color": "#8a9c6e", "partition_style": "alcove", "bed_count": 2, "furniture_clusters": ["bed_bay", "desk_set", "storage_nook"]},
            {"name": "南侧宿屋", "role": "home", "floor_tile": "tatami", "rug_tile": "wood_floor", "wall_tile": "wood_wall", "roof_color": "#b8706a", "partition_style": "alcove", "bed_count": 2, "furniture_clusters": ["bed_bay", "sofa_lounge", "storage_nook"]},
        ],
    },
    "academy": {
        "name": "星野学园活动楼",
        "palette_id": "warm_stardew",
        "ambient_phrase": "下课后社团灯火亮起的红砖二层活动楼，连着资料室、电玩屋与值宿间。",
        "initial_hour": 16.5,
        "allowed_weathers": ["clear", "sakura", "cloudy", "rain"],
        "initial_weather": "clear",
        "hub": {
            "name": "一楼连廊门厅", "role": "public", "floor_tile": "stone_tile", "rug_tile": "carpet_blue",
            "wall_tile": "brick_wall", "partition_style": "pillar", "bed_count": 0,
            "furniture_clusters": ["sofa_lounge", "desk_set", "storage_nook"],
        },
        "rooms": [
            {"name": "社团活动室", "role": "arcade", "floor_tile": "carpet_blue", "rug_tile": "dark_wood", "wall_tile": "wood_wall", "roof_color": "#875fa3", "partition_style": "wing_split", "bed_count": 0, "furniture_clusters": ["arcade_row", "sofa_lounge", "desk_set"]},
            {"name": "旧书资料室", "role": "library", "floor_tile": "dark_wood", "rug_tile": "wood_floor", "wall_tile": "brick_wall", "roof_color": "#527d78", "partition_style": "alcove", "bed_count": 0, "furniture_clusters": ["bookshelf_wall", "desk_set", "storage_nook"]},
            {"name": "轻食备餐室", "role": "cafe", "floor_tile": "wood_floor", "rug_tile": "carpet_pink", "wall_tile": "white_wall", "roof_color": "#c78556", "partition_style": "none", "bed_count": 0, "furniture_clusters": ["coffee_bar", "sofa_lounge"]},
            {"name": "东翼合宿室", "role": "home", "floor_tile": "tatami", "rug_tile": "wood_floor", "wall_tile": "wood_wall", "roof_color": "#b55d66", "partition_style": "alcove", "bed_count": 2, "furniture_clusters": ["bed_bay", "desk_set", "storage_nook"]},
            {"name": "西翼宿直室", "role": "home", "floor_tile": "wood_floor", "rug_tile": "carpet_blue", "wall_tile": "wood_wall", "roof_color": "#739468", "partition_style": "alcove", "bed_count": 2, "furniture_clusters": ["bed_bay", "sofa_lounge", "storage_nook"]},
        ],
    },
    "onsen_inn": {
        "name": "冬雪温泉宿屋",
        "palette_id": "ink_terminal",
        "ambient_phrase": "挂着暖黄纸灯笼的木构温泉宿屋，推开纸拉门便是茶屋、汤廊与榻榻米客房。",
        "initial_hour": 19.0,
        "allowed_weathers": ["snow", "cloudy", "clear"],
        "initial_weather": "snow",
        "hub": {
            "name": "玄关账房前厅", "role": "public", "floor_tile": "dark_wood", "rug_tile": "tatami",
            "wall_tile": "wood_wall", "partition_style": "alcove", "bed_count": 0,
            "furniture_clusters": ["desk_set", "sofa_lounge", "storage_nook"],
        },
        "rooms": [
            {"name": "松风榻榻米房", "role": "home", "floor_tile": "tatami", "rug_tile": "wood_floor", "wall_tile": "wood_wall", "roof_color": "#8f5446", "partition_style": "alcove", "bed_count": 2, "furniture_clusters": ["bed_bay", "desk_set", "storage_nook"]},
            {"name": "竹影宿房", "role": "home", "floor_tile": "tatami", "rug_tile": "dark_wood", "wall_tile": "wood_wall", "roof_color": "#6c7f68", "partition_style": "alcove", "bed_count": 2, "furniture_clusters": ["bed_bay", "sofa_lounge", "storage_nook"]},
            {"name": "雪见围炉茶室", "role": "cafe", "floor_tile": "dark_wood", "rug_tile": "tatami", "wall_tile": "wood_wall", "roof_color": "#a86d4c", "partition_style": "none", "bed_count": 0, "furniture_clusters": ["coffee_bar", "sofa_lounge", "desk_set"]},
            {"name": "文库静读阁", "role": "library", "floor_tile": "wood_floor", "rug_tile": "stone_tile", "wall_tile": "wood_wall", "roof_color": "#556b68", "partition_style": "wing_split", "bed_count": 0, "furniture_clusters": ["bookshelf_wall", "desk_set", "atelier_corner"]},
        ],
    },
    "cyber_loft": {
        "name": "星夜错层工作室",
        "palette_id": "midnight_handheld",
        "ambient_phrase": "闪烁着柔色灯带的都市复式工作室群，配齐机房、咖啡吧与隔音休息舱。",
        "initial_hour": 20.5,
        "allowed_weathers": ["rain", "cloudy", "clear"],
        "initial_weather": "rain",
        "hub": {
            "name": "中庭休息主厅", "role": "public", "floor_tile": "stone_tile", "rug_tile": "carpet_blue",
            "wall_tile": "glass_wall", "partition_style": "pillar", "bed_count": 0,
            "furniture_clusters": ["sofa_lounge", "desk_set", "storage_nook"],
        },
        "rooms": [
            {"name": "终端控制间", "role": "arcade", "floor_tile": "carpet_blue", "rug_tile": "stone_tile", "wall_tile": "glass_wall", "roof_color": "#7b62a8", "partition_style": "wing_split", "bed_count": 0, "furniture_clusters": ["arcade_row", "desk_set", "storage_nook"]},
            {"name": "夜间咖啡角", "role": "cafe", "floor_tile": "dark_wood", "rug_tile": "carpet_pink", "wall_tile": "glass_wall", "roof_color": "#c96f89", "partition_style": "none", "bed_count": 0, "furniture_clusters": ["coffee_bar", "sofa_lounge"]},
            {"name": "东侧休眠舱", "role": "home", "floor_tile": "wood_floor", "rug_tile": "carpet_blue", "wall_tile": "white_wall", "roof_color": "#556ea6", "partition_style": "alcove", "bed_count": 2, "furniture_clusters": ["bed_bay", "desk_set", "storage_nook"]},
            {"name": "西侧休眠舱", "role": "home", "floor_tile": "wood_floor", "rug_tile": "carpet_pink", "wall_tile": "white_wall", "roof_color": "#6a5f94", "partition_style": "alcove", "bed_count": 2, "furniture_clusters": ["bed_bay", "sofa_lounge", "storage_nook"]},
        ],
    },
    "cozy_villa": {
        "name": "微风庭院起居馆",
        "palette_id": "warm_stardew",
        "ambient_phrase": "窗明几净的起居公馆，玄关客厅通向书屋、茶点角与暖木卧室。",
        "initial_hour": 14.5,
        "allowed_weathers": ["clear", "sakura", "cloudy", "rain"],
        "initial_weather": "clear",
        "hub": {
            "name": "起居会客厅", "role": "public", "floor_tile": "wood_floor", "rug_tile": "carpet_pink",
            "wall_tile": "wood_wall", "partition_style": "alcove", "bed_count": 0,
            "furniture_clusters": ["sofa_lounge", "desk_set", "bookshelf_wall", "storage_nook"],
        },
        "rooms": [
            {"name": "暖木主卧", "role": "home", "floor_tile": "wood_floor", "rug_tile": "tatami", "wall_tile": "wood_wall", "roof_color": "#c9647a", "partition_style": "alcove", "bed_count": 2, "furniture_clusters": ["bed_bay", "desk_set", "storage_nook"]},
            {"name": "客居和室", "role": "home", "floor_tile": "tatami", "rug_tile": "wood_floor", "wall_tile": "wood_wall", "roof_color": "#729968", "partition_style": "alcove", "bed_count": 2, "furniture_clusters": ["bed_bay", "sofa_lounge", "storage_nook"]},
            {"name": "花房茶点间", "role": "cafe", "floor_tile": "stone_tile", "rug_tile": "carpet_pink", "wall_tile": "white_wall", "roof_color": "#c9885b", "partition_style": "none", "bed_count": 0, "furniture_clusters": ["coffee_bar", "sofa_lounge", "desk_set"]},
            {"name": "木香藏书室", "role": "library", "floor_tile": "dark_wood", "rug_tile": "carpet_blue", "wall_tile": "brick_wall", "roof_color": "#5a827e", "partition_style": "wing_split", "bed_count": 0, "furniture_clusters": ["bookshelf_wall", "desk_set", "atelier_corner"]},
        ],
    },
}

_ROLE_DEFAULTS: dict[str, dict[str, Any]] = {
    "home": {
        "floor_tile": "wood_floor", "rug_tile": "tatami", "wall_tile": "wood_wall",
        "roof_color": "#c9647a", "partition_style": "alcove",
        "clusters": ["bed_bay", "desk_set", "storage_nook", "sofa_lounge"],
    },
    "cafe": {
        "floor_tile": "dark_wood", "rug_tile": "carpet_pink", "wall_tile": "white_wall",
        "roof_color": "#c9885b", "partition_style": "none",
        "clusters": ["coffee_bar", "sofa_lounge", "desk_set"],
    },
    "shop": {
        "floor_tile": "stone_tile", "rug_tile": "dark_wood", "wall_tile": "brick_wall",
        "roof_color": "#5e7ea6", "partition_style": "alcove",
        "clusters": ["shop_counter", "storage_nook", "desk_set"],
    },
    "library": {
        "floor_tile": "dark_wood", "rug_tile": "carpet_blue", "wall_tile": "wood_wall",
        "roof_color": "#5a827e", "partition_style": "wing_split",
        "clusters": ["bookshelf_wall", "desk_set", "atelier_corner", "sofa_lounge"],
    },
    "arcade": {
        "floor_tile": "carpet_blue", "rug_tile": "stone_tile", "wall_tile": "brick_wall",
        "roof_color": "#875fa3", "partition_style": "pillar",
        "clusters": ["arcade_row", "sofa_lounge", "desk_set"],
    },
    "public": {
        "floor_tile": "wood_floor", "rug_tile": "stone_tile", "wall_tile": "white_wall",
        "roof_color": "#728699", "partition_style": "pillar",
        "clusters": ["sofa_lounge", "desk_set", "bookshelf_wall", "storage_nook"],
    },
}


def _pick_floor(raw: Any, fallback: str) -> str:
    tid = str(raw or "").strip()
    return tid if tid in _FLOOR_TILES else fallback


def _pick_wall(raw: Any, fallback: str) -> str:
    tid = str(raw or "").strip()
    return tid if tid in _WALL_TILES else fallback


def _detect_indoor_theme(prompt_text: str) -> str:
    low = str(prompt_text or "").casefold()
    if any(k in low for k in ("画", "美术", "艺术", "雕塑", "工坊", "atelier", "studio", "gallery")):
        return "atelier"
    if any(k in low for k in ("雪", "温泉", "汤", "宿屋", "旅馆", "冬", "onsen", "ryokan")):
        return "onsen_inn"
    if any(k in low for k in ("学院", "校园", "社团", "教室", "排练", "高校", "academy", "school", "club")):
        return "academy"
    if any(k in low for k in ("霓虹", "赛博", "机房", "电脑", "studio", "cyber", "neon", "loft")):
        return "cyber_loft"
    if any(k in low for k in ("公馆", "庄园", "别墅", "洋馆", "钟楼", "档案", "值班", "manor", "villa", "mystery")):
        return "manor"
    return "cozy_villa"


def _rect_cells(x0: int, y0: int, rw: int, rh: int, tile_id: str) -> list[dict[str, Any]]:
    return [{"x": x0 + dx, "y": y0 + dy, "tile_id": tile_id} for dy in range(rh) for dx in range(rw)]


def _normalize_room_plan(raw: Mapping[str, Any] | None, *, fallback_name: str, default_role: str = "public", slot_idx: int = 0) -> dict[str, Any]:
    src = raw if isinstance(raw, Mapping) else {}
    name = clip_text(src.get("name"), 16, fallback_name)
    role = venue_semantics.normalize_venue_role(src.get("role"), category=src.get("category"), name=name)
    if not src.get("role") and not src.get("category") and role == "public" and default_role != "public":
        role = default_role
    rd = _ROLE_DEFAULTS.get(role, _ROLE_DEFAULTS["public"])
    floor_tile = _pick_floor(src.get("floor_tile") or src.get("interior_floor_tile"), str(rd["floor_tile"]))
    rug_tile = _pick_floor(src.get("rug_tile"), str(rd["rug_tile"]))
    if rug_tile == floor_tile:
        rug_tile = "tatami" if floor_tile != "tatami" else "carpet_blue"
    wall_tile = _pick_wall(src.get("wall_tile") or src.get("interior_wall_tile"), str(rd["wall_tile"]))
    pstyle = str(src.get("partition_style") or rd["partition_style"]).strip().lower()
    if pstyle not in VALID_PARTITIONS:
        pstyle = str(rd["partition_style"])
    clusters = [str(c).strip().lower() for c in (src.get("furniture_clusters") or rd["clusters"]) if str(c).strip()]
    if role == "home" and "bed_bay" not in clusters:
        clusters.insert(0, "bed_bay")
    if role == "cafe" and "coffee_bar" not in clusters:
        clusters.insert(0, "coffee_bar")
    if role == "shop" and "shop_counter" not in clusters:
        clusters.insert(0, "shop_counter")
    if role == "library" and "bookshelf_wall" not in clusters:
        clusters.insert(0, "bookshelf_wall")
    if role == "arcade" and "arcade_row" not in clusters:
        clusters.insert(0, "arcade_row")

    raw_beds = src.get("bed_count")
    try:
        bed_count = int(raw_beds) if raw_beds is not None else interior_designer.default_bed_count(role, name)
    except (TypeError, ValueError):
        bed_count = 1 if role == "home" else 0
    if role == "home":
        bed_count = max(1, min(4, bed_count))
    else:
        bed_count = max(0, min(4, bed_count))

    zone_slot = str(src.get("zone_slot") or ("nw", "ne", "sw", "se")[slot_idx % 4]).strip().lower()
    if zone_slot not in ("nw", "ne", "sw", "se"):
        zone_slot = ("nw", "ne", "sw", "se")[slot_idx % 4]

    roof = str(src.get("roof_color") or rd["roof_color"]).strip()
    if not (len(roof) == 7 and roof.startswith("#")):
        roof = str(rd["roof_color"])
    raw_conn = src.get("connect_to")
    if isinstance(raw_conn, str) and raw_conn.strip():
        connect_to = [clip_text(raw_conn, 24, "")]
    elif isinstance(raw_conn, (list, tuple)):
        connect_to = [clip_text(x, 24, "") for x in raw_conn[:4] if str(x or "").strip()]
    else:
        connect_to = []
    raw_ext_w = src.get("ext_w")
    raw_ext_h = src.get("ext_h")
    ext_w = clamp_int(raw_ext_w, 4, 10, 0) if raw_ext_w is not None else None
    ext_h = clamp_int(raw_ext_h, 4, 6, 0) if raw_ext_h is not None else None
    raw_scene = src.get("scene_items") if isinstance(src.get("scene_items"), list) else (
        src.get("custom_items") if isinstance(src.get("custom_items"), list) else []
    )
    scene_items = [dict(x) for x in raw_scene[:8] if isinstance(x, Mapping)]
    return {
        "room_id": str(src.get("room_id") or "").strip(),
        "room_shape": str(src.get("room_shape") or ""),
        **{k: src[k] for k in ("interior_width", "interior_height") if src.get(k) is not None},
        "location_id": str(src.get("location_id") or "").strip(),
        "name": name,
        "role": role,
        "category": venue_semantics.default_category_for_role(role),
        "zone_slot": zone_slot,
        "floor_tile": floor_tile,
        "rug_tile": rug_tile,
        "rug_style": str(src.get("rug_style") or "").strip().lower(),
        "wall_tile": wall_tile,
        "exterior_wall_tile": _pick_wall(src.get("exterior_wall_tile"), wall_tile),
        "roof_color": roof,
        "partition_style": pstyle,
        "bed_count": bed_count,
        "furniture_clusters": list(dict.fromkeys(clusters)),
        "scene_items": scene_items,
        "connect_to": connect_to,
        "ext_w": ext_w if (ext_w and ext_w >= 4) else None,
        "ext_h": ext_h if (ext_h and ext_h >= 4) else None,
        "interior_ambient": clip_text(src.get("interior_ambient") or src.get("description"), 60, f"「{name}」内部布置整齐，各处功能区清晰分明。"),
        "interior_palette": str(src.get("interior_palette") or "").strip(),
        "icon": clip_text(src.get("icon"), 4, venue_semantics.default_icon_for_role(role)),
        "note_text": clip_text(src.get("note_text"), 80, ""),
    }


def sanitize_map_plan(
    raw: Mapping[str, Any] | None,
    *,
    mode: str = "room",
    prompt_text: str = "",
    name: str = "",
    width: int | None = None,
    height: int | None = None,
    places: Sequence[Mapping[str, Any]] = (),
    resident_count: int = 0,
    initial_hour: float | None = None,
    building_count: int | None = None,
    palette_id: str = "",
) -> dict[str, Any]:
    """将 LLM 或预设地图方案归一化，确保地点 1:1 对齐与住宿人数床位充足。"""
    src = dict(raw) if isinstance(raw, Mapping) else {}
    clean_mode = str(mode or "room").strip().lower()
    if clean_mode not in VALID_MAP_MODES:
        clean_mode = "room"

    clean_prompt = clip_text(prompt_text or name, 240, "布置合理、有明显分区与生活气息的舞台")
    theme_hint = str(src.get("theme") or "").strip().lower()
    indoor_theme = theme_hint if theme_hint in _INDOOR_PRESETS else _detect_indoor_theme(clean_prompt)
    ipreset = _INDOOR_PRESETS[indoor_theme]

    clean_places = [dict(p) for p in (places or ()) if isinstance(p, Mapping) and (p.get("name") or p.get("location_id"))]
    residents = max(0, int(resident_count or 0))

    raw_hour = initial_hour if initial_hour is not None else src.get("initial_hour", ipreset["initial_hour"])
    try:
        resolved_hour = round(float(raw_hour) % 24.0, 2) if raw_hour is not None else None
    except (TypeError, ValueError):
        resolved_hour = float(ipreset["initial_hour"])

    if clean_mode == "town":
        tw = clamp_int(width if width is not None else src.get("width"), 28, 72, 48)
        th = clamp_int(height if height is not None else src.get("height"), 22, 52, 36)
        raw_bldgs = [dict(r) for r in (src.get("rooms") or src.get("buildings") or ()) if isinstance(r, Mapping)]
        town_seed = {
            **src,
            "name": name or src.get("name") or ipreset["name"],
            **({"palette_id": palette_id} if palette_id in _PALETTES else {}),
            **({"buildings": raw_bldgs} if raw_bldgs else {}),
        }
        desired_cnt = building_count if building_count is not None else max(5, min(8, len(clean_places) + 3 if clean_places else 6))
        if residents > 2:
            desired_cnt = max(desired_cnt, min(8, 3 + (residents + 1) // 2))
        town_spec = text_compiler._sanitize_town_spec(
            town_seed,
            width=tw,
            height=th,
            building_count=clamp_int(desired_cnt, 3, 8, 6),
            prompt_text=clean_prompt,
        )
        rooms = [_normalize_room_plan(b, fallback_name=str(b.get("name") or "建筑"), slot_idx=i) for i, b in enumerate(town_spec["buildings"])]
        for b_orig, r_norm in zip(town_spec["buildings"], rooms):
            if r_norm.get("ext_w"):
                b_orig["ext_w"] = r_norm["ext_w"]
            if r_norm.get("ext_h"):
                b_orig["ext_h"] = r_norm["ext_h"]
            if r_norm.get("scene_items"):
                b_orig["scene_items"] = r_norm["scene_items"]
            if r_norm.get("rug_style"):
                b_orig["rug_style"] = r_norm["rug_style"]
        hub_loc_id = ""
        # 将声明式场所清单精确映射到室外中枢与独立建筑房间（1:1 保留 location_id）
        used_idx: set[int] = set()
        remaining_places: list[dict[str, Any]] = []
        for p in clean_places:
            lid = str(p.get("location_id") or "").strip()
            pname = clip_text(p.get("name"), 16, lid or "场所")
            prole = venue_semantics.normalize_venue_role(p.get("venue_role") or p.get("role"), name=pname)
            if prole == "public" and not hub_loc_id:
                hub_loc_id = lid
                continue
            hit = next((i for i, r in enumerate(rooms) if i not in used_idx and r.get("location_id") == lid and lid), None)
            if hit is None:
                hit = next((i for i, r in enumerate(rooms) if i not in used_idx and not r.get("location_id") and r["role"] == prole), None)
            if hit is None:
                hit = next((i for i, r in enumerate(rooms) if i not in used_idx and not r.get("location_id")), None)
            if hit is not None:
                rooms[hit]["location_id"] = lid
                rooms[hit]["name"] = pname
                rooms[hit]["role"] = prole
                rooms[hit]["category"] = venue_semantics.default_category_for_role(prole)
                if prole == "home":
                    rooms[hit]["bed_count"] = max(2, rooms[hit]["bed_count"])
                    if "bed_bay" not in rooms[hit]["furniture_clusters"]:
                        rooms[hit]["furniture_clusters"].insert(0, "bed_bay")
                used_idx.add(hit)
            else:
                remaining_places.append(p)
        for p in remaining_places:
            if len(rooms) >= 8:
                break
            lid = str(p.get("location_id") or "").strip()
            pname = clip_text(p.get("name"), 16, lid or f"馆舍{len(rooms) + 1}")
            prole = venue_semantics.normalize_venue_role(p.get("venue_role") or p.get("role"), name=pname)
            rooms.append(_normalize_room_plan({**p, "location_id": lid, "name": pname, "role": prole}, fallback_name=pname, default_role=prole, slot_idx=len(rooms)))

        # 按 resident_count 保障镇内总床位足够
        target_beds = max(1, residents)
        home_rooms = [r for r in rooms if venue_semantics.is_lodging_building(r)]
        if not home_rooms and rooms:
            target = next((r for r in reversed(rooms) if not r.get("location_id")), rooms[0])
            target["role"] = "home"
            target["category"] = "home"
            target["bed_count"] = max(1, target["bed_count"])
            home_rooms = [target]
        cur_beds = sum(r["bed_count"] for r in home_rooms)
        while cur_beds < target_beds:
            upgraded = False
            for r in home_rooms:
                if r["bed_count"] < 3:
                    r["bed_count"] += 1
                    cur_beds += 1
                    upgraded = True
                    if cur_beds >= target_beds:
                        break
            if upgraded:
                continue
            if len(rooms) < 8:
                guest = _normalize_room_plan({"name": f"旅人客房{len(home_rooms) + 1}", "role": "home", "bed_count": min(3, target_beds - cur_beds)}, fallback_name="旅人客房", default_role="home", slot_idx=len(rooms))
                rooms.append(guest)
                home_rooms.append(guest)
                cur_beds += guest["bed_count"]
            else:
                for r in home_rooms:
                    if r["bed_count"] < 4 and cur_beds < target_beds:
                        r["bed_count"] += 1
                        cur_beds += 1
                break

        for r in rooms:
            int_w, int_h = (16, 12) if r["bed_count"] >= 2 else ((16, 11) if (tw >= 36 and r["role"] in ("home", "cafe", "library", "arcade")) else (14, 10))
            layout = interior_designer.activity_layout(r["role"], r["name"], r["bed_count"])
            int_w, int_h = interior_designer.activity_dimensions(layout, (int_w, int_h))
            r["interior_width"] = clamp_int(r.get("interior_width"), 8, 36, int_w)
            r["interior_height"] = clamp_int(r.get("interior_height"), 6, 28, int_h)
        town_spec["buildings"] = rooms
        return {
            "version": 1,
            "mode": "town",
            "theme": town_spec["theme"],
            "world_prompt": clean_prompt,
            "name": town_spec["name"],
            "ambient_phrase": town_spec["ambient_phrase"],
            "palette_id": town_spec["palette_id"],
            "initial_hour": resolved_hour,
            "width": tw,
            "height": th,
            "resident_count": residents,
            "hub_location_id": hub_loc_id,
            "town_spec": town_spec,
            "rooms": rooms,
        }

    # 单室套间（room）与多房连通建筑群（compound）
    pal = str(src.get("palette_id") or palette_id or ipreset["palette_id"]).strip()
    if pal not in _PALETTES:
        pal = str(ipreset["palette_id"])
    raw_weathers = [str(w).strip().lower() for w in (src.get("allowed_weathers") or ipreset["allowed_weathers"]) if str(w).strip().lower() in weather.VALID_WEATHERS]
    if not raw_weathers:
        raw_weathers = list(ipreset["allowed_weathers"])
    init_w = str(src.get("initial_weather") or ipreset["initial_weather"]).strip().lower()
    if init_w not in raw_weathers:
        init_w = raw_weathers[0]

    hub_raw = src.get("hub") if isinstance(src.get("hub"), Mapping) else dict(ipreset["hub"])
    hub = _normalize_room_plan(hub_raw, fallback_name=name or str(ipreset["hub"]["name"]), default_role=str(ipreset["hub"].get("role") or "public"))

    if clean_mode == "room":
        subject = f"{clean_prompt} {name or src.get('name') or ''}"
        explicit_hub = src.get("hub") if isinstance(src.get("hub"), Mapping) else {}
        if interior_designer.default_bed_count(hub["role"], subject) == 2 and "bed_count" not in explicit_hub:
            hub["bed_count"] = max(2, hub["bed_count"])
        layout = interior_designer.activity_layout(hub["role"], subject, hub["bed_count"])
        layout = layout or interior_designer.activity_layout(hub["role"], hub["name"], hub["bed_count"])
        if layout and not explicit_hub.get("partition_style"):
            hub["partition_style"] = ""
        if layout and not explicit_hub:
            hub["role"], fallback_name = {
                "cafe": ("cafe", "咖啡馆"), "bookshop": ("library", "书店"), "twin_room": ("home", "双人客房"),
                "ramen": ("cafe", "拉面屋"), "study": ("library", "书斋"),
                "onsen": ("home", "汤宿"), "hearth": ("public", "暖炉休息室"),
            }[layout]
            hub["category"] = venue_semantics.default_category_for_role(hub["role"])
            hub["name"] = clean_places[0]["name"] if clean_places and clean_places[0].get("name") else fallback_name
        default_w, default_h = interior_designer.activity_dimensions(layout, (18, 13))
        rw = clamp_int(width if width is not None else src.get("width"), MIN_SPACE_WIDTH, 32, default_w)
        rh = clamp_int(height if height is not None else src.get("height"), MIN_SPACE_HEIGHT, 24, default_h)
        needed_beds = max(1 if (hub["role"] == "home" or residents > 0) else 0, min(3, residents))
        if needed_beds > 0:
            hub["bed_count"] = max(hub["bed_count"], needed_beds)
            if "bed_bay" not in hub["furniture_clusters"]:
                hub["furniture_clusters"].insert(0, "bed_bay")
        if needed_beds >= 2 and not layout:
            if width is None and src.get("width") is None:
                rw = max(rw, 18)
            if height is None and src.get("height") is None:
                rh = max(rh, 13)
        hub["name"] = clip_text(name or src.get("name") or hub["name"], 16, "精致套间")
        hub["location_id"] = str(clean_places[0].get("location_id") or "") if clean_places else ""
        hub["location_ids"] = [str(p.get("location_id") or "") for p in clean_places if p.get("location_id")]
        return {
            "version": 1,
            "mode": "room",
            "theme": indoor_theme,
            "name": hub["name"],
            "ambient_phrase": clip_text(src.get("ambient_phrase") or ipreset["ambient_phrase"], 60),
            "palette_id": pal,
            "initial_hour": resolved_hour,
            "allowed_weathers": raw_weathers,
            "initial_weather": init_w,
            "world_prompt": clean_prompt,
            "width": rw,
            "height": rh,
            "resident_count": residents,
            "hub": hub,
            "rooms": [],
        }

    # compound：中央中枢大厅 + 3~8 间独立双向门洞子房（支持 connect_to 多跳拓扑）
    hw = clamp_int(width if width is not None else src.get("width"), 16, 32, 22)
    hh = clamp_int(height if height is not None else src.get("height"), 12, 22, 14)
    raw_rooms = list(src.get("rooms") or ipreset["rooms"])
    rooms = [_normalize_room_plan(r, fallback_name=f"厅室{i + 1}", slot_idx=i) for i, r in enumerate(raw_rooms[:8])]
    seen_names: set[str] = {hub["name"]}

    def _dedup(room: dict[str, Any]) -> None:
        base = room["name"]
        cand = base
        idx = 2
        while cand in seen_names:
            cand = clip_text(f"{base[:12]}{idx}", 16, f"房间{idx}")
            idx += 1
        room["name"] = cand
        seen_names.add(cand)

    # 先按 clean_places 精确映射（首个 public 归属中枢大厅，其余各得一间专属子房）
    hub_loc_id = ""
    mapped_rooms: list[dict[str, Any]] = []
    orig_to_new_name: dict[str, str] = {}
    if clean_places:
        pool = list(rooms)
        for p in clean_places:
            lid = str(p.get("location_id") or "").strip()
            pname = clip_text(p.get("name"), 16, lid or "厅室")
            prole = venue_semantics.normalize_venue_role(p.get("venue_role") or p.get("role"), name=pname)
            if prole == "public" and not hub_loc_id:
                hub_loc_id = lid
                hub["location_id"] = lid
                if p.get("name"):
                    orig_to_new_name[hub["name"]] = pname
                    hub["name"] = pname
                seen_names.add(hub["name"])
                continue
            matched = next((r for r in pool if (lid and r.get("location_id") == lid) or r["role"] == prole), None)
            if matched is None and pool:
                matched = pool[0]
            if matched is not None:
                old_name = matched["name"]
                pool.remove(matched)
                room = {**matched, "location_id": lid, "name": pname, "role": prole, "category": venue_semantics.default_category_for_role(prole)}
                orig_to_new_name[old_name] = pname
            else:
                room = _normalize_room_plan({"location_id": lid, "name": pname, "role": prole}, fallback_name=pname, default_role=prole, slot_idx=len(mapped_rooms))
            if prole == "home":
                room["bed_count"] = max(2 if residents >= 2 else 1, room["bed_count"])
                if "bed_bay" not in room["furniture_clusters"]:
                    room["furniture_clusters"].insert(0, "bed_bay")
            _dedup(room)
            if matched is not None:
                orig_to_new_name[matched["name"]] = room["name"]
            mapped_rooms.append(room)
        # 补充辅助功能房至至少 3 间
        for extra in pool:
            if len(mapped_rooms) >= 4:
                break
            extra_copy = {**extra, "location_id": ""}
            _dedup(extra_copy)
            orig_to_new_name[extra["name"]] = extra_copy["name"]
            mapped_rooms.append(extra_copy)
        rooms = mapped_rooms[:8]
    else:
        for r in rooms:
            old_n = r["name"]
            _dedup(r)
            orig_to_new_name[old_n] = r["name"]

    target_cnt = clamp_int(building_count if building_count is not None else max(4, len(rooms)), 3, 8, 4)
    for fallback_r in ipreset["rooms"]:
        if len(rooms) >= target_cnt:
            break
        cand_r = _normalize_room_plan({**fallback_r, "location_id": ""}, fallback_name=str(fallback_r["name"]), slot_idx=len(rooms))
        _dedup(cand_r)
        rooms.append(cand_r)
    rooms = rooms[:8]

    # 保障至少 1 间饮食补给房与足够容量的住宿房
    if len(rooms) >= 2 and not any(venue_semantics.is_food_building(r) for r in rooms):
        cafe_r = _normalize_room_plan({"name": "暖灯茶水间", "role": "cafe"}, fallback_name="茶水间", default_role="cafe", slot_idx=len(rooms))
        _dedup(cafe_r)
        if len(rooms) < 8:
            rooms.append(cafe_r)
        else:
            rooms[-1] = {**cafe_r, "location_id": rooms[-1].get("location_id") or ""}

    home_rooms = [r for r in rooms if venue_semantics.is_lodging_building(r)]
    if not home_rooms:
        lodging_r = _normalize_room_plan({"name": "客居宿舍", "role": "home", "bed_count": max(2, min(4, residents or 2))}, fallback_name="客居宿舍", default_role="home", slot_idx=len(rooms))
        _dedup(lodging_r)
        if len(rooms) < 8:
            rooms.append(lodging_r)
            home_rooms = [lodging_r]
        else:
            rooms[-1] = {**lodging_r, "location_id": rooms[-1].get("location_id") or ""}
            home_rooms = [rooms[-1]]

    target_beds = max(1, residents)
    cur_beds = sum(r["bed_count"] for r in home_rooms)
    while cur_beds < target_beds:
        upgraded = False
        for r in home_rooms:
            if r["bed_count"] < 3:
                r["bed_count"] += 1
                cur_beds += 1
                upgraded = True
                if cur_beds >= target_beds:
                    break
        if upgraded:
            continue
        if len(rooms) < 8:
            extra_home = _normalize_room_plan({"name": f"侧翼客房{len(home_rooms) + 1}", "role": "home", "bed_count": min(3, target_beds - cur_beds)}, fallback_name="侧翼客房", default_role="home", slot_idx=len(rooms))
            _dedup(extra_home)
            rooms.append(extra_home)
            home_rooms.append(extra_home)
            cur_beds += extra_home["bed_count"]
        else:
            for r in home_rooms:
                if r["bed_count"] < 4 and cur_beds < target_beds:
                    r["bed_count"] += 1
                    cur_beds += 1
            break

    # 归一化并修复多跳 connect_to 边：确保全部房间与中枢大厅构成连通无孤岛拓扑
    alias_to_idx: dict[str, int] = {"hub": -1, "中枢": -1, "lobby": -1, hub["name"]: -1}
    if hub.get("room_id"):
        alias_to_idx[hub["room_id"]] = -1
    if hub_loc_id:
        alias_to_idx[hub_loc_id] = -1
    for i, r in enumerate(rooms):
        alias_to_idx[r["name"]] = i
        if r.get("room_id"):
            alias_to_idx[r["room_id"]] = i
        if r.get("location_id"):
            alias_to_idx[r["location_id"]] = i
    for old_n, new_n in orig_to_new_name.items():
        if new_n in alias_to_idx and old_n not in alias_to_idx:
            alias_to_idx[old_n] = alias_to_idx[new_n]

    adj: dict[int, set[int]] = {-1: set(), **{i: set() for i in range(len(rooms))}}
    for i, r in enumerate(rooms):
        resolved_targets: list[str] = []
        raw_targets = r.get("connect_to") or []
        for t in raw_targets:
            t_idx = alias_to_idx.get(str(t).strip())
            if t_idx is None or t_idx == i:
                continue
            adj[i].add(t_idx)
            adj[t_idx].add(i)
            resolved_targets.append("hub" if t_idx == -1 else rooms[t_idx]["name"])
        if not resolved_targets:
            adj[i].add(-1)
            adj[-1].add(i)
            resolved_targets = ["hub"]
        r["connect_to"] = list(dict.fromkeys(resolved_targets))

    # BFS Connectivity Repair（防止孤立环与 hub 脱节）
    visited_nodes: set[int] = {-1}
    bfs_q: deque[int] = deque([-1])
    while bfs_q:
        cur = bfs_q.popleft()
        for nb in adj.get(cur, ()):
            if nb not in visited_nodes:
                visited_nodes.add(nb)
                bfs_q.append(nb)
    for i in range(len(rooms)):
        if i not in visited_nodes:
            adj[i].add(-1)
            adj[-1].add(i)
            if "hub" not in rooms[i]["connect_to"]:
                rooms[i]["connect_to"].append("hub")
            bfs_q.append(i)
            visited_nodes.add(i)
            while bfs_q:
                cur = bfs_q.popleft()
                for nb in adj.get(cur, ()):
                    if nb not in visited_nodes:
                        visited_nodes.add(nb)
                        bfs_q.append(nb)

    for r in rooms:
        layout = interior_designer.activity_layout(r["role"], r["name"], r["bed_count"])
        default_size = (18, 12) if r["bed_count"] >= 3 else (16, 11)
        int_w, int_h = interior_designer.activity_dimensions(layout, default_size)
        r["interior_width"] = clamp_int(r.get("interior_width"), 12, 22, int_w)
        r["interior_height"] = clamp_int(r.get("interior_height"), 10, 16, int_h)

    hub["location_id"] = hub_loc_id
    return {
        "version": 1,
        "mode": "compound",
        "theme": indoor_theme,
        "world_prompt": clean_prompt,
        "name": clip_text(name or src.get("name") or ipreset["name"], 16, ipreset["name"]),
        "ambient_phrase": clip_text(src.get("ambient_phrase") or ipreset["ambient_phrase"], 60),
        "palette_id": pal,
        "initial_hour": resolved_hour,
        "allowed_weathers": raw_weathers,
        "initial_weather": init_w,
        "width": hw,
        "height": hh,
        "resident_count": residents,
        "hub": hub,
        "rooms": rooms,
    }


def compile_map_plan(
    prompt_text: str = "",
    *,
    mode: str = "room",
    name: str = "",
    width: int | None = None,
    height: int | None = None,
    places: Sequence[Mapping[str, Any]] = (),
    resident_count: int = 0,
    initial_hour: float | None = None,
    building_count: int | None = None,
    palette_id: str = "",
    use_llm: bool = True,
    adapter_provider: Callable[[], Any] | None = None,
    timeout_sec: float = 18.0,
) -> dict[str, Any]:
    """统一地图生成调度入口：由 LLM 规划语义建筑与生活区，确定性编译落地。"""
    clean_mode = str(mode or "room").strip().lower()
    if clean_mode not in VALID_MAP_MODES:
        clean_mode = "room"
    clean_prompt = clip_text(prompt_text or name, 240, "布置合理、有明显分区与生活气息的像素舞台")

    parsed: dict[str, Any] | None = None
    used_llm = False
    if use_llm:
        place_hints = [
            {
                "location_id": str(p.get("location_id") or ""),
                "name": str(p.get("name") or ""),
                "role": str(p.get("venue_role") or p.get("role") or ""),
                "description": clip_text(p.get("description"), 60),
            }
            for p in (places or ())
            if isinstance(p, Mapping) and (p.get("name") or p.get("location_id"))
        ]
        user_msg = (
            f"空间模式：{clean_mode}\n"
            f"场景名称提示：{clip_text(name, 20) or '自动'}\n"
            f"场景描述：{clean_prompt}\n"
            f"常住/参演角色数 resident_count：{max(0, int(resident_count or 0))}\n"
            f"期望房间/建筑数：{building_count or '按场景自动(3-7间)'}\n"
            f"声明式地点清单 places：{place_hints or '无，请按题材自由设计合理厅室'}\n"
            "请严格只输出 JSON。"
        )
        try:
            raw = text_compiler._ask_llm_json(
                _MAP_PLAN_SYSTEM_PROMPT,
                user_msg,
                adapter_provider=adapter_provider,
                timeout_sec=timeout_sec,
                max_tokens=1800,
            )
            if isinstance(raw, Mapping) and (raw.get("rooms") or raw.get("buildings") or raw.get("hub")):
                parsed = dict(raw)
                used_llm = True
        except Exception:
            logger.debug("nanami_world: LLM 地图规划回退离线架构模板", exc_info=True)

    plan = sanitize_map_plan(
        parsed,
        mode=clean_mode,
        prompt_text=clean_prompt,
        name=name,
        width=width,
        height=height,
        places=places,
        resident_count=resident_count,
        initial_hour=initial_hour,
        building_count=building_count,
        palette_id=palette_id,
    )
    if use_llm and plan.get("mode") in ("room", "compound"):
        batch_rooms: list[dict[str, Any]] = []
        hub_plan = plan.get("hub")
        if isinstance(hub_plan, dict) and not hub_plan.get("scene_items"):
            batch_rooms.append({
                "room_id": str(hub_plan.get("location_id") or hub_plan.get("name") or "hub"),
                "name": hub_plan.get("name"),
                "kind": hub_plan.get("role"),
                "w": plan.get("width", 18),
                "h": plan.get("height", 13),
                "brief": hub_plan.get("interior_ambient"),
                "_ref": hub_plan,
            })
        for rm in (plan.get("rooms") or []):
            if isinstance(rm, dict) and not rm.get("scene_items"):
                batch_rooms.append({
                    "room_id": str(rm.get("location_id") or rm.get("name") or f"room_{len(batch_rooms)}"),
                    "name": rm.get("name"),
                    "kind": rm.get("role"),
                    "w": rm.get("interior_width", 16),
                    "h": rm.get("interior_height", 11),
                    "brief": rm.get("interior_ambient"),
                    "_ref": rm,
                })
        if batch_rooms:
            try:
                sg_map = interior_designer.compile_scene_graphs_batch(
                    batch_rooms,
                    world_prompt=clean_prompt,
                    adapter_provider=adapter_provider,
                    timeout_sec=min(timeout_sec, 10.0),
                )
                for br in batch_rooms:
                    sg_entry = sg_map.get(br["room_id"])
                    if isinstance(sg_entry, Mapping):
                        target_rm = br["_ref"]
                        if sg_entry.get("custom_items"):
                            target_rm["scene_items"] = list(sg_entry["custom_items"])
                        if sg_entry.get("partition_style") in VALID_PARTITIONS:
                            target_rm["partition_style"] = sg_entry["partition_style"]
                        if sg_entry.get("rug_style"):
                            target_rm["rug_style"] = sg_entry["rug_style"]
            except Exception:
                logger.debug("nanami_world: batch scene graph fallback", exc_info=True)
    return {"map_plan": plan, "used_llm": used_llm}


def _blueprint_footprint(engine: Any, bp_id: str) -> tuple[int, int, bool, str]:
    bp = engine.all_blueprints().get(bp_id) or {}
    fp = bp.get("footprint") or [1, 1]
    return int(fp[0]), int(fp[1]), bool(bp.get("solid", False)), str(bp.get("placement") or "any")


def populate_architectural_interior(
    engine: Any,
    space_id: str,
    *,
    role: str = "public",
    building_name: str = "空间",
    floor_tile: str = "wood_floor",
    rug_tile: str = "carpet_blue",
    wall_tile: str = "wood_wall",
    partition_style: str = "",
    room_shape: str = "",
    bed_count: int = 0,
    furniture_clusters: Sequence[str] = (),
    note_text: str = "",
    door_coords: Sequence[tuple[int, int]] = (),
    variant: int = 0,
    build_perimeter_bottom_wall: bool = True,
    world_prompt: str = "",
    scene_items: Sequence[Mapping[str, Any]] = (),
    rug_style: str = "",
    brief: str = "",
    detail: str = "",
    seed: int | None = None,
    used_landmarks: set[str] | None = None,
    is_standalone: bool = False,
) -> dict[str, Any]:
    """根据建筑语义与几何约束，调用 Holodeck 场景图规划器与 3 阶段 CSP 求解器完成室内布置。"""
    with engine._lock:
        sp = engine._world["spaces"].get(space_id)
        if sp is None:
            raise ValueError(f"空间不存在：{space_id}")
        w, h = int(sp["width"]), int(sp["height"])
        wall_rows = int(sp.get("wall_rows", 1))
        pal = str(sp.get("palette_id") or "warm_stardew")

    norm_role = venue_semantics.normalize_venue_role(role, name=building_name)
    rd = _ROLE_DEFAULTS.get(norm_role, _ROLE_DEFAULTS["public"])
    ft = _pick_floor(floor_tile, str(rd["floor_tile"]))
    rt = _pick_floor(rug_tile, str(rd["rug_tile"]))
    if rt == ft:
        rt = "tatami" if ft != "tatami" else "carpet_blue"
    wt = _pick_wall(wall_tile, str(rd["wall_tile"]))
    clean_name = clip_text(building_name, 16, "房间")
    note = clip_text(note_text, 80, "")

    doors = {(int(dx), int(dy)) for dx, dy in door_coords}
    tile_ops: list[dict[str, Any]] = []
    if wall_rows > 0:
        tile_ops.extend(_rect_cells(0, 0, w, wall_rows, wt))
    tile_ops.extend(_rect_cells(0, wall_rows, w, max(1, h - wall_rows), ft))

    if build_perimeter_bottom_wall and h >= 5:
        for sx in range(w):
            is_door_col = any(dx == sx and dy == h - 1 for dx, dy in doors)
            tile_ops.append({"x": sx, "y": h - 1, "tile_id": ft if is_door_col else wt})

    top_y = max(1, wall_rows)
    bot_y = h - 2 if build_perimeter_bottom_wall else h - 1
    zones_raw: list[dict[str, Any]] = []
    if w >= 12 and bot_y - top_y >= 5:
        left_w = max(4, (w - 2) // 2)
        right_x = min(w - 4, left_w + 2)
        zone_h = max(3, min(5, bot_y - top_y - 2))
        right_rug_w = min(5, max(3, w - 1 - right_x))
        zones_raw = [
            {"zone_id": "west_corner", "name": "西侧功能区", "x": 1, "y": top_y, "w": min(5, left_w), "h": zone_h, "color": "#8cb8de", "description": "独立布置了家具与地毯的西侧活动区。"},
            {"zone_id": "east_corner", "name": "东侧功能区", "x": w - 1 - right_rug_w, "y": top_y, "w": right_rug_w, "h": zone_h, "color": "#e8b48b", "description": "适合会客、阅读或休息的东侧区域。"},
        ]

    for dx, dy in doors:
        if dy >= wall_rows:
            tile_ops.append({"x": dx, "y": dy, "tile_id": ft})

    engine.paint_tiles(space_id, tile_ops)

    clusters = [str(c).strip().lower() for c in (furniture_clusters or rd["clusters"]) if str(c).strip()]
    if norm_role == "home" and bed_count <= 0:
        bed_count = 1
    effective_beds = max(0, int(bed_count))

    extra_items = [dict(x) for x in (scene_items or ()) if isinstance(x, Mapping)]
    if note:
        has_note_book = any(x.get("blueprint_id") == "default_notebook" for x in extra_items)
        if not has_note_book:
            extra_items.append({
                "ref": "room_note",
                "blueprint_id": "default_notebook",
                "name": "货品账本" if norm_role == "shop" else "室内记录本",
                "relation": "on_surface",
                "note_text": note,
            })

    resolved_seed = (
        int(seed)
        if seed is not None
        else (random.SystemRandom().randint(1, 2_000_000_000) ^ zlib.crc32(f"{space_id}|{clean_name}|{variant}".encode("utf-8")))
    )
    room_spec: dict[str, Any] = {
        "space_id": space_id,
        "name": clean_name,
        "kind": norm_role,
        "role": norm_role,
        "is_standalone": bool(is_standalone),
        "brief": brief or note or f"「{clean_name}」室内场景",
        "detail": detail,
        "palette_id": pal,
        "w": w,
        "h": h,
        "bed_count": effective_beds,
        "partition_style": partition_style,
        "room_shape": room_shape,
        "rug_style": rug_style,
        "floor_tile": ft,
        "rug_tile": rt,
        "wall_tile": wt,
        "furniture_clusters": clusters,
        "scene_items": extra_items,
    }
    act_layout = interior_designer.activity_layout(norm_role, clean_name, effective_beds)
    if not is_standalone and not room_shape and not act_layout and w >= 11 and h >= 9 and len(doors) <= 2:
        mf_info = building_architect.populate_multiroom_floor(
            engine,
            space_id,
            role=norm_role,
            building_name=clean_name,
            floor_index=1,
            floor_tile=ft,
            rug_tile=rt,
            wall_tile=wt,
            bed_count=effective_beds,
            note_text=note,
            door_coords=sorted(doors),
            build_perimeter_bottom_wall=build_perimeter_bottom_wall,
            world_prompt=world_prompt,
            scene_items=extra_items,
            brief=brief,
            detail=detail,
            seed=resolved_seed,
            used_landmarks=used_landmarks,
        )
        return {
            "space_id": space_id,
            "placed_count": int(mf_info.get("placed_count") or 0),
            "room_shape": str(mf_info.get("layout_mode") or "multiroom"),
            "partition_style": partition_style or "multiroom",
            "rug_style": rug_style or "center",
            "theme_key": interior_designer.infer_theme_key(clean_name, pal),
        }

    sg = interior_designer.design_room_scene_graph(
        room_spec,
        world_prompt=world_prompt,
        seed=resolved_seed,
        used_landmarks=used_landmarks,
    )
    placed_info = interior_designer.solve_and_place_scene_graph(
        engine,
        space_id,
        room_spec,
        sg,
        door_cells=doors,
        seed=resolved_seed,
    )
    zones = placed_info.get("zones") or zones_raw
    if zones:
        with engine._lock:
            engine._world["spaces"][space_id]["zones"] = normalize_zones(zones, w, h)
            engine._save_world_locked()
    return {
        "space_id": space_id,
        "placed_count": int(placed_info.get("placed_count") or 0),
        "room_shape": str(placed_info.get("room_shape") or "rect"),
        "partition_style": str(placed_info.get("partition_style") or partition_style),
        "rug_style": str(placed_info.get("rug_style") or rug_style),
        "theme_key": str(placed_info.get("theme_key") or ""),
    }


def build_architectural_room(
    engine: Any,
    map_plan: Mapping[str, Any],
    *,
    room_id: str = "",
    parent_space_id: str = "",
    opaque_minds: bool = False,
    isolated_persona: bool = False,
    switch_active: bool = True,
) -> dict[str, Any]:
    """根据 MapPlan 创建带内室分区与家具簇的独立建筑级单间。"""
    hub = dict(map_plan.get("hub") or {})
    w = clamp_int(map_plan.get("width"), MIN_SPACE_WIDTH, MAX_SPACE_WIDTH, 18)
    h = clamp_int(map_plan.get("height"), MIN_SPACE_HEIGHT, MAX_SPACE_HEIGHT, 13)
    pal = str(map_plan.get("palette_id") or "warm_stardew")
    ambient = clip_text(map_plan.get("ambient_phrase") or hub.get("interior_ambient"), 60)

    sp = engine.create_space(
        name=clip_text(map_plan.get("name") or hub.get("name"), 16, "精致套间"),
        width=w,
        height=h,
        wall_rows=1,
        space_kind="room",
        parent_space_id=str(parent_space_id or "").strip(),
        hub_location_id=str(hub.get("location_id") or ""),
        ambient_phrase=ambient,
        palette_id=pal,
        fixed_space_id=str(room_id or "").strip(),
        switch_active=switch_active,
        opaque_minds=opaque_minds,
        isolated_persona=isolated_persona,
    )
    sid = sp["space_id"]
    if map_plan.get("initial_hour") is not None:
        engine.set_realm_initial_hour(sid, float(map_plan["initial_hour"]))
    populate_architectural_interior(
        engine,
        sid,
        role=str(hub.get("role") or "home"),
        building_name=sp["name"],
        floor_tile=str(hub.get("floor_tile") or "wood_floor"),
        rug_tile=str(hub.get("rug_tile") or "tatami"),
        wall_tile=str(hub.get("wall_tile") or "wood_wall"),
        partition_style=str(hub.get("partition_style") or ""),
        room_shape=str(hub.get("room_shape") or ""),
        bed_count=int(hub.get("bed_count") or 0),
        furniture_clusters=hub.get("furniture_clusters") or (),
        note_text=str(hub.get("note_text") or ""),
        build_perimeter_bottom_wall=True,
        world_prompt=str(map_plan.get("world_prompt") or ""),
        scene_items=hub.get("scene_items") or (),
        rug_style=str(hub.get("rug_style") or ""),
        brief=str(hub.get("interior_ambient") or ""),
        seed=map_plan.get("seed"),
        is_standalone=True,
    )
    return {"space_id": sid, "name": sp["name"], "mode": "room", "created": True, "rooms": []}


def _compound_door_slots(w: int, h: int, count: int) -> list[tuple[int, int, int, int, str]]:
    """在多房建筑群或子房四周墙面生成互不冲突的门洞槽位 (dx, dy, ret_x, ret_y, zone_id)。"""
    slots: list[tuple[int, int, int, int, str]] = [
        (max(3, w // 4), 0, max(3, w // 4), 1, "north_west"),
        (min(w - 4, (w * 3) // 4), 0, min(w - 4, (w * 3) // 4), 1, "north_east"),
        (0, max(3, h // 3), 1, max(3, h // 3), "west_wing"),
        (w - 1, max(3, h // 3), w - 2, max(3, h // 3), "east_wing"),
        (0, min(h - 3, (h * 2) // 3), 1, min(h - 3, (h * 2) // 3), "south_west"),
        (w - 1, min(h - 3, (h * 2) // 3), w - 2, min(h - 3, (h * 2) // 3), "south_east"),
        (w // 2, 0, w // 2, 1, "north_center"),
        (w // 2, h - 1, w // 2, h - 2, "south_exit"),
    ]
    return slots[: max(1, min(len(slots), count))]


def _subroom_door_slots(w: int, h: int) -> list[tuple[int, int, int, int, str]]:
    """子房除南侧入口外的扩展门洞槽位，支持大厅 -> 书房 -> 密室等多跳门图。"""
    return [
        (max(3, w // 4), 0, max(3, w // 4), 1, "north_west"),
        (min(w - 4, (w * 3) // 4), 0, min(w - 4, (w * 3) // 4), 1, "north_east"),
        (0, max(3, h // 3), 1, max(3, h // 3), "west_wing"),
        (w - 1, max(3, h // 3), w - 2, max(3, h // 3), "east_wing"),
        (0, min(h - 3, (h * 2) // 3), 1, min(h - 3, (h * 2) // 3), "south_west"),
        (w - 1, min(h - 3, (h * 2) // 3), w - 2, min(h - 3, (h * 2) // 3), "south_east"),
        (w // 2, 0, w // 2, 1, "north_center"),
    ]


def build_compound_world(
    engine: Any,
    map_plan: Mapping[str, Any],
    *,
    hub_id: str = "",
    opaque_minds: bool = False,
    isolated_persona: bool = False,
    switch_active: bool = True,
) -> dict[str, Any]:
    """构建室内多房间双向连通建筑群（支持星型与任意 connect_to 多跳树/图结构）。"""
    hub_cfg = dict(map_plan.get("hub") or {})
    rooms_cfg = [dict(r) for r in (map_plan.get("rooms") or ()) if isinstance(r, Mapping)]
    hw = clamp_int(map_plan.get("width"), 16, 32, 22)
    hh = clamp_int(map_plan.get("height"), 12, 22, 14)
    pal = str(map_plan.get("palette_id") or "warm_stardew")
    world_prompt = str(map_plan.get("world_prompt") or map_plan.get("ambient_phrase") or "")
    clean_name = clip_text(map_plan.get("name") or hub_cfg.get("name"), 16, "公馆主厅")

    with engine._lock:
        base_id = str(hub_id or "").strip() or slugify(clean_name, prefix="compound")
        resolved_hid = base_id
        suffix = 2
        while resolved_hid in engine._world["spaces"]:
            resolved_hid = f"{base_id}_{suffix}"
            suffix += 1

    with engine.batch_mutations():
        hub_sp = engine.create_space(
            name=clip_text(hub_cfg.get("name") or clean_name, 16, clean_name),
            width=hw,
            height=hh,
            wall_rows=1,
            space_kind="room",
            hub_location_id=str(hub_cfg.get("location_id") or ""),
            ambient_phrase=clip_text(map_plan.get("ambient_phrase") or hub_cfg.get("interior_ambient"), 60),
            palette_id=pal,
            fixed_space_id=resolved_hid,
            switch_active=switch_active,
            opaque_minds=opaque_minds,
            isolated_persona=isolated_persona,
        )
        if map_plan.get("initial_hour") is not None:
            engine.set_realm_initial_hour(resolved_hid, float(map_plan["initial_hour"]))

        # 1. 解析多跳连接边并用 BFS 确定各房距中枢深度与主父节点
        alias_to_idx: dict[str, int] = {"hub": -1, "中枢": -1, "lobby": -1, hub_sp["name"]: -1, str(hub_cfg.get("name") or ""): -1}
        if hub_cfg.get("room_id"):
            alias_to_idx[str(hub_cfg["room_id"])] = -1
        if hub_cfg.get("location_id"):
            alias_to_idx[str(hub_cfg["location_id"])] = -1
        for i, rc in enumerate(rooms_cfg):
            rname = clip_text(rc.get("name"), 16, f"房间{i + 1}")
            rc["_resolved_name"] = rname
            alias_to_idx[rname] = i
            if rc.get("room_id"):
                alias_to_idx[str(rc["room_id"])] = i
            if rc.get("location_id"):
                alias_to_idx[str(rc["location_id"])] = i

        raw_edges: list[tuple[int, int]] = []
        seen_pairs: set[tuple[int, int]] = set()
        for i, rc in enumerate(rooms_cfg):
            conns = rc.get("connect_to") or []
            linked = False
            for target in conns:
                t_idx = alias_to_idx.get(str(target).strip())
                if t_idx is None or t_idx == i:
                    continue
                pair = (min(i, t_idx), max(i, t_idx))
                if pair not in seen_pairs:
                    seen_pairs.add(pair)
                    raw_edges.append(pair)
                linked = True
            if not linked:
                pair = (-1, i)
                if pair not in seen_pairs:
                    seen_pairs.add(pair)
                    raw_edges.append(pair)

        adj: dict[int, list[int]] = {-1: [], **{i: [] for i in range(len(rooms_cfg))}}
        for u, v in raw_edges:
            adj[u].append(v)
            adj[v].append(u)

        depth: dict[int, int] = {-1: 0}
        parent_of: dict[int, int] = {}
        q_bfs: deque[int] = deque([-1])
        while q_bfs:
            cur = q_bfs.popleft()
            for nb in adj.get(cur, ()):
                if nb not in depth:
                    depth[nb] = depth[cur] + 1
                    parent_of[nb] = cur
                    q_bfs.append(nb)
        for i in range(len(rooms_cfg)):
            if i not in depth:
                depth[i] = 1
                parent_of[i] = -1
                raw_edges.append((-1, i))

        # 2. 先批量创建全部房间 space，以获知精确宽高并预留所有门槽
        node_space_ids: dict[int, str] = {-1: resolved_hid}
        node_dims: dict[int, tuple[int, int]] = {-1: (hw, hh)}
        node_spaces: dict[int, dict[str, Any]] = {-1: hub_sp}
        has_floor_tags = len(rooms_cfg) >= 4
        if has_floor_tags:
            with engine._lock:
                hub_live_tag = engine._world["spaces"][resolved_hid]
                hub_live_tag["floor_index"] = 1
                hub_live_tag["floor_label"] = "1F"
                hub_live_tag["building_group_id"] = resolved_hid
        for idx, rcfg in enumerate(rooms_cfg):
            rname = str(rcfg["_resolved_name"])
            role = venue_semantics.normalize_venue_role(rcfg.get("role"), name=rname)
            beds = clamp_int(rcfg.get("bed_count"), 0, 8, interior_designer.default_bed_count(role, rname))
            rcfg["bed_count"] = beds
            layout = interior_designer.activity_layout(role, rname, beds)
            default_w, default_h = interior_designer.activity_dimensions(layout, (16, 11))
            iw = clamp_int(rcfg.get("interior_width"), 12, 22, default_w)
            ih = clamp_int(rcfg.get("interior_height"), 10, 16, default_h)
            int_sid_base = f"{resolved_hid}__{slugify(rname, prefix=str(idx + 1))}"
            int_sid = int_sid_base
            sub_s = 2
            with engine._lock:
                while int_sid in engine._world["spaces"]:
                    int_sid = f"{int_sid_base}_{sub_s}"
                    sub_s += 1
            p_node = parent_of[idx]
            p_sid = node_space_ids.get(p_node, resolved_hid)
            p_name = hub_sp["name"] if p_node == -1 else str(rooms_cfg[p_node]["_resolved_name"])
            rname_cf = rname.casefold()
            r_fi = 0
            r_flbl = ""
            r_grp = ""
            if any(k in rname_cf for k in ("地下", "地窖", "b1", "酒窖", "cellar")):
                r_fi = -1
            elif any(k in rname_cf for k in ("二楼", "2f", "二层", "阁楼", "楼上", "attic")):
                r_fi = 2
            elif has_floor_tags:
                r_fi = 1
            if r_fi != 0:
                r_flbl = building_architect.floor_label(r_fi)
                r_grp = resolved_hid
                if has_floor_tags:
                    with engine._lock:
                        engine._world["spaces"][resolved_hid]["floor_index"] = 1
                        engine._world["spaces"][resolved_hid]["floor_label"] = "1F"
                        engine._world["spaces"][resolved_hid]["building_group_id"] = resolved_hid
            int_sp = engine.create_space(
                name=rname,
                width=iw,
                height=ih,
                wall_rows=1,
                space_kind="building",
                parent_space_id=p_sid,
                district=hub_sp["name"],
                ambient_phrase=clip_text(rcfg.get("interior_ambient"), 60, f"「{rname}」内部陈设整齐，推门可回到「{p_name}」。"),
                palette_id=str(rcfg.get("interior_palette") or pal) if str(rcfg.get("interior_palette") or "") in _PALETTES else pal,
                floor_index=r_fi,
                floor_label=r_flbl,
                building_group_id=r_grp,
                fixed_space_id=int_sid,
                switch_active=False,
            )
            node_space_ids[idx] = int_sid
            node_dims[idx] = (iw, ih)
            node_spaces[idx] = int_sp

        # 3. 为每条双向边预分配互不重迭的两端门洞槽位
        hub_pool = list(_compound_door_slots(hw, hh, 8))
        node_out_slots: dict[int, list[tuple[int, int, int, int, str]]] = {
            -1: hub_pool,
            **{i: list(_subroom_door_slots(*node_dims[i])) for i in range(len(rooms_cfg))},
        }
        node_used_south: set[int] = set()
        wired_edges: list[tuple[int, int, tuple[int, int, int, int, str], tuple[int, int, int, int, str]]] = []
        door_coords_by_node: dict[int, list[tuple[int, int]]] = {-1: [], **{i: [] for i in range(len(rooms_cfg))}}
        raw_cseed = map_plan.get("seed")
        c_rng = random.Random(int(raw_cseed) if raw_cseed is not None else random.SystemRandom().randint(1, 2_000_000_000))

        ordered_edges = sorted(raw_edges, key=lambda e: (min(depth.get(e[0], 0), depth.get(e[1], 0)), e[0], e[1]))
        for u, v in ordered_edges:
            if depth.get(u, 0) > depth.get(v, 0) or (depth.get(u, 0) == depth.get(v, 0) and v == -1):
                u, v = v, u
            if not node_out_slots[u]:
                continue
            slot_u = node_out_slots[u].pop(0)
            vw, vh = node_dims[v]
            if v >= 0 and v not in node_used_south and parent_of.get(v) == u:
                room = rooms_cfg[v]
                layout = interior_designer.activity_layout(str(room.get("role") or ""), str(room.get("name") or ""), room.get("bed_count") or 0)
                entry_x = interior_designer.interior_entry_x(layout, vw, c_rng.randrange(2))
                slot_v = (entry_x, vh - 1, entry_x, vh - 2, "south_entry")
                node_used_south.add(v)
            elif node_out_slots[v]:
                slot_v = node_out_slots[v].pop(0)
            else:
                slot_v = (vw // 2, vh - 1, vw // 2, vh - 2, "south_entry")
            door_coords_by_node[u].append((slot_u[0], slot_u[1]))
            door_coords_by_node[v].append((slot_v[0], slot_v[1]))
            wired_edges.append((u, v, slot_u, slot_v))

        # 4. 一次性布置全部房间的地板、壁挂、隔断与家具（避开所有预分配门槽，共享全馆唯一地标预算）
        used_landmarks: set[str] = set()
        populate_architectural_interior(
            engine,
            resolved_hid,
            role=str(hub_cfg.get("role") or "public"),
            building_name=hub_sp["name"],
            floor_tile=str(hub_cfg.get("floor_tile") or "dark_wood"),
            rug_tile=str(hub_cfg.get("rug_tile") or "carpet_pink"),
            wall_tile=str(hub_cfg.get("wall_tile") or "wood_wall"),
            partition_style=str(hub_cfg.get("partition_style") or "pillar"),
            room_shape=str(hub_cfg.get("room_shape") or ""),
            bed_count=int(hub_cfg.get("bed_count") or 0),
            furniture_clusters=hub_cfg.get("furniture_clusters") or (),
            note_text=str(hub_cfg.get("note_text") or ""),
            door_coords=door_coords_by_node[-1],
            build_perimeter_bottom_wall=True,
            world_prompt=world_prompt,
            scene_items=hub_cfg.get("scene_items") or (),
            rug_style=str(hub_cfg.get("rug_style") or ""),
            brief=str(hub_cfg.get("interior_ambient") or ""),
            seed=c_rng.randint(1, 2_000_000_000),
            used_landmarks=used_landmarks,
        )
        for idx, rcfg in enumerate(rooms_cfg):
            rname = str(rcfg["_resolved_name"])
            rrole = venue_semantics.normalize_venue_role(rcfg.get("role"), name=rname)
            populate_architectural_interior(
                engine,
                node_space_ids[idx],
                role=rrole,
                building_name=rname,
                floor_tile=str(rcfg.get("floor_tile") or "wood_floor"),
                rug_tile=str(rcfg.get("rug_tile") or "tatami"),
                wall_tile=str(rcfg.get("wall_tile") or "wood_wall"),
                partition_style=str(rcfg.get("partition_style") or "alcove"),
                room_shape=str(rcfg.get("room_shape") or ""),
                bed_count=int(rcfg.get("bed_count") or 0),
                furniture_clusters=rcfg.get("furniture_clusters") or (),
                note_text=str(rcfg.get("note_text") or ""),
                door_coords=door_coords_by_node[idx],
                variant=idx,
                build_perimeter_bottom_wall=True,
                world_prompt=world_prompt,
                scene_items=rcfg.get("scene_items") or (),
                rug_style=str(rcfg.get("rug_style") or ""),
                brief=str(rcfg.get("interior_ambient") or ""),
                seed=c_rng.randint(1, 2_000_000_000),
                used_landmarks=used_landmarks,
            )

        # 5. 逐边铺设双向门洞实体，并把每个门所在房间记为子房直接宿主 metadata
        buildings_by_sid: dict[str, list[dict[str, Any]]] = {resolved_hid: []}
        created_rooms: list[dict[str, Any]] = []
        recorded_nodes: set[int] = set()
        for u, v, slot_u, slot_v in wired_edges:
            sid_u = node_space_ids[u]
            sid_v = node_space_ids[v]
            name_u = hub_sp["name"] if u == -1 else str(rooms_cfg[u]["_resolved_name"])
            name_v = hub_sp["name"] if v == -1 else str(rooms_cfg[v]["_resolved_name"])
            ux, uy, ret_ux, ret_uy, zid_u = slot_u
            vx, vy, ret_vx, ret_vy, _ = slot_v

            floor_u = _pick_floor((hub_cfg if u == -1 else rooms_cfg[u]).get("floor_tile"), "dark_wood")
            floor_v = _pick_floor((hub_cfg if v == -1 else rooms_cfg[v]).get("floor_tile"), "wood_floor")
            if uy >= 1:
                engine.paint_tiles(sid_u, [{"x": ux, "y": uy, "tile_id": floor_u}])
            if vy >= 1:
                engine.paint_tiles(sid_v, [{"x": vx, "y": vy, "tile_id": floor_v}])

            door_u = engine.place_from_blueprint("default_door", sid_u, ux, uy, delivery_mode="silent")
            engine.update_entity_spec(
                door_u["entity_id"],
                name=clip_text(f"{name_v}之门", 12, "内室木门"),
                brief=clip_text(f"通往「{name_v}」的木门", 25),
                detail=clip_text(f"推开这扇门就能进入「{name_v}」。", 60),
                traits={"portal": {"target_space_id": sid_v, "target_x": ret_vx, "target_y": ret_vy, "is_open": False}},
            )

            door_v = engine.place_from_blueprint("default_door", sid_v, vx, vy, delivery_mode="silent")
            engine.update_entity_spec(
                door_v["entity_id"],
                name="回廊木门" if u == -1 else clip_text(f"回{name_u}", 12, "回廊木门"),
                brief=clip_text(f"推开回到「{name_u}」", 25),
                detail=clip_text(f"推开这扇门，就能走回「{name_u}」。", 60),
                traits={"portal": {"target_space_id": sid_u, "target_x": ret_ux, "target_y": ret_uy, "is_open": False}},
            )

            if v >= 0 and v not in recorded_nodes and parent_of.get(v) == u:
                rcfg = rooms_cfg[v]
                rrole = venue_semantics.normalize_venue_role(rcfg.get("role"), name=name_v)
                meta = {
                    "building_id": f"room_{v + 1}_{slugify(name_v, prefix='rm')}"[:32],
                    "name": name_v,
                    "zone_id": zid_u,
                    "x": ux, "y": uy, "w": 1, "h": 1,
                    "door_x": ux, "door_y": uy,
                    "roof_color": str(rcfg.get("roof_color") or "#8a7060"),
                    "interior_space_id": sid_v,
                    "category": str(rcfg.get("category") or venue_semantics.default_category_for_role(rrole)),
                    "role": rrole,
                    "restock_profile": venue_semantics.restock_profile_for_building({"role": rrole, "name": name_v}),
                    "affordances": venue_semantics.building_affordance_hints({"role": rrole, "name": name_v}),
                    "icon": str(rcfg.get("icon") or venue_semantics.default_icon_for_role(rrole)),
                    "location_id": str(rcfg.get("location_id") or ""),
                    "bed_count": int(rcfg.get("bed_count") or 0),
                    "parent_space_id": sid_u,
                }
                buildings_by_sid.setdefault(sid_u, []).append(meta)
                created_rooms.append({**meta, "space": node_spaces[v]})
                recorded_nodes.add(v)

        with engine._lock:
            hub_live = engine._world["spaces"][resolved_hid]
            for host_sid, b_list in buildings_by_sid.items():
                host_sp = engine._world["spaces"][host_sid]
                host_sp["buildings"] = normalize_buildings(b_list, int(host_sp["width"]), int(host_sp["height"]))
            sim_now = float(engine._world.get("simulation_minutes") or 0.0)
            allowed_w = list(map_plan.get("allowed_weathers") or weather.DEFAULT_ALLOWED_WEATHERS)
            init_w = str(map_plan.get("initial_weather") or allowed_w[0])
            engine._world.setdefault("weather_by_realm", {})[resolved_hid] = weather.default_weather_state(
                sim_now, mode="auto", current=init_w, allowed=allowed_w,
            )
            engine._world.setdefault("shop_restock_day_by_realm", {})[resolved_hid] = engine._realm_business_day_locked(
                hub_live, time.time(),
            )
            engine._save_world_locked()

    if switch_active:
        engine.switch_space(resolved_hid)
    validation = validate_compound_layout(engine, resolved_hid)
    return {
        "space_id": resolved_hid,
        "hub_space_id": resolved_hid,
        "name": hub_sp["name"],
        "mode": "compound",
        "created": True,
        "room_space_ids": [r["interior_space_id"] for r in created_rooms],
        "buildings": created_rooms,
        "hub_location_id": str(map_plan.get("hub", {}).get("location_id") or ""),
        "validation": validation,
    }


def validate_compound_layout(engine: Any, hub_space_id: str) -> dict[str, Any]:
    """全图 BFS 校验室内多房多跳连通性与全物件可达性。"""
    errors: list[str] = []
    with engine._lock:
        spaces = engine._world["spaces"]
        entities = engine._world["entities"]
        hub = spaces.get(hub_space_id)
        if hub is None:
            return {"ok": False, "errors": [f"空间「{hub_space_id}」不存在"], "room_count": 0, "reachable_doors": 0, "hops_max": 0}

        def _reach(sp_obj: Mapping[str, Any], sx: int, sy: int) -> set[tuple[int, int]]:
            sw, sh = int(sp_obj["width"]), int(sp_obj["height"])
            sid = str(sp_obj["space_id"])
            solids: set[tuple[int, int]] = set()
            for ent in entities.values():
                if ent.get("space_id") != sid or int(ent.get("z", 0)) < 0 or "portal" in (ent.get("traits") or {}):
                    continue
                if ent.get("solid"):
                    fw, fh = ent.get("footprint") or [1, 1]
                    for fx in range(int(fw)):
                        for fy in range(int(fh)):
                            solids.add((int(ent["x"]) + fx, int(ent["y"]) + fy))
            if not (0 <= sx < sw and 0 <= sy < sh) or engine.is_wall_cell(sp_obj, sx, sy) or (sx, sy) in solids:
                return set()
            vis: set[tuple[int, int]] = {(sx, sy)}
            q: deque[tuple[int, int]] = deque([(sx, sy)])
            while q:
                cx, cy = q.popleft()
                for ox, oy in ((0, 1), (0, -1), (1, 0), (-1, 0)):
                    nx, ny = cx + ox, cy + oy
                    if not (0 <= nx < sw and 0 <= ny < sh) or (nx, ny) in vis:
                        continue
                    if engine.is_wall_cell(sp_obj, nx, ny) or (nx, ny) in solids:
                        continue
                    if not engine.can_step_elevation(sp_obj, cx, cy, nx, ny):
                        continue
                    vis.add((nx, ny))
                    q.append((nx, ny))
            return vis

        hops_by_sid: dict[str, int] = {hub_space_id: 0}
        entry_point_by_sid: dict[str, tuple[int, int]] = {}
        bfs_queue: deque[str] = deque([hub_space_id])
        reachable_doors = 0

        while bfs_queue:
            cur_sid = bfs_queue.popleft()
            sp_obj = spaces.get(cur_sid)
            if sp_obj is None:
                errors.append(f"空间「{cur_sid}」缺失")
                continue
            sw, sh = int(sp_obj["width"]), int(sp_obj["height"])
            pref = entry_point_by_sid.get(cur_sid, (sw // 2, sh // 2))
            seed_x, seed_y = engine._find_open_floor_cell_locked(sp_obj, preferred_x=pref[0], preferred_y=pref[1])
            sp_reach = _reach(sp_obj, seed_x, seed_y)
            if not sp_reach:
                errors.append(f"「{sp_obj['name']}」无有效站立起点")
                continue

            portals = [
                e for e in entities.values()
                if e.get("space_id") == cur_sid and "portal" in (e.get("traits") or {})
            ]
            for portal in portals:
                dx, dy = int(portal["x"]), int(portal["y"])
                pmeta = (portal.get("traits") or {}).get("portal") or {}
                tsid = str(pmeta.get("target_space_id") or "")
                if not tsid and "interior_door" in (portal.get("tags") or ()):
                    if not ((dx, dy) in sp_reach or any((dx + ox, dy + oy) in sp_reach for ox, oy in ((0, 1), (0, -1), (1, 0), (-1, 0)))):
                        errors.append(f"「{sp_obj['name']}」无法走到内室门「{portal['name']}」({dx}, {dy})")
                    continue
                tx = int(pmeta.get("target_x", 1))
                ty = int(pmeta.get("target_y", 1))
                if (dx, dy) in sp_reach or any((dx + ox, dy + oy) in sp_reach for ox, oy in ((0, 1), (0, -1), (1, 0), (-1, 0))):
                    reachable_doors += 1
                else:
                    errors.append(f"「{sp_obj['name']}」无法走到门洞「{portal['name']}」({dx}, {dy})")
                if not tsid or tsid not in spaces:
                    errors.append(f"「{sp_obj['name']}」的门洞「{portal['name']}」指向不存在的空间：{tsid}")
                    continue
                target_sp = spaces[tsid]
                if engine.is_wall_cell(target_sp, tx, ty):
                    errors.append(f"「{sp_obj['name']}」门洞落点 ({tx}, {ty}) 处于墙体上")
                if tsid not in hops_by_sid:
                    hops_by_sid[tsid] = hops_by_sid[cur_sid] + 1
                    entry_point_by_sid[tsid] = (tx, ty)
                    bfs_queue.append(tsid)

            for ent in entities.values():
                if ent.get("space_id") != cur_sid or int(ent.get("z", 0)) < 0 or ent.get("placement") == "wall" or int(ent.get("y", 0)) < int(sp_obj.get("wall_rows", 1)):
                    continue
                if "portal" in (ent.get("traits") or {}):
                    continue
                ex, ey = int(ent["x"]), int(ent["y"])
                fw, fh = ent.get("footprint") or [1, 1]
                adj_hit = False
                for cx in range(ex, ex + int(fw)):
                    for cy in range(ey, ey + int(fh)):
                        if (cx, cy) in sp_reach or any((cx + ox, cy + oy) in sp_reach for ox, oy in ((0, 1), (0, -1), (1, 0), (-1, 0))):
                            adj_hit = True
                            break
                    if adj_hit:
                        break
                if not adj_hit:
                    errors.append(f"「{sp_obj['name']}」室内物件「{ent['name']}」({ex}, {ey}) 不可到达")

        expected_realm_sids = set(engine._descendant_space_ids_locked(hub_space_id))
        missing = expected_realm_sids - set(hops_by_sid.keys())
        for msid in sorted(missing):
            errors.append(f"子房「{spaces.get(msid, {}).get('name', msid)}」无法从主厅通过门洞逐跳到达")

        return {
            "ok": len(errors) == 0,
            "errors": errors,
            "room_count": max(0, len(hops_by_sid) - 1),
            "reachable_doors": reachable_doors,
            "hops_max": max(hops_by_sid.values(), default=0),
        }
