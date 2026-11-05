"""指令树：参考 Afedium 的 command 系统。

- 指令可以分组，``register("kernel")`` 返回节点，继续 ``subcommand(...)``；
- 处理器支持两种签名：``handler(ctx, args)`` 或 ``handler(args=args, output_pipe=ctx)``；
- 返回值 dict 自动转 JSON，str 原样，bool 忽略，其它转 str；
- 处理器异常被捕获并写入输出，不打断控制台。
"""

from __future__ import annotations

import inspect
import json
import logging
import traceback
from typing import Any, Callable, Iterable

_log = logging.getLogger("console.command")

Handler = Callable[..., Any]


class CommandContext:
    def __init__(self, client_id: Any = None) -> None:
        self.client_id = client_id
        self.outputs: list[str] = []

    def reply(self, text: Any) -> None:
        self.outputs.append(str(text))

    def write(self, text: Any) -> None:
        self.outputs.append(str(text))

    def get_result(self) -> str:
        return "".join(self.outputs)


class CommandNode:
    def __init__(self, name: str, description: str = "") -> None:
        self.name = name
        self.description = description
        self.handler: Handler | None = None
        self.subcommands: dict[str, "CommandNode"] = {}

    def subcommand(
        self,
        path: str,
        handler: Handler | None = None,
        description: str = "",
    ) -> "CommandNode":
        """注册子指令并返回叶子节点，支持 ``cmd.subcommand("install", ...)``。"""
        parts = str(path).strip().split()
        current = self
        for part in parts:
            if part not in current.subcommands:
                current.subcommands[part] = CommandNode(part)
            current = current.subcommands[part]
        if handler is not None:
            current.handler = handler
        if description:
            current.description = description
        return current

    def get_help(self, prefix: str = "", is_last: bool = True, is_root: bool = False) -> list[str]:
        lines: list[str] = []
        if not is_root:
            connector = "└── " if is_last else "├── "
            desc = ""
            if self.description:
                desc = f" - {self.description}"
            elif self.subcommands:
                desc = " - [指令组]"
            lines.append(f"{prefix}{connector}{self.name}{desc}")
            child_prefix = prefix + ("    " if is_last else "│   ")
        else:
            child_prefix = prefix
        for index, name in enumerate(sorted(self.subcommands)):
            lines.extend(
                self.subcommands[name].get_help(
                    child_prefix, index == len(self.subcommands) - 1, is_root=False
                )
            )
        return lines

    def execute(self, ctx: CommandContext, args: list[str]) -> str:
        if args and args[0] in self.subcommands:
            return self.subcommands[args[0]].execute(ctx, args[1:])

        if self.handler is not None:
            try:
                self._invoke(self.handler, ctx, args)
            except Exception as exc:  # noqa: BLE001 - 控制台必须继续可用
                ctx.reply(f"执行异常:\n{traceback.format_exc()}")
                _log.error("指令 %s 执行异常: %s", self.name, exc)
            return ctx.get_result()

        help_text = "\n".join(self.get_help())
        ctx.reply(f"这是一个指令组，包含以下子指令:\n{help_text}")
        return ctx.get_result()

    @staticmethod
    def _invoke(handler: Handler, ctx: CommandContext, args: list[str]) -> None:
        result: Any
        try:
            signature = inspect.signature(handler)
            parameters = signature.parameters
        except (TypeError, ValueError):
            parameters = {}
        if "output_pipe" in parameters:
            result = handler(args=args, output_pipe=ctx)
        elif len(parameters) >= 2:
            result = handler(ctx, args)
        else:
            result = handler(args)
        if result is None or isinstance(result, bool):
            return
        if isinstance(result, dict):
            ctx.reply(json.dumps(result, ensure_ascii=False, indent=2))
        elif isinstance(result, str):
            ctx.reply(result)
        else:
            ctx.reply(str(result))


class CommandRoot(CommandNode):
    def __init__(self) -> None:
        super().__init__("")

    def register(self, path: str, handler: Handler | None = None, description: str = "") -> CommandNode:
        return self.subcommand(path, handler, description)

    def unregister(self, path: str) -> str:
        parts = str(path).strip().split()
        if not parts:
            return "路径为空"
        current: CommandNode = self
        for part in parts[:-1]:
            if part not in current.subcommands:
                return "命令不存在"
            current = current.subcommands[part]
        last = parts[-1]
        if last in current.subcommands:
            del current.subcommands[last]
            return f"已注销: {' '.join(parts)}"
        return "命令不存在"

    def dispatch(self, args: Iterable[str], client_id: Any = None) -> str:
        if isinstance(args, str):
            args = args.split()
        arguments = [str(item) for item in args]
        if not arguments:
            return "错误: 空指令\n"
        name = arguments[0]
        ctx = CommandContext(client_id=client_id)
        if name == "help":
            if len(arguments) > 1:
                node = self._find(arguments[1:])
                if node is None:
                    return f"未知指令: {' '.join(arguments[1:])}\n"
                return "\n".join(node.get_help()) + "\n"
            return "--- 可用指令列表 ---\n" + "\n".join(self.get_help(is_root=True)) + "\n"
        node = self.subcommands.get(name)
        if node is None:
            return "未知的指令。输入 'help' 获取可用指令列表。\n"
        result = node.execute(ctx, arguments[1:])
        if result and not result.endswith("\n"):
            result += "\n"
        return result

    def _find(self, parts: Iterable[str]) -> CommandNode | None:
        node: CommandNode = self
        for part in parts:
            child = node.subcommands.get(part)
            if child is None:
                return None
            node = child
        return node
