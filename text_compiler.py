"""「以文生像素」造物编译器：仅靠纯文本 LLM 一次产出智能物件规范与受限色槽像素矩阵。

零 VLM、零生图模型、零本地显存消耗；任何阶段异常均 fail-open 退回规则启发式造物。
"""

from __future__ import annotations

import base64
import html
import json
import logging
import os
import re
import subprocess
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from collections.abc import Mapping
from pathlib import Path
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
    default_appearance,
    normalize_appearance,
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
- consumable: {"max_uses": 2, "uses_left": 2, "consume_verb": "喝"|"吃"|"用", "on_empty": "keep_empty"|"vanish", "empty_name": "空容器名", "empty_brief": "空了的白描"}
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


def _load_vision_image_block(image_path: str | Path | None) -> dict[str, Any] | None:
    if not image_path:
        return None
    try:
        p = Path(image_path)
        if not p.is_file() or p.stat().st_size > 4 * 1024 * 1024:
            return None
        ext = p.suffix.lower()
        mime_map = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}
        mime = mime_map.get(ext)
        if not mime:
            return None
        b64 = base64.b64encode(p.read_bytes()).decode("ascii")
        return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}
    except Exception:
        return None


def _ask_llm_json(
    system_prompt: str,
    user_prompt: str,
    *,
    adapter_provider: Callable[[], Any] | None = None,
    timeout_sec: float = 12.0,
    max_tokens: int = 900,
    image_path: str | Path | None = None,
    meta_out: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    adapter = (adapter_provider or _default_adapter)()
    if adapter is None:
        return None
    box: dict[str, Any] = {}
    done = threading.Event()

    supports_vision = False
    try:
        fn = getattr(adapter, "supports_native_vision", None)
        supports_vision = bool(fn() if callable(fn) else fn)
    except Exception:
        supports_vision = False

    img_block = _load_vision_image_block(image_path) if supports_vision else None

    def _call(content: Any) -> dict[str, Any] | None:
        resp = adapter.chat(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": content},
            ],
            stream=False,
            temperature=0.35,
            max_tokens=max_tokens,
            response_format={"type": "json_object"},
        )
        raw_content = str(getattr(adapter.decode_response(resp), "content", "") or "")
        return _extract_json_object(raw_content)

    def _work() -> None:
        try:
            if img_block is not None:
                try:
                    parsed = _call([{"type": "text", "text": user_prompt}, img_block])
                    if parsed is not None:
                        box["parsed"] = parsed
                        box["used_vision"] = True
                        return
                except Exception as exc:
                    logger.debug("nanami_world: 附带参考图调用失败，回退纯文本模式: %s", exc)
            box["parsed"] = _call(user_prompt)
            box["used_vision"] = False
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
    if meta_out is not None:
        meta_out["used_vision"] = bool(box.get("used_vision"))
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


