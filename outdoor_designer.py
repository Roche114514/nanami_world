"""2.5D 城镇地形高差生成、院落/街块切分（Parcel Subdivision）与户外立体景观库。

职责：
1. 生成 0 / 1 / 2 三层台地（`elevations`）与石阶坡道（`stairs`），消除整张地图纯平一团感，
   并通过连通分量修复保证所有高度层与道路 100% 由阶梯连通；
2. 对建筑周边进行院落切分（木栅栏 / 矮石墙 / 绿篱围合 + 庭院材质铺装 + 留门开口），
   对空余街块切分为口袋公园、树丛、市集角与高台水井观景台；
3. 内置 14 种 16×16 / 32×16 / 32×32 户外像素景观母版（树木、围栏、长椅、水井、喷泉、
   自动贩卖机、石灯笼、霓虹灯柱、路牌、花坛、市集摊位），按道路与桥头自适应分布路灯。
"""

from __future__ import annotations

import copy
import math
import random
from collections import deque
from typing import Any, Mapping, Sequence

from .schema import (
    DEFAULT_SLOT_COLORS,
    TILE_CATALOG,
    clamp_int,
    clip_text,
    normalize_pixel_grid,
)


def _mirror32(halves: Sequence[str]) -> tuple[str, ...]:
    rows: list[str] = []
    for raw in halves:
        left = (str(raw) + "." * 16)[:16]
        rows.append(left + left[::-1])
    return tuple(rows)


def _fit_rows(rows: Sequence[str], w: int, h: int) -> tuple[str, ...]:
    out: list[str] = []
    for i in range(h):
        line = str(rows[i]) if i < len(rows) else ""
        if len(line) < w:
            line = line + "." * (w - len(line))
        elif len(line) > w:
            line = line[:w]
        out.append(line)
    return tuple(out)


# ---------------------------------------------------------------------------
# 14 种户外立体像素景观母版
# ---------------------------------------------------------------------------

