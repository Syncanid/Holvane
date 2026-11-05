"""时间。

只提供毫秒级 Unix 时间戳，以及把它转成 UTC / 本地时区（仅供展示）。
存储、传输、比较一律用整数毫秒；格式化只发生在显示层。
"""

from __future__ import annotations

import time
from datetime import datetime, timezone


def now_ms() -> int:
    """当前 Unix 时间戳，毫秒。"""
    return int(time.time() * 1000)


def to_utc(timestamp_ms: int) -> datetime:
    """毫秒时间戳 -> UTC datetime（仅用于展示/格式化）。"""
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)


def to_local(timestamp_ms: int) -> datetime:
    """毫秒时间戳 -> 本地时区 datetime（仅用于展示/格式化）。"""
    return datetime.fromtimestamp(timestamp_ms / 1000).astimezone()


def format_ms(moment: datetime, *, fmt: str = "%Y-%m-%d %H:%M:%S.%f") -> str:
    """只做格式化，不关心 moment 是 UTC 还是本地时间。"""
    return moment.strftime(fmt)