_APPEARANCE_SYSTEM_PROMPT = """你是弹丸论破风格 8-bit 像素 Q 版小人的顶级像素画师。请结合角色设定、网络资料与（若有）官方原作立绘附图，还原角色最具辨识度的发型、头饰、服装与配色，只输出一个合法 JSON 对象（不要 markdown 围栏）：

{
  "hair_style": "spiky|long|bob|twin|ponytail|short 之一",
  "hair_color": "#rrggbb",
  "hair_shade": "#rrggbb",
  "skin": "#rrggbb",
  "eye_color": "#rrggbb",
  "outfit_style": "jacket|hoodie|dress|uniform 之一",
  "outfit_color": "#rrggbb",
  "outfit_accent": "#rrggbb",
  "outfit_inner": "#rrggbb",
  "legs_color": "#rrggbb",
  "shoes_color": "#rrggbb",
  "accessory": "none|bow|glasses|ears|headband 之一",
  "accessory_color": "#rrggbb",
  "custom_frames": {
    "palette": {"h":"#主发色","H":"#发尾阴影","s":"#肤色","e":"#瞳色","m":"#嘴色","j":"#外套主色","J":"#点缀色/领结/发饰","i":"#内衬/白衣","l":"#下装/袜","f":"#鞋","a":"#头饰/帽子/包主色","b":"#帽子/道具副色"},
    "down": ["16字符共24行（正面站立）"],
    "right": ["16字符共24行（可选，朝右站立）"]
  }
}

16×24 弹丸论破 8-bit 像素比例规范（`.` 为透明，不需要画外描边，渲染器会自动加 1px 深黑描边；只用 palette 中的单字符）：
- y=0..2：头顶发冠 / 呆毛 / 大帽子 / 发饰（底部第 23 行留空给描边）
- y=2..12：2 头身大头！y=7..8 刘海与鬓角，y=9..10 双眼（正面左眼 x=5..6、右眼 x=9..10），y=11 小嘴（x=7..8）
- y=13..18：上衣/连帽衫/水手裙/领结/背带/外套开襟
- y=19..21：双腿或长袜（左右分离，如 x=5..6 与 x=9..10，方便渲染器自动切走路步态）
- y=22：鞋子

七海千秋 16×24 正面 `down` 范例（可参考这种清晰比例来画任意角色）：
[
  "................",
  ".....hhhhhh.....",
  "....hhhhhhhh....",
  "...hhhhhhhhhh...",
  "...hhhhhJJhhh...",
  "...hhhhhhhhhh...",
  "...hhhhhhHHHH...",
  "...hssssssssH...",
  "...hsHHssHHsH...",
  "...hseesssees...",
  "...hssssssssH...",
  "....sssmmsss....",
  "....jjJiiJjj....",
  "...sjjjJJjjjs...",
  "...sjjiiiiJjs...",
  "...sjjjjjjjjs...",
  "....jjjjjjjj....",
  "....llllllll....",
  ".....ll..ll.....",
  ".....ss..ss.....",
  ".....ss..ss.....",
  ".....ff..ff.....",
  "................",
  "................"
]"""


def _http_get_text(url: str, *, timeout_sec: float = 2.8) -> str:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (compatible; Shinsekai-NanamiWorld/0.2)"},
    )
    with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
        raw = resp.read(200_000)
        return raw.decode("utf-8", errors="ignore")


def _query_mediawiki_extract(api_base: str, name: str, *, timeout_sec: float = 2.8) -> str:
    q = urllib.parse.urlencode({
        "action": "query",
        "prop": "extracts",
        "exintro": "1",
        "explaintext": "1",
        "redirects": "1",
        "titles": name,
        "format": "json",
        "utf8": "1",
    })
    data = json.loads(_http_get_text(f"{api_base}?{q}", timeout_sec=timeout_sec))
    pages = ((data.get("query") or {}).get("pages") or {})
    for page in pages.values():
        ext = str(page.get("extract") or "").strip()
        if len(ext) >= 30:
            return re.sub(r"\s+", " ", ext)[:500]
    return ""


def search_character_reference(name: str, *, timeout_sec: float = 3.0) -> dict[str, Any]:
    """零依赖联网检索角色外貌与服装资料（萌娘百科 / 维基百科 / DuckDuckGo 摘要），失败自动降级。"""
    clean = clip_text(name, 30, "")
    if not clean:
        return {"summary": "", "sources": [], "used_web": False}

    snippets: list[str] = []
    sources: list[str] = []

    for label, api_base in (
        ("萌娘百科", "https://zh.moegirl.org.cn/api.php"),
        ("维基百科", "https://zh.wikipedia.org/w/api.php"),
    ):
        try:
            ext = _query_mediawiki_extract(api_base, clean, timeout_sec=timeout_sec)
            if ext:
                snippets.append(f"[{label}] {ext}")
                sources.append(label)
                break
        except Exception:
            continue

    if not snippets:
        try:
            q = urllib.parse.urlencode({"q": f"{clean} 外貌 服装 发型 立绘"})
            page = _http_get_text(f"https://html.duckduckgo.com/html/?{q}", timeout_sec=timeout_sec)
            found = re.findall(r'class="result__snippet"[^>]*>([\s\S]*?)</a>', page, flags=re.IGNORECASE)
            cleaned = [re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s))).strip() for s in found[:3]]
            cleaned = [c for c in cleaned if len(c) >= 15]
            if cleaned:
                snippets.append("[网络检索] " + "；".join(cleaned)[:500])
                sources.append("DuckDuckGo")
        except Exception:
            pass

    summary = "\n".join(snippets)[:800]
    return {"summary": summary, "sources": sources, "used_web": bool(summary)}

