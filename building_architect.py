"""多层多隔间建筑室内生成器（Building Architect）。

在每个建筑内部按楼层尺寸与功能语义划分 2–4 个带实墙与关合 `interior_door`
（可推门点亮）的紧凑房间，并按照分级规则分配地下室（B1）/ 一层（1F）/ 二层（2F）/
三层阁楼（3F）与垂直对齐楼梯传送门。

Strictly Python stdlib + `.schema` / `.venue_semantics` / `.interior_designer` only.
"""

from __future__ import annotations

import copy
from contextlib import nullcontext
import random
import zlib
from typing import Any, Mapping, Sequence

from . import interior_designer, venue_semantics
from .schema import clamp_int, clip_text, normalize_buildings, normalize_zones

_LANDMARK_KWS: tuple[str, ...] = (
    "博物馆", "档案馆", "图书馆", "书局", "钟楼", "天文", "观星",
    "炼金", "剧场", "沙龙", "大旅馆", "公馆", "庄园", "汤屋", "总店",
)
_BASEMENT_HINTS: tuple[str, ...] = (
    "酒", "餐", "咖啡", "杂货", "商店", "百货", "炼金", "药",
    "工坊", "锻造", "铁匠", "码头", "仓库", "地下", "地窖",
)
_ATTIC_HINTS: tuple[str, ...] = (
    "天文", "观星", "钟楼", "图书", "书房", "书斋", "书店",
    "音乐", "钢琴", "茶室", "公馆", "庄园",
)

_ROOM_LABELS: dict[str, str] = {
    "foyer_lounge": "迎客厅",
    "cafe_front": "点单前厅",
    "ramen_front": "拉面客席",
    "shop_front": "货架前厅",
    "bookshop_front": "选书大厅",
    "arcade_hall": "游艺主厅",
    "kitchen_back": "后厨灶间",
    "storage_room": "储物库房",
    "bedroom": "静谧卧房",
    "study_room": "静读室",
    "onsen_bath": "汤池沐浴间",
    "cellar_b1": "地下窖室",
    "attic_3f": "阁楼瞭望室",
    "hallway": "楼梯走廊",
    "hearth_lounge": "壁炉起居间",
    "alchemy_lab": "炼金实验室",
    "music_salon": "琴房沙龙",
    "clockwork_room": "齿轮机房",
    "clinic_room": "诊疗调药室",
    "workshop_room": "手作工坊间",
}

_ROOM_COLORS: dict[str, str] = {
    "foyer_lounge": "#f0c869",
    "cafe_front": "#f2a65a",
    "ramen_front": "#eb8b38",
    "shop_front": "#7cc6d6",
    "bookshop_front": "#8cb8de",
    "arcade_hall": "#ff66c4",
    "kitchen_back": "#e88b5a",
    "storage_room": "#b89f82",
    "bedroom": "#c49be8",
    "study_room": "#7db8d6",
    "onsen_bath": "#69b9d6",
    "cellar_b1": "#9d8bb8",
    "attic_3f": "#d9b8ff",
    "hallway": "#d4c4a8",
    "hearth_lounge": "#f59e42",
    "alchemy_lab": "#69e3c6",
    "music_salon": "#e0b65c",
    "clockwork_room": "#7ae7ff",
    "clinic_room": "#7ed6df",
    "workshop_room": "#d99241",
}


def floor_label(floor_index: int) -> str:
    """将数字楼层索引映射为展示标签（如 -1 -> 'B1', 1 -> '1F', 2 -> '2F'）。"""
    fi = int(floor_index)
    if fi < 0:
        return f"B{abs(fi)}"
    return f"{max(1, fi)}F"


def choose_building_floor_plan(
    role: str,
    name: str,
    *,
    bed_count: int = 0,
    ext_w: int = 5,
    is_landmark: bool = False,
    special_budget_left: int = 0,
    seed: int = 0,
) -> list[int]:
    """按 2.A 分级覆盖规则为单栋建筑决定楼层方案。"""
    norm_role = venue_semantics.normalize_venue_role(role, name=name)
    text = str(name or "").casefold()
    kw_landmark = any(kw in text for kw in _LANDMARK_KWS)

    if special_budget_left > 0 and (is_landmark or kw_landmark or ext_w >= 7 or norm_role == "library"):
        prefer_attic = any(kw in text for kw in _ATTIC_HINTS)
        prefer_basement = any(kw in text for kw in _BASEMENT_HINTS)
        if prefer_basement and not prefer_attic:
            return [-1, 1, 2]
        if prefer_attic and not prefer_basement:
            return [1, 2, 3]
        return [-1, 1, 2] if (int(seed) % 2 == 0) else [1, 2, 3]

    if (
        norm_role in ("cafe", "library", "arcade")
        or (norm_role == "home" and (bed_count >= 2 or ext_w >= 6))
        or (norm_role in ("shop", "public") and ext_w >= 6)
    ):
        return [1, 2]

    return [1]


