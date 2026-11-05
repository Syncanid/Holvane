"""应用入口：只负责启动内核。

业务逻辑不写在这里：每个模块在自己的 ``start()`` 里启动服务，内核负责编排。
本文件只做三件事：导入模块完成自注册、创建并启动内核、保持进程存活到退出。
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Mapping

import modules  # noqa: F401  # 导入即完成所有模块自注册
from core import Kernel

ROOT = Path(__file__).resolve().parent


def build_kernel(*, env: Mapping[str, str] | None = None) -> Kernel:
    """创建内核（不启动）；配置路径和运行时覆盖都通过 env 传入。"""
    return Kernel(env=env)


def main() -> int:
    kernel = build_kernel(
        env={
            "APP_LOG__LEVEL": "debug",
            "APP_CONFIG__PATH": str(ROOT / "config" / "default.json"),
        }
    )
    kernel.boot()
    logging.getLogger("main").info("应用就绪")
    try:
        while True:
            threading.Event().wait(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        kernel.shutdown()
        logging.getLogger("main").info("应用退出")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
