"""内核：模块注册、依赖编排、生命周期、事件分发、模块间调用。

内核只依赖 :mod:`core` 内部的契约，不 import 任何模块。所有模块（含内置模块）
都在 ``modules/`` 包里，导入时通过 ``@register`` 自注册。
"""

from __future__ import annotations

import logging
import os
import random
import threading
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from .lib.config import Config
from .lib.console import Console, get_console
from .lib.errors import (
    DuplicateModule,
    LifecycleError,
    MissingDependency,
    ModuleNotFound,
    ModuleUnavailable,
)
from .lib.events import Event, EventBus, Subscription
from .lib.log import get_logger
from .lib.registry import ModuleRegistry, get_registry
from .module import Module, ModuleState
from .utils.env import reset_env, set_env
from .utils.time import now_ms
from .utils.timeout import run_with_timeout


class KernelState(str, Enum):
    NEW = "new"
    BOOTING = "booting"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"


@dataclass(frozen=True)
class KernelOptions:
    """内核构造参数（不可变，模块可通过 ``self.kernel.options`` 读取）。"""

    entry_points_group: str = "core.modules"
    autoload: bool = True
    discover: bool = False
    module_timeout: float = 30.0
    request_timeout: float | None = None
    recovery_attempts: int = 3
    health_interval: float = 0.0


