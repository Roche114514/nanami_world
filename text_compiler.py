"""「以文生像素」造物编译器：仅靠纯文本 LLM 一次产出智能物件规范与受限色槽像素矩阵。

零 VLM、零生图模型、零本地显存消耗；任何阶段异常均 fail-open 退回规则启发式造物。
"""

from __future__ import annotations

import json
import logging
import re
import threading
from collections.abc import Mapping
from typing import Any, Callable

from .schema import (
    DEFAULT_ACTOR_PIXELS_16X24,
    DEFAULT_ACTOR_SLOT_COLORS,
    DEFAULT_SLOT_COLORS,
    MAX_SPACE_HEIGHT,
    MAX_SPACE_WIDTH,
    MIN_SPACE_HEIGHT,
    MIN_SPACE_WIDTH,
    TILE_CATALOG,
    clamp_int,
    clip_text,
    normalize_blueprint,
    normalize_pixel_grid,
    normalize_slot_colors,
)

logger = logging.getLogger(__name__)

_THINK_RE = re.compile(r"<think>[\s\S]*?</think>", re.IGNORECASE)
_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE | re.MULTILINE)

_ITEM_SYSTEM_PROMPT = """你是像素风文字世界的造物编译器。用户输入一句物品描述，你必须只输出一个合法 JSON 对象（不要 markdown 围栏、不要解释），同时完成：
1. 规范化物品属性（客观白描 + 正交能力组件 traits + 需求效应 effects）；
2. 用受限 7 色槽画一张清晰的 16x16（或指定尺寸）透明底像素图（必须给出 1-2 个状态帧）。

受限色槽字符规范（每行长度必须严格等于宽度，只能用下面 7 个字符）：
- `.` 透明背景
- `o` 深色外描边
- `1` 物品固有主色
- `2` 受光高光
- `3` 暗部阴影
- `a` 主点缀色（如热饮液体、灯光、屏幕、果酱、锁孔）
- `b` 次点缀色（如热气、纸面字迹、按钮、高反光）

可用组件 traits（按需只选 1-3 个，没用到的组件不要写进 traits）：
- portable: {"weight": "light"|"heavy"}
- surface: {"capacity": 4}  （桌/架/柜顶等承载面）
- container: {"capacity": 6, "key_tag": "", "is_open": false, "is_locked": false}
- consumable: {"max_uses": 2, "uses_left": 2, "consume_verb": "喝"|"吃"|"用", "on_empty": "keep_empty"|"vanish", "empty_brief": "空了的白描"}
- thermal: {"cool_minutes": 45, "temp": "hot"|"warm"|"cold"}
- switchable: {"provides_light": true|false, "is_on": false, "on_brief": "开启时白描", "off_brief": "关闭时白描"}
- restable: {"energy_regen_per_min": 0.015, "comfort": 0.8, "occupied_by": null}
- activity: {"domain": "game"|"reading"|"music"|"hobby", "progress_unit": "关"|"页"|"首"|"次", "progress": 0, "battery": null|100, "battery_cost": 10}
- inscribable: {"writable_by_ai": true|false, "max_entries": 12, "entries": []}
- portal: {"target_space_id": "", "target_x": 5, "target_y": 2, "is_open": true}

参考范例 1（热抹茶杯，16x16）：
{"name":"热抹茶杯","brief":"一只粗陶小杯，装着热抹茶","detail":"绿釉粗陶杯温热着，杯面还浮着一层细腻茶沫。","tags":["drink","cozy"],"footprint":[1,1],"placement":"any","solid":false,"traits":{"portable":{"weight":"light"},"consumable":{"max_uses":2,"uses_left":2,"consume_verb":"喝","on_empty":"keep_empty","empty_brief":"喝空的绿釉粗陶杯"},"thermal":{"cool_minutes":45,"temp":"hot"}},"effects":{"energy_delta":0.06,"sleep_pressure_delta":-0.02,"mood_v_delta":0.07,"satisfies":["warmth","comfort"],"domain":null},"sprite":{"w":16,"h":16,"slot_colors":{"o":"#21181b","1":"#7a9c6d","2":"#b4d4a5","3":"#4d6643","a":"#5ec46a","b":"#e6f2ff"},"frames":{"default":[".....b....b.....","....b....b......","................","...ooooooooo....","..o2aaaaaaa1o...","..o1ooooooo1oo..","..o111111111o1o.","..o111221111o.o.","..o111111111oo..","..o311111133o...","...o3333333o....","....ooooooo.....","................","................","................","................"],"empty":["................","................","................","...ooooooooo....","..o233333331o...","..o1ooooooo1oo..","..o111111111o1o.","..o111221111o.o.","..o111111111oo..","..o311111133o...","...o3333333o....","....ooooooo.....","................","................","................","................"]}}}

参考范例 2（香薰小夜灯，16x16）：
{"name":"玻璃小夜灯","brief":"一盏琥珀色玻璃小夜灯","detail":"圆圆的磨砂玻璃罩，底座是深色木头。","tags":["light","cozy"],"footprint":[1,1],"placement":"any","solid":false,"traits":{"portable":{"weight":"light"},"switchable":{"provides_light":true,"is_on":false,"on_brief":"透着柔光的玻璃小夜灯","off_brief":"灭着的玻璃小夜灯"}},"effects":{"energy_delta":0.0,"sleep_pressure_delta":0.0,"mood_v_delta":0.05,"satisfies":["safety","cozy"],"domain":null},"sprite":{"w":16,"h":16,"slot_colors":{"o":"#21181b","1":"#d9a773","2":"#ffe2ba","3":"#8f6239","a":"#ffda6b","b":"#6b4b33"},"frames":{"off":["................","......oooo......","....oo2211oo....","...o22111133o...","...o21111113o...","...o11111113o...","....o333333o....","....obbbbbbo....","...oobbbbbboo...","...oooooooooo...","................","................","................","................","................","................"],"on":["................","......oooo......","....ooaaaaoo....","...o2aaaaaaaao..","...o2aaaaaaaao..","...oaaaaaaaaao..","....oaaaaaaao...","....obbbbbbo....","...oobbbbbboo...","...oooooooooo...","................","................","................","................","................","................"]}}}

注意：
- brief 严格不超过 25 个字，客观白描，不要写成诗或系统提示；
- detail 严格不超过 60 个字；
- frames 至少包含 "default"（或对应状态名）一帧，每帧行数严格等于 h、每行字符数严格等于 w。"""

