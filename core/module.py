"""模块基类与生命周期定义。

框架里"一切皆模块"：配置、日志、标准输入输出、数据库，以及用户自己的业务逻辑，
都是 :class:`Module` 的子类。模块只依赖核心提供的契约，不依赖彼此的具体实现：

* 通过 :meth:`Module.require` / :meth:`Module.has` 确认另一个模块是否存在；
* 通过 :meth:`Module.request` 调用另一个模块公开的动作；
* 通过 :meth:`Module.emit` / :meth:`Module.on` 收发事件。

生命周期由内核驱动：``setup()`` -> ``start()`` -> ``stop()``。某个模块在任一阶段
抛异常或超时只会让它自己标记为 ``FAILED``，其它模块继续启动；硬依赖它的模块随后
被跳过（``phase="dependency"``）。

事件契约
--------
内核不认识任何具体事件主题：模块在 ``events`` 里声明自己关心什么，内核负责在
初始化时挂载、停止时卸载。没有声明就没有订阅。

.. code-block:: python

    class Cache(Module):
        name = "cache"
        events = (
            EventSpec("module.started", "on_module_started"),
            ("demo.changed", "on_demo_changed"),
        )

        def on_module_started(self, event):
            ...
"""

from __future__ import annotations

import logging
import re
from enum import Enum
from typing import TYPE_CHECKING, Any, ClassVar, Mapping, Sequence

from .lib.errors import ModuleContractError, UnknownAction
from .lib.events import EventSpec, Subscription
from .lib.log import get_logger
from .utils.text import to_snake_case

if TYPE_CHECKING:  # pragma: no cover - 仅用于类型检查
    from .kernel import Kernel

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")


class ModuleState(str, Enum):
    REGISTERED = "registered"
    READY = "ready"
    RUNNING = "running"
    STOPPED = "stopped"
    FAILED = "failed"


