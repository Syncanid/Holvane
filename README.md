# Holvane

Python 应用基座：一个内核负责模块的编排、隔离与自愈。

- `core/`：内核（模块注册、依赖、生命周期、事件、请求），外加平台层 `compat`、
  平台无关的 `utils`（时间、文本、超时、环境上下文）和 `lib`（配置、事件、注册表、
  错误、控制台、日志）。**不 import 任何模块**。
- `modules/`：所有模块，每个模块一个文件夹。console 默认编入；database 默认不编入；
  业务模块也放这里，和基础模块没有区别。
- `main.py`：只负责启动内核并保持进程存活。

零第三方依赖，Python 3.10+。

## 目录结构

```
.
├── main.py                    启动内核
├── core/
│   ├── kernel.py              注册 / 依赖 / 生命周期 / 事件 / 请求 / 环境
│   ├── module.py              模块基类
│   ├── compat.py              平台层：路径 / 编码 / 控制台流 / 原子写
│   ├── utils/                 时间 / 文本 / 超时 / 环境上下文
│   └── lib/                   配置 / 事件 / 注册表 / 错误 / 控制台 / 日志
├── modules/
│   ├── console/               交互控制台：指令注册与执行
│   └── database/              默认不编入构建
└── data/                      运行期数据（git 忽略）
```

## 启动

```bash
python main.py        # 启动内核，Ctrl+C 退出
```

`main.py` 只做三件事：`import modules`（触发自注册）、`Kernel(...).boot()`、等待退出
信号后 `shutdown()`。需要常驻的服务由各模块在 `start()` 里自己启动。

## 新增模块

1. 新建 `modules/bookmarks/__init__.py`：

```python
import json
from pathlib import Path

from core import Module
from core.compat import atomic_write_text, read_text
from core.lib.registry import register
from core.utils.time import now_ms


@register
class Bookmarks(Module):
    name = "bookmarks"
    description = "收藏夹"

    def setup(self) -> None:
        self._path = Path("data/bookmarks.json")

    def start(self) -> None:
        self.log.info("bookmarks ready")

    def handle_add(self, url: str) -> dict:
        items = json.loads(read_text(self._path)) if self._path.exists() else []
        items.append({"url": url, "ts": now_ms()})
        atomic_write_text(self._path, json.dumps(items, ensure_ascii=False, indent=2))
        return {"count": len(items)}
```

2. 在 `modules/__init__.py` 里 import 一行：`from .bookmarks import Bookmarks`。

没有第三步。启动时 `Kernel()` 从注册表实例化所有模块。

## 内核

| 需求 | API |
| --- | --- |
| 注册模块 | `@register` 装饰器（导入即注册）、`kernel.register(...)`、`kernel.register_all([...])` |
| 判断存在 | `has()`（已注册、启用且未失败）、`is_registered()`、`is_enabled()`、`get()`、`require()` |
| 调用模块 | `kernel.request("name", "action", {...})` → `handle_<action>`，可传 `timeout=` |
| 发事件 | `kernel.emit("topic", key=value)` |
| 收事件 | `kernel.on("topic", handler, priority=0, once=False)` |
| 超时/恢复 | `Kernel(module_timeout=..., request_timeout=..., recovery_attempts=..., health_interval=...)`、`kernel.check_health()` |
| 错误表 | `kernel.errors`（模块注册上来的异常类型）、`kernel.load_errors` |
| 环境 | `Kernel(env={...})`、`kernel.env`、`core.current_env()` |
| 配置 | `kernel.config`、`core.Config` |
| 生命周期 | `boot()` / `shutdown()` / `with Kernel() as k:` |

启动顺序：先按依赖拓扑排序，同层按 `priority`（数值小者先）。**允许循环依赖**：
检测到环时，环内模块用随机顺序启动；环外的依赖关系仍然保持。

启用/禁用：注册即启用；只有显式 `Kernel(disable=[...])` 或
`register(..., enabled=False)` 才会关。没有类级默认开关。没编入构建的模块不在
注册表里，运行时无法打开。

### 超时

`Kernel(module_timeout=30.0)` 给 `setup/start/stop/health/repair` 每个操作加超时；
`Kernel(request_timeout=...)` 给所有 `kernel.request` 加超时，也可以单次
`kernel.request(..., timeout=...)`。超时后内核不再等待，把模块标记为 `FAILED`。

Python 线程杀不掉，超时的操作线程可能还在跑；框架只能保证主流程不被卡死。

### 故障隔离

- 启动时一个模块 `setup/start` 失败或超时：只把该模块标记 `FAILED`，继续启动其它模块；
- 硬依赖失败/未运行的模块会被跳过（`phase="dependency"`），不会拖垮无关模块；
- 运行期只有模块自己 `report_error()` 才会进入恢复流程；内核不根据异常猜故障；
- `health_interval > 0` 时的周期探测只记 warning 日志，**不改变状态、不停模块、不触发恢复**；
- `has()` / `require()` 对 `FAILED` 模块返回不存在/不可用；依赖它的模块之后失败是预期行为；
- `kernel.load_errors` 记录实例化失败的模块。