_ACTOR_SYSTEM_PROMPT = """你是像素画师。请根据角色名称和外貌描述，只输出一个合法 JSON 对象，画一张 16x24 透明底静态像素小人（头身比 Q 版正面立绘）。
只能使用 7 个字符：`.` 透明、`o` 深色描边、`1` 主发色或衣服主色、`2` 高光、`3` 暗部鞋袜/阴影、`a` 点缀色（发卡/领结/装饰）、`b` 肤色。
输出格式：
{"accent_color":"#e8a2b8","slot_colors":{"o":"#201721","1":"#c9a8b8","2":"#ecd5e0","3":"#4f5666","a":"#84b89a","b":"#ffe2cf"},"pixels":["...16字符共24行..."]}"""

_MAP_SYSTEM_PROMPT = """你是像素风文字世界的空间建筑师。请根据用户的场景描述与指定尺寸，只输出一个合法 JSON 对象（不要 markdown 围栏、不要多余解释），构造出可直接游玩和搭建的二维离散格网地图。

可用地形/墙体方块 tile_id：
- 室内地板：wood_floor（暖木地板）、dark_wood（胡桃木板）、tatami（日式榻榻米）、carpet_pink（樱粉软毯）、carpet_blue（星夜地毯）、stone_tile（复古石砖）
- 户外地形：cobble（鹅卵石道）、grass（青翠草地）、flower_grass（繁花草坪）、sand（暖金沙地）、water（池水，不可行走）、hedge（灌木篱笆，不可行走）
- 墙体隔断（is_wall=true，可挂墙物）：brick_wall（红砖墙）、wood_wall（木质墙）、white_wall（奶油白墙）、glass_wall（玻璃隔断）

可用调色板 palette_id：warm_stardew, cream_pastel, cyber_game, zen_moss, late_night_neon

JSON 结构规范：
{
  "name": "空间名称(<=12字)",
  "width": 16,
  "height": 12,
  "palette_id": "warm_stardew",
  "ambient_phrase": "一句话环境白描(<=55字)",
  "base_tile": "wood_floor",
  "tile_regions": [
    {"tile_id": "wood_wall", "x": 0, "y": 0, "w": 16, "h": 1, "hollow": false},
    {"tile_id": "tatami", "x": 1, "y": 2, "w": 6, "h": 5, "hollow": false},
    {"tile_id": "brick_wall", "x": 8, "y": 2, "w": 6, "h": 6, "hollow": true}
  ],
  "placements": [
    {"blueprint_id": "default_desk", "x": 3, "y": 3},
    {"blueprint_id": "default_hot_drink", "x": 3, "y": 3}
  ],
  "custom_blueprints": []
}

设计规则：
1. 若用 hollow=true 围出隔断墙，务必给房间留出门洞，不要把角色或中心封闭死；
2. 只能使用用户提示里列出的可用 blueprint_id（如缺特别关键物件，最多在 custom_blueprints 中新造 1-2 个 16x16 蓝图）；
3. placement="wall" 的挂钟/房门/海报要放在墙体格（如 y=0 或内墙格）；2x1 桌子（如 default_desk）放后，小物可放在同坐标叠上桌面（z=1）；
4. 预留足够的落脚通路（建议放置 7-14 件错落有致的物件）。"""


