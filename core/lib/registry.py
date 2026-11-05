"""模块注册表与发现机制。

注册表只保存"类"，内核实例化时才产生"对象"——这样同一个进程里可以并行跑多个
内核（测试里很常用），互不干扰。

发现来源有三种：

1. 内置模块：``import modules`` 时通过 ``@register`` 自注册；
2. 第三方包：在 ``pyproject.toml`` 里声明 ``[project.entry-points."core.modules"]``；
3. 显式路径：``registry.load_paths(["my_pkg.mod:MyModule"])``。
"""

from __future__ import annotations

import importlib
import logging
import threading
from typing import Iterable, Sequence

from ..module import Module
from .errors import DuplicateModule, ModuleError, ModuleNotFound

_log = logging.getLogger("core.registry")


class ModuleRegistry:
    """模块类注册表（name -> class）。"""

    def __init__(self) -> None:
        self._classes: dict[str, type[Module]] = {}
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ #
    # 注册
    # ------------------------------------------------------------------ #
    def register(
            self,
            cls: type[Module],
            *,
            name: str | None = None,
            replace: bool = False,
    ) -> type[Module]:
        if not isinstance(cls, type) or not issubclass(cls, Module):
            raise TypeError(f"只能注册 Module 子类，收到 {cls!r}")
        resolved = name or cls.module_name()
        with self._lock:
            existing = self._classes.get(resolved)
            if existing is cls:
                return cls
            if existing is not None and not replace:
                raise DuplicateModule(resolved)
            self._classes[resolved] = cls
        _log.debug("注册模块类 %s -> %s", resolved, cls.__name__)
        return cls

    def unregister(self, name: str) -> bool:
        with self._lock:
            removed = self._classes.pop(name, None) is not None
        if removed:
            _log.debug("注销模块类 %s", name)
        return removed

    def clear(self) -> None:
        with self._lock:
            self._classes.clear()

    # ------------------------------------------------------------------ #
    # 查询 / 实例化
    # ------------------------------------------------------------------ #
    def get(self, name: str) -> type[Module]:
        try:
            return self._classes[name]
        except KeyError:
            raise ModuleNotFound(name) from None

    def create(self, name: str) -> Module:
        return self.get(name)()

    def names(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._classes)

    def classes(self) -> tuple[type[Module], ...]:
        with self._lock:
            return tuple(self._classes.values())

    def __contains__(self, name: object) -> bool:
        return name in self._classes

    def __len__(self) -> int:
        return len(self._classes)

    # ------------------------------------------------------------------ #
    # 发现
    # ------------------------------------------------------------------ #
    def load_entry_points(self, group: str = "core.modules") -> list[str]:
        """加载 ``group`` 下的所有入口点，返回注册成功的模块名。"""
        from importlib import metadata

        try:
            entry_points = metadata.entry_points(group=group)
        except TypeError:  # Python < 3.10 的旧签名
            entry_points = metadata.entry_points().get(group, [])  # type: ignore[attr-defined]

        loaded: list[str] = []
        for entry_point in entry_points:
            try:
                target = entry_point.load()
            except Exception as exc:  # noqa: BLE001 - 统一转换成框架异常
                raise ModuleError(f"加载入口点 {entry_point.name!r} 失败: {exc}") from exc
            loaded.extend(self._register_targets(target, origin=f"entry point {entry_point.name!r}"))
        _log.debug("加载入口点 group=%s loaded=%d", group, len(loaded))
        return loaded

    def load_paths(self, specs: Iterable[str]) -> list[str]:
        """按 ``"pkg.mod:Class"`` 或 ``"pkg.mod"`` 加载并注册模块类。"""
        specs = list(specs)
        loaded: list[str] = []
        for spec in specs:
            module_path, _, attribute = spec.partition(":")
            module = importlib.import_module(module_path)
            if attribute:
                target = getattr(module, attribute, None)
                if target is None:
                    raise ModuleError(f"{module_path!r} 中没有 {attribute!r}")
                loaded.extend(self._register_targets(target, origin=spec))
                continue
            for value in vars(module).values():
                if (
                        isinstance(value, type)
                        and issubclass(value, Module)
                        and value is not Module
                        and value.__module__ == module.__name__
                ):
                    self.register(value)
                    loaded.append(value.module_name())
        _log.debug("加载模块路径 specs=%d loaded=%d", len(specs), len(loaded))
        return loaded

    def _register_targets(self, target: object, *, origin: str) -> list[str]:
        candidates: Sequence[object]
        if isinstance(target, type) and issubclass(target, Module):
            candidates = (target,)
        elif callable(target) and not isinstance(target, type):
            result = target()
            if isinstance(result, type) and issubclass(result, Module):
                candidates = (result,)
            elif isinstance(result, Iterable) and not isinstance(result, (str, bytes)):
                candidates = tuple(result)
            else:
                raise ModuleError(f"{origin} 返回了无法识别的对象 {result!r}")
        else:
            raise ModuleError(f"{origin} 指向的对象 {target!r} 不是 Module 子类或工厂函数")

        loaded: list[str] = []
        for candidate in candidates:
            if not (isinstance(candidate, type) and issubclass(candidate, Module)):
                raise ModuleError(f"{origin} 产出了非模块对象 {candidate!r}")
            self.register(candidate)
            loaded.append(candidate.module_name())
        return loaded


_default_registry = ModuleRegistry()


def get_registry() -> ModuleRegistry:
    """返回进程级默认注册表。"""
    return _default_registry


def register(
        cls: type[Module] | None = None,
        *,
        name: str | None = None,
        replace: bool = False,
):
    """类装饰器：把模块类登记到默认注册表。

    .. code-block:: python

        @register
        class Greeter(Module):
            name = "greeter"
    """

    def decorator(target: type[Module]) -> type[Module]:
        _default_registry.register(target, name=name, replace=replace)
        return target

    if cls is None:
        return decorator
    return decorator(cls)


__all__ = ["ModuleRegistry", "get_registry", "register"]