class Kernel:
    """模块容器与协调者。

    典型用法::

        kernel = Kernel()
        kernel.boot()
        try:
            kernel.request("greeter", "greet", {"name": "world"})
        finally:
            kernel.shutdown()

    或者用上下文管理器::

        with Kernel() as kernel:
            ...
    """

    def __init__(
            self,
            *,
            registry: ModuleRegistry | None = None,
            env: Mapping[str, str] | None = None,
            config: Config | None = None,
            autoload: bool = True,
            discover: bool = False,
            entry_points_group: str = "core.modules",
            enable: Iterable[str] = (),
            disable: Iterable[str] = (),
            module_timeout: float = 30.0,
            request_timeout: float | None = None,
            recovery_attempts: int = 3,
            health_interval: float = 0.0,
    ) -> None:
        if registry is None:
            registry = get_registry()

        self._registry = registry
        merged_env = dict(os.environ)
        if env is not None:
            merged_env.update(env)
        self._env = MappingProxyType(merged_env)
        self._env_token: Any = None
        self._config = config if config is not None else Config(env=self._env)
        self._log = logging.getLogger("kernel")
        self.options = KernelOptions(
            entry_points_group=entry_points_group,
            autoload=autoload,
            discover=discover,
            module_timeout=module_timeout,
            request_timeout=request_timeout,
            recovery_attempts=recovery_attempts,
            health_interval=health_interval,
        )
        self._modules: dict[str, Module] = {}
        self._errors: dict[str, type[Exception]] = {}
        self._load_errors: dict[str, Exception] = {}
        self._explicit_enabled: dict[str, bool] = {}
        self._pending_enable: set[str] = set(enable)
        self._pending_disable: set[str] = set(disable)
        self._events = EventBus(on_error=self._on_event_error)
        self._state = KernelState.NEW
        self._lock = threading.RLock()
        self._order: list[str] = []
        self._started_at: int | None = None
        self._event_errors = 0
        self._recovering: set[str] = set()
        self._health_stop = threading.Event()
        self._health_thread: threading.Thread | None = None

    # ------------------------------------------------------------------ #
    # 基本状态
    # ------------------------------------------------------------------ #
    @property
    def state(self) -> KernelState:
        return self._state

    @property
    def events(self) -> EventBus:
        return self._events

    @property
    def env(self) -> Mapping[str, str]:
        """内核持有的环境快照；boot 时注入当前上下文。"""
        return self._env

    @property
    def console(self) -> Console:
        """全局控制台；日志输出也走它。"""
        return get_console()

    @property
    def config(self) -> Config:
        """core 配置系统：配置文件 > env > 注册默认值。"""
        return self._config

    @property
    def started_at(self) -> float | None:
        return self._started_at

    @property
    def uptime(self) -> float:
        return 0.0 if self._started_at is None else (now_ms() - self._started_at) / 1000

    @property
    def event_errors(self) -> int:
        """被隔离的事件处理器异常计数（便于监控）。"""
        return self._event_errors

    def __enter__(self) -> "Kernel":
        return self.boot()

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.shutdown()

    # ------------------------------------------------------------------ #
    # 注册
    # ------------------------------------------------------------------ #
    def register(
            self,
            module: Module | type[Module],
            *,
            enabled: bool | None = None,
            replace: bool = False,
    ) -> Module:
        """注册模块实例或类。必须在 :meth:`boot` 之前调用。"""
        with self._lock:
            if self._state not in (KernelState.NEW, KernelState.BOOTING):
                raise LifecycleError("内核已启动，不能再注册模块；请在 boot() 之前注册")
            if isinstance(module, type):
                if not issubclass(module, Module):
                    raise TypeError(f"只能注册 Module 子类，收到 {module!r}")
                module = module()
            if not isinstance(module, Module):
                raise TypeError(f"只能注册 Module 实例，收到 {module!r}")

            name = module.module_name()
            existing = self._modules.get(name)
            if existing is module:
                return module
            if existing is not None and not replace:
                raise DuplicateModule(name)
            if existing is not None:
                existing._clear_subscriptions()
                existing._set_state(ModuleState.STOPPED)
                self._log.info("替换模块 %s", name)

            module._attach(self)
            self._modules[name] = module
            for error in module.errors:
                if not (isinstance(error, type) and issubclass(error, Exception)):
                    raise TypeError(f"模块 {name!r} 声明的错误 {error!r} 不是异常类型")
                self._errors[f"{name}.{error.__name__}"] = error
            if enabled is not None:
                self._explicit_enabled[name] = bool(enabled)

        self._log.debug("模块已注册: %s (%s)", name, type(module).__name__)
        self.emit("module.registered", module=name, enabled=self.is_enabled(name))
        return module

    def register_all(
            self,
            modules: Iterable[Module | type[Module]],
            *,
            replace: bool = False,
    ) -> list[Module]:
        return [self.register(module, replace=replace) for module in modules]

    def load_registered(self, names: Iterable[str] | None = None) -> list[Module]:
        """实例化注册表中尚未注册的模块类；单个类构造失败不影响其它模块。"""
        wanted = list(names) if names is not None else list(self._registry.names())
        created: list[Module] = []
        for name in wanted:
            if name in self._modules:
                continue
            try:
                created.append(self.register(self._registry.get(name)()))
            except Exception as exc:  # noqa: BLE001 - 坏模块不阻断其它模块
                self._load_errors[name] = exc
                self.emit("module.failed", module=name, phase="construct", error=exc)
        self._flush_pending()
        return created

    def discover(self, *, group: str | None = None, paths: Iterable[str] = ()) -> list[str]:
        """从入口点或模块路径发现模块类。"""
        loaded: list[str] = []
        target_group = group or self.options.entry_points_group
        if target_group:
            loaded.extend(self._registry.load_entry_points(target_group))
        if paths:
            loaded.extend(self._registry.load_paths(paths))
        return loaded

    # ------------------------------------------------------------------ #
    # 启用 / 禁用
    # ------------------------------------------------------------------ #
    def enable(self, name: str) -> None:
        with self._lock:
            if self._state is not KernelState.NEW:
                raise LifecycleError("只能在 boot() 之前启用/禁用模块")
            if name not in self._modules:
                self._pending_enable.add(name)
                self._pending_disable.discard(name)
                return
            self._explicit_enabled[name] = True
        self._log.debug("启用模块 %s", name)
        self.emit("module.enabled", module=name)

    def disable(self, name: str) -> None:
        with self._lock:
            if self._state is not KernelState.NEW:
                raise LifecycleError("只能在 boot() 之前启用/禁用模块")
            if name not in self._modules:
                self._pending_disable.add(name)
                self._pending_enable.discard(name)
                return
            self._explicit_enabled[name] = False
        self._log.debug("禁用模块 %s", name)
        self.emit("module.disabled", module=name)

    def is_registered(self, name: str) -> bool:
        return name in self._modules

    @property
    def errors(self) -> dict[str, type[Exception]]:
        """模块注册上来的异常类型（键为 ``模块名.类名``）。"""
        return dict(self._errors)

    @property
    def load_errors(self) -> dict[str, Exception]:
        """实例化失败的模块（坏模块不会阻断其它模块）。"""
        return dict(self._load_errors)

    def is_enabled(self, name: str) -> bool:
        module = self._modules.get(name)
        if module is None:
            return False
        if name in self._explicit_enabled:
            return self._explicit_enabled[name]
        return True

    # ------------------------------------------------------------------ #
    # 查找
    # ------------------------------------------------------------------ #
    def has(self, name: str) -> bool:
        """模块存在、已启用且没有失败。"""
        module = self._modules.get(name)
        return (
            module is not None
            and self.is_enabled(name)
            and module.state is not ModuleState.FAILED
        )

    def get(self, name: str, default: Any = None) -> Module | Any:
        return self._modules[name] if self.has(name) else default

    def require(self, name: str) -> Module:
        """取得模块；未注册抛 :class:`ModuleNotFound`，禁用/失败抛 :class:`ModuleUnavailable`。"""
        module = self._modules.get(name)
        if module is None:
            raise ModuleNotFound(name)
        if not self.is_enabled(name) or module.state is ModuleState.FAILED:
            raise ModuleUnavailable(name)
        return module

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and self.has(name)

    def __getitem__(self, name: str) -> Module:
        return self.require(name)

    def modules(self, *, enabled_only: bool = False) -> tuple[Module, ...]:
        ordered = self._ordered()
        if enabled_only:
            ordered = [module for module in ordered if self.is_enabled(module.module_name())]
        return tuple(ordered)

    def module_names(self, *, enabled_only: bool = False) -> tuple[str, ...]:
        return tuple(module.module_name() for module in self.modules(enabled_only=enabled_only))

    # ------------------------------------------------------------------ #
    # 事件
    # ------------------------------------------------------------------ #
    def emit(
            self,
            topic: str,
            payload: Mapping[str, Any] | None = None,
            *,
            source: str | None = None,
            **fields: Any,
    ) -> list[Exception]:
        """同步发布事件，返回被隔离的处理器异常列表。

        负载既可以用字典传入，也可以直接写关键字参数::

            kernel.emit("module.started", module="demo")
            kernel.emit("module.started", {"module": "demo"})
        """
        data = dict(payload or {})
        data.update(fields)
        event = Event(topic=topic, payload=data, source=source)
        return self._events.publish(event)

    def on(
            self,
            topic: str,
            handler: Any,
            *,
            priority: int = 0,
            once: bool = False,
            source: str | None = None,
    ) -> Subscription:
        return self._events.subscribe(
            topic, handler, priority=priority, once=once, source=source
        )

    def off(self, subscription: Subscription) -> bool:
        return self._events.unsubscribe(subscription)

    def subscriptions(self, topic: str | None = None) -> tuple[Subscription, ...]:
        return self._events.subscriptions(topic)

    def has_subscribers(self, topic: str) -> bool:
        return self._events.has_subscribers(topic)

    def _wire_events(self, module: Module) -> None:
        """按模块自己的声明挂订阅；内核不认识任何具体主题。"""
        name = module.module_name()
        subscriptions = []
        for spec in module.events:
            handler = getattr(module, spec.handler)
            subscriptions.append(
                self.on(
                    spec.topic,
                    handler,
                    priority=spec.priority,
                    once=spec.once,
                    source=name,
                )
            )
        module._add_subscriptions(subscriptions)
        self._log.debug("事件订阅 module=%s count=%d", name, len(subscriptions))

    # ------------------------------------------------------------------ #
    # 模块间请求
    # ------------------------------------------------------------------ #
    def request(
            self,
            target: str,
            action: str,
            payload: Mapping[str, Any] | None = None,
            *,
            timeout: float | None = None,
            **kwargs: Any,
    ) -> Any:
        """调用目标模块的 ``handle_<action>``，可选超时。"""
        module = self.require(target)
        data = dict(payload or {})
        data.update(kwargs)
        limit = self.options.request_timeout if timeout is None else timeout
        started = now_ms()
        try:
            if limit:
                result = self._call_with_timeout(
                    lambda: module.handle(action, data),
                    timeout=limit,
                    label=f"{target}.{action}",
                )
            else:
                result = module.handle(action, data)
        except Exception:
            self._log.exception("请求失败 target=%s action=%s", target, action)
            raise
        self._log.debug(
            "请求完成 target=%s action=%s 耗时=%dms", target, action, now_ms() - started
        )
        return result

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    def boot(self) -> "Kernel":
        with self._lock:
            if self._state is not KernelState.NEW:
                raise LifecycleError(f"内核当前状态为 {self._state.value!r}，不能重复 boot()")
            self._state = KernelState.BOOTING

        try:
            self._env_token = set_env(self._env)
            self._log = get_logger("kernel")
            self._log.info("配置 path=%s", self._config.path or "(内存)")
            if self.options.discover:
                self.discover()
            if self.options.autoload:
                self.load_registered()
            self._flush_pending()
            self._log.info("内核启动中 modules=%d", len(self._modules))

            order = self._compute_order()
            self._order = [module.module_name() for module in order]

            for module in order:
                self._boot_module(module)
        except Exception as exc:
            with self._lock:
                self._state = KernelState.FAILED
            self._reset_env()
            self._log.exception("内核启动失败")
            self._rollback()
            self.emit("kernel.failed", error=exc)
            raise

        with self._lock:
            self._state = KernelState.RUNNING
            self._started_at = now_ms()
        self.emit("kernel.started", kernel=self)
        self._log.info("内核已启动 modules=%s", ", ".join(self._order) or "(无)")
        self._start_health_monitor()
        return self

    def shutdown(self) -> None:
        with self._lock:
            if self._state in (KernelState.STOPPED,):
                return
            if self._state is KernelState.NEW:
                self._state = KernelState.STOPPED
                return
            if self._state is KernelState.STOPPING:
                return
            self._state = KernelState.STOPPING

        self._stop_health_monitor()
        self._log.info("内核停止中")
        self.emit("kernel.stopping")
        errors: list[Exception] = []
        for module in reversed(self._ordered()):
            # READY 的模块 setup() 已经执行过，同样需要 stop() 释放资源。
            if module.state in (ModuleState.RUNNING, ModuleState.READY):
                error = self._stop(module)
                if error is not None:
                    errors.append(error)

        with self._lock:
            self._state = KernelState.STOPPED
        self._reset_env()
        self.emit("kernel.stopped", errors=errors)
        self._log.info("内核已停止 errors=%d", len(errors))

    def _reset_env(self) -> None:
        if self._env_token is not None:
            reset_env(self._env_token)
            self._env_token = None

    # ------------------------------------------------------------------ #
    # 内部：单模块生命周期
    # ------------------------------------------------------------------ #
    def _boot_module(self, module: Module) -> None:
        """启动单个模块；失败只影响它自己和依赖它的模块。"""
        name = module.module_name()
        if module.state is not ModuleState.REGISTERED or not self.is_enabled(name):
            return
        missing = self._unavailable_dependencies(module)
        if missing:
            module._set_state(ModuleState.FAILED)
            self._log.warning("模块 %s 跳过，依赖不可用: %s", name, ", ".join(missing))
            self.emit(
                "module.failed",
                module=name,
                phase="dependency",
                error=MissingDependency(name, missing[0], "不可用"),
            )
            return
        if not self._setup(module):
            return
        if not self._start(module):
            self._stop(module, phase="start-failure")
            module._set_state(ModuleState.FAILED)

    def _unavailable_dependencies(self, module: Module) -> list[str]:
        missing: list[str] = []
        for dependency in module.depends:
            target = self._modules.get(dependency)
            if target is None:
                missing.append(f"{dependency}(未注册)")
            elif not self.is_enabled(dependency):
                missing.append(f"{dependency}(已禁用)")
            elif target.state is not ModuleState.RUNNING:
                missing.append(f"{dependency}({target.state.value})")
        return missing

    def _setup(self, module: Module) -> bool:
        name = module.module_name()
        self._wire_events(module)
        try:
            self._call_with_timeout(
                module.setup, timeout=self.options.module_timeout, label=f"{name}.setup"
            )
        except Exception as exc:
            module._clear_subscriptions()
            module._set_state(ModuleState.FAILED)
            self._log.exception("模块 %s setup 失败", name)
            self.emit("module.failed", module=name, phase="setup", error=exc)
            return False
        module._set_state(ModuleState.READY)
        self._log.debug("模块就绪: %s", name)
        self.emit("module.ready", module=name)
        return True

    def _start(self, module: Module) -> bool:
        name = module.module_name()
        try:
            self._call_with_timeout(
                module.start, timeout=self.options.module_timeout, label=f"{name}.start"
            )
        except Exception as exc:
            module._clear_subscriptions()
            module._set_state(ModuleState.FAILED)
            self._log.exception("模块 %s start 失败", name)
            self.emit("module.failed", module=name, phase="start", error=exc)
            return False
        module._set_state(ModuleState.RUNNING)
        self._log.debug("模块启动: %s", name)
        self.emit("module.started", module=name)
        return True

    def _stop(self, module: Module, *, phase: str = "stop") -> Exception | None:
        name = module.module_name()
        try:
            self._call_with_timeout(
                module.stop, timeout=self.options.module_timeout, label=f"{name}.stop"
            )
        except Exception as exc:
            module._set_state(ModuleState.FAILED)
            self._log.exception("模块 %s %s 失败", name, phase)
            self.emit("module.failed", module=name, phase=phase, error=exc)
            return exc
        finally:
            module._clear_subscriptions()
        module._set_state(ModuleState.STOPPED)
        self._log.debug("模块停止: %s", name)
        self.emit("module.stopped", module=name)
        return None

    def report_error(self, name: str, error: BaseException) -> bool:
        """模块自报错误后尝试修复/重启；这是唯一的恢复入口。"""
        if self._state is not KernelState.RUNNING:
            return False
        module = self._modules.get(name)
        if module is None or not self.is_enabled(name):
            return False
        return self._recover(module, error)

    def _recover(self, module: Module, error: BaseException) -> bool:
        name = module.module_name()
        if name in self._recovering:
            return False
        attempts = max(1, int(self.options.recovery_attempts))
        self._recovering.add(name)
        try:
            for attempt in range(1, attempts + 1):
                self._log.warning(
                    "模块 %s 自报错误，第 %d/%d 次恢复: %s", name, attempt, attempts, error
                )
                self.emit(
                    "module.recovering",
                    module=name,
                    phase="recovery",
                    attempt=attempt,
                    error=error,
                )
                try:
                    repaired = bool(
                        self._call_with_timeout(
                            module.repair,
                            timeout=self.options.module_timeout,
                            label=f"{name}.repair",
                        )
                    )
                except Exception:  # noqa: BLE001 - 修复失败按未修复处理
                    repaired = False

                if repaired and self._activate(module):
                    self._log.info("模块 %s 已修复", name)
                    self.emit("module.recovered", module=name, attempt=attempt)
                    return True
                if module.restartable and self._restart(module):
                    self._log.info("模块 %s 已重启", name)
                    self.emit("module.recovered", module=name, attempt=attempt)
                    return True

            if module.state is ModuleState.RUNNING:
                self._stop(module, phase="recovery")
            module._set_state(ModuleState.FAILED)
            self._log.error("模块 %s 恢复失败，已停用", name)
            self.emit(
                "module.failed",
                module=name,
                phase="recovery",
                error=error,
                attempts=attempts,
            )
            return False
        finally:
            self._recovering.discard(name)

    def _activate(self, module: Module) -> bool:
        """把 repair 之后的模块带回 RUNNING；不负责重启，失败返回 False。"""
        if module.state is ModuleState.RUNNING:
            return True
        if module.state is ModuleState.READY:
            return self._start(module)
        if module.state is ModuleState.REGISTERED:
            return self._setup(module) and self._start(module)
        return False

    def _restart(self, module: Module) -> bool:
        name = module.module_name()
        self._log.info("重启模块 %s", name)
        if module.state is ModuleState.RUNNING:
            if self._stop(module, phase="recovery") is not None:
                return False
        module._set_state(ModuleState.REGISTERED)
        if not self._setup(module):
            return False
        if not self._start(module):
            self._stop(module, phase="recovery")
            module._set_state(ModuleState.FAILED)
            return False
        return True

    def _call_with_timeout(self, func: Any, *, timeout: float | None, label: str) -> Any:
        return run_with_timeout(func, timeout=timeout, label=label)

    def _rollback(self) -> None:
        for module in reversed(self._ordered()):
            if module.state in (ModuleState.RUNNING, ModuleState.READY):
                self._stop(module, phase="rollback")

    # ------------------------------------------------------------------ #
    # 内部：编排
    # ------------------------------------------------------------------ #
    def _compute_order(self) -> list[Module]:
        """依赖优先排序；遇到环时在环内随机挑一个先启动。"""
        modules = list(self._modules.values())
        by_name = {module.module_name(): module for module in modules}
        position = {module.module_name(): index for index, module in enumerate(modules)}
        dependencies: dict[str, list[str]] = {}
        for module in modules:
            name = module.module_name()
            dependencies[name] = [
                dependency
                for dependency in (*module.depends, *module.optional_depends)
                if dependency in by_name and dependency != name
            ]

        done: set[str] = set()
        remaining = set(dependencies)
        ordered: list[Module] = []
        while remaining:
            ready = [
                name
                for name in remaining
                if all(dependency in done for dependency in dependencies[name])
            ]
            if ready:
                ready.sort(key=lambda name: (by_name[name].priority, position[name]))
                chosen = ready[0]
            else:
                cycle = self._find_cycle(dependencies, remaining)
                candidates = sorted(set(cycle)) or sorted(remaining)
                chosen = random.choice(candidates)
            ordered.append(by_name[chosen])
            remaining.remove(chosen)
            done.add(chosen)
        return ordered

    @staticmethod
    def _find_cycle(
        dependencies: Mapping[str, list[str]], remaining: Iterable[str]
    ) -> list[str]:
        remaining = set(remaining)
        color: dict[str, int] = {name: 0 for name in remaining}
        stack: list[str] = []

        def visit(node: str) -> list[str] | None:
            color[node] = 1
            stack.append(node)
            for dependency in dependencies[node]:
                if dependency not in remaining:
                    continue
                if color[dependency] == 1:
                    return stack[stack.index(dependency):] + [dependency]
                if color[dependency] == 0:
                    found = visit(dependency)
                    if found:
                        return found
            stack.pop()
            color[node] = 2
            return None

        for name in sorted(remaining):
            if color[name] == 0:
                found = visit(name)
                if found:
                    return found
        return []

    def _flush_pending(self) -> None:
        for name in sorted(self._pending_enable):
            if name not in self._modules:
                raise ModuleNotFound(name)
            self._explicit_enabled[name] = True
        for name in sorted(self._pending_disable):
            if name not in self._modules:
                raise ModuleNotFound(name)
            self._explicit_enabled[name] = False
        self._pending_enable.clear()
        self._pending_disable.clear()

    def _ordered(self) -> list[Module]:
        if self._order:
            return [self._modules[name] for name in self._order if name in self._modules]
        return list(self._modules.values())

    # ------------------------------------------------------------------ #
    # 诊断
    # ------------------------------------------------------------------ #
    def check_health(self, *, timeout: float | None = None) -> dict[str, dict[str, Any]]:
        """对运行中的模块逐个调用 ``health()``，带超时；异常记为不健康。"""
        limit = self.options.module_timeout if timeout is None else timeout
        result: dict[str, dict[str, Any]] = {}
        for module in self._ordered():
            if module.state is not ModuleState.RUNNING:
                continue
            name = module.module_name()
            try:
                data = self._call_with_timeout(
                    module.health, timeout=limit, label=f"{name}.health"
                )
                payload = dict(data) if isinstance(data, Mapping) else {"ok": bool(data)}
                payload.setdefault("ok", True)
                result[name] = payload
            except Exception as exc:
                result[name] = {"ok": False, "error": str(exc)}
        for name, data in result.items():
            if not data.get("ok", True):
                self._log.debug("健康检查异常 module=%s data=%s", name, data)
        return result

    def health(self) -> dict[str, Any]:
        checks = self.check_health()
        modules: dict[str, Any] = {}
        for module in self._ordered():
            name = module.module_name()
            entry: dict[str, Any] = {
                "enabled": self.is_enabled(name),
                "state": module.state.value,
            }
            if name in checks:
                entry.update(checks[name])
            elif module.state is ModuleState.FAILED:
                entry["ok"] = False
            modules[name] = entry
        return {
            "state": self._state.value,
            "uptime": round(self.uptime, 3),
            "modules": modules,
            "load_errors": {name: str(error) for name, error in self._load_errors.items()},
        }

    def _start_health_monitor(self) -> None:
        interval = self.options.health_interval
        if interval <= 0:
            return
        self._health_stop.clear()
        self._health_thread = threading.Thread(
            target=self._health_loop, name="kernel-health", daemon=True
        )
        self._health_thread.start()

    def _stop_health_monitor(self) -> None:
        self._health_stop.set()
        thread = self._health_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
        self._health_thread = None

    def _health_loop(self) -> None:
        """周期探测只报告异常；不记录状态、不触发恢复。"""
        while not self._health_stop.wait(self.options.health_interval):
            if self._state is not KernelState.RUNNING:
                return
            for name, data in self.check_health().items():
                if not data.get("ok", True):
                    self._log.warning("健康检查异常 module=%s data=%s", name, data)

    def _on_event_error(self, event: Event, exc: Exception) -> None:
        self._event_errors += 1
        self._log.warning("事件处理器异常 topic=%s: %s", event.topic, exc)

    def __repr__(self) -> str:
        return (
            f"<Kernel state={self._state.value} "
            f"modules={list(self._modules)}>"
        )


__all__ = ["Kernel", "KernelOptions", "KernelState"]
