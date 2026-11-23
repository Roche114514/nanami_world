"""七海千秋 · 文字世界（nanami_world）：独立像素风文字世界插件主入口。

职责仅限生命周期与宿主钩子接线：
- init_chat：匹配当前会话角色并做离线时钟补算
- before_chat：把确定性语言视野【世界现实】与受控 world_* 工具按需注入上下文
- shutdown：停止后台心跳线程并刷盘
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any

from sdk.plugin import PluginBase

from . import (
    PLUGIN_AUTHOR,
    PLUGIN_DESCRIPTION,
    PLUGIN_ID,
    PLUGIN_NAME,
    PLUGIN_VERSION,
    projector,
    text_compiler,
    world_page,
    world_tools,
)
from .engine import WorldEngine
from .heartbeat import WorldHeartbeat
from .settings import SETTINGS_FILENAME, SettingsStore
from .world_chat import WorldChatService

logger = logging.getLogger(__name__)

_WORLD_MARKER = "【世界现实】"


def _strip_world_messages(messages: list[dict[str, Any]]) -> None:
    """移除已有的旧世界视野系统消息，防止工具循环重试时重复堆叠。"""
    for idx in range(len(messages) - 1, -1, -1):
        msg = messages[idx]
        if not isinstance(msg, dict):
            continue
        if str(msg.get("role") or "") == "system" and str(msg.get("content") or "").lstrip().startswith(_WORLD_MARKER):
            messages.pop(idx)


def _insert_after_system(messages: list[dict[str, Any]], block: str) -> None:
    """插在开场 system 设定之后（避免落在末尾被当成强指令复读）。"""
    index = 0
    while index < len(messages) and isinstance(messages[index], dict) and str(messages[index].get("role") or "") == "system":
        index += 1
    messages.insert(index, {"role": "system", "content": block})


class NanamiWorldPlugin(PluginBase):
    """Shinsekai 独立文字世界插件入口（支持无参实例化）。"""

    def __init__(self) -> None:
        self._code_dir: Path | None = None
        self._data_root: Path | None = None
        self._settings_store: SettingsStore | None = None
        self._engine: WorldEngine | None = None
        self._heartbeat: WorldHeartbeat | None = None
        self._world_chat: WorldChatService | None = None
        self._session_character: str = ""
        self._lock = threading.RLock()

    @property
    def plugin_id(self) -> str:
        return PLUGIN_ID

    @property
    def plugin_version(self) -> str:
        return PLUGIN_VERSION

    @property
    def plugin_name(self) -> str:
        return PLUGIN_NAME

    @property
    def plugin_description(self) -> str:
        return PLUGIN_DESCRIPTION

    @property
    def plugin_author(self) -> str:
        return PLUGIN_AUTHOR

    @property
    def priority(self) -> int:
        return 60

    def _current_actor_id(self) -> str:
        if self._engine is None:
            return ""
        with self._lock:
            char_name = self._session_character
        return self._engine.resolve_actor_id(char_name)

    def initialize(self, register, plugin_root: Path, host) -> None:
        _ = plugin_root
        code_dir = Path(__file__).resolve().parent
        project_root = Path.cwd().resolve()
        raw_data_dir = Path(getattr(host, "project_data_dir", None) or "data")
        if not raw_data_dir.is_absolute():
            raw_data_dir = (project_root / raw_data_dir).resolve()
        data_root = raw_data_dir / "plugins" / PLUGIN_ID

        self._code_dir = code_dir
        self._data_root = data_root
        self._settings_store = SettingsStore(data_root / SETTINGS_FILENAME)
        settings = self._settings_store.get()

        self._engine = WorldEngine(
            code_dir=code_dir,
            data_root=data_root,
            project_root=project_root,
        )
        self._heartbeat = WorldHeartbeat(
            self._engine,
            self._settings_store.get,
            project_root=project_root,
            adapter_provider=text_compiler._default_adapter,
            chat_service_provider=lambda: self._world_chat,
        )
        self._world_chat = WorldChatService(
            self._engine,
            project_root=project_root,
            settings_provider=self._settings_store.get,
            adapter_provider=text_compiler._default_adapter,
            action_scheduler=self._heartbeat.live.queue_action,
        )
        world_tools.bind_runtime(self._engine, self._current_actor_id)

        user_input_ctrl = None
        if hasattr(register, "frontend_user_input"):
            try:
                user_input_ctrl = register.frontend_user_input()
            except Exception:
                user_input_ctrl = None

        def _submit_user_input(text: str) -> bool:
            if user_input_ctrl is None:
                return False
            try:
                user_input_ctrl.submit_text(text)
                return True
            except Exception:
                logger.debug("nanami_world: 提交前端对话事件跳过（无活动聊天流）", exc_info=True)
                return False

        register.register_init_chat_hook(
            self._on_init_chat,
            label="nanami_world",
            weight=0.1,
            critical=False,
        )
        register.register_before_chat_hook(self._on_before_chat)
        if hasattr(register, "register_llm_tool"):
            register.register_llm_tool(world_tools.register_tools)

        world_page.register_pages(
            register,
            code_dir=code_dir,
            engine=self._engine,
            settings_store=self._settings_store,
            heartbeat=self._heartbeat,
            world_chat=self._world_chat,
            submit_user_input=_submit_user_input,
        )

        if settings.enabled and settings.heartbeat_enabled:
            self._heartbeat.start()

        logger.info("nanami_world: 初始化完成（data=%s）", data_root)

    def _on_init_chat(self, context) -> None:
        if self._settings_store is None or self._engine is None:
            return
        settings = self._settings_store.get()
        if not settings.enabled:
            return

        names = [str(n).strip() for n in (getattr(context, "character_names", None) or ()) if str(n).strip()]
        with self._lock:
            self._session_character = names[0] if names else ""

        aid = self._current_actor_id()
        if aid and self._heartbeat is not None:
            try:
                self._heartbeat.catch_up()
            except Exception:  # noqa: BLE001
                logger.debug("nanami_world: init_chat 补算跳过", exc_info=True)

        if hasattr(context, "report"):
            msg = f"文字世界已就绪（角色：{aid or '未导入'}）"
            context.report(1.0, msg, phase="nanami_world.ready")

    def _on_before_chat(self, context) -> None:
        if self._settings_store is None or self._engine is None:
            return
        settings = self._settings_store.get()
        if not settings.enabled:
            return
        aid = self._current_actor_id()
        if not aid:
            return

        if settings.inject_world_prompt and isinstance(getattr(context, "messages", None), list):
            cue = ""
            memory = ""
            bound = False
            if settings.episodic_memory_enabled:
                with self._lock:
                    character = self._session_character
                actor = next((item for item in self._engine.list_actors() if item["actor_id"] == aid), None)
                if character and actor and character in (actor["host_character"], actor["name"]):
                    bound = True
                    cue = next((msg["content"] for msg in reversed(context.messages)
                                if isinstance(msg, dict) and msg.get("role") == "user"
                                and isinstance(msg.get("content"), str)), "")
                    memory = projector.recall_puzzle(self._engine, aid, actor["space_id"], cue)
            block = projector.project_view(
                self._engine, aid, consume_diffs=True, memory_cue=cue, include_memories=bound,
                player_conversation=True,
            )
            if memory:
                block += f"\n【亲历的解谜记录】\n{memory}"
            if block:
                _strip_world_messages(context.messages)
                _insert_after_system(context.messages, block)

        if settings.inject_world_tools and hasattr(context, "tools"):
            tool_defs = world_tools.get_tool_definitions()
            if tool_defs:
                existing = list(getattr(context, "tools", None) or [])
                seen = {
                    str(item.get("function", {}).get("name") or "")
                    for item in existing
                    if isinstance(item, dict)
                }
                for td in tool_defs:
                    tname = str(td.get("function", {}).get("name") or "")
                    if tname and tname not in seen:
                        existing.append(td)
                        seen.add(tname)
                context.tools = existing

    def shutdown(self) -> None:
        if self._heartbeat is not None:
            self._heartbeat.stop()
