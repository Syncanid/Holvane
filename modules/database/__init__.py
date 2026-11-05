"""数据库模块（默认不编入构建）。

启用方式是源码级改动：在 ``modules/__init__.py`` 里取消
``# from .database import Database`` 的注释。没编入构建时，本文件不会被导入，
不会注册，配置和环境变量也无法打开它。

内置 ``sqlite`` 后端（标准库，无需安装驱动），连接串：

* ``sqlite://:memory:``（默认，进程内临时库）
* ``sqlite:///绝对路径.db``
* ``sqlite://相对路径.db``

其它数据库通过 :func:`register_backend` 注册后端后即可使用，例如
``register_backend("postgres", PostgresBackend)``。
"""

from __future__ import annotations

import logging
import re
import sqlite3
import threading
from contextlib import contextmanager
from typing import Any, Callable, Iterator, Mapping, Protocol, Sequence

from core.lib.errors import FrameworkError
from core.lib.registry import register
from core.module import Module


class DatabaseError(FrameworkError):
    """数据库操作失败。"""


class BackendNotAvailable(DatabaseError):
    def __init__(self, scheme: str) -> None:
        super().__init__(f"没有可用的数据库后端 {scheme!r}；请先 register_backend() 注册")
        self.scheme = scheme


_PASSWORD_RE = re.compile(r"(://[^:/@]+:)[^@]*@")


class Backend(Protocol):
    """数据库后端契约。"""

    scheme: str

    def connect(self, url: str) -> Any:
        ...

    def close(self, connection: Any) -> None:
        ...


def _sqlite_path(url: str) -> str:
    remainder = url.split("://", 1)[1] if "://" in url else url
    if remainder in (":memory:", "", "/:memory:"):
        return ":memory:"
    if remainder.startswith("//"):
        return remainder[1:]
    # sqlite:///C:/data/app.db -> /C:/data/app.db
    if remainder.startswith("/") and len(remainder) > 2 and remainder[2] == ":":
        return remainder[1:]
    return remainder


class SQLiteBackend:
    scheme = "sqlite"

    def connect(self, url: str) -> sqlite3.Connection:
        path = _sqlite_path(url)
        try:
            connection = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        except sqlite3.Error as exc:
            raise DatabaseError(f"无法打开 SQLite 数据库 {path!r}: {exc}") from exc
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def close(self, connection: sqlite3.Connection) -> None:
        connection.close()


_BACKENDS: dict[str, Callable[[], Backend]] = {}
_BACKEND_LOCK = threading.Lock()


def register_backend(scheme: str, factory: Callable[[], Backend]) -> None:
    """注册数据库后端工厂。"""
    if not callable(factory):
        raise TypeError(f"后端工厂必须可调用，收到 {factory!r}")
    with _BACKEND_LOCK:
        _BACKENDS[scheme.lower()] = factory
    logging.getLogger("database").info("注册数据库后端 scheme=%s", scheme.lower())


def get_backend(scheme: str) -> Backend:
    with _BACKEND_LOCK:
        factory = _BACKENDS.get(scheme.lower())
    if factory is None:
        raise BackendNotAvailable(scheme)
    return factory()


register_backend("sqlite", SQLiteBackend)


def _rows(cursor: Any) -> list[dict[str, Any]]:
    columns = [description[0] for description in (cursor.description or ())]
    result: list[dict[str, Any]] = []
    for row in cursor.fetchall():
        if isinstance(row, sqlite3.Row):
            result.append(dict(row))
        elif isinstance(row, Mapping):
            result.append(dict(row))
        else:
            result.append(dict(zip(columns, row)))
    return result


