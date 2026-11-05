"""核心事件系统。

模块之间不直接互相 import，也不互相持有引用；跨模块协作通过两种方式完成：

* **事件（event）**：一对多广播，发布方不关心谁在听。
* **请求（request）**：一对一调用，调用方明确知道目标模块与动作。

事件主题使用点号分段，例如 ``demo.changed``、``demo.record``。订阅时支持两种
通配符：

* ``*``  匹配一个分段，如 ``demo.*`` 匹配 ``demo.record``；
* ``**`` 匹配剩余所有分段，如 ``module.**`` 匹配 ``module.started`` 和
  ``module.failed.setup``。

分发是同步的、按优先级（数值大者先）进行。任何处理器的异常都会被隔离：记录到
``on_error`` 回调并继续分发，不会影响其它订阅者。
"""

from __future__ import annotations

import itertools
import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from ..utils.time import now_ms

_log = logging.getLogger("core.events")
Handler = Callable[["Event"], None]


@dataclass(frozen=True)
class Event:
    """一次事件分发携带的不可变数据。"""

    topic: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    source: str | None = None
    timestamp: int = field(default_factory=now_ms)

    def get(self, key: str, default: Any = None) -> Any:
        return self.payload.get(key, default)

    def __getitem__(self, key: str) -> Any:
        return self.payload[key]

    def __contains__(self, key: object) -> bool:
        return key in self.payload


@dataclass(frozen=True)
class EventSpec:
    """模块声明式订阅：主题 + 本模块上的处理方法名。"""

    topic: str
    handler: str
    priority: int = 0
    once: bool = False

    @classmethod
    def coerce(cls, value: "EventSpec | tuple[str, str]") -> "EventSpec":
        if isinstance(value, cls):
            return value
        if isinstance(value, tuple) and len(value) == 2:
            topic, handler = value
            return cls(str(topic), str(handler))
        raise TypeError(f"无法把 {value!r} 解析为 EventSpec，请使用 EventSpec(...) 或 (topic, handler)")


@dataclass(frozen=True)
class Subscription:
    """订阅句柄，可传给 :meth:`Kernel.off` 取消订阅。"""

    id: int
    topic: str
    handler: Handler
    priority: int = 0
    once: bool = False
    source: str | None = None

    @property
    def handler_name(self) -> str:
        return getattr(self.handler, "__qualname__", repr(self.handler))


def topic_matches(pattern: str, topic: str) -> bool:
    """判断 ``pattern`` 是否匹配 ``topic``。"""
    if pattern == topic or pattern == "*":
        return True
    pattern_parts = pattern.split(".")
    topic_parts = topic.split(".")
    for index, part in enumerate(pattern_parts):
        if part == "**":
            return True
        if index >= len(topic_parts):
            return False
        if part != "*" and part != topic_parts[index]:
            return False
    return len(pattern_parts) == len(topic_parts)


class EventBus:
    """线程安全的同步事件总线。"""

    def __init__(self, on_error: Callable[[Event, Exception], None] | None = None) -> None:
        self._lock = threading.RLock()
        self._subscriptions: list[Subscription] = []
        self._counter = itertools.count(1)
        self._on_error = on_error

    # ------------------------------------------------------------------ #
    # 订阅管理
    # ------------------------------------------------------------------ #
    def subscribe(
            self,
            topic: str,
            handler: Handler,
            *,
            priority: int = 0,
            once: bool = False,
            source: str | None = None,
    ) -> Subscription:
        if not callable(handler):
            raise TypeError(f"事件处理器必须可调用，收到 {handler!r}")
        if not topic:
            raise ValueError("事件主题不能为空")
        subscription = Subscription(
            id=next(self._counter),
            topic=topic,
            handler=handler,
            priority=priority,
            once=once,
            source=source,
        )
        with self._lock:
            self._subscriptions.append(subscription)
        _log.debug(
            "订阅事件 topic=%s source=%s priority=%s once=%s",
            topic,
            source or "-",
            priority,
            once,
        )
        return subscription

    def unsubscribe(self, subscription: Subscription) -> bool:
        with self._lock:
            try:
                self._subscriptions.remove(subscription)
            except ValueError:
                return False
        _log.debug("取消订阅 topic=%s source=%s", subscription.topic, subscription.source or "-")
        return True

    def clear(self) -> None:
        with self._lock:
            count = len(self._subscriptions)
            self._subscriptions.clear()
        _log.debug("清空所有订阅 count=%d", count)

    def subscriptions(self, topic: str | None = None) -> tuple[Subscription, ...]:
        with self._lock:
            items = tuple(self._subscriptions)
        if topic is None:
            return items
        return tuple(sub for sub in items if topic_matches(sub.topic, topic))

    def has_subscribers(self, topic: str) -> bool:
        with self._lock:
            return any(topic_matches(sub.topic, topic) for sub in self._subscriptions)

    # ------------------------------------------------------------------ #
    # 分发
    # ------------------------------------------------------------------ #
    def publish(self, event: Event) -> list[Exception]:
        """同步分发事件，返回所有处理器抛出的异常（已隔离）。"""
        with self._lock:
            matched = [sub for sub in self._subscriptions if topic_matches(sub.topic, event.topic)]
            for sub in matched:
                if sub.once:
                    self._subscriptions.remove(sub)
        matched.sort(key=lambda sub: (-sub.priority, sub.id))
        _log.debug("发布事件 topic=%s handlers=%d", event.topic, len(matched))

        errors: list[Exception] = []
        for sub in matched:
            try:
                sub.handler(event)
            except Exception as exc:  # noqa: BLE001 - 事件处理器必须互相隔离
                errors.append(exc)
                if self._on_error is not None:
                    try:
                        self._on_error(event, exc)
                    except Exception:  # noqa: BLE001 - 错误回调本身也不能打断分发
                        pass
        return errors


__all__ = [
    "Event",
    "EventBus",
    "EventSpec",
    "Handler",
    "Subscription",
    "topic_matches",
]