OUTDOOR_PROP_MOTIFS: dict[str, dict[str, Any]] = {
    "sakura_tree": {
        "footprint": [2, 2],
        "default_name": "樱花树",
        "default_brief": "枝冠如云、随风飘落粉白花瓣的老樱树。",
        "solid": True,
        "placement": "floor",
        "traits": {},
        "tags": ["outdoor", "tree", "flora"],
        "accent_colors": {"a": "#ff9ebb", "b": "#ffdce8", "1": "#7d5136", "3": "#4e311f"},
        "rows": _mirror32([
            ".......ooooooooo",
            ".....ooaaaaaaaaa",
            "....oaaabbbbaaaa",
            "...oaabbbbbbaaaa",
            "..oaabbbbaaaaaaa",
            "..oaaaaaaaabbbba",
            ".oaaabbbbaabbbba",
            ".oaabbbbbaaaaaaa",
            "oaabbbbaaaaaaaao",
            "oaaaaaaaaabbbbaa",
            "oaaaabbbbabbbbaa",
            "oaaaabbbbaaaaaaa",
            ".oaaaaaaaaaaaaao",
            ".ooaaaaaoooooaaa",
            "...ooooo.o311111",
            ".........o331111",
            "..........o33111",
            "..........o33111",
            "...........o3111",
            "...........o3111",
            "...........o3111",
            "...........o3111",
            "..........o33111",
            "..........o31111",
            ".........o331111",
            "........oo311111",
            ".......o33111133",
            ".......ooooooooo",
            "................",
            "................",
            "................",
            "................",
        ]),
    },
    "pine_tree": {
        "footprint": [2, 2],
        "default_name": "苍松",
        "default_brief": "层叠松针挺拔苍翠、带着山林清香的常青松树。",
        "solid": True,
        "placement": "floor",
        "traits": {},
        "tags": ["outdoor", "tree", "flora"],
        "accent_colors": {"a": "#3f7d58", "b": "#84bf96", "1": "#6e462c", "3": "#432918"},
        "rows": _mirror32([
            "..............oo",
            ".............oaa",
            "............oaba",
            "...........oabba",
            "..........oaaaaa",
            ".........ooooaaa",
            "...........ooaba",
            "..........oabbaa",
            ".........oaabbaa",
            "........oaaaaaaa",
            ".......ooooaaaaa",
            ".........ooabbaa",
            "........oaabbaaa",
            ".......oaabbaaaa",
            "......oaaaaaaaaa",
            ".....ooooaaaaaaa",
            ".......ooabbaaaa",
            "......oaabbaaaaa",
            ".....oaaaaaaaaaa",
            "....oooooooooooo",
            "............o311",
            "............o311",
            "............o311",
            "............o311",
            "...........o3311",
            "..........oo3111",
            "..........oooooo",
            "................",
            "................",
            "................",
            "................",
            "................",
        ]),
    },
    "shade_tree": {
        "footprint": [2, 2],
        "default_name": "阔叶行道树",
        "default_brief": "树冠浓密圆润、在街角投下一片清凉绿荫的阔叶树。",
        "solid": True,
        "placement": "floor",
        "traits": {},
        "tags": ["outdoor", "tree", "flora"],
        "accent_colors": {"a": "#569e52", "b": "#99d678", "1": "#784e33", "3": "#4a2e1c"},
        "rows": _mirror32([
            "......oooooooooo",
            "....ooaaaaaaaaaa",
            "...oaaabbbbbaaaa",
            "..oaabbbbbbbaaaa",
            ".oaabbbbbaaaaaaa",
            ".oaaaaaaaaabbbba",
            "oaaaabbbbaabbbba",
            "oaaaabbbbaaaaaaa",
            "oaaaaaaaaaaaaaaa",
            "oaaabbbbaaaaaaaa",
            ".oaabbbbaaaaaaaa",
            ".oaaaaaaaaaaaaao",
            "..ooaaaaaaaaaooo",
            "....ooooooooo111",
            "..........o33111",
            "..........o33111",
            "...........o3111",
            "...........o3111",
            "...........o3111",
            "...........o3111",
            "..........o33111",
            ".........o331111",
            "........oo311133",
            "........oooooooo",
            "................",
            "................",
            "................",
            "................",
            "................",
            "................",
            "................",
            "................",
        ]),
    },
    "wood_fence": {
        "footprint": [1, 1],
        "default_name": "木栅栏",
        "default_brief": "围出安静小院边界的原木矮栅栏。",
        "solid": True,
        "placement": "floor",
        "traits": {},
        "tags": ["outdoor", "fence"],
        "accent_colors": {"1": "#a87449", "2": "#cfa074", "3": "#6c4628"},
        "rows": _fit_rows([
            "................",
            "................",
            "..oo........oo..",
            ".o22o......o22o.",
            ".o11o......o11o.",
            "oo11oooooooo11oo",
            "o22222222222222o",
            "o31111111111113o",
            "oo11oooooooo11oo",
            ".o11o......o11o.",
            "oo11oooooooo11oo",
            "o22222222222222o",
            "o31111111111113o",
            "oo33oooooooo33oo",
            ".oooo......oooo.",
            "................",
        ], 16, 16),
    },
    "stone_wall_low": {
        "footprint": [1, 1],
        "default_name": "矮石垣",
        "default_brief": "由青灰方石严丝合缝垒起的院落矮墙。",
        "solid": True,
        "placement": "floor",
        "traits": {},
        "tags": ["outdoor", "fence", "stone"],
        "accent_colors": {"1": "#86909c", "2": "#b8c2ce", "3": "#525a66"},
        "rows": _fit_rows([
            "................",
            "................",
            "................",
            "................",
            "oooooooooooooooo",
            "o22222222222222o",
            "oooooooooooooooo",
            "o11113o111113o1o",
            "o11113o111113o1o",
            "oooooooooooooooo",
            "o113o11113o1113o",
            "o113o11113o1113o",
            "o333o33333o3333o",
            "oooooooooooooooo",
            "................",
            "................",
        ], 16, 16),
    },
    "park_bench": {
        "footprint": [2, 1],
        "default_name": "街角长椅",
        "default_brief": "铸铁扶手配宽木条靠背的户外休息长椅。",
        "solid": False,
        "placement": "floor",
        "traits": {
            "restable": {
                "rest_mode": "sit",
                "energy_regen_per_min": 0.008,
                "comfort": 0.65,
                "occupied_by": None,
            }
        },
        "tags": ["outdoor", "bench", "rest"],
        "accent_colors": {"1": "#a87045", "2": "#d49d6a", "3": "#3d434d"},
        "rows": _mirror32([
            "................",
            "................",
            "..oooooooooooooo",
            "..o2222222222222",
            "..o1111111111111",
            "..oooooooooooooo",
            "..o2222222222222",
            ".oo33ooooooooooo",
            ".o33o11111111111",
            ".o33o22222222222",
            ".o33oooooooooooo",
            "..o33o........o3",
            "..o33o........o3",
            "..oooo........oo",
            "................",
            "................",
        ]),
    },
    "stone_well": {
        "footprint": [2, 2],
        "default_name": "古石井",
        "default_brief": "支着木顶辘轳、井壁覆满青苔的清冽石井。",
        "solid": True,
        "placement": "floor",
        "traits": {
            "activity": {
                "domain": "explore",
                "progress_unit": "次",
                "progress": 0,
                "battery": None,
                "battery_cost": 0,
            }
        },
        "tags": ["outdoor", "well", "water"],
        "accent_colors": {"1": "#8a929a", "2": "#c4ccd4", "3": "#545b63", "a": "#9c6840", "b": "#5eb5e0"},
        "rows": _mirror32([
            "......oooooooooo",
            "....ooaaaaaaaaaa",
            "...oaa22aaaaaaaa",
            "..oaaaaaaaaaaaaa",
            ".ooooooooooooooo",
            "...ooaaao.......",
            "....oaaao.....oo",
            "....oaaao....oaa",
            "....oaaaoooooaaa",
            "....oaaao......o",
            "....oaaao......o",
            "....oaaao.....oa",
            "...ooooooooooooo",
            "..o2222222222222",
            "..o111bbbbbbbbb1",
            "..o11bbbbbbbbbb1",
            "..o2222222222222",
            "..oooooooooooooo",
            "..o1113o11113o11",
            "..o1113o11113o11",
            "..oooooooooooooo",
            "..o113o11113o113",
            "..o333o33333o333",
            "..oooooooooooooo",
            "................",
            "................",
            "................",
            "................",
            "................",
            "................",
            "................",
            "................",
        ]),
    },
    "fountain_basin": {
        "footprint": [2, 2],
        "default_name": "石雕喷泉池",
        "default_brief": "水珠层层跌落石盘、漾起清亮水声的小型喷泉池。",
        "solid": True,
        "placement": "floor",
        "traits": {},
        "tags": ["outdoor", "fountain", "water"],
        "accent_colors": {"1": "#929ca8", "2": "#dce6f2", "3": "#586270", "a": "#b5f0ff", "b": "#58b4e8"},
        "rows": _mirror32([
            "................",
            ".............aa.",
            "............abba",
            "...........ab..b",
            "............oooo",
            "...........o2222",
            "..........oo11bb",
            "..........o33333",
            "............o111",
            "...........ab111",
            "..........abb111",
            ".....ooooooooooo",
            "...oo22222222222",
            "..o22bbbbbbbbbbb",
            ".o22bbbabbbbbabb",
            ".o11bbbbbbbbbbbb",
            ".o11bbbabbbbbbbb",
            ".o22bbbbbbbbbbbb",
            "..o2222222222222",
            "..oooooooooooooo",
            "...o1113o1113o11",
            "...o3333o3333o33",
            "...ooooooooooooo",
            "................",
            "................",
            "................",
            "................",
            "................",
            "................",
            "................",
            "................",
            "................",
        ]),
    },
    "vending_machine": {
        "footprint": [1, 1],
        "default_name": "自动贩卖机",
        "default_brief": "街角嗡嗡运转、橱窗里排列着冷热罐装饮品的自动贩卖机。",
        "solid": True,
        "placement": "floor",
        "traits": {
            "switchable": {
                "provides_light": True,
                "is_on": True,
                "on_brief": "灯箱明亮、陈列着冷热饮品的自动贩卖机",
                "off_brief": "省电熄屏的街角自动贩卖机",
            }
        },
        "tags": ["outdoor", "vending", "light"],
        "accent_colors": {"1": "#d94f5c", "2": "#f5eef0", "3": "#8a2934", "a": "#80e5ff", "b": "#ffd166"},
        "rows": _fit_rows([
            "..oooooooooooo..",
            ".o222222222222o.",
            ".o11oooooooo11o.",
            ".o11oaaaaabo11o.",
            ".o11oabbbaao11o.",
            ".o11oaaaaabo11o.",
            ".o11oooooooo11o.",
            ".o111111111bb1o.",
            ".o111111111bb1o.",
            ".o11oooooooo11o.",
            ".o11o333333o11o.",
            ".o11oooooooo11o.",
            ".o333333333333o.",
            ".oo33oooooo33oo.",
            "..oooo....oooo..",
            "................",
        ], 16, 16),
    },
    "stone_lantern": {
        "footprint": [1, 1],
        "default_name": "石灯笼",
        "default_brief": "火袋窗口透出柔和暖黄微光的庭院石灯笼。",
        "solid": True,
        "placement": "floor",
        "traits": {
            "switchable": {
                "provides_light": True,
                "is_on": True,
                "on_brief": "火袋透出柔和暖黄光晕的石灯笼",
                "off_brief": "灯芯未燃的青灰石灯笼",
            }
        },
        "tags": ["outdoor", "light", "lantern"],
        "accent_colors": {"1": "#8d9499", "2": "#c5ccd1", "3": "#555b61", "a": "#ffcf6e", "b": "#fff2b2"},
        "rows": _fit_rows([
            ".......oo.......",
            "......o22o......",
            "....oo2112oo....",
            "..oo22111122oo..",
            "..oooooooooooo..",
            "....ooaaaaao....",
            "....oabbbbao....",
            "....ooaaaaao....",
            "...oooooooooo...",
            ".....o1111o.....",
            ".....o1111o.....",
            "....o211112o....",
            "...o22333322o...",
            "...oooooooooo...",
            "................",
            "................",
        ], 16, 16),
    },
    "neon_bollard": {
        "footprint": [1, 1],
        "default_name": "霓虹街灯柱",
        "default_brief": "嵌有冰蓝与品红导光灯带的现代街角灯柱。",
        "solid": True,
        "placement": "floor",
        "traits": {
            "switchable": {
                "provides_light": True,
                "is_on": True,
                "on_brief": "折射着清冷霓虹光晕的街角灯柱",
                "off_brief": "灯带熄灭的深灰街角灯柱",
            }
        },
        "tags": ["outdoor", "light", "neon"],
        "accent_colors": {"1": "#3b4254", "2": "#78849e", "3": "#222633", "a": "#5ce1e6", "b": "#ff66c4"},
        "rows": _fit_rows([
            ".....oooooo.....",
            "....o22aa22o....",
            "....o1aaaa1o....",
            "....o1abba1o....",
            ".....oabba1o....",
            ".....o1aa1o.....",
            ".....o1bb1o.....",
            ".....o1aa1o.....",
            ".....o1111o.....",
            ".....o1aa1o.....",
            ".....o1111o.....",
            "....o211112o....",
            "...o33333333o...",
            "...oooooooooo...",
            "................",
            "................",
        ], 16, 16),
    },
    "wooden_signpost": {
        "footprint": [1, 1],
        "default_name": "木制指路牌",
        "default_brief": "钉着两块方向木箭头的街口指路木牌。",
        "solid": True,
        "placement": "floor",
        "traits": {"inscribable": {"text": "", "max_chars": 80}},
        "tags": ["outdoor", "sign"],
        "accent_colors": {"1": "#9e6b43", "2": "#d6a87c", "3": "#633f24"},
        "rows": _fit_rows([
            "......oooo......",
            "..ooooooooooo...",
            ".o22222222222oo.",
            "o22111111111222o",
            ".oooooooooooooo.",
            "......o11o......",
            "...ooooooooooo..",
            "..o22222222222o.",
            "..o31111111113o.",
            "...ooooooooooo..",
            "......o11o......",
            "......o31o......",
            ".....oo31oo.....",
            "....o333333o....",
            "....oooooooo....",
            "................",
        ], 16, 16),
    },
    "flower_planter": {
        "footprint": [2, 1],
        "default_name": "街头花箱",
        "default_brief": "木槽中盛开着缤纷时令草花的街景花箱。",
        "solid": True,
        "placement": "floor",
        "traits": {},
        "tags": ["outdoor", "flora", "planter"],
        "accent_colors": {"1": "#99653e", "2": "#c99569", "3": "#548c46", "a": "#ff8fab", "b": "#ffd166"},
        "rows": _mirror32([
            "................",
            "....oo....oo....",
            "...oaao..obbo..o",
            "..oaabaoobbbaoob",
            "..o3333333333333",
            ".oo33a333b333a33",
            ".o33333333333333",
            ".ooooooooooooooo",
            ".o22222222222222",
            ".o1111o11111o111",
            ".o1111o11111o111",
            ".o1111o11111o111",
            "..oooooooooooooo",
            "................",
            "................",
            "................",
        ]),
    },
    "market_stall": {
        "footprint": [2, 2],
        "default_name": "遮阳露天摊位",
        "default_brief": "撑着红白条纹遮阳布蓬、台面干净敞亮的街边小摊。",
        "solid": True,
        "placement": "floor",
        "traits": {"surface": {"capacity": 3}},
        "tags": ["outdoor", "stall", "surface"],
        "accent_colors": {"1": "#a66d43", "2": "#f5ede2", "3": "#694124", "a": "#d95b5b", "b": "#f2b84b"},
        "rows": _mirror32([
            "...ooooooooooooo",
            "..oaa222aa222aa2",
            ".oaaa222aa222aa2",
            ".oaaa222aa222aa2",
            "oaaaa222aa222aa2",
            "oaaaa222aa222aa2",
            "oooooooooooooooo",
            ".ooaaaooo222ooaa",
            "..oooo..ooooo..o",
            "..o31o.........o",
            "..o31o.........o",
            "..o31o....bb...o",
            "..o31o...bbbb..o",
            ".ooooooooooooooo",
            ".o22222222222222",
            ".o11111111111111",
            ".ooooooooooooooo",
            "..o1133333333333",
            "..o1131111111111",
            "..o1131111111111",
            "..o3333333333333",
            "..oooooooooooooo",
            "................",
            "................",
            "................",
            "................",
            "................",
            "................",
            "................",
            "................",
            "................",
            "................",
        ]),
    },
}