### 自愈

只有模块自己调用 `self.report_error(error)` 才会触发恢复。启动失败、`health()` 返回
`ok=False`、事件处理器异常都不会自动触发修复。

模块可以：

- 覆盖 `repair()` 做非破坏性修复，返回 `True` 表示已恢复；不得删除数据或改变外部状态；
- 声明 `restartable = True`，允许内核按 `stop -> setup -> start` 重启它。

`Kernel(recovery_attempts=3)` 控制尝试次数：每轮先 `repair()`，不行再重启；全部失败后
模块标记 `FAILED`，`has()` / `require()` 随即视为不可用。恢复过程发
`module.recovering` / `module.recovered` / `module.failed(phase="recovery")`。

```python
class Worker(Module):
    name = "worker"
    restartable = True

    def repair(self) -> bool:
        return self._reconnect()   # 非破坏性，不删数据

    def run(self) -> None:
        try:
            ...
        except Exception as exc:
            self.report_error(exc)
```

`Module.health()` 仍可用于显式诊断（`kernel.check_health()`），但它不触发恢复。
`health_interval > 0` 时后台周期探测只把异常写成 warning 日志，不保存状态。

### 环境上下文

`Kernel(env={...})` 会先把传入值与 `os.environ` 合并（同名以传入值为准），
在 `boot()` 时把快照注入 `ContextVar`；模块用 `kernel.env` 或 `core.current_env()`
读取。不修改进程环境，`shutdown()` 后恢复到上一层。

### 配置

配置在 core 里，不再是模块。`Config` 提供注册、读取、写入：

```python
config = kernel.config
config.register("app.name", "demo")
config.register("http.port", 8080, cast=int)

config.get("http.port")             # 文件 > env > 默认值
config.set("app.name", "myapp")     # 写回文件，作为覆盖
```

- `register(key, default, cast=...)` 注册配置项和默认值；
- 优先级：配置文件 > 环境变量 > 默认值；
- `get()` 每次检查文件 mtime，读到的总是最新值；
- `set()` 原子写回 JSON 文件，并立即生效；
- env 映射：`APP_` + 大写 + `.` 转 `__`，如 `APP_HTTP__PORT`；
- 没有文件路径的 `Config()` 只读 env 和默认值（日志就是这样）；
- 配置文件路径由 env 传入：`APP_CONFIG__PATH`，`Config()` 自己读它；`main.py`
  把它指向 `config/default.json`，文件不存在时按空配置运行，第一次 `set()` 才创建；
- 需要显式注入时仍可 `Kernel(config=Config(path=...))`。

## 事件契约

内核不认识任何具体主题：模块在 `events` 里声明自己关心什么，内核负责挂载、停止时卸载。

```python
from core import EventSpec, Module


class Cache(Module):
    name = "cache"
    events = (
        EventSpec("module.started", "on_module_started"),
        ("demo.changed", "on_demo_changed"),
    )

    def on_module_started(self, event) -> None:
        ...
```

声明了不存在的处理方法会在类定义时抛 `ModuleContractError`。主题支持 `demo.*`、
`module.**`、`*`；处理器异常被隔离并计入 `kernel.event_errors`。

内核只发自己的生命周期事件：`kernel.started/stopping/stopped/failed`、
`module.registered/ready/started/stopped/failed`。业务事件由模块自己定义、自己发布。

## 错误注册

每个模块声明自己的异常，注册时由内核登记：

```python
from core.lib.errors import FrameworkError


class ConfigError(FrameworkError):
    ...


class Config(Module):
    name = "config"
    errors = (ConfigError,)
```

`kernel.errors` 返回 `{"config.ConfigError": ConfigError, ...}`。core 不定义、不 import
任何模块的错误类型。

## 日志

日志是 core 组件，基于标准库 `logging`；**模块不直接 print，所有输出统一用 `self.log`**：

```python
self.log.info("started port=%s", 8080)
self.log.exception("failed")
```

默认级别 `info`，控制台 + 文件同时输出。控制台格式：

```
06:54:50 - INFO - demo:12 - started port=8080
```

文件默认写 `data/logs/app_YYYY-MM-DD.log`，跨天自动切文件，格式：

```
2026-11-05 06:54:50,849 - INFO - demo:12 - started port=8080
```

env 覆盖：

```bash
APP_LOG__LEVEL=debug
APP_LOG__DIR=data/logs     # 默认 data/logs
APP_LOG__FILE=app          # 文件名前缀，最终为 <prefix>_YYYY-MM-DD.log
APP_LOG__CONSOLE=true
APP_LOG__COLOR=always    # always / auto / never
```

