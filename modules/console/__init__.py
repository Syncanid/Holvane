"""交互控制台模块：指令树注册、执行和 Python 代码片段。

指令系统参考 Afedium：``register("kernel")`` 返回节点，继续 ``subcommand(...)``；
处理器签名 ``handler(ctx, args)``，返回值/异常统一进 ``CommandContext``。

其它模块可以这样注册::

    console = kernel.require("console")
    console.register("bookmarks", handler, "列出收藏")
    group = console.register("bookmarks")
    group.subcommand("add", add_handler, "添加收藏")
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import sys
import threading
from pathlib import Path
from typing import Any, Callable, Sequence

import core
from core.lib.errors import FrameworkError
from core.lib.registry import register
from core.module import Module
from core.utils.time import format_ms, now_ms, to_local

from .command import CommandContext, CommandNode, CommandRoot

_log = logging.getLogger("console")


class ConsoleCommandError(FrameworkError):
    """控制台指令注册或执行失败。"""


Handler = Callable[..., Any]


@register
class ConsoleModule(Module):
    name = "console"
    description = "交互控制台：指令树注册、执行与 Python 代码"
    priority = 90
    errors = (ConsoleCommandError,)

    def setup(self) -> None:
        self._root = CommandRoot()
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._py_namespace: dict[str, Any] = dict(globals())
        self._py_namespace.update(
            {"kernel": self.kernel, "console": self, "self": self}
        )
        config = self.kernel.config
        config.register("console.repl", True, cast=bool)
        config.register("console.prompt", "console> ")
        self._register_builtin_commands()

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    def start(self) -> None:
        self.log.info("控制台就绪 commands=%d", len(self._root.subcommands))
        if not self.kernel.config.get_bool("console.repl", True):
            return
        self._stop.clear()
        context = contextvars.copy_context()
        self._thread = threading.Thread(
            target=context.run, args=(self._loop,), name="console-repl", daemon=True
        )
        self._thread.start()
        self.log.info(
            "控制台 REPL 已启动 interactive=%s", self.kernel.console.interactive
        )

    def stop(self) -> None:
        self._stop.set()
        self._thread = None

    # ------------------------------------------------------------------ #
    # 指令注册
    # ------------------------------------------------------------------ #
    def register(
        self,
        path: str,
        handler: Handler | None = None,
        description: str = "",
    ) -> CommandNode:
        node = self._root.register(path, handler, description)
        self.log.debug("注册控制台指令 path=%s handler=%s", path, bool(handler))
        return node

    def register_command(
        self,
        path: str,
        handler: Handler,
        *,
        help: str = "",
        description: str = "",
        aliases: Sequence[str] = (),
    ) -> CommandNode:
        node = self.register(path, handler, description or help)
        for alias in aliases:
            self.register(str(alias), handler, description or help)
        return node

    def command(
        self,
        path: str,
        *,
        description: str = "",
        help: str = "",
    ) -> Callable[[Handler], Handler]:
        def decorator(function: Handler) -> Handler:
            self.register(path, function, description or help)
            return function

        return decorator

    def unregister(self, path: str) -> str:
        return self._root.unregister(path)

    def commands(self) -> dict[str, str]:
        return {
            name: node.description
            for name, node in sorted(self._root.subcommands.items())
        }

    # ------------------------------------------------------------------ #
    # 执行
    # ------------------------------------------------------------------ #
    def execute(self, line: str) -> str:
        """执行一行指令，返回可直接打印的文本。"""
        text = str(line).strip()
        if not text:
            return ""
        arguments = text.split()
        result = self._root.dispatch(arguments)
        self.log.debug("控制台指令 line=%r", text)
        return result

    def dispatch(self, args: Sequence[str] | str, client_id: Any = None) -> str:
        if isinstance(args, str):
            args = args.split()
        return self._root.dispatch(args, client_id=client_id)

    def handle_execute(self, line: str) -> str:
        return self.execute(line)

    # ------------------------------------------------------------------ #
    # 内置指令
    # ------------------------------------------------------------------ #
    def _register_builtin_commands(self) -> None:
        kernel = self.register("kernel", description="内核指令组")
        kernel.subcommand("modules", self._cmd_modules, "列出模块和状态")
        kernel.subcommand("health", self._cmd_health, "输出内核健康报告")
        kernel.subcommand("config", self._cmd_config, "config <key>：读取配置")
        kernel.subcommand("request", self._cmd_request, "request <module> <action> [json]")
        kernel.subcommand("emit", self._cmd_emit, "emit <topic> [json]")
        kernel.subcommand("loglevel", self._cmd_loglevel, "loglevel <level>")
        self.register("py", self._cmd_py, "py <python code>")

    def _cmd_modules(self, ctx: CommandContext, args: list[str]) -> str:
        lines = []
        for module in self.kernel.modules():
            lines.append(
                f"{module.module_name():<16} {module.state.value:<10} {module.description}"
            )
        return "\n".join(lines)

    def _cmd_health(self, ctx: CommandContext, args: list[str]) -> str:
        return json.dumps(self.kernel.health(), ensure_ascii=False, indent=2, default=str)

    def _cmd_config(self, ctx: CommandContext, args: list[str]) -> str:
        if not args:
            return json.dumps(
                self.kernel.config.as_dict(), ensure_ascii=False, indent=2, default=str
            )
        return repr(self.kernel.config.get(args[0]))

    def _cmd_request(self, ctx: CommandContext, args: list[str]) -> Any:
        if len(args) < 2:
            raise ConsoleCommandError("用法: kernel request <module> <action> [json]")
        payload = json.loads(" ".join(args[2:])) if len(args) > 2 else {}
        return self.kernel.request(args[0], args[1], payload)

    def _cmd_emit(self, ctx: CommandContext, args: list[str]) -> Any:
        if not args:
            raise ConsoleCommandError("用法: kernel emit <topic> [json]")
        payload = json.loads(" ".join(args[1:])) if len(args) > 1 else {}
        return self.kernel.emit(args[0], payload, source=self.name)

    def _cmd_loglevel(self, ctx: CommandContext, args: list[str]) -> str:
        if not args:
            raise ConsoleCommandError("用法: kernel loglevel <DEBUG|INFO|WARNING|ERROR|CRITICAL>")
        level = args[0].upper()
        logger = logging.getLogger()
        logger.setLevel(getattr(logging, level, None) or logging.INFO)
        return f"level={logging.getLevelName(logger.level)}"

    def _cmd_py(self, ctx: CommandContext, args: list[str]) -> Any:
        code = " ".join(args).strip()
        if not code:
            raise ConsoleCommandError("用法: py <python code>")
        try:
            return eval(code, self._py_namespace)
        except SyntaxError:
            exec(code, self._py_namespace)
            return None

    # ------------------------------------------------------------------ #
    # REPL
    # ------------------------------------------------------------------ #
    def _loop(self) -> None:
        prompt = (
            self.kernel.config.get("console.prompt", "console> ")
            if self.kernel.console.interactive
            else ""
        )
        while not self._stop.is_set():
            try:
                line = self.kernel.console.read_line(prompt)
            except (EOFError, ValueError, OSError):
                break
            if line.strip().lower() in ("quit", "exit"):
                break
            try:
                result = self.execute(line)
            except ConsoleCommandError as exc:
                self.kernel.console.writeln_err(str(exc))
                continue
            except Exception as exc:  # noqa: BLE001 - REPL 不能因为单条指令退出
                self.log.exception("控制台指令解析失败 line=%r", line)
                self.kernel.console.writeln_err(f"指令失败: {exc}")
                continue
            if result:
                self.kernel.console.write(result)