_HAIR_COLOR_WORDS: tuple[tuple[tuple[str, ...], tuple[str, str]], ...] = (
    (("白发", "银发", "灰白", "雪白", "白色短发", "白色长发", "银色短发", "银色长发", "白毛"), ("#f2f0f0", "#d9dce6")),
    (("金发", "金色", "黄发", "淡金", "金色短发", "金色长发", "金毛"), ("#e8c864", "#f6e2a0")),
    (("粉发", "粉色头发", "粉色短发", "粉色长发", "粉红色", "粉红", "樱色", "粉毛"), ("#f0a0b8", "#f8cad6")),
    (("红发", "赤发", "红色头发", "红色短发", "红色长发", "红毛"), ("#c43a36", "#e8786a")),
    (("橙发", "橘发", "橙色", "橘色", "橙色短发", "橘色短发"), ("#e08a3c", "#f2b878")),
    (("蓝发", "蓝色头发", "蓝色短发", "蓝色长发", "水蓝", "青发"), ("#5e86c4", "#9bbcea")),
    (("绿发", "绿色头发", "绿色短发", "绿色长发"), ("#5aa070", "#98d0a8")),
    (("紫发", "紫色头发", "紫色短发", "紫色长发", "淡紫"), ("#8a62b8", "#bca0dc")),
    (("棕发", "栗色", "茶色", "褐发", "棕色短发", "棕色长发", "咖啡色"), ("#7a5238", "#a67c5a")),
    (("黑发", "黑色头发", "黑色短发", "黑色长发", "黑长直", "乌黑", "深色头发"), ("#2f2a2e", "#4b4348")),
)

_OUTFIT_COLOR_WORDS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("黑衣", "黑色", "黑西装"), "#2f2f38"),
    (("白衣", "白色", "白大褂"), "#eceaf2"),
    (("红色", "红衣", "红裙"), "#c8403c"),
    (("粉色", "粉衣", "粉裙"), "#f0a8bc"),
    (("蓝色", "蓝衣", "蓝裙", "藏青", "海军"), "#3c5c8c"),
    (("绿色", "绿衣", "军绿", "墨绿"), "#4f6b3c"),
    (("黄色", "黄衣"), "#e0c050"),
    (("紫色", "紫衣"), "#7a58a8"),
    (("灰色", "灰衣"), "#8a8d98"),
)


def _fallback_appearance_from_text(name: str, text: str) -> dict[str, Any]:
    base = default_appearance(name)
    t = str(text or "")
    raw = dict(base)
    for words, (main, shade) in _HAIR_COLOR_WORDS:
        if any(w in t for w in words):
            raw["hair_color"], raw["hair_shade"] = main, shade
            break
    for words, color in _OUTFIT_COLOR_WORDS:
        if any(w in t for w in words):
            raw["outfit_color"] = color
            break
    if any(k in t for k in ("双马尾", "两条辫", "双辫")):
        raw["hair_style"] = "twin"
    elif any(k in t for k in ("马尾", "辫子", "侧马尾")):
        raw["hair_style"] = "ponytail"
    elif any(k in t for k in ("长发", "及腰", "披肩", "黑长直")):
        raw["hair_style"] = "long"
    elif any(k in t for k in ("短发", "寸头", "利落")):
        raw["hair_style"] = "short"
    elif any(k in t for k in ("波波", "齐肩", "bob", "蘑菇头")):
        raw["hair_style"] = "bob"
    elif any(k in t for k in ("乱发", "翘发", "乱翘", "翘起", "炸毛", "刺猬", "蓬松", "卷毛", "天然卷", "凌乱")):
        raw["hair_style"] = "spiky"
    if any(k in t for k in ("裙", "水手服", "连衣", "女仆", "少女", "女生")):
        raw["outfit_style"] = "dress"
    elif any(k in t for k in ("西装", "制服", "校服", "西服")):
        raw["outfit_style"] = "uniform"
    elif any(k in t for k in ("卫衣", "连帽", "帽衫")):
        raw["outfit_style"] = "hoodie"
    elif any(k in t for k in ("外套", "夹克", "风衣")):
        raw["outfit_style"] = "jacket"
    if any(k in t for k in ("眼镜",)):
        raw["accessory"] = "glasses"
    elif any(k in t for k in ("蝴蝶结", "发饰", "发夹", "缎带", "丝带")):
        raw["accessory"] = "bow"
    elif any(k in t for k in ("猫耳", "兽耳", "耳朵")):
        raw["accessory"] = "ears"
    elif any(k in t for k in ("发带", "头带", "发箍")):
        raw["accessory"] = "headband"
    return normalize_appearance(raw, seed=name)


