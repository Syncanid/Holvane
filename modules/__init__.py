"""基础模块清单，也就是"编译清单"。

本文件 import 哪些模块，运行时就有哪些模块——这就是编译级开关。
``database`` 默认不在清单里：它的代码不会被导入、不会注册。要启用它，取消下面
那行 import 的注释（源码级改动），配置和环境变量都打不开一个没编入构建的模块。

每个模块是一个文件夹，至少含 ``__init__.py``；模块自己的私有代码可以继续拆成
同目录下的其它文件，不会污染 ``modules/`` 顶层。

业务模块同样放在本目录：新建 ``modules/xxx/__init__.py``，写一个带 ``@register``
的 ``Module`` 子类，然后在本文件 import 一行即可。
"""

from .console import ConsoleModule

# 编译级开关：取消注释即把 database 编入本次构建
# from .database import Database

__all__ = ["ConsoleModule"]
