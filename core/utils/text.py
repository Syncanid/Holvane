"""与平台无关的文本处理。"""

from __future__ import annotations

import json
import re
from typing import Any


def stringify(value: Any) -> str:
    """把日志/控制台字段转成一行文本。"""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)) or value is None:
        return repr(value)
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return repr(value)


def to_snake_case(name: str) -> str:
    """``HttpModule`` -> ``http``，``ConfigModule`` -> ``config``。"""
    step_one = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", name)
    step_two = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", step_one)
    result = step_two.lower()
    if result.endswith("_module"):
        result = result[: -len("_module")]
    return result