def compile_actor_appearance(
    name: str,
    description: str = "",
    *,
    use_llm: bool = True,
    use_web: bool = False,
    reference_image_path: str | Path | None = None,
    adapter_provider: Callable[[], Any] | None = None,
    timeout_sec: float = 20.0,
) -> dict[str, Any]:
    """由角色名、设定、联网参考资料与原作立绘（若支持多模态）编译 Q 版像素小人外观。"""
    clean_name = clip_text(name, 20, "角色")
    clean_desc = clip_text(description, 450, "")
    web_info = search_character_reference(clean_name) if (use_llm and use_web) else {"summary": "", "sources": [], "used_web": False}
    web_txt = str(web_info.get("summary") or "").strip()

    if use_llm and (clean_desc or web_txt or reference_image_path):
        parts = [f"角色名：{clean_name}"]
        if clean_desc:
            parts.append(f"【角色设定】\n{clean_desc}")
        if web_txt:
            parts.append(f"【网络参考资料】\n{web_txt}")
        if reference_image_path:
            parts.append("【原作立绘参考】见附图，请严格对照附图与资料还原标志性发色、头饰、帽子/发卡与服装轮廓。")
        parts.append("请输出 JSON（务必给出 custom_frames.palette 与 custom_frames.down 16x24 像素矩阵，同时填写基础字段）。")

        meta: dict[str, Any] = {}
        parsed = _ask_llm_json(
            _APPEARANCE_SYSTEM_PROMPT,
            "\n\n".join(parts),
            adapter_provider=adapter_provider,
            timeout_sec=timeout_sec,
            max_tokens=1600,
            image_path=reference_image_path,
            meta_out=meta,
        )
        if isinstance(parsed, dict) and (parsed.get("hair_style") or parsed.get("custom_frames")):
            norm = normalize_appearance(parsed, seed=clean_name)
            return {
                "appearance": norm,
                "used_llm": True,
                "used_web": bool(web_info.get("used_web")),
                "web_sources": list(web_info.get("sources") or []),
                "used_vision": bool(meta.get("used_vision")),
                "has_custom_frames": bool(norm.get("custom_frames")),
            }

    fallback = _fallback_appearance_from_text(clean_name, f"{clean_desc}\n{web_txt}".strip())
    return {
        "appearance": fallback,
        "used_llm": False,
        "used_web": bool(web_info.get("used_web")),
        "web_sources": list(web_info.get("sources") or []),
        "used_vision": False,
        "has_custom_frames": False,
    }


