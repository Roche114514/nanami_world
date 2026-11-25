"""Holodeck 风格室内场景图规划器、3 阶段 CSP 布局求解器与主题多格像素家具库。

负责打破室内摆设的固定九宫格模板：
1. 根据房间语义 / 世界观 / 单批 LLM 规划生成带空间关系（centerpiece / against_wall /
   in_corner / near / on_surface / wall_hang / open）的 Scene Graph。
2. 通过 3 阶段约束松弛（strict -> relaxed -> open）与每步 BFS 连通性校验，将 [2,2]、[2,1]、[1,1]
   多格主题家具与内置交互物件摆放到无堵门、全可达、且各房间布局各异的位置。
3. 内置 20 种 16×16 / 32×16 / 32×32 多格像素家具母版与主题色板，通过临时实体通道实例化。
"""

from __future__ import annotations

import copy
import json
import random
import re
import zlib
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from .schema import (
    DEFAULT_SLOT_COLORS,
    clamp_int,
    clip_text,
    normalize_pixel_grid,
    slugify,
)


def _mirror32(halves: Sequence[str]) -> tuple[str, ...]:
    """将 16 列左半幅像素行左右镜像为严格 32 列宽的像素画。"""
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
# 20 种多格像素家具母版（16×16 / 32×16 / 32×32）
# ---------------------------------------------------------------------------

