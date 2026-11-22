"""樱町默认示范小镇与可进入建筑子世界构造器（town_builder.py）。

职责：
- 构造一张 40x30 的多区域户外小镇（`town_sakura`）及其 6 栋可推门进入的独立建筑子世界；
- 提供通用的 `build_building_subworld()`，支持后续扩展或由玩家/LLM 新增可进入建筑；
- 全部逻辑仅依赖 Python 标准库与 `WorldEngine` 公开/内部原子操作，保证物质守恒、门洞双向绑定与 100% 连通性。
"""

from __future__ import annotations

import logging
import time
from collections import Counter, deque
from typing import TYPE_CHECKING, Any, Mapping, Sequence

from . import venue_semantics, weather
from .schema import TILE_CATALOG, clip_text, normalize_buildings, normalize_zones, slugify

if TYPE_CHECKING:
    from .engine import WorldEngine

logger = logging.getLogger(__name__)

DEFAULT_TOWN_ID = "town_sakura"
DEFAULT_TOWN_SPAWN: tuple[int, int] = (19, 15)

_TOWN_ZONES: tuple[dict[str, Any], ...] = (
    {
        "zone_id": "north_west_residential",
        "name": "北西住宅区",
        "x": 1,
        "y": 1,
        "w": 17,
        "h": 12,
        "color": "#9ad49b",
        "description": "安静整洁的住宅街区，分布着七海的小屋与伙伴宿舍。",
    },
    {
        "zone_id": "north_east_market",
        "name": "北东商业街",
        "x": 22,
        "y": 1,
        "w": 17,
        "h": 12,
        "color": "#f2b880",
        "description": "咖啡香气与杂货工坊交织的小镇商店街。",
    },
    {
        "zone_id": "central_plaza",
        "name": "中央喷泉广场",
        "x": 14,
        "y": 11,
        "w": 12,
        "h": 8,
        "color": "#ffd166",
        "description": "城镇十字路口与喷泉广场，布告栏与街灯围绕四周。",
    },
    {
        "zone_id": "south_west_park",
        "name": "南西湖畔公园",
        "x": 1,
        "y": 17,
        "w": 17,
        "h": 12,
        "color": "#7cc7e8",
        "description": "绿意湖畔、露营篝火与静谧图书馆所在的休闲公园。",
    },
    {
        "zone_id": "south_east_arcade",
        "name": "南东文娱区",
        "x": 22,
        "y": 17,
        "w": 17,
        "h": 12,
        "color": "#ff9dbb",
        "description": "霓虹闪烁的星海电玩厅与花坛庭院。",
    },
)


def _rect_cells(x0: int, y0: int, w: int, h: int, tile_id: str) -> list[dict[str, Any]]:
    return [
        {"x": x0 + dx, "y": y0 + dy, "tile_id": tile_id}
        for dy in range(h)
        for dx in range(w)
    ]


def _default_town_building_specs() -> list[dict[str, Any]]:
    return [
        {
            "building_id": "bldg_home",
            "name": "七海的小屋",
            "zone_id": "north_west_residential",
            "zone_name": "北西住宅区",
            "ext_x": 3,
            "ext_y": 3,
            "ext_w": 7,
            "ext_h": 5,
            "door_x": 6,
            "door_y": 7,
            "roof_color": "#c9647a",
            "wall_tile": "brick_wall",
            "category": "home",
            "icon": "🏠",
            "walkway": [{"x": 6, "y": y, "tile_id": "cobble"} for y in range(8, 14)],
            "interior_space_id": f"{DEFAULT_TOWN_ID}__home",
            "interior_width": 14,
            "interior_height": 10,
            "interior_palette": "warm_stardew",
            "interior_ambient": "七海千秋的温馨卧室，木地板微暖，摆着掌机、书架与热饮。",
            "interior_floor_tile": "wood_floor",
            "interior_wall_tile": "wood_wall",
            "interior_items": [
                {"bp": "default_bed", "x": 1, "y": 1},
                {"bp": "default_lamp", "x": 4, "y": 1, "light_on": True},
                {"bp": "default_desk", "x": 5, "y": 1},
                {"bp": "default_handheld", "x": 5, "y": 1},
                {"bp": "default_hot_drink", "x": 6, "y": 1},
                {"bp": "default_bookshelf", "x": 8, "y": 1},
                {"bp": "default_plant", "x": 12, "y": 1},
                {"bp": "default_sofa", "x": 10, "y": 4},
                {"bp": "default_box", "x": 1, "y": 5},
                {"bp": "default_notebook", "x": 3, "y": 5, "note_text": "今天搬进了樱町小镇的新家，欢迎大家来串门。"},
                {"bp": "default_wall_clock", "x": 7, "y": 0},
                {"bp": "default_window_poster", "x": 10, "y": 0},
            ],
        },
        {
            "building_id": "bldg_guest",
            "name": "伙伴宿舍",
            "zone_id": "north_west_residential",
            "zone_name": "北西住宅区",
            "ext_x": 12,
            "ext_y": 3,
            "ext_w": 6,
            "ext_h": 5,
            "door_x": 15,
            "door_y": 7,
            "roof_color": "#729968",
            "wall_tile": "wood_wall",
            "category": "home",
            "icon": "🏡",
            "walkway": [{"x": 15, "y": y, "tile_id": "cobble"} for y in range(8, 14)],
            "interior_space_id": f"{DEFAULT_TOWN_ID}__guest",
            "interior_width": 12,
            "interior_height": 10,
            "interior_palette": "warm_stardew",
            "interior_ambient": "铺着日式榻榻米的伙伴宿舍，干净温馨，还藏着一只待解锁的木箱。",
            "interior_floor_tile": "tatami",
            "interior_wall_tile": "wood_wall",
            "interior_items": [
                {"bp": "default_bed", "x": 1, "y": 1},
                {"bp": "default_desk", "x": 5, "y": 1},
                {"bp": "default_lamp", "x": 5, "y": 1},
                {"bp": "default_snack", "x": 6, "y": 1},
                {"bp": "default_sticky_note", "x": 8, "y": 1, "note_text": "钥匙就放在床南边的木宝箱旁边。"},
                {"bp": "default_cat_cushion", "x": 9, "y": 4},
                {"bp": "default_wooden_chest", "x": 1, "y": 5},
                {"bp": "default_brass_key", "x": 2, "y": 5},
                {"bp": "default_wall_clock", "x": 6, "y": 0},
            ],
        },
        {
            "building_id": "bldg_cafe",
            "name": "微风咖啡馆",
            "zone_id": "north_east_market",
            "zone_name": "北东商业街",
            "ext_x": 23,
            "ext_y": 3,
            "ext_w": 7,
            "ext_h": 5,
            "door_x": 26,
            "door_y": 7,
            "roof_color": "#c9885b",
            "wall_tile": "white_wall",
            "category": "shop",
            "icon": "☕",
            "walkway": [{"x": 26, "y": y, "tile_id": "cobble"} for y in range(8, 14)],
            "interior_space_id": f"{DEFAULT_TOWN_ID}__cafe",
            "interior_width": 16,
            "interior_height": 10,
            "interior_palette": "warm_stardew",
            "interior_ambient": "飘着咖啡与黑胶唱片旋律的微风咖啡馆，桌上放着草莓蛋糕与热饮。",
            "interior_floor_tile": "dark_wood",
            "interior_wall_tile": "white_wall",
            "interior_items": [
                {"bp": "default_fridge", "x": 1, "y": 1},
                {"bp": "default_desk", "x": 2, "y": 1, "name": "咖啡吧台", "brief": "整洁的木质咖啡吧台"},
                {"bp": "default_coffee_maker", "x": 2, "y": 1},
                {"bp": "default_hot_drink", "x": 3, "y": 1},
                {"bp": "default_gramophone", "x": 5, "y": 1},
                {"bp": "default_lamp", "x": 7, "y": 1, "light_on": True},
                {"bp": "default_lamp", "x": 14, "y": 1},
                {"bp": "default_desk", "x": 10, "y": 2, "name": "甜品餐桌", "brief": "靠窗的小木餐桌"},
                {"bp": "default_cake", "x": 10, "y": 2},
                {"bp": "default_ramen", "x": 11, "y": 2},
                {"bp": "default_sofa", "x": 12, "y": 5},
                {"bp": "default_plant", "x": 1, "y": 6},
                {"bp": "default_plant", "x": 14, "y": 7},
                {"bp": "default_wall_clock", "x": 8, "y": 0},
            ],
        },
        {
            "building_id": "bldg_workshop",
            "name": "樱町杂货工坊",
            "zone_id": "north_east_market",
            "zone_name": "北东商业街",
            "ext_x": 32,
            "ext_y": 3,
            "ext_w": 6,
            "ext_h": 5,
            "door_x": 35,
            "door_y": 7,
            "roof_color": "#5e7ea6",
            "wall_tile": "wood_wall",
            "category": "shop",
            "icon": "🏪",
            "walkway": [{"x": 35, "y": y, "tile_id": "cobble"} for y in range(8, 14)],
            "interior_space_id": f"{DEFAULT_TOWN_ID}__workshop",
            "interior_width": 14,
            "interior_height": 10,
            "interior_palette": "ink_terminal",
            "interior_ambient": "摆满货柜、储物箱与补给品的杂货工坊，适合采购和交换道具。",
            "interior_floor_tile": "stone_tile",
            "interior_wall_tile": "brick_wall",
            "interior_items": [
                {"bp": "default_desk", "x": 2, "y": 1, "name": "工坊柜台", "brief": "整洁的杂货工坊柜台"},
                {"bp": "default_notebook", "x": 2, "y": 1, "name": "货品账本", "note_text": "杂货工坊常备小点心、热饮与备用铜钥匙。"},
                {"bp": "default_lamp", "x": 3, "y": 1, "light_on": True},
                {"bp": "default_desk", "x": 9, "y": 1, "name": "陈列台", "brief": "摆着商品的木陈列台"},
                {"bp": "default_snack", "x": 9, "y": 1},
                {"bp": "default_hot_drink", "x": 10, "y": 1},
                {"bp": "default_brass_key", "x": 6, "y": 2},
                {"bp": "default_box", "x": 1, "y": 4},
                {"bp": "default_box", "x": 1, "y": 6},
                {"bp": "default_wooden_chest", "x": 11, "y": 5},
                {"bp": "default_plant", "x": 12, "y": 1},
                {"bp": "default_wall_clock", "x": 7, "y": 0},
            ],
        },
        {
            "building_id": "bldg_library",
            "name": "湖畔图书馆",
            "zone_id": "south_west_park",
            "zone_name": "南西湖畔公园",
            "ext_x": 10,
            "ext_y": 19,
            "ext_w": 7,
            "ext_h": 5,
            "door_x": 13,
            "door_y": 23,
            "roof_color": "#5a827e",
            "wall_tile": "white_wall",
            "category": "public",
            "icon": "📚",
            "walkway": [
                {"x": 13, "y": 24, "tile_id": "cobble"},
                *[{"x": x, "y": 25, "tile_id": "cobble"} for x in range(13, 20)],
            ],
            "interior_space_id": f"{DEFAULT_TOWN_ID}__library",
            "interior_width": 14,
            "interior_height": 10,
            "interior_palette": "ink_terminal",
            "interior_ambient": "临湖而建的安静图书馆，书架林立，阳光洒在阅览桌和画架上。",
            "interior_floor_tile": "wood_floor",
            "interior_wall_tile": "wood_wall",
            "interior_items": [
                {"bp": "default_bookshelf", "x": 1, "y": 1},
                {"bp": "default_bookshelf", "x": 4, "y": 1},
                {"bp": "default_bookshelf", "x": 10, "y": 1},
                {"bp": "default_plant", "x": 12, "y": 1},
                {"bp": "default_desk", "x": 2, "y": 4, "name": "阅览桌", "brief": "安静的木质阅览长桌"},
                {"bp": "default_notebook", "x": 2, "y": 4, "name": "借阅登记簿", "note_text": "请轻声翻阅，欢迎在留言簿写下读后感。"},
                {"bp": "default_lamp", "x": 3, "y": 4, "light_on": True},
                {"bp": "default_desk", "x": 9, "y": 4, "name": "自习桌", "brief": "台灯明亮的自习书桌"},
                {"bp": "default_lamp", "x": 9, "y": 4},
                {"bp": "default_sofa", "x": 1, "y": 6},
                {"bp": "default_easel", "x": 11, "y": 6},
                {"bp": "default_wall_clock", "x": 7, "y": 0},
            ],
        },
        {
            "building_id": "bldg_arcade",
            "name": "星海电玩厅",
            "zone_id": "south_east_arcade",
            "zone_name": "南东文娱区",
            "ext_x": 25,
            "ext_y": 19,
            "ext_w": 9,
            "ext_h": 5,
            "door_x": 29,
            "door_y": 23,
            "roof_color": "#694d8c",
            "wall_tile": "glass_wall",
            "category": "arcade",
            "icon": "🕹️",
            "walkway": [
                {"x": 29, "y": 24, "tile_id": "cobble"},
                *[{"x": x, "y": 25, "tile_id": "cobble"} for x in range(20, 30)],
            ],
            "interior_space_id": f"{DEFAULT_TOWN_ID}__arcade",
            "interior_width": 16,
            "interior_height": 10,
            "interior_palette": "midnight_handheld",
            "interior_ambient": "闪烁着街机荧幕光影的星海电玩厅，摆着街机、电脑与复古掌机。",
            "interior_floor_tile": "carpet_blue",
            "interior_wall_tile": "glass_wall",
            "interior_items": [
                {"bp": "default_lamp", "x": 1, "y": 1, "light_on": True},
                {"bp": "default_arcade", "x": 2, "y": 1},
                {"bp": "default_arcade", "x": 4, "y": 1},
                {"bp": "default_arcade", "x": 6, "y": 1},
                {"bp": "default_pc", "x": 10, "y": 1},
                {"bp": "default_pc", "x": 12, "y": 1},
                {"bp": "default_fridge", "x": 14, "y": 1},
                {"bp": "default_desk", "x": 11, "y": 4, "name": "掌机试玩台", "brief": "放着试玩掌机的吧台"},
                {"bp": "default_handheld", "x": 11, "y": 4},
                {"bp": "default_lamp", "x": 12, "y": 4},
                {"bp": "default_sofa", "x": 2, "y": 5},
                {"bp": "default_window_poster", "x": 5, "y": 0},
                {"bp": "default_window_poster", "x": 11, "y": 0},
            ],
        },
    ]


