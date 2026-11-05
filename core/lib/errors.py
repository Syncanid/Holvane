"""框架异常层级。

所有异常都继承 :class:`FrameworkError`，调用方既可以精确捕获，也可以一把兜住。
"""

from __future__ import annotations


class FrameworkError(Exception):
    """框架异常基类。"""


# --------------------------------------------------------------------------- #
# 模块 / 生命周期
# --------------------------------------------------------------------------- #
class ModuleError(FrameworkError):
    """模块相关错误的基类。"""


class ModuleContractError(ModuleError):
    """模块没有满足框架契约（例如声明了不存在的事件处理器）。"""


class DuplicateModule(ModuleError):
    def __init__(self, name: str) -> None:
        super().__init__(f"模块 {name!r} 已注册；如需替换请传入 replace=True")
        self.name = name


class ModuleNotFound(ModuleError):
    def __init__(self, name: str) -> None:
        super().__init__(f"模块 {name!r} 未注册")
        self.name = name


class ModuleUnavailable(ModuleError):
    """模块已注册，但当前被禁用。"""

    def __init__(self, name: str) -> None:
        super().__init__(f"模块 {name!r} 已被禁用，无法使用")
        self.name = name


class MissingDependency(ModuleError):
    def __init__(self, module: str, dependency: str, reason: str) -> None:
        super().__init__(f"模块 {module!r} 依赖 {dependency!r}，但后者{reason}")
        self.module = module
        self.dependency = dependency
        self.reason = reason


class LifecycleError(ModuleError):
    """在错误的生命周期阶段执行了操作。"""


class UnknownAction(ModuleError):
    """请求了一个模块没有暴露的动作。"""

    def __init__(self, module: str, action: str) -> None:
        super().__init__(f"模块 {module!r} 没有动作 {action!r}（需要 handle_{action} 方法）")
        self.module = module
        self.action = action