def _pick_door_on_span(
    candidates: Sequence[int],
    avoid_xs: set[int],
    rng: random.Random,
) -> int:
    clean = [x for x in candidates if x not in avoid_xs]
    if clean:
        return rng.choice(clean)
    return candidates[len(candidates) // 2]


def _resolve_floor_room_kinds(
    *,
    role: str,
    building_name: str,
    floor_index: int,
    theme_key: str,
    room_count: int,
    bed_count: int,
    used_landmarks: set[str] | None,
) -> list[str]:
    """为当前楼层的隔间槽位挑选合理不重复的主题房间类型（第 0 槽固定为入口厅/走廊）。"""
    norm_role = venue_semantics.normalize_venue_role(role, name=building_name)
    text = str(building_name or "").casefold()
    resolved_theme = theme_key or interior_designer.infer_theme_key(building_name)
    if resolved_theme == "observatory":
        # 天文馆复用安静书房与茶室家具族，不生成观星仪式祭坛
        resolved_theme = "archive"

    can_onsen = (
        resolved_theme == "onsen"
        and floor_index == 1
        and norm_role not in ("cafe", "shop", "library", "arcade")
        and (used_landmarks is None or "hot_spring_tub" not in used_landmarks)
    )

    if floor_index < 0:
        kinds = ["hallway", "cellar_b1", "storage_room", "alchemy_lab"]
    elif floor_index >= 3:
        kinds = ["hallway", "attic_3f", "study_room", "hearth_lounge"]
    elif floor_index == 2:
        if bed_count >= 2:
            kinds = ["hallway", "bedroom", "bedroom", "study_room"]
        elif bed_count == 1:
            kinds = ["hallway", "bedroom", "study_room", "hearth_lounge"]
        elif norm_role in ("library",):
            kinds = ["hallway", "study_room", "storage_room", "hearth_lounge"]
        elif norm_role in ("cafe", "shop"):
            kinds = ["hallway", "hearth_lounge", "storage_room", "study_room"]
        else:
            kinds = ["hallway", "study_room", "hearth_lounge", "storage_room"]
    else:
        if norm_role == "cafe":
            front = "ramen_front" if any(k in text for k in ("拉面", "面馆", "ramen")) else "cafe_front"
            kinds = [front, "kitchen_back", "storage_room", "hearth_lounge"]
        elif norm_role == "shop":
            kinds = ["shop_front", "storage_room", "study_room", "kitchen_back"]
        elif norm_role == "library":
            kinds = ["bookshop_front", "study_room", "storage_room", "hearth_lounge"]
        elif norm_role == "arcade":
            kinds = ["arcade_hall", "foyer_lounge", "storage_room", "kitchen_back"]
        elif norm_role == "home":
            kinds = ["foyer_lounge", "kitchen_back", "bedroom" if bed_count > 0 else "study_room", "storage_room"]
        else:
            if can_onsen:
                kinds = ["foyer_lounge", "onsen_bath", "hearth_lounge", "storage_room"]
            elif resolved_theme == "alchemy":
                kinds = ["foyer_lounge", "alchemy_lab", "storage_room", "study_room"]
            elif resolved_theme == "music":
                kinds = ["foyer_lounge", "music_salon", "hearth_lounge", "study_room"]
            elif resolved_theme == "clockwork":
                kinds = ["foyer_lounge", "clockwork_room", "workshop_room", "storage_room"]
            elif resolved_theme == "medical":
                kinds = ["foyer_lounge", "clinic_room", "storage_room", "study_room"]
            elif resolved_theme == "workshop":
                kinds = ["workshop_room", "storage_room", "kitchen_back", "study_room"]
            elif any(k in text for k in ("壁炉", "暖炉", "围炉")):
                kinds = ["hearth_lounge", "kitchen_back", "study_room", "storage_room"]
            else:
                kinds = ["foyer_lounge", "hearth_lounge", "study_room", "storage_room"]

    return kinds[:room_count]


def plan_floor_rooms(
    width: int,
    height: int,
    *,
    wall_rows: int = 1,
    role: str = "public",
    building_name: str = "",
    floor_index: int = 1,
    theme_key: str = "",
    external_doors: Sequence[tuple[int, int]] = (),
    stair_coords: Sequence[tuple[int, int]] = (),
    build_bottom_wall: bool = True,
    seed: int = 0,
    used_landmarks: set[str] | None = None,
) -> dict[str, Any]:
    """将单层建筑矩形切分为 2–4 个带实墙与推门阈值的紧凑功能房间，并计算北墙采光窗位。"""
    w = int(width)
    h = int(height)
    top = max(0, int(wall_rows))
    bot_y = h - 2 if build_bottom_wall and h >= 5 else h - 1
    inner_h = bot_y - top + 1
    rng = random.Random(int(seed) if seed else zlib.crc32(f"{w}x{h}|{building_name}|{floor_index}".encode("utf-8")))

    ext_pts = [(int(dx), int(dy)) for dx, dy in external_doors]
    stair_pts = [(int(sx), int(sy)) for sx, sy in stair_coords]
    anchor_pts = [
        (px, min(bot_y, max(top, py if py < h - 1 else bot_y)))
        for px, py in ext_pts
    ] + stair_pts

    protected_xs: set[int] = set()
    protected_ys: set[int] = set()
    for ax, ay in anchor_pts:
        protected_xs.add(ax)
        protected_ys.add(ay)
    for sx, sy in stair_pts:
        protected_xs.update((sx - 1, sx, sx + 1))

    rooms_raw: list[dict[str, Any]] = []
    wall_cells: set[tuple[int, int]] = set()
    interior_doors: list[dict[str, Any]] = []
    layout_mode = "single"

    # 1. T-split (+ optional side quad room): South entry corridor/hall + 2 enclosed north rooms
    if w >= 12 and inner_h >= 7:
        valid_ys = [
            y for y in range(top + 3, bot_y - 2)
            if y not in protected_ys and all(ay > y for _, ay in anchor_pts)
        ]
        if valid_ys:
            split_y = min(valid_ys, key=lambda y: abs(y - (top + inner_h // 2)))
            north_h = split_y - top
            south_y = split_y + 1
            south_h = bot_y - split_y
            split_x = clamp_int(w // 2 + (rng.choice((-1, 0, 1)) if w >= 15 else 0), 4, w - 5, w // 2)

            # South entry hall (0)
            entry_rect = (0, south_y, w, south_h)
            nw_rect = (0, top, split_x, north_h)
            ne_rect = (split_x + 1, top, w - split_x - 1, north_h)

            for x in range(w):
                wall_cells.add((x, split_y))
            for y in range(top, split_y):
                wall_cells.add((split_x, y))

            # Carve a 4th side room off the South hall FIRST so north doors always open directly into Entry Hall.
            side_room: dict[str, Any] | None = None
            nw_lo = 1
            ne_hi = w - 1
            side_avoid = set(protected_xs)
            if w >= 16 and inner_h >= 9 and south_h >= 3:
                sw_vx = 5
                se_vx = w - 6
                if sw_vx + 2 <= split_x - 1 and all(ax > sw_vx + 1 for ax, _ in anchor_pts):
                    entry_rect = (sw_vx + 1, south_y, w - sw_vx - 1, south_h)
                    for y in range(south_y, bot_y + 1):
                        wall_cells.add((sw_vx, y))
                    side_door_y = south_y + south_h // 2
                    wall_cells.discard((sw_vx, side_door_y))
                    interior_doors.append({"x": sw_vx, "y": side_door_y, "room_idx": 3})
                    side_room = {"rect": (0, south_y, sw_vx, south_h), "north_wall_y": split_y, "is_entry": False}
                    nw_lo = sw_vx + 1
                    side_avoid.add(sw_vx)
                    layout_mode = "quad_split"
                elif se_vx - 2 >= split_x + 1 and all(ax < se_vx - 1 for ax, _ in anchor_pts):
                    entry_rect = (0, south_y, se_vx, south_h)
                    for y in range(south_y, bot_y + 1):
                        wall_cells.add((se_vx, y))
                    side_door_y = south_y + south_h // 2
                    wall_cells.discard((se_vx, side_door_y))
                    interior_doors.append({"x": se_vx, "y": side_door_y, "room_idx": 3})
                    side_room = {"rect": (se_vx + 1, south_y, w - se_vx - 1, south_h), "north_wall_y": split_y, "is_entry": False}
                    ne_hi = se_vx
                    side_avoid.add(se_vx)
                    layout_mode = "quad_split"

            door_nw_x = _pick_door_on_span(list(range(nw_lo, max(nw_lo + 1, split_x - 1))), side_avoid, rng)
            door_ne_x = _pick_door_on_span(list(range(split_x + 2, max(split_x + 3, ne_hi))), side_avoid, rng)

            wall_cells.discard((door_nw_x, split_y))
            wall_cells.discard((door_ne_x, split_y))
            interior_doors.insert(0, {"x": door_nw_x, "y": split_y, "room_idx": 1})
            interior_doors.insert(1, {"x": door_ne_x, "y": split_y, "room_idx": 2})

            rooms_raw = [
                {"rect": entry_rect, "north_wall_y": -1, "is_entry": True},
                {"rect": nw_rect, "north_wall_y": 0 if top >= 1 else -1, "is_entry": False},
                {"rect": ne_rect, "north_wall_y": 0 if top >= 1 else -1, "is_entry": False},
            ]
            if side_room is not None:
                rooms_raw.append(side_room)
            if layout_mode == "single":
                layout_mode = "t_split"

    # 2. Compact 2-room fallback (horizontal split first, then vertical split)
    if not rooms_raw and w >= 10 and inner_h >= 7:
        valid_ys = [
            y for y in range(top + 3, bot_y - 2)
            if y not in protected_ys and all(ay > y for _, ay in anchor_pts)
        ]
        if valid_ys:
            split_y = valid_ys[len(valid_ys) // 2]
            for x in range(w):
                wall_cells.add((x, split_y))
            dx_door = _pick_door_on_span(list(range(2, w - 2)), protected_xs, rng)
            wall_cells.discard((dx_door, split_y))
            rooms_raw = [
                {"rect": (0, split_y + 1, w, bot_y - split_y), "north_wall_y": -1, "is_entry": True},
                {"rect": (0, top, w, split_y - top), "north_wall_y": 0 if top >= 1 else -1, "is_entry": False},
            ]
            interior_doors.append({"x": dx_door, "y": split_y, "room_idx": 1})
            layout_mode = "h_split_2"

    if not rooms_raw and w >= 11 and inner_h >= 6:
        for split_x in sorted(range(4, w - 4), key=lambda x: abs(x - w // 2)):
            if split_x in protected_xs:
                continue
            on_left = all(ax < split_x for ax, _ in anchor_pts)
            on_right = all(ax > split_x for ax, _ in anchor_pts)
            if on_left or on_right:
                for y in range(top, bot_y + 1):
                    wall_cells.add((split_x, y))
                dy_door = top + max(1, inner_h // 2)
                wall_cells.discard((split_x, dy_door))
                left_r = {"rect": (0, top, split_x, inner_h), "north_wall_y": 0 if top >= 1 else -1, "is_entry": on_left}
                right_r = {"rect": (split_x + 1, top, w - split_x - 1, inner_h), "north_wall_y": 0 if top >= 1 else -1, "is_entry": on_right}
                rooms_raw = [left_r, right_r] if on_left else [right_r, left_r]
                interior_doors.append({"x": split_x, "y": dy_door, "room_idx": 1})
                layout_mode = "v_split_2"
                break

    if not rooms_raw:
        rooms_raw = [{"rect": (0, top, w, max(1, inner_h)), "north_wall_y": 0 if top >= 1 else -1, "is_entry": True}]

    # Assign semantic room_kinds, labels, colors
    kinds = _resolve_floor_room_kinds(
        role=role,
        building_name=building_name,
        floor_index=floor_index,
        theme_key=theme_key,
        room_count=len(rooms_raw),
        bed_count=0,
        used_landmarks=used_landmarks,
    )
    used_names: set[str] = set()
    rooms: list[dict[str, Any]] = []
    for idx, rraw in enumerate(rooms_raw):
        rk = kinds[idx] if idx < len(kinds) else "storage_room"
        base_name = _ROOM_LABELS.get(rk, f"隔间{idx + 1}")
        rname = base_name if base_name not in used_names else f"{base_name}{idx + 1}"
        used_names.add(rname)
        rooms.append({
            "room_index": idx,
            "room_id": f"rm_{floor_label(floor_index).lower()}_{idx + 1}",
            "name": rname,
            "room_kind": rk,
            "color": _ROOM_COLORS.get(rk, "#ffd166"),
            "rect": tuple(rraw["rect"]),
            "north_wall_y": int(rraw["north_wall_y"]),
            "is_entry": bool(rraw["is_entry"]),
            "enclosed": not bool(rraw["is_entry"]) and len(rooms_raw) > 1,
        })

    for d_info in interior_doors:
        r_idx = int(d_info["room_idx"])
        r_name = rooms[r_idx]["name"] if 0 <= r_idx < len(rooms) else "内室"
        d_info["name"] = clip_text(f"{r_name}木门", 16, "内室木门")
        d_info["brief"] = clip_text(f"通往「{r_name}」的木门，推开后能点亮门后空间。", 64)

    # North-wall "glass_wall" windows (only on ground/upper floors; basement B1 uses vents)
    wall_col_xs = {wx for wx, wy in wall_cells if wy == top}
    windows: list[tuple[int, int]] = []
    if floor_index >= 1 and top >= 1:
        for rm in rooms:
            rx, ry, rw, _ = rm["rect"]
            if ry != top or rw < 3:
                continue
            inner_xs = [x for x in range(rx + 1, rx + rw - 1) if x not in wall_col_xs]
            if not inner_xs:
                continue
            mid = inner_xs[len(inner_xs) // 2]
            windows.append((mid, 0))
            if rw >= 6 and (mid + 1) in inner_xs and len(windows) < 6:
                windows.append((mid + 1, 0))

    return {
        "layout_mode": layout_mode,
        "rooms": rooms,
        "wall_cells": sorted(wall_cells),
        "interior_doors": interior_doors,
        "windows": sorted(set(windows)),
        "bot_y": bot_y,
    }


def _place_doormat_near_entry(
    engine: Any,
    space_id: str,
    *,
    door_coords: Sequence[tuple[int, int]],
    stair_set: set[tuple[int, int]],
    bot_y: int,
    palette_id: str,
    theme_key: str,
) -> dict[str, Any] | None:
    if not door_coords:
        return None
    dx, dy = int(door_coords[0][0]), int(door_coords[0][1])
    target_pt = (dx, min(bot_y, dy - 1 if dy > bot_y else dy))
    if target_pt in stair_set:
        return None
    bp = interior_designer.synthesize_furniture_blueprint(
        {"motif": "doormat", "name": "迎宾编织脚垫", "brief": "铺在门内正前方的剑麻迎宾垫。"},
        palette_id=palette_id,
        theme_key=theme_key,
    )
    try:
        return engine.place_ephemeral_entity(space_id, bp, x=target_pt[0], y=target_pt[1])
    except ValueError:
        return None


def populate_multiroom_floor(
    engine: Any,
    space_id: str,
    *,
    role: str = "public",
    building_name: str = "空间",
    floor_index: int = 1,
    floor_tile: str = "wood_floor",
    rug_tile: str = "carpet_blue",
    wall_tile: str = "wood_wall",
    bed_count: int = 0,
    note_text: str = "",
    door_coords: Sequence[tuple[int, int]] = (),
    stair_coords: Sequence[tuple[int, int]] = (),
    build_perimeter_bottom_wall: bool = True,
    world_prompt: str = "",
    scene_items: Sequence[Mapping[str, Any]] = (),
    brief: str = "",
    detail: str = "",
    seed: int | None = None,
    used_landmarks: set[str] | None = None,
    is_standalone: bool = False,
) -> dict[str, Any]:
    """在单层楼面 Space 内划分隔间实墙、安装门洞、并高密度布置家具/壁饰/桌面杂物。"""
    del world_prompt, brief, detail, is_standalone
    with engine._lock:
        sp = engine._world["spaces"].get(space_id)
        if sp is None:
            raise ValueError(f"空间不存在：{space_id}")
        w, h = int(sp["width"]), int(sp["height"])
        wall_rows = int(sp.get("wall_rows", 1))
        pal = str(sp.get("palette_id") or "warm_stardew")

    norm_role = venue_semantics.normalize_venue_role(role, name=building_name)
    resolved_theme = interior_designer.infer_theme_key(building_name, pal)
    ft = floor_tile or "wood_floor"
    rt = rug_tile or "carpet_blue"
    if rt == ft:
        rt = "tatami" if ft != "tatami" else "carpet_blue"
    wt = wall_tile or "wood_wall"
    resolved_seed = (
        int(seed) if seed is not None
        else (random.SystemRandom().randint(1, 2_000_000_000) ^ zlib.crc32(f"{space_id}|{floor_index}".encode("utf-8")))
    )

    doors = {(int(dx), int(dy)) for dx, dy in door_coords}
    stairs = {(int(sx), int(sy)) for sx, sy in stair_coords}
    plan = plan_floor_rooms(
        w,
        h,
        wall_rows=wall_rows,
        role=norm_role,
        building_name=building_name,
        floor_index=floor_index,
        theme_key=resolved_theme,
        external_doors=sorted(doors),
        stair_coords=sorted(stairs),
        build_bottom_wall=build_perimeter_bottom_wall,
        seed=resolved_seed,
        used_landmarks=used_landmarks,
    )

    all_bps = engine.all_blueprints(space_id, include_ephemeral=False)
    batch_cm = engine.batch_mutations() if hasattr(engine, "batch_mutations") else nullcontext()
    with batch_cm:
        tile_ops: list[dict[str, Any]] = []
        if wall_rows > 0:
            for y in range(wall_rows):
                for x in range(w):
                    tile_ops.append({"x": x, "y": y, "tile_id": wt})
        for y in range(wall_rows, h):
            for x in range(w):
                tile_ops.append({"x": x, "y": y, "tile_id": ft})
        if build_perimeter_bottom_wall and h >= 5:
            for x in range(w):
                is_door_col = any(dx == x and dy == h - 1 for dx, dy in doors)
                tile_ops.append({"x": x, "y": h - 1, "tile_id": ft if is_door_col else wt})

        for wx, wy in plan["wall_cells"]:
            tile_ops.append({"x": wx, "y": wy, "tile_id": wt})
        for d_info in plan["interior_doors"]:
            tile_ops.append({"x": d_info["x"], "y": d_info["y"], "tile_id": ft})
        for wx, wy in plan["windows"]:
            tile_ops.append({"x": wx, "y": wy, "tile_id": "glass_wall"})
        for dx, dy in doors:
            if dy >= wall_rows:
                tile_ops.append({"x": dx, "y": dy, "tile_id": ft})

        engine.paint_tiles(space_id, tile_ops)

        # Spawn closed interior_door entities & entrance doormat
        interior_door_ids: list[str] = []
        door_clear_cells: set[tuple[int, int]] = set()
        bot_y = int(plan["bot_y"])
        for dx, dy in doors:
            land_y = bot_y if dy >= h - 1 else dy
            door_clear_cells.add((dx, land_y))
            if land_y - 1 >= wall_rows:
                door_clear_cells.add((dx, land_y - 1))

        for d_info in plan["interior_doors"]:
            ix, iy = int(d_info["x"]), int(d_info["y"])
            door_bp = interior_designer.synthesize_furniture_blueprint(
                {
                    "motif": "interior_door",
                    "name": d_info["name"],
                    "brief": d_info["brief"],
                },
                palette_id=pal,
                theme_key=resolved_theme,
            )
            dent = engine.place_ephemeral_entity(space_id, door_bp, x=ix, y=iy)
            interior_door_ids.append(dent["entity_id"])
            for ox, oy in ((0, -1), (0, 1), (-1, 0), (1, 0)):
                door_clear_cells.add((ix + ox, iy + oy))

        _place_doormat_near_entry(
            engine,
            space_id,
            door_coords=sorted(doors),
            stair_set=stairs,
            bot_y=bot_y,
            palette_id=pal,
            theme_key=resolved_theme,
        )

        rooms: list[dict[str, Any]] = plan["rooms"]
        # Upgrade primary non-entry room on upper bedroom floor into bedroom if bed_count > 0 and no bedroom tagged yet
        beds_left = max(0, int(bed_count))
        bed_alloc: dict[int, int] = {i: 0 for i in range(len(rooms))}
        if beds_left > 0 and rooms:
            bed_rooms = [r["room_index"] for r in rooms if r["room_kind"] == "bedroom"]
            if not bed_rooms:
                quiet_rooms = [r["room_index"] for r in rooms if not r["is_entry"]] or [0]
                first_q = quiet_rooms[0]
                rooms[first_q]["room_kind"] = "bedroom"
                if rooms[first_q]["name"].startswith("静读室"):
                    rooms[first_q]["name"] = "静谧寝室"
                bed_rooms = quiet_rooms
            for bi in range(beds_left):
                r_target = bed_rooms[bi % len(bed_rooms)]
                bed_alloc[r_target] += 1

        primary_custom_idx = next((r["room_index"] for r in rooms if not r["is_entry"]), 0) if len(rooms) > 1 else 0
        total_placed = 0
        total_beds = 0
        all_dropped: list[str] = []
        zones_out: list[dict[str, Any]] = []

        for idx, rm in enumerate(rooms):
            rx, ry, rw, rh = rm["rect"]
            sub_res = interior_designer.populate_subroom_interior(
                engine,
                space_id,
                rect=rm["rect"],
                room_kind=rm["room_kind"],
                room_name=rm["name"],
                palette_id=pal,
                theme_key=resolved_theme,
                floor_tile=ft,
                rug_tile=rt if rm["room_kind"] != "hallway" else ft,
                bed_count=bed_alloc.get(idx, 0),
                custom_items=scene_items if idx == primary_custom_idx else (),
                note_text=note_text if idx == primary_custom_idx else "",
                door_clear_cells=door_clear_cells,
                stair_cells=stairs,
                north_wall_y=int(rm["north_wall_y"]),
                allow_wall_hang=(int(rm["north_wall_y"]) >= 0),
                used_landmarks=used_landmarks,
                all_bps=all_bps,
                seed=resolved_seed + idx * 97,
            )
            total_placed += len(sub_res["placed_entities"])
            total_beds += int(sub_res["beds_placed"])
            all_dropped.extend(sub_res["dropped"])
            zones_out.append({
                "zone_id": rm["room_id"],
                "name": rm["name"],
                "x": rx,
                "y": ry,
                "w": rw,
                "h": rh,
                "color": rm["color"],
                "description": f"位于「{building_name}」的{rm['name']}。",
                "enclosed": bool(rm["enclosed"]),
            })

        # Fallback: if a tiny enclosed room dropped a bed, place remaining beds in entry/largest room.
        remaining_beds = beds_left - total_beds
        if remaining_beds > 0 and len(rooms) > 1:
            fallback_rooms = sorted(rooms, key=lambda r: r["rect"][2] * r["rect"][3], reverse=True)
            for frm in fallback_rooms:
                if remaining_beds <= 0:
                    break
                extra_res = interior_designer.populate_subroom_interior(
                    engine,
                    space_id,
                    rect=frm["rect"],
                    room_kind="bedroom",
                    room_name=frm["name"],
                    palette_id=pal,
                    theme_key=resolved_theme,
                    floor_tile=ft,
                    rug_tile=rt,
                    bed_count=remaining_beds,
                    door_clear_cells=door_clear_cells,
                    stair_cells=stairs,
                    north_wall_y=-1,
                    allow_wall_hang=False,
                    used_landmarks=used_landmarks,
                    all_bps=all_bps,
                    seed=resolved_seed + 999,
                )
                placed_now = int(extra_res["beds_placed"])
                total_beds += placed_now
                total_placed += len(extra_res["placed_entities"])
                remaining_beds -= placed_now

        norm_zones = normalize_zones(zones_out, w, h)
        with engine._lock:
            sp_live = engine._world["spaces"][space_id]
            sp_live["zones"] = norm_zones
            engine._save_world_locked()

    return {
        "space_id": space_id,
        "layout_mode": plan["layout_mode"],
        "rooms": rooms,
        "interior_door_ids": interior_door_ids,
        "window_cells": plan["windows"],
        "beds_placed": total_beds,
        "zones": norm_zones,
        "placed_count": total_placed,
        "dropped": all_dropped,
    }


def _pick_stair_x(w: int, avoid_xs: set[int]) -> int:
    candidates = [
        x for x in (w - 3, 2, w - 4, 3, w // 2 - 2, w // 2 + 2)
        if 1 <= x <= w - 2 and x not in avoid_xs
    ]
    if candidates:
        return candidates[0]
    fallback = [x for x in range(1, max(2, w - 1)) if x not in avoid_xs]
    return fallback[0] if fallback else max(1, min(w - 2, w // 2))


def _place_stair_portal(
    engine: Any,
    *,
    src_sid: str,
    src_xy: tuple[int, int],
    dst_sid: str,
    dst_xy: tuple[int, int],
    motif: str,
    name: str,
    brief: str,
    palette_id: str,
    theme_key: str,
) -> dict[str, Any]:
    bp = interior_designer.synthesize_furniture_blueprint(
        {
            "motif": motif,
            "name": name,
            "brief": brief,
            "traits": {
                "portal": {
                    "target_space_id": dst_sid,
                    "target_x": int(dst_xy[0]),
                    "target_y": int(dst_xy[1]),
                    "is_open": True,
                }
            },
        },
        palette_id=palette_id,
        theme_key=theme_key,
    )
    return engine.place_ephemeral_entity(src_sid, bp, x=int(src_xy[0]), y=int(src_xy[1]))


def _populate_activity_1f_floor(
    engine: Any,
    space_id: str,
    *,
    role: str,
    building_name: str,
    floor_tile: str,
    rug_tile: str,
    wall_tile: str,
    bed_count: int,
    note_text: str,
    door_coords: Sequence[tuple[int, int]],
    stair_coords: Sequence[tuple[int, int]],
    world_prompt: str,
    scene_items: Sequence[Mapping[str, Any]],
    brief: str,
    detail: str,
    seed: int,
    used_landmarks: set[str] | None,
) -> dict[str, Any]:
    """针对带精选 activity_layout 的 1F 场馆保留其经典桌面/吧台动线与无隔墙开间布局。"""
    with engine._lock:
        sp = engine._world["spaces"].get(space_id)
        if sp is None:
            raise ValueError(f"空间不存在：{space_id}")
        w, h = int(sp["width"]), int(sp["height"])
        wall_rows = int(sp.get("wall_rows", 1))
        pal = str(sp.get("palette_id") or "warm_stardew")

    norm_role = venue_semantics.normalize_venue_role(role, name=building_name)
    ft = floor_tile or "wood_floor"
    rt = rug_tile or ("tatami" if norm_role == "home" else "carpet_blue")
    if rt == ft:
        rt = "tatami" if ft != "tatami" else "carpet_blue"
    wt = wall_tile or "wood_wall"
    doors = {(int(dx), int(dy)) for dx, dy in door_coords}
    stairs = {(int(sx), int(sy)) for sx, sy in stair_coords}

    tile_ops: list[dict[str, Any]] = []
    if wall_rows > 0:
        for y in range(wall_rows):
            for x in range(w):
                tile_ops.append({"x": x, "y": y, "tile_id": wt})
    for y in range(wall_rows, h):
        for x in range(w):
            tile_ops.append({"x": x, "y": y, "tile_id": ft})
    if h >= 5:
        for x in range(w):
            tile_ops.append({"x": x, "y": h - 1, "tile_id": ft if any(dx == x and dy == h - 1 for dx, dy in doors) else wt})
    for dx, dy in doors:
        if dy >= wall_rows:
            tile_ops.append({"x": dx, "y": dy, "tile_id": ft})
    engine.paint_tiles(space_id, tile_ops)

    extra_items = [dict(x) for x in (scene_items or ()) if isinstance(x, Mapping)]
    clean_note = clip_text(note_text, 80, "")
    if clean_note and not any(x.get("blueprint_id") == "default_notebook" for x in extra_items):
        extra_items.append({
            "ref": "room_note",
            "blueprint_id": "default_notebook",
            "name": "货品账本" if norm_role == "shop" else "室内记录本",
            "relation": "on_surface",
            "note_text": clean_note,
        })

    clean_name = clip_text(building_name, 16, "房间")
    room_spec: dict[str, Any] = {
        "space_id": space_id,
        "name": clean_name,
        "kind": norm_role,
        "role": norm_role,
        "is_standalone": False,
        "brief": brief or clean_note or f"「{clean_name}」室内场景",
        "detail": detail,
        "palette_id": pal,
        "w": w,
        "h": h,
        "bed_count": max(0, int(bed_count)),
        "partition_style": "alcove" if norm_role in ("home", "library") else "none",
        "room_shape": "",
        "rug_style": "",
        "floor_tile": ft,
        "rug_tile": rt,
        "wall_tile": wt,
        "furniture_clusters": (),
        "scene_items": extra_items,
    }
    sg = interior_designer.design_room_scene_graph(
        room_spec,
        world_prompt=world_prompt,
        seed=seed,
        used_landmarks=used_landmarks,
    )
    placed_info = interior_designer.solve_and_place_scene_graph(
        engine,
        space_id,
        room_spec,
        sg,
        door_cells=doors,
        reserved_cells=stairs,
        seed=seed,
    )
    zones = placed_info.get("zones") or ()
    if zones:
        with engine._lock:
            engine._world["spaces"][space_id]["zones"] = normalize_zones(zones, w, h)
            engine._save_world_locked()
    with engine._lock:
        beds_placed = sum(
            1 for e in engine._world["entities"].values()
            if e.get("space_id") == space_id and e.get("blueprint_id") == "default_bed"
        )
    return {"space_id": space_id, "beds_placed": beds_placed, "placed_count": int(placed_info.get("placed_count") or 0)}


def build_multifloor_building_interiors(
    engine: Any,
    *,
    town_space_id: str,
    space_1f_id: str,
    building_meta: Mapping[str, Any],
    role: str,
    building_name: str,
    floor_plan: Sequence[int],
    floor_tile: str = "wood_floor",
    rug_tile: str = "carpet_blue",
    wall_tile: str = "wood_wall",
    bed_count: int = 0,
    note_text: str = "",
    door_coords_1f: Sequence[tuple[int, int]] = (),
    world_prompt: str = "",
    scene_items: Sequence[Mapping[str, Any]] = (),
    brief: str = "",
    detail: str = "",
    seed: int = 0,
    used_landmarks: set[str] | None = None,
) -> dict[str, Any]:
    """为单栋建筑建造 1F 及附属楼层（B1 / 2F / 3F），划分多隔间并铺设双向对齐楼梯传送门。"""
    del town_space_id
    with engine._lock:
        sp_1f = copy.deepcopy(engine._world["spaces"].get(space_1f_id))
        if sp_1f is None:
            raise ValueError(f"一层空间不存在：{space_1f_id}")

    w1 = int(sp_1f["width"])
    h1 = int(sp_1f["height"])
    pal1 = str(sp_1f.get("palette_id") or "warm_stardew")
    norm_role = venue_semantics.normalize_venue_role(role, name=building_name)
    resolved_theme = interior_designer.infer_theme_key(building_name, pal1)
    floors_ordered = sorted({int(fi) for fi in (floor_plan or [1])})
    if 1 not in floors_ordered:
        floors_ordered = sorted([1, *floors_ordered])

    with engine._lock:
        sp_1f_live = engine._world["spaces"][space_1f_id]
        sp_1f_live["floor_index"] = 1
        sp_1f_live["floor_label"] = "1F"
        sp_1f_live["building_group_id"] = space_1f_id

    doors_1f = [(int(dx), int(dy)) for dx, dy in door_coords_1f]
    avoid_1f_xs: set[int] = set()
    for dx, _ in doors_1f:
        avoid_1f_xs.update((dx - 1, dx, dx + 1))

    act_layout_1f = interior_designer.activity_layout(norm_role, building_name, max(0, int(bed_count)))
    bot1 = h1 - 2 if h1 >= 5 else h1 - 1
    stair_1f_y = bot1 if act_layout_1f else max(2, bot1 - 1)
    up_anchor_1f: tuple[int, int] | None = None
    down_anchor_1f: tuple[int, int] | None = None

    if any(fi >= 2 for fi in floors_ordered):
        sx_up = _pick_stair_x(w1, avoid_1f_xs)
        up_anchor_1f = (sx_up, stair_1f_y)
        avoid_1f_xs.update((sx_up - 1, sx_up, sx_up + 1))
    if any(fi < 0 for fi in floors_ordered):
        sx_dn = _pick_stair_x(w1, avoid_1f_xs)
        down_anchor_1f = (sx_dn, stair_1f_y)

    # Bed distribution: keep twin_room beds on 1F; move standard multi-floor resident beds onto 2F.
    beds_1f = max(0, int(bed_count))
    beds_2f = 0
    if 2 in floors_ordered and beds_1f > 0 and act_layout_1f != "twin_room":
        if beds_1f >= 4 and norm_role == "home":
            beds_1f = 1
            beds_2f = int(bed_count) - 1
        else:
            beds_2f = beds_1f
            beds_1f = 0

    floor_dims: dict[int, tuple[int, int]] = {1: (w1, h1)}
    floor_sids: dict[int, str] = {1: space_1f_id}
    floor_spaces: dict[int, dict[str, Any]] = {1: sp_1f}
    floor_stairs: dict[int, list[tuple[int, int]]] = {1: [p for p in (up_anchor_1f, down_anchor_1f) if p is not None]}
    stair_pairs: list[dict[str, Any]] = []

    batch_cm = engine.batch_mutations() if hasattr(engine, "batch_mutations") else nullcontext()
    with batch_cm:
        # 1. Create secondary floor spaces & compute aligned stair coordinates
        for fi in floors_ordered:
            if fi == 1:
                continue
            flbl = floor_label(fi)
            if fi < 0:
                fw = clamp_int(w1 - 2, 12, 18, 12)
                fh = clamp_int(h1 - 1, 10, 14, 10)
                f_wall = "stone_brick"
                f_floor = "stone_tile"
                f_parent = space_1f_id
                f_ambient = f"「{building_name}」地下一层，砖石清凉安静。"
            elif fi == 2:
                fw = clamp_int(w1, 12, 20, w1)
                fh = clamp_int(h1, 10, 15, h1)
                f_wall = wall_tile
                f_floor = floor_tile
                f_parent = space_1f_id
                f_ambient = f"「{building_name}」二层房间，安静私密，凭窗可望向镇中。"
            else:
                fw = clamp_int(w1 - 2, 12, 18, 12)
                fh = clamp_int(h1 - 1, 10, 14, 10)
                f_wall = wall_tile
                f_floor = floor_tile
                f_parent = floor_sids.get(2, space_1f_id)
                f_ambient = f"「{building_name}」顶层阁楼，木梁斜顶下安静开阔。"

            cand_sid = f"{space_1f_id}__{flbl.lower()}"
            with engine._lock:
                suffix = 2
                while cand_sid in engine._world["spaces"]:
                    cand_sid = f"{space_1f_id}__{flbl.lower()}_{suffix}"
                    suffix += 1
            f_sp = engine.create_space(
                name=clip_text(f"{building_name} {flbl}", 16, f"{building_name}{flbl}"),
                width=fw,
                height=fh,
                wall_rows=1,
                space_kind="building",
                parent_space_id=f_parent,
                district=str(sp_1f.get("district") or ""),
                ambient_phrase=clip_text(f_ambient, 60),
                palette_id=pal1,
                floor_index=fi,
                floor_label=flbl,
                building_group_id=space_1f_id,
                fixed_space_id=cand_sid,
                switch_active=False,
            )
            floor_dims[fi] = (fw, fh)
            floor_sids[fi] = cand_sid
            floor_spaces[fi] = f_sp
            floor_stairs[fi] = []

        # Assign vertical stair pairs: B1 <-> 1F, 1F <-> 2F, 2F <-> 3F
        if -1 in floor_sids and down_anchor_1f is not None:
            wb, hb = floor_dims[-1]
            bot_b = hb - 2 if hb >= 5 else hb - 1
            bx = clamp_int(down_anchor_1f[0], 1, wb - 2, min(down_anchor_1f[0], wb - 2))
            by = max(2, bot_b - 1)
            b_pt = (bx, by)
            floor_stairs[-1].append(b_pt)
            stair_pairs.append({
                "low_fi": -1, "low_sid": floor_sids[-1], "low_xy": b_pt, "low_motif": "staircase_up",
                "low_name": "上行楼梯", "low_brief": f"顺着阶梯走回「{building_name} 1F」。",
                "high_fi": 1, "high_sid": space_1f_id, "high_xy": down_anchor_1f, "high_motif": "basement_hatch",
                "high_name": "地下室活板门", "high_brief": f"掀起木盖梯，顺着台阶下到「{building_name} B1」。",
            })

        if 2 in floor_sids and up_anchor_1f is not None:
            w2, h2 = floor_dims[2]
            bot_2 = h2 - 2 if h2 >= 5 else h2 - 1
            ux2 = clamp_int(up_anchor_1f[0], 1, w2 - 2, min(up_anchor_1f[0], w2 - 2))
            uy2 = max(2, bot_2 - 1)
            p2_dn = (ux2, uy2)
            floor_stairs[2].append(p2_dn)
            stair_pairs.append({
                "low_fi": 1, "low_sid": space_1f_id, "low_xy": up_anchor_1f, "low_motif": "staircase_up",
                "low_name": "上行2F楼梯", "low_brief": f"沿木梯踏步前往「{building_name} 2F」。",
                "high_fi": 2, "high_sid": floor_sids[2], "high_xy": p2_dn, "high_motif": "staircase_down",
                "high_name": "下行1F楼梯", "high_brief": f"顺着楼梯回到「{building_name} 1F」。",
            })

        if 3 in floor_sids and 2 in floor_sids:
            w2, h2 = floor_dims[2]
            w3, h3 = floor_dims[3]
            bot_2 = h2 - 2 if h2 >= 5 else h2 - 1
            bot_3 = h3 - 2 if h3 >= 5 else h3 - 1
            avoid_2 = {pt[0] for pt in floor_stairs[2]} | {pt[0] - 1 for pt in floor_stairs[2]} | {pt[0] + 1 for pt in floor_stairs[2]}
            x2_up = _pick_stair_x(w2, avoid_2)
            p2_up = (x2_up, max(2, bot_2 - 1))
            floor_stairs[2].append(p2_up)
            x3_dn = clamp_int(x2_up, 1, w3 - 2, min(x2_up, w3 - 2))
            p3_dn = (x3_dn, max(2, bot_3 - 1))
            floor_stairs[3].append(p3_dn)
            stair_pairs.append({
                "low_fi": 2, "low_sid": floor_sids[2], "low_xy": p2_up, "low_motif": "staircase_up",
                "low_name": "上行3F阁楼梯", "low_brief": f"推阶而上前往「{building_name} 3F」。",
                "high_fi": 3, "high_sid": floor_sids[3], "high_xy": p3_dn, "high_motif": "staircase_down",
                "high_name": "下行2F楼梯", "high_brief": f"顺着阁楼木梯回到「{building_name} 2F」。",
            })

        # 2. Populate every floor BEFORE placing stair portal entities so reserved stair cells stay clear.
        floor_results: dict[int, dict[str, Any]] = {}
        for idx_f, fi in enumerate(floors_ordered):
            sid_f = floor_sids[fi]
            f_role = "home" if (fi == 2 and beds_2f > 0) else ("public" if fi != 1 else norm_role)
            f_floor = "stone_tile" if fi < 0 else floor_tile
            f_wall = "stone_brick" if fi < 0 else wall_tile
            f_beds = beds_1f if fi == 1 else (beds_2f if fi == 2 else 0)
            f_seed = (int(seed) + idx_f * 131) if seed else zlib.crc32(f"{sid_f}|{fi}".encode("utf-8"))
            if fi == 1 and act_layout_1f:
                res_f = _populate_activity_1f_floor(
                    engine,
                    sid_f,
                    role=f_role,
                    building_name=building_name,
                    floor_tile=f_floor,
                    rug_tile=rug_tile,
                    wall_tile=f_wall,
                    bed_count=f_beds,
                    note_text=note_text,
                    door_coords=doors_1f,
                    stair_coords=floor_stairs.get(1, ()),
                    world_prompt=world_prompt,
                    scene_items=scene_items,
                    brief=brief,
                    detail=detail,
                    seed=f_seed,
                    used_landmarks=(used_landmarks if fi == 1 else set(interior_designer.HERO_LANDMARK_MOTIFS)),
                )
            else:
                res_f = populate_multiroom_floor(
                    engine,
                    sid_f,
                    role=f_role,
                    building_name=building_name if fi == 1 else f"{building_name} {floor_label(fi)}",
                    floor_index=fi,
                    floor_tile=f_floor,
                    rug_tile=rug_tile,
                    wall_tile=f_wall,
                    bed_count=f_beds,
                    note_text=note_text if fi == 1 else "",
                    door_coords=doors_1f if fi == 1 else (),
                    stair_coords=floor_stairs.get(fi, ()),
                    build_perimeter_bottom_wall=True,
                    world_prompt=world_prompt,
                    scene_items=scene_items if fi == 1 else (),
                    brief=brief,
                    detail=detail,
                    seed=f_seed,
                    used_landmarks=(used_landmarks if fi == 1 else set(interior_designer.HERO_LANDMARK_MOTIFS)),
                )
            floor_results[fi] = res_f

        # 3. Place bidirectional stair portals connecting consecutive floors
        for sp_pair in stair_pairs:
            low_fi = int(sp_pair["low_fi"])
            high_fi = int(sp_pair["high_fi"])
            _place_stair_portal(
                engine,
                src_sid=str(sp_pair["low_sid"]),
                src_xy=sp_pair["low_xy"],
                dst_sid=str(sp_pair["high_sid"]),
                dst_xy=sp_pair["high_xy"],
                motif=str(sp_pair["low_motif"]),
                name=str(sp_pair["low_name"]),
                brief=str(sp_pair["low_brief"]),
                palette_id=pal1,
                theme_key=resolved_theme,
            )
            _place_stair_portal(
                engine,
                src_sid=str(sp_pair["high_sid"]),
                src_xy=sp_pair["high_xy"],
                dst_sid=str(sp_pair["low_sid"]),
                dst_xy=sp_pair["low_xy"],
                motif=str(sp_pair["high_motif"]),
                name=str(sp_pair["high_name"]),
                brief=str(sp_pair["high_brief"]),
                palette_id=pal1,
                theme_key=resolved_theme,
            )

            # Register secondary floor in its parent floor's `buildings` (role avoids cafe/shop so restocking stays on 1F).
            child_fi = -1 if low_fi < 0 else high_fi
            parent_fi = 1 if child_fi in (-1, 2) else 2
            parent_sid = floor_sids[parent_fi]
            parent_anchor = sp_pair["high_xy"] if child_fi < 0 else sp_pair["low_xy"]
            child_sid = floor_sids[child_fi]
            child_beds = beds_2f if child_fi == 2 else 0
            child_role = "home" if child_beds > 0 else ("library" if norm_role == "library" else "public")
            child_meta = {
                "building_id": f"fl_{child_fi}_{child_sid[-10:]}"[:32],
                "name": clip_text(f"{building_name} {floor_label(child_fi)}", 20, floor_label(child_fi)),
                "x": int(parent_anchor[0]),
                "y": int(parent_anchor[1]),
                "w": 1,
                "h": 1,
                "door_x": int(parent_anchor[0]),
                "door_y": int(parent_anchor[1]),
                "roof_color": str(building_meta.get("roof_color") or "#8a7060"),
                "interior_space_id": child_sid,
                "category": venue_semantics.default_category_for_role(child_role),
                "role": child_role,
                "restock_profile": "none",
                "affordances": venue_semantics.building_affordance_hints({"role": child_role, "name": floor_label(child_fi)}),
                "icon": "🪜",
                "location_id": "",
                "bed_count": child_beds,
                "parent_space_id": parent_sid,
            }
            with engine._lock:
                host_sp = engine._world["spaces"][parent_sid]
                existing_b = list(host_sp.get("buildings") or [])
                existing_b.append(child_meta)
                host_sp["buildings"] = normalize_buildings(existing_b, int(host_sp["width"]), int(host_sp["height"]))

        with engine._lock:
            engine._save_world_locked()

    return {
        "floor_space_ids": [floor_sids[fi] for fi in floors_ordered],
        "floor_map": dict(floor_sids),
        "floors": [
            {
                "floor_index": fi,
                "floor_label": floor_label(fi),
                "space_id": floor_sids[fi],
                "beds_placed": int(floor_results[fi]["beds_placed"]),
            }
            for fi in floors_ordered
        ],
        "beds_1f": int(floor_results[1]["beds_placed"]),
        "total_beds": sum(int(r["beds_placed"]) for r in floor_results.values()),
    }


__all__ = [
    "build_multifloor_building_interiors",
    "choose_building_floor_plan",
    "floor_label",
    "plan_floor_rooms",
    "populate_multiroom_floor",
]
