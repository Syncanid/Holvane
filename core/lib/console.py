"""控制台输入输出标准化。

平台差异（编码、stdin/stdout 的可替换性）交给 :mod:`core.compat`；本模块只提供
统一的读写与交互语义：``write`` / ``writeln`` / ``write_err`` / ``read_line`` /
``ask`` / ``confirm`` / ``choose``。需要接入 GUI / socket 时，构造一个覆盖
``write`` 与 ``read_line`` 的 ``Console`` 子类，再交给 :func:`set_console`。
"""

from __future__ import annotations

import sys
from typing import Sequence, TextIO

from ..compat import safe_write, supports_color


class Console:
    """标准输入输出封装；streams 可注入，便于嵌入或测试。"""

    def __init__(
        self,
        stdin: TextIO | None = None,
        stdout: TextIO | None = None,
        stderr: TextIO | None = None,
    ) -> None:
        self._stdin = stdin if stdin is not None else sys.stdin
        self._stdout = stdout if stdout is not None else sys.stdout
        self._stderr = stderr if stderr is not None else sys.stderr

    @property
    def interactive(self) -> bool:
        try:
            return bool(self._stdin.isatty() and self._stdout.isatty())
        except (AttributeError, ValueError):
            return False

    @property
    def supports_color(self) -> bool:
        return supports_color(self._stdout)

    # ------------------------------------------------------------------ #
    # 输出
    # ------------------------------------------------------------------ #
    def write(self, text: str = "", *, err: bool = False) -> None:
        safe_write(self._stderr if err else self._stdout, text)

    def writeln(self, text: str = "") -> None:
        self.write(text + "\n")

    def write_err(self, text: str = "") -> None:
        self.write(text, err=True)

    def writeln_err(self, text: str = "") -> None:
        self.write_err(text + "\n")

    def flush(self) -> None:
        for stream in (self._stdout, self._stderr):
            try:
                stream.flush()
            except (AttributeError, ValueError):
                pass

    # ------------------------------------------------------------------ #
    # 输入
    # ------------------------------------------------------------------ #
    def read_line(self, prompt: str = "") -> str:
        if prompt:
            self.write(prompt)
            self.flush()
        line = self._stdin.readline()
        if not line:
            raise EOFError("标准输入已关闭")
        return line.rstrip("\r\n")

    def ask(self, prompt: str, default: str | None = None, *, required: bool = False) -> str:
        suffix = f" [{default}]" if default is not None else ""
        while True:
            answer = self.read_line(f"{prompt}{suffix}: ").strip()
            if answer:
                return answer
            if default is not None:
                return default
            if not required:
                return ""
            self.writeln_err("该项为必填")

    def confirm(self, prompt: str, default: bool = False) -> bool:
        suffix = "[Y/n]" if default else "[y/N]"
        while True:
            answer = self.read_line(f"{prompt} {suffix}: ").strip().lower()
            if not answer:
                return default
            if answer in ("y", "yes", "是", "1", "true"):
                return True
            if answer in ("n", "no", "否", "0", "false"):
                return False
            self.writeln_err("请输入 y 或 n")

    def choose(self, prompt: str, options: Sequence[str], default: str | None = None) -> str:
        choices = [str(option) for option in options]
        if not choices:
            raise ValueError("options 不能为空")
        self.writeln("  ".join(f"[{index + 1}] {choice}" for index, choice in enumerate(choices)))
        while True:
            answer = self.read_line(f"{prompt}: ").strip()
            if not answer and default is not None:
                return default
            if answer in choices:
                return answer
            if answer.isdigit() and 1 <= int(answer) <= len(choices):
                return choices[int(answer) - 1]
            self.writeln_err(f"请输入 1-{len(choices)} 或选项原文")


_console: Console = Console()


def get_console() -> Console:
    return _console


def set_console(console: Console) -> None:
    """替换全局控制台；日志输出也会跟着切换。"""
    if not isinstance(console, Console):
        raise TypeError(f"{console!r} 不是 Console")
    global _console
    _console = console
