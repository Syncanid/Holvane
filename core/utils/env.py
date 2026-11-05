"""当前环境的上下文。

内核启动时把自己持有的 env 注入 ``ContextVar``，同一上下文里的任何代码都能用
``current_env()`` 读到；不读 ``os.environ``，也不修改它。多个内核嵌套 boot 时，
token 保证 shutdown 能恢复到上一层。
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from types import MappingProxyType
from typing import Mapping

_current: ContextVar[Mapping[str, str]] = ContextVar(
    "current_env", default=MappingProxyType({})
)


def current_env() -> Mapping[str, str]:
    """当前上下文中生效的环境。"""
    return _current.get()


def set_env(env: Mapping[str, str]) -> Token:
    return _current.set(MappingProxyType(dict(env)))


def reset_env(token: Token) -> None:
    _current.reset(token)
