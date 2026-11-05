"""与平台无关的超时执行。

Python 线程无法强杀；超时只能做到“不再等待”，被调用的线程可能仍在运行。
"""

from __future__ import annotations

import contextvars
import logging
import threading
from typing import Any, Callable

_log = logging.getLogger("core.timeout")


def run_with_timeout(
    func: Callable[[], Any],
    *,
    timeout: float | None,
    label: str = "operation",
) -> Any:
    if not timeout or timeout <= 0:
        return func()
    box: dict[str, Any] = {}
    context = contextvars.copy_context()

    def runner() -> None:
        try:
            box["value"] = func()
        except BaseException as exc:  # noqa: BLE001 - 原样抛回调用方
            box["error"] = exc

    thread = threading.Thread(
        target=context.run, args=(runner,), name=f"timeout:{label}", daemon=True
    )
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        _log.warning("%s 超过 %gs 未完成（线程仍在运行）", label, timeout)
        raise TimeoutError(f"{label} 超过 {timeout:g}s 未完成")
    if "error" in box:
        raise box["error"]
    return box.get("value")
