"""与平台无关的通用组件：控制台、日志。"""

from .config import Config, ConfigError
from .console import Console, get_console, set_console
from .log import configure, get_logger, setup_logging, shutdown_logging

__all__ = [
    "Config",
    "ConfigError",
    "Console",
    "configure",
    "get_console",
    "get_logger",
    "set_console",
    "setup_logging",
    "shutdown_logging",
]
