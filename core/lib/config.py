"""core 自带的配置系统。

使用方式::

    config = Config(path="config/default.json")
    config.register("app.name", "demo")
    config.register("http.port", 8080, cast=int)

    config.get("http.port")        # 文件 > env > 默认值
    config.set("app.name", "myapp")  # 写回文件，作为覆盖

优先级：配置文件 > 环境变量 > 注册时的默认值。每次读取都会检查文件是否变化，
环境变量也是实时读，因此 ``get`` 总是拿到最新值。
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Callable, Mapping

from ..compat import atomic_write_text, read_text
from ..utils.env import current_env
from .errors import FrameworkError

_MISSING = object()
_log = logging.getLogger("core.config")


class ConfigError(FrameworkError):
    """配置加载、转换或写入失败。"""


def _to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in ("1", "true", "yes", "on", "y"):
        return True
    if normalized in ("0", "false", "no", "off", "n"):
        return False
    raise ValueError(f"{value!r} 不是布尔值")


def _parse_scalar(value: str) -> Any:
    text = value.strip()
    lower = text.lower()
    if lower in ("true", "yes", "on"):
        return True
    if lower in ("false", "no", "off"):
        return False
    if lower in ("null", "none"):
        return None
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        pass
    if text.startswith(("{", "[", '"')):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
    return value


def _flatten(data: Mapping[str, Any], prefix: str = "") -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in data.items():
        full = f"{prefix}{key}"
        if isinstance(value, Mapping):
            result.update(_flatten(value, f"{full}."))
        else:
            result[full] = value
    return result


def _unflatten(values: Mapping[str, Any]) -> dict[str, Any]:
    tree: dict[str, Any] = {}
    for key, value in values.items():
        parts = key.split(".")
        node = tree
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value
    return tree


class Config:
    """配置文件 + 环境变量 + 默认值的配置容器。"""

    def __init__(
        self,
        path: str | os.PathLike[str] | None = None,
        *,
        env: Mapping[str, str] | None = None,
        env_prefix: str = "APP_",
        env_separator: str = "__",
    ) -> None:
        self._env = dict(env) if env is not None else None
        self._env_prefix = env_prefix
        self._env_separator = env_separator
        if path is None:
            raw_path = self._env_value("config.path")
            if raw_path is not _MISSING:
                path = str(raw_path)
        self._path = Path(path).expanduser() if path is not None else None
        self._lock = threading.RLock()
        self._defaults: dict[str, Any] = {}
        self._casts: dict[str, Callable[[Any], Any]] = {}
        self._file_values: dict[str, Any] = {}
        self._mtime_ns: int | None = None
        self._size: int | None = None
        self.reload()

    # ------------------------------------------------------------------ #
    # 注册
    # ------------------------------------------------------------------ #
    def register(
        self,
        key: str,
        default: Any = None,
        *,
        cast: Callable[[Any], Any] | None = None,
        description: str = "",
    ) -> None:
        """注册配置项及默认值；重复注册不覆盖已有默认值。"""
        normalized = self._normalize(key)
        with self._lock:
            self._defaults.setdefault(normalized, default)
            if cast is not None:
                self._casts.setdefault(normalized, cast)
        _log.debug("注册配置 %s", normalized)

    @property
    def path(self) -> Path | None:
        return self._path

    # ------------------------------------------------------------------ #
    # 读取
    # ------------------------------------------------------------------ #
    def get(
        self,
        key: str,
        default: Any = _MISSING,
        *,
        cast: Callable[[Any], Any] | None = None,
    ) -> Any:
        normalized = self._normalize(key)
        self._refresh()
        with self._lock:
            if normalized in self._file_values:
                value = self._file_values[normalized]
            else:
                value = self._env_value(normalized)
                if value is _MISSING:
                    if default is not _MISSING:
                        return default
                    if normalized not in self._defaults:
                        raise ConfigError(f"配置 {key!r} 未注册")
                    value = self._defaults[normalized]
            caster = cast if cast is not None else self._casts.get(normalized)
        return self._cast(normalized, value, caster)

    def require(self, key: str, *, cast: Callable[[Any], Any] | None = None) -> Any:
        return self.get(key, cast=cast)

    def get_bool(self, key: str, default: bool = False) -> bool:
        return _to_bool(self.get(key, default))

    def get_int(self, key: str, default: int = 0) -> int:
        return int(self.get(key, default))

    def get_float(self, key: str, default: float = 0.0) -> float:
        return float(self.get(key, default))

    def __contains__(self, key: object) -> bool:
        if not isinstance(key, str):
            return False
        try:
            self.get(key)
            return True
        except ConfigError:
            return False

    def __getitem__(self, key: str) -> Any:
        return self.get(key)

    # ------------------------------------------------------------------ #
    # 写入
    # ------------------------------------------------------------------ #
    def set(self, key: str, value: Any) -> Any:
        """写入配置并持久化到文件；返回旧值。"""
        normalized = self._normalize(key)
        self._refresh()
        with self._lock:
            old = self._file_values.get(normalized, _MISSING)
            self._file_values[normalized] = value
            self._persist()
        _log.info("配置写入 key=%s path=%s", normalized, self._path or "(内存)")
        return None if old is _MISSING else old

    def update(self, values: Mapping[str, Any]) -> None:
        for key, value in _flatten(values).items():
            self.set(key, value)

    # ------------------------------------------------------------------ #
    # 文件
    # ------------------------------------------------------------------ #
    def reload(self) -> None:
        if self._path is None:
            return
        with self._lock:
            if not self._path.exists():
                self._file_values = {}
                self._mtime_ns = None
                self._size = None
                return
            try:
                raw = read_text(self._path)
                data = json.loads(raw) if raw.strip() else {}
            except (OSError, json.JSONDecodeError) as exc:
                raise ConfigError(f"{self._path} 不是合法 JSON: {exc}") from exc
            if not isinstance(data, Mapping):
                raise ConfigError(f"{self._path} 的顶层必须是对象")
            self._file_values = _flatten(data)
            stat = self._path.stat()
            self._mtime_ns = stat.st_mtime_ns
            self._size = stat.st_size
        _log.debug("读取配置文件 %s keys=%d", self._path, len(self._file_values))

    def _refresh(self) -> None:
        if self._path is None:
            return
        try:
            stat = self._path.stat()
        except OSError:
            return
        with self._lock:
            if stat.st_mtime_ns != self._mtime_ns or stat.st_size != self._size:
                self.reload()

    def _persist(self) -> None:
        if self._path is None:
            return
        data = _unflatten(self._file_values)
        text = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        atomic_write_text(self._path, text)
        stat = self._path.stat()
        self._mtime_ns = stat.st_mtime_ns
        self._size = stat.st_size

    # ------------------------------------------------------------------ #
    # 视图
    # ------------------------------------------------------------------ #
    def as_dict(self) -> dict[str, Any]:
        self._refresh()
        with self._lock:
            result: dict[str, Any] = dict(self._defaults)
            result.update(self._file_values)
        return result

    def section(self, prefix: str) -> "ConfigSection":
        return ConfigSection(self, prefix)

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #
    def _env_value(self, key: str) -> Any:
        mapping = self._env if self._env is not None else (current_env() or os.environ)
        upper = key.upper()
        candidates = [
            f"{self._env_prefix}{upper.replace('.', self._env_separator)}",
            f"{self._env_prefix}{upper.replace('.', '_')}",
        ]
        for name in candidates:
            if name in mapping:
                return _parse_scalar(mapping[name])
        return _MISSING

    @staticmethod
    def _normalize(key: str) -> str:
        return str(key).strip().lower()

    @staticmethod
    def _cast(key: str, value: Any, caster: Callable[[Any], Any] | None) -> Any:
        if caster is None:
            return value
        try:
            if caster is bool:
                return _to_bool(value)
            return caster(value)
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"配置 {key!r} 的值 {value!r} 无法转换: {exc}") from exc


class ConfigSection:
    """某个前缀下的配置视图。"""

    def __init__(self, config: Config, prefix: str) -> None:
        self._config = config
        self._prefix = prefix.strip(".")

    def _full(self, key: str) -> str:
        return f"{self._prefix}.{key}" if self._prefix else key

    def get(self, key: str, default: Any = _MISSING) -> Any:
        return self._config.get(self._full(key), default)

    def require(self, key: str) -> Any:
        return self._config.require(self._full(key))

    def as_dict(self) -> dict[str, Any]:
        prefix = f"{self._prefix}."
        return {
            key[len(prefix):]: value
            for key, value in self._config.as_dict().items()
            if key.startswith(prefix)
        }


__all__ = ["Config", "ConfigError", "ConfigSection"]