def build_building_subworld(
    engine: WorldEngine,
    town_space_id: str,
    *,
    building_id: str = "",
    name: str = "新建筑",
    zone_id: str = "",
    zone_name: str = "",
    ext_x: int = 2,
    ext_y: int = 2,
    ext_w: int = 5,
    ext_h: int = 4,
    door_x: int | None = None,
    door_y: int | None = None,
    roof_color: str = "#a85646",
    wall_tile: str = "brick_wall",
    road_tile: str = "cobble",
    category: str = "house",
    role: str = "",
    restock_profile: str = "",
    affordances: Sequence[str] | None = None,
    icon: str = "🏠",
    walkway: Sequence[Mapping[str, Any]] | None = None,
    interior_space_id: str = "",
    interior_width: int = 12,
    interior_height: int = 9,
    interior_palette: str = "warm_stardew",
    interior_ambient: str = "",
    interior_floor_tile: str = "wood_floor",
    interior_wall_tile: str = "wood_wall",
    interior_items: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """在城镇空间上构建一栋建筑外观及其双向门洞连通的独立室内子世界。"""
    with engine._lock:
        town_space = engine._world["spaces"].get(town_space_id)
        if not town_space:
            raise ValueError(f"母城镇空间不存在：{town_space_id}")
        tw, th = int(town_space["width"]), int(town_space["height"])

    clean_name = clip_text(name, 16, "新建筑")
    dx = int(door_x if door_x is not None else ext_x + max(0, ext_w // 2))
    dy = int(door_y if door_y is not None else ext_y + max(0, ext_h - 1))
    dx = max(0, min(tw - 1, dx))
    dy = max(0, min(th - 1, dy))

    bid = str(building_id or "").strip() or slugify(clean_name, prefix="bldg")
    int_sid = str(interior_space_id or "").strip()
    if not int_sid:
        base_int = f"{town_space_id}__{slugify(bid, prefix='bldg')}"
        int_sid = base_int
        idx = 2
        with engine._lock:
            while int_sid in engine._world["spaces"]:
                int_sid = f"{base_int}_{idx}"
                idx += 1

    resolved_role = venue_semantics.normalize_venue_role(role, category=category, name=clean_name)
    resolved_cat = str(category or "").strip() or venue_semantics.default_category_for_role(resolved_role)
    resolved_restock = (
        str(restock_profile).strip().lower()
        if restock_profile in venue_semantics.VALID_RESTOCK_PROFILES and restock_profile != ""
        else venue_semantics.restock_profile_for_building({"role": resolved_role, "category": resolved_cat, "name": clean_name})
    )
    resolved_aff = (
        list(affordances)
        if affordances
        else venue_semantics.building_affordance_hints({"role": resolved_role, "category": resolved_cat, "name": clean_name})
    )
    resolved_icon = clip_text(icon, 4, "") or venue_semantics.default_icon_for_role(resolved_role)
    door_tile = road_tile if road_tile in TILE_CATALOG and TILE_CATALOG[road_tile].get("walkable") else "cobble"

    resolved_zone = zone_name
    if not resolved_zone and zone_id:
        for z in town_space.get("zones") or []:
            if str(z.get("zone_id") or "") == zone_id:
                resolved_zone = str(z.get("name") or "")
                break
    if not resolved_zone:
        z_hit = engine.get_zone_at(town_space, dx, dy)
        if z_hit:
            resolved_zone = str(z_hit.get("name") or "")
            if not zone_id:
                zone_id = str(z_hit.get("zone_id") or "")

    # 1. 在城镇画布上铺设建筑外墙体、门洞踏步与连接步道
    shell_cells = _rect_cells(ext_x, ext_y, ext_w, ext_h, wall_tile)
    shell_cells.append({"x": dx, "y": dy, "tile_id": door_tile})
    if dy + 1 < th:
        shell_cells.append({"x": dx, "y": dy + 1, "tile_id": door_tile})
    for wcell in walkway or ():
        if isinstance(wcell, Mapping) and "x" in wcell and "y" in wcell:
            shell_cells.append({
                "x": int(wcell["x"]),
                "y": int(wcell["y"]),
                "tile_id": str(wcell.get("tile_id") or door_tile),
            })
    engine.paint_tiles(town_space_id, shell_cells)

    # 2. 创建独立的室内子世界
    iw = max(8, int(interior_width))
    ih = max(6, int(interior_height))
    exit_x = iw // 2
    exit_y = ih - 1
    interior_sp = engine.create_space(
        name=clean_name,
        width=iw,
        height=ih,
        wall_rows=1,
        space_kind="building",
        parent_space_id=town_space_id,
        district=resolved_zone,
        ambient_phrase=interior_ambient or f"「{clean_name}」的室内空间，推门可回到「{town_space['name']}」。",
        palette_id=interior_palette,
        fixed_space_id=int_sid,
        switch_active=False,
    )

    interior_tiles: list[dict[str, Any]] = []
    interior_tiles.extend(_rect_cells(0, 0, iw, 1, interior_wall_tile))
    interior_tiles.extend(_rect_cells(0, 1, iw, ih - 1, interior_floor_tile))
    for sx in range(iw):
        interior_tiles.append({
            "x": sx,
            "y": exit_y,
            "tile_id": interior_floor_tile if sx == exit_x else interior_wall_tile,
        })
    engine.paint_tiles(interior_sp["space_id"], interior_tiles)

    # 3. 双向绑定正门（街道 <-> 室内）
    ext_door = engine.place_from_blueprint("default_door", town_space_id, dx, dy, delivery_mode="silent")
    engine.update_entity_spec(
        ext_door["entity_id"],
        name=clip_text(f"{clean_name}正门", 12, "建筑正门"),
        brief=clip_text(f"通往「{clean_name}」的门", 25),
        detail=clip_text(f"一扇通往「{clean_name}」的门，推开即可进入室内。", 60),
        traits={
            "portal": {
                "target_space_id": interior_sp["space_id"],
                "target_x": exit_x,
                "target_y": max(1, exit_y - 1),
                "is_open": False,
            }
        },
    )

    int_door = engine.place_from_blueprint("default_door", interior_sp["space_id"], exit_x, exit_y, delivery_mode="silent")
    engine.update_entity_spec(
        int_door["entity_id"],
        name="出口大门",
        brief=clip_text(f"推开回到「{town_space['name']}」", 25),
        detail=clip_text(f"推开这扇门，就能走回「{town_space['name']}」的街道。", 60),
        traits={
            "portal": {
                "target_space_id": town_space_id,
                "target_x": dx,
                "target_y": min(th - 1, dy + 1),
                "is_open": False,
            }
        },
    )

    # 4. 放置室内家具与小物
    for item in interior_items:
        if not isinstance(item, Mapping):
            continue
        bp_id = str(item.get("bp") or item.get("blueprint_id") or "").strip()
        if not bp_id:
            continue
        try:
            ent = engine.place_from_blueprint(
                bp_id,
                interior_sp["space_id"],
                int(item.get("x") or 1),
                int(item.get("y") or 1),
                delivery_mode="silent",
                note_text=str(item.get("note_text") or ""),
            )
            upd_traits = dict(ent.get("traits") or {})
            changed_traits = False
            if item.get("light_on") and "switchable" in upd_traits:
                upd_traits["switchable"] = {**upd_traits["switchable"], "is_on": True}
                changed_traits = True
            if item.get("name") or item.get("brief") or changed_traits:
                engine.update_entity_spec(
                    ent["entity_id"],
                    name=item.get("name"),
                    brief=item.get("brief"),
                    traits=upd_traits if changed_traits else None,
                )
        except Exception:
            logger.debug("nanami_world: 放置建筑室内物品跳过 %s in %s", item, interior_sp["space_id"], exc_info=True)

    # 5. 将建筑元数据写入母城镇空间
    with engine._lock:
        tsp = engine._world["spaces"][town_space_id]
        existing = [
            b for b in (tsp.get("buildings") or [])
            if str(b.get("building_id") or "") != bid
            and str(b.get("interior_space_id") or "") != interior_sp["space_id"]
        ]
        existing.append({
            "building_id": bid,
            "name": clean_name,
            "zone_id": zone_id,
            "x": ext_x,
            "y": ext_y,
            "w": ext_w,
            "h": ext_h,
            "door_x": dx,
            "door_y": dy,
            "roof_color": roof_color,
            "interior_space_id": interior_sp["space_id"],
            "category": resolved_cat,
            "role": resolved_role,
            "restock_profile": resolved_restock,
            "affordances": resolved_aff,
            "icon": resolved_icon,
        })
        tsp["buildings"] = normalize_buildings(existing, int(tsp["width"]), int(tsp["height"]))
        engine._save_world_locked()

    return {
        "building_id": bid,
        "name": clean_name,
        "role": resolved_role,
        "zone_id": zone_id,
        "interior_space_id": interior_sp["space_id"],
        "exterior_door_id": ext_door["entity_id"],
        "interior_door_id": int_door["entity_id"],
        "door_x": dx,
        "door_y": dy,
    }


def build_default_town(
    engine: WorldEngine,
    *,
    town_id: str = DEFAULT_TOWN_ID,
    replace: bool = False,
    move_actor_id: str = "",
) -> dict[str, Any]:
    """构建（或打开）默认 40x30 多区域示范小镇「樱町小镇」与 6 栋可进入建筑子世界。"""
    with engine._lock:
        exists = town_id in engine._world["spaces"]
    if exists and not replace:
        engine.switch_space(town_id)
        with engine._lock:
            child_ids = [
                sid for sid, sp in engine._world["spaces"].items()
                if str(sp.get("parent_space_id") or "").strip() == town_id
            ]
        return {
            "town_space_id": town_id,
            "created": False,
            "building_space_ids": child_ids,
        }

    tmp_guard_sid = ""
    if exists and replace:
        with engine._lock:
            descendants = engine._descendant_space_ids_locked(town_id)
            delete_ids = {town_id, *descendants}
            surviving = [sid for sid in engine._world["spaces"].keys() if sid not in delete_ids]
        if not surviving:
            tmp_sp = engine.create_space(name="临时中转", fixed_space_id="__town_rebuild_tmp", switch_active=False)
            tmp_guard_sid = tmp_sp["space_id"]
        engine.delete_space(town_id)

    tw, th = 40, 30
    engine.create_space(
        name="樱町小镇",
        width=tw,
        height=th,
        wall_rows=0,
        space_kind="town",
        ambient_phrase="樱花树与石板路交织的樱町小镇，五个街区与六栋建筑均可推门进入。",
        palette_id="warm_stardew",
        zones=_TOWN_ZONES,
        fixed_space_id=town_id,
        switch_active=True,
    )

    # 1. 全图铺设户外草地 + 四周灌木围栏 + 十字石板主干道 + 广场 & 公园水景
    tile_cells: list[dict[str, Any]] = _rect_cells(0, 0, tw, th, "grass")
    for x in range(tw):
        tile_cells.append({"x": x, "y": 0, "tile_id": "hedge"})
        tile_cells.append({"x": x, "y": th - 1, "tile_id": "hedge"})
    for y in range(th):
        tile_cells.append({"x": 0, "y": y, "tile_id": "hedge"})
        tile_cells.append({"x": tw - 1, "y": y, "tile_id": "hedge"})

    # 十字石板大道
    tile_cells.extend(_rect_cells(2, 14, tw - 4, 2, "cobble"))
    tile_cells.extend(_rect_cells(19, 2, 2, th - 4, "cobble"))

    # 中央广场石砖与四角花坛/小水景（保持十字中线 18..21 / 14..15 畅通）
    tile_cells.extend(_rect_cells(15, 12, 10, 6, "stone_tile"))
    tile_cells.extend(_rect_cells(18, 12, 4, 6, "cobble"))
    tile_cells.extend(_rect_cells(15, 14, 10, 2, "cobble"))
    for fx, fy in ((15, 12), (24, 12), (15, 17), (24, 17)):
        tile_cells.append({"x": fx, "y": fy, "tile_id": "flower_grass"})
    for wx, wy in ((16, 12), (23, 12), (16, 17), (23, 17)):
        tile_cells.append({"x": wx, "y": wy, "tile_id": "water"})

    # 南西湖畔公园：池塘、沙滩与繁花草坪
    tile_cells.extend(_rect_cells(2, 19, 7, 2, "flower_grass"))
    tile_cells.extend(_rect_cells(2, 22, 7, 1, "sand"))
    for py in range(23, 27):
        tile_cells.append({"x": 2, "y": py, "tile_id": "sand"})
        tile_cells.append({"x": 8, "y": py, "tile_id": "sand"})
    tile_cells.extend(_rect_cells(3, 23, 5, 4, "water"))
    tile_cells.extend(_rect_cells(11, 26, 6, 2, "flower_grass"))

    # 南东文娱区花坛点缀
    tile_cells.extend(_rect_cells(23, 26, 14, 2, "flower_grass"))
    engine.paint_tiles(town_id, tile_cells)

    # 2. 户外广场与公园设施
    outdoor_props: list[tuple[str, int, int, dict[str, Any]]] = [
        ("default_street_lamp", 17, 13, {"light_on": True}),
        ("default_street_lamp", 22, 13, {"light_on": True}),
        ("default_street_lamp", 17, 16, {"light_on": True}),
        ("default_street_lamp", 22, 16, {"light_on": True}),
        (
            "default_notebook",
            18,
            12,
            {
                "name": "小镇布告栏",
                "brief": "写着小镇导览与邻里留言的布告栏",
                "note_text": "欢迎来到樱町小镇！双击任意建筑正门即可推门进入室内。",
            },
        ),
        ("default_cat_cushion", 21, 16, {}),
        ("default_campfire", 5, 21, {"light_on": True}),
        ("default_snack", 4, 21, {}),
        ("default_street_lamp", 9, 21, {"light_on": True}),
        ("default_street_lamp", 24, 25, {"light_on": True}),
        ("default_street_lamp", 34, 25, {"light_on": True}),
        ("default_cat_cushion", 35, 21, {}),
    ]
    for bp_id, ox, oy, meta in outdoor_props:
        ent = engine.place_from_blueprint(
            bp_id,
            town_id,
            ox,
            oy,
            delivery_mode="silent",
            note_text=str(meta.get("note_text") or ""),
        )
        upd_traits = dict(ent.get("traits") or {})
        changed = False
        if meta.get("light_on") and "switchable" in upd_traits:
            upd_traits["switchable"] = {**upd_traits["switchable"], "is_on": True}
            changed = True
        if meta.get("name") or meta.get("brief") or changed:
            engine.update_entity_spec(
                ent["entity_id"],
                name=meta.get("name"),
                brief=meta.get("brief"),
                traits=upd_traits if changed else None,
            )

    # 3. 逐栋创建 6 个可进入建筑子世界
    building_results: list[dict[str, Any]] = []
    for spec in _default_town_building_specs():
        res = build_building_subworld(
            engine,
            town_id,
            building_id=spec["building_id"],
            name=spec["name"],
            zone_id=spec["zone_id"],
            zone_name=spec["zone_name"],
            ext_x=spec["ext_x"],
            ext_y=spec["ext_y"],
            ext_w=spec["ext_w"],
            ext_h=spec["ext_h"],
            door_x=spec["door_x"],
            door_y=spec["door_y"],
            roof_color=spec["roof_color"],
            wall_tile=spec["wall_tile"],
            category=spec["category"],
            icon=spec["icon"],
            walkway=spec["walkway"],
            interior_space_id=spec["interior_space_id"],
            interior_width=spec["interior_width"],
            interior_height=spec["interior_height"],
            interior_palette=spec["interior_palette"],
            interior_ambient=spec["interior_ambient"],
            interior_floor_tile=spec["interior_floor_tile"],
            interior_wall_tile=spec["interior_wall_tile"],
            interior_items=spec["interior_items"],
        )
        building_results.append(res)

    if tmp_guard_sid:
        engine.delete_space(tmp_guard_sid)
    engine.switch_space(town_id)
    with engine._lock:
        tsp = engine._world["spaces"].get(town_id)
        if tsp is not None:
            engine._world.setdefault("shop_restock_day_by_realm", {})[town_id] = engine._realm_business_day_locked(
                tsp, time.time(),
            )
            engine._save_world_locked()

    if move_actor_id:
        try:
            engine.place_actor(move_actor_id, town_id, *DEFAULT_TOWN_SPAWN)
        except ValueError:
            logger.debug("nanami_world: 放置默认角色到小镇广场跳过", exc_info=True)

    return {
        "town_space_id": town_id,
        "created": True,
        "building_space_ids": [b["interior_space_id"] for b in building_results],
        "buildings": building_results,
    }


def synthesize_interior_items(
    role: str,
    *,
    width: int = 14,
    height: int = 10,
    building_name: str = "",
    note_text: str = "",
    variant: int = 0,
) -> list[dict[str, Any]]:
    """按建筑角色合成安全、不挡门口、无重叠的内室家具清单（仅用内置蓝图）。"""
    w = max(12, int(width))
    h = max(9, int(height))
    mid = w // 2
    r = venue_semantics.normalize_venue_role(role, name=building_name)
    clean_name = clip_text(building_name, 16, "新建筑")
    note = clip_text(note_text, 80, "")

    if r == "home":
        if variant % 2 == 1:
            return [
                {"bp": "default_bed", "x": 1, "y": 1},
                {"bp": "default_desk", "x": 5, "y": 1},
                {"bp": "default_lamp", "x": 5, "y": 1, "light_on": True},
                {"bp": "default_snack", "x": 6, "y": 1},
                {"bp": "default_sticky_note", "x": 8, "y": 1, "note_text": note or f"欢迎来到「{clean_name}」，累了可以在床上好好休息。"},
                {"bp": "default_cat_cushion", "x": w - 3, "y": 4},
                {"bp": "default_wooden_chest", "x": 1, "y": 5},
                {"bp": "default_brass_key", "x": 2, "y": 5},
                {"bp": "default_wall_clock", "x": mid, "y": 0},
            ]
        return [
            {"bp": "default_bed", "x": 1, "y": 1},
            {"bp": "default_lamp", "x": 4, "y": 1, "light_on": True},
            {"bp": "default_desk", "x": 5, "y": 1},
            {"bp": "default_handheld", "x": 5, "y": 1},
            {"bp": "default_hot_drink", "x": 6, "y": 1},
            {"bp": "default_bookshelf", "x": w - 4, "y": 1},
            {"bp": "default_plant", "x": w - 2, "y": 1},
            {"bp": "default_sofa", "x": w - 4, "y": 4},
            {"bp": "default_box", "x": 1, "y": 5},
            {"bp": "default_notebook", "x": 3, "y": 5, "note_text": note or f"记录着「{clean_name}」日常点滴的桌边笔记本。"},
            {"bp": "default_wall_clock", "x": mid, "y": 0},
            {"bp": "default_window_poster", "x": max(2, w - 3), "y": 0},
        ]

    if r == "cafe":
        return [
            {"bp": "default_fridge", "x": 1, "y": 1},
            {"bp": "default_desk", "x": 2, "y": 1, "name": "咖啡吧台", "brief": "整洁的木质咖啡吧台"},
            {"bp": "default_coffee_maker", "x": 2, "y": 1},
            {"bp": "default_hot_drink", "x": 3, "y": 1},
            {"bp": "default_gramophone", "x": 5, "y": 1},
            {"bp": "default_lamp", "x": 7, "y": 1, "light_on": True},
            {"bp": "default_desk", "x": w - 4, "y": 2, "name": "甜品餐桌", "brief": "靠窗的小木餐桌"},
            {"bp": "default_cake", "x": w - 4, "y": 2},
            {"bp": "default_ramen", "x": w - 3, "y": 2},
            {"bp": "default_sofa", "x": w - 4, "y": max(4, h - 5)},
            {"bp": "default_plant", "x": 1, "y": h - 3},
            {"bp": "default_plant", "x": w - 2, "y": h - 3},
            {"bp": "default_wall_clock", "x": mid, "y": 0},
        ]

    if r == "shop":
        return [
            {"bp": "default_desk", "x": 2, "y": 1, "name": "商店柜台", "brief": "整洁的商店木柜台"},
            {"bp": "default_notebook", "x": 2, "y": 1, "name": "货品账本", "note_text": note or f"「{clean_name}」每日清晨补齐点心、热饮与实用小物。"},
            {"bp": "default_lamp", "x": 3, "y": 1, "light_on": True},
            {"bp": "default_desk", "x": w - 5, "y": 1, "name": "陈列台", "brief": "摆着货物与点心的陈列台"},
            {"bp": "default_snack", "x": w - 5, "y": 1},
            {"bp": "default_hot_drink", "x": w - 4, "y": 1},
            {"bp": "default_brass_key", "x": mid - 1, "y": 3},
            {"bp": "default_box", "x": 1, "y": 4},
            {"bp": "default_box", "x": 1, "y": h - 3},
            {"bp": "default_wooden_chest", "x": w - 3, "y": 5},
            {"bp": "default_plant", "x": w - 2, "y": 1},
            {"bp": "default_wall_clock", "x": mid, "y": 0},
        ]

    if r == "library":
        return [
            {"bp": "default_bookshelf", "x": 1, "y": 1},
            {"bp": "default_bookshelf", "x": 4, "y": 1},
            {"bp": "default_bookshelf", "x": w - 4, "y": 1},
            {"bp": "default_desk", "x": 2, "y": 4, "name": "阅览桌", "brief": "安静的木质阅览长桌"},
            {"bp": "default_notebook", "x": 2, "y": 4, "name": "借阅登记簿", "note_text": note or f"欢迎来「{clean_name}」静静看书并写下感想。"},
            {"bp": "default_lamp", "x": 3, "y": 4, "light_on": True},
            {"bp": "default_easel", "x": w - 2, "y": 2},
            {"bp": "default_sofa", "x": w - 4, "y": 4},
            {"bp": "default_plant", "x": 1, "y": h - 3},
            {"bp": "default_wall_clock", "x": mid, "y": 0},
        ]

    if r == "arcade":
        return [
            {"bp": "default_lamp", "x": 1, "y": 1, "light_on": True},
            {"bp": "default_arcade", "x": 2, "y": 1},
            {"bp": "default_arcade", "x": 4, "y": 1},
            {"bp": "default_pc", "x": 7, "y": 1},
            {"bp": "default_pc", "x": w - 3, "y": 1},
            {"bp": "default_fridge", "x": w - 2, "y": 1},
            {"bp": "default_desk", "x": w - 4, "y": 4, "name": "试玩吧台", "brief": "放着试玩掌机的吧台"},
            {"bp": "default_handheld", "x": w - 4, "y": 4},
            {"bp": "default_lamp", "x": w - 3, "y": 4},
            {"bp": "default_sofa", "x": 2, "y": 5},
            {"bp": "default_window_poster", "x": max(2, mid - 2), "y": 0},
            {"bp": "default_window_poster", "x": min(w - 2, mid + 2), "y": 0},
        ]

    # public fallback
    return [
        {"bp": "default_desk", "x": 2, "y": 1, "name": "会馆长桌", "brief": "铺着笔记与桌灯的接待长桌"},
        {"bp": "default_notebook", "x": 2, "y": 1, "name": "来访留言簿", "note_text": note or f"欢迎来到「{clean_name}」，大家可以在这里碰面聊天。"},
        {"bp": "default_lamp", "x": 3, "y": 1, "light_on": True},
        {"bp": "default_easel", "x": 5, "y": 1},
        {"bp": "default_bookshelf", "x": w - 5, "y": 1},
        {"bp": "default_gramophone", "x": w - 3, "y": 1},
        {"bp": "default_cat_cushion", "x": 2, "y": 5},
        {"bp": "default_sofa", "x": w - 4, "y": 4},
        {"bp": "default_plant", "x": 1, "y": h - 3},
        {"bp": "default_plant", "x": w - 2, "y": h - 3},
        {"bp": "default_wall_clock", "x": mid, "y": 0},
    ]


_PROTECTED_ROAD_TILES: frozenset[str] = frozenset({"cobble", "stone_tile", "carpet_pink", "carpet_blue"})


def _is_walkable_tile_id(tid: str) -> bool:
    meta = TILE_CATALOG.get(str(tid or "wood_floor"))
    return bool(meta and meta.get("walkable", False))


def _cell_tile_id(space: Mapping[str, Any], x: int, y: int) -> str:
    default_tid = "grass" if space.get("space_kind") == "town" else "wood_floor"
    raw = space.get("tiles")
    if isinstance(raw, Mapping):
        return str(raw.get(f"{x},{y}") or default_tid)
    return default_tid


def _walkable_tile_counts(space: Mapping[str, Any]) -> Counter[str]:
    tw, th = int(space["width"]), int(space["height"])
    counts: Counter[str] = Counter()
    for y in range(1, th - 1):
        for x in range(1, tw - 1):
            tid = _cell_tile_id(space, x, y)
            if _is_walkable_tile_id(tid):
                counts[tid] += 1
    return counts


def _collect_building_boxes(space: Mapping[str, Any]) -> list[tuple[int, int, int, int]]:
    return [
        (int(b["x"]), int(b["y"]), int(b["x"]) + int(b["w"]) - 1, int(b["y"]) + int(b["h"]) - 1)
        for b in (space.get("buildings") or ())
        if isinstance(b, Mapping)
    ]


def _shortest_walkway_to_road(
    space: Mapping[str, Any],
    entities: Mapping[str, Mapping[str, Any]],
    start: tuple[int, int],
    *,
    building_boxes: Sequence[tuple[int, int, int, int]],
    road_targets: set[tuple[int, int]],
    reserved_cells: set[tuple[int, int]] = frozenset(),
    tile_id: str = "cobble",
) -> list[dict[str, Any]] | None:
    tw, th = int(space["width"]), int(space["height"])
    sx, sy = start
    if not (1 <= sx < tw - 1 and 1 <= sy < th - 1):
        return None
    if start in road_targets:
        return [{"x": sx, "y": sy, "tile_id": tile_id}]

    blocked_bldg: set[tuple[int, int]] = set()
    for bx0, by0, bx1, by1 in building_boxes:
        for cx in range(bx0, bx1 + 1):
            for cy in range(by0, by1 + 1):
                blocked_bldg.add((cx, cy))

    sid = str(space.get("space_id") or "")
    solid_ents: set[tuple[int, int]] = set()
    for ent in entities.values():
        if ent.get("space_id") != sid or int(ent.get("z", 0)) < 0:
            continue
        if ent.get("solid"):
            fw, fh = ent.get("footprint") or [1, 1]
            ex, ey = int(ent["x"]), int(ent["y"])
            for dx in range(int(fw)):
                for dy in range(int(fh)):
                    solid_ents.add((ex + dx, ey + dy))

    if start in blocked_bldg or start in reserved_cells or start in solid_ents:
        return None
    if not _is_walkable_tile_id(_cell_tile_id(space, sx, sy)):
        return None

    queue: deque[tuple[int, int]] = deque([start])
    parent: dict[tuple[int, int], tuple[int, int] | None] = {start: None}
    hit: tuple[int, int] | None = None
    while queue:
        cx, cy = queue.popleft()
        if (cx, cy) in road_targets:
            hit = (cx, cy)
            break
        for dx, dy in ((0, 1), (0, -1), (1, 0), (-1, 0)):
            nx, ny = cx + dx, cy + dy
            nc = (nx, ny)
            if not (1 <= nx < tw - 1 and 1 <= ny < th - 1) or nc in parent:
                continue
            if nc in blocked_bldg or nc in reserved_cells or nc in solid_ents:
                continue
            if nc not in road_targets and not _is_walkable_tile_id(_cell_tile_id(space, nx, ny)):
                continue
            parent[nc] = (cx, cy)
            queue.append(nc)

    if hit is None:
        return None
    path: list[tuple[int, int]] = []
    cur: tuple[int, int] | None = hit
    while cur is not None:
        path.append(cur)
        cur = parent[cur]
    path.reverse()
    return [{"x": px, "y": py, "tile_id": tile_id} for px, py in path if (px, py) not in road_targets] or [
        {"x": sx, "y": sy, "tile_id": tile_id}
    ]


def find_free_building_lot(
    engine: WorldEngine,
    town_space_id: str,
    *,
    ext_w: int = 6,
    ext_h: int = 5,
    zone_id: str = "",
    road_tile: str = "cobble",
    reserved_cells: set[tuple[int, int]] | None = None,
    region: tuple[int, int, int, int] | None = None,
) -> dict[str, Any]:
    """在城镇空间内确定性寻找不遮挡道路/水域/既有建筑且可达主路网的空闲建地。"""
    with engine._lock:
        space = engine._world["spaces"].get(town_space_id)
        if not space or space.get("space_kind") != "town":
            raise ValueError(f"目标不是城镇空间：{town_space_id}")
        tw, th = int(space["width"]), int(space["height"])
        zones = list(space.get("zones") or ())
        buildings = _collect_building_boxes(space)

        occupied_entities: set[tuple[int, int]] = set()
        for ent in engine._world["entities"].values():
            if ent.get("space_id") != town_space_id or int(ent.get("z", 0)) < 0:
                continue
            fw, fh = ent.get("footprint") or [1, 1]
            ex, ey = int(ent["x"]), int(ent["y"])
            for dx in range(int(fw)):
                for dy in range(int(fh)):
                    occupied_entities.add((ex + dx, ey + dy))

        # Road/plaza network targets: exclude the town's dominant ground tile so stone_tile/sand/etc. towns still have lots.
        counts = _walkable_tile_counts(space)
        dominant_ground = counts.most_common(1)[0][0] if counts else "grass"
        protected_road_ids = (_PROTECTED_ROAD_TILES - {dominant_ground}) | ({road_tile} - {dominant_ground})

        road_cells: set[tuple[int, int]] = set()
        for y in range(1, th - 1):
            for x in range(1, tw - 1):
                tid = _cell_tile_id(space, x, y)
                if tid in protected_road_ids and _is_walkable_tile_id(tid):
                    road_cells.add((x, y))

        if not road_cells:
            ax, ay = tw // 2 - 1, th // 2 - 1
            for y in range(2, th - 2):
                for x in (ax, ax + 1):
                    if 1 <= x < tw - 1 and _is_walkable_tile_id(_cell_tile_id(space, x, y)):
                        road_cells.add((x, y))
            for x in range(2, tw - 2):
                for y in (ay, ay + 1):
                    if 1 <= y < th - 1 and _is_walkable_tile_id(_cell_tile_id(space, x, y)):
                        road_cells.add((x, y))

        for z in zones:
            if str(z.get("zone_id") or "") not in ("center", "central_plaza"):
                continue
            zx0 = max(1, int(z.get("x", 1)))
            zy0 = max(1, int(z.get("y", 1)))
            zx1 = min(tw - 2, int(z.get("x", 1)) + int(z.get("w", 0)) - 1)
            zy1 = min(th - 2, int(z.get("y", 1)) + int(z.get("h", 0)) - 1)
            for cy in range(zy0, zy1 + 1):
                for cx in range(zx0, zx1 + 1):
                    if _is_walkable_tile_id(_cell_tile_id(space, cx, cy)):
                        road_cells.add((cx, cy))

        if not road_cells:
            road_cells.add((tw // 2 - 1, th // 2))

        extra_reserved = set(reserved_cells or ())
        padded_bldgs: set[tuple[int, int]] = set()
        for bx0, by0, bx1, by1 in buildings:
            for cx in range(bx0 - 1, bx1 + 2):
                for cy in range(by0 - 1, by1 + 2):
                    padded_bldgs.add((cx, cy))

        # Build prioritized search regions: requested zone -> other non-center zones -> whole town.
        search_boxes: list[tuple[int, int, int, int]] = []
        if region is not None:
            search_boxes.append(region)
        else:
            if zone_id:
                for z in zones:
                    if str(z.get("zone_id") or "") == zone_id:
                        search_boxes.append((int(z["x"]), int(z["y"]), int(z["x"]) + int(z["w"]) - 1, int(z["y"]) + int(z["h"]) - 1))
            for z in zones:
                zid_x = str(z.get("zone_id") or "")
                if zid_x in (zone_id, "center", "central_plaza"):
                    continue
                search_boxes.append((int(z["x"]), int(z["y"]), int(z["x"]) + int(z["w"]) - 1, int(z["y"]) + int(z["h"]) - 1))
            search_boxes.append((1, 1, tw - 2, th - 2))

        size_trials = list(dict.fromkeys([(max(4, int(ext_w)), max(4, int(ext_h))), (6, 4), (5, 4), (4, 4)]))
        entities_view = engine._world["entities"]

        for bw, bh in size_trials:
            for rx0, ry0, rx1, ry1 in search_boxes:
                min_x = max(1, rx0)
                max_x = min(tw - 1 - bw, rx1 - bw + 1)
                min_y = max(1, ry0)
                max_y = min(th - 2 - bh, ry1 - bh + 1)
                if min_x > max_x or min_y > max_y:
                    continue
                for ly in range(min_y, max_y + 1):
                    for lx in range(min_x, max_x + 1):
                        dx = lx + bw // 2
                        dy = ly + bh - 1
                        step_out = (dx, dy + 1)
                        if not (1 <= step_out[0] < tw - 1 and 1 <= step_out[1] < th - 1):
                            continue
                        if step_out in padded_bldgs or step_out in occupied_entities or step_out in extra_reserved:
                            continue
                        if not _is_walkable_tile_id(_cell_tile_id(space, *step_out)):
                            continue

                        lot_ok = True
                        for cy in range(ly, ly + bh):
                            for cx in range(lx, lx + bw):
                                cell = (cx, cy)
                                if (
                                    cell in padded_bldgs
                                    or cell in occupied_entities
                                    or cell in extra_reserved
                                    or cell in road_cells
                                    or not _is_walkable_tile_id(_cell_tile_id(space, cx, cy))
                                ):
                                    lot_ok = False
                                    break
                            if not lot_ok:
                                break
                        if not lot_ok:
                            continue

                        next_boxes = [*buildings, (lx, ly, lx + bw - 1, ly + bh - 1)]
                        walkway = _shortest_walkway_to_road(
                            space,
                            entities_view,
                            step_out,
                            building_boxes=next_boxes,
                            road_targets=road_cells,
                            reserved_cells=extra_reserved,
                            tile_id=road_tile,
                        )
                        if walkway is None:
                            continue
                        return {
                            "ext_x": lx,
                            "ext_y": ly,
                            "ext_w": bw,
                            "ext_h": bh,
                            "door_x": dx,
                            "door_y": dy,
                            "walkway": walkway,
                        }

        raise ValueError("当前城镇暂未找到足够的空闲建地，可先向外扩图后再加盖建筑。")


def build_generated_town(
    engine: WorldEngine,
    town_spec: Mapping[str, Any],
    *,
    town_id: str = "",
    move_actor_id: str = "",
) -> dict[str, Any]:
    """三层确定性城镇生成管线：L1 宏观骨架路网 -> L2 建筑子世界与内室 -> L3 天气生态与连通校验。"""
    clean_name = clip_text(town_spec.get("name"), 16, "新像素小镇")
    tw = max(28, min(64, int(town_spec.get("width") or 40)))
    th = max(22, min(48, int(town_spec.get("height") or 30)))

    with engine._lock:
        base_id = str(town_id or "").strip() or slugify(clean_name, prefix="town")
        resolved_tid = base_id
        suffix = 2
        while resolved_tid in engine._world["spaces"]:
            resolved_tid = f"{base_id}_{suffix}"
            suffix += 1

    rx = tw // 2 - 1
    ry = th // 2 - 1
    px0 = max(3, rx - 4)
    px1 = min(tw - 4, rx + 5)
    py0 = max(3, ry - 2)
    py1 = min(th - 4, ry + 3)

    zone_meta = {
        str(z.get("slot") or ""): z
        for z in (town_spec.get("zones") or ())
        if isinstance(z, Mapping) and z.get("slot")
    }
    zone_specs = [
        {
            "zone_id": "north_west",
            "name": clip_text((zone_meta.get("nw") or {}).get("name"), 12, "西北街区"),
            "x": 1, "y": 1, "w": max(4, rx - 1), "h": max(4, ry - 1),
            "color": "#9ad49b",
            "description": clip_text((zone_meta.get("nw") or {}).get("description"), 42, "安静整洁的西北街区。"),
        },
        {
            "zone_id": "north_east",
            "name": clip_text((zone_meta.get("ne") or {}).get("name"), 12, "东北商街"),
            "x": rx + 2, "y": 1, "w": max(4, tw - rx - 3), "h": max(4, ry - 1),
            "color": "#f2b880",
            "description": clip_text((zone_meta.get("ne") or {}).get("description"), 42, "商店与咖啡香气交织的街区。"),
        },
        {
            "zone_id": "center",
            "name": clip_text((zone_meta.get("center") or {}).get("name"), 12, "中央广场"),
            "x": px0, "y": py0, "w": px1 - px0 + 1, "h": py1 - py0 + 1,
            "color": "#ffd166",
            "description": clip_text((zone_meta.get("center") or {}).get("description"), 42, "十字道路交汇的中央广场。"),
        },
        {
            "zone_id": "south_west",
            "name": clip_text((zone_meta.get("sw") or {}).get("name"), 12, "西南园畔"),
            "x": 1, "y": ry + 2, "w": max(4, rx - 1), "h": max(4, th - ry - 3),
            "color": "#7cc7e8",
            "description": clip_text((zone_meta.get("sw") or {}).get("description"), 42, "带休闲景致与静谧去处的西南园区。"),
        },
        {
            "zone_id": "south_east",
            "name": clip_text((zone_meta.get("se") or {}).get("name"), 12, "东南游憩区"),
            "x": rx + 2, "y": ry + 2, "w": max(4, tw - rx - 3), "h": max(4, th - ry - 3),
            "color": "#ff9dbb",
            "description": clip_text((zone_meta.get("se") or {}).get("description"), 42, "聚会、娱乐与交流所在的东南街区。"),
        },
    ]

    ground_tile = str(town_spec.get("ground_tile") or "grass")
    road_tile = str(town_spec.get("road_tile") or "cobble")
    plaza_tile = str(town_spec.get("plaza_tile") or "stone_tile")
    border_tile = str(town_spec.get("border_tile") or "hedge")
    accent_tile = str(town_spec.get("accent_tile") or "flower_grass")
    palette_id = str(town_spec.get("palette_id") or "warm_stardew")
    ambient = clip_text(town_spec.get("ambient_phrase"), 60, f"微风轻拂的「{clean_name}」，每栋建筑都可推门进入。")
    has_water = bool(town_spec.get("has_water_feature", True))

    engine.create_space(
        name=clean_name,
        width=tw,
        height=th,
        wall_rows=0,
        space_kind="town",
        ambient_phrase=ambient,
        palette_id=palette_id,
        zones=zone_specs,
        fixed_space_id=resolved_tid,
        switch_active=True,
    )

    # Layer 1: 铺设地面、外围篱笆/水域、十字主干道、中央广场与西南角水景
    tile_cells: list[dict[str, Any]] = _rect_cells(0, 0, tw, th, ground_tile)
    for x in range(tw):
        tile_cells.append({"x": x, "y": 0, "tile_id": border_tile})
        tile_cells.append({"x": x, "y": th - 1, "tile_id": border_tile})
    for y in range(th):
        tile_cells.append({"x": 0, "y": y, "tile_id": border_tile})
        tile_cells.append({"x": tw - 1, "y": y, "tile_id": border_tile})

    tile_cells.extend(_rect_cells(2, ry, tw - 4, 2, road_tile))
    tile_cells.extend(_rect_cells(rx, 2, 2, th - 4, road_tile))
    tile_cells.extend(_rect_cells(px0, py0, px1 - px0 + 1, py1 - py0 + 1, plaza_tile))
    tile_cells.extend(_rect_cells(rx - 1, py0, 4, py1 - py0 + 1, road_tile))
    tile_cells.extend(_rect_cells(px0, ry, px1 - px0 + 1, 2, road_tile))

    reserved_lot_cells: set[tuple[int, int]] = set()
    for py in range(py0 - 1, py1 + 2):
        for px in range(px0 - 1, px1 + 2):
            reserved_lot_cells.add((px, py))

    for ax, ay in ((px0, py0), (px1, py0), (px0, py1), (px1, py1)):
        tile_cells.append({"x": ax, "y": ay, "tile_id": accent_tile})
    for x in range(rx - 1, rx + 3):
        tile_cells.append({"x": x, "y": th - 3, "tile_id": accent_tile})

    pond_campfire_cell: tuple[int, int] | None = None
    if has_water:
        wx0 = 2
        wx1 = min(6, rx - 3)
        wy0 = max(ry + 4, th - (5 if th >= 26 else 4))
        wy1 = th - 3
        if wx1 >= wx0 + 2 and wy1 >= wy0 + 1:
            shore_tile = "sand" if ground_tile != "sand" else accent_tile
            tile_cells.extend(_rect_cells(wx0, wy0 - 1, wx1 - wx0 + 2, 1, shore_tile))
            tile_cells.extend(_rect_cells(wx0, wy0, wx1 - wx0 + 1, wy1 - wy0 + 1, "water"))
            for r_y in range(wy0 - 1, wy1 + 1):
                for r_x in range(wx0 - 1, wx1 + 2):
                    reserved_lot_cells.add((r_x, r_y))
            pond_campfire_cell = (wx0 + 1, wy0 - 1)

    engine.paint_tiles(resolved_tid, tile_cells)

    bldgs_raw = list(town_spec.get("buildings") or ())
    dir_names = "、".join(str(b.get("name") or "") for b in bldgs_raw if b.get("name"))
    board_note = clip_text(
        f"欢迎来到「{clean_name}」！镇上建有：{dir_names}。双击建筑正门即可推门进入。",
        120,
        f"欢迎来到「{clean_name}」！",
    )
    plaza_props: list[tuple[str, int, int, dict[str, Any]]] = [
        ("default_street_lamp", max(2, px0 + 2), py0 + 1, {"light_on": True}),
        ("default_street_lamp", min(tw - 3, px1 - 2), py0 + 1, {"light_on": True}),
        ("default_street_lamp", max(2, px0 + 2), max(py0, py1 - 1), {"light_on": True}),
        ("default_street_lamp", min(tw - 3, px1 - 2), max(py0, py1 - 1), {"light_on": True}),
        (
            "default_notebook",
            px0 + 3,
            py0,
            {"name": "小镇布告栏", "brief": f"写着「{clean_name}」导览与留言的布告栏", "note_text": board_note},
        ),
        ("default_cat_cushion", min(tw - 3, px1 - 3), max(py0, py1 - 1), {}),
    ]
    if pond_campfire_cell is not None:
        cx_f, cy_f = pond_campfire_cell
        plaza_props.append(("default_campfire", cx_f, cy_f, {"light_on": True}))
        if cx_f - 1 >= 1:
            plaza_props.append(("default_snack", cx_f - 1, cy_f, {}))

    for bp_id, ox, oy, meta in plaza_props:
        try:
            ent = engine.place_from_blueprint(
                bp_id,
                resolved_tid,
                ox,
                oy,
                delivery_mode="silent",
                note_text=str(meta.get("note_text") or ""),
            )
            upd_traits = dict(ent.get("traits") or {})
            changed = False
            if meta.get("light_on") and "switchable" in upd_traits:
                upd_traits["switchable"] = {**upd_traits["switchable"], "is_on": True}
                changed = True
            if meta.get("name") or meta.get("brief") or changed:
                engine.update_entity_spec(
                    ent["entity_id"],
                    name=meta.get("name"),
                    brief=meta.get("brief"),
                    traits=upd_traits if changed else None,
                )
        except Exception:
            logger.debug("nanami_world: 生成城镇广场设施跳过 %s at (%d, %d)", bp_id, ox, oy, exc_info=True)

    # Layer 2: 确定性分区选址并构造各建筑子世界与家具
    slot_to_zone_id = {
        "nw": "north_west",
        "ne": "north_east",
        "sw": "south_west",
        "se": "south_east",
    }
    pref_w, pref_h = (6, 5) if (tw >= 36 and th >= 26) else (5, 4)
    home_variant = 0
    building_results: list[dict[str, Any]] = []
    for idx, b in enumerate(bldgs_raw):
        if not isinstance(b, Mapping):
            continue
        bname = clip_text(b.get("name"), 16, f"建筑{idx + 1}")
        role = venue_semantics.normalize_venue_role(b.get("role"), category=b.get("category"), name=bname)
        slot = str(b.get("zone_slot") or ("nw", "ne", "sw", "se")[idx % 4]).strip().lower()
        target_zid = slot_to_zone_id.get(slot, "north_west")
        try:
            lot = find_free_building_lot(
                engine,
                resolved_tid,
                ext_w=pref_w,
                ext_h=pref_h,
                zone_id=target_zid,
                road_tile=road_tile,
                reserved_cells=reserved_lot_cells,
            )
        except ValueError:
            logger.warning("nanami_world: 生成城镇跳过无法选址的建筑 %s", bname)
            continue

        iw, ih = (14, 10) if role in ("cafe", "arcade", "library", "home") and tw >= 36 else (12, 9)
        var_i = home_variant if role == "home" else idx
        if role == "home":
            home_variant += 1
        items = synthesize_interior_items(
            role,
            width=iw,
            height=ih,
            building_name=bname,
            note_text=str(b.get("note_text") or ""),
            variant=var_i,
        )
        res = build_building_subworld(
            engine,
            resolved_tid,
            building_id=f"bldg_{slugify(bname, prefix=str(idx + 1))}",
            name=bname,
            zone_id=target_zid,
            ext_x=lot["ext_x"],
            ext_y=lot["ext_y"],
            ext_w=lot["ext_w"],
            ext_h=lot["ext_h"],
            door_x=lot["door_x"],
            door_y=lot["door_y"],
            roof_color=str(b.get("roof_color") or "#a85646"),
            wall_tile=str(b.get("wall_tile") or "brick_wall"),
            road_tile=road_tile,
            category=str(b.get("category") or venue_semantics.default_category_for_role(role)),
            role=role,
            restock_profile=str(b.get("restock_profile") or ""),
            affordances=b.get("affordances"),
            icon=str(b.get("icon") or venue_semantics.default_icon_for_role(role)),
            walkway=lot["walkway"],
            interior_width=iw,
            interior_height=ih,
            interior_palette=str(b.get("interior_palette") or palette_id),
            interior_ambient=str(b.get("interior_ambient") or f"「{bname}」的室内空间，推门可回到「{clean_name}」。"),
            interior_floor_tile=str(b.get("interior_floor_tile") or "wood_floor"),
            interior_wall_tile=str(b.get("interior_wall_tile") or "wood_wall"),
            interior_items=items,
        )
        building_results.append(res)

    # Layer 3: 初始化主题天气、切换当前场景、放入角色并运行连通校验
    with engine._lock:
        sim_now = float(engine._world.get("simulation_minutes") or 0.0)
        allowed_w = list(town_spec.get("allowed_weathers") or weather.DEFAULT_ALLOWED_WEATHERS)
        init_w = str(town_spec.get("initial_weather") or allowed_w[0])
        engine._world.setdefault("weather_by_realm", {})[resolved_tid] = weather.default_weather_state(
            sim_now,
            mode="auto",
            current=init_w,
            allowed=allowed_w,
        )
        tsp = engine._world["spaces"].get(resolved_tid)
        if tsp is not None:
            engine._world.setdefault("shop_restock_day_by_realm", {})[resolved_tid] = engine._realm_business_day_locked(
                tsp, time.time(),
            )
        engine._save_world_locked()

    engine.switch_space(resolved_tid)
    if move_actor_id:
        with engine._lock:
            sp_now = engine._world["spaces"].get(resolved_tid)
            spawn_x, spawn_y = (
                engine._find_open_floor_cell_locked(sp_now, preferred_x=rx, preferred_y=ry + 1)
                if sp_now
                else (rx, ry + 1)
            )
        try:
            engine.place_actor(move_actor_id, resolved_tid, spawn_x, spawn_y)
        except ValueError:
            logger.debug("nanami_world: 放置默认角色到生成小镇跳过", exc_info=True)

    validation = validate_town_layout(engine, resolved_tid)
    return {
        "town_space_id": resolved_tid,
        "name": clean_name,
        "theme": str(town_spec.get("theme") or "sakura"),
        "created": True,
        "building_space_ids": [b["interior_space_id"] for b in building_results],
        "buildings": building_results,
        "validation": validation,
    }


def add_building_to_town(
    engine: WorldEngine,
    town_space_id: str,
    *,
    name: str,
    role: str = "public",
    zone_id: str = "",
    ext_w: int = 6,
    ext_h: int = 5,
    roof_color: str = "",
    wall_tile: str = "",
    interior_floor_tile: str = "",
    interior_wall_tile: str = "",
    interior_palette: str = "",
    interior_ambient: str = "",
    icon: str = "",
    note_text: str = "",
) -> dict[str, Any]:
    """在既有城镇空闲地带增量加盖一栋可进入建筑子世界，自动连接主路网并同步导览。"""
    resolved_town = engine.realm_space_id(town_space_id) if hasattr(engine, "realm_space_id") else town_space_id
    with engine._lock:
        tsp = engine._world["spaces"].get(resolved_town)
        if not tsp or tsp.get("space_kind") != "town":
            raise ValueError("只能在城镇地图内加盖独立建筑。")
        town_palette = str(tsp.get("palette_id") or "warm_stardew")
        town_name = str(tsp.get("name") or resolved_town)
        existing_bldg_count = len(tsp.get("buildings") or ())
        counts = _walkable_tile_counts(tsp)
        dominant_ground = counts.most_common(1)[0][0] if counts else "grass"
        if counts.get("cobble", 0) > 0 and dominant_ground != "cobble":
            road_tile = "cobble"
        elif counts.get("stone_tile", 0) > 0 and dominant_ground != "stone_tile":
            road_tile = "stone_tile"
        else:
            road_tile = "cobble" if dominant_ground != "cobble" else "stone_tile"

    clean_name = clip_text(name, 16, "新建建筑")
    norm_role = venue_semantics.normalize_venue_role(role, name=clean_name)
    lot = find_free_building_lot(
        engine,
        resolved_town,
        ext_w=ext_w,
        ext_h=ext_h,
        zone_id=zone_id,
        road_tile=road_tile,
    )

    role_defaults = {
        "home": ("#c9647a", "brick_wall", "wood_floor", "wood_wall"),
        "cafe": ("#c9885b", "white_wall", "dark_wood", "white_wall"),
        "shop": ("#5e7ea6", "wood_wall", "stone_tile", "brick_wall"),
        "library": ("#5a827e", "white_wall", "wood_floor", "wood_wall"),
        "arcade": ("#694d8c", "glass_wall", "carpet_blue", "glass_wall"),
        "public": ("#728699", "white_wall", "stone_tile", "white_wall"),
    }
    def_roof, def_wall, def_ifloor, def_iwall = role_defaults.get(norm_role, role_defaults["public"])
    iw, ih = (14, 10) if norm_role in ("cafe", "arcade", "library") else (12, 9)
    items = synthesize_interior_items(
        norm_role,
        width=iw,
        height=ih,
        building_name=clean_name,
        note_text=note_text,
        variant=existing_bldg_count,
    )
    res = build_building_subworld(
        engine,
        resolved_town,
        building_id=f"bldg_{slugify(clean_name, prefix='ext')}_{int(lot['ext_x'])}_{int(lot['ext_y'])}",
        name=clean_name,
        zone_id=zone_id,
        ext_x=lot["ext_x"],
        ext_y=lot["ext_y"],
        ext_w=lot["ext_w"],
        ext_h=lot["ext_h"],
        door_x=lot["door_x"],
        door_y=lot["door_y"],
        roof_color=roof_color or def_roof,
        wall_tile=wall_tile or def_wall,
        road_tile=road_tile,
        category=venue_semantics.default_category_for_role(norm_role),
        role=norm_role,
        restock_profile=venue_semantics.restock_profile_for_building({"role": norm_role, "name": clean_name}),
        affordances=venue_semantics.building_affordance_hints({"role": norm_role, "name": clean_name}),
        icon=icon or venue_semantics.default_icon_for_role(norm_role),
        walkway=lot["walkway"],
        interior_width=iw,
        interior_height=ih,
        interior_palette=interior_palette or town_palette,
        interior_ambient=interior_ambient or f"刚落成于「{town_name}」的「{clean_name}」，推门即可回到街道。",
        interior_floor_tile=interior_floor_tile or def_ifloor,
        interior_wall_tile=interior_wall_tile or def_iwall,
        interior_items=items,
    )

    with engine._lock:
        for ent in engine._world["entities"].values():
            if ent.get("space_id") != resolved_town or "inscribable" not in (ent.get("traits") or {}):
                continue
            if "布告栏" in str(ent.get("name") or "") or "notice_board" in (ent.get("tags") or ()):
                ins = ent["traits"]["inscribable"]
                entries = ins.setdefault("entries", [])
                max_e = int(ins.get("max_entries") or 12)
                if len(entries) < max_e:
                    entries.append({"by": "town", "ts": time.time(), "text": f"小镇新添了一处去处：「{clean_name}」。"})
                break
        engine._save_world_locked()

    res["validation"] = validate_town_layout(engine, resolved_town)
    return res


def validate_town_layout(engine: WorldEngine, town_space_id: str) -> dict[str, Any]:
    """确定性校验城镇布局：无建筑重叠、门洞双向绑定合法、广场到每扇门可达、室内家具可达。"""
    errors: list[str] = []
    with engine._lock:
        spaces = engine._world["spaces"]
        entities = engine._world["entities"]
        tsp = spaces.get(town_space_id)
        if not tsp or tsp.get("space_kind") != "town":
            return {"ok": False, "errors": [f"空间「{town_space_id}」不存在或不是城镇"], "building_count": 0, "reachable_doors": 0, "town_space_id": town_space_id}

        tw, th = int(tsp["width"]), int(tsp["height"])
        bldgs = [b for b in (tsp.get("buildings") or ()) if isinstance(b, Mapping)]

        # 1. Check zero building overlaps.
        cell_owner: dict[tuple[int, int], str] = {}
        for b in bldgs:
            bname = str(b.get("name") or b.get("building_id") or "")
            bx, by, bw, bh = int(b["x"]), int(b["y"]), int(b["w"]), int(b["h"])
            for cy in range(by, by + bh):
                for cx in range(bx, bx + bw):
                    prev = cell_owner.get((cx, cy))
                    if prev:
                        errors.append(f"建筑「{bname}」与「{prev}」在 ({cx}, {cy}) 重叠")
                        break
                    cell_owner[(cx, cy)] = bname

        # Build town walkability helper (ignoring actors and portal door entities themselves).
        town_solids: set[tuple[int, int]] = set()
        town_portals_at: dict[tuple[int, int], dict[str, Any]] = {}
        for ent in entities.values():
            if ent.get("space_id") != town_space_id or int(ent.get("z", 0)) < 0:
                continue
            traits = ent.get("traits") or {}
            if "portal" in traits:
                town_portals_at[(int(ent["x"]), int(ent["y"]))] = ent
                continue
            if ent.get("solid"):
                fw, fh = ent.get("footprint") or [1, 1]
                for dx in range(int(fw)):
                    for dy in range(int(fh)):
                        town_solids.add((int(ent["x"]) + dx, int(ent["y"]) + dy))

        def _town_passable(x: int, y: int) -> bool:
            return 0 <= x < tw and 0 <= y < th and _is_walkable_tile_id(_cell_tile_id(tsp, x, y)) and (x, y) not in town_solids

        # BFS from central spawn across town ground.
        rx, ry = engine._find_open_floor_cell_locked(tsp, preferred_x=tw // 2 - 1, preferred_y=th // 2)
        reachable_town: set[tuple[int, int]] = set()
        if _town_passable(rx, ry):
            q: deque[tuple[int, int]] = deque([(rx, ry)])
            reachable_town.add((rx, ry))
            while q:
                cx, cy = q.popleft()
                for dx, dy in ((0, 1), (0, -1), (1, 0), (-1, 0)):
                    nx, ny = cx + dx, cy + dy
                    if (nx, ny) not in reachable_town and _town_passable(nx, ny):
                        reachable_town.add((nx, ny))
                        q.append((nx, ny))
        else:
            errors.append(f"城镇广场落脚点 ({rx}, {ry}) 不可通行")

        reachable_doors = 0
        for b in bldgs:
            bname = str(b.get("name") or b.get("building_id") or "")
            int_sid = str(b.get("interior_space_id") or "")
            dx, dy = int(b["door_x"]), int(b["door_y"])
            if not int_sid or int_sid not in spaces:
                errors.append(f"建筑「{bname}」缺少子世界「{int_sid}」")
                continue
            int_sp = spaces[int_sid]
            if int_sp.get("space_kind") != "building" or int_sp.get("parent_space_id") != town_space_id:
                errors.append(f"建筑「{bname}」子世界层级关系无效")

            ext_ent = town_portals_at.get((dx, dy))
            if not ext_ent or (ext_ent.get("traits") or {}).get("portal", {}).get("target_space_id") != int_sid:
                errors.append(f"建筑「{bname}」在 ({dx}, {dy}) 缺少指向室内的正门传送点")

            if any((dx + ox, dy + oy) in reachable_town for ox, oy in ((0, 1), (0, -1), (1, 0), (-1, 0))):
                reachable_doors += 1
            else:
                errors.append(f"广场无法步行到达「{bname}」正门 ({dx}, {dy})")

            # Validate interior landing, exit portal, and interior entity reachability.
            iw, ih = int(int_sp["width"]), int(int_sp["height"])
            int_solids: set[tuple[int, int]] = set()
            int_entities: list[dict[str, Any]] = []
            exit_ent: dict[str, Any] | None = None
            for ent in entities.values():
                if ent.get("space_id") != int_sid or int(ent.get("z", 0)) < 0:
                    continue
                traits = ent.get("traits") or {}
                if "portal" in traits and traits["portal"].get("target_space_id") == town_space_id:
                    exit_ent = ent
                    continue
                int_entities.append(ent)
                if ent.get("solid"):
                    fw, fh = ent.get("footprint") or [1, 1]
                    for fx in range(int(fw)):
                        for fy in range(int(fh)):
                            int_solids.add((int(ent["x"]) + fx, int(ent["y"]) + fy))

            if exit_ent is None:
                errors.append(f"建筑「{bname}」室内缺少返回城镇的出口大门")
            else:
                ep = exit_ent["traits"]["portal"]
                tx, ty = int(ep.get("target_x", dx)), int(ep.get("target_y", dy + 1))
                if not _town_passable(tx, ty):
                    errors.append(f"建筑「{bname}」出门落脚点 ({tx}, {ty}) 被阻挡")

            ext_pt = ((ext_ent or {}).get("traits") or {}).get("portal") or {}
            land_x = int(ext_pt.get("target_x", iw // 2))
            land_y = int(ext_pt.get("target_y", max(1, ih - 2)))

            def _int_passable(x: int, y: int) -> bool:
                return (
                    0 <= x < iw and 0 <= y < ih
                    and not engine.is_wall_cell(int_sp, x, y)
                    and _is_walkable_tile_id(_cell_tile_id(int_sp, x, y))
                    and (x, y) not in int_solids
                )

            reach_int: set[tuple[int, int]] = set()
            if _int_passable(land_x, land_y):
                iq: deque[tuple[int, int]] = deque([(land_x, land_y)])
                reach_int.add((land_x, land_y))
                while iq:
                    cx, cy = iq.popleft()
                    for ox, oy in ((0, 1), (0, -1), (1, 0), (-1, 0)):
                        nx, ny = cx + ox, cy + oy
                        if (nx, ny) not in reach_int and _int_passable(nx, ny):
                            reach_int.add((nx, ny))
                            iq.append((nx, ny))
            else:
                errors.append(f"建筑「{bname}」室内入门落脚点 ({land_x}, {land_y}) 不可站立")

            for ent in int_entities:
                if ent.get("placement") == "wall" or int(ent.get("y", 0)) < int(int_sp.get("wall_rows", 1)):
                    continue
                ex, ey = int(ent["x"]), int(ent["y"])
                if not any((ex + ox, ey + oy) in reach_int for ox in (-1, 0, 1) for oy in (-1, 0, 1)):
                    errors.append(f"建筑「{bname}」室内物件「{ent['name']}」({ex}, {ey}) 不可到达")

        return {
            "ok": len(errors) == 0,
            "errors": errors,
            "building_count": len(bldgs),
            "reachable_doors": reachable_doors,
            "town_space_id": town_space_id,
        }
