"""她的「手」：受控的文字世界交互工具集（严格检验物质守恒与坐标连通性）。"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable

logger = logging.getLogger(__name__)

TOOL_GROUP = "nanami_world"
_TOOL_NAMES = ("world_act", "world_move", "world_write", "world_combine")

_LOCK = threading.RLock()
_ENGINE: Any = None
_ACTOR_RESOLVER: Callable[[], str] | None = None
_REGISTERED_DEFINITIONS: list[dict[str, Any]] = []


def bind_runtime(engine: Any, actor_resolver: Callable[[], str] | None = None) -> None:
    with _LOCK:
        global _ENGINE, _ACTOR_RESOLVER
        _ENGINE = engine
        _ACTOR_RESOLVER = actor_resolver


def _active_ctx() -> tuple[Any, str]:
    with _LOCK:
        engine = _ENGINE
        resolver = _ACTOR_RESOLVER
    if engine is None:
        return None, ""
    aid = resolver() if resolver else engine.resolve_actor_id()
    return engine, str(aid or "")


def world_act(entity_id: str, action: str, target_id: str = "") -> str:
    """对文字世界里真实存在的物件执行一个动作（如 look/take/place/open/close/unlock/drink/eat/turn_on/turn_off/rest/leave_rest/interact/read/enter）。

    Args:
        entity_id: 视野清单里方括号中的物品 ID（如 mug_1）。
        action: 该物品此刻开放的合法动词或中文动词名。
        target_id: 仅在 action=place 时使用，传桌子/箱子 ID、"floor" 或 "x,y"。
    """
    engine, aid = _active_ctx()
    if engine is None or not aid:
        return "世界里还没有激活的角色。"
    return str(engine.execute_act(aid, str(entity_id or ""), str(action or ""), str(target_id or "")))


def world_move(x: int, y: int) -> str:
    """走到当前房间指定的可达空坐标 (x, y)。

    Args:
        x: 目标横坐标（整数）。
        y: 目标纵坐标（整数，需在地面区）。
    """
    engine, aid = _active_ctx()
    if engine is None or not aid:
        return "世界里还没有激活的角色。"
    return str(engine.execute_move(aid, int(x), int(y)))


def world_write(entity_id: str, text: str) -> str:
    """往允许书写的载体（如空白笔记本）上写下一段真实的简短文字。

    Args:
        entity_id: 可写物品的 ID（如 notebook_1）。
        text: 要写下的真实内容（不超过 120 字，不要包含舞台括号指示）。
    """
    engine, aid = _active_ctx()
    if engine is None or not aid:
        return "世界里还没有激活的角色。"
    return str(engine.execute_write(aid, str(entity_id or ""), str(text or "")))


def world_combine(source_id: str, target_id: str) -> str:
    """把手里或身边的某件物品（如钥匙、水壶、充电线）作用到另一件物品上。

    Args:
        source_id: 拿来使用的源物品 ID。
        target_id: 目标物品 ID。
    """
    engine, aid = _active_ctx()
    if engine is None or not aid:
        return "世界里还没有激活的角色。"
    return str(engine.execute_combine(aid, str(source_id or ""), str(target_id or "")))


def register_tools(tool_manager: Any) -> None:
    """注册 4 个统一文字世界工具至宿主 ToolManager。"""
    for fn in (world_act, world_move, world_write, world_combine):
        tool_manager.register_function(
            fn,
            name=fn.__name__,
            description=(fn.__doc__ or "").strip(),
            group=TOOL_GROUP,
            risk="low",
        )
    with _LOCK:
        _REGISTERED_DEFINITIONS.clear()
        for item in tool_manager.get_definitions(groups=[TOOL_GROUP]):
            _REGISTERED_DEFINITIONS.append(item)


def get_tool_definitions() -> list[dict[str, Any]]:
    """取出工具 schema 列表以注入 BeforeChatContext.tools。"""
    with _LOCK:
        if _REGISTERED_DEFINITIONS:
            return [
                d for d in _REGISTERED_DEFINITIONS
                if str(d.get("function", {}).get("name") or "") in _TOOL_NAMES
            ]
    try:
        from ai.tools.tool_manager import ToolManager

        tm = ToolManager()
        register_tools(tm)
        return [
            d for d in tm.get_definitions(groups=[TOOL_GROUP])
            if str(d.get("function", {}).get("name") or "") in _TOOL_NAMES
        ]
    except Exception:
        return []