def resolve_character_search_info(
    name: str,
    description: str = "",
    *,
    use_llm: bool = True,
    use_web: bool = True,
    adapter_provider: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    """用联网资料 + LLM 提取角色英文搜索名与全身服装英文关键词，用于检索全身照与驱动生图。"""
    clean_name = clip_text(name, 20, "character")
    clean_desc = clip_text(description, 450, "")
    web_info = search_character_reference(clean_name) if use_web else {"summary": "", "sources": [], "used_web": False}
    web_txt = str(web_info.get("summary") or "").strip()

    english_name = ""
    outfit_prompt = ""
    used_llm = False
    if use_llm:
        sys_p = (
            "你是动漫游戏角色资料整理助手。根据角色名、本地设定和网络资料，只输出一个合法 JSON 对象：\n"
            '{"english_name":"官方英文名/罗马音，如 Chiaki Nanami 或 Habetrot（用于 Wiki 搜全身立绘）",'
            '"outfit_prompt":"简明英文全身服装外观描述（<=35词，必须准确写出偏暗/偏浅的真实色调，如 dark charcoal-teal jacket、dusty mauve-pink bob hair，勿写成鲜亮荧光色；包含发型、头饰、上衣、下装裙/裤、袜子与鞋子）"}'
        )
        usr_p = f"角色名：{clean_name}\n设定：{clean_desc or '无'}\n网络资料：{web_txt or '无'}"
        parsed = _ask_llm_json(sys_p, usr_p, adapter_provider=adapter_provider, timeout_sec=10.0, max_tokens=220)
        if isinstance(parsed, dict):
            english_name = clip_text(parsed.get("english_name"), 60, "")
            outfit_prompt = clip_text(parsed.get("outfit_prompt"), 240, "")
            used_llm = bool(english_name or outfit_prompt)

    if not english_name:
        known_en = {"七海千秋": "Chiaki Nanami", "日向创": "Hajime Hinata", "狛枝凪斗": "Nagito Komaeda", "哈贝特洛特": "Habetrot"}
        english_name = known_en.get(clean_name, clean_name)

    return {
        "english_name": english_name,
        "outfit_prompt": outfit_prompt,
        "used_llm": used_llm,
        "used_web": bool(web_info.get("used_web")),
        "web_sources": list(web_info.get("sources") or []),
    }


def search_official_pixel_sprite(
    name: str,
    english_name: str = "",
    *,
    timeout_sec: float = 5.0,
) -> dict[str, Any] | None:
    """检索弹丸论破 Wiki 原版 8-bit 像素小人图（如 75x84 @ 3x），返回原生 data URL。"""
    q_name = str(english_name or "").strip()
    if not q_name:
        known_en = {"七海千秋": "Chiaki Nanami", "日向创": "Hajime Hinata", "狛枝凪斗": "Nagito Komaeda", "索妮娅": "Sonia Nevermind", "雾切响子": "Kyoko Kirigiri", "苗木诚": "Makoto Naegi"}
        q_name = known_en.get(str(name or "").strip(), "")
    if not q_name:
        return None

    api = "https://danganronpa.fandom.com/api.php"
    for term in (f"{q_name} Pixel Sprites", f"{q_name} Pixel"):
        try:
            q = urllib.parse.urlencode({
                "action": "query",
                "generator": "search",
                "gsrsearch": term,
                "gsrnamespace": "6",
                "gsrlimit": "6",
                "prop": "imageinfo",
                "iiprop": "url|size",
                "format": "json",
            })
            data = json.loads(_http_get_text(f"{api}?{q}", timeout_sec=timeout_sec))
            pages = sorted((data.get("query") or {}).get("pages", {}).values(), key=lambda p: str(p.get("title") or ""))
            for page in pages:
                title = str(page.get("title") or "")
                if "pixel" not in title.lower():
                    continue
                ii = (page.get("imageinfo") or [{}])[0]
                w, h = int(ii.get("width") or 0), int(ii.get("height") or 0)
                img_url = str(ii.get("url") or "")
                if not img_url or not (20 <= w <= 160 and 20 <= h <= 160):
                    continue
                req = urllib.request.Request(img_url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=timeout_sec) as r:
                    raw = r.read(300_000)
                if raw[:6] in (b"GIF87a", b"GIF89a"):
                    mime = "image/gif"
                elif raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
                    mime = "image/webp"
                else:
                    mime = "image/png"
                return {
                    "image_data_url": f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}",
                    "title": title,
                    "width": w,
                    "height": h,
                    "english_name": q_name,
                }
        except Exception:
            continue
    return None