def _default_adapter() -> Any:
    try:
        from application.runtime.context import try_get_app_runtime

        runtime = try_get_app_runtime()
        manager = getattr(runtime, "llm_manager", None) if runtime is not None else None
        adapter = getattr(manager, "llm_adapter", None)
        if adapter is not None:
            return adapter
    except Exception:
        pass

    try:
        from ai.llm.adapter_factory import LLMAdapterFactory
        from config.config_manager import ConfigManager

        manager = ConfigManager()
        provider, model, base_url, api_key = manager.get_llm_api_config()
        if not provider:
            return None
        return LLMAdapterFactory.create_adapter(
            **manager.merged_llm_factory_kwargs(
                provider,
                {"llm_provider": provider, "api_key": api_key, "base_url": base_url, "model": model},
            )
        )
    except Exception:
        return None


def _extract_json_object(raw_text: str) -> dict[str, Any] | None:
    text = _THINK_RE.sub("", str(raw_text or "")).strip()
    text = _FENCE_RE.sub("", text).strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except ValueError:
        pass

    start = text.find("{")
    while start >= 0:
        depth = 0
        for idx in range(start, len(text)):
            ch = text[idx]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        parsed = json.loads(text[start : idx + 1])
                        if isinstance(parsed, dict):
                            return parsed
                    except ValueError:
                        break
        start = text.find("{", start + 1)
    return None


