"""跨平台兼容层。

统一路径与文本 I/O：

* 路径一律走 :class:`pathlib.Path`，不拼字符串、不手写分隔符；
* 内部统一 UTF-8 + LF；
* 读外部文件时自动识别 BOM，UTF-8 失败时回退系统默认编码，再失败用替换字符；
* 写文件时用 UTF-8、LF，并提供原子写（临时文件 + ``os.replace`` + ``fsync``）；
* 控制台/管道输出按目标流的编码自动降级，不让一个字符把进程打崩。
"""

from __future__ import annotations

import locale
import os
import tempfile
from pathlib import Path
from typing import IO, Any


def as_path(value: Any) -> Path:
    """把任意路径值统一成 ``Path``，展开 ``~``。"""
    return Path(os.fspath(value)).expanduser()


def ensure_dir(path: Any) -> Path:
    resolved = as_path(path)
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def system_encoding() -> str:
    return locale.getpreferredencoding(False) or "utf-8"


def ensure_utf8_streams() -> None:
    """尽量把 stdout/stderr 切成 UTF-8；被重定向/替换过的流失败就算了。"""
    import sys

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass


def enable_ansi() -> None:
    """Windows 上尝试开启 VT 处理；其它平台无需操作。"""
    if os.name == "nt":
        _enable_windows_ansi()


def supports_color(stream: Any) -> bool:
    """流支持 ANSI 颜色时返回 True；Windows 上会尝试开启 VT 处理。"""
    if "NO_COLOR" in os.environ:
        return False
    try:
        if not stream.isatty():
            return False
    except (AttributeError, ValueError):
        return False
    if os.name == "nt":
        _enable_windows_ansi()
    return True


def _enable_windows_ansi() -> None:
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
    except (AttributeError, ImportError):
        return
    for std_handle in (-11, -12):  # stdout / stderr
        handle = kernel32.GetStdHandle(std_handle)
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)


def read_text(path: Any, *, encoding: str = "utf-8") -> str:
    """读文本：优先 UTF-8（识别 BOM），失败回退系统编码。"""
    raw = as_path(path).read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw[3:].decode("utf-8", errors="replace")
    for candidate in (encoding, system_encoding()):
        try:
            return raw.decode(candidate)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode(encoding, errors="replace")


def write_text(path: Any, text: str, *, encoding: str = "utf-8") -> Path:
    """写文本：UTF-8 + LF；父目录不存在时创建。"""
    resolved = as_path(path)
    ensure_dir(resolved.parent)
    with open(resolved, "w", encoding=encoding, newline="\n") as handle:
        handle.write(text)
    return resolved


def append_text(path: Any, text: str, *, encoding: str = "utf-8") -> Path:
    """追加文本并 fsync。"""
    resolved = as_path(path)
    ensure_dir(resolved.parent)
    with open(resolved, "a", encoding=encoding, newline="\n") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    return resolved


def atomic_write_text(path: Any, text: str, *, encoding: str = "utf-8") -> Path:
    """原子写：同目录临时文件 -> fsync -> ``os.replace``。"""
    resolved = as_path(path)
    ensure_dir(resolved.parent)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{resolved.name}.", suffix=".tmp", dir=str(resolved.parent)
    )
    try:
        with os.fdopen(descriptor, "w", encoding=encoding, newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, resolved)
        _fsync_dir(resolved.parent)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return resolved


def _fsync_dir(path: Path) -> None:
    try:
        descriptor = os.open(str(path), os.O_RDONLY)
    except OSError:  # Windows 不支持打开目录
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def safe_write(stream: IO[str], text: str) -> None:
    """按目标流编码写文本，编码失败时反斜杠转义。"""
    try:
        stream.write(text)
        return
    except UnicodeEncodeError:
        pass
    encoding = getattr(stream, "encoding", None) or system_encoding()
    stream.write(text.encode(encoding, "backslashreplace").decode(encoding))


def console_write(text: str, *, err: bool = False) -> None:
    """写控制台；编码按当前平台自动适配。"""
    import sys

    stream = sys.stderr if err else sys.stdout
    safe_write(stream, text)
