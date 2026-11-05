"""内核对外公开的契约。

这里只有内核自己的东西：模块、注册表、事件、生命周期、异常。
任何具体模块的类型都不出现在这里。
"""

from .lib.config import Config, ConfigError
from .lib.console import Console, get_console, set_console
from .lib.errors import (
    DuplicateModule,
    FrameworkError,
    LifecycleError,
    MissingDependency,
    ModuleContractError,
    ModuleError,
    ModuleNotFound,
    ModuleUnavailable,
    UnknownAction,
)
from .lib.events import Event, EventBus, EventSpec, Subscription, topic_matches
from .lib.log import configure as configure_logging, get_logger, setup_logging, shutdown_logging
from .lib.registry import ModuleRegistry, get_registry, register
from .module import Module, ModuleState
from .utils.env import current_env
from .utils.text import to_snake_case
from .utils.time import format_ms, now_ms, to_local, to_utc
from .kernel import Kernel, KernelOptions, KernelState

__all__ = [
    "Config",
    "ConfigError",
    "Console",
    "DuplicateModule",
    "Event",
    "EventBus",
    "EventSpec",
    "FrameworkError",
    "Kernel",
    "KernelOptions",
    "KernelState",
    "LifecycleError",
    "MissingDependency",
    "Module",
    "ModuleContractError",
    "ModuleError",
    "ModuleNotFound",
    "ModuleRegistry",
    "ModuleState",
    "ModuleUnavailable",
    "Subscription",
    "UnknownAction",
    "configure_logging",
    "current_env",
    "format_ms",
    "get_console",
    "get_logger",
    "get_registry",
    "now_ms",
    "register",
    "set_console",
    "setup_logging",
    "shutdown_logging",
    "to_local",
    "to_snake_case",
    "to_utc",
    "topic_matches",
]