```python
from core import setup_logging

setup_logging()            # 只读 env 和默认值
```

控制台写入走 `core.lib.console`，`set_console(Console(...))` 会同时切换日志出口。
进程退出前可调用 `shutdown_logging()` 关闭文件句柄。
`httpx`、`chromadb`、`aiosqlite`、`apscheduler`、`watchdog` 会被压到 `WARNING`。

装配时会打印：

```
日志已装配 level=DEBUG console=on file=.../app_yyyy-mm-dd.log
```

控制台在 TTY 下自动启用 ANSI 颜色（Windows 会尝试开启 VT 处理）；重定向或
`APP_LOG__COLOR`：`always`（默认，强制彩色）、`auto`（仅 TTY 且未设置
`NO_COLOR`）、`never`（关闭）。

## 时间

```python
from core.utils.time import now_ms, to_utc, to_local, format_ms

now_ms()                    # 1793827527454
to_utc(ms)                  # aware datetime
to_local(ms)                # 本地 aware datetime
format_ms(to_local(ms))     # 只做 strftime，不关心时区
```

存储、传输、比较一律用整数毫秒；字符串时间只用于显示。

## 跨平台兼容层

`core.compat` 统一路径与文本 I/O：

- `as_path(value)`：统一成 `Path`，展开 `~`；
- `read_text(path)`：UTF-8（识别 BOM），失败回退系统编码，再失败替换字符；
- `write_text` / `append_text`：内部固定 UTF-8 + LF；
- `atomic_write_text(path, text)`：临时文件 + `fsync` + `os.replace`；
- `safe_write(stream, text)` / `console_write(text)`：按目标流编码自动降级。

## 基础模块

### console

交互控制台模块，分组、树形 help、上下文输出。

```python
console = kernel.require("console")
console.register("bookmarks", handler, "列出收藏")          # 单条指令
group = console.register("bookmarks", description="收藏夹")  # 指令组
group.subcommand("add", add_handler, "添加收藏")
console.execute("bookmarks add https://example.com")
```

处理器支持两种签名：

```python
def handler(ctx, args: list[str]):       # ctx.reply(...), return 值
def handler(args: list[str], output_pipe):  # output_pipe.reply(...)
```

返回值 `dict` 转 JSON、`str` 原样、`bool` 忽略、其它转 `str`；异常写入输出，不打断控制台。

内置指令：

```
help
kernel modules|health|config|request|emit|loglevel
py <code>
```

`help` 输出树形结构；`help kernel` 查看组内指令。

REPL 只在交互终端启动（`APP_CONSOLE__REPL=true`，默认开）；非交互环境直接调
`console.execute(line)`。提示符可用 `APP_CONSOLE__PROMPT` 覆盖。

### database

默认不编入构建：`modules/__init__.py` 里那行 import 是注释掉的，所以它的代码不会
被导入、不会注册、不会出现在模块列表里。

启用是源码级改动，取消这一行的注释即可：

```python
# modules/__init__.py
from .database import Database
```

没编入构建时，任何运行时方式都打不开它——它不在注册表里。

内置 sqlite 后端，连接串 `sqlite://:memory:`、`sqlite:///绝对路径.db`、`sqlite://相对路径.db`；
其它数据库通过 `register_backend("postgres", Factory)` 接入。事务支持嵌套 SAVEPOINT：

```python
db = kernel.require("database")
db.execute("CREATE TABLE item (id INTEGER PRIMARY KEY, name TEXT)")
db.execute("INSERT INTO item (name) VALUES (?)", ("apple",))
db.fetch_all("SELECT * FROM item")        # [{"id": 1, "name": "apple"}]
with db.transaction():
    db.execute("INSERT INTO item (name) VALUES (?)", ("banana",))
```

日志里用 `db.safe_url`（自动隐去密码）。

## 数据落盘约定

框架不再提供 storage 模块。需要持久化的模块自行处理：

- 相对路径放 `data/`，目录由模块自己创建；
- 文本/JSON 用 `core.compat.atomic_write_text` / `read_text`；
- JSONL 追加用 `core.compat.append_text`；
- 关系型数据用可选的 `database`。

落盘统一 UTF-8 + LF；时间戳用毫秒 Unix 时间，字符串只用于显示。

## 边界

- 事件同步分发，不引入 asyncio；处理器里不要做重活。
- 模块只能在 `boot()` 前注册/启停；运行期动态增删未实现。
- 依赖允许成环，环内随机顺序启动；环内模块不要在 `setup()` 里假设对方已就绪。
- 不做数据库 schema 自动迁移；表结构由业务模块在 `setup()` 里用
  `CREATE TABLE IF NOT EXISTS` 或自己的版本表维护。
- `main.py` 只启动内核，不提供命令行；需要运维命令时自己加模块动作或入口。
- 数据落盘由模块自己负责，core 只提供原子写和文本读取工具。