class Module:
    """所有模块的基类。

    子类通常只需要声明类属性并覆盖生命周期钩子，构造函数保持无参（内核负责实例化）。
    """

    #: 模块名，全局唯一；留空时由类名推导（``HttpModule`` -> ``http``）。
    name: ClassVar[str] = ""
    #: 一句话描述，用于诊断输出。
    description: ClassVar[str] = ""
    #: 硬依赖：必须已注册且已启用，否则启动失败。
    depends: ClassVar[Sequence[str]] = ()
    #: 软依赖：存在时保证先于本模块初始化，不存在则跳过。
    optional_depends: ClassVar[Sequence[str]] = ()
    #: 同层模块的初始化顺序，数值小者先。
    priority: ClassVar[int] = 0
    #: 声明式事件订阅，元素为 :class:`EventSpec` 或 ``(topic, handler)``。
    events: ClassVar[Sequence[EventSpec | tuple[str, str]]] = ()
    #: 本模块自己的异常类型，注册时由内核登记到 ``kernel.errors``。
    errors: ClassVar[Sequence[type[Exception]]] = ()
    #: 收到本模块的错误汇报后，是否允许内核重启它（stop -> setup -> start）。
    restartable: ClassVar[bool] = False

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        specs = tuple(EventSpec.coerce(spec) for spec in cls.events)
        for spec in specs:
            handler = getattr(cls, spec.handler, None)
            if handler is None or not callable(handler):
                raise ModuleContractError(
                    f"{cls.__name__} 声明了事件 {spec.topic!r} 的处理器 {spec.handler!r}，"
                    f"但该类没有这个方法"
                )
        cls.events = specs
        if cls.name and not _NAME_RE.match(cls.name):
            raise ModuleContractError(
                f"{cls.__name__}.name={cls.name!r} 不合法，必须匹配 {_NAME_RE.pattern}"
            )

    def __init__(self) -> None:
        self._kernel: Kernel | None = None
        self._state: ModuleState = ModuleState.REGISTERED
        self._subscriptions: list[Subscription] = []

    # ------------------------------------------------------------------ #
    # 基本信息
    # ------------------------------------------------------------------ #
    @classmethod
    def module_name(cls) -> str:
        return cls.name or to_snake_case(cls.__name__)

    @property
    def kernel(self) -> "Kernel":
        if self._kernel is None:
            raise RuntimeError(f"模块 {self.module_name()!r} 尚未注册到内核")
        return self._kernel

    @property
    def state(self) -> ModuleState:
        return self._state

    @property
    def log(self) -> logging.Logger:
        """以模块名命名的 logger（标准库 logging，core 统一装配）。"""
        return get_logger(self.module_name())

    @property
    def subscriptions(self) -> tuple[Subscription, ...]:
        return tuple(self._subscriptions)

    # ------------------------------------------------------------------ #
    # 生命周期钩子（子类按需覆盖）
    # ------------------------------------------------------------------ #
    def setup(self) -> None:
        """初始化：读取配置、建立内部结构。此时依赖模块已经就绪。"""

    def start(self) -> None:
        """启动：开始对外提供服务。"""

    def stop(self) -> None:
        """停止：释放资源。内核会在本方法返回后自动取消本模块的所有订阅。"""

    def health(self) -> Mapping[str, Any]:
        return {"state": self._state.value, "ok": self._state is not ModuleState.FAILED}

    def repair(self) -> bool:
        """非破坏性修复，返回 True 表示已恢复；默认不处理。

        只有本模块调用 :meth:`report_error` 时才会被内核调用。不得删除数据或改变
        外部状态，只能把模块自己的内部状态恢复到可用。
        """
        return False

    def report_error(self, error: BaseException) -> bool:
        """模块自报错误；这是触发修复/重启的唯一入口。"""
        return bool(self.kernel.report_error(self.module_name(), error))

    # ------------------------------------------------------------------ #
    # 事件
    # ------------------------------------------------------------------ #
    def emit(self, topic: str, **payload: Any) -> list[Exception]:
        """发布事件，``source`` 自动标记为本模块名。"""
        return self.kernel.emit(topic, payload, source=self.module_name())

    def on(
            self,
            topic: str,
            handler: Any,
            *,
            priority: int = 0,
            once: bool = False,
    ) -> Subscription:
        """运行期临时订阅；模块停止时会被内核自动取消。"""
        subscription = self.kernel.on(
            topic, handler, priority=priority, once=once, source=self.module_name()
        )
        self._subscriptions.append(subscription)
        return subscription

    def off(self, subscription: Subscription) -> bool:
        if subscription in self._subscriptions:
            self._subscriptions.remove(subscription)
        return self.kernel.off(subscription)

    # ------------------------------------------------------------------ #
    # 模块间调用
    # ------------------------------------------------------------------ #
    def has(self, name: str) -> bool:
        return self.kernel.has(name)

    def require(self, name: str) -> "Module":
        return self.kernel.require(name)

    def request(self, target: str, action: str, payload: Mapping[str, Any] | None = None, **kwargs: Any) -> Any:
        return self.kernel.request(target, action, payload, **kwargs)

    def handle(self, action: str, payload: Mapping[str, Any]) -> Any:
        """默认的动作分发：调用 ``handle_<action>(**payload)``。"""
        handler = getattr(self, f"handle_{action}", None)
        if handler is None or not callable(handler):
            self.log.warning("未知动作 module=%s action=%s", self.module_name(), action)
            raise UnknownAction(self.module_name(), action)
        self.log.debug("处理动作 module=%s action=%s", self.module_name(), action)
        return handler(**dict(payload))

    # ------------------------------------------------------------------ #
    # 内核内部使用
    # ------------------------------------------------------------------ #
    def _attach(self, kernel: "Kernel") -> None:
        self._kernel = kernel
        if not hasattr(self, "_state"):
            self._state = ModuleState.REGISTERED
        if not hasattr(self, "_subscriptions"):
            self._subscriptions = []

    def _set_state(self, state: ModuleState) -> None:
        self._state = state

    def _add_subscriptions(self, subscriptions: Sequence[Subscription]) -> None:
        self._subscriptions.extend(subscriptions)

    def _clear_subscriptions(self) -> None:
        for subscription in self._subscriptions:
            self.kernel.off(subscription)
        self._subscriptions.clear()

    def __repr__(self) -> str:
        return f"<{type(self).__name__} name={self.module_name()!r} state={self._state.value}>"