@register
class Database(Module):
    name = "database"
    description = "数据库访问（默认不编入构建）：内置 sqlite 后端，可注册其它后端"
    errors = (DatabaseError, BackendNotAvailable)

    def setup(self) -> None:
        self._lock = threading.RLock()
        self._conn: Any = None
        self._tx_depth = 0
        self._url = "sqlite://:memory:"
        config = self.kernel.config
        config.register("database.url", "sqlite://:memory:")
        config.register("database.connect_on_start", False, cast=bool)
        self._url = str(config.get("database.url"))
        self._eager = config.get_bool("database.connect_on_start", False)
        scheme = self._url.split("://", 1)[0].lower() if "://" in self._url else "sqlite"
        self._scheme = scheme
        self._backend = get_backend(scheme)
        self.log.debug("数据库配置 scheme=%s url=%s", scheme, self.safe_url)

    def start(self) -> None:
        if self._eager:
            self.connect()
        self.log.info("数据库模块已启用 url=%s", self.safe_url)

    # ------------------------------------------------------------------ #
    # 连接
    # ------------------------------------------------------------------ #
    @property
    def url(self) -> str:
        return self._url

    @property
    def safe_url(self) -> str:
        """隐去密码后的连接串，可安全写日志。"""
        return _PASSWORD_RE.sub(r"\1***@", self._url)

    @property
    def connected(self) -> bool:
        return self._conn is not None

    def connect(self) -> Any:
        with self._lock:
            if self._conn is None:
                self._conn = self._backend.connect(self._url)
                self.log.info("数据库已连接 url=%s", self.safe_url)
            return self._conn

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._backend.close(self._conn)
                finally:
                    self._conn = None
                self.log.info("数据库已断开 url=%s", self.safe_url)

    def stop(self) -> None:
        self.close()

    # ------------------------------------------------------------------ #
    # 查询
    # ------------------------------------------------------------------ #
    def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        with self._lock:
            cursor = self._run(sql, params)
            if self._tx_depth == 0:
                self._conn.commit()
            self.log.debug("SQL 执行 statement=%s rows=%s", sql.split(None, 1)[0].upper(), cursor.rowcount)
            return cursor.rowcount

    def executemany(self, sql: str, rows: Sequence[Sequence[Any]]) -> int:
        with self._lock:
            connection = self.connect()
            try:
                cursor = connection.executemany(sql, [tuple(row) for row in rows])
            except Exception as exc:  # noqa: BLE001
                raise DatabaseError(f"SQL 执行失败: {exc}") from exc
            if self._tx_depth == 0:
                connection.commit()
            self.log.debug(
                "SQL 批量执行 statement=%s rows=%d", sql.split(None, 1)[0].upper(), len(rows)
            )
            return cursor.rowcount

    def fetch_all(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        with self._lock:
            rows = _rows(self._run(sql, params))
        self.log.debug("SQL 查询 statement=%s rows=%d", sql.split(None, 1)[0].upper(), len(rows))
        return rows

    def fetch_one(self, sql: str, params: Sequence[Any] = ()) -> dict[str, Any] | None:
        rows = self.fetch_all(sql, params)
        return rows[0] if rows else None

    def scalar(self, sql: str, params: Sequence[Any] = ()) -> Any:
        rows = self.fetch_all(sql, params)
        if not rows:
            return None
        return next(iter(rows[0].values()))

    def _run(self, sql: str, params: Sequence[Any]) -> Any:
        connection = self.connect()
        try:
            return connection.execute(sql, tuple(params))
        except Exception as exc:  # noqa: BLE001
            raise DatabaseError(f"SQL 执行失败: {exc}") from exc

    # ------------------------------------------------------------------ #
    # 事务
    # ------------------------------------------------------------------ #
    @contextmanager
    def transaction(self) -> Iterator[Any]:
        """事务上下文，支持嵌套（内层使用 SAVEPOINT）。"""
        with self._lock:
            connection = self.connect()
            depth = self._tx_depth
            savepoint = f"core_sp_{depth}"
            if depth == 0:
                connection.execute("BEGIN")
                self.log.debug("事务开始 depth=0")
            else:
                connection.execute(f"SAVEPOINT {savepoint}")
                self.log.debug("事务开始 depth=%d savepoint=%s", depth, savepoint)
            self._tx_depth += 1
            try:
                yield connection
            except BaseException:
                self._tx_depth -= 1
                if depth == 0:
                    connection.rollback()
                    self.log.warning("事务回滚 depth=0")
                else:
                    connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                    connection.execute(f"RELEASE SAVEPOINT {savepoint}")
                    self.log.warning("事务回滚 depth=%d savepoint=%s", depth, savepoint)
                raise
            else:
                self._tx_depth -= 1
                if depth == 0:
                    connection.commit()
                    self.log.debug("事务提交 depth=0")
                else:
                    connection.execute(f"RELEASE SAVEPOINT {savepoint}")
                    self.log.debug("事务释放 depth=%d savepoint=%s", depth, savepoint)

    def health(self) -> Mapping[str, Any]:
        return {
            "url": self.safe_url,
            "backend": self._scheme,
            "connected": self.connected,
            "in_transaction": self._tx_depth > 0,
        }
