"""日志：基于标准库 :mod:`logging` 的统一出口。

模块不直接 ``print``：全部通过 ``self.log``（即 ``logging.getLogger(模块名)``）输出。
装配参考现成项目：固定格式、控制台彩色、按天文件、幂等、抑制嘈杂库。

默认写 ``data/logs/app_YYYY-MM-DD.log``，同时输出控制台；路径可用 env 覆盖。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from ..compat import enable_ansi, ensure_dir, ensure_utf8_streams
from ..utils.time import now_ms, to_local
from .config import Config
from .console import get_console

CONSOLE_FORMAT = "%(asctime)s - %(levelname)s - %(name)s:%(lineno)d - %(message)s"
FILE_FORMAT = "%(asctime)s - %(levelname)s - %(name)s:%(lineno)d - %(message)s"
CONSOLE_DATE_FORMAT = "%H:%M:%S"
LOG_FILE_PREFIX = "app"
LOG_DIR = "data/logs"
NOISY_LOGGERS = ("httpx", "chromadb", "aiosqlite", "apscheduler", "watchdog")

LEVEL_COLORS = {
    "DEBUG": "\033[36m",
    "INFO": "\033[32m",
    "WARNING": "\033[33m",
    "ERROR": "\033[31m",
    "CRITICAL": "\033[31;47m",
}
RESET = "\033[0m"


class ColorFormatter(logging.Formatter):
    """控制台彩色格式：只给级别名上色，文件日志用普通 Formatter。"""

    def __init__(
        self,
        fmt: str = CONSOLE_FORMAT,
        *,
        use_color: bool = True,
        datefmt: str = CONSOLE_DATE_FORMAT,
    ) -> None:
        super().__init__(fmt, datefmt=datefmt)
        self.use_color = use_color

    def format(self, record: logging.LogRecord) -> str:
        if not self.use_color:
            return super().format(record)
        levelname = record.levelname
        color = LEVEL_COLORS.get(levelname)
        if color is not None:
            record.levelname = f"{color}{levelname}{RESET}"
        try:
            return super().format(record)
        finally:
            record.levelname = levelname


class DailyFileHandler(logging.FileHandler):
    """文件名带日期；跨天时自动切到新文件。"""

    def __init__(self, directory: str | Path, prefix: str, *, encoding: str = "utf-8") -> None:
        self._directory = Path(directory)
        self._prefix = prefix
        self._date = self._today()
        ensure_dir(self._directory)
        super().__init__(str(self._path_for(self._date)), mode="a", encoding=encoding, delay=True)

    @staticmethod
    def _today() -> str:
        return to_local(now_ms()).strftime("%Y-%m-%d")

    def _path_for(self, day: str) -> Path:
        return self._directory / f"{self._prefix}_{day}.log"

    def emit(self, record: logging.LogRecord) -> None:
        day = self._today()
        if day != self._date:
            if self.stream is not None:
                try:
                    self.stream.close()
                finally:
                    self.stream = None
            self._date = day
            self.baseFilename = str(self._path_for(day))
        super().emit(record)


def _as_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on", "y"}:
        return True
    if normalized in {"0", "false", "no", "off", "n"}:
        return False
    raise ValueError(f"{value!r} 不是布尔值")


def _log_config() -> Config:
    config = Config()
    config.register("log.level", "INFO")
    config.register("log.dir", LOG_DIR)
    config.register("log.file", LOG_FILE_PREFIX)
    config.register("log.console", True, cast=_as_bool)
    config.register("log.color", "always")
    return config


class ConsoleHandler(logging.Handler):
    """写全局 Console；``set_console()`` 会同时切换日志出口。"""

    def __init__(self, *, err: bool = False) -> None:
        super().__init__()
        self._err = err

    def emit(self, record: logging.LogRecord) -> None:
        try:
            get_console().write(self.format(record) + "\n", err=self._err)
        except Exception:  # noqa: BLE001 - 日志失败不能反过来炸业务
            self.handleError(record)


def _level_value(level: str | int) -> int:
    text = str(level).strip()
    if text.isdigit():
        return int(text)
    return getattr(logging, text.upper(), logging.INFO)


def _resolve_color(value: object, console: object) -> bool:
    """always/true 强制开；never/false 强制关；auto 按 TTY + NO_COLOR 检测。"""
    text = str(value).strip().lower()
    if text in {"never", "false", "0", "off", "no"}:
        return False
    if text in {"always", "true", "1", "on", "yes"}:
        enable_ansi()
        return True
    if "NO_COLOR" in os.environ:
        return False
    return bool(getattr(console, "supports_color", False))


def setup_logging(config: Config | None = None) -> logging.Logger:
    """装配根 logger；传 ``Config``，否则只读 env 和默认值。"""
    cfg = config if config is not None else _log_config()

    # 注入的 Config 未必注册过 log.*；用显式默认值兜底，不要求调用方先注册。
    level = cfg.get("log.level", "INFO")
    log_dir = cfg.get("log.dir", LOG_DIR)
    file_prefix = cfg.get("log.file", LOG_FILE_PREFIX)
    console = cfg.get_bool("log.console", True)

    ensure_utf8_streams()
    root = logging.getLogger()
    root.setLevel(_level_value(level))
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()

    use_color = _resolve_color(cfg.get("log.color", "always"), get_console())
    if console:
        console_handler = ConsoleHandler()
        console_handler.setFormatter(ColorFormatter(use_color=use_color))
        root.addHandler(console_handler)

    file_path: Path | None = None
    if log_dir:
        directory = ensure_dir(log_dir)
        file_handler = DailyFileHandler(directory, file_prefix)
        file_handler.setFormatter(logging.Formatter(FILE_FORMAT))
        root.addHandler(file_handler)
        file_path = file_handler._path_for(file_handler._date)

    for name in NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)

    get_logger("core.log").info(
        "日志已装配 level=%s console=%s color=%s file=%s",
        str(level).upper(),
        "on" if console else "off",
        "on" if use_color else "off",
        str(file_path) if file_path is not None else "(仅控制台)",
    )
    return logging.getLogger("app")


def configure(config: Config | None = None) -> logging.Logger:
    """``setup_logging`` 的别名。"""
    return setup_logging(config)


def get_logger(name: str = "app") -> logging.Logger:
    """取模块 logger；第一次取且根 logger 没 handler 时自动装配。"""
    if not logging.getLogger().handlers:
        setup_logging()
    return logging.getLogger(name)


def shutdown_logging() -> None:
    """关闭并摘掉根 logger 的所有 handler（进程退出前或测试清理时用）。"""
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