def _ask_llm_json(
    system_prompt: str,
    user_prompt: str,
    *,
    adapter_provider: Callable[[], Any] | None = None,
    timeout_sec: float = 12.0,
    max_tokens: int = 900,
) -> dict[str, Any] | None:
    adapter = (adapter_provider or _default_adapter)()
    if adapter is None:
        return None
    box: dict[str, Any] = {}
    done = threading.Event()

    def _work() -> None:
        try:
            resp = adapter.chat(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                stream=False,
                temperature=0.35,
                max_tokens=max_tokens,
                response_format={"type": "json_object"},
            )
            content = str(getattr(adapter.decode_response(resp), "content", "") or "")
            box["parsed"] = _extract_json_object(content)
        except Exception as exc:
            box["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            done.set()

    thread = threading.Thread(target=_work, name="nanami-world-compiler", daemon=True)
    thread.start()
    if not done.wait(timeout_sec):
        logger.info("nanami_world: 文字造物 LLM 超时（%.1fs），切换规则兜底", timeout_sec)
        return None
    if box.get("error"):
        logger.info("nanami_world: 文字造物 LLM 调用失败（%s），切换规则兜底", box["error"])
        return None
    parsed = box.get("parsed")
    return parsed if isinstance(parsed, dict) else None


def _generic_pixel_box(w: int, h: int, shape: str = "box") -> list[str]:
    if shape == "cup":
        rows = [
            "................",
            "................",
            "................",
            "...oooooooooo...",
            "..o2aaaaaaaa1o..",
            "..o1oooooooo1oo.",
            "..o111122111o1o.",
            "..o111111111o1o.",
            "..o111111111oo..",
            "..o311111133o...",
            "...o3333333o....",
            "....ooooooo.....",
            "................",
            "................",
            "................",
            "................",
        ]
        return normalize_pixel_grid(rows, w, h, align_bottom=True)
    if shape == "lamp":
        rows = [
            "................",
            ".....oooooo.....",
            "....o22aa11o....",
            "...o22aaaa33o...",
            "..o22aaaaaa33o..",
            "..oooooooooooo..",
            "......o33o......",
            "......o33o......",
            "......o33o......",
            ".....oo33oo.....",
            "....oooooooo....",
            "................",
            "................",
            "................",
            "................",
            "................",
        ]
        return normalize_pixel_grid(rows, w, h, align_bottom=True)
    if shape == "book":
        rows = [
            "................",
            "...oooooooooo...",
            "..o3o22222222o..",
            "..o3o111aa111o..",
            "..o3o11bbbb11o..",
            "..o3o11111111o..",
            "..o3o11bbbb11o..",
            "..o3o11111111o..",
            "..o3o11111111o..",
            "..o3o33333333o..",
            "...oooooooooo...",
            "................",
            "................",
            "................",
            "................",
            "................",
        ]
        return normalize_pixel_grid(rows, w, h, align_bottom=True)
    # 默认小方块物件图标
    grid = []
    for y in range(h):
        line = []
        for x in range(w):
            if 3 <= x < w - 3 and 4 <= y < h - 2:
                if x in (3, w - 4) or y in (4, h - 3):
                    line.append("o")
                elif y == 5:
                    line.append("2")
                elif y >= h - 5:
                    line.append("3")
                elif x in (w // 2 - 1, w // 2) and y in (h // 2, h // 2 + 1):
                    line.append("a")
                else:
                    line.append("1")
            else:
                line.append(".")
        grid.append("".join(line))
    return grid


def _fallback_item_from_text(prompt_text: str, preferred_size: tuple[int, int]) -> dict[str, Any]:
    text = str(prompt_text or "").strip() or "神秘小物件"
    name = clip_text(re.split(r"[，,。！？\s]+", text)[-1], 8, "") or clip_text(text, 8, "自定义物件")
    if len(name) > 8:
        name = name[-8:]
    w, h = preferred_size
    traits: dict[str, Any] = {}
    tags: list[str] = ["custom"]
    footprint = [1, 1]
    placement = "any"
    solid = False
    shape = "box"
    slot_colors = dict(DEFAULT_SLOT_COLORS)

    if any(k in text for k in ("桌", "台", "柜台", "架")):
        traits["surface"] = {"capacity": 4}
        footprint = [2, 1] if w >= 24 else [1, 1]
        placement = "floor"
        solid = True
        tags.append("furniture")
    elif any(k in text for k in ("沙发", "床", "榻", "毯", "椅")):
        traits["restable"] = {"energy_regen_per_min": 0.015, "comfort": 0.8, "occupied_by": None}
        footprint = [2, 2] if w >= 24 else [1, 1]
        placement = "floor"
        solid = False
        tags.append("rest")
    elif any(k in text for k in ("门", "闸", "通道")):
        traits["portal"] = {"target_space_id": "", "target_x": 5, "target_y": 2, "is_open": False}
        placement = "any"
        solid = True
        tags.append("door")
    elif any(k in text for k in ("箱", "盒", "抽屉", "柜")):
        traits["portable"] = {"weight": "light"}
        traits["container"] = {"capacity": 6, "key_tag": "brass_key" if "锁" in text else "", "is_open": False, "is_locked": "锁" in text}
        solid = True
        tags.append("storage")
    else:
        traits["portable"] = {"weight": "light"}

    if any(k in text for k in ("灯", "蜡烛", "夜灯", "台灯")):
        traits["switchable"] = {"provides_light": True, "is_on": False, "on_brief": f"亮着的{name}", "off_brief": f"灭着的{name}"}
        shape = "lamp"
        tags.append("light")
    elif any(k in text for k in ("杯", "茶", "咖啡", "可可", "水", "奶", "汤")):
        traits["consumable"] = {"max_uses": 2, "uses_left": 2, "consume_verb": "喝", "on_empty": "keep_empty", "empty_brief": f"喝空的{name}"}
        if any(k in text for k in ("热", "温", "烫")):
            traits["thermal"] = {"cool_minutes": 45, "temp": "hot"}
        shape = "cup"
        tags.append("drink")
    elif any(k in text for k in ("蛋糕", "饼", "糖", "点心", "果", "面包", "饭")):
        traits["consumable"] = {"max_uses": 2, "uses_left": 2, "consume_verb": "吃", "on_empty": "vanish", "empty_brief": "吃光了"}
        tags.append("food")
    elif any(k in text for k in ("本", "日记", "纸", "便签", "便利贴", "信")):
        traits["inscribable"] = {"writable_by_ai": True, "max_entries": 12, "entries": []}
        shape = "book"
        tags.append("paper")
    elif any(k in text for k in ("掌机", "游戏", "书", "琴", "音乐", "画")):
        domain = "game" if any(k in text for k in ("游戏", "掌机")) else ("music" if any(k in text for k in ("琴", "音乐")) else "reading")
        traits["activity"] = {"domain": domain, "progress_unit": "关" if domain == "game" else "次", "progress": 0, "battery": 80 if "电" in text or "机" in text else None, "battery_cost": 10}
        shape = "book" if domain == "reading" else "box"
        tags.append(domain)

    if any(k in text for k in ("蓝", "海", "青")):
        slot_colors.update({"1": "#5c88b8", "2": "#8eb8e5", "3": "#39597d"})
    elif any(k in text for k in ("粉", "樱", "红")):
        slot_colors.update({"1": "#cf6f87", "2": "#f2a2b6", "3": "#8c4254"})
    elif any(k in text for k in ("绿", "茶", "草")):
        slot_colors.update({"1": "#6a9e67", "2": "#9cd198", "3": "#40663e"})

    grid = _generic_pixel_box(w, h, shape=shape)
    return normalize_blueprint({
        "name": name,
        "brief": clip_text(text, 25, name),
        "detail": clip_text(text, 60, f"一件{name}。"),
        "tags": tags,
        "footprint": footprint,
        "placement": placement,
        "solid": solid,
        "traits": traits,
        "effects": {"energy_delta": 0.03 if "consumable" in traits else 0.0, "mood_v_delta": 0.05, "satisfies": tags[:2]},
        "sprite": {
            "mode": "pixel_grid",
            "w": w,
            "h": h,
            "slot_colors": slot_colors,
            "frames": {"default": grid},
        },
    })


def compile_item_from_text(
    prompt_text: str,
    *,
    preferred_size: tuple[int, int] = (16, 16),
    use_llm: bool = True,
    adapter_provider: Callable[[], Any] | None = None,
    timeout_sec: float = 12.0,
) -> dict[str, Any]:
    """把用户一句自然语言编译为规范化的物品蓝图 + 受限色槽像素贴图。"""
    clean_prompt = clip_text(prompt_text, 200, "")
    if not clean_prompt:
        raise ValueError("请输入一段物品描述。")

    pw = max(16, min(32, int(preferred_size[0])))
    ph = max(16, min(32, int(preferred_size[1])))

    if use_llm:
        user_msg = f"物品描述：{clean_prompt}\n目标像素尺寸：w={pw}, h={ph}。\n请严格只输出一个 JSON 对象。"
        parsed = _ask_llm_json(
            _ITEM_SYSTEM_PROMPT,
            user_msg,
            adapter_provider=adapter_provider,
            timeout_sec=timeout_sec,
            max_tokens=1100,
        )
        if isinstance(parsed, dict) and parsed.get("name"):
            sp_raw = parsed.get("sprite") if isinstance(parsed.get("sprite"), dict) else {}
            w = int(sp_raw.get("w") or pw)
            h = int(sp_raw.get("h") or ph)
            raw_frames = sp_raw.get("frames") if isinstance(sp_raw.get("frames"), dict) else {}
            clean_frames: dict[str, list[str]] = {}
            for fname, rows in raw_frames.items():
                if isinstance(rows, list) and rows:
                    clean_frames[str(fname)] = normalize_pixel_grid(rows, w, h, align_bottom=True, auto_outline=False)
            if not clean_frames and isinstance(sp_raw.get("pixels"), list):
                clean_frames["default"] = normalize_pixel_grid(sp_raw["pixels"], w, h, align_bottom=True, auto_outline=False)
            if not clean_frames:
                clean_frames["default"] = _generic_pixel_box(w, h)
            parsed["sprite"] = {
                "mode": "pixel_grid",
                "w": w,
                "h": h,
                "slot_colors": normalize_slot_colors(sp_raw.get("slot_colors")),
                "frames": clean_frames,
            }
            bp = normalize_blueprint(parsed, builtin=False)
            return {
                "blueprint": bp,
                "used_llm": True,
                "note": "已由纯文本大模型编译出规范属性与像素贴图。",
            }

    bp = _fallback_item_from_text(clean_prompt, (pw, ph))
    return {
        "blueprint": bp,
        "used_llm": False,
        "note": "未调用大模型（已离线/未配置或超时），已按描述关键词生成规范蓝图与基础像素框，可直接在弹层微调。",
    }


def compile_actor_sprite_from_text(
    name: str,
    prompt_text: str = "",
    *,
    use_llm: bool = True,
    adapter_provider: Callable[[], Any] | None = None,
    timeout_sec: float = 12.0,
) -> dict[str, Any]:
    """根据角色名与外貌描述编译单帧 16x24 静态像素立牌。"""
    clean_name = clip_text(name, 20, "角色")
    clean_desc = clip_text(prompt_text, 160, f"{clean_name}，日常便服")

    if use_llm:
        parsed = _ask_llm_json(
            _ACTOR_SYSTEM_PROMPT,
            f"角色名：{clean_name}\n外貌：{clean_desc}\n请只输出 JSON。",
            adapter_provider=adapter_provider,
            timeout_sec=timeout_sec,
            max_tokens=900,
        )
        if isinstance(parsed, dict) and isinstance(parsed.get("pixels"), list) and parsed["pixels"]:
            pixels = normalize_pixel_grid(parsed["pixels"], 16, 24, align_bottom=True)
            accent = str(parsed.get("accent_color") or "#e8a2b8").strip()
            if not re.fullmatch(r"#[0-9a-fA-F]{6}", accent):
                accent = "#e8a2b8"
            return {
                "mode": "pixel_grid",
                "w": 16,
                "h": 24,
                "pixels": pixels,
                "slot_colors": normalize_slot_colors(parsed.get("slot_colors"), fallback=DEFAULT_ACTOR_SLOT_COLORS),
                "accent_color": accent,
                "used_llm": True,
            }

    slot_colors = dict(DEFAULT_ACTOR_SLOT_COLORS)
    accent = "#e8a2b8"
    if any(k in clean_desc for k in ("黑发", "黑衣", "深色")):
        slot_colors.update({"1": "#3b3d4a", "2": "#66697d", "3": "#262833"})
        accent = "#7d8ba8"
    elif any(k in clean_desc for k in ("金发", "黄", "橙")):
        slot_colors.update({"1": "#d9b45f", "2": "#fae093", "3": "#997736"})
        accent = "#f0be5b"
    elif any(k in clean_desc for k in ("蓝", "青", "水")):
        slot_colors.update({"1": "#6a8ec2", "2": "#9ec0f0", "3": "#415b85"})
        accent = "#7eb8f0"
    elif any(k in clean_desc for k in ("白发", "银发", "白")):
        slot_colors.update({"1": "#d5dae3", "2": "#f2f5fa", "3": "#959eab"})
        accent = "#b8cce6"
    return {
        "mode": "pixel_grid",
        "w": 16,
        "h": 24,
        "pixels": list(DEFAULT_ACTOR_PIXELS_16X24),
        "slot_colors": slot_colors,
        "accent_color": accent,
        "used_llm": False,
    }


def _fallback_map_from_text(prompt_text: str, width: int, height: int) -> dict[str, Any]:
    text = str(prompt_text or "").strip()
    w = clamp_int(width, MIN_SPACE_WIDTH, MAX_SPACE_WIDTH, 16)
    h = clamp_int(height, MIN_SPACE_HEIGHT, MAX_SPACE_HEIGHT, 12)
    name = clip_text(re.split(r"[，,。！？\s]+", text)[0], 12, "") or "灵感新空间"

    # 主题 1：庭院 / 露营 / 花园 / 池塘
    if any(k in text for k in ("庭院", "花园", "露营", "草地", "池塘", "户外", "公园", "森林", "樱花", "营地")):
        cottage_w = max(5, min(7, w // 2))
        cottage_h = max(4, min(6, h // 2))
        regions = [
            {"tile_id": "hedge", "x": 0, "y": 0, "w": w, "h": h, "hollow": True},
            {"tile_id": "flower_grass", "x": 1, "y": 1, "w": w - 2, "h": h - 2, "hollow": False},
            {"tile_id": "cobble", "x": cottage_w, "y": h // 2, "w": max(2, w - cottage_w - 1), "h": 2, "hollow": False},
            {"tile_id": "wood_wall", "x": 0, "y": 0, "w": cottage_w, "h": cottage_h, "hollow": True},
            {"tile_id": "tatami", "x": 1, "y": 1, "w": max(2, cottage_w - 2), "h": max(2, cottage_h - 2), "hollow": False},
            {"tile_id": "wood_floor", "x": cottage_w // 2, "y": cottage_h - 1, "w": 1, "h": 1, "hollow": False},
        ]
        if w >= 12 and h >= 9:
            px, py = max(cottage_w + 1, w - 5), 2
            regions.append({"tile_id": "sand", "x": px - 1, "y": py, "w": 4, "h": 4, "hollow": False})
            regions.append({"tile_id": "water", "x": px, "y": py + 1, "w": 2, "h": 2, "hollow": False})
        placements = [
            {"blueprint_id": "default_wall_clock", "x": 1, "y": 0},
            {"blueprint_id": "default_desk", "x": 1, "y": 2},
            {"blueprint_id": "default_hot_drink", "x": 1, "y": 2},
            {"blueprint_id": "default_cat_cushion", "x": 2, "y": max(1, cottage_h - 2)},
            {"blueprint_id": "default_campfire", "x": min(w - 3, cottage_w + 2), "y": max(2, h - 3)},
            {"blueprint_id": "default_street_lamp", "x": min(w - 2, cottage_w + 1), "y": max(1, h // 2 - 1)},
            {"blueprint_id": "default_plant", "x": 1, "y": h - 2},
            {"blueprint_id": "default_wooden_chest", "x": min(w - 2, cottage_w + 4), "y": max(1, h - 3)},
            {"blueprint_id": "default_brass_key", "x": 2, "y": 2},
            {"blueprint_id": "default_snack", "x": min(w - 3, cottage_w + 3), "y": max(2, h - 3)},
        ]
        return {
            "name": name if len(name) >= 2 else "微风庭院",
            "width": w,
            "height": h,
            "wall_rows": 0,
            "palette_id": "zen_moss",
            "ambient_phrase": clip_text(text, 55, "花草环绕的小木屋庭院，池水旁立着街灯与篝火。"),
            "base_tile": "grass",
            "tile_regions": regions,
            "placements": placements,
            "custom_blueprints": [],
        }

    # 主题 2：游戏房 / 街机室 / 赛博房间
    if any(k in text for k in ("游戏", "街机", "赛博", "霓虹", "电竞", "电脑", "电玩", "玩家")):
        regions = [
            {"tile_id": "brick_wall", "x": 0, "y": 0, "w": w, "h": 1, "hollow": False},
            {"tile_id": "carpet_blue", "x": 2, "y": 2, "w": max(4, w - 4), "h": max(3, h - 4), "hollow": False},
        ]
        if w >= 14 and h >= 10:
            regions.append({"tile_id": "glass_wall", "x": w // 2, "y": 1, "w": 1, "h": max(2, h // 2 - 1), "hollow": False})
        placements = [
            {"blueprint_id": "default_window_poster", "x": 2, "y": 0},
            {"blueprint_id": "default_wall_clock", "x": max(3, w - 3), "y": 0},
            {"blueprint_id": "default_door", "x": w // 2, "y": 0},
            {"blueprint_id": "default_arcade", "x": 1, "y": 2},
            {"blueprint_id": "default_arcade", "x": 1, "y": 4},
            {"blueprint_id": "default_desk", "x": 3, "y": 2},
            {"blueprint_id": "default_pc", "x": 3, "y": 2},
            {"blueprint_id": "default_handheld", "x": 4, "y": 2},
            {"blueprint_id": "default_sofa", "x": max(3, w - 4), "y": max(3, h - 4)},
            {"blueprint_id": "default_fridge", "x": max(2, w - 2), "y": 1},
            {"blueprint_id": "default_ramen", "x": 2, "y": max(3, h - 3)},
            {"blueprint_id": "default_lamp", "x": max(2, w - 4), "y": 2},
        ]
        return {
            "name": name if len(name) >= 2 else "电玩秘密基地",
            "width": w,
            "height": h,
            "wall_rows": 1,
            "palette_id": "cyber_game",
            "ambient_phrase": clip_text(text, 55, "深木地板配星夜地毯的电玩房，街机和双屏电脑亮着微光。"),
            "base_tile": "dark_wood",
            "tile_regions": regions,
            "placements": placements,
            "custom_blueprints": [],
        }

    # 主题 3：咖啡馆 / 甜点屋 / 画室音乐厅
    if any(k in text for k in ("咖啡", "甜点", "蛋糕", "茶", "餐厅", "厨房", "画室", "音乐", "阳台")):
        regions = [
            {"tile_id": "white_wall", "x": 0, "y": 0, "w": w, "h": 1, "hollow": False},
            {"tile_id": "carpet_pink", "x": 1, "y": 2, "w": max(4, w // 2), "h": max(3, h - 4), "hollow": False},
            {"tile_id": "cobble", "x": max(2, w - 4), "y": 1, "w": 3, "h": h - 2, "hollow": False},
        ]
        placements = [
            {"blueprint_id": "default_wall_clock", "x": 2, "y": 0},
            {"blueprint_id": "default_door", "x": w - 3, "y": 0},
            {"blueprint_id": "default_desk", "x": 2, "y": 3},
            {"blueprint_id": "default_coffee_maker", "x": 2, "y": 3},
            {"blueprint_id": "default_cake", "x": 3, "y": 3},
            {"blueprint_id": "default_fridge", "x": 1, "y": 1},
            {"blueprint_id": "default_gramophone", "x": max(3, w // 2 + 1), "y": 2},
            {"blueprint_id": "default_easel", "x": max(3, w // 2 + 1), "y": max(3, h - 3)},
            {"blueprint_id": "default_sofa", "x": 2, "y": max(4, h - 3)},
            {"blueprint_id": "default_plant", "x": max(2, w - 2), "y": 2},
            {"blueprint_id": "default_street_lamp", "x": max(2, w - 2), "y": max(3, h - 3)},
        ]
        return {
            "name": name if len(name) >= 2 else "午后咖啡角",
            "width": w,
            "height": h,
            "wall_rows": 1,
            "palette_id": "cream_pastel",
            "ambient_phrase": clip_text(text, 55, "石砖配樱粉地毯的咖啡音乐角落，飘着甜点与黑胶唱片的暖意。"),
            "base_tile": "stone_tile",
            "tile_regions": regions,
            "placements": placements,
            "custom_blueprints": [],
        }

    # 主题 4：默认温馨卧室书房套间
    mid_x = max(5, w // 2)
    regions = [
        {"tile_id": "wood_wall", "x": 0, "y": 0, "w": w, "h": 1, "hollow": False},
        {"tile_id": "tatami", "x": 1, "y": 2, "w": max(3, mid_x - 2), "h": max(3, h - 4), "hollow": False},
        {"tile_id": "carpet_pink", "x": mid_x + 1, "y": 2, "w": max(3, w - mid_x - 2), "h": max(3, h - 4), "hollow": False},
        {"tile_id": "wood_wall", "x": mid_x, "y": 1, "w": 1, "h": max(2, h // 2 - 1), "hollow": False},
    ]
    placements = [
        {"blueprint_id": "default_wall_clock", "x": 2, "y": 0},
        {"blueprint_id": "default_window_poster", "x": min(w - 2, mid_x + 2), "y": 0},
        {"blueprint_id": "default_door", "x": mid_x, "y": 0},
        {"blueprint_id": "default_bed", "x": min(w - 3, mid_x + 2), "y": max(2, h - 3)},
        {"blueprint_id": "default_bookshelf", "x": 1, "y": 1},
        {"blueprint_id": "default_desk", "x": 2, "y": 3},
        {"blueprint_id": "default_lamp", "x": 2, "y": 3},
        {"blueprint_id": "default_notebook", "x": 3, "y": 3},
        {"blueprint_id": "default_cat_cushion", "x": 2, "y": max(4, h - 2)},
        {"blueprint_id": "default_plant", "x": min(w - 2, w - 2), "y": 2},
        {"blueprint_id": "default_hot_drink", "x": 1, "y": 1},
    ]
    return {
        "name": name if len(name) >= 2 else "温馨起居套间",
        "width": w,
        "height": h,
        "wall_rows": 1,
        "palette_id": "warm_stardew",
        "ambient_phrase": clip_text(text, 55, "划分出榻榻米阅读区与睡眠区的温暖套间，光线柔和安静。"),
        "base_tile": "wood_floor",
        "tile_regions": regions,
        "placements": placements,
        "custom_blueprints": [],
    }


def compile_map_from_text(
    prompt_text: str,
    *,
    width: int = 16,
    height: int = 12,
    use_llm: bool = True,
    available_blueprints: Mapping[str, Any] | None = None,
    adapter_provider: Callable[[], Any] | None = None,
    timeout_sec: float = 18.0,
) -> dict[str, Any]:
    """根据用户一句描述，由 LLM（或主题兜底）生成带地形区划与物件布局的地图方案。"""
    clean_prompt = clip_text(prompt_text, 240, "一间温馨舒适的小屋")
    w = clamp_int(width, MIN_SPACE_WIDTH, MAX_SPACE_WIDTH, 16)
    h = clamp_int(height, MIN_SPACE_HEIGHT, MAX_SPACE_HEIGHT, 12)

    if use_llm:
        bp_lines: list[str] = []
        for bid, bp in list((available_blueprints or {}).items())[:36]:
            if isinstance(bp, Mapping):
                bp_lines.append(f"- {bid}: {bp.get('name', bid)}（{bp.get('placement', 'any')}, 占地{bp.get('footprint', [1, 1])}）")
        bp_hint = "\n".join(bp_lines) if bp_lines else "- default_desk, default_lamp, default_sofa, default_bed, default_plant, default_hot_drink, default_handheld"
        user_msg = (
            f"场景需求：{clean_prompt}\n"
            f"目标地图尺寸：width={w}, height={h}\n"
            f"可用内置蓝图：\n{bp_hint}\n"
            "请严格只输出一个 JSON 对象。"
        )
        parsed = _ask_llm_json(
            _MAP_SYSTEM_PROMPT,
            user_msg,
            adapter_provider=adapter_provider,
            timeout_sec=timeout_sec,
            max_tokens=1600,
        )
        if isinstance(parsed, dict) and (parsed.get("tile_regions") or parsed.get("placements")):
            custom_bps: list[dict[str, Any]] = []
            for raw_bp in (parsed.get("custom_blueprints") or [])[:2]:
                if isinstance(raw_bp, Mapping) and raw_bp.get("name"):
                    custom_bps.append(normalize_blueprint(raw_bp, builtin=False))

            regions: list[dict[str, Any]] = []
            for reg in (parsed.get("tile_regions") or [])[:48]:
                if not isinstance(reg, Mapping):
                    continue
                tid = str(reg.get("tile_id") or "").strip()
                if tid not in TILE_CATALOG:
                    continue
                regions.append({
                    "tile_id": tid,
                    "x": clamp_int(reg.get("x"), 0, w - 1, 0),
                    "y": clamp_int(reg.get("y"), 0, h - 1, 0),
                    "w": clamp_int(reg.get("w"), 1, w, 1),
                    "h": clamp_int(reg.get("h"), 1, h, 1),
                    "hollow": bool(reg.get("hollow", False)),
                })

            placements: list[dict[str, Any]] = []
            for pl in (parsed.get("placements") or [])[:32]:
                if not isinstance(pl, Mapping):
                    continue
                bid = str(pl.get("blueprint_id") or "").strip()
                if not bid:
                    continue
                entry: dict[str, Any] = {
                    "blueprint_id": bid,
                    "x": clamp_int(pl.get("x"), 0, w - 1, w // 2),
                    "y": clamp_int(pl.get("y"), 0, h - 1, max(1, h // 2)),
                }
                if pl.get("note_text"):
                    entry["note_text"] = clip_text(pl.get("note_text"), 80, "")
                placements.append(entry)

            base_tile = str(parsed.get("base_tile") or "wood_floor").strip()
            if base_tile not in TILE_CATALOG:
                base_tile = "wood_floor"

            map_spec = {
                "name": clip_text(parsed.get("name"), 16, "灵感地图"),
                "width": w,
                "height": h,
                "wall_rows": clamp_int(parsed.get("wall_rows"), 0, 2, 0),
                "palette_id": str(parsed.get("palette_id") or "warm_stardew").strip(),
                "ambient_phrase": clip_text(parsed.get("ambient_phrase"), 60, clean_prompt),
                "base_tile": base_tile,
                "tile_regions": regions,
                "placements": placements,
                "custom_blueprints": custom_bps,
            }
            return {
                "map_spec": map_spec,
                "used_llm": True,
                "note": "已由大模型设计地图分区、地形材质与家具布局。",
            }

    map_spec = _fallback_map_from_text(clean_prompt, w, h)
    return {
        "map_spec": map_spec,
        "used_llm": False,
        "note": "未调用大模型，已按场景关键词生成配套地形与物件布局。",
    }