FURNITURE_MOTIFS: dict[str, dict[str, Any]] = {
    "fireplace": {
        "footprint": [2, 2],
        "default_name": "雕花壁炉",
        "default_brief": "砌着厚石烟道、炉膛里跳动着暖橘柴火的宽大壁炉。",
        "solid": True,
        "placement": "floor",
        "traits": {
            "switchable": {
                "provides_light": True,
                "is_on": True,
                "on_brief": "炉火正旺、散发暖意的雕花壁炉",
                "off_brief": "只余灰烬的冷清雕花壁炉",
            },
            "thermal": {"cool_minutes": 240, "temp": "hot"},
        },
        "tags": ["furniture", "light", "hearth"],
        "accent_colors": {"a": "#ff8c42", "2": "#ffd97d"},
        "rows": _mirror32([
            ".....ooooooooooo",
            "....o33333333333",
            "....o31111111111",
            "....o31133331111",
            "....o31111111111",
            "...ooooooooooooo",
            "...o222222222222",
            "...o333333333333",
            "..oooooooooooooo",
            "..o2222222222222",
            "..o3111333311133",
            "..o3111111111111",
            "..o3111333311111",
            ".ooooooooooooooo",
            ".o22222222222222",
            ".o33311111133311",
            ".o311333333o3111",
            ".o31131111oo....",
            ".o3113111o......",
            ".o311311o....aa.",
            ".o311311o...a22a",
            ".o311311o..aa22a",
            ".o311311o.oa22aa",
            ".o311311o.oa2aaa",
            ".o311311oo3a2aa3",
            ".o311311oo333333",
            ".o311311oooooooo",
            ".o33333311111111",
            "oooooooooooooooo",
            "o333333333333333",
            "oooooooooooooooo",
            "................",
        ]),
    },
    "grand_piano": {
        "footprint": [2, 2],
        "default_name": "三角钢琴",
        "default_brief": "漆面光可鉴人、支着斜盖的古典三角钢琴。",
        "solid": True,
        "placement": "floor",
        "traits": {
            "switchable": {
                "provides_light": False,
                "is_on": False,
                "on_brief": "琴盖敞开、仿佛刚响过余音的三角钢琴",
                "off_brief": "静静合着琴盖的古典三角钢琴",
            },
            "activity": {"domain": "music", "progress_unit": "首", "progress": 0, "battery": None, "battery_cost": 0},
        },
        "tags": ["furniture", "music", "piano"],
        "accent_colors": {"a": "#e0b65c", "b": "#d8e6f2"},
        "rows": _fit_rows([
            "................................",
            "........ooooooooooooooo.........",
            "......oo333333333333333oo.......",
            ".....o3331111111111111333o......",
            "....o331112221111111111133o.....",
            "...o33112222211111111111133o....",
            "...o31122111111111111111113o....",
            "..o3111111111111111111111113o...",
            "..o3111111111111111111111113o...",
            "..o311111111111111111111113o....",
            "..o31111111111111111111113o.....",
            "..o311111111111111111113oo......",
            "..o3111111111111111113oo........",
            "..o31111111111bbbbb33o..........",
            "..o33333333333bbbbb3o...........",
            "..o31111111111333333oooooo......",
            ".oooooooooooooooooooooooooo.....",
            ".o32o2o22o2o22o2o22o2o22o3o.....",
            ".o32o2o22o2o22o2o22o2o22o3o.....",
            ".o322222222222222222222223o.....",
            ".oooooooooooooooooooooooooo.....",
            "..o3333333333333333333333o......",
            "..o311o..............o113o......",
            "..o311o......oo......o113o......",
            "..o311o.....o33o.....o113o......",
            "..o311o.....o33o.....o113o......",
            "..o311o.....oaaao....o113o......",
            "..o311o......ooo.....o113o......",
            "..o333o..............o333o......",
            "..ooooo..............ooooo......",
            "................................",
            "................................",
        ], 32, 32),
    },
    "long_table": {
        "footprint": [2, 2],
        "default_name": "长条议事桌",
        "default_brief": "铺着绣边桌旗、足可摆满器皿或卷宗的厚实长桌。",
        "solid": True,
        "placement": "floor",
        "traits": {"surface": {"capacity": 8}},
        "tags": ["furniture", "table", "surface"],
        "rows": _mirror32([
            "................................",
            "................................",
            "...ooooooooooooo",
            "..o2222222222222",
            ".o2211111111111a",
            ".o211111111111aa",
            ".o21111111111aa2",
            ".o21111111111aa2",
            ".o21111111111aa2",
            ".o21111111111aa2",
            ".o21111111111aa2",
            ".o21111111111aa2",
            ".o21111111111aa2",
            ".o21111111111aa2",
            ".o21111111111aa2",
            ".o21111111111aa2",
            ".o21111111111aa2",
            ".o21111111111aa2",
            ".o211111111111aa",
            ".o3333333333333a",
            ".oooooooooooooooo",
            "..o3113o......o3",
            "..o3113o......oa",
            "..o3113o........",
            "..o3113o........",
            "..o3113o........",
            "..o3113o........",
            "..o3333o........",
            "..oooooo........",
            "................",
            "................",
            "................",
        ]),
    },
    "sofa_plush": {
        "footprint": [2, 2],
        "default_name": "丝绒长沙发",
        "default_brief": "嵌着木雕扶手与厚实软垫的宽敞双人沙发。",
        "solid": False,
        "placement": "floor",
        "traits": {"restable": {"rest_mode": "sit", "energy_regen_per_min": 0.016, "comfort": 0.88, "occupied_by": None}},
        "tags": ["furniture", "sofa", "rest"],
        "rows": _mirror32([
            "................................",
            "................................",
            "................................",
            "..oooooooooooooo",
            ".o31111111111111",
            ".o31222222222222",
            ".o31222222222222",
            ".o31221122222112",
            ".o31222222222222",
            ".o31111111111111",
            "oooooooooooooooo",
            "o3112o2222222222",
            "o3112o2222222222",
            "o3112o2221222222",
            "o3112o2222222222",
            "o3112o222222222a",
            "o3112o11111111aa",
            "o3112o11111111aa",
            "o3333o33333333aa",
            "oooooooooooooooo",
            ".o311o..........",
            ".o333o..........",
            ".ooooo..........",
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
    "hot_spring_tub": {
        "footprint": [2, 2],
        "default_name": "桧木汤池",
        "default_brief": "冒着淡淡温热雾气、嵌石砌边的室内宽汤池。",
        "solid": True,
        "placement": "floor",
        "traits": {
            "thermal": {"cool_minutes": 180, "temp": "hot"},
            "activity": {"domain": "hobby", "progress_unit": "次泡汤", "progress": 0, "battery": None, "battery_cost": 0},
        },
        "tags": ["furniture", "bath", "water", "tea"],
        "accent_colors": {"b": "#69b9d6", "2": "#c9f0fa"},
        "rows": _mirror32([
            ".....2.....2....",
            "....22......2...",
            ".......2........",
            "..oooooooooooooo",
            ".o31111111111111",
            ".o12222222222222",
            "o312oooooooooooo",
            "o312obbbbbbbbbbb",
            "o312obbb222bbbbb",
            "o312obbbbbbb22bb",
            "o312obb22bbbbbbb",
            "o312obbbbbbbbb22",
            "o312obbbb22bbbbb",
            "o312obbbbbbbbbbb",
            "o312obb222bbbbbb",
            "o312obbbbbbb22bb",
            "o312obbbbbbbbbbb",
            "o312obbb22bbbbbb",
            "o312obbbbbbbbb2b",
            "o312obbbbbbbbbbb",
            "o312o33333333333",
            "o311oooooooooooo",
            "o331111111111111",
            ".o33333333333333",
            ".o31133111331113",
            ".o31133111331113",
            ".o33333333333333",
            "..oooooooooooooo",
            "................",
            "................",
            "................",
            "................",
        ]),
    },
    "altar_pedestal": {
        "footprint": [2, 2],
        "default_name": "秘仪石台",
        "default_brief": "刻满星象与纹章凹槽的方正石座展台。",
        "solid": True,
        "placement": "floor",
        "traits": {
            "surface": {"capacity": 4},
            "inscribable": {"writable_by_ai": True, "max_entries": 8, "entries": []},
        },
        "tags": ["furniture", "pedestal", "ritual"],
        "rows": _mirror32([
            "................",
            "......oooooooooo",
            ".....obbbbbbbbbb",
            "....obbb22222bbb",
            "....obb22aaaa2bb",
            "....obbbbbbbbbbb",
            ".....ooooooooooo",
            "....o22222222222",
            "...o111111111111",
            "..o3111aaaaa1111",
            "..o3111111111111",
            "..o3333333333333",
            "...ooooooooooooo",
            "...o311111111111",
            "...o311abbbba111",
            "...o311ab22ba111",
            "...o311ab22ba111",
            "...o311abbbba111",
            "...o311aaaaaa111",
            "...o311111111111",
            "..oooooooooooooo",
            ".o22222222222222",
            ".o11111111111111",
            ".o33333333333333",
            "oooooooooooooooo",
            "o311133311133311",
            "o333333333333333",
            "oooooooooooooooo",
            "................",
            "................",
            "................",
            "................",
        ]),
    },
    "lab_bench": {
        "footprint": [2, 1],
        "default_name": "炼金实验长台",
        "default_brief": "摆满玻璃曲颈瓶、酒精灯与试剂架的宽阔实验台。",
        "solid": True,
        "placement": "floor",
        "traits": {
            "surface": {"capacity": 6},
            "activity": {"domain": "craft", "progress_unit": "次实验", "progress": 0, "battery": None, "battery_cost": 0},
        },
        "tags": ["furniture", "lab", "surface"],
        "accent_colors": {"b": "#69e3c6", "a": "#f59e42"},
        "rows": _fit_rows([
            "....oo........oo.........oo.....",
            "...obbo......obbo.......oaao....",
            "...obbbo....obbbo......oaaaao...",
            "..o2bbbo....ob2bo......oa22ao...",
            "..ooooo..oo..ooo...oo...oooo....",
            ".oooooooooooooooooooooooooooooo.",
            ".o2222222222222222222222222222o.",
            ".o1111111111111111111111111111o.",
            ".o3333333333333333333333333333o.",
            ".oooooooooooooooooooooooooooooo.",
            "..o311o..o31111111o..o311o.o31o.",
            "..o311o..o31aa1aa1o..o311o.o31o.",
            "..o311o..o33333333o..o311o.o31o.",
            "..o311o..o31111111o..o311o.o31o.",
            "..o333o..oooooooooo..o333o.o33o.",
            "..ooooo..............ooooo.oooo.",
        ], 32, 16),
    },
    "bar_counter": {
        "footprint": [2, 1],
        "default_name": "木制吧台",
        "default_brief": "打磨出柔光的实木宽吧台，下沿嵌有黄铜栏杆。",
        "solid": True,
        "placement": "floor",
        "traits": {"surface": {"capacity": 6}},
        "tags": ["furniture", "counter", "surface"],
        "rows": _mirror32([
            "................................",
            ".ooooooooooooooo",
            "o222222222222222",
            "o111111111111111",
            "o333333333333333",
            "oooooooooooooooo",
            ".o31113111311131",
            ".o31113111311131",
            ".o31113111311131",
            ".o31113111311131",
            ".oaaaaaaaaaaaaaa",
            ".o31113111311131",
            ".o33333333333333",
            ".ooooooooooooooo",
            "................",
            "................",
        ]),
    },
    "display_case": {
        "footprint": [2, 1],
        "default_name": "玻璃陈列柜",
        "default_brief": "装有亮洁玻璃罩与天鹅绒衬垫的双联博物展柜。",
        "solid": True,
        "placement": "floor",
        "traits": {
            "container": {"capacity": 6, "key_tag": "", "is_open": False, "is_locked": False},
            "surface": {"capacity": 4},
        },
        "tags": ["furniture", "container", "museum"],
        "rows": _mirror32([
            ".ooooooooooooooo",
            "obbbbbbbbbbbbbbb",
            "obb22bbbbbbbbbbb",
            "obb2bbbbbaaaabbb",
            "obbbbbbbba22abbb",
            "obbbbbbbbaaaabbb",
            "obbbbbbbbbbbbbbb",
            "oooooooooooooooo",
            "o111111111111111",
            "o31111o331111o31",
            "o31111o331111o31",
            "o33333o333333o33",
            "oooooooooooooooo",
            ".o33o......o33o.",
            ".oooo......oooo.",
            "................",
        ]),
    },
    "gear_console": {
        "footprint": [2, 1],
        "default_name": "黄铜齿轮控制台",
        "default_brief": "密布铜制咬合齿轮、压力指针与指示灯的机械台。",
        "solid": True,
        "placement": "floor",
        "traits": {
            "switchable": {
                "provides_light": True,
                "is_on": True,
                "on_brief": "指针微颤、指示灯亮着的齿轮控制台",
                "off_brief": "彻底停摆的黄铜齿轮控制台",
            },
            "activity": {"domain": "craft", "progress_unit": "次校准", "progress": 0, "battery": None, "battery_cost": 0},
        },
        "tags": ["furniture", "clockwork", "machine"],
        "accent_colors": {"a": "#e0a938", "b": "#7ae7ff"},
        "rows": _fit_rows([
            "...ooooo.......ooooo............",
            "..oabbbao.....o2bbb2o....oooo...",
            "..ob2bbbo.....obbb2bo...oaaaao..",
            "..oaaaaao.....oaaaaao...oa22ao..",
            ".oooooooooooooooooooooooooooooo.",
            ".o3311111111111111111111111133o.",
            ".o31a1a13333bbbbbbbb3333a1a113o.",
            ".o31aaa13111b2bbbb2b1113aaa113o.",
            ".o311a113111bbbbbbbb11131a1113o.",
            ".o31aaa13111111111111113aaa113o.",
            ".o31a1a13333333333333333a1a113o.",
            ".o3311111111111111111111111133o.",
            ".o3333333333333333333333333333o.",
            "..o33o....................o33o..",
            "..oooo....................oooo..",
            "................................",
        ], 32, 16),
    },
    "folding_screen": {
        "footprint": [2, 1],
        "default_name": "绘金折扇屏风",
        "default_brief": "四扇细木棂框嵌着暖黄绢面的双格屏风。",
        "solid": True,
        "placement": "floor",
        "traits": {},
        "tags": ["furniture", "partition", "decor"],
        "rows": _mirror32([
            ".ooooooooooooooo",
            "o3113o2222o3113o",
            "o3223o2aa2o3223o",
            "o3223o2aa2o3223o",
            "o32a3o2222o32a3o",
            "o3223o2222o3223o",
            "o3223o2aa2o3223o",
            "o3223o2222o3223o",
            "o3113o1111o3113o",
            "o3223o2222o3223o",
            "o3223o2222o3223o",
            "o3113o1111o3113o",
            "o3333o3333o3333o",
            "oooooooooooooooo",
            ".o3o........o3o.",
            ".ooo........ooo.",
        ]),
    },
    "wardrobe": {
        "footprint": [2, 1],
        "default_name": "双门实木衣橱",
        "default_brief": "雕有浮雕线脚与黄铜拉手的双开门大衣橱。",
        "solid": True,
        "placement": "floor",
        "traits": {"container": {"capacity": 8, "key_tag": "", "is_open": False, "is_locked": False}},
        "tags": ["furniture", "container", "bedroom"],
        "rows": _mirror32([
            ".ooooooooooooooo",
            "o222222222222222",
            "o31111111111111o",
            "o31333333311111o",
            "o31311111311111o",
            "o31312211311111o",
            "o313111113111aao",
            "o313111113111aao",
            "o31311111311111o",
            "o31333333311111o",
            "o31111111111111o",
            "o33333333333333o",
            "oooooooooooooooo",
            ".o33o......o33o.",
            ".oooo......oooo.",
            "................",
        ]),
    },
    "tea_hearth": {
        "footprint": [2, 1],
        "default_name": "地炉茶席",
        "default_brief": "中央嵌着红碳火塘与铸铁茶釜的矮方茶桌。",
        "solid": True,
        "placement": "floor",
        "traits": {
            "surface": {"capacity": 4},
            "thermal": {"cool_minutes": 180, "temp": "hot"},
        },
        "tags": ["furniture", "tea", "hearth"],
        "accent_colors": {"a": "#f97316", "b": "#6ec29b"},
        "rows": _mirror32([
            "..............oo",
            "............oobb",
            ".ooooooooooob22b",
            "o22222222222oooo",
            "o111111133333111",
            "o11bbbb13aaaaa11",
            "o11b22b13a22aa11",
            "o11bbbb13aaaaa11",
            "o111111133333111",
            "o333333333333333",
            "oooooooooooooooo",
            ".o31o........o3o",
            ".o31o........o3o",
            ".o33o........ooo",
            ".oooo...........",
            "................",
        ]),
    },
    "workbench": {
        "footprint": [2, 1],
        "default_name": "重木工匠台",
        "default_brief": "台面留有凿痕、装有金属台钳与底层工具架的木工台。",
        "solid": True,
        "placement": "floor",
        "traits": {
            "surface": {"capacity": 5},
            "activity": {"domain": "craft", "progress_unit": "件", "progress": 0, "battery": None, "battery_cost": 0},
        },
        "tags": ["furniture", "workshop", "surface"],
        "rows": _fit_rows([
            "................................",
            "....oo.....oo........ooo........",
            "...o33o...oaao......obbo........",
            "..oooooooooooooooooooooooooooo..",
            "..o22222222222222222222222222o..",
            "..o11111111111111111111111111o..",
            ".obbo333333333333333333333333o..",
            ".oooo311o..............o3111o...",
            "..o31111ooooooooooooooooo311o...",
            "..o31111o111333333111111o311o...",
            "..o31111o11b1111aa111111o311o...",
            "..o31111ooooooooooooooooo311o...",
            "..o31111o..............o3111o...",
            "..o33333o..............o3333o...",
            "..ooooooo..............oooooo...",
            "................................",
        ], 32, 16),
    },
    "standing_mirror": {
        "footprint": [1, 1],
        "default_name": "落地雕花穿衣镜",
        "default_brief": "嵌着亮银镜面、带着雕花底座的立式穿衣镜。",
        "solid": True,
        "placement": "floor",
        "traits": {},
        "tags": ["furniture", "decor", "mirror"],
        "rows": _fit_rows([
            "....oooooooo....",
            "...oaaaaaaaaao..",
            "..oa3bbbbbb3ao..",
            "..o3bbb22bbb3o..",
            "..o3bb22bbbb3o..",
            "..o3bbbbbbbb3o..",
            "..o3bbbb22bb3o..",
            "..o3bbb2bbbb3o..",
            "..o3bbbbbbbb3o..",
            "..o3bbbbbbbb3o..",
            "..oaaaaaaaaaao..",
            "...o31111113o...",
            "..o3o.o33o.o3o..",
            "..oo..o33o..oo..",
            ".....oooooo.....",
            "................",
        ], 16, 16),
    },
    "statue_bust": {
        "footprint": [1, 1],
        "default_name": "石刻纪念胸像",
        "default_brief": "立于条石底座上的半身石雕像，基座刻着铭文。",
        "solid": True,
        "placement": "floor",
        "traits": {"inscribable": {"writable_by_ai": True, "max_entries": 4, "entries": []}},
        "tags": ["furniture", "statue", "decor"],
        "rows": _fit_rows([
            ".....oooooo.....",
            "....o222222o....",
            "...o22211222o...",
            "...o22122122o...",
            "....o222222o....",
            ".....o2222o.....",
            "....o221122o....",
            "...o22111122o...",
            "...o11111111o...",
            "....oooooooo....",
            "...o31111113o...",
            "...o31aaaa13o...",
            "...o31111113o...",
            "..o3333333333o..",
            "..oooooooooooo..",
            "................",
        ], 16, 16),
    },
    "globe_stand": {
        "footprint": [1, 1],
        "default_name": "古铜地球仪架",
        "default_brief": "悬在倾斜黄铜子午环里的老式羊皮海图地球仪。",
        "solid": False,
        "placement": "floor",
        "traits": {
            "portable": {"weight": "heavy"},
            "activity": {"domain": "reading", "progress_unit": "次研读", "progress": 0, "battery": None, "battery_cost": 0},
        },
        "tags": ["furniture", "study", "observatory"],
        "rows": _fit_rows([
            ".....oooooo.....",
            "...oaaabbbaao...",
            "..oaabbb22baao..",
            "..oabb11bbb2ao..",
            "..oab111bb11ao..",
            "..oab11bb111ao..",
            "..oabb2bbbbbao..",
            "...oaaabbbaao...",
            "....oooooooo....",
            "......oaaao.....",
            "......o33o......",
            ".....o3113o.....",
            "....o33oo33o....",
            "...o33o..o33o...",
            "...ooo....ooo...",
            "................",
        ], 16, 16),
    },
    "candelabra": {
        "footprint": [1, 1],
        "default_name": "多头黄铜烛台",
        "default_brief": "插着三支白蜡烛、火光摇曳的复古高脚铜烛台。",
        "solid": False,
        "placement": "any",
        "traits": {
            "switchable": {
                "provides_light": True,
                "is_on": True,
                "on_brief": "三支烛火柔亮跳动的多头铜烛台",
                "off_brief": "烛芯凝蜡、暂未点燃的多头铜烛台",
            }
        },
        "tags": ["furniture", "light", "candle"],
        "accent_colors": {"a": "#ffd166", "2": "#fff3c4"},
        "rows": _fit_rows([
            "..a....aa....a..",
            ".a2a..a22a..a2a.",
            "..o....oo....o..",
            ".o2o..o22o..o2o.",
            ".o2o..o22o..o2o.",
            ".oaaaooaaaooaaao",
            "..ooaaa33aaaoo..",
            "....oo3333oo....",
            "......o33o......",
            "......oaao......",
            "......o33o......",
            "......o33o......",
            ".....o3aa3o.....",
            "....o333333o....",
            "...oooooooooo...",
            "................",
        ], 16, 16),
    },
    "specimen_rack": {
        "footprint": [1, 1],
        "default_name": "药剂标本架",
        "default_brief": "分格排着各色玻璃瓶与干草药抽屉的木质标本柜。",
        "solid": True,
        "placement": "floor",
        "traits": {"container": {"capacity": 4, "key_tag": "", "is_open": False, "is_locked": False}},
        "tags": ["furniture", "alchemy", "container"],
        "rows": _fit_rows([
            "..oooooooooooo..",
            ".o311111111113o.",
            ".o3b2oaoob2o33o.",
            ".o3bboaaobbo33o.",
            ".o333333333333o.",
            ".o3aoob2oob2o3o.",
            ".o3aaobboobbo3o.",
            ".o333333333333o.",
            ".o311o1111o113o.",
            ".o31ao1aa1o113o.",
            ".o311o1111o113o.",
            ".o333333333333o.",
            ".o311111111113o.",
            ".o333333333333o.",
            "..ooo......ooo..",
            "................",
        ], 16, 16),
    },
    "flower_vase": {
        "footprint": [1, 1],
        "default_name": "青釉花瓶座",
        "default_brief": "插满时令鲜枝的高口陶瓷花瓶，摆在方木几上。",
        "solid": False,
        "placement": "any",
        "traits": {"portable": {"weight": "heavy"}},
        "tags": ["furniture", "decor", "plant"],
        "rows": _fit_rows([
            "....aa..aa......",
            "...a2aoa2aoo....",
            "....ooa2aa2ao...",
            "..oaaaooaaao....",
            "...oo33o33o.....",
            ".....obbbbo.....",
            "......obbo......",
            ".....ob22bo.....",
            "....obb22bbo....",
            "....obbbbbbo....",
            ".....obbbbo.....",
            "....o333333o....",
            "....o311113o....",
            "....o333333o....",
            "....oooooooo....",
            "................",
        ], 16, 16),
    },
    "interior_door": {
        "footprint": [1, 1],
        "default_name": "内室木门",
        "default_brief": "连接内室隔间的带锁扣合叶木门，推开后才会点亮门后空间。",
        "solid": True,
        "placement": "any",
        "traits": {"portal": {"target_space_id": "", "target_x": 0, "target_y": 0, "is_open": False}},
        "tags": ["portal", "door", "interior_door"],
        "rows": _fit_rows([
            "..oooooooooooo..",
            ".o222222222222o.",
            ".o2obbbbbbbbo2o.",
            ".o2oooooooooo2o.",
            ".o2o11111111o2o.",
            ".o2o12222221o2o.",
            ".o2o12333321o2o.",
            ".o2o111111a1o2o.",
            ".o2o111111a1o2o.",
            ".o2o12222221o2o.",
            ".o2o12333321o2o.",
            ".o2o12333321o2o.",
            ".o2o11111111o2o.",
            ".o3o33333333o3o.",
            ".oooooooooooooo.",
            "................",
        ], 16, 16),
        "frames": {
            "closed": _fit_rows([
                "..oooooooooooo..",
                ".o222222222222o.",
                ".o2obbbbbbbbo2o.",
                ".o2oooooooooo2o.",
                ".o2o11111111o2o.",
                ".o2o12222221o2o.",
                ".o2o12333321o2o.",
                ".o2o111111a1o2o.",
                ".o2o111111a1o2o.",
                ".o2o12222221o2o.",
                ".o2o12333321o2o.",
                ".o2o12333321o2o.",
                ".o2o11111111o2o.",
                ".o3o33333333o3o.",
                ".oooooooooooooo.",
                "................",
            ], 16, 16),
            "open": _fit_rows([
                "..oooooooooooo..",
                ".o222222222222o.",
                ".o2oooooooooo2o.",
                ".o2o3333333o12o.",
                ".o2o3333333o12o.",
                ".o2o3333333o12o.",
                ".o2o3333333oa2o.",
                ".o2o3333333o12o.",
                ".o2o3333333o12o.",
                ".o2o3333333o12o.",
                ".o2o3333333o32o.",
                ".o2o3333333o32o.",
                ".o2o2222222o32o.",
                ".o3o2222222o33o.",
                ".oooooooooooooo.",
                "................",
            ], 16, 16),
        },
    },
    "staircase_up": {
        "footprint": [1, 1],
        "default_name": "上行木楼梯",
        "default_brief": "铺着防滑铜条、通往上层楼面的室内木质楼梯。",
        "solid": False,
        "placement": "floor",
        "traits": {"portal": {"target_space_id": "", "target_x": 0, "target_y": 0, "is_open": True}},
        "tags": ["portal", "stair", "stair_up"],
        "rows": _fit_rows([
            ".oooooooooooooo.",
            ".o32222aa22223o.",
            ".o311111111113o.",
            ".o322222222223o.",
            ".o311111111113o.",
            ".o32222a2a2223o.",
            ".o311111111113o.",
            ".o322222222223o.",
            ".o311111111113o.",
            ".o322222222223o.",
            ".o311111111113o.",
            ".o322222222223o.",
            ".o311111111113o.",
            ".o311111111113o.",
            ".oooooooooooooo.",
            "................",
        ], 16, 16),
    },
    "staircase_down": {
        "footprint": [1, 1],
        "default_name": "下行楼梯口",
        "default_brief": "装有扶手护栏、拾级而下的室内楼梯口。",
        "solid": False,
        "placement": "floor",
        "traits": {"portal": {"target_space_id": "", "target_x": 0, "target_y": 0, "is_open": True}},
        "tags": ["portal", "stair", "stair_down"],
        "rows": _fit_rows([
            ".oooooooooooooo.",
            ".o333333333333o.",
            ".o333333333333o.",
            ".o311111111113o.",
            ".o322222222223o.",
            ".o311111111113o.",
            ".o322222222223o.",
            ".o31111b1b1113o.",
            ".o322222b22223o.",
            ".o311111111113o.",
            ".o322222222223o.",
            ".o311111111113o.",
            ".o322222222223o.",
            ".o322222222223o.",
            ".oooooooooooooo.",
            "................",
        ], 16, 16),
    },
    "basement_hatch": {
        "footprint": [1, 1],
        "default_name": "地窖活板门",
        "default_brief": "嵌在地板上的铁扣厚木盖板，掀开后有窄梯通往地下一层。",
        "solid": False,
        "placement": "floor",
        "traits": {"portal": {"target_space_id": "", "target_x": 0, "target_y": 0, "is_open": True}},
        "tags": ["portal", "stair", "hatch"],
        "rows": _fit_rows([
            "................",
            ".oooooooooooooo.",
            ".o311111111113o.",
            ".o122222222221o.",
            ".o12oooooooo21o.",
            ".o12o331133o21o.",
            ".o12o312213o21o.",
            ".o12o331133o21o.",
            ".o12o312213o21o.",
            ".o12o331a33o21o.",
            ".o12o312a21o21o.",
            ".o12oooooooo21o.",
            ".o122222222221o.",
            ".o333333333333o.",
            ".oooooooooooooo.",
            "................",
        ], 16, 16),
    },
    "wood_chair": {
        "footprint": [1, 1],
        "default_name": "靠背木椅",
        "default_brief": "弧形扶栏的温润实木椅，座面放着薄布坐垫。",
        "solid": False,
        "placement": "floor",
        "traits": {"restable": {"rest_mode": "sit", "energy_regen_per_min": 0.011, "comfort": 0.72, "occupied_by": None}},
        "tags": ["furniture", "chair", "rest"],
        "rows": _fit_rows([
            "................",
            "....oooooooo....",
            "....o222222o....",
            "....o131131o....",
            "....o131131o....",
            "....o111111o....",
            "....oooooooo....",
            "...o22222222o...",
            "...o11aaaa11o...",
            "...o11111111o...",
            "...oooooooooo...",
            "....o3o..o3o....",
            "....o3o..o3o....",
            "....o3o..o3o....",
            "....ooo..ooo....",
            "................",
        ], 16, 16),
    },
    "bar_stool": {
        "footprint": [1, 1],
        "default_name": "铜圈高脚凳",
        "default_brief": "旋转铁木面高脚凳，最适合挨在吧台边小坐。",
        "solid": False,
        "placement": "floor",
        "traits": {"restable": {"rest_mode": "sit", "energy_regen_per_min": 0.010, "comfort": 0.68, "occupied_by": None}},
        "tags": ["furniture", "stool", "rest"],
        "rows": _fit_rows([
            "................",
            "................",
            "................",
            "....oooooooo....",
            "...o22222222o...",
            "...o11111111o...",
            "...o33333333o...",
            "....oooooooo....",
            ".....o3o3o3o....",
            ".....o3o.o3o....",
            "....oaaaoaaao...",
            "....o3o...o3o...",
            "...o3o.....o3o..",
            "...ooo.....ooo..",
            "................",
            "................",
        ], 16, 16),
    },
    "nightstand": {
        "footprint": [1, 1],
        "default_name": "抽屉床头柜",
        "default_brief": "带黄铜小拉手的方正木头床头柜，台面正好放盏小夜灯。",
        "solid": True,
        "placement": "floor",
        "traits": {
            "surface": {"capacity": 2},
            "container": {"capacity": 3, "is_open": False, "is_locked": False},
        },
        "tags": ["furniture", "table", "surface"],
        "rows": _fit_rows([
            "................",
            "..oooooooooooo..",
            ".o222222222222o.",
            ".o211111111112o.",
            ".o333333333333o.",
            ".o1oooooooooo1o.",
            ".o1o111aa111o1o.",
            ".o1o33333333o1o.",
            ".o1oooooooooo1o.",
            ".o1o111aa111o1o.",
            ".o1o33333333o1o.",
            ".o1oooooooooo1o.",
            ".o3o........o3o.",
            ".ooo........ooo.",
            "................",
            "................",
        ], 16, 16),
    },
    "kitchen_stove": {
        "footprint": [2, 1],
        "default_name": "铸铁灶台",
        "default_brief": "炉膛正映着暖红火光的双灶厨房铁炉，锅盖边还冒着细气。",
        "solid": True,
        "placement": "floor",
        "traits": {
            "surface": {"capacity": 3},
            "thermal": {"cool_minutes": 180, "temp": "hot"},
            "switchable": {"is_on": True},
        },
        "tags": ["furniture", "stove", "kitchen", "light_source"],
        "accent_colors": {"a": "#ff8f3a"},
        "rows": _fit_rows([
            ".oooooooooooooooooooooooooooooo.",
            "o222222222222222222222222222222o",
            "o111o3333o1111111111o3333o11111o",
            "o111o3bb3o1111111111o3223o11111o",
            "o111o3333o1111111111o3333o11111o",
            "o333333333333333333333333333333o",
            "oooooooooooooooooooooooooooooooo",
            "o311oaaaaao111o22222o111o222o11o",
            "o311oa222ao111o11111o111o1a1o11o",
            "o311oaaaaao111o33333o111o333o11o",
            "o311o33333o111ooooooo111ooooo11o",
            "o311111111111111111111111111111o",
            "o333333333333333333333333333333o",
            ".oooooooooooooooooooooooooooooo.",
            "..ooo......................ooo..",
            "................................",
        ], 32, 16),
    },
    "wash_sink": {
        "footprint": [1, 1],
        "default_name": "陶瓷洗手槽",
        "default_brief": "接了弯铜水龙头的白釉陶瓷台盆，池底还留着水珠。",
        "solid": True,
        "placement": "floor",
        "traits": {"surface": {"capacity": 2}},
        "tags": ["furniture", "sink", "water"],
        "accent_colors": {"b": "#7fd4f0"},
        "rows": _fit_rows([
            ".......oo.......",
            "......oaa.......",
            "......o2o.......",
            ".oooooooooooooo.",
            ".o222222222222o.",
            ".o2obbbbbbbbo2o.",
            ".o2ob22bb22bo2o.",
            ".o2obbbbbbbbo2o.",
            ".o2oooooooooo2o.",
            ".o333333333333o.",
            "..o1111111111o..",
            "..o11o3333o11o..",
            "..o11o3333o11o..",
            "..o33oooooo33o..",
            "..oooooooooooo..",
            "................",
        ], 16, 16),
    },
    "coat_rack": {
        "footprint": [1, 1],
        "default_name": "实木衣帽架",
        "default_brief": "四杈立柱衣帽架，挂着大衣与一顶软呢礼帽。",
        "solid": True,
        "placement": "floor",
        "traits": {},
        "tags": ["furniture", "decor", "foyer"],
        "rows": _fit_rows([
            "......oooo......",
            ".....o2222o.....",
            "....oooooooo....",
            "...oao3113oao...",
            "....oo3113oo....",
            "....obbb11ao....",
            "....obbbb1ao....",
            "....obbbb1oo....",
            ".....o3113o.....",
            "......o11o......",
            "......o11o......",
            "......o11o......",
            ".....oo11oo.....",
            "...oo333333oo...",
            "...oooooooooo...",
            "................",
        ], 16, 16),
    },
    "crate_stack": {
        "footprint": [1, 1],
        "default_name": "叠放补给木箱",
        "default_brief": "钉着斜木筋与麻绳标签的大小货箱，堆在墙角方便随手取货。",
        "solid": True,
        "placement": "floor",
        "traits": {"container": {"capacity": 6, "is_open": False, "is_locked": False}},
        "tags": ["furniture", "storage", "crate"],
        "rows": _fit_rows([
            "......oooooooo..",
            ".....o22222222o.",
            ".....o11221111o.",
            ".....o12112111o.",
            ".....o33333333o.",
            ".ooooooo333333o.",
            "o2222222o3oooo..",
            "o1211112o.......",
            "o1121121o22222o.",
            "o1112211o1aa11o.",
            "o1121121o11111o.",
            "o1211112o33333o.",
            "o3333333o33333o.",
            "ooooooooooooooo.",
            "................",
            "................",
        ], 16, 16),
    },
    "wall_painting": {
        "footprint": [1, 1],
        "default_name": "镶框风景挂画",
        "default_brief": "画框雕着淡金纹理、描绘远山晨雾的油画挂画。",
        "solid": False,
        "placement": "wall",
        "traits": {},
        "tags": ["furniture", "wall_decor", "painting"],
        "rows": _fit_rows([
            "................",
            "..oooooooooooo..",
            ".oaaaaaaaaaaaao.",
            ".oa2222222222ao.",
            ".oa2bbb222bbb2o.",
            ".oa2bbbaaabbb2o.",
            ".oa2bb1111bbb2o.",
            ".oa2111331112ao.",
            ".oa2333333332ao.",
            ".oaaaaaaaaaaaao.",
            "..oooooooooooo..",
            "................",
            "................",
            "................",
            "................",
            "................",
        ], 16, 16),
    },
    "menu_chalkboard": {
        "footprint": [1, 1],
        "default_name": "手写黑板菜单",
        "default_brief": "木框小黑板上用粉笔工整写着今日招牌与时价。",
        "solid": False,
        "placement": "wall",
        "traits": {"readable": {"title": "今日看板", "content": "招牌特调与每日手工点心按时令供应，请到柜台前点单。"}},
        "tags": ["furniture", "wall_decor", "menu", "readable"],
        "rows": _fit_rows([
            "................",
            "..oooooooooooo..",
            ".o111111111111o.",
            ".o1o33333333o1o.",
            ".o1o32222a23o1o.",
            ".o1o33333333o1o.",
            ".o1o32222233o1o.",
            ".o1o3222b233o1o.",
            ".o1o33333333o1o.",
            ".o1o22a22222o1o.",
            ".o1o33333333o1o.",
            ".o3oooooooooo3o.",
            "..oooooooooooo..",
            "................",
            "................",
            "................",
        ], 16, 16),
    },
    "wall_sconce": {
        "footprint": [1, 1],
        "default_name": "黄铜壁灯",
        "default_brief": "装在墙面托架上的暖光磨砂玻璃壁灯，映出柔和晕光。",
        "solid": False,
        "placement": "wall",
        "traits": {"switchable": {"is_on": True}},
        "tags": ["furniture", "wall_decor", "lamp", "light_source"],
        "accent_colors": {"a": "#ffd166"},
        "rows": _fit_rows([
            "......oooo......",
            ".....o2aa2o.....",
            "....o2aaaa2o....",
            "....oaaaaaa2....",
            "....o2aaaa2o....",
            ".....oooooo.....",
            "......oaaao.....",
            ".....oo33oo.....",
            "....o333333o....",
            "....oooooooo....",
            "................",
            "................",
            "................",
            "................",
            "................",
            "................",
        ], 16, 16),
    },
    "wall_shelf": {
        "footprint": [1, 1],
        "default_name": "杂物挂墙层板",
        "default_brief": "钉在墙腰上的木质短搁板，摆着小陶罐与绿植枝条。",
        "solid": False,
        "placement": "wall",
        "traits": {},
        "tags": ["furniture", "wall_decor", "shelf"],
        "rows": _fit_rows([
            "................",
            "...ooo..oooo....",
            "..ob2booa22ao...",
            "..obbbooa22ao...",
            "...ooo..oooo....",
            ".oooooooooooooo.",
            ".o222222222222o.",
            ".o111111111111o.",
            ".o33o333333o33o.",
            "..oo........oo..",
            "................",
            "................",
            "................",
            "................",
            "................",
            "................",
        ], 16, 16),
    },
    "doormat": {
        "footprint": [1, 1],
        "default_name": "编织迎宾脚垫",
        "default_brief": "门口铺着暖色滚边的剑麻迎宾小地垫。",
        "solid": False,
        "placement": "floor",
        "traits": {},
        "tags": ["furniture", "decor", "rug", "doormat"],
        "rows": _fit_rows([
            "................",
            "................",
            "................",
            "..oooooooooooo..",
            ".o2aaaaaaaaaa2o.",
            ".o211111111112o.",
            ".o212211112212o.",
            ".o211111111112o.",
            ".o211111111112o.",
            ".o212211112212o.",
            ".o211111111112o.",
            ".o2aaaaaaaaaa2o.",
            "..oooooooooooo..",
            "................",
            "................",
            "................",
        ], 16, 16),
    },
    "book_stack": {
        "footprint": [1, 1],
        "default_name": "绑绳精装书叠",
        "default_brief": "两三册翻开折角夹着书签的布面精装书叠。",
        "solid": False,
        "placement": "surface_only",
        "traits": {"portable": {"weight": "light"}},
        "tags": ["decor", "book", "clutter"],
        "rows": _fit_rows([
            "................",
            "................",
            "....oooooooo....",
            "...obbbbbbbbo...",
            "...ob222222bo...",
            "...oooooooooo...",
            "..ooooooooooo...",
            "..oaaaaaaaaaao..",
            "..oa22222222ao..",
            "..oooooooooooo..",
            "..o1111111111o..",
            "..o1222222221o..",
            "..o3333333333o..",
            "..oooooooooooo..",
            "................",
            "................",
        ], 16, 16),
    },
    "tea_set": {
        "footprint": [1, 1],
        "default_name": "粗陶茶具托盘",
        "default_brief": "木盘里摆着一把矮肚陶壶和两只温热茶杯。",
        "solid": False,
        "placement": "surface_only",
        "traits": {"portable": {"weight": "light"}},
        "tags": ["decor", "tea", "clutter"],
        "rows": _fit_rows([
            "................",
            "................",
            ".....oooo.......",
            "....o2bb2o......",
            "...obbbbbbo.....",
            "..o1obbbbob2o...",
            "...o1obbbo......",
            ".........oooo...",
            "........ob22bo..",
            "..oooooooooooo..",
            ".o222222222222o.",
            ".o111111111111o.",
            ".o333333333333o.",
            "..oooooooooooo..",
            "................",
            "................",
        ], 16, 16),
    },
}


# ---------------------------------------------------------------------------
# 主题调色板（按语义匹配）
# ---------------------------------------------------------------------------

_MOTIF_PALETTES: dict[str, dict[str, str]] = {
    "onsen": {"o": "#221b17", "1": "#b88c58", "2": "#e8dcc2", "3": "#694b2e", "a": "#6ca676", "b": "#8ed1c2"},
    "tea": {"o": "#241d19", "1": "#b8905c", "2": "#e6d8b8", "3": "#6c4e31", "a": "#73a86c", "b": "#9ad1be"},
    "alchemy": {"o": "#1b1424", "1": "#7d513e", "2": "#d1b392", "3": "#472c22", "a": "#f59e42", "b": "#69e3c6"},
    "music": {"o": "#1e161d", "1": "#6b3733", "2": "#f2dec4", "3": "#3d1e1c", "a": "#e0b65c", "b": "#d8e6f2"},
    "clockwork": {"o": "#1a191c", "1": "#9c7040", "2": "#dfc298", "3": "#4a3828", "a": "#f2b830", "b": "#7ae7ff"},
    "archive": {"o": "#201916", "1": "#8f5f3c", "2": "#e4cfa8", "3": "#523522", "a": "#d99241", "b": "#8bb8cc"},
    "medical": {"o": "#1f262a", "1": "#a8b8c0", "2": "#edf4f7", "3": "#5c707a", "a": "#e06d6d", "b": "#7ed6df"},
    "inn": {"o": "#231915", "1": "#b87a4e", "2": "#f0d3af", "3": "#6e4226", "a": "#f5b856", "b": "#9ac4d6"},
    "cafe": {"o": "#241a16", "1": "#c48454", "2": "#f6dfc2", "3": "#744429", "a": "#f2a65a", "b": "#8dc6cf"},
    "shop": {"o": "#221b16", "1": "#b57c4a", "2": "#f2dec4", "3": "#694326", "a": "#e89f4c", "b": "#7cc6d6"},
    "arcade": {"o": "#171624", "1": "#4c5270", "2": "#cad4f2", "3": "#292d42", "a": "#ff66c4", "b": "#5ce1e6"},
    "harbor": {"o": "#182128", "1": "#7d8b8f", "2": "#dae3e6", "3": "#3f4d52", "a": "#e3a64b", "b": "#64b6db"},
    "observatory": {"o": "#161724", "1": "#556180", "2": "#cad6f0", "3": "#2d344d", "a": "#f0c954", "b": "#7dc4ff"},
    "workshop": {"o": "#211914", "1": "#ad6e42", "2": "#e6be94", "3": "#5e3820", "a": "#eb8b38", "b": "#9bb5c2"},
    "manor": {"o": "#21161b", "1": "#9e4d47", "2": "#edd4c2", "3": "#592826", "a": "#e3b754", "b": "#9abed9"},
    "default": dict(DEFAULT_SLOT_COLORS),
}

_THEME_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("onsen", ("温泉", "汤池", "浴场", "汤屋", "澡堂", "汤宿", "onsen", "bathhouse", "hot_spring")),
    ("tea", ("茶室", "茶屋", "和室", "茶馆", "禅", "tea", "tatami", "zen")),
    ("alchemy", ("炼金", "药剂", "魔药", "炼丹", "魔导", "秘术", "标本", "蒸馏", "alchem", "potion", "arcane")),
    ("music", ("音乐", "钢琴", "沙龙", "琴房", "演奏", "歌剧", "乐团", "music", "piano", "salon", "concert")),
    ("clockwork", ("钟楼", "齿轮", "机房", "蒸汽", "发条", "机械", "仪表", "锅炉", "clock", "gear", "steam")),
    ("observatory", ("天文", "观星", "星象", "占星", "望远", "observatory", "astrolabe")),
    ("medical", ("医务", "诊所", "手术", "病房", "药房", "解剖", "clinic", "hospital", "medical")),
    ("archive", ("书房", "书斋", "书店", "书屋", "书局", "图书", "文库", "档案", "资料", "密室", "古籍", "博物", "展厅", "画廊", "画室", "bookstore", "bookshop", "library", "archive", "study", "museum")),
    ("workshop", ("工坊", "锻造", "木工", "铁匠", "作坊", "修理", "repair", "workshop", "forge", "studio")),
    ("arcade", ("电玩", "街机", "游戏", "游艺", "网吧", "终端", "arcade", "gaming")),
    ("shop", ("杂货", "便利店", "商店", "商铺", "百货", "道具屋", "集市", "货铺", "shop", "store", "mart")),
    ("harbor", ("码头", "船长", "港口", "灯塔", "航海", "海边", "渔村", "harbor", "pier", "ship", "port")),
    ("cafe", ("咖啡", "甜点", "餐馆", "食堂", "面包", "酒馆", "吧台", "厨房", "茶歇", "cafe", "bakery", "tavern", "diner", "bar")),
    ("manor", ("洋馆", "公馆", "庄园", "大厅", "会客", "客厅", "走廊", "玄关", "manor", "lobby", "parlor")),
    ("inn", ("宿", "卧室", "起居", "客栈", "旅店", "寝室", "公寓", "客房", "家", "小屋", "home", "bedroom", "inn", "dorm")),
]


def infer_theme_key(text: str, palette_id: str = "") -> str:
    lowered = str(text or "").lower()
    for key, kws in _THEME_KEYWORDS:
        if any(k in lowered for k in kws):
            return key
    pal = str(palette_id or "").lower()
    if "moss" in pal or "tea" in pal:
        return "tea"
    if "dusk" in pal:
        return "manor"
    return "default"


def _match_motif_for_custom(item: Mapping[str, Any], theme_key: str) -> str:
    raw_motif = str(item.get("motif") or "").strip().lower()
    if raw_motif in FURNITURE_MOTIFS:
        return raw_motif
    blob = f"{item.get('name', '')} {item.get('brief', '')} {item.get('kind', '')}".lower()
    checks: list[tuple[str, tuple[str, ...]]] = [
        ("interior_door", ("内室门", "室内门", "房门", "隔门", "interior_door")),
        ("basement_hatch", ("活板门", "地窖门", "地窖口", "hatch")),
        ("staircase_up", ("上行楼梯", "上楼", "楼梯上", "stair_up", "staircase_up")),
        ("staircase_down", ("下行楼梯", "下楼", "楼梯下", "stair_down", "staircase_down")),
        ("kitchen_stove", ("灶台", "炉灶", "煤气灶", "铁炉", "kitchen_stove", "stove")),
        ("wash_sink", ("水槽", "洗手池", "洗手台", "洗菜池", "wash_sink", "sink")),
        ("nightstand", ("床头柜", "小边柜", "nightstand")),
        ("coat_rack", ("衣帽架", "挂衣架", "coat_rack")),
        ("crate_stack", ("木箱", "补给箱", "货箱", "crate")),
        ("menu_chalkboard", ("黑板", "菜单", "价目", "menu", "chalkboard")),
        ("wall_sconce", ("壁灯", "墙灯", "sconce")),
        ("wall_painting", ("挂画", "油画", "风景画", "painting")),
        ("wall_shelf", ("搁板", "壁架", "层板", "wall_shelf")),
        ("doormat", ("脚垫", "门垫", "地垫", "doormat")),
        ("book_stack", ("书堆", "书叠", "精装书", "book_stack")),
        ("tea_set", ("茶具", "茶壶托盘", "tea_set")),
        ("bar_stool", ("吧凳", "高脚凳", "stool")),
        ("wood_chair", ("木椅", "餐椅", "靠背椅", "chair")),
        ("fireplace", ("壁炉", "炉火", "火塘", "fireplace", "hearth")),
        ("grand_piano", ("钢琴", "琴", "piano")),
        ("hot_spring_tub", ("汤池", "温泉", "浴", "bath", "tub")),
        ("tea_hearth", ("茶席", "地炉", "茶桌", "tea")),
        ("folding_screen", ("屏风", "隔扇", "screen")),
        ("lab_bench", ("实验", "蒸馏", "药台", "炼金台", "bench", "lab")),
        ("specimen_rack", ("标本", "药柜", "试剂", "药架", "specimen", "apothecary")),
        ("gear_console", ("齿轮", "控制台", "仪表", "机器", "发条", "console", "gear")),
        ("bar_counter", ("吧台", "柜台", "前台", "长台", "counter", "bar")),
        ("display_case", ("展柜", "陈列", "标本柜", "玻璃柜", "display", "case")),
        ("altar_pedestal", ("祭坛", "石台", "星象台", "神龛", "altar", "pedestal")),
        ("long_table", ("长桌", "会议", "餐桌", "议事", "长案", "table")),
        ("sofa_plush", ("沙发", "软座", "卡座", "sofa", "couch")),
        ("wardrobe", ("衣橱", "衣柜", "储物柜", "wardrobe", "closet")),
        ("workbench", ("工台", "木工", "铁砧", "修理台", "workbench")),
        ("standing_mirror", ("镜", "mirror")),
        ("statue_bust", ("雕像", "胸像", "图腾", "statue", "bust")),
        ("globe_stand", ("地球仪", "天球", "浑天仪", "globe")),
        ("candelabra", ("烛台", "蜡烛", "油灯", "枝形灯", "candelabra", "candle")),
        ("flower_vase", ("花瓶", "插花", "盆景", "花座", "vase")),
    ]
    for m_key, kws in checks:
        if any(k in blob for k in kws):
            return m_key
    fallback_by_theme = {
        "tea": "tea_hearth",
        "alchemy": "lab_bench",
        "music": "grand_piano",
        "clockwork": "gear_console",
        "archive": "display_case",
        "medical": "lab_bench",
        "cafe": "bar_counter",
        "manor": "fireplace",
        "observatory": "globe_stand",
        "workshop": "workbench",
    }
    return fallback_by_theme.get(theme_key, "candelabra")


def synthesize_furniture_blueprint(
    spec: Mapping[str, Any],
    *,
    palette_id: str = "warm_stardew",
    theme_key: str = "",
) -> dict[str, Any]:
    """根据母版键或自定义设计草图合成可直接写入引擎的完整多格像素蓝图。"""
    resolved_theme = theme_key or infer_theme_key(
        f"{spec.get('name', '')} {spec.get('brief', '')}", palette_id
    )
    motif_id = _match_motif_for_custom(spec, resolved_theme)
    tpl = FURNITURE_MOTIFS[motif_id]

    default_fp = list(tpl["footprint"])
    raw_fp = spec.get("footprint")
    if isinstance(raw_fp, (list, tuple)) and len(raw_fp) == 2:
        fw = clamp_int(raw_fp[0], 1, 2, default_fp[0])
        fh = clamp_int(raw_fp[1], 1, 2, default_fp[1])
    else:
        fw, fh = default_fp[0], default_fp[1]

    # 如果自定义了 footprint 与母版一致则复用原高精像素；否则自适应裁剪/缩放至目标像素尺寸
    px_w = fw * 16
    px_h = fh * 16
    custom_pixels = spec.get("pixels")
    frames_out: dict[str, list[str]]
    if isinstance(custom_pixels, (list, tuple)) and len(custom_pixels) >= 8:
        frames_out = {"default": normalize_pixel_grid(custom_pixels, px_w, px_h, align_bottom=True)}
    elif isinstance(tpl.get("frames"), Mapping) and tpl["frames"]:
        frames_out = {}
        for fname, frows in tpl["frames"].items():
            if [fw, fh] == default_fp:
                frames_out[str(fname)] = list(frows)
            else:
                frames_out[str(fname)] = normalize_pixel_grid(frows, px_w, px_h, align_bottom=True)
        if "default" not in frames_out:
            first_frame = frames_out.get("closed") or next(iter(frames_out.values()))
            frames_out["default"] = list(first_frame)
    elif [fw, fh] == default_fp:
        frames_out = {"default": list(tpl["rows"])}
    else:
        frames_out = {"default": normalize_pixel_grid(tpl["rows"], px_w, px_h, align_bottom=True)}

    colors = dict(_MOTIF_PALETTES.get(resolved_theme, _MOTIF_PALETTES["default"]))
    for k, v in (tpl.get("accent_colors") or {}).items():
        colors[k] = v
    raw_colors = spec.get("slot_colors")
    if isinstance(raw_colors, Mapping):
        for slot in ("o", "1", "2", "3", "a", "b"):
            val = str(raw_colors.get(slot) or "").strip()
            if re.fullmatch(r"#[0-9a-fA-F]{6}", val):
                colors[slot] = val

    traits = copy.deepcopy(tpl.get("traits") or {})
    extra_traits = spec.get("traits")
    if isinstance(extra_traits, Mapping):
        for tk, tv in extra_traits.items():
            if isinstance(tv, Mapping) and tk in (
                "surface", "container", "switchable", "restable",
                "activity", "inscribable", "thermal", "portal", "portable", "readable",
            ):
                traits[tk] = dict(tv)

    name = clip_text(spec.get("name"), 20, str(tpl["default_name"]))
    brief = clip_text(spec.get("brief"), 96, str(tpl["default_brief"]))
    solid = bool(spec["solid"]) if "solid" in spec else bool(tpl.get("solid", True))
    placement = str(spec.get("placement") or tpl.get("placement") or "floor")
    if placement not in ("floor", "wall", "surface_only", "any"):
        placement = "floor"

    return {
        "name": name,
        "brief": brief,
        "detail": clip_text(spec.get("detail"), 240, brief),
        "tags": list(spec.get("tags") or tpl.get("tags") or ["furniture"]),
        "placement": placement,
        "solid": solid,
        "footprint": [fw, fh],
        "traits": traits,
        "effects": copy.deepcopy(spec.get("effects") or {}),
        "sprite": {
            "mode": "pixel_grid",
            "w": px_w,
            "h": px_h,
            "frames": frames_out,
            "slot_colors": colors,
        },
    }


# ---------------------------------------------------------------------------
# Holodeck Scene Graph 规划与批处理 LLM 设计
# ---------------------------------------------------------------------------

HERO_LANDMARK_MOTIFS: frozenset[str] = frozenset({
    "hot_spring_tub",
    "altar_pedestal",
    "grand_piano",
    "fireplace",
    "gear_console",
})

_THEME_FURNITURE_PACKS: dict[str, list[dict[str, Any]]] = {
    "onsen": [
        {"ref": "tub", "motif": "hot_spring_tub", "name": "桧木温汤池", "brief": "木边光滑、池面飘着温热白雾的室内汤池。", "relation": "in_corner"},
        {"ref": "screen", "motif": "folding_screen", "name": "松竹折屏", "brief": "绘有淡墨松影的细格木屏风，隔出更衣角落。", "relation": "against_wall"},
        {"ref": "hearth", "motif": "tea_hearth", "name": "汤后歇脚茶席", "brief": "摆着粗陶茶壶与竹托盘的矮木茶案。", "relation": "centerpiece"},
        {"ref": "wardrobe", "motif": "wardrobe", "name": "浴衣收纳藤柜", "brief": "叠放着干净棉麻浴衣与浴巾的木柜。", "relation": "against_wall"},
        {"ref": "cup", "blueprint_id": "default_hot_drink", "name": "温热玄米茶", "brief": "泡汤后解渴暖身的粗陶茶碗。", "relation": "on_surface", "anchor_ref": "hearth"},
        {"ref": "candle", "motif": "candelabra", "name": "纸罩行灯", "brief": "火光柔和、防风木架护着的和式落地灯。", "relation": "in_corner"},
    ],
    "tea": [
        {"ref": "hearth", "motif": "tea_hearth", "name": "围炉茶席", "brief": "中央吊着老铁壶、四周铺开茶垫的矮茶案。", "relation": "centerpiece"},
        {"ref": "screen", "motif": "folding_screen", "name": "水墨折屏", "brief": "绘有远山竹影的木框屏风。", "relation": "against_wall"},
        {"ref": "vase", "motif": "flower_vase", "name": "青瓷插花座", "brief": "供着早梅细枝的青釉瓷瓶。", "relation": "near", "anchor_ref": "hearth"},
        {"ref": "cup", "blueprint_id": "default_hot_drink", "name": "煎茶茶碗", "brief": "刚注满热煎茶、泛着清香的粗陶茶碗。", "relation": "on_surface", "anchor_ref": "hearth"},
        {"ref": "snack", "blueprint_id": "default_snack", "name": "和果子木碟", "brief": "配茶享用的小巧季节和果子。", "relation": "on_surface", "anchor_ref": "hearth"},
        {"ref": "cushion1", "blueprint_id": "default_cat_cushion", "name": "软绒榻榻米垫", "brief": "摆在茶席一侧的厚软跪坐垫。", "relation": "near", "anchor_ref": "hearth"},
        {"ref": "candle", "motif": "candelabra", "name": "纸罩行灯", "brief": "火光柔和、防风木架护着的和式落地灯。", "relation": "in_corner"},
    ],
    "alchemy": [
        {"ref": "bench", "motif": "lab_bench", "name": "蒸馏长实验台", "brief": "架着蛇形冷凝管与三连烧瓶的暗木长台。", "relation": "centerpiece"},
        {"ref": "rack", "motif": "specimen_rack", "name": "浸液标本柜", "brief": "整整齐齐排满贴有拉丁标签药瓶的窄木柜。", "relation": "against_wall"},
        {"ref": "altar", "motif": "altar_pedestal", "name": "秘仪刻阵石台", "brief": "台面嵌有金属星环、用于定量称重的玄石座。", "relation": "against_wall"},
        {"ref": "shelf", "blueprint_id": "default_bookshelf", "name": "手稿秘典架", "brief": "塞满羊皮药典与铅封笔记的深色高书架。", "relation": "against_wall"},
        {"ref": "candle", "motif": "candelabra", "name": "三枝铜烛台", "brief": "蜡油沿铜臂垂落、仍亮着微光的实验台烛台。", "relation": "near", "anchor_ref": "bench"},
        {"ref": "note", "blueprint_id": "default_notebook", "name": "滴定实验簿", "brief": "纸页微焦、密密麻麻写满试剂配比的笔记簿。", "relation": "on_surface", "anchor_ref": "bench"},
        {"ref": "pot", "blueprint_id": "default_plant", "name": "夜光草盆栽", "brief": "土壤潮湿、叶背泛淡银脉络的药用植物。", "relation": "in_corner"},
    ],
    "music": [
        {"ref": "piano", "motif": "grand_piano", "name": "漆面三角钢琴", "brief": "谱架还摊着未合曲谱的黑色大三角钢琴。", "relation": "centerpiece"},
        {"ref": "sofa", "motif": "sofa_plush", "name": "丝绒听音长椅", "brief": "弧背软垫、专供客人细听演奏的长沙发。", "relation": "against_wall"},
        {"ref": "mirror", "motif": "standing_mirror", "name": "鎏金立式仪容镜", "brief": "沙龙一角映着柔黄灯光的雕框穿衣镜。", "relation": "in_corner"},
        {"ref": "gram", "blueprint_id": "default_gramophone", "name": "黄铜大喇叭留声机", "brief": "唱片盘上还放着黑胶、铜喇叭擦得雪亮的唱机。", "relation": "near", "anchor_ref": "sofa"},
        {"ref": "candle", "motif": "candelabra", "name": "高脚铜烛台", "brief": "照亮琴谱与弦键的高身多头烛台。", "relation": "near", "anchor_ref": "piano"},
        {"ref": "vase", "motif": "flower_vase", "name": "金边花瓶座", "brief": "摆在沙发旁烘托气息的白瓷花瓶。", "relation": "near", "anchor_ref": "sofa"},
        {"ref": "table", "blueprint_id": "default_desk", "name": "乐谱整理案", "brief": "堆着乐章手稿与节拍器的木桌。", "relation": "against_wall"},
    ],
    "clockwork": [
        {"ref": "console", "motif": "gear_console", "name": "主钟齿轮控制台", "brief": "金属仪表指针轻微抖动、大小铜轮连动的心脏台。", "relation": "centerpiece"},
        {"ref": "bench", "motif": "workbench", "name": "发条校正工匠台", "brief": "夹着精密镊子与表油罐的重型修表工台。", "relation": "against_wall"},
        {"ref": "globe", "motif": "globe_stand", "name": "黄铜浑天星仪", "brief": "两道铜环套嵌、标注星轨度数的天文仪。", "relation": "in_corner"},
        {"ref": "display", "motif": "display_case", "name": "精密摆轮展柜", "brief": "防尘玻璃罩里陈列着备用擒纵叉与发条盒。", "relation": "against_wall"},
        {"ref": "lamp", "blueprint_id": "default_lamp", "name": "工匠万向台灯", "brief": "长臂可掰动、专照细小齿轮的暖光台灯。", "relation": "on_surface", "anchor_ref": "bench"},
        {"ref": "clock", "blueprint_id": "default_wall_clock", "name": "母钟校准挂表", "brief": "走时声清脆分明、与主轴同步的机械壁钟。", "relation": "wall_hang"},
    ],
    "observatory": [
        {"ref": "altar", "motif": "altar_pedestal", "name": "星象观测石盘", "brief": "刻满黄道刻度、中心可架六分仪的石座。", "relation": "centerpiece"},
        {"ref": "globe", "motif": "globe_stand", "name": "古老天球仪", "brief": "绘制着褪色星座线与银箔星点的黄铜天球。", "relation": "near", "anchor_ref": "altar"},
        {"ref": "desk", "blueprint_id": "default_desk", "name": "星历记录书桌", "brief": "铺满天文计算纸与角尺的宽工作桌。", "relation": "against_wall"},
        {"ref": "shelf", "blueprint_id": "default_bookshelf", "name": "星图卷宗柜", "brief": "插满星图圆筒与天文年鉴的靠墙高架。", "relation": "against_wall"},
        {"ref": "candle", "motif": "candelabra", "name": "铜制防风烛台", "brief": "在昏暗观星室里提供低调照明的烛架。", "relation": "in_corner"},
        {"ref": "note", "blueprint_id": "default_notebook", "name": "夜空观测日记", "brief": "记录每晚流星方位角与云量的观测本。", "relation": "on_surface", "anchor_ref": "desk"},
    ],
    "medical": [
        {"ref": "bench", "motif": "lab_bench", "name": "药剂调配操作台", "brief": "台面擦得一尘不染、摆着研钵与玻璃量杯的长台。", "relation": "against_wall"},
        {"ref": "rack", "motif": "specimen_rack", "name": "分类西药柜", "brief": "格子上印有剂量标号、锁着玻璃药瓶的立柜。", "relation": "against_wall"},
        {"ref": "screen", "motif": "folding_screen", "name": "白布诊断屏风", "brief": "挡在看诊床前的干净折叠屏风。", "relation": "centerpiece"},
        {"ref": "desk", "blueprint_id": "default_desk", "name": "接诊病历桌", "brief": "留着钢笔墨水瓶与门诊记录夹的白木桌。", "relation": "against_wall"},
        {"ref": "lamp", "blueprint_id": "default_lamp", "name": "诊断立灯", "brief": "聚光柔和、方便查看瓶签与伤口的小立灯。", "relation": "on_surface", "anchor_ref": "desk"},
        {"ref": "fridge", "blueprint_id": "default_fridge", "name": "低温药剂冷藏柜", "brief": "柜门密封严实、微微发出低沉嗡鸣的小冷藏箱。", "relation": "in_corner"},
    ],
    "archive": [
        {"ref": "long_t", "motif": "long_table", "name": "典籍研阅长案", "brief": "足够摊开大幅手卷与多册厚典的长条阅览桌。", "relation": "centerpiece"},
        {"ref": "display", "motif": "display_case", "name": "孤本玻璃展柜", "brief": "用天鹅绒衬着几件泛黄封蜡文书的展柜。", "relation": "against_wall"},
        {"ref": "shelf1", "blueprint_id": "default_bookshelf", "name": "编目高书架", "brief": "贴着年份分类牌、排满旧籍与案卷的木书架。", "relation": "against_wall"},
        {"ref": "bust", "motif": "statue_bust", "name": "先贤石刻像", "brief": "立于入口侧畔、底座铭刻着落成年份的石像。", "relation": "in_corner"},
        {"ref": "lamp", "blueprint_id": "default_lamp", "name": "绿色玻璃罩台灯", "brief": "亮着温暖聚光的经典老式阅览台灯。", "relation": "on_surface", "anchor_ref": "long_t"},
        {"ref": "globe", "motif": "globe_stand", "name": "旧大陆舆图仪", "brief": "漆面微裂却仍能流畅转动的古舆图仪。", "relation": "in_corner"},
        {"ref": "note", "blueprint_id": "default_notebook", "name": "借阅登记薄", "brief": "夹着书签的厚皮登记册。", "relation": "on_surface", "anchor_ref": "long_t"},
    ],
    "workshop": [
        {"ref": "bench", "motif": "workbench", "name": "重型木工台", "brief": "台角固定着铁制虎钳、落着细碎木屑的厚实工作台。", "relation": "centerpiece"},
        {"ref": "rack", "motif": "specimen_rack", "name": "五金零件抽屉柜", "brief": "贴着螺母与木钉标签的小格木架。", "relation": "against_wall"},
        {"ref": "console", "motif": "gear_console", "name": "传动检修机台", "brief": "嵌着校准齿轮组与摇把的机械台。", "relation": "against_wall"},
        {"ref": "chest", "blueprint_id": "default_box", "name": "工具储物箱", "brief": "装着锤凿锯刨的大号木制工具箱。", "relation": "in_corner"},
        {"ref": "lamp", "blueprint_id": "default_lamp", "name": "便携检修提灯", "brief": "挂着金属提手的工作照明灯。", "relation": "on_surface", "anchor_ref": "bench"},
    ],
    "harbor": [
        {"ref": "table", "motif": "long_table", "name": "海图航线长桌", "brief": "铺着大幅海洋等深线海图与铜镇纸的厚桌。", "relation": "centerpiece"},
        {"ref": "globe", "motif": "globe_stand", "name": "航海地球仪", "brief": "标着群岛航道与季风箭头的古铜地球仪。", "relation": "near", "anchor_ref": "table"},
        {"ref": "chest", "blueprint_id": "default_box", "name": "铁箍航海木箱", "brief": "防潮浸蜡、挂着铜扣的船用大行李箱。", "relation": "in_corner"},
        {"ref": "bar", "motif": "bar_counter", "name": "船舱吧柜", "brief": "嵌有防滑木框缘、防风浪滑落的酒水木吧。", "relation": "against_wall"},
        {"ref": "candle", "motif": "candelabra", "name": "防风铜烛台", "brief": "底座加重的老式航海铜烛台。", "relation": "on_surface", "anchor_ref": "table"},
    ],
    "cafe": [
        {"ref": "bar", "motif": "bar_counter", "name": "原木点单吧台", "brief": "飘出现磨咖啡豆与烘焙香气的长条点单吧台。", "relation": "against_wall"},
        {"ref": "coffee", "blueprint_id": "default_coffee_maker", "name": "意式咖啡机", "brief": "气压表指在绿区、热汽管随时待命的咖啡机。", "relation": "on_surface", "anchor_ref": "bar"},
        {"ref": "cup", "blueprint_id": "default_hot_drink", "name": "现煮热拿铁", "brief": "表面拉着树叶奶泡、温热烫手的白瓷杯。", "relation": "on_surface", "anchor_ref": "bar"},
        {"ref": "cake", "blueprint_id": "default_cake", "name": "草莓奶油蛋糕", "brief": "玻璃垫上刚切好的一角奶油夹心蛋糕。", "relation": "on_surface", "anchor_ref": "bar"},
        {"ref": "snack", "blueprint_id": "default_snack", "name": "黄油曲奇碟", "brief": "酥松微甜的下午茶小点心。", "relation": "on_surface", "anchor_ref": "desk"},
        {"ref": "fridge", "blueprint_id": "default_fridge", "name": "冷饮甜品柜", "brief": "透过玻璃门能看见牛乳瓶与布丁的冷柜。", "relation": "in_corner"},
        {"ref": "sofa", "motif": "sofa_plush", "name": "靠窗卡座沙发", "brief": "暖布垫包裹的舒适小憩卡座。", "relation": "against_wall"},
        {"ref": "desk", "blueprint_id": "default_desk", "name": "双人橡木圆方桌", "brief": "摆在卡座旁铺着亚麻杯垫的客用茶桌。", "relation": "near", "anchor_ref": "sofa"},
        {"ref": "lamp", "blueprint_id": "default_lamp", "name": "暖黄台灯", "brief": "让桌面点心镀上金边的玻璃圆灯。", "relation": "on_surface", "anchor_ref": "desk"},
    ],
    "shop": [
        {"ref": "counter", "motif": "bar_counter", "name": "收银结账柜台", "brief": "摆着铜铃、零钱托盘与包装纸的木制收银台。", "relation": "against_wall"},
        {"ref": "display", "motif": "display_case", "name": "精品玻璃陈列柜", "brief": "擦得透亮、展示着镇店杂货与纪念小物的玻璃柜。", "relation": "centerpiece"},
        {"ref": "rack", "motif": "specimen_rack", "name": "多格百货木架", "brief": "分门别类码放着日用百货与罐头瓶罐的高木架。", "relation": "against_wall"},
        {"ref": "snack", "blueprint_id": "default_snack", "name": "零售点心篮", "brief": "装着独立包装饼干与糖果的藤编小篮。", "relation": "on_surface", "anchor_ref": "counter"},
        {"ref": "drink", "blueprint_id": "default_hot_drink", "name": "保温热饮壶", "brief": "供进店客人顺手买一杯暖手的便携热饮。", "relation": "on_surface", "anchor_ref": "counter"},
        {"ref": "box1", "blueprint_id": "default_box", "name": "未拆封货箱", "brief": "刚进货堆在墙角、印着产地戳记的木箱。", "relation": "in_corner"},
        {"ref": "chest", "blueprint_id": "default_wooden_chest", "name": "库存储物柜", "brief": "收纳备用货品与账册的厚木箱。", "relation": "in_corner"},
        {"ref": "lamp", "blueprint_id": "default_lamp", "name": "柜台小台灯", "brief": "照亮价目牌与账本的暖光台灯。", "relation": "on_surface", "anchor_ref": "counter"},
    ],
    "arcade": [
        {"ref": "arc1", "blueprint_id": "default_arcade", "name": "复古格斗街机", "brief": "摇杆清脆、屏幕闪烁着像素火花的立式街机。", "relation": "against_wall"},
        {"ref": "arc2", "blueprint_id": "default_arcade", "name": "音乐节奏机台", "brief": "彩色按键灯随节拍律动的热门街机。", "relation": "against_wall"},
        {"ref": "pc", "blueprint_id": "default_pc", "name": "联机对战终端", "brief": "配着机械键盘与高刷屏幕的联机电脑桌。", "relation": "against_wall"},
        {"ref": "bar", "motif": "bar_counter", "name": "兑币与冷饮吧台", "brief": "可兑换游戏代币并售卖冰镇汽水的服务台。", "relation": "centerpiece"},
        {"ref": "handheld", "blueprint_id": "default_handheld", "name": "试玩掌机", "brief": "连着防盗绳供客人体验新卡带的掌机。", "relation": "on_surface", "anchor_ref": "bar"},
        {"ref": "sofa", "motif": "sofa_plush", "name": "观战软皮沙发", "brief": "供等位玩家歇脚聊天的宽大软沙发。", "relation": "in_corner"},
        {"ref": "fridge", "blueprint_id": "default_fridge", "name": "冰镇汽水柜", "brief": "塞满各色气泡饮料的玻璃门冷柜。", "relation": "in_corner"},
        {"ref": "poster", "blueprint_id": "default_window_poster", "name": "像素大赛海报", "brief": "印着高分榜与复古像素插画的宣传画。", "relation": "wall_hang"},
    ],
    "manor": [
        {"ref": "hearth", "motif": "fireplace", "name": "主厅大理石壁炉", "brief": "壁炉架陈着铜钟与瓷盘、炉火劈啪作响。", "relation": "against_wall"},
        {"ref": "sofa", "motif": "sofa_plush", "name": "雕木丝绒长沙发", "brief": "正对壁炉摆放、供访客交谈的华贵沙发。", "relation": "near", "anchor_ref": "hearth"},
        {"ref": "long_t", "motif": "long_table", "name": "宴会议事长桌", "brief": "铺着流苏桌旗的橡木主桌。", "relation": "centerpiece"},
        {"ref": "bust", "motif": "statue_bust", "name": "庄园家主石像", "brief": "雕工细腻、注视着整座厅堂的大理石胸像。", "relation": "in_corner"},
        {"ref": "candle", "motif": "candelabra", "name": "立式铜烛台", "brief": "照亮廊柱与阶影的多头铜烛台。", "relation": "near", "anchor_ref": "hearth"},
        {"ref": "shelf", "blueprint_id": "default_bookshelf", "name": "红木藏书架", "brief": "列着装帧精美的全套年鉴与家族日记。", "relation": "against_wall"},
        {"ref": "clock", "blueprint_id": "default_wall_clock", "name": "报时摆钟", "brief": "挂在厅堂高墙上的精致机械壁钟。", "relation": "wall_hang"},
    ],
    "inn": [
        {"ref": "wardrobe", "motif": "wardrobe", "name": "双门旅人衣橱", "brief": "漆面整洁、里头挂着备用毛巾与衣架的木橱。", "relation": "against_wall"},
        {"ref": "desk", "blueprint_id": "default_desk", "name": "木质写字台", "brief": "放着灯盏与随记纸的靠墙小桌。", "relation": "against_wall"},
        {"ref": "mirror", "motif": "standing_mirror", "name": "半身高木框镜", "brief": "擦拭光亮、方便整装的立式镜。", "relation": "in_corner"},
        {"ref": "lamp", "blueprint_id": "default_lamp", "name": "床头暖灯", "brief": "适合夜读的小圆罩暖黄台灯。", "relation": "on_surface", "anchor_ref": "desk"},
        {"ref": "cushion", "blueprint_id": "default_cat_cushion", "name": "绒面地垫", "brief": "铺在窗边角落里的懒散软垫。", "relation": "in_corner"},
        {"ref": "vase", "motif": "flower_vase", "name": "窗边小花瓶", "brief": "插着两枝干花的瓷瓶。", "relation": "on_surface", "anchor_ref": "desk"},
    ],
    "default": [
        {"ref": "desk", "blueprint_id": "default_desk", "name": "实木方桌", "brief": "木纹清晰、适合放杂物与笔记的旧木桌。", "relation": "centerpiece"},
        {"ref": "shelf", "blueprint_id": "default_bookshelf", "name": "立式木书架", "brief": "堆着零散册子与器物的小木架。", "relation": "against_wall"},
        {"ref": "box", "blueprint_id": "default_box", "name": "木制收纳箱", "brief": "带暗锁扣的常见储物木箱。", "relation": "in_corner"},
        {"ref": "lamp", "blueprint_id": "default_lamp", "name": "暖黄台灯", "brief": "散发安心暖光的台灯。", "relation": "on_surface", "anchor_ref": "desk"},
        {"ref": "vase", "motif": "flower_vase", "name": "青瓷插花瓶", "brief": "给室内添了一点亮色的瓷瓶。", "relation": "in_corner"},
    ],
}

_PARTITION_POOL_BY_THEME: dict[str, tuple[str, ...]] = {
    "onsen": ("l_alcove", "center_screen", "corner_nook"),
    "tea": ("l_alcove", "center_screen", "corner_nook", "none"),
    "alchemy": ("pillar_colonnade", "wing_split", "l_alcove", "none"),
    "music": ("pillar_colonnade", "corner_nook", "none", "center_screen"),
    "clockwork": ("wing_split", "pillar_colonnade", "l_alcove", "none"),
    "observatory": ("corner_nook", "pillar_colonnade", "none"),
    "medical": ("center_screen", "wing_split", "l_alcove"),
    "archive": ("pillar_colonnade", "double_wing", "wing_split", "l_alcove"),
    "workshop": ("l_alcove", "wing_split", "none"),
    "arcade": ("pillar_colonnade", "corner_nook", "none"),
    "shop": ("l_alcove", "pantry", "none"),
    "harbor": ("corner_nook", "l_alcove", "none"),
    "cafe": ("l_alcove", "corner_nook", "pantry", "none"),
    "manor": ("pillar_colonnade", "double_wing", "wing_split", "none"),
    "inn": ("alcove", "corner_nook", "l_alcove", "none"),
    "default": ("none", "l_alcove", "corner_nook", "pillar_colonnade"),
}

_RUG_POOL_BY_THEME: dict[str, tuple[str, ...]] = {
    "onsen": ("twin_mats", "corner_lounge", "runner_h"),
    "tea": ("twin_mats", "center", "checker_patch"),
    "alchemy": ("runner_v", "corner_lounge", "none"),
    "music": ("center", "checker_patch", "runner_h"),
    "clockwork": ("runner_h", "runner_v", "none"),
    "observatory": ("center", "checker_patch"),
    "medical": ("runner_v", "none"),
    "archive": ("runner_h", "runner_v", "center"),
    "workshop": ("none", "corner_lounge"),
    "arcade": ("checker_patch", "runner_h", "corner_lounge"),
    "shop": ("runner_h", "corner_lounge", "none"),
    "harbor": ("runner_h", "corner_lounge"),
    "cafe": ("corner_lounge", "checker_patch", "twin_mats"),
    "manor": ("center", "checker_patch", "runner_h"),
    "inn": ("corner_lounge", "twin_mats", "center"),
    "default": ("center", "corner_lounge", "runner_h", "none"),
}

_ALLOWED_RELATIONS = ("centerpiece", "against_wall", "in_corner", "near", "on_surface", "wall_hang", "open")
_ROOM_SHAPES = ("rect", "l_shape", "foyer_hall", "bay_recess", "split_terrace")


def _infer_room_archetype(room: Mapping[str, Any], world_prompt: str) -> str:
    """严格区分建筑自身职能与外部世界主题，杜绝全镇每栋建筑被世界观同质化污染。"""
    kind = str(room.get("kind") or room.get("role") or "").strip().lower()
    room_name = str(room.get("name") or "").strip()
    is_standalone = bool(room.get("is_standalone", False))

    kind_map = {
        "cafe": "cafe",
        "shop": "shop",
        "arcade": "arcade",
        "kitchen": "cafe",
        "lab": "alchemy",
        "clinic": "medical",
        "library": "archive",
        "study": "archive",
        "gallery": "archive",
        "inn": "inn",
        "home": "inn",
        "bedroom": "inn",
        "dock": "harbor",
        "observatory": "observatory",
        "workshop": "workshop",
        "lobby": "manor",
        "hall": "manor",
    }

    if is_standalone:
        # 独立单间模式（mode="room"）：world_prompt 即该房间本身的主题描述
        prompt_theme = infer_theme_key(world_prompt, "")
        if prompt_theme != "default":
            return prompt_theme
        name_theme = infer_theme_key(room_name, "")
        if name_theme != "default":
            return name_theme
        return kind_map.get(kind, "default")

    # 城镇子建筑 / 复合建筑子房间：
    # 1) 仅检查当前建筑/房间【自身名字】（绝不混入含镇名的 brief 或 world_prompt）
    name_theme = infer_theme_key(room_name, "")
    if name_theme not in ("default", "manor"):
        return name_theme
    # 2) 严格遵循建筑职能 role / kind（书店就是书店、杂货铺就是杂货铺、住处就是住处）
    if kind in kind_map:
        return kind_map[kind]
    if name_theme == "manor":
        return "manor"
    # 3) 仅当为无明确细分的 public 公共厅堂时，才参考 detail / world_prompt
    fallback_theme = infer_theme_key(f"{room.get('detail', '')} {world_prompt}", "")
    return fallback_theme if fallback_theme != "default" else "default"


def _sanitize_custom_scene_item(item: Mapping[str, Any], idx: int, theme_key: str) -> dict[str, Any]:
    motif = _match_motif_for_custom(item, theme_key)
    relation = str(item.get("relation") or "").strip().lower()
    if relation not in _ALLOWED_RELATIONS:
        if motif in ("fireplace", "wardrobe", "folding_screen", "display_case", "bar_counter"):
            relation = "against_wall"
        elif motif in ("grand_piano", "long_table", "lab_bench", "tea_hearth", "gear_console", "altar_pedestal", "workbench"):
            relation = "centerpiece"
        elif motif in ("hot_spring_tub", "standing_mirror", "statue_bust", "globe_stand"):
            relation = "in_corner"
        else:
            relation = "near" if item.get("anchor_ref") else "open"

    raw_fp = item.get("footprint")
    default_fp = list(FURNITURE_MOTIFS[motif]["footprint"])
    if isinstance(raw_fp, (list, tuple)) and len(raw_fp) == 2:
        fp = [clamp_int(raw_fp[0], 1, 2, default_fp[0]), clamp_int(raw_fp[1], 1, 2, default_fp[1])]
    else:
        fp = default_fp

    out: dict[str, Any] = {
        "ref": slugify(str(item.get("ref") or f"custom_{idx}"), prefix="item"),
        "motif": motif,
        "name": clip_text(item.get("name"), 20, str(FURNITURE_MOTIFS[motif]["default_name"])),
        "brief": clip_text(item.get("brief"), 96, str(FURNITURE_MOTIFS[motif]["default_brief"])),
        "footprint": fp,
        "relation": relation,
        "anchor_ref": slugify(str(item.get("anchor_ref") or ""), prefix="item") if item.get("anchor_ref") else "",
    }
    if "solid" in item:
        out["solid"] = bool(item["solid"])
    if isinstance(item.get("traits"), Mapping):
        out["traits"] = dict(item["traits"])
    if isinstance(item.get("pixels"), (list, tuple)):
        out["pixels"] = list(item["pixels"])
    if isinstance(item.get("slot_colors"), Mapping):
        out["slot_colors"] = dict(item["slot_colors"])
    if item.get("blueprint_id"):
        out["blueprint_id"] = str(item["blueprint_id"]).strip()
    return out


def default_bed_count(role: str, name: str) -> int:
    if any(word in name.casefold() for word in ("双人客房", "双人房", "双人间", "双床", "twin room")):
        return 2
    return 1 if role in ("home", "bedroom", "inn") else 0


def activity_layout(role: str, name: str, beds: int = 0) -> str:
    text = name.casefold()
    if any(word in text for word in ("拉面", "ramen")):
        return "ramen"
    if any(word in text for word in ("书店", "书屋", "书局", "bookshop", "bookstore")):
        return "bookshop"
    if any(word in text for word in ("书斋", "书房", "阅览室", "图书馆", "文库", "reading room", "library", "study")):
        return "study"
    theme = infer_theme_key(text)
    if theme == "onsen" and role not in ("cafe", "shop", "library", "arcade"):
        return "onsen"
    if theme == "cafe":
        return "cafe"
    if any(word in text for word in ("暖炉", "壁炉", "围炉", "fireplace")):
        return "hearth"
    if role == "cafe" and theme in ("default", "onsen"):
        return "cafe"
    if beds == 2 and (theme == "inn" or default_bed_count(role, text) == 2
                      or (role in ("home", "bedroom", "inn") and theme == "default")):
        return "twin_room"
    return ""


def activity_dimensions(layout: str, default: tuple[int, int]) -> tuple[int, int]:
    return {"cafe": (20, 11), "bookshop": (13, 16)}.get(layout, default)


def interior_entry_x(layout: str, width: int, seed: int = 0) -> int:
    if layout in ("cafe", "ramen", "twin_room"):
        return width - 1 - width // 3 if seed % 2 else width // 3
    return width // 2


@dataclass
class _ActivityPlan:
    targets: dict[str, tuple[int, int]] = field(default_factory=dict)
    aisle: set[tuple[int, int]] = field(default_factory=set)
    screen: set[tuple[int, int]] = field(default_factory=set)
    windows: set[tuple[int, int]] = field(default_factory=set)


_ACTIVITY_ZONES = {
    "cafe": (("点单区", ("bar", "fridge")), ("窗边客座", ("sofa", "desk"))),
    "ramen": (("出餐区", ("bar", "prep", "fridge")), ("用餐区", ("desk", "seat", "desk2", "seat2"))),
    "bookshop": (("新书展台", ("display",)), ("选书区", ("shelf1", "shelf2", "shelf3", "shelf4")),
                 ("窗边阅览角", ("reading_seat", "long_t"))),
    "study": (("藏书区", ("shelf1", "shelf2", "shelf3")), ("阅览区", ("long_t", "desk", "reading_seat"))),
    "onsen": (("泡汤区", ("tub", "washstand")), ("更衣区", ("screen", "wardrobe")), ("汤后休息区", ("hearth", "cushion1", "cushion2")),
              ("客房", ("bed_0", "bed_1"))),
    "hearth": (("炉边休息区", ("hearth", "seat", "seat2", "desk")), ("茶水区", ("prep",))),
    "twin_room": (("就寝区", ("bed_0", "bed_1")), ("入口更衣区", ("wardrobe", "mirror"))),
}


_ACTIVITY_FURNITURE = {
    "ramen": [
        {"ref": "bar", "motif": "bar_counter", "name": "拉面出餐台", "relation": "open"},
        {"ref": "prep", "blueprint_id": "default_desk", "name": "备餐案台", "relation": "against_wall"},
        {"ref": "fridge", "blueprint_id": "default_fridge", "name": "食材冷藏柜", "relation": "against_wall"},
        {"ref": "ramen", "blueprint_id": "default_ramen", "relation": "on_surface", "anchor_ref": "bar"},
        {"ref": "tea", "blueprint_id": "default_hot_drink", "name": "焙茶杯", "relation": "on_surface", "anchor_ref": "bar"},
        {"ref": "desk", "blueprint_id": "default_desk", "name": "窗边食桌", "relation": "open"},
        {"ref": "seat", "blueprint_id": "default_sofa", "name": "窗边软座", "relation": "open"},
        {"ref": "desk2", "blueprint_id": "default_desk", "name": "双人食桌", "relation": "open"},
        {"ref": "seat2", "blueprint_id": "default_cat_cushion", "name": "用餐坐垫", "relation": "open"},
        {"ref": "plant", "blueprint_id": "default_plant", "relation": "against_wall"},
        {"ref": "lamp", "blueprint_id": "default_lamp", "relation": "on_surface", "anchor_ref": "desk2"},
    ],
    "study": [
        {"ref": "shelf2", "blueprint_id": "default_bookshelf", "name": "地方志书架", "relation": "against_wall"},
        {"ref": "shelf3", "blueprint_id": "default_bookshelf", "name": "文集书架", "relation": "against_wall"},
        {"ref": "reading_seat", "blueprint_id": "default_sofa", "name": "窗边阅读座", "relation": "open"},
        {"ref": "desk", "blueprint_id": "default_desk", "name": "抄读写字台", "relation": "open"},
        {"ref": "note", "blueprint_id": "default_notebook", "name": "阅览手记", "relation": "on_surface", "anchor_ref": "desk"},
        {"ref": "plant", "blueprint_id": "default_plant", "relation": "against_wall"},
    ],
    "onsen": [
        {"ref": "washstand", "blueprint_id": "default_desk", "name": "清洗案", "brief": "浴衣柜旁放着木盆和干净浴巾的矮案。", "relation": "against_wall"},
        {"ref": "cushion1", "blueprint_id": "default_cat_cushion", "name": "茶席坐垫", "relation": "open"},
        {"ref": "cushion2", "blueprint_id": "default_cat_cushion", "name": "茶席软垫", "relation": "open"},
        {"ref": "snack", "blueprint_id": "default_snack", "name": "汤后茶点", "relation": "on_surface", "anchor_ref": "hearth"},
    ],
    "hearth": [
        {"ref": "hearth", "motif": "fireplace", "name": "石砌暖炉", "relation": "against_wall"},
        {"ref": "seat", "blueprint_id": "default_sofa", "name": "炉边长座", "relation": "open"},
        {"ref": "seat2", "blueprint_id": "default_cat_cushion", "name": "炉边软垫", "relation": "open"},
        {"ref": "desk", "motif": "tea_hearth", "name": "炉边矮桌", "relation": "open"},
        {"ref": "prep", "blueprint_id": "default_desk", "name": "茶水台", "relation": "against_wall"},
        {"ref": "rack", "blueprint_id": "default_box", "name": "添柴木箱", "relation": "against_wall"},
        {"ref": "plant", "blueprint_id": "default_plant", "relation": "open"},
        {"ref": "tea", "blueprint_id": "default_hot_drink", "relation": "on_surface", "anchor_ref": "prep"},
        {"ref": "lamp", "blueprint_id": "default_lamp", "relation": "on_surface", "anchor_ref": "prep"},
    ],
}


def _placed_activity_zones(layout: str, placed: Mapping[str, dict[str, Any]]) -> list[dict[str, Any]]:
    zones = []
    for name, refs in _ACTIVITY_ZONES.get(layout, ()):
        if refs[0] == "tub" and "tub" not in placed:
            continue
        group = [placed[ref] for ref in refs if ref in placed]
        if group:
            x, y = min(e["x"] for e in group), min(e["y"] for e in group)
            end_x = max(e["x"] + e["footprint"][0] for e in group)
            end_y = max(e["y"] + e["footprint"][1] for e in group)
            zones.append({"zone_id": refs[0], "name": name, "x": x, "y": y,
                          "w": end_x - x, "h": end_y - y + 1})
    return zones


def _activity_plan(layout: str, w: int, h: int, top: int, doors: set[tuple[int, int]],
                   rng: random.Random) -> _ActivityPlan:
    plan = _ActivityPlan()
    if not layout or w < 10 or h - top < 7:
        return plan
    mid = w // 2
    entry = next((x for x, y in sorted(doors) if y == h - 1), interior_entry_x(layout, w, rng.randrange(2)))
    left, right = max(1, w // 4 - 1), min(w - 3, 3 * w // 4 - 1)
    if entry > mid or (entry == mid and rng.randrange(2)):
        left, right = right, left
    back = top + rng.randrange(2)
    front = h - 4
    left_edge, right_edge = (1, w - 3) if left < mid else (w - 3, 1)
    # A two-tile spine with a cross aisle at each real landing, including side doors.
    plan.aisle.update((x, y) for x in (mid - 1, mid) for y in range(top, h - 1))
    for dx, dy in doors or {(entry, h - 1)}:
        y = min(h - 2, max(top, dy))
        plan.aisle.update((x, y) for x in range(min(mid - 1, dx), max(mid, dx) + 1))
    if layout == "cafe":
        pastry_x = left - 2 if left < mid else left + 2
        plan.targets = {"bar": (left, front - 1), "pastry": (pastry_x, front - 1), "fridge": (left, back),
                        "sofa": (right, back), "desk": (right, back + 3)}
    elif layout == "ramen":
        plan.targets = {"bar": (left, front - 1), "prep": (left, back), "fridge": (left_edge, back),
                        "desk": (right, back + 3), "seat": (right, back), "desk2": (right, front),
                        "seat2": (right_edge, front), "plant": (right_edge, back)}
    elif layout == "study":
        plan.targets = {"shelf1": (left, back), "shelf2": (left, back + 3), "shelf3": (left, front),
                        "long_t": (right, back + 3), "reading_seat": (right, back),
                        "desk": (right, front + 1), "plant": (right_edge, back)}
    elif layout == "onsen":
        plan.targets = {"tub": (right, back), "screen": (right_edge, back + 3),
                        "wardrobe": (right, front), "washstand": (right_edge, front),
                        "hearth": (left, front - 1), "cushion1": (left_edge, front - 1),
                        "cushion2": (left, front + 1), "candle": (left_edge, back + 3),
                        "bed_0": (left, back), "bed_1": (left_edge, back)}
    elif layout == "hearth":
        plan.targets = {"hearth": (right, top), "seat": (right_edge, back + 2),
                        "seat2": (right, front), "desk": (right, back + 3),
                        "prep": (left, back + 1), "rack": (left_edge, back),
                        "plant": (left, front)}
    elif layout == "bookshop":
        shelf_y = max(top + (h - top) // 2 - 1, back + 6) if h >= 13 else top + (h - top) // 2
        plan.targets = {"display": (left, h - 3), "shelf1": (right, shelf_y),
                        "shelf2": (right, shelf_y + 3), "long_t": (right, back + 3),
                        "reading_seat": (right, back)}
        if h >= 13:
            plan.targets.update({"shelf3": (left, shelf_y), "shelf4": (left, shelf_y + 3)})
    elif layout == "twin_room":
        plan.targets = {"bed_0": (left, back), "bed_1": (right, back),
                        "desk": (right + (2 if right > mid else -2), back),
                        "wardrobe": (left, front), "mirror": (left, front - 2)}
        plan.screen.update((x, front) for x in range(max(0, entry - 1), min(w, entry + 2)))
        plan.screen.difference_update(plan.aisle)
    if layout in ("cafe", "bookshop", "study", "ramen") and top:
        plan.windows = {(x, top - 1) for x in range(right, right + 2)} - doors
    return plan


def design_room_scene_graph(
    room: Mapping[str, Any],
    *,
    world_prompt: str = "",
    seed: int = 0,
    llm_scene: Mapping[str, Any] | None = None,
    used_landmarks: set[str] | None = None,
) -> dict[str, Any]:
    """为单个室内空间构建完整的主题化 Holodeck Scene Graph（含异形房型规划与全镇唯一地标去重）。"""
    room_name = str(room.get("name") or "")
    palette_id = str(room.get("palette_id") or "warm_stardew")
    theme_key = _infer_room_archetype(room, world_prompt)
    rng = random.Random(seed ^ zlib.crc32(f"{room.get('space_id') or room_name}|{theme_key}".encode("utf-8")))

    w = clamp_int(room.get("w") or room.get("width"), 6, 36, 12)
    h = clamp_int(room.get("h") or room.get("height"), 6, 28, 9)
    bed_count = clamp_int(room.get("bed_count"), 0, 8, 0)
    room_kind = str(room.get("kind") or room.get("role") or "").strip().lower()
    area = w * max(1, h - 1)
    layout = activity_layout(room_kind, room_name, bed_count)
    if room.get("is_standalone"):
        layout = activity_layout(room_kind, world_prompt, bed_count) or layout
    if w < 10 or h < 8:
        layout = ""

    llm_data = llm_scene if isinstance(llm_scene, Mapping) else {}
    req_shape = str(llm_data.get("room_shape") or room.get("room_shape") or "").strip().lower()
    if req_shape in _ROOM_SHAPES:
        room_shape = req_shape
    elif layout == "cafe" and w >= 14 and h >= 10:
        room_shape = rng.choice(("rect", "bay_recess"))
    elif layout:
        room_shape = "rect"
    elif w >= 14 and h >= 10:
        shape_weights = {
            "onsen": ("split_terrace", "l_shape", "foyer_hall", "rect"),
            "tea": ("split_terrace", "foyer_hall", "l_shape", "rect"),
            "music": ("split_terrace", "bay_recess", "rect", "foyer_hall"),
            "manor": ("bay_recess", "split_terrace", "foyer_hall", "rect"),
            "archive": ("bay_recess", "l_shape", "split_terrace", "rect"),
            "cafe": ("l_shape", "bay_recess", "foyer_hall", "rect"),
            "shop": ("l_shape", "foyer_hall", "rect"),
            "inn": ("foyer_hall", "l_shape", "split_terrace", "rect"),
        }.get(theme_key, ("l_shape", "foyer_hall", "bay_recess", "split_terrace", "rect"))
        room_shape = rng.choice(shape_weights)
    else:
        room_shape = "rect"
    req_part = str(llm_data.get("partition_style") or room.get("partition_style") or "").strip().lower()
    valid_parts = {
        "none", "l_alcove", "pillar_colonnade", "center_screen", "corner_nook",
        "wing_split", "alcove", "divider", "double_wing", "pantry",
    }
    if layout in _ACTIVITY_FURNITURE:
        partition_style = "none"
    elif layout == "twin_room" and req_part in ("", "alcove", "l_alcove"):
        partition_style = "entry_screen"
    elif layout and req_part in valid_parts:
        partition_style = req_part
    elif layout:
        partition_style = "entry_screen" if layout == "twin_room" else "none"
    elif req_part in valid_parts and req_part != "none":
        partition_style = req_part
    elif req_part == "none" and room.get("partition_style") == "none" and not llm_data:
        # 小房间或显式传 none 时，大房间仍允许随机丰富隔断，小房间保持开阔
        if area >= 108:
            partition_style = rng.choice(_PARTITION_POOL_BY_THEME.get(theme_key, _PARTITION_POOL_BY_THEME["default"]))
        else:
            partition_style = "none"
    else:
        pool = _PARTITION_POOL_BY_THEME.get(theme_key, _PARTITION_POOL_BY_THEME["default"])
        partition_style = rng.choice(pool) if area >= 72 else "none"

    req_rug = str(llm_data.get("rug_style") or room.get("rug_style") or "").strip().lower()
    if req_rug in ("center", "runner_h", "runner_v", "corner_lounge", "twin_mats", "checker_patch", "none"):
        rug_style = req_rug
    elif room.get("rug") is False:
        rug_style = "none"
    elif layout:
        rug_style = {
            "cafe": "corner_lounge", "ramen": "corner_lounge", "bookshop": "runner_v",
            "study": "runner_v", "twin_room": "twin_mats", "onsen": "twin_mats", "hearth": "corner_lounge",
        }[layout]
    else:
        rug_style = rng.choice(_RUG_POOL_BY_THEME.get(theme_key, _RUG_POOL_BY_THEME["default"]))

    items: list[dict[str, Any]] = []
    used_refs: set[str] = set()

    def _add_item(raw: Mapping[str, Any]) -> None:
        copy_item = copy.deepcopy(dict(raw))
        ref = str(copy_item.get("ref") or f"obj_{len(items)}")
        while ref in used_refs:
            ref = f"{ref}_{len(items)}"
        copy_item["ref"] = ref
        used_refs.add(ref)
        items.append(copy_item)

    def _can_use_landmark(motif: str) -> bool:
        if not motif or motif not in HERO_LANDMARK_MOTIFS or used_landmarks is None:
            return True
        return motif not in used_landmarks

    def _mark_landmark(motif: str) -> None:
        if motif and motif in HERO_LANDMARK_MOTIFS and used_landmarks is not None:
            used_landmarks.add(motif)

    # Place functional sets before optional accents, even with a full list of custom items.
    if layout in _ACTIVITY_FURNITURE:
        core = list(_ACTIVITY_FURNITURE[layout])
        if layout == "study":
            core.extend(entry for entry in _THEME_FURNITURE_PACKS["archive"] if entry["ref"] in ("shelf1", "long_t", "lamp"))
        elif layout == "onsen":
            core.extend(_THEME_FURNITURE_PACKS["onsen"])
        for entry in core:
            motif = str(entry.get("motif") or "")
            if _can_use_landmark(motif):
                _add_item(entry)
                _mark_landmark(motif)
            elif layout == "hearth" and entry["ref"] == "hearth":
                _add_item({**entry, "motif": "tea_hearth", "name": "铁壶地炉"})
    elif layout == "cafe":
        for entry in _THEME_FURNITURE_PACKS["cafe"]:
            if entry["ref"] in ("cake", "snack"):
                entry = {**entry, "anchor_ref": "pastry"}
            _add_item(entry)
        _add_item({"ref": "pastry", "motif": "display_case", "name": "点心展柜",
                   "brief": "木吧台旁的玻璃柜，陈列着当天的甜点。", "relation": "open"})
    elif layout == "twin_room":
        for entry in _THEME_FURNITURE_PACKS["inn"]:
            _add_item(entry)
    elif layout == "bookshop":
        for entry in _THEME_FURNITURE_PACKS["archive"]:
            if entry["ref"] in ("shelf1", "long_t", "lamp", "display"):
                if entry["ref"] == "display":
                    entry = {**entry, "name": "新书展台", "brief": "玻璃下摆着本周新到的书，封面朝向进门的读者。"}
                _add_item(entry)
        _add_item({"ref": "shelf2", "blueprint_id": "default_bookshelf", "name": "分类选书架", "relation": "open"})
        if h >= 13:
            for ref, name in (("shelf3", "文学书架"), ("shelf4", "旅行书架")):
                _add_item({"ref": ref, "blueprint_id": "default_bookshelf", "name": name, "relation": "open"})
        _add_item({"ref": "reading_seat", "blueprint_id": "default_sofa", "name": "窗边阅读座", "relation": "open"})

    # 1. 优先吸收 LLM 批规划或 room["scene_items"] 提供的签名物件
    raw_custom = list(llm_data.get("custom_items") or room.get("scene_items") or [])
    custom_refs: dict[str, str] = {}
    custom_start = len(items)
    for idx, c_item in enumerate(raw_custom[:8]):
        if isinstance(c_item, Mapping):
            sanitized = _sanitize_custom_scene_item(c_item, idx, theme_key)
            m_id = str(sanitized.get("motif") or "")
            if not _can_use_landmark(m_id):
                continue
            _add_item(sanitized)
            custom_refs[sanitized["ref"]] = items[-1]["ref"]
            _mark_landmark(m_id)
    for item in items[custom_start:]:
        anchor = item.get("anchor_ref")
        if anchor in custom_refs:
            item["anchor_ref"] = custom_refs[anchor]

    # 2. 按床位需求硬约束添加床铺
    for b_idx in range(bed_count):
        _add_item({
            "ref": f"bed_{b_idx}",
            "blueprint_id": "default_bed",
            "relation": rng.choice(("in_corner", "against_wall")) if b_idx < 2 else "against_wall",
        })

    # 3. 主题核心家具包（全套洗牌锚点并执行全镇唯一大型地标预算，彻底打破固定坐标与重复件）
    target_pack = list(_THEME_FURNITURE_PACKS.get(theme_key, _THEME_FURNITURE_PACKS["default"]))
    if layout == "bookshop" or layout in _ACTIVITY_FURNITURE:
        target_pack = []
    existing_names = {str(x.get("name") or "") for x in items}
    max_total = max(5, min(14, area // 9))

    # 确保咖啡厅/商铺具备可补货食物台面
    if room_kind == "cafe" and theme_key != "cafe" and layout != "ramen":
        for extra in _THEME_FURNITURE_PACKS["cafe"][:4]:
            if len(items) < max_total and str(extra.get("name")) not in existing_names:
                _add_item(extra)
                existing_names.add(str(extra.get("name")))
    elif room_kind == "shop" and theme_key != "shop":
        for extra in _THEME_FURNITURE_PACKS["shop"][:4]:
            if len(items) < max_total and str(extra.get("name")) not in existing_names:
                _add_item(extra)
                existing_names.add(str(extra.get("name")))

    anchor_candidates = [x for x in target_pack if x.get("relation") in ("centerpiece", "against_wall", "in_corner")]
    detail_candidates = [x for x in target_pack if x.get("relation") in ("near", "on_surface", "wall_hang", "open")]
    rng.shuffle(anchor_candidates)
    rng.shuffle(detail_candidates)

    for entry in (*anchor_candidates, *detail_candidates):
        if len(items) >= max_total:
            break
        if str(entry.get("name") or "") in existing_names:
            continue
        m_id = str(entry.get("motif") or "")
        if not _can_use_landmark(m_id):
            continue
        if m_id and sum(1 for x in items if x.get("motif") == m_id) >= 1:
            continue
        entry_copy = dict(entry)
        # 随机对非唯一锚点做适度关系变体（例如边墙或角落互换），防止所有同类建筑家具落点雷同
        if entry_copy.get("relation") == "in_corner" and rng.random() < 0.38:
            entry_copy["relation"] = "against_wall"
        elif entry_copy.get("relation") == "against_wall" and rng.random() < 0.28:
            entry_copy["relation"] = "in_corner"
        _add_item(entry_copy)
        _mark_landmark(m_id)
        existing_names.add(str(entry.get("name") or ""))

    # 4. 补足环境基础不变量：至少 1 个 surface（承载台）、至少 1 个光源、至少 1 个墙挂或点缀
    has_surface = any(
        x.get("blueprint_id") in ("default_desk", "default_bookshelf")
        or (x.get("motif") and "surface" in (FURNITURE_MOTIFS.get(x["motif"], {}).get("traits") or {}))
        or (isinstance(x.get("traits"), Mapping) and "surface" in x["traits"])
        for x in items
    )
    if not has_surface:
        _add_item({
            "ref": "auto_desk",
            "blueprint_id": "default_desk",
            "relation": "against_wall" if items else "centerpiece",
        })

    has_light = any(
        x.get("blueprint_id") in ("default_lamp", "default_arcade", "default_pc", "default_campfire", "default_street_lamp")
        or (x.get("motif") and "switchable" in (FURNITURE_MOTIFS.get(x["motif"], {}).get("traits") or {})
            and (FURNITURE_MOTIFS[x["motif"]]["traits"]["switchable"].get("provides_light")))
        for x in items
    )
    if not has_light:
        surf_ref = next(
            (x["ref"] for x in items if x.get("blueprint_id") == "default_desk" or (x.get("motif") and "surface" in (FURNITURE_MOTIFS.get(x["motif"], {}).get("traits") or {}))),
            "",
        )
        _add_item({
            "ref": "auto_lamp",
            "blueprint_id": "default_lamp",
            "relation": "on_surface" if surf_ref else "in_corner",
            "anchor_ref": surf_ref,
        })

    if not any(x.get("relation") == "wall_hang" for x in items):
        _add_item({
            "ref": "wall_accent",
            "blueprint_id": "default_window_poster" if theme_key == "arcade" else "default_wall_clock",
            "relation": "wall_hang",
        })

    return {
        "theme_key": theme_key,
        "activity_layout": layout,
        "palette_id": palette_id,
        "room_shape": room_shape,
        "partition_style": partition_style,
        "rug_style": rug_style,
        "items": items,
    }


_BATCH_SCENE_GRAPH_SYSTEM_PROMPT = """你是 2D 像素 RPG 的室内美术与空间关系设计师。
给定世界主题与若干室内房间的名字、职责和尺寸，请为【每个房间】设计独特、有故事感且不千篇一律的室内布局与 2~4 件主题定制家具。
必须严格返回 JSON 对象（不要 markdown 代码块），键为房间 room_id，值为：
{
  "partition_style": "none|l_alcove|pillar_colonnade|center_screen|corner_nook|wing_split",
  "rug_style": "center|runner_h|runner_v|corner_lounge|twin_mats|checker_patch|none",
  "custom_items": [
    {
      "ref": "短英文id",
      "name": "中文道具名(<=14字)",
      "brief": "简短有画面感的中文描述(<=45字)",
      "motif": "fireplace|grand_piano|long_table|sofa_plush|hot_spring_tub|altar_pedestal|lab_bench|bar_counter|display_case|gear_console|folding_screen|wardrobe|tea_hearth|workbench|standing_mirror|statue_bust|globe_stand|candelabra|specimen_rack|flower_vase",
      "footprint": [1, 1],
      "relation": "centerpiece|against_wall|in_corner|near|on_surface|wall_hang|open",
      "anchor_ref": "若为near或on_surface，可填写同房间另一个ref"
    }
  ]
}
注意：不同房间的 motif 组合和 partition_style 应各具特色，切勿全照抄同一套。"""


def compile_scene_graphs_batch(
    rooms: Sequence[Mapping[str, Any]],
    *,
    world_prompt: str = "",
    adapter_provider: Any = None,
    timeout_sec: float = 12.0,
) -> dict[str, dict[str, Any]]:
    """单批 LLM 调用为整座复合建筑的所有房间设计签名主题家具与分区风格（失败静默降级）。"""
    if not rooms:
        return {}
    try:
        from .text_compiler import _ask_llm_json  # 延迟导入避免与 text_compiler 循环依赖
    except Exception:
        return {}

    room_lines = []
    valid_ids: list[str] = []
    for idx, rm in enumerate(rooms[:12]):
        rid = str(rm.get("room_id") or rm.get("space_id") or f"room_{idx}")
        valid_ids.append(rid)
        room_lines.append({
            "room_id": rid,
            "name": str(rm.get("name") or rid),
            "kind": str(rm.get("kind") or rm.get("role") or "room"),
            "size": [clamp_int(rm.get("w") or rm.get("width"), 6, 36, 12), clamp_int(rm.get("h") or rm.get("height"), 6, 28, 9)],
            "brief": clip_text(rm.get("brief") or rm.get("detail"), 80, ""),
        })

    user_prompt = json.dumps({
        "world_prompt": clip_text(world_prompt, 240, "复古像素小镇室内空间"),
        "rooms": room_lines,
    }, ensure_ascii=False)

    parsed = _ask_llm_json(
        _BATCH_SCENE_GRAPH_SYSTEM_PROMPT,
        user_prompt,
        adapter_provider=adapter_provider,
        timeout_sec=timeout_sec,
        max_tokens=min(1800, 380 * max(1, len(room_lines))),
    )
    if not isinstance(parsed, Mapping):
        return {}

    results: dict[str, dict[str, Any]] = {}
    for rid in valid_ids:
        entry = parsed.get(rid)
        if not isinstance(entry, Mapping):
            continue
        theme_guess = infer_theme_key(f"{world_prompt} {rid}", "")
        custom_clean: list[dict[str, Any]] = []
        raw_list = entry.get("custom_items")
        if isinstance(raw_list, list):
            for idx, raw in enumerate(raw_list[:5]):
                if isinstance(raw, Mapping):
                    custom_clean.append(_sanitize_custom_scene_item(raw, idx, theme_guess))
        results[rid] = {
            "partition_style": str(entry.get("partition_style") or "").strip().lower(),
            "rug_style": str(entry.get("rug_style") or "").strip().lower(),
            "custom_items": custom_clean,
        }
    return results


# ---------------------------------------------------------------------------
# 地毯 / 隔断涂刷 + 3 阶段 CSP 约束求解与 BFS 全可达验证
# ---------------------------------------------------------------------------

def _relation_priority(relation: str) -> int:
    order = {
        "centerpiece": 0,
        "in_corner": 1,
        "against_wall": 2,
        "near": 3,
        "open": 4,
        "wall_hang": 5,
        "on_surface": 6,
    }
    return order.get(relation, 4)


def _bfs_reachable_set(
    w: int,
    h: int,
    wall_rows: int,
    blocked: set[tuple[int, int]],
    seeds: Iterable[tuple[int, int]],
    *,
    engine: Any = None,
    space: Mapping[str, Any] | None = None,
) -> set[tuple[int, int]]:
    q: deque[tuple[int, int]] = deque()
    seen: set[tuple[int, int]] = set()
    for sx, sy in seeds:
        if 0 <= sx < w and wall_rows <= sy < h and (sx, sy) not in blocked and (sx, sy) not in seen:
            seen.add((sx, sy))
            q.append((sx, sy))
    while q:
        cx, cy = q.popleft()
        for nx, ny in ((cx + 1, cy), (cx - 1, cy), (cx, cy + 1), (cx, cy - 1)):
            if 0 <= nx < w and wall_rows <= ny < h and (nx, ny) not in blocked and (nx, ny) not in seen:
                if engine is not None and space is not None and not engine.can_step_elevation(space, cx, cy, nx, ny):
                    continue
                seen.add((nx, ny))
                q.append((nx, ny))
    return seen


def _paint_interior_architecture(
    engine: Any,
    space_id: str,
    space: Mapping[str, Any],
    *,
    w: int,
    h: int,
    wall_rows: int,
    floor_tile: str,
    rug_tile: str,
    wall_tile: str,
    rug_style: str,
    partition_style: str,
    room_shape: str = "rect",
    door_cells: set[tuple[int, int]],
    plan: _ActivityPlan,
    rng: random.Random,
) -> set[tuple[int, int]]:
    """绘制异形房型轮廓（L型/凸窗/玄关/室内错层高台）、地毯与隔断墙，返回不可通行墙格集合。"""
    cells_to_paint: list[dict[str, Any]] = []
    cells_to_paint.extend({"x": x, "y": y, "tile_id": "glass_wall"} for x, y in sorted(plan.windows))
    y0 = wall_rows
    floor_h = h - y0

    door_wide_clearance: set[tuple[int, int]] = set()
    for dx, dy in door_cells:
        for ox in range(-2, 3):
            for oy in range(-2, 3):
                door_wide_clearance.add((dx + ox, dy + oy))

    # 0. 异形房型轮廓切削（L 型切角 / 凸窗凹龛 / 玄关内廊 / 室内错层高台）
    shape_walls: list[tuple[int, int]] = []
    indoor_elevs: dict[str, int] = {}
    indoor_stairs: dict[str, str] = {}

    if room_shape == "l_shape" and w >= 13 and floor_h >= 8:
        cw = rng.choice((3, 4))
        ch = rng.choice((3, 4)) if floor_h >= 10 else 3
        corner_boxes = [
            [(x, y) for y in range(y0, y0 + ch) for x in range(0, cw)],
            [(x, y) for y in range(y0, y0 + ch) for x in range(w - cw, w)],
            [(x, y) for y in range(h - ch - 1, h - 1) for x in range(0, cw)],
            [(x, y) for y in range(h - ch - 1, h - 1) for x in range(w - cw, w)],
        ]
        rng.shuffle(corner_boxes)
        for box in corner_boxes:
            if not any(pt in door_wide_clearance for pt in box):
                shape_walls.extend(box)
                break
    elif room_shape == "bay_recess" and w >= 14 and floor_h >= 8:
        pw = 3 if w >= 16 else 2
        ph = 2
        left_pier = [(x, y) for y in range(y0, y0 + ph) for x in range(0, pw)]
        right_pier = [(x, y) for y in range(y0, y0 + ph) for x in range(w - pw, w)]
        if not any(pt in door_wide_clearance for pt in left_pier):
            shape_walls.extend(left_pier)
        if not any(pt in door_wide_clearance for pt in right_pier):
            shape_walls.extend(right_pier)
    elif room_shape == "foyer_hall" and w >= 13 and floor_h >= 8:
        south_doors = [d for d in door_cells if d[1] >= h - 2]
        if south_doors:
            dx0, dy0 = south_doors[0]
            fy = max(y0 + 3, dy0 - 2)
            # 玄关铺石砖，并在一侧立矮隔墙形成折入式玄关内廊
            for gx in range(max(1, dx0 - 2), min(w - 1, dx0 + 3)):
                for gy in range(fy, h):
                    if (gx, gy) not in door_cells and not engine.is_wall_cell(space, gx, gy):
                        cells_to_paint.append({"x": gx, "y": gy, "tile_id": "stone_tile"})
            side_left = rng.random() < 0.5
            seg = (
                [(x, fy) for x in range(max(1, dx0 - 4), max(1, dx0 - 1))]
                if side_left
                else [(x, fy) for x in range(min(w - 2, dx0 + 2), min(w - 1, dx0 + 5))]
            )
            shape_walls.extend(pt for pt in seg if pt not in door_wide_clearance)
    elif room_shape == "split_terrace" and w >= 14 and floor_h >= 9:
        # 优先抬升北侧后半厅为 elev=1 错层高台，若北侧有门则改为抬升东/西侧翼
        th_band = 3 if floor_h < 11 else 4
        north_band = [(x, y) for y in range(y0, y0 + th_band) for x in range(0, w)]
        if not any(pt in door_wide_clearance for pt in north_band):
            for tx, ty in north_band:
                indoor_elevs[f"{tx},{ty}"] = 1
            for tx in range(w):
                for ty in range(0, y0):
                    indoor_elevs[f"{tx},{ty}"] = 1
            stair_x = clamp_int(w // 2 + rng.randint(-3, 2), 2, w - 4, w // 2)
            for sx in (stair_x, stair_x + 1):
                indoor_stairs[f"{sx},{y0 + th_band - 1}"] = "ns"
                cells_to_paint.append({"x": sx, "y": y0 + th_band - 1, "tile_id": "wood_floor"})
        else:
            wing_w = 4 if w < 18 else 5
            for side_east in ([True, False] if rng.random() < 0.5 else [False, True]):
                xs = range(w - wing_w, w) if side_east else range(0, wing_w)
                wing_cells = [(x, y) for y in range(y0, h) for x in xs]
                if not any(pt in door_wide_clearance for pt in wing_cells):
                    for tx, ty in wing_cells:
                        indoor_elevs[f"{tx},{ty}"] = 1
                    edge_x = (w - wing_w) if side_east else (wing_w - 1)
                    stair_y = clamp_int(y0 + floor_h // 2 + rng.randint(-1, 1), y0 + 1, h - 3, y0 + 3)
                    for sy in (stair_y, stair_y + 1):
                        indoor_stairs[f"{edge_x},{sy}"] = "ew"
                        cells_to_paint.append({"x": edge_x, "y": sy, "tile_id": "wood_floor"})
                    break

    if indoor_elevs:
        engine.set_space_terrain_relief(space_id, elevations=indoor_elevs, stairs=indoor_stairs)
        space, _ = _read_space_and_entities(engine, space_id)

    def _emit_rect(rx0: int, ry0: int, rw: int, rh: int, tid: str) -> None:
        for ty in range(ry0, ry0 + rh):
            for tx in range(rx0, rx0 + rw):
                if (
                    0 <= tx < w
                    and y0 <= ty < h
                    and (tx, ty) not in door_cells
                    and not engine.is_wall_cell(space, tx, ty)
                ):
                    cells_to_paint.append({"x": tx, "y": ty, "tile_id": tid})

    if y0 < h:
        # 1. 地毯样式多样化（逐格输出 tile_id，并跳过外围墙体与门洞）
        cx = w // 2
        cy = y0 + max(0, h - y0) // 2
        if rug_tile and rug_tile != floor_tile and rug_style != "none" and w >= 8 and h - y0 >= 5:
            if rug_style == "center":
                rw = max(3, min(w - 4, w // 2 + (1 if w % 2 else 0)))
                rh = max(2, min(h - y0 - 2, (h - y0) // 2 + 1))
                _emit_rect(max(1, cx - rw // 2), max(y0 + 1, cy - rh // 2), rw, rh, rug_tile)
            elif rug_style == "runner_h":
                _emit_rect(1, cy, max(2, w - 2), min(2, max(1, h - y0 - 2)), rug_tile)
            elif rug_style == "runner_v":
                _emit_rect(max(1, cx - 1), y0 + 1, min(2, max(1, w - 2)), max(2, h - y0 - 2), rug_tile)
            elif rug_style == "corner_lounge":
                rw = max(3, w // 3)
                rh = max(2, (h - y0) // 2)
                lounge = plan.targets.get("sofa") or plan.targets.get("seat")
                left_side = lounge[0] < cx if lounge else rng.random() < 0.5
                rx = 1 if left_side else max(1, w - 1 - rw)
                _emit_rect(rx, y0 + 1, rw, rh, rug_tile)
            elif rug_style == "twin_mats":
                rw = max(2, min(4, (w - 5) // 2))
                rh = max(2, min(3, h - y0 - 2))
                _emit_rect(1, y0 + 1, rw, rh, rug_tile)
                _emit_rect(max(1, w - 1 - rw), max(y0 + 1, h - 1 - rh), rw, rh, rug_tile)
            elif rug_style == "checker_patch":
                rx0, rx1 = max(2, cx - 3), min(w - 2, cx + 3)
                ry0, ry1 = max(y0 + 1, cy - 2), min(h - 1, cy + 2)
                for ty in range(ry0, ry1):
                    for tx in range(rx0, rx1):
                        if (tx + ty) % 2 == 0 and (tx, ty) not in door_cells and not engine.is_wall_cell(space, tx, ty):
                            cells_to_paint.append({"x": tx, "y": ty, "tile_id": rug_tile})

    # 2. 计算并安全校验室内隔断墙（保证全图单连通、且门前留 1 格净空）
    door_clearance: set[tuple[int, int]] = set()
    for dx, dy in door_cells:
        for ox in range(-1, 2):
            for oy in range(-1, 2):
                door_clearance.add((dx + ox, dy + oy))

    cand_walls: list[tuple[int, int]] = list(shape_walls)
    if partition_style != "none":
        cand_walls.extend(sorted(plan.screen))
    floor_h = h - y0
    if partition_style != "none" and room_shape not in ("split_terrace", "l_shape") and w >= 10 and floor_h >= 6:
        if partition_style in ("l_alcove", "alcove"):
            px = max(3, min(w - 4, int(w * rng.choice((0.36, 0.62)))))
            py_end = y0 + max(2, min(floor_h - 3, 3))
            cand_walls.extend((px, py) for py in range(y0, py_end))
            if px <= w // 2:
                cand_walls.extend((tx, py_end) for tx in range(max(1, px - 1), px + 1))
            else:
                cand_walls.extend((tx, py_end) for tx in range(px, min(w - 1, px + 2)))
        elif partition_style == "pillar_colonnade":
            cols = [x for x in (3, w - 4) if 2 <= x <= w - 3]
            rows = [y0 + 2]
            if floor_h >= 8:
                rows.append(h - 3)
            for px in cols:
                for py in rows:
                    cand_walls.append((px, py))
        elif partition_style == "center_screen":
            px = w // 2
            mid_y = y0 + floor_h // 2
            for dy in (-1, 0, 1):
                if y0 + 1 <= mid_y + dy <= h - 3:
                    cand_walls.append((px, mid_y + dy))
        elif partition_style == "corner_nook":
            px = w - 4 if rng.random() < 0.5 else 3
            for py in range(y0 + 1, min(h - 3, y0 + 3)):
                cand_walls.append((px, py))
            step = 1 if px < w // 2 else -1
            if 1 <= px + step <= w - 2:
                cand_walls.append((px + step, y0 + 2))
        elif partition_style in ("wing_split", "divider", "pantry"):
            px = max(3, min(w - 4, w // 2 - (1 if w % 2 == 0 else 0)))
            for py in range(y0, min(h - 2, y0 + floor_h // 2)):
                cand_walls.append((px, py))
            for py in range(max(y0 + floor_h // 2 + 2, h - 2), h - 1):
                cand_walls.append((px, py))
        elif partition_style == "double_wing" and w >= 14:
            for px in (4, w - 5):
                for py in range(y0, y0 + max(2, floor_h // 2 - 1)):
                    cand_walls.append((px, py))

    # 过滤会堵门、贴门或把空间切碎的隔断墙格
    accepted_walls: set[tuple[int, int]] = set()
    walkable_total = {
        (x, y)
        for y in range(y0, h)
        for x in range(w)
        if not engine.is_wall_cell(space, x, y)
    }
    base_blocked = {(x, y) for y in range(y0, h) for x in range(w) if (x, y) not in walkable_total}
    seed_pool = [
        (dx, max(y0, min(h - 1, dy - 1 if dy == h - 1 else dy)))
        for dx, dy in door_cells
    ]
    seed_pool = [s for s in seed_pool if s in walkable_total]
    if not seed_pool and walkable_total:
        seed_pool = [sorted(walkable_total)[len(walkable_total) // 2]]

    filtered_candidates = [
        c for c in cand_walls
        if c not in door_clearance and c not in plan.aisle
        and not engine.is_stair_cell(space, c[0], c[1])
        and 0 <= c[0] < w
        and y0 <= c[1] < h - 1
        and c in walkable_total
    ]
    if filtered_candidates and seed_pool:
        trial_set = set(filtered_candidates)
        reach = _bfs_reachable_set(w, h, y0, base_blocked | trial_set, seed_pool[:1], engine=engine, space=space)
        expected = walkable_total - trial_set
        if reach == expected:
            accepted_walls = trial_set
        else:
            for cell in filtered_candidates:
                trial_set = accepted_walls | {cell}
                if _bfs_reachable_set(w, h, y0, base_blocked | trial_set, seed_pool[:1], engine=engine, space=space) == (walkable_total - trial_set):
                    accepted_walls = trial_set

    for wx, wy in sorted(accepted_walls):
        cells_to_paint.append({"x": wx, "y": wy, "tile_id": wall_tile})

    if cells_to_paint:
        engine.paint_tiles(space_id, cells_to_paint)
    return accepted_walls


def _read_space_and_entities(engine: Any, space_id: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    with engine._lock:
        raw_space = engine._world["spaces"].get(space_id)
        if not raw_space:
            raise ValueError(f"空间不存在：{space_id}")
        space = copy.deepcopy(raw_space)
        ents = [
            copy.deepcopy(ent)
            for ent in engine._world["entities"].values()
            if ent.get("space_id") == space_id
        ]
    return space, ents


def solve_and_place_scene_graph(
    engine: Any,
    space_id: str,
    room: Mapping[str, Any],
    scene_graph: Mapping[str, Any] | None = None,
    *,
    door_cells: Iterable[tuple[int, int]] = (),
    reserved_cells: Iterable[tuple[int, int]] = (),
    seed: int = 0,
) -> dict[str, Any]:
    """用 3 阶段约束松弛 CSP 求解器将 Scene Graph 无阻断地放置进室内空间。"""
    space, space_entities = _read_space_and_entities(engine, space_id)
    w = int(space["width"])
    h = int(space["height"])
    wall_rows = clamp_int(space.get("wall_rows"), 0, 3, 1)
    palette_id = str(space.get("palette_id") or room.get("palette_id") or "warm_stardew")

    sg = dict(scene_graph) if isinstance(scene_graph, Mapping) else design_room_scene_graph(room, seed=seed)
    rng = random.Random(seed ^ zlib.crc32(f"{space_id}|{sg.get('theme_key')}|{sg.get('partition_style')}".encode("utf-8")))

    floor_tile = str(room.get("floor_tile") or "wood_floor")
    rug_tile = str(room.get("rug_tile") or "carpet_blue")
    wall_tile = str(room.get("wall_tile") or "wood_wall")

    # 收集已有门坐标与门前预留点
    detected_doors: set[tuple[int, int]] = set(tuple(c) for c in door_cells)
    existing_solid: set[tuple[int, int]] = set()
    existing_ground: set[tuple[int, int]] = set()
    existing_wall_occ: set[tuple[int, int]] = set()
    surface_records: list[dict[str, Any]] = []

    for ent in space_entities:
        ex, ey = int(ent["x"]), int(ent["y"])
        fw, fh = ent.get("footprint") or [1, 1]
        ez = int(ent.get("z", 0))
        cells = {(ex + dx, ey + dy) for dx in range(int(fw)) for dy in range(int(fh))}
        if "portal" in (ent.get("traits") or {}):
            detected_doors.update(cells)
        if ez == 0:
            if any(cy < wall_rows or engine.is_wall_cell(space, cx, cy) for cx, cy in cells):
                existing_wall_occ.update(cells)
            else:
                existing_ground.update(cells)
                if ent.get("solid"):
                    existing_solid.update(cells)
            if "surface" in (ent.get("traits") or {}):
                surface_records.append({
                    "ref": f"existing_{ent['entity_id']}",
                    "entity": ent,
                    "cells": [(ex + dx, ey + dy) for dx in range(int(fw)) for dy in range(int(fh))],
                    "capacity": int((ent["traits"]["surface"] or {}).get("capacity") or 4),
                    "used": set(),
                })

    # 也把地图中的非行走地砖（例如现有墙/水体）与异形房型纳入阻挡集合
    layout = str(sg.get("activity_layout") or "")
    plan = _activity_plan(layout, w, h, wall_rows, detected_doors, rng)
    existing_wall_occ.update(plan.windows)
    partition_walls = _paint_interior_architecture(
        engine,
        space_id,
        space,
        w=w,
        h=h,
        wall_rows=wall_rows,
        floor_tile=floor_tile,
        rug_tile=rug_tile,
        wall_tile=wall_tile,
        rug_style=str(sg.get("rug_style") or "center"),
        partition_style=str(sg.get("partition_style") or "none"),
        room_shape=str(sg.get("room_shape") or "rect"),
        door_cells=detected_doors,
        plan=plan,
        rng=rng,
    )

    refreshed_space, _ = _read_space_and_entities(engine, space_id)
    non_walkable_tiles: set[tuple[int, int]] = set(partition_walls)
    stair_cells: set[tuple[int, int]] = set()
    for ty in range(wall_rows, h):
        for tx in range(w):
            if not engine.get_cell_tile(refreshed_space, tx, ty).get("walkable", True):
                non_walkable_tiles.add((tx, ty))
            if engine.is_stair_cell(refreshed_space, tx, ty):
                stair_cells.add((tx, ty))

    door_guard: set[tuple[int, int]] = set()
    for dx, dy in detected_doors:
        door_guard.add((dx, dy))
        for ox, oy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            door_guard.add((dx + ox, dy + oy))
    for sx_t, sy_t in stair_cells:
        door_guard.add((sx_t, sy_t))
        for ox, oy in ((0, 1), (0, -1), (1, 0), (-1, 0)):
            door_guard.add((sx_t + ox, sy_t + oy))

    reserved_floor = set(tuple(c) for c in reserved_cells) | door_guard | plan.aisle
    occupied_ground = set(existing_ground) | non_walkable_tiles
    solid_map = set(existing_solid) | non_walkable_tiles

    # 所有室内地砖可达性检验的基准入口
    reach_seeds: list[tuple[int, int]] = []
    for dx, dy in sorted(detected_doors):
        for nx, ny in ((dx, dy), (dx, dy - 1), (dx, dy + 1), (dx + 1, dy), (dx - 1, dy)):
            if 0 <= nx < w and wall_rows <= ny < h and (nx, ny) not in solid_map:
                reach_seeds.append((nx, ny))
                break
    if not reach_seeds:
        for ty in range(h - 1, wall_rows - 1, -1):
            for tx in range(w):
                if (tx, ty) not in solid_map:
                    reach_seeds.append((tx, ty))
                    break
            if reach_seeds:
                break

    all_walkable_base = {(x, y) for y in range(wall_rows, h) for x in range(w) if (x, y) not in non_walkable_tiles}
    reach_seeds = reach_seeds[:1]
    initial_reachable = _bfs_reachable_set(
        w, h, wall_rows, solid_map, reach_seeds, engine=engine, space=refreshed_space,
    )

    def _approach_cells(cells: Iterable[tuple[int, int]]) -> set[tuple[int, int]]:
        return {
            (x + dx, y + dy)
            for x, y in cells
            for dx, dy in ((0, 0), (1, 0), (-1, 0), (0, 1), (0, -1))
        }

    item_approaches: list[set[tuple[int, int]]] = []

    all_bps = engine.all_blueprints(space_id)
    placed_refs: dict[str, dict[str, Any]] = {}
    placed_entities: list[dict[str, Any]] = []
    dropped: list[str] = []

    raw_items = list(sg.get("items") or [])
    raw_items.sort(
        key=lambda it: (
            it.get("relation") == "wall_hang"
            or all_bps.get(str(it.get("blueprint_id") or ""), {}).get("placement") == "wall",
            0 if it.get("ref") in plan.targets else 1,
            0 if "bar" in plan.targets and it.get("ref") in ("coffee", "ramen") else 1,
            _relation_priority(str(it.get("relation") or "open")),
            -(clamp_int((it.get("footprint") or [1, 1])[0], 1, 2, 1) * clamp_int((it.get("footprint") or [1, 1])[1], 1, 2, 1)),
        )
    )

    # 真随机视觉焦点偏移：让 centerpiece 不再死锁在几何正中心，且动态识别异形房间的全部内凹转角
    cx_mid = max(2.0, min(w - 3.0, (w - 1) / 2.0 + rng.uniform(-2.4, 2.4)))
    cy_mid = max(float(wall_rows + 1), min(h - 2.5, wall_rows + max(1, h - wall_rows - 1) / 2.0 + rng.uniform(-1.4, 1.4)))
    corners: list[tuple[int, int]] = []
    for cy_c in range(wall_rows, h):
        for cx_c in range(w):
            if (cx_c, cy_c) in non_walkable_tiles:
                continue
            wall_touch = 0
            for ox, oy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nx_c, ny_c = cx_c + ox, cy_c + oy
                if not (0 <= nx_c < w and wall_rows <= ny_c < h) or (nx_c, ny_c) in non_walkable_tiles:
                    wall_touch += 1
            if wall_touch >= 2:
                corners.append((cx_c, cy_c))
    if not corners:
        corners = [(0, wall_rows), (max(0, w - 1), wall_rows), (0, max(wall_rows, h - 1)), (max(0, w - 1), max(wall_rows, h - 1))]
    rng.shuffle(corners)

    def _is_against_wall(x: int, y: int, fw: int, fh: int, max_dist: int = 0) -> bool:
        touches_border = (
            x <= max_dist
            or (x + fw) >= w - max_dist
            or y <= wall_rows + max_dist
            or (y + fh) >= h - max_dist
        )
        if touches_border:
            return True
        if non_walkable_tiles:
            for cx in range(x, x + fw):
                for cy in range(y, y + fh):
                    for nx, ny in ((cx + 1, cy), (cx - 1, cy), (cx, cy + 1), (cx, cy - 1)):
                        if (nx, ny) in non_walkable_tiles:
                            return True
        return False

    def _in_corner_dist(x: int, y: int, fw: int, fh: int, max_dist: int = 0) -> bool:
        box_corners = [(x, y), (x + fw - 1, y), (x, y + fh - 1), (x + fw - 1, y + fh - 1)]
        for bx, by in box_corners:
            for cx, cy in corners:
                if max(abs(bx - cx), abs(by - cy)) <= max_dist:
                    return True
        return False

    def _near_anchor_dist(x: int, y: int, fw: int, fh: int, anchor: Mapping[str, Any] | None, max_dist: int) -> bool:
        if not anchor:
            return True
        ax, ay = int(anchor["x"]), int(anchor["y"])
        afw, afh = [int(v) for v in (anchor.get("footprint") or [1, 1])]
        dx = max(0, max(ax - (x + fw - 1), x - (ax + afw - 1)))
        dy = max(0, max(ay - (y + fh - 1), y - (ay + afh - 1)))
        return max(dx, dy) <= max_dist

    def _can_place_ground(x: int, y: int, fw: int, fh: int, is_solid: bool) -> bool:
        if x < 0 or y < wall_rows or x + fw > w or y + fh > h:
            return False
        cells = {(x + dx, y + dy) for dx in range(fw) for dy in range(fh)}
        if cells & occupied_ground:
            return False
        if cells & reserved_floor:
            return False
        if cells & detected_doors or cells & stair_cells:
            return False
        if len(cells) > 1:
            elev_set = {engine.get_cell_elevation(refreshed_space, cx_e, cy_e) for cx_e, cy_e in cells}
            if len(elev_set) > 1:
                return False
        if not is_solid:
            return bool(_approach_cells(cells) & initial_reachable)
        # 检查不破坏整体单连通性（含室内错层阶梯）与已有实体的可达性
        trial_solid = solid_map | cells
        reachable_after = _bfs_reachable_set(
            w, h, wall_rows, trial_solid, reach_seeds, engine=engine, space=refreshed_space,
        )
        # 1) 所有仍未被实体占据的可走地面必须全部互通，不出现孤岛
        if reachable_after != (initial_reachable - cells):
            return False
        # 2) 新放的固体自身至少有一侧站在可达地面上，方便角色交互
        if not _approach_cells(cells) & reachable_after:
            return False
        if any(not approaches & reachable_after for approaches in item_approaches):
            return False
        # 3) 所有门旁边均保留可立足可达格
        for dx, dy in detected_doors:
            if (dx, dy) not in reachable_after and not any(
                (dx + ox, dy + oy) in reachable_after for ox, oy in ((1, 0), (-1, 0), (0, 1), (0, -1))
            ):
                return False
        return True

    for item in raw_items:
        bid = str(item.get("blueprint_id") or "").strip()
        relation = str(item.get("relation") or "open")
        ref = str(item.get("ref") or "")
        anchor_ref = str(item.get("anchor_ref") or "")

        if bid and bid in all_bps:
            bp = all_bps[bid]
            fw, fh = [clamp_int(v, 1, 4, 1) for v in (bp.get("footprint") or [1, 1])]
            is_solid = bool(bp.get("solid", False))
            placement_mode = str(bp.get("placement") or "floor")
            synth_bp = None
        else:
            synth_bp = synthesize_furniture_blueprint(
                item,
                palette_id=palette_id,
                theme_key=str(sg.get("theme_key") or ""),
            )
            fw, fh = synth_bp["footprint"]
            is_solid = bool(synth_bp["solid"])
            placement_mode = str(synth_bp["placement"])

        # A. 挂墙物放置（wall_rows >= 1）
        if relation == "wall_hang" or placement_mode == "wall":
            if wall_rows <= 0:
                continue
            wall_candidates = [
                (wx, wy)
                for wy in range(wall_rows - fh + 1)
                for wx in range(1, max(2, w - fw))
                if all(
                    (x, wall_rows) in initial_reachable
                    and (x, wall_rows) not in occupied_ground
                    and all(
                        (x, y) not in existing_wall_occ and (x, y) not in detected_doors
                        and engine.is_wall_cell(refreshed_space, x, y)
                        and engine.get_cell_tile(refreshed_space, x, y).get("tile_id") != "glass_wall"
                        for y in range(wy, wall_rows)
                    )
                    for x in range(wx, wx + fw)
                )
            ]
            if not wall_candidates:
                continue
            wx, wy = rng.choice(wall_candidates)
            try:
                if bid and bid in all_bps:
                    ent = engine.place_from_blueprint(bid, space_id, wx, wy, delivery_mode="silent")
                    if item.get("name") or item.get("brief"):
                        ent = engine.update_entity_spec(
                            ent["entity_id"],
                            name=clip_text(item.get("name"), 20) or None,
                            brief=clip_text(item.get("brief"), 96) or None,
                        )
                else:
                    assert synth_bp is not None
                    ent = engine.place_ephemeral_entity(space_id, synth_bp, x=wx, y=wy)
                for ox in range(fw):
                    for oy in range(fh):
                        existing_wall_occ.add((wx + ox, wy + oy))
                if ref:
                    placed_refs[ref] = ent
                placed_entities.append(ent)
            except ValueError:
                dropped.append(ref or bid or "wall_item")
            continue

        # B. 尝试放置到桌面（on_surface）
        placed_on_surface = False
        if relation == "on_surface" and surface_records:
            ordered_surfs = list(surface_records)
            if anchor_ref and anchor_ref in placed_refs:
                target_eid = placed_refs[anchor_ref]["entity_id"]
                ordered_surfs.sort(key=lambda rec: 0 if rec["entity"]["entity_id"] == target_eid else 1)
            for rec in ordered_surfs:
                if len(rec["used"]) >= rec["capacity"]:
                    continue
                for sx, sy in rec["cells"]:
                    if (sx, sy) in rec["used"] or fw != 1 or fh != 1:
                        continue
                    approaches = _approach_cells([(sx, sy)])
                    if not approaches & initial_reachable:
                        continue
                    try:
                        if bid and bid in all_bps:
                            ent = engine.place_from_blueprint(
                                bid,
                                space_id,
                                sx,
                                sy,
                                delivery_mode="silent",
                                note_text=str(item.get("note_text") or ""),
                            )
                            if item.get("name") or item.get("brief"):
                                ent = engine.update_entity_spec(
                                    ent["entity_id"],
                                    name=clip_text(item.get("name"), 20) or None,
                                    brief=clip_text(item.get("brief"), 96) or None,
                                )
                        else:
                            assert synth_bp is not None
                            ent = engine.place_ephemeral_entity(space_id, synth_bp, x=sx, y=sy)
                        rec["used"].add((sx, sy))
                        item_approaches.append(approaches)
                        if ref:
                            placed_refs[ref] = ent
                        placed_entities.append(ent)
                        placed_on_surface = True
                        break
                    except ValueError:
                        continue
                if placed_on_surface:
                    break
            if placed_on_surface:
                continue
            if placement_mode == "surface_only":
                dropped.append(ref or bid or "surface_item")
                continue

        # C. 3 阶段约束松弛 CSP 地面求解（strict -> relaxed -> open）
        anchor_ent = placed_refs.get(anchor_ref)
        all_coords = [(x, y) for y in range(wall_rows, h - fh + 1) for x in range(0, w - fw + 1)]
        chosen_xy: tuple[int, int] | None = None
        target = plan.targets.get(ref)
        use_cells: set[tuple[int, int]] = set()
        if target:
            # Keep use positions off the main aisle, so two occupants can stop here.
            for x, y in sorted(all_coords, key=lambda pt: abs(pt[0] - target[0]) + abs(pt[1] - target[1])):
                stands = {(x, y + fh)}
                if ref == "bar":
                    stands.add((x, y - 1))
                if not stands <= initial_reachable or stands & (occupied_ground | reserved_floor):
                    continue
                if _can_place_ground(x, y, fw, fh, is_solid):
                    chosen_xy = (x, y)
                    use_cells = stands
                    break

        for stage in (0, 1, 2):
            if chosen_xy is not None:
                break
            cands: list[tuple[int, int]] = []
            for x, y in all_coords:
                if stage == 2:
                    cands.append((x, y))
                    continue

                if relation == "centerpiece":
                    max_d = 2.6 if stage == 0 else 4.2
                    dist = abs((x + fw / 2.0 - 0.5) - cx_mid) + abs((y + fh / 2.0 - 0.5) - cy_mid)
                    if dist <= max_d:
                        cands.append((x, y))
                elif relation == "in_corner":
                    if stage == 0 and _in_corner_dist(x, y, fw, fh, max_dist=1):
                        cands.append((x, y))
                    elif stage == 1 and _is_against_wall(x, y, fw, fh, max_dist=0):
                        cands.append((x, y))
                elif relation == "against_wall":
                    if _is_against_wall(x, y, fw, fh, max_dist=(0 if stage == 0 else 1)):
                        cands.append((x, y))
                elif relation == "near":
                    if _near_anchor_dist(x, y, fw, fh, anchor_ent, max_dist=(2 if stage == 0 else 4)):
                        cands.append((x, y))
                else:
                    # on_surface 降级或 open 物件，优先稍微避开正中央留出行走通道
                    if stage == 0 and ((x, y) not in reserved_floor):
                        cands.append((x, y))
                    elif stage == 1:
                        cands.append((x, y))

            if cands:
                # 尽量避免紧挨其它多格家具挤成一团（若 non-reserved 候选够多先选离门稍远的）
                if relation == "centerpiece":
                    cands.sort(key=lambda pt: abs((pt[0] + fw / 2.0 - 0.5) - cx_mid) + abs((pt[1] + fh / 2.0 - 0.5) - cy_mid) + rng.uniform(0.0, 0.9))
                else:
                    rng.shuffle(cands)
                chosen_xy = next((pt for pt in cands if _can_place_ground(*pt, fw, fh, is_solid)), None)
                if chosen_xy is not None:
                    break

        if chosen_xy is None:
            dropped.append(ref or bid or str(item.get("name") or "item"))
            continue

        gx, gy = chosen_xy
        try:
            if bid and bid in all_bps:
                ent = engine.place_from_blueprint(
                    bid,
                    space_id,
                    gx,
                    gy,
                    delivery_mode="silent",
                    note_text=str(item.get("note_text") or ""),
                )
                if item.get("name") or item.get("brief"):
                    ent = engine.update_entity_spec(
                        ent["entity_id"],
                        name=clip_text(item.get("name"), 20) or None,
                        brief=clip_text(item.get("brief"), 96) or None,
                    )
            else:
                assert synth_bp is not None
                ent = engine.place_ephemeral_entity(space_id, synth_bp, x=gx, y=gy)
        except ValueError:
            dropped.append(ref or bid or str(item.get("name") or "item"))
            continue

        cells = {(gx + ox, gy + oy) for ox in range(fw) for oy in range(fh)}
        occupied_ground.update(cells)
        reserved_floor.update(use_cells)
        item_approaches.append(_approach_cells(cells))
        if ent.get("solid"):
            solid_map.update(cells)
            initial_reachable = _bfs_reachable_set(
                w, h, wall_rows, solid_map, reach_seeds, engine=engine, space=refreshed_space,
            )
        if "surface" in (ent.get("traits") or {}):
            surface_records.append({
                "ref": ref or ent["entity_id"],
                "entity": ent,
                "cells": [(gx + ox, gy + oy) for ox in range(fw) for oy in range(fh)],
                "capacity": int((ent["traits"]["surface"] or {}).get("capacity") or 4),
                "used": set(),
            })
        if ref:
            placed_refs[ref] = ent
        placed_entities.append(ent)

    return {
        "space_id": space_id,
        "theme_key": str(sg.get("theme_key") or ""),
        "room_shape": str(sg.get("room_shape") or "rect"),
        "partition_style": str(sg.get("partition_style") or "none"),
        "rug_style": str(sg.get("rug_style") or "center"),
        "placed_count": len(placed_entities),
        "dropped": dropped,
        "entities": [e["entity_id"] for e in placed_entities],
        "reachable_cells": len(initial_reachable),
        "zones": _placed_activity_zones(layout, placed_refs),
        "walkable_cells": len(all_walkable_base),
    }


# ---------------------------------------------------------------------------
# 多隔间紧凑内室配装包与局部 CSP 求解器（4 层丰富度：家具簇 + 墙饰 + 桌面满载 + 局部可达）
# ---------------------------------------------------------------------------

SUBROOM_ENSEMBLE_PACKS: dict[str, list[dict[str, Any]]] = {
    "foyer_lounge": [
        {"ref": "sofa", "motif": "sofa_plush", "name": "迎客绒布长椅", "relation": "against_wall"},
        {"ref": "desk", "blueprint_id": "default_desk", "name": "迎宾茶几", "relation": "near", "anchor_ref": "sofa"},
        {"ref": "rack", "motif": "coat_rack", "name": "立柱衣帽架", "relation": "in_corner"},
        {"ref": "chair", "motif": "wood_chair", "name": "客座木椅", "relation": "near", "anchor_ref": "desk"},
        {"ref": "vase", "motif": "flower_vase", "name": "玄关插花座", "relation": "on_surface", "anchor_ref": "desk"},
        {"ref": "plant", "blueprint_id": "default_plant", "name": "门厅绿植", "relation": "in_corner"},
    ],
    "cafe_front": [
        {"ref": "bar", "motif": "bar_counter", "name": "原木手冲吧台", "relation": "against_wall"},
        {"ref": "coffee", "blueprint_id": "default_coffee_maker", "name": "意式咖啡机", "relation": "on_surface", "anchor_ref": "bar"},
        {"ref": "cake", "blueprint_id": "default_cake", "name": "草莓奶油蛋糕", "relation": "on_surface", "anchor_ref": "bar"},
        {"ref": "stool", "motif": "bar_stool", "name": "吧台高脚凳", "relation": "near", "anchor_ref": "bar"},
        {"ref": "desk", "blueprint_id": "default_desk", "name": "双人橡木圆桌", "relation": "centerpiece"},
        {"ref": "chair", "motif": "wood_chair", "name": "靠背餐椅", "relation": "near", "anchor_ref": "desk"},
        {"ref": "cup", "blueprint_id": "default_hot_drink", "name": "现煮热拿铁", "relation": "on_surface", "anchor_ref": "desk"},
        {"ref": "fridge", "blueprint_id": "default_fridge", "name": "甜品冰柜", "relation": "in_corner"},
    ],
    "ramen_front": [
        {"ref": "bar", "motif": "bar_counter", "name": "拉面明档木台", "relation": "against_wall"},
        {"ref": "stool1", "motif": "bar_stool", "name": "吧前圆木凳", "relation": "near", "anchor_ref": "bar"},
        {"ref": "stool2", "motif": "bar_stool", "name": "吧前圆木凳", "relation": "near", "anchor_ref": "bar"},
        {"ref": "desk", "blueprint_id": "default_desk", "name": "靠墙双人木桌", "relation": "centerpiece"},
        {"ref": "chair", "motif": "wood_chair", "name": "木质食客椅", "relation": "near", "anchor_ref": "desk"},
        {"ref": "cup", "blueprint_id": "default_hot_drink", "name": "热麦茶碗", "relation": "on_surface", "anchor_ref": "desk"},
        {"ref": "tea", "motif": "tea_set", "name": "调味壶托盘", "relation": "on_surface", "anchor_ref": "bar"},
        {"ref": "plant", "blueprint_id": "default_plant", "name": "竹叶盆栽", "relation": "in_corner"},
    ],
    "shop_front": [
        {"ref": "bar", "motif": "bar_counter", "name": "收银木柜台", "relation": "against_wall"},
        {"ref": "case", "motif": "display_case", "name": "玻璃精品展柜", "relation": "centerpiece"},
        {"ref": "crate", "motif": "crate_stack", "name": "时令货品木箱", "relation": "in_corner"},
        {"ref": "fridge", "blueprint_id": "default_fridge", "name": "保鲜冷饮柜", "relation": "against_wall"},
        {"ref": "desk", "blueprint_id": "default_desk", "name": "杂货陈列桌", "relation": "open"},
        {"ref": "snack", "blueprint_id": "default_snack", "name": "手工点心礼盒", "relation": "on_surface", "anchor_ref": "bar"},
    ],
    "bookshop_front": [
        {"ref": "shelf1", "blueprint_id": "default_bookshelf", "name": "新书畅销木架", "relation": "against_wall"},
        {"ref": "shelf2", "blueprint_id": "default_bookshelf", "name": "人文典籍高架", "relation": "against_wall"},
        {"ref": "case", "motif": "display_case", "name": "珍本玻璃展柜", "relation": "centerpiece"},
        {"ref": "bar", "motif": "bar_counter", "name": "借阅包书柜台", "relation": "against_wall"},
        {"ref": "chair", "motif": "wood_chair", "name": "选书休息木椅", "relation": "near", "anchor_ref": "bar"},
        {"ref": "books", "motif": "book_stack", "name": "推荐新书叠", "relation": "on_surface", "anchor_ref": "bar"},
        {"ref": "lamp", "blueprint_id": "default_lamp", "name": "青铜阅读台灯", "relation": "on_surface", "anchor_ref": "bar"},
    ],
    "arcade_hall": [
        {"ref": "desk", "blueprint_id": "default_desk", "name": "复古立式街机台", "relation": "against_wall"},
        {"ref": "stool", "motif": "bar_stool", "name": "机台高脚凳", "relation": "near", "anchor_ref": "desk"},
        {"ref": "gram", "blueprint_id": "default_gramophone", "name": "霓虹点唱音箱", "relation": "against_wall"},
        {"ref": "sofa", "motif": "sofa_plush", "name": "候场双人软座", "relation": "in_corner"},
        {"ref": "fridge", "blueprint_id": "default_fridge", "name": "冰汽水贩卖柜", "relation": "against_wall"},
        {"ref": "console", "motif": "gear_console", "name": "双人竞速控制台", "relation": "centerpiece"},
    ],
    "kitchen_back": [
        {"ref": "stove", "motif": "kitchen_stove", "name": "双灶铸铁炉台", "relation": "against_wall"},
        {"ref": "sink", "motif": "wash_sink", "name": "陶瓷双格洗池", "relation": "against_wall"},
        {"ref": "fridge", "blueprint_id": "default_fridge", "name": "食材保鲜冰箱", "relation": "in_corner"},
        {"ref": "desk", "blueprint_id": "default_desk", "name": "备菜料理木案", "relation": "centerpiece"},
        {"ref": "crate", "motif": "crate_stack", "name": "干货面粉木箱", "relation": "in_corner"},
        {"ref": "tea", "motif": "tea_set", "name": "暖水茶壶组", "relation": "on_surface", "anchor_ref": "desk"},
    ],
    "storage_room": [
        {"ref": "crate1", "motif": "crate_stack", "name": "封蜡货品木箱", "relation": "against_wall"},
        {"ref": "crate2", "motif": "crate_stack", "name": "备用杂物箱垛", "relation": "in_corner"},
        {"ref": "rack", "motif": "specimen_rack", "name": "分类物料木架", "relation": "against_wall"},
        {"ref": "box", "blueprint_id": "default_box", "name": "五金工具储物箱", "relation": "in_corner"},
        {"ref": "desk", "blueprint_id": "default_desk", "name": "清点盘货方桌", "relation": "open"},
    ],
    "bedroom": [
        {"ref": "wardrobe", "motif": "wardrobe", "name": "整衣木衣橱", "relation": "in_corner"},
        {"ref": "desk", "blueprint_id": "default_desk", "name": "窗边小书桌", "relation": "against_wall"},
        {"ref": "chair", "motif": "wood_chair", "name": "实木书桌椅", "relation": "near", "anchor_ref": "desk"},
        {"ref": "mirror", "motif": "standing_mirror", "name": "立式穿衣镜", "relation": "in_corner"},
        {"ref": "books", "motif": "book_stack", "name": "枕边闲读诗集", "relation": "on_surface", "anchor_ref": "desk"},
        {"ref": "plant", "blueprint_id": "default_plant", "name": "绿萝小盆栽", "relation": "open"},
    ],
    "study_room": [
        {"ref": "shelf", "blueprint_id": "default_bookshelf", "name": "顶天实木书柜", "relation": "against_wall"},
        {"ref": "desk", "blueprint_id": "default_desk", "name": "宽面研墨书案", "relation": "centerpiece"},
        {"ref": "chair", "motif": "wood_chair", "name": "舒适藤面扶手椅", "relation": "near", "anchor_ref": "desk"},
        {"ref": "globe", "motif": "globe_stand", "name": "铜圈旧世界地球仪", "relation": "in_corner"},
        {"ref": "lamp", "blueprint_id": "default_lamp", "name": "护眼绿罩台灯", "relation": "on_surface", "anchor_ref": "desk"},
        {"ref": "books", "motif": "book_stack", "name": "批注文献厚卷", "relation": "on_surface", "anchor_ref": "desk"},
    ],
    "onsen_bath": [
        {"ref": "tub", "motif": "hot_spring_tub", "name": "桧木温汤池", "relation": "centerpiece"},
        {"ref": "screen", "motif": "folding_screen", "name": "防风松影折屏", "relation": "against_wall"},
        {"ref": "hearth", "motif": "tea_hearth", "name": "浴后歇脚茶案", "relation": "against_wall"},
        {"ref": "wardrobe", "motif": "wardrobe", "name": "干净浴衣藤柜", "relation": "in_corner"},
        {"ref": "tea", "motif": "tea_set", "name": "玄米热茶组", "relation": "on_surface", "anchor_ref": "hearth"},
        {"ref": "candle", "motif": "candelabra", "name": "纸罩行灯", "relation": "in_corner"},
    ],
    "cellar_b1": [
        {"ref": "crate1", "motif": "crate_stack", "name": "陈酿木箱堆", "relation": "against_wall"},
        {"ref": "rack", "motif": "specimen_rack", "name": "封口瓶装酒架", "relation": "against_wall"},
        {"ref": "table", "motif": "long_table", "name": "地下盘点长桌", "relation": "centerpiece"},
        {"ref": "box", "blueprint_id": "default_box", "name": "铁箍旧箱子", "relation": "in_corner"},
        {"ref": "candle", "motif": "candelabra", "name": "地窖三叉烛台", "relation": "on_surface", "anchor_ref": "table"},
    ],
    "attic_3f": [
        {"ref": "desk", "blueprint_id": "default_desk", "name": "天窗写字桌", "relation": "against_wall"},
        {"ref": "chair", "motif": "wood_chair", "name": "藤编靠背椅", "relation": "near", "anchor_ref": "desk"},
        {"ref": "globe", "motif": "globe_stand", "name": "星象观天仪", "relation": "in_corner"},
        {"ref": "shelf", "blueprint_id": "default_bookshelf", "name": "手卷旧书架", "relation": "against_wall"},
        {"ref": "box", "blueprint_id": "default_box", "name": "封存纪念木箱", "relation": "in_corner"},
        {"ref": "candle", "motif": "candelabra", "name": "铜脚黄蜡台", "relation": "on_surface", "anchor_ref": "desk"},
    ],
    "hallway": [
        {"ref": "rack", "motif": "coat_rack", "name": "走廊衣帽架", "relation": "in_corner"},
        {"ref": "plant", "blueprint_id": "default_plant", "name": "转角常青盆栽", "relation": "against_wall"},
        {"ref": "chair", "motif": "wood_chair", "name": "走廊歇脚木椅", "relation": "against_wall"},
    ],
    "hearth_lounge": [
        {"ref": "fireplace", "motif": "fireplace", "name": "红砖暖炉壁炉", "relation": "against_wall"},
        {"ref": "sofa", "motif": "sofa_plush", "name": "围炉软垫沙发", "relation": "centerpiece"},
        {"ref": "hearth", "motif": "tea_hearth", "name": "围炉矮茶几", "relation": "near", "anchor_ref": "sofa"},
        {"ref": "tea", "motif": "tea_set", "name": "热红茶具组", "relation": "on_surface", "anchor_ref": "hearth"},
        {"ref": "plant", "blueprint_id": "default_plant", "name": "炉边常春藤盆", "relation": "in_corner"},
    ],
    "alchemy_lab": [
        {"ref": "bench", "motif": "lab_bench", "name": "蒸馏长实验台", "relation": "centerpiece"},
        {"ref": "rack", "motif": "specimen_rack", "name": "密标试剂标本柜", "relation": "against_wall"},
        {"ref": "sink", "motif": "wash_sink", "name": "器皿冲洗水槽", "relation": "against_wall"},
        {"ref": "shelf", "blueprint_id": "default_bookshelf", "name": "羊皮秘典书架", "relation": "against_wall"},
        {"ref": "candle", "motif": "candelabra", "name": "实验铜烛台", "relation": "on_surface", "anchor_ref": "bench"},
        {"ref": "books", "motif": "book_stack", "name": "配比记录厚典", "relation": "on_surface", "anchor_ref": "bench"},
    ],
    "music_salon": [
        {"ref": "piano", "motif": "grand_piano", "name": "黑漆大三角钢琴", "relation": "centerpiece"},
        {"ref": "sofa", "motif": "sofa_plush", "name": "听音丝绒沙发", "relation": "against_wall"},
        {"ref": "gram", "blueprint_id": "default_gramophone", "name": "铜喇叭留声机", "relation": "in_corner"},
        {"ref": "chair", "motif": "wood_chair", "name": "琴凳靠背椅", "relation": "near", "anchor_ref": "piano"},
        {"ref": "mirror", "motif": "standing_mirror", "name": "沙龙立式长镜", "relation": "in_corner"},
        {"ref": "vase", "motif": "flower_vase", "name": "白瓷插花座", "relation": "open"},
    ],
    "clockwork_room": [
        {"ref": "console", "motif": "gear_console", "name": "齿轮联动控制台", "relation": "centerpiece"},
        {"ref": "bench", "motif": "workbench", "name": "擒纵校表工台", "relation": "against_wall"},
        {"ref": "case", "motif": "display_case", "name": "发条零件展柜", "relation": "against_wall"},
        {"ref": "globe", "motif": "globe_stand", "name": "黄铜浑天星仪", "relation": "in_corner"},
        {"ref": "lamp", "blueprint_id": "default_lamp", "name": "工匠万向台灯", "relation": "on_surface", "anchor_ref": "bench"},
    ],
    "clinic_room": [
        {"ref": "bench", "motif": "lab_bench", "name": "药剂调配长台", "relation": "against_wall"},
        {"ref": "rack", "motif": "specimen_rack", "name": "分类西药柜", "relation": "against_wall"},
        {"ref": "screen", "motif": "folding_screen", "name": "白布诊察屏风", "relation": "centerpiece"},
        {"ref": "sink", "motif": "wash_sink", "name": "诊室消毒洗手台", "relation": "in_corner"},
        {"ref": "desk", "blueprint_id": "default_desk", "name": "门诊病历桌", "relation": "open"},
        {"ref": "chair", "motif": "wood_chair", "name": "候诊木椅", "relation": "near", "anchor_ref": "desk"},
        {"ref": "lamp", "blueprint_id": "default_lamp", "name": "暖光诊断台灯", "relation": "on_surface", "anchor_ref": "desk"},
    ],
    "workshop_room": [
        {"ref": "bench", "motif": "workbench", "name": "虎钳重木工台", "relation": "centerpiece"},
        {"ref": "rack", "motif": "specimen_rack", "name": "五金螺钉抽屉架", "relation": "against_wall"},
        {"ref": "crate", "motif": "crate_stack", "name": "木料零件木箱", "relation": "in_corner"},
        {"ref": "sink", "motif": "wash_sink", "name": "清灰洗手槽", "relation": "against_wall"},
        {"ref": "box", "blueprint_id": "default_box", "name": "铁皮工具箱", "relation": "in_corner"},
        {"ref": "lamp", "blueprint_id": "default_lamp", "name": "工作长臂台灯", "relation": "on_surface", "anchor_ref": "bench"},
    ],
}

_WALL_DECOR_BY_KIND: dict[str, tuple[str, ...]] = {
    "cafe_front": ("menu_chalkboard", "wall_sconce", "default_wall_clock"),
    "ramen_front": ("menu_chalkboard", "wall_sconce", "wall_shelf"),
    "shop_front": ("menu_chalkboard", "wall_shelf", "wall_sconce"),
    "bookshop_front": ("wall_painting", "wall_sconce", "default_wall_clock"),
    "kitchen_back": ("wall_shelf", "wall_sconce"),
    "bedroom": ("wall_painting", "wall_sconce"),
    "study_room": ("wall_painting", "default_wall_clock", "wall_sconce"),
    "hearth_lounge": ("wall_painting", "wall_sconce", "default_wall_clock"),
    "cellar_b1": ("wall_sconce", "wall_shelf"),
    "attic_3f": ("wall_sconce", "wall_painting", "default_wall_clock"),
}

_CLUTTER_BY_KIND: dict[str, tuple[str, ...]] = {
    "cafe_front": ("default_coffee_maker", "default_hot_drink", "default_cake", "default_snack", "default_lamp", "flower_vase"),
    "ramen_front": ("default_hot_drink", "tea_set", "default_snack", "default_lamp"),
    "shop_front": ("default_snack", "tea_set", "default_plant", "default_lamp"),
    "bookshop_front": ("book_stack", "default_lamp", "tea_set", "default_notebook"),
    "kitchen_back": ("tea_set", "default_snack", "default_hot_drink", "default_plant"),
    "bedroom": ("default_lamp", "book_stack", "tea_set", "default_plant", "flower_vase"),
    "study_room": ("default_lamp", "book_stack", "default_notebook", "tea_set", "flower_vase"),
    "hearth_lounge": ("tea_set", "default_hot_drink", "book_stack", "default_snack", "default_lamp"),
    "cellar_b1": ("candelabra", "default_notebook", "tea_set"),
    "attic_3f": ("default_lamp", "book_stack", "default_notebook", "candelabra"),
    "alchemy_lab": ("default_notebook", "book_stack", "candelabra", "tea_set"),
    "music_salon": ("flower_vase", "candelabra", "book_stack", "tea_set"),
    "clockwork_room": ("default_lamp", "default_notebook", "book_stack"),
    "clinic_room": ("default_lamp", "default_notebook", "tea_set", "default_plant"),
    "workshop_room": ("default_lamp", "default_notebook", "tea_set"),
}


def _approach_cells_4(cells: Iterable[tuple[int, int]]) -> set[tuple[int, int]]:
    return {
        (x + dx, y + dy)
        for x, y in cells
        for dx, dy in ((0, 0), (1, 0), (-1, 0), (0, 1), (0, -1))
    }


def _place_spec_in_space(
    engine: Any,
    space_id: str,
    spec: Mapping[str, Any],
    x: int,
    y: int,
    *,
    all_bps: Mapping[str, Any],
    synth_bp: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    bid = str(spec.get("blueprint_id") or "").strip()
    if bid and bid in all_bps:
        ent = engine.place_from_blueprint(
            bid,
            space_id,
            x,
            y,
            delivery_mode="silent",
            note_text=str(spec.get("note_text") or ""),
        )
        if spec.get("name") or spec.get("brief"):
            ent = engine.update_entity_spec(
                ent["entity_id"],
                name=clip_text(spec.get("name"), 20) or None,
                brief=clip_text(spec.get("brief"), 96) or None,
            )
        return ent
    assert synth_bp is not None
    return engine.place_ephemeral_entity(space_id, synth_bp, x=x, y=y)


def _fill_subroom_wall_decor(
    engine: Any,
    space_id: str,
    space: Mapping[str, Any],
    *,
    rect: tuple[int, int, int, int],
    room_kind: str,
    palette_id: str,
    theme_key: str,
    north_wall_y: int,
    existing_wall_occ: set[tuple[int, int]],
    reachable_floor: set[tuple[int, int]],
    all_bps: Mapping[str, Any],
    rng: random.Random,
) -> list[dict[str, Any]]:
    """在北向墙面无窗且非门洞处点缀壁灯、挂画、菜单牌或搁板。"""
    rx, ry, rw, rh = rect
    wall_rows = int(space.get("wall_rows", 1))
    if north_wall_y < 0 or north_wall_y >= int(space["height"]) or ry <= north_wall_y:
        return []
    candidates = [
        wx for wx in range(rx, rx + rw)
        if (wx, north_wall_y) not in existing_wall_occ
        and engine.is_wall_cell(space, wx, north_wall_y)
        and engine.get_cell_tile(space, wx, north_wall_y).get("tile_id") != "glass_wall"
        and (wx, north_wall_y + 1) in reachable_floor
    ]
    if not candidates:
        return []
    if len(candidates) >= 3:
        picked_xs = [candidates[0], candidates[-1]]
        if len(candidates) >= 5:
            picked_xs.append(candidates[len(candidates) // 2])
    else:
        picked_xs = [candidates[0]]

    motifs = _WALL_DECOR_BY_KIND.get(room_kind, ("wall_sconce", "wall_painting", "default_wall_clock"))
    placed: list[dict[str, Any]] = []
    for idx, wx in enumerate(picked_xs[:3]):
        # Prefer sconces at outer slots and focal paintings/menus in center slot.
        key = motifs[idx % len(motifs)] if idx == 0 or key_is_non_wall_clock(motifs, idx) else motifs[0]
        if len(picked_xs) >= 3 and idx == 1:
            key = "wall_sconce"
        spec: dict[str, Any] = {"blueprint_id": key} if key in all_bps else {"motif": key}
        if key == "wall_sconce":
            spec["relation"] = "wall_hang"
        try:
            synth = None if "blueprint_id" in spec else synthesize_furniture_blueprint(
                spec, palette_id=palette_id, theme_key=theme_key,
            )
            # Note: validate_town_layout skips placement=='wall' unconditionally, keeping subroom BFS valid even on interior north walls when y >= wall_rows.
            if wall_rows == 0:
                continue
            ent = _place_spec_in_space(engine, space_id, spec, wx, north_wall_y, all_bps=all_bps, synth_bp=synth)
            existing_wall_occ.add((wx, north_wall_y))
            placed.append(ent)
        except ValueError:
            continue
    return placed


def key_is_non_wall_clock(motifs: Sequence[str], idx: int) -> bool:
    return motifs[idx % len(motifs)] != "default_wall_clock" or idx <= 2


def _saturate_subroom_clutter(
    engine: Any,
    space_id: str,
    *,
    room_kind: str,
    palette_id: str,
    theme_key: str,
    surface_records: list[dict[str, Any]],
    sub_reachable: set[tuple[int, int]],
    all_bps: Mapping[str, Any],
    note_text: str,
    rng: random.Random,
) -> list[dict[str, Any]]:
    """将已放置家具上的空闲桌面格子填充到约 65%~80% 饱和度，消除空桌子感。"""
    free_slots: list[tuple[dict[str, Any], int, int]] = []
    total_cap = 0
    used_total = 0
    for rec in surface_records:
        cap = int(rec.get("capacity") or 2)
        cells = [(int(cx), int(cy)) for cx, cy in rec.get("cells") or []]
        # Effective slots on a table/counter cannot exceed its tile cell count without overlapping z=1 sprites.
        cell_cap = min(cap, len(cells))
        total_cap += cell_cap
        used_set: set[tuple[int, int]] = rec["used"]
        used_total += len(used_set)
        for cx, cy in cells:
            if (cx, cy) not in used_set and (_approach_cells_4([(cx, cy)]) & sub_reachable):
                free_slots.append((rec, cx, cy))

    if not free_slots or total_cap <= 0:
        return []

    target_total = min(total_cap, max(used_total + 1, round(total_cap * 0.75)))
    need = max(0, target_total - used_total)
    if need <= 0:
        return []

    rng.shuffle(free_slots)
    clutter_pool = list(_CLUTTER_BY_KIND.get(room_kind, ("default_lamp", "tea_set", "book_stack", "default_plant", "flower_vase")))
    placed: list[dict[str, Any]] = []
    placed_note = False
    cycle_idx = 0
    for rec, cx, cy in free_slots:
        if len(placed) >= need:
            break
        if len(rec["used"]) >= int(rec.get("capacity") or 2):
            continue
        if note_text and not placed_note:
            spec: dict[str, Any] = {
                "blueprint_id": "default_notebook",
                "name": "货品账本" if room_kind in ("shop_front", "storage_room") else "室内记事簿",
                "note_text": note_text,
            }
            placed_note = True
        else:
            key = clutter_pool[cycle_idx % len(clutter_pool)]
            cycle_idx += 1
            spec = {"blueprint_id": key} if key in all_bps else {"motif": key}
        synth = None if "blueprint_id" in spec else synthesize_furniture_blueprint(
            spec, palette_id=palette_id, theme_key=theme_key,
        )
        try:
            ent = _place_spec_in_space(engine, space_id, spec, cx, cy, all_bps=all_bps, synth_bp=synth)
            rec["used"].add((cx, cy))
            placed.append(ent)
        except ValueError:
            continue
    return placed


def populate_subroom_interior(
    engine: Any,
    space_id: str,
    *,
    rect: tuple[int, int, int, int],
    room_kind: str,
    room_name: str = "",
    palette_id: str = "warm_stardew",
    theme_key: str = "",
    floor_tile: str = "wood_floor",
    rug_tile: str = "carpet_blue",
    bed_count: int = 0,
    custom_items: Sequence[Mapping[str, Any]] = (),
    note_text: str = "",
    door_clear_cells: Iterable[tuple[int, int]] = (),
    stair_cells: Iterable[tuple[int, int]] = (),
    north_wall_y: int = 0,
    allow_wall_hang: bool = True,
    used_landmarks: set[str] | None = None,
    all_bps: Mapping[str, Any] | None = None,
    seed: int = 0,
) -> dict[str, Any]:
    """在指定隔间矩形 rect=(rx, ry, rw, rh) 内高密度无阻断布置主题家具、壁饰与桌面杂物。"""
    space, space_entities = _read_space_and_entities(engine, space_id)
    w, h = int(space["width"]), int(space["height"])
    wall_rows = int(space.get("wall_rows", 1))
    rx, ry, rw, rh = [int(v) for v in rect]
    resolved_theme = theme_key or infer_theme_key(f"{room_name} {room_kind}", palette_id)
    rng = random.Random(int(seed) if seed else (zlib.crc32(f"{space_id}|{rx},{ry},{rw},{rh}|{room_kind}".encode("utf-8"))))
    resolved_bps = all_bps if all_bps is not None else engine.all_blueprints(space_id, include_ephemeral=False)
    all_bps = resolved_bps

    sub_all = {(x, y) for y in range(max(wall_rows, ry), min(h, ry + rh)) for x in range(max(0, rx), min(w, rx + rw))}
    sub_walkable = {
        (x, y) for (x, y) in sub_all
        if not engine.is_wall_cell(space, x, y) and engine.get_cell_tile(space, x, y).get("walkable", True)
    }

    existing_ground: set[tuple[int, int]] = set()
    existing_solid: set[tuple[int, int]] = set()
    existing_wall_occ: set[tuple[int, int]] = set()
    surface_records: list[dict[str, Any]] = []
    for ent in space_entities:
        ex, ey = int(ent["x"]), int(ent["y"])
        fw, fh = ent.get("footprint") or [1, 1]
        ez = int(ent.get("z", 0))
        cells = {(ex + dx, ey + dy) for dx in range(int(fw)) for dy in range(int(fh))}
        if ez == 0:
            if any(cy < wall_rows or engine.is_wall_cell(space, cx, cy) for cx, cy in cells):
                existing_wall_occ.update(cells)
            else:
                existing_ground.update(cells)
                if ent.get("solid"):
                    existing_solid.update(cells)
            if "surface" in (ent.get("traits") or {}) and (cells & sub_all):
                surface_records.append({
                    "ref": f"existing_{ent['entity_id']}",
                    "entity": ent,
                    "cells": [(ex + dx, ey + dy) for dx in range(int(fw)) for dy in range(int(fh))],
                    "capacity": int((ent["traits"]["surface"] or {}).get("capacity") or 4),
                    "used": set(),
                })

    clear_set = {tuple(c) for c in door_clear_cells}
    stair_set = {tuple(c) for c in stair_cells}
    reserved_floor = (clear_set | stair_set) & sub_all

    # Paint a cozy inset accent rug inside rooms >= 4x4 (never touching doors, stairs, or walls).
    if rug_tile and rug_tile != floor_tile and rw >= 4 and rh >= 4:
        rug_ops = [
            {"x": x, "y": y, "tile_id": rug_tile}
            for y in range(ry + 1, ry + rh - 1)
            for x in range(rx + 1, rx + rw - 1)
            if (x, y) in sub_walkable and (x, y) not in stair_set and (x, y) not in clear_set
        ]
        if rug_ops:
            engine.paint_tiles(space_id, rug_ops)
            space, _ = _read_space_and_entities(engine, space_id)

    outside_block = {
        (x, y)
        for y in range(wall_rows, h)
        for x in range(w)
        if (x, y) not in sub_walkable
    }
    solid_map = set(existing_solid) | outside_block
    occupied_ground = set(existing_ground) | outside_block

    seeds: list[tuple[int, int]] = []
    for pt in sorted(clear_set | stair_set):
        for nx, ny in ((pt[0], pt[1]), (pt[0], pt[1] - 1), (pt[0], pt[1] + 1), (pt[0] - 1, pt[1]), (pt[0] + 1, pt[1])):
            if (nx, ny) in sub_walkable and (nx, ny) not in solid_map:
                seeds.append((nx, ny))
    if not seeds:
        for pt in sorted(sub_walkable - solid_map):
            seeds.append(pt)
            break
    reach_seeds = seeds[:1]
    sub_reachable = _bfs_reachable_set(w, h, wall_rows, solid_map, reach_seeds, engine=engine, space=space)

    placed_entities: list[dict[str, Any]] = []
    placed_refs: dict[str, dict[str, Any]] = {}
    dropped: list[str] = []
    item_approaches: list[set[tuple[int, int]]] = []
    sub_corners = [(rx, ry), (rx + rw - 1, ry), (rx, ry + rh - 1), (rx + rw - 1, ry + rh - 1)]

    def _is_against_sub_wall(x: int, y: int, fw: int, fh: int, max_dist: int = 0) -> bool:
        return (
            x <= rx + max_dist
            or y <= ry + max_dist
            or (x + fw - 1) >= (rx + rw - 1 - max_dist)
            or (y + fh - 1) >= (ry + rh - 1 - max_dist)
        )

    def _in_sub_corner(x: int, y: int, fw: int, fh: int, max_dist: int = 1) -> bool:
        pts = [(x, y), (x + fw - 1, y), (x, y + fh - 1), (x + fw - 1, y + fh - 1)]
        return any(max(abs(px - cx), abs(py - cy)) <= max_dist for px, py in pts for cx, cy in sub_corners)

    def _can_place(x: int, y: int, fw: int, fh: int, is_solid: bool) -> bool:
        cells = {(x + dx, y + dy) for dx in range(fw) for dy in range(fh)}
        if not cells <= sub_walkable:
            return False
        if cells & occupied_ground or cells & reserved_floor:
            return False
        if len(cells) > 1:
            elevs = {engine.get_cell_elevation(space, cx, cy) for cx, cy in cells}
            if len(elevs) > 1:
                return False
        if not is_solid:
            return bool(_approach_cells_4(cells) & sub_reachable)
        trial_solid = solid_map | cells
        after = _bfs_reachable_set(w, h, wall_rows, trial_solid, reach_seeds, engine=engine, space=space)
        if after != (sub_reachable - cells):
            return False
        if not (_approach_cells_4(cells) & after):
            return False
        if any(not (app & after) for app in item_approaches):
            return False
        for pt in clear_set & sub_walkable:
            if pt not in after and not any((pt[0] + ox, pt[1] + oy) in after for ox, oy in ((1, 0), (-1, 0), (0, 1), (0, -1))):
                return False
        return True

    def _register_ground(ent: dict[str, Any], ref: str, gx: int, gy: int, fw: int, fh: int) -> None:
        nonlocal sub_reachable
        cells = {(gx + ox, gy + oy) for ox in range(fw) for oy in range(fh)}
        occupied_ground.update(cells)
        item_approaches.append(_approach_cells_4(cells))
        if ent.get("solid"):
            solid_map.update(cells)
            sub_reachable = _bfs_reachable_set(w, h, wall_rows, solid_map, reach_seeds, engine=engine, space=space)
        if "surface" in (ent.get("traits") or {}):
            surface_records.append({
                "ref": ref or ent["entity_id"],
                "entity": ent,
                "cells": [(gx + ox, gy + oy) for ox in range(fw) for oy in range(fh)],
                "capacity": int((ent["traits"]["surface"] or {}).get("capacity") or 4),
                "used": set(),
            })
        if ref:
            placed_refs[ref] = ent
        placed_entities.append(ent)

    def _place_item_subroom(spec: Mapping[str, Any]) -> bool:
        bid = str(spec.get("blueprint_id") or "").strip()
        relation = str(spec.get("relation") or "open")
        ref = str(spec.get("ref") or "")
        anchor_ref = str(spec.get("anchor_ref") or "")
        if bid and bid in all_bps:
            bp = all_bps[bid]
            fw, fh = [clamp_int(v, 1, 4, 1) for v in (bp.get("footprint") or [1, 1])]
            is_solid = bool(bp.get("solid", False))
            placement_mode = str(bp.get("placement") or "floor")
            synth_bp = None
        else:
            motif_cand = _match_motif_for_custom(spec, resolved_theme)
            if used_landmarks is not None and motif_cand in HERO_LANDMARK_MOTIFS:
                if motif_cand in used_landmarks:
                    return False
            synth_bp = synthesize_furniture_blueprint(spec, palette_id=palette_id, theme_key=resolved_theme)
            fw, fh = synth_bp["footprint"]
            is_solid = bool(synth_bp["solid"])
            placement_mode = str(synth_bp["placement"])
            if used_landmarks is not None and motif_cand in HERO_LANDMARK_MOTIFS:
                spec_motif = motif_cand
            else:
                spec_motif = ""
        if bid or not isinstance(spec, Mapping):
            spec_motif = ""
        elif "synth_bp" in locals() and synth_bp is not None:
            spec_motif = _match_motif_for_custom(spec, resolved_theme)

        if relation == "on_surface" and surface_records:
            ordered = list(surface_records)
            if anchor_ref and anchor_ref in placed_refs:
                aid = placed_refs[anchor_ref]["entity_id"]
                ordered.sort(key=lambda r: 0 if r["entity"]["entity_id"] == aid else 1)
            for rec in ordered:
                if len(rec["used"]) >= rec["capacity"] or fw != 1 or fh != 1:
                    continue
                for sx, sy in rec["cells"]:
                    if (sx, sy) in rec["used"] or not (_approach_cells_4([(sx, sy)]) & sub_reachable):
                        continue
                    try:
                        ent = _place_spec_in_space(engine, space_id, spec, sx, sy, all_bps=all_bps, synth_bp=synth_bp)
                        rec["used"].add((sx, sy))
                        if ref:
                            placed_refs[ref] = ent
                        placed_entities.append(ent)
                        if used_landmarks is not None and spec_motif in HERO_LANDMARK_MOTIFS:
                            used_landmarks.add(spec_motif)
                        return True
                    except ValueError:
                        continue
            if placement_mode == "surface_only":
                return False

        if placement_mode == "surface_only" or relation == "wall_hang" or placement_mode == "wall":
            return False

        coords = [(x, y) for y in range(ry, ry + rh - fh + 1) for x in range(rx, rx + rw - fw + 1)]
        anchor_ent = placed_refs.get(anchor_ref)
        cx_mid = rx + (rw - 1) / 2.0
        cy_mid = ry + (rh - 1) / 2.0
        chosen: tuple[int, int] | None = None
        for stage in (0, 1, 2):
            cands: list[tuple[int, int]] = []
            for x, y in coords:
                if stage == 2:
                    cands.append((x, y))
                elif relation == "against_wall":
                    if _is_against_sub_wall(x, y, fw, fh, max_dist=(0 if stage == 0 else 1)):
                        cands.append((x, y))
                elif relation == "in_corner":
                    if stage == 0 and _in_sub_corner(x, y, fw, fh, max_dist=1):
                        cands.append((x, y))
                    elif stage == 1 and _is_against_sub_wall(x, y, fw, fh, max_dist=0):
                        cands.append((x, y))
                elif relation == "centerpiece":
                    max_d = 2.0 if stage == 0 else 3.5
                    if abs((x + fw / 2.0 - 0.5) - cx_mid) + abs((y + fh / 2.0 - 0.5) - cy_mid) <= max_d:
                        cands.append((x, y))
                elif relation == "near" and anchor_ent is not None:
                    ax, ay = int(anchor_ent["x"]), int(anchor_ent["y"])
                    afw, afh = [int(v) for v in (anchor_ent.get("footprint") or [1, 1])]
                    d = max(0, max(ax - (x + fw - 1), x - (ax + afw - 1)), max(ay - (y + fh - 1), y - (ay + afh - 1)))
                    if d <= (1 if stage == 0 else 3):
                        cands.append((x, y))
                else:
                    cands.append((x, y))
            if not cands:
                continue
            if relation == "centerpiece":
                cands.sort(key=lambda pt: abs((pt[0] + fw / 2.0 - 0.5) - cx_mid) + abs((pt[1] + fh / 2.0 - 0.5) - cy_mid) + rng.uniform(0.0, 0.7))
            else:
                rng.shuffle(cands)
            chosen = next((pt for pt in cands if _can_place(*pt, fw, fh, is_solid)), None)
            if chosen is not None:
                break
        if chosen is None:
            return False
        try:
            ent = _place_spec_in_space(engine, space_id, spec, *chosen, all_bps=all_bps, synth_bp=synth_bp)
        except ValueError:
            return False
        if used_landmarks is not None and spec_motif in HERO_LANDMARK_MOTIFS:
            used_landmarks.add(spec_motif)
        _register_ground(ent, ref, *chosen, fw, fh)
        return True

    # 1) Beds + bedside nightstand + lamp (never drops below requested bed_count when space fits)
    beds_placed = 0
    for bi in range(max(0, int(bed_count))):
        b_ref = f"bed_{bi}"
        if _place_item_subroom({"ref": b_ref, "blueprint_id": "default_bed", "relation": "against_wall"}):
            beds_placed += 1
            ns_ref = f"nightstand_{bi}"
            if _place_item_subroom({"ref": ns_ref, "motif": "nightstand", "relation": "near", "anchor_ref": b_ref}):
                _place_item_subroom({"ref": f"bedlamp_{bi}", "blueprint_id": "default_lamp", "relation": "on_surface", "anchor_ref": ns_ref})

    # 2) Custom scene_items + thematic pack items sorted by spatial priority
    pack = [dict(x) for x in SUBROOM_ENSEMBLE_PACKS.get(room_kind, SUBROOM_ENSEMBLE_PACKS["foyer_lounge"])]
    queue: list[dict[str, Any]] = []
    for idx, citem in enumerate(custom_items):
        if isinstance(citem, Mapping):
            queue.append(_sanitize_custom_scene_item(citem, idx, resolved_theme))
    queue.extend(pack)
    queue.sort(key=lambda it: _relation_priority(str(it.get("relation") or "open")))

    for spec in queue:
        ok = _place_item_subroom(spec)
        if not ok:
            dropped.append(str(spec.get("ref") or spec.get("blueprint_id") or spec.get("name") or "item"))

    # 3) Wall decor layer along north boundary
    wall_ents: list[dict[str, Any]] = []
    if allow_wall_hang:
        wall_ents = _fill_subroom_wall_decor(
            engine,
            space_id,
            space,
            rect=rect,
            room_kind=room_kind,
            palette_id=palette_id,
            theme_key=resolved_theme,
            north_wall_y=north_wall_y,
            existing_wall_occ=existing_wall_occ,
            reachable_floor=sub_reachable,
            all_bps=all_bps,
            rng=rng,
        )
        placed_entities.extend(wall_ents)

    # 4) Tabletop clutter saturation (~65-80% across available surfaces)
    clutter_ents = _saturate_subroom_clutter(
        engine,
        space_id,
        room_kind=room_kind,
        palette_id=palette_id,
        theme_key=resolved_theme,
        surface_records=surface_records,
        sub_reachable=sub_reachable,
        all_bps=all_bps,
        note_text=note_text,
        rng=rng,
    )
    placed_entities.extend(clutter_ents)

    total_surf = sum(min(int(r.get("capacity") or 2), len(r.get("cells") or [])) for r in surface_records)
    used_surf = sum(len(r["used"]) for r in surface_records)
    return {
        "placed_entities": placed_entities,
        "dropped": dropped,
        "beds_placed": beds_placed,
        "wall_decor_count": len(wall_ents),
        "clutter_count": len(clutter_ents),
        "surface_cells_total": total_surf,
        "surface_cells_used": used_surf,
    }


__all__ = [
    "FURNITURE_MOTIFS",
    "HERO_LANDMARK_MOTIFS",
    "SUBROOM_ENSEMBLE_PACKS",
    "compile_scene_graphs_batch",
    "design_room_scene_graph",
    "infer_theme_key",
    "populate_subroom_interior",
    "solve_and_place_scene_graph",
    "synthesize_furniture_blueprint",
]
