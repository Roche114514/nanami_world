"""樱町默认示范小镇与可进入建筑子世界构造器（town_builder.py）。

职责：
- 构造一张 40x30 的多区域户外小镇（`town_sakura`）及其 6 栋可推门进入的独立建筑子世界；
- 提供通用的 `build_building_subworld()`，支持后续扩展或由玩家/LLM 新增可进入建筑；
- 全部逻辑仅依赖 Python 标准库与 `WorldEngine` 公开/内部原子操作，保证物质守恒、门洞双向绑定与 100% 连通性。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Mapping, Sequence

from .schema import clip_text, normalize_buildings, slugify

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
    category: str = "house",
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
    shell_cells.append({"x": dx, "y": dy, "tile_id": "cobble"})
    if dy + 1 < th:
        shell_cells.append({"x": dx, "y": dy + 1, "tile_id": "cobble"})
    for wcell in walkway or ():
        if isinstance(wcell, Mapping) and "x" in wcell and "y" in wcell:
            shell_cells.append({
                "x": int(wcell["x"]),
                "y": int(wcell["y"]),
                "tile_id": str(wcell.get("tile_id") or "cobble"),
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
            "category": category,
            "icon": icon,
        })
        tsp["buildings"] = normalize_buildings(existing, int(tsp["width"]), int(tsp["height"]))
        engine._save_world_locked()

    return {
        "building_id": bid,
        "name": clean_name,
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