_OUTDOOR_THEME_PALETTES: dict[str, dict[str, str]] = {
    "sakura": {"o": "#24191e", "1": "#9e6b43", "2": "#e8cbb1", "3": "#5e3c24", "a": "#ff9ebb", "b": "#ffe3ec"},
    "snow_onsen": {"o": "#1e2229", "1": "#7e624c", "2": "#e6eef5", "3": "#47372b", "a": "#528265", "b": "#dcebf7"},
    "neon_market": {"o": "#171824", "1": "#4c546b", "2": "#b8c4de", "3": "#292e3d", "a": "#5ce1e6", "b": "#ff66c4"},
    "seaside": {"o": "#1a242b", "1": "#9c744f", "2": "#eef4f7", "3": "#5c422c", "a": "#5db7de", "b": "#ffd166"},
    "forest_academy": {"o": "#1d241b", "1": "#85593a", "2": "#dce6cc", "3": "#4d321f", "a": "#569e52", "b": "#a3db7d"},
}


def synthesize_outdoor_blueprint(
    motif_id: str,
    *,
    theme: str = "sakura",
    name: str = "",
    brief: str = "",
) -> dict[str, Any]:
    tpl = OUTDOOR_PROP_MOTIFS.get(motif_id) or OUTDOOR_PROP_MOTIFS["stone_lantern"]
    fw, fh = int(tpl["footprint"][0]), int(tpl["footprint"][1])
    px_w, px_h = fw * 16, fh * 16
    rows = normalize_pixel_grid(tpl["rows"], px_w, px_h, align_bottom=True)

    colors = dict(DEFAULT_SLOT_COLORS)
    colors.update(_OUTDOOR_THEME_PALETTES.get(theme, _OUTDOOR_THEME_PALETTES["sakura"]))
    for k, v in (tpl.get("accent_colors") or {}).items():
        # 樱花树 / 松树 / 阔叶树保留物种色彩，但在雪乡等主题下做细腻适配
        if motif_id == "pine_tree" and theme == "snow_onsen" and k == "b":
            colors[k] = "#eef7ff"
        else:
            colors[k] = v

    resolved_name = clip_text(name, 20, str(tpl["default_name"]))
    resolved_brief = clip_text(brief, 96, str(tpl["default_brief"]))
    return {
        "name": resolved_name,
        "brief": resolved_brief,
        "detail": resolved_brief,
        "tags": list(tpl.get("tags") or ["outdoor"]),
        "placement": str(tpl.get("placement") or "floor"),
        "solid": bool(tpl.get("solid", True)),
        "footprint": [fw, fh],
        "traits": copy.deepcopy(tpl.get("traits") or {}),
        "effects": {},
        "sprite": {
            "mode": "pixel_grid",
            "w": px_w,
            "h": px_h,
            "frames": {"default": rows},
            "slot_colors": colors,
        },
    }


