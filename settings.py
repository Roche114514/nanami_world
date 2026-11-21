"""插件级运行时配置管理（纯标准库 JSON，原子写盘）。"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

logger = logging.getLogger(__name__)

SETTINGS_FILENAME = "settings.json"


def _clamp_int(value: Any, low: int, high: int, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, parsed))


def _clamp_float(value: Any, low: float, high: float, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if parsed != parsed:
        return default
    return max(low, min(high, parsed))


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("1", "true", "yes", "on"):
            return True
        if lowered in ("0", "false", "no", "off"):
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    return default


@dataclass(frozen=True)
class WorldSettings:
    """`nanami_world` 插件级设置（全部保存于插件独立数据目录）。"""

    enabled: bool = True
    inject_world_prompt: bool = True
    inject_world_tools: bool = True
    heartbeat_enabled: bool = True
    heartbeat_interval_sec: int = 300
    heartbeat_use_llm: bool = False
    episodic_memory_enabled: bool = True
    allow_ai_diary: bool = False
    diary_cooldown_hours: float = 6.0
    auto_compile_llm: bool = True
    persona_data_dir: str = ""
    default_space_width: int = 12
    default_space_height: int = 9
    pixellab_api_key: str = ""

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> "WorldSettings":
        src = raw if isinstance(raw, Mapping) else {}
        return cls(
            enabled=_as_bool(src.get("enabled"), True),
            inject_world_prompt=_as_bool(src.get("inject_world_prompt"), True),
            inject_world_tools=_as_bool(src.get("inject_world_tools"), True),
            heartbeat_enabled=_as_bool(src.get("heartbeat_enabled"), True),
            heartbeat_interval_sec=_clamp_int(src.get("heartbeat_interval_sec"), 30, 3600, 300),
            heartbeat_use_llm=_as_bool(src.get("heartbeat_use_llm"), False),
            episodic_memory_enabled=_as_bool(src.get("episodic_memory_enabled"), True),
            allow_ai_diary=_as_bool(src.get("allow_ai_diary"), False),
            diary_cooldown_hours=_clamp_float(src.get("diary_cooldown_hours"), 1.0, 72.0, 6.0),
            auto_compile_llm=_as_bool(src.get("auto_compile_llm"), True),
            persona_data_dir=str(src.get("persona_data_dir") or "").strip(),
            default_space_width=_clamp_int(src.get("default_space_width"), 8, 64, 12),
            default_space_height=_clamp_int(src.get("default_space_height"), 6, 64, 9),
            pixellab_api_key=str(src.get("pixellab_api_key") or "").strip(),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class SettingsStore:
    """线程安全配置读写器。"""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._lock = threading.RLock()
        self._cached: WorldSettings | None = None

    @property
    def path(self) -> Path:
        return self._path

    def get(self) -> WorldSettings:
        with self._lock:
            if self._cached is not None:
                return self._cached
            self._cached = self._load_locked()
            return self._cached

    def reload(self) -> WorldSettings:
        with self._lock:
            self._cached = self._load_locked()
            return self._cached

    def update(self, incoming: Mapping[str, Any] | None) -> WorldSettings:
        with self._lock:
            current = self._load_locked().to_dict()
            if isinstance(incoming, Mapping):
                for key in current:
                    if key in incoming:
                        current[key] = incoming[key]
            updated = WorldSettings.from_mapping(current)
            self._save_locked(updated)
            self._cached = updated
            return updated

    def _load_locked(self) -> WorldSettings:
        if not self._path.is_file():
            settings = WorldSettings()
            self._save_locked(settings)
            return settings
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.warning("nanami_world: 读取设置失败，使用默认配置: %s", self._path, exc_info=True)
            return WorldSettings()
        return WorldSettings.from_mapping(raw if isinstance(raw, Mapping) else {})

    def _save_locked(self, settings: WorldSettings) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_name(f"{self._path.name}.tmp")
            tmp.write_text(
                json.dumps(settings.to_dict(), ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(tmp, self._path)
        except OSError:
            logger.warning("nanami_world: 保存设置失败: %s", self._path, exc_info=True)