def generate_pixellab_4dir_sprites(
    name: str,
    description: str = "",
    *,
    api_key: str,
    use_web: bool = True,
    use_llm: bool = True,
    adapter_provider: Callable[[], Any] | None = None,
    timeout_sec: float = 90.0,
) -> dict[str, Any]:
    """调用 PixelLab v2 生成原生低像素透明底四向 Q 版角色精灵（不经缩放）。"""
    tok = str(api_key or "").strip()
    if not tok:
        raise ValueError("尚未配置 PixelLab API Key。")

    info = resolve_character_search_info(
        name,
        description,
        use_llm=use_llm,
        use_web=use_web,
        adapter_provider=adapter_provider,
    )
    en_name = info["english_name"]
    outfit = info["outfit_prompt"]
    desc_text = f"{en_name}" + (f", {outfit}" if outfit else "") + ", cute 8-bit anime chibi character"

    headers = {
        "Authorization": f"Bearer {tok}",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0",
    }
    body = {
        "description": desc_text[:480],
        "image_size": {"width": 32, "height": 32},
        "proportions": {"type": "preset", "name": "chibi"},
        "outline": "single color black outline",
        "shading": "basic shading",
        "detail": "medium detail",
        "view": "low top-down",
        "async_mode": True,
    }
    req = urllib.request.Request(
        "https://api.pixellab.ai/v2/create-character-with-4-directions",
        data=json.dumps(body).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        sub = json.loads(resp.read().decode("utf-8"))

    jid = str(sub.get("background_job_id") or "")
    cid = str(sub.get("character_id") or "")
    if not jid:
        raise RuntimeError(f"PixelLab 提交失败：{sub}")

    deadline = time.time() + timeout_sec
    last_job: dict[str, Any] = {}
    while time.time() < deadline:
        time.sleep(2.5)
        jreq = urllib.request.Request(
            f"https://api.pixellab.ai/v2/background-jobs/{jid}",
            headers={"Authorization": f"Bearer {tok}", "User-Agent": "Mozilla/5.0"},
        )
        with urllib.request.urlopen(jreq, timeout=15) as jr:
            last_job = json.loads(jr.read().decode("utf-8"))
        st = str(last_job.get("status") or "")
        if st == "completed":
            break
        if st == "failed":
            raise RuntimeError(f"PixelLab 生成失败：{last_job.get('error') or last_job}")
    else:
        raise RuntimeError("PixelLab 生成超时，请稍后重试。")

    raw_imgs = ((last_job.get("last_response") or {}).get("images") or {})
    dir_map = {"south": "down", "east": "right", "north": "up", "west": "left"}
    images: dict[str, str] = {}
    for pl_dir, our_dir in dir_map.items():
        item = raw_imgs.get(pl_dir)
        if isinstance(item, dict) and item.get("base64"):
            b64 = str(item["base64"])
            images[our_dir] = b64 if b64.startswith("data:") else f"data:image/png;base64,{b64}"

    if len(images) < 4 and cid:
        creq = urllib.request.Request(
            f"https://api.pixellab.ai/v2/characters/{cid}",
            headers={"Authorization": f"Bearer {tok}", "User-Agent": "Mozilla/5.0"},
        )
        with urllib.request.urlopen(creq, timeout=15) as cr:
            cinfo = json.loads(cr.read().decode("utf-8"))
        rurls = cinfo.get("rotation_urls") or {}
        for pl_dir, our_dir in dir_map.items():
            if our_dir not in images and rurls.get(pl_dir):
                raw_png = urllib.request.urlopen(urllib.request.Request(rurls[pl_dir], headers={"User-Agent": "Mozilla/5.0"}), timeout=10).read()
                images[our_dir] = f"data:image/png;base64,{base64.b64encode(raw_png).decode('ascii')}"

    if "down" not in images:
        raise RuntimeError("PixelLab 未返回正面像素图。")

    return {
        "used_pixellab": True,
        "images": images,
        "used_llm": info["used_llm"],
        "used_web": info["used_web"],
        "web_sources": info["web_sources"],
        "english_name": en_name,
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