# ---------------------------------------------------------------------------
# 2.5D 台地地形与阶梯生成器（0 / 1 / 2 三层海拔 + 100% 阶梯连通保证）
# ---------------------------------------------------------------------------

def _can_step_grid(
    elev: list[list[int]],
    stairs: dict[str, str],
    x0: int,
    y0: int,
    x1: int,
    y1: int,
) -> bool:
    e0 = elev[y0][x0]
    e1 = elev[y1][x1]
    if e0 == e1:
        return True
    if abs(e0 - e1) > 1:
        return False
    need = "ns" if x0 == x1 else "ew"
    s0 = stairs.get(f"{x0},{y0}")
    s1 = stairs.get(f"{x1},{y1}")
    return s0 in (need, "both") or s1 in (need, "both")


def generate_town_elevation(
    tw: int,
    th: int,
    *,
    layout_style: str,
    theme: str,
    tile_grid: list[list[str]],
    road_tiles: set[str],
    rng: random.Random,
) -> tuple[dict[str, int], dict[str, str]]:
    """生成三层自然台地 `elevations` 与石阶 `stairs`，并自动铺设阶梯石板。"""
    elev: list[list[int]] = [[0 for _ in range(tw)] for _ in range(th)]

    style = str(layout_style or "avenue").strip().lower()
    variant = rng.choice(("north_ridge", "east_west", "corner_acropolis"))
    phase = rng.uniform(0.0, math.tau)
    amp = rng.uniform(1.0, 2.2)

    # 水体与近岸保持海拔 0。外圈边界砖不参与台地计算。
    water_buffer: set[tuple[int, int]] = set()
    for y in range(1, th - 1):
        for x in range(1, tw - 1):
            if tile_grid[y][x] == "water":
                for dy in (-2, -1, 0, 1, 2):
                    for dx in (-2, -1, 0, 1, 2):
                        nx, ny = x + dx, y + dy
                        if 0 <= nx < tw and 0 <= ny < th and abs(dx) + abs(dy) <= 2:
                            water_buffer.add((nx, ny))

    # 按城镇骨架塑造大尺度台地（每阶至少 7~10 格纵深，便于建筑整块落位）
    if "coastal" in style:
        if th <= 28:
            # 紧凑海岸的台地沿横街分层，给街区留出连续的建房进深。
            promenade_y = th - (5 if th < 28 else 7) - 3
            y_high = max(3, promenade_y // 3 + 1) + rng.randint(0, 1)
            y_mid = promenade_y - 1
            amp = 0.0
        else:
            y_high = clamp_int(int(th * 0.27) + rng.randint(-1, 1), 7, max(8, th // 3), 8)
            y_mid = clamp_int(int(th * 0.56) + rng.randint(-2, 1), y_high + 7, max(y_high + 8, th - 11), 17)
        for y in range(1, th - 1):
            for x in range(1, tw - 1):
                wave = int(round(math.sin(x * 0.18 + phase) * amp))
                if y <= y_high + wave and 3 <= x <= tw - 4:
                    elev[y][x] = 2
                elif y <= y_mid + wave:
                    elev[y][x] = 1
    elif "river" in style:
        # 寻找河道中心列，河谷为 0，东西两岸抬升为 1，一侧高坡抬升为 2
        water_xs = [x for y in range(2, th - 2) for x in range(2, tw - 2) if tile_grid[y][x] == "water"]
        rx = int(sum(water_xs) / len(water_xs)) if water_xs else tw // 2
        high_west = rng.random() < 0.5
        y_bluff = clamp_int(int(th * 0.42) + rng.randint(-2, 2), 9, th - 10, 12)
        for y in range(1, th - 1):
            for x in range(1, tw - 1):
                dist_r = abs(x - rx)
                if dist_r >= 6:
                    elev[y][x] = 1
                    if high_west and x <= rx - 8 and y <= y_bluff:
                        elev[y][x] = 2
                    elif (not high_west) and x >= rx + 8 and y <= y_bluff:
                        elev[y][x] = 2
    elif "hill" in style or "winding" in style or variant == "north_ridge":
        north_up = rng.random() < 0.78
        y1 = clamp_int(int(th * 0.30) + rng.randint(-1, 2), 8, max(9, th // 2 - 3), 9)
        y2 = clamp_int(int(th * 0.62) + rng.randint(-2, 2), y1 + 7, max(y1 + 8, th - 8), 18)
        x_cut = rng.randint(max(6, tw // 5), max(8, tw // 3))
        flip_x = rng.random() < 0.5
        for y in range(1, th - 1):
            for x in range(1, tw - 1):
                yy = y if north_up else (th - 1 - y)
                xx = (tw - 1 - x) if flip_x else x
                step_shift = 2 if xx > tw // 2 else 0
                if yy <= y1 + step_shift and xx >= x_cut:
                    elev[y][x] = 2
                elif yy <= y2 + step_shift:
                    elev[y][x] = 1
    elif "ring" in style or "courtyard" in style:
        # 外环低街 0 -> 中轴台地 1 -> 北侧或东侧高台望楼 2
        x_split = clamp_int(int(tw * 0.36) + rng.randint(-2, 2), 9, tw - 14, 14)
        y_top = clamp_int(int(th * 0.36) + rng.randint(-1, 2), 8, th - 12, 11)
        east_high = rng.random() < 0.5
        for y in range(1, th - 1):
            for x in range(1, tw - 1):
                xx = x if east_high else (tw - 1 - x)
                if xx >= x_split:
                    elev[y][x] = 1
                    if xx >= x_split + 8 and y <= y_top:
                        elev[y][x] = 2
    else:
        # avenue / twin terraces
        y_mid = clamp_int(int(th * 0.54) + rng.randint(-2, 2), 11, th - 10, 15)
        y_high = clamp_int(y_mid - rng.randint(7, 9), 7, y_mid - 6, 8)
        side_right = rng.random() < 0.5
        for y in range(1, th - 1):
            for x in range(1, tw - 1):
                xx = x if side_right else (tw - 1 - x)
                jog = 2 if xx >= tw // 2 else -1
                if y <= y_high + jog and xx >= tw // 3:
                    elev[y][x] = 2
                elif y <= y_mid + jog:
                    elev[y][x] = 1

    # 水域及近岸强制归 0
    for wx, wy in water_buffer:
        elev[wy][wx] = 0

    # 确保三层之间不存在直接 2->0 断崖：所有 2 周围曼哈顿距离 <= 3 的 0 过渡为 1（若在水岸则将 2 降为 1）
    for _ in range(4):
        changed = False
        for y in range(1, th - 1):
            for x in range(1, tw - 1):
                if elev[y][x] != 2:
                    continue
                near_water_zero = False
                for dy in range(-3, 4):
                    for dx in range(-3, 4):
                        if abs(dx) + abs(dy) > 3:
                            continue
                        nx, ny = x + dx, y + dy
                        if not (1 <= nx < tw - 1 and 1 <= ny < th - 1):
                            continue
                        if elev[ny][nx] == 0:
                            if (nx, ny) in water_buffer:
                                near_water_zero = True
                            else:
                                elev[ny][nx] = 1
                                changed = True
                if near_water_zero:
                    elev[y][x] = 1
                    changed = True
        if not changed:
            break

    # 形态学平滑（消除 1 格锯齿孤岛，令挡土崖壁平直利落）
    for _ in range(3):
        snap = [row[:] for row in elev]
        for y in range(1, th - 1):
            for x in range(1, tw - 1):
                if (x, y) in water_buffer:
                    elev[y][x] = 0
                    continue
                cur = snap[y][x]
                nbrs = [snap[y + dy][x + dx] for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1))]
                if nbrs.count(cur) <= 1:
                    # 取众数
                    best = max((0, 1, 2), key=lambda lv: (nbrs.count(lv), -abs(lv - cur)))
                    elev[y][x] = best

    # 二次兜底：绝对禁止正交相邻 |Δelev| >= 2
    for _ in range(4):
        fixed = False
        for y in range(1, th - 1):
            for x in range(1, tw - 1):
                if elev[y][x] == 2:
                    for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                        nx, ny = x + dx, y + dy
                        if 1 <= nx < tw - 1 and 1 <= ny < th - 1 and elev[ny][nx] == 0:
                            elev[y][x] = 1
                            fixed = True
                            break
        if not fixed:
            break

    # 外圈围墙继承内侧相邻格高度，避免贴边产生无意义断崖线
    for x in range(1, tw - 1):
        elev[0][x] = elev[1][x]
        elev[th - 1][x] = elev[th - 2][x]
    for y in range(th):
        elev[y][0] = elev[y][1]
        elev[y][tw - 1] = elev[y][tw - 2]

    def _is_walkable_tid(tid: str) -> bool:
        info = TILE_CATALOG.get(tid) or TILE_CATALOG["grass"]
        return bool(info.get("walkable", True)) and not bool(info.get("is_wall", False))

    stairs: dict[str, str] = {}
    road_cells = {(x, y) for y, row in enumerate(tile_grid) for x, tile in enumerate(row) if tile in road_tiles}

    def _mark_stair_cell(sx: int, sy: int, orient: str) -> None:
        key = f"{sx},{sy}"
        prev = stairs.get(key)
        if prev and prev != orient:
            stairs[key] = "both"
        else:
            stairs[key] = orient
        if tile_grid[sy][sx] not in ("stone_tile", "wood_floor"):
            tile_grid[sy][sx] = "stone_tile"

    # 1) 道路自然跨越高度差处自动雕刻石阶
    for y in range(1, th - 1):
        for x in range(1, tw - 1):
            if not _is_walkable_tid(tile_grid[y][x]):
                continue
            for dx, dy, orient in ((0, 1, "ns"), (1, 0, "ew")):
                nx, ny = x + dx, y + dy
                if not (1 <= nx < tw - 1 and 1 <= ny < th - 1):
                    continue
                if not _is_walkable_tid(tile_grid[ny][nx]):
                    continue
                e0, e1 = elev[y][x], elev[ny][nx]
                if abs(e0 - e1) != 1:
                    continue
                if (x, y) in road_cells and (nx, ny) in road_cells:
                    hx, hy = (x, y) if e0 > e1 else (nx, ny)
                    lx, ly = (nx, ny) if e0 > e1 else (x, y)
                    _mark_stair_cell(hx, hy, orient)
                    if tile_grid[ly][lx] not in ("stone_tile", "wood_floor"):
                        tile_grid[ly][lx] = "stone_tile"

    # 2) 连通分量检测：若仍有被断崖隔离的台地，自动架设 2 格宽景观石阶直至全图 100% 连通
    def _compute_components() -> list[list[int]]:
        comp = [[-1 for _ in range(tw)] for _ in range(th)]
        cid = 0
        for sy in range(1, th - 1):
            for sx in range(1, tw - 1):
                if comp[sy][sx] != -1 or not _is_walkable_tid(tile_grid[sy][sx]):
                    continue
                q: deque[tuple[int, int]] = deque([(sx, sy)])
                comp[sy][sx] = cid
                while q:
                    cx, cy = q.popleft()
                    for dx, dy in ((0, 1), (0, -1), (1, 0), (-1, 0)):
                        nx, ny = cx + dx, cy + dy
                        if not (1 <= nx < tw - 1 and 1 <= ny < th - 1):
                            continue
                        if comp[ny][nx] != -1 or not _is_walkable_tid(tile_grid[ny][nx]):
                            continue
                        if not _can_step_grid(elev, stairs, cx, cy, nx, ny):
                            continue
                        comp[ny][nx] = cid
                        q.append((nx, ny))
                cid += 1
        return comp

    for _ in range(16):
        comp = _compute_components()
        active_cids = {comp[y][x] for y in range(1, th - 1) for x in range(1, tw - 1) if comp[y][x] >= 0}
        if len(active_cids) <= 1:
            break
        # 寻找连接不同连通分量的最佳平直崖边（优先 2 格并排宽阶梯）
        best_pair: tuple[int, tuple[tuple[int, int, int, int, str], ...]] | None = None
        for y in range(2, th - 2):
            for x in range(2, tw - 3):
                c_a, c_b = comp[y][x], comp[y + 1][x]
                if c_a >= 0 and c_b >= 0 and c_a != c_b and abs(elev[y][x] - elev[y + 1][x]) == 1:
                    if (
                        comp[y][x + 1] == c_a
                        and comp[y + 1][x + 1] == c_b
                        and elev[y][x + 1] == elev[y][x]
                        and elev[y + 1][x + 1] == elev[y + 1][x]
                    ):
                        score = 100 - abs(x - tw // 2)
                        cand = ((x, y, x, y + 1, "ns"), (x + 1, y, x + 1, y + 1, "ns"))
                        if best_pair is None or score > best_pair[0]:
                            best_pair = (score, cand)
                    elif best_pair is None:
                        best_pair = (20 - abs(x - tw // 2), ((x, y, x, y + 1, "ns"),))
        for y in range(2, th - 3):
            for x in range(2, tw - 2):
                c_a, c_b = comp[y][x], comp[y][x + 1]
                if c_a >= 0 and c_b >= 0 and c_a != c_b and abs(elev[y][x] - elev[y][x + 1]) == 1:
                    if (
                        comp[y + 1][x] == c_a
                        and comp[y + 1][x + 1] == c_b
                        and elev[y + 1][x] == elev[y][x]
                        and elev[y + 1][x + 1] == elev[y][x + 1]
                    ):
                        score = 95 - abs(y - th // 2)
                        cand = ((x, y, x + 1, y, "ew"), (x, y + 1, x + 1, y + 1, "ew"))
                        if best_pair is None or score > best_pair[0]:
                            best_pair = (score, cand)
                    elif best_pair is None:
                        best_pair = (15 - abs(y - th // 2), ((x, y, x + 1, y, "ew"),))
        if best_pair is None:
            break
        for x0, y0, x1, y1, orient in best_pair[1]:
            hx, hy = (x0, y0) if elev[y0][x0] > elev[y1][x1] else (x1, y1)
            lx, ly = (x1, y1) if elev[y0][x0] > elev[y1][x1] else (x0, y0)
            _mark_stair_cell(hx, hy, orient)
            if tile_grid[ly][lx] not in ("stone_tile", "wood_floor"):
                tile_grid[ly][lx] = "stone_tile"

    # 3) 为跨度过长的单侧崖线增补 1~2 处副阶梯，避免绕远路并丰富立体街景
    for y in range(2, th - 2):
        run: list[int] = []
        for x in range(2, tw - 2):
            if (
                _is_walkable_tid(tile_grid[y][x])
                and _is_walkable_tid(tile_grid[y + 1][x])
                and abs(elev[y][x] - elev[y + 1][x]) == 1
            ):
                run.append(x)
            else:
                if len(run) >= 14:
                    has_st = any(
                        f"{rx},{y}" in stairs or f"{rx},{y + 1}" in stairs
                        for rx in run
                    )
                    if not has_st:
                        mid = run[len(run) // 2]
                        for sx in (mid, mid + 1):
                            if sx in run and elev[y][sx] == elev[y][mid] and elev[y + 1][sx] == elev[y + 1][mid]:
                                hx, hy = (sx, y) if elev[y][sx] > elev[y + 1][sx] else (sx, y + 1)
                                lx, ly = (sx, y + 1) if elev[y][sx] > elev[y + 1][sx] else (sx, y)
                                _mark_stair_cell(hx, hy, "ns")
                                tile_grid[ly][lx] = "stone_tile"
                run = []

    elev_dict: dict[str, int] = {}
    for y in range(th):
        for x in range(tw):
            if elev[y][x] > 0:
                elev_dict[f"{x},{y}"] = int(elev[y][x])
    return elev_dict, stairs


# ---------------------------------------------------------------------------
# 院落切分（Courtyards）、街块微景观（Pocket Parks）与自适应街灯系统
# ---------------------------------------------------------------------------

def _town_critical_points_reachable(
    engine: Any,
    town_space_id: str,
    start: tuple[int, int],
    critical_goals: Sequence[tuple[int, int]],
) -> bool:
    if not critical_goals:
        return True
    with engine._lock:
        tsp = engine._world["spaces"].get(town_space_id)
        if not tsp:
            return True
        tw, th = int(tsp["width"]), int(tsp["height"])
        reachable = engine._reachable_cells_locked(town_space_id, start, max_steps=tw * th)
    return all(pt in reachable for pt in critical_goals)


def subdivide_parcels_and_populate_outdoors(
    engine: Any,
    town_space_id: str,
    *,
    tw: int,
    th: int,
    theme: str,
    layout_style: str,
    buildings: Sequence[Mapping[str, Any]],
    road_cells: set[tuple[int, int]],
    plaza_bounds: tuple[int, int, int, int],
    plaza_start: tuple[int, int],
    board_note: str,
    rng: random.Random,
) -> dict[str, Any]:
    """切分院落与街块，放置围栏、树木、长椅、水井与沿路自适应街灯（取代固定中心四盏灯）。"""
    with engine._lock:
        tsp = engine._world["spaces"][town_space_id]
        elev_map = dict(tsp.get("elevations") or {})
        stair_map = dict(tsp.get("stairs") or {})
        ground_sample = engine.get_cell_tile(tsp, 2, 2)["tile_id"]

    def _elev(x: int, y: int) -> int:
        return int(elev_map.get(f"{x},{y}", 0) or 0)

    def _is_stair(x: int, y: int) -> bool:
        return f"{x},{y}" in stair_map

    # 收集不可侵占的核心通道格：建筑本体、门前走廊、阶梯与阶梯出入口、主干道路
    bldg_cells: set[tuple[int, int]] = set()
    door_keepout: set[tuple[int, int]] = set()
    critical_goals: list[tuple[int, int]] = []
    for b in buildings:
        bx, by, bw, bh = int(b["x"]), int(b["y"]), int(b["w"]), int(b["h"])
        dx, dy = int(b["door_x"]), int(b["door_y"])
        for yy in range(by, by + bh):
            for xx in range(bx, bx + bw):
                bldg_cells.add((xx, yy))
        if 1 <= dy + 1 < th - 1:
            critical_goals.append((dx, dy + 1))
        for oy in range(0, 4):
            for ox in (-1, 0, 1):
                door_keepout.add((dx + ox, dy + oy))

    stair_keepout: set[tuple[int, int]] = set()
    for sk in stair_map:
        sx, sy = (int(p) for p in sk.split(",", 1))
        for oy in (-1, 0, 1):
            for ox in (-1, 0, 1):
                stair_keepout.add((sx + ox, sy + oy))

    protected = bldg_cells | door_keepout | stair_keepout | road_cells | {plaza_start}

    # 1) 建筑专属院落切分：差异化庭院铺装 + 半围合木栏/石垣 + 门侧主题小品
    yard_paint: list[dict[str, Any]] = []
    fence_motif = "stone_wall_low" if theme in ("snow_onsen", "neon_market") else "wood_fence"
    fence_bp = synthesize_outdoor_blueprint(fence_motif, theme=theme)
    placed_fences = 0
    placed_props = 0

    yard_tiles_by_role: dict[str, tuple[str, ...]] = {
        "home": ("flower_grass", "grass", "wood_floor"),
        "cafe": ("wood_floor", "stone_tile", "tatami"),
        "shop": ("stone_tile", "cobble_road", "wood_floor"),
        "library": ("stone_tile", "flower_grass"),
        "arcade": ("stone_tile", "carpet_blue"),
        "public": ("stone_tile", "flower_grass", "wood_floor"),
    }

    for idx, b in enumerate(buildings):
        bx, by, bw, bh = int(b["x"]), int(b["y"]), int(b["w"]), int(b["h"])
        dx, dy = int(b["door_x"]), int(b["door_y"])
        role = str(b.get("role") or "public")
        b_elev = _elev(bx, by)
        candidates = [
            t for t in yard_tiles_by_role.get(role, ("stone_tile", "flower_grass"))
            if t != ground_sample and t in TILE_CATALOG
        ]
        yard_tid = candidates[idx % len(candidates)] if candidates else "stone_tile"

        # 院落范围：左右各扩 2 格、后方扩 1 格、门前扩 2 格（仅限同海拔且非道路/水体）
        yx0 = max(2, bx - 2)
        yx1 = min(tw - 3, bx + bw + 1)
        yy0 = max(2, by - 1)
        yy1 = min(th - 3, by + bh + 1)
        yard_ring: list[tuple[int, int]] = []
        for yy in range(yy0, yy1 + 1):
            for xx in range(yx0, yx1 + 1):
                if (xx, yy) in bldg_cells or (xx, yy) in road_cells or _is_stair(xx, yy):
                    continue
                if _elev(xx, yy) != b_elev:
                    continue
                if not engine.get_cell_tile(tsp, xx, yy).get("walkable", True):
                    continue
                yard_paint.append({"x": xx, "y": yy, "tile_id": yard_tid})
                is_edge = xx in (yx0, yx1) or yy in (yy0, yy1)
                if is_edge and (xx, yy) not in protected:
                    # 留出正面大门 >= 3 格宽敞院门开口
                    if yy >= dy and abs(xx - dx) <= 2:
                        continue
                    yard_ring.append((xx, yy))

        # 约 70% 的建筑生成半围合院墙/木栅栏（跳格或转角围合，透气不死板）
        if yard_ring and rng.random() < 0.72:
            rng.shuffle(yard_ring)
            max_seg = min(len(yard_ring), rng.randint(4, 9))
            for fx, fy in yard_ring[:max_seg]:
                # 不贴悬崖外沿摆双层墙，只摆在同高度平整院界
                if any(_elev(fx + ox, fy + oy) != b_elev for ox, oy in ((1, 0), (-1, 0), (0, 1), (0, -1))):
                    continue
                try:
                    ent = engine.place_ephemeral_entity(town_space_id, fence_bp, x=fx, y=fy)
                    if not _town_critical_points_reachable(engine, town_space_id, plaza_start, critical_goals):
                        engine.remove_entity(ent["entity_id"])
                        continue
                    placed_fences += 1
                    protected.add((fx, fy))
                except ValueError:
                    continue

        # 门旁庭院特色物件（贩卖机 / 花箱 / 石灯笼 / 路牌）
        accent_motif = {
            "shop": "vending_machine" if theme in ("neon_market", "sakura", "seaside") else "market_stall",
            "arcade": "vending_machine",
            "cafe": "flower_planter",
            "home": "stone_lantern" if theme == "snow_onsen" else "flower_planter",
            "library": "wooden_signpost",
            "public": "stone_lantern",
        }.get(role, "flower_planter")
        accent_bp = synthesize_outdoor_blueprint(accent_motif, theme=theme)
        aw, ah = accent_bp["footprint"]
        side_spots = [(bx - aw - 1, dy), (bx + bw + 1, dy), (dx - aw - 2, dy + 1), (dx + 2, dy + 1)]
        rng.shuffle(side_spots)
        for sx, sy in side_spots:
            cells = [(sx + ox, sy + oy) for ox in range(aw) for oy in range(ah)]
            if any(
                not (2 <= cx < tw - 2 and 2 <= cy < th - 2)
                or (cx, cy) in protected
                or _elev(cx, cy) != b_elev
                for cx, cy in cells
            ):
                continue
            try:
                ent = engine.place_ephemeral_entity(town_space_id, accent_bp, x=sx, y=sy)
                if not _town_critical_points_reachable(engine, town_space_id, plaza_start, critical_goals):
                    engine.remove_entity(ent["entity_id"])
                else:
                    placed_props += 1
                    protected.update(cells)
                    break
            except ValueError:
                continue

    if yard_paint:
        engine.paint_tiles(town_space_id, cells=yard_paint)

    # 2) 空余街块切分：高台古井/喷泉观景台 + 树丛口袋公园 + 休息长椅
    tree_pool = {
        "sakura": ("sakura_tree", "sakura_tree", "shade_tree"),
        "snow_onsen": ("pine_tree", "pine_tree", "sakura_tree"),
        "neon_market": ("shade_tree", "sakura_tree"),
        "seaside": ("shade_tree", "pine_tree"),
        "forest_academy": ("pine_tree", "shade_tree", "shade_tree"),
    }.get(theme, ("sakura_tree", "shade_tree"))

    # 网格采样切分 6×6 街块微单元
    patch_paint: list[dict[str, Any]] = []
    placed_trees = 0
    well_placed = False
    cell_step = 7
    patch_coords = [
        (px, py)
        for py in range(2, th - 6, cell_step)
        for px in range(2, tw - 6, cell_step)
    ]
    rng.shuffle(patch_coords)

    for px, py in patch_coords:
        free_2x2: list[tuple[int, int, int]] = []
        for yy in range(py, min(th - 3, py + cell_step - 1)):
            for xx in range(px, min(tw - 3, px + cell_step - 1)):
                quad = [(xx, yy), (xx + 1, yy), (xx, yy + 1), (xx + 1, yy + 1)]
                if any(q in protected for q in quad):
                    continue
                ev_set = {_elev(qx, qy) for qx, qy in quad}
                if len(ev_set) != 1:
                    continue
                with engine._lock:
                    sp_now = engine._world["spaces"][town_space_id]
                    if any(not engine.get_cell_tile(sp_now, qx, qy).get("walkable", True) for qx, qy in quad):
                        continue
                free_2x2.append((xx, yy, next(iter(ev_set))))
        if not free_2x2:
            continue
        rng.shuffle(free_2x2)
        tx, ty, pev = free_2x2[0]

        # 优先在最高台地（elev >= 1）打造 1 处古井或喷泉小广场
        if not well_placed and pev >= 1:
            hero_motif = "stone_well" if theme in ("snow_onsen", "sakura", "forest_academy") else "fountain_basin"
            hero_bp = synthesize_outdoor_blueprint(hero_motif, theme=theme)
            try:
                ent = engine.place_ephemeral_entity(town_space_id, hero_bp, x=tx, y=ty)
                if _town_critical_points_reachable(engine, town_space_id, plaza_start, critical_goals):
                    well_placed = True
                    placed_props += 1
                    protected.update({(tx, ty), (tx + 1, ty), (tx, ty + 1), (tx + 1, ty + 1)})
                    for oy in range(-1, 3):
                        for ox in range(-1, 3):
                            gx, gy = tx + ox, ty + oy
                            if 1 <= gx < tw - 1 and 1 <= gy < th - 1 and _elev(gx, gy) == pev and not _is_stair(gx, gy):
                                if (gx, gy) not in bldg_cells and (gx, gy) not in road_cells and engine.get_cell_tile(tsp, gx, gy).get("walkable", True):
                                    patch_paint.append({"x": gx, "y": gy, "tile_id": "stone_tile"})
                    continue
                engine.remove_entity(ent["entity_id"])
            except ValueError:
                pass

        # 口袋树丛与花草地被
        if placed_trees < max(6, (tw * th) // 155):
            tmotif = rng.choice(tree_pool)
            tbp = synthesize_outdoor_blueprint(tmotif, theme=theme)
            try:
                ent = engine.place_ephemeral_entity(town_space_id, tbp, x=tx, y=ty)
                if _town_critical_points_reachable(engine, town_space_id, plaza_start, critical_goals):
                    placed_trees += 1
                    placed_props += 1
                    protected.update({(tx, ty), (tx + 1, ty), (tx, ty + 1), (tx + 1, ty + 1)})
                    lawn_tid = "flower_grass" if theme in ("sakura", "forest_academy") else "grass"
                    if theme != "neon_market":
                        for oy in range(-1, 3):
                            for ox in range(-1, 3):
                                gx, gy = tx + ox, ty + oy
                                if (
                                    1 <= gx < tw - 1
                                    and 1 <= gy < th - 1
                                    and (gx, gy) not in road_cells
                                    and (gx, gy) not in bldg_cells
                                    and not _is_stair(gx, gy)
                                    and _elev(gx, gy) == pev
                                    and engine.get_cell_tile(tsp, gx, gy).get("walkable", True)
                                ):
                                    patch_paint.append({"x": gx, "y": gy, "tile_id": lawn_tid})
                    # 树荫下顺手放一张街角长椅（非阻挡 restable）
                    if len(free_2x2) > 1 and rng.random() < 0.55:
                        bx2, by2, bev2 = free_2x2[1]
                        if (bx2, by2) not in protected and (bx2 + 1, by2) not in protected and bev2 == pev:
                            bench_bp = synthesize_outdoor_blueprint("park_bench", theme=theme)
                            try:
                                engine.place_ephemeral_entity(town_space_id, bench_bp, x=bx2, y=by2)
                                protected.update({(bx2, by2), (bx2 + 1, by2)})
                                placed_props += 1
                            except ValueError:
                                pass
                else:
                    engine.remove_entity(ent["entity_id"])
            except ValueError:
                pass

    if patch_paint:
        engine.paint_tiles(town_space_id, cells=patch_paint)

    # 3) 小镇布告栏与指路牌：放在广场或主干道交汇处的路肩，不再伴生四角路灯阵
    px0, py0, px1, py1 = plaza_bounds
    board_candidates: list[tuple[int, int]] = []
    for yy in range(max(2, py0 - 1), min(th - 2, py1 + 2)):
        for xx in range(max(2, px0 - 1), min(tw - 2, px1 + 2)):
            if (xx, yy) in bldg_cells or (xx, yy) in door_keepout or _is_stair(xx, yy):
                continue
            is_edge = xx in (px0, px1) or yy in (py0, py1)
            if is_edge:
                board_candidates.append((xx, yy))
    rng.shuffle(board_candidates)
    for bx_c, by_c in board_candidates + [(px0 + 1, py0 + 1)]:
        try:
            board = engine.place_from_blueprint(
                "default_notebook",
                town_space_id,
                bx_c,
                by_c,
                delivery_mode="silent",
                note_text=board_note,
            )
            engine.update_entity_spec(board["entity_id"], name="小镇布告栏")
            protected.add((bx_c, by_c))
            break
        except ValueError:
            continue

    # 4) 沿道路与阶梯口自适应分布街灯（彻底告别地图正中死板的 4 盏灯）
    lamp_shoulders: list[tuple[int, int, int]] = []
    with engine._lock:
        sp_now = engine._world["spaces"][town_space_id]
        for y in range(2, th - 2):
            for x in range(2, tw - 2):
                if (x, y) in protected or _is_stair(x, y):
                    continue
                if not engine.get_cell_tile(sp_now, x, y).get("walkable", True):
                    continue
                ev = _elev(x, y)
                adj_road = any((x + ox, y + oy) in road_cells for ox, oy in ((1, 0), (-1, 0), (0, 1), (0, -1)))
                adj_stair = any(_is_stair(x + ox, y + oy) for ox, oy in ((2, 0), (-2, 0), (0, 2), (0, -2)))
                if adj_road or adj_stair:
                    prio = 2 if adj_stair else 1
                    lamp_shoulders.append((prio, x, y))

    rng.shuffle(lamp_shoulders)
    lamp_shoulders.sort(key=lambda item: -item[0])

    lamp_positions: list[tuple[int, int]] = []
    target_lamps = clamp_int((tw + th) // 9, 5, 10, 7)
    min_lamp_dist = 8
    lamp_motif = {
        "snow_onsen": "stone_lantern",
        "neon_market": "neon_bollard",
    }.get(theme, "")

    for _, lx, ly in lamp_shoulders:
        if len(lamp_positions) >= target_lamps:
            break
        if any(abs(lx - px_i) + abs(ly - py_i) < min_lamp_dist for px_i, py_i in lamp_positions):
            continue
        try:
            if lamp_motif:
                lbp = synthesize_outdoor_blueprint(lamp_motif, theme=theme)
                ent = engine.place_ephemeral_entity(town_space_id, lbp, x=lx, y=ly)
            else:
                ent = engine.place_from_blueprint(
                    "default_street_lamp",
                    town_space_id,
                    lx,
                    ly,
                    delivery_mode="silent",
                )
                upd_tr = dict(ent.get("traits") or {})
                if "switchable" in upd_tr:
                    upd_tr["switchable"] = {**upd_tr["switchable"], "is_on": True}
                    engine.update_entity_spec(ent["entity_id"], traits=upd_tr)
            if not _town_critical_points_reachable(engine, town_space_id, plaza_start, critical_goals):
                engine.remove_entity(ent["entity_id"])
                continue
            lamp_positions.append((lx, ly))
            protected.add((lx, ly))
        except ValueError:
            continue

    return {
        "fences": placed_fences,
        "trees": placed_trees,
        "props": placed_props,
        "street_lamps": len(lamp_positions),
        "lamp_positions": lamp_positions,
    }
