"""SQLite 账号库：连接管理、建表与事务。

为什么用 SQLite 而不是 MySQL：
    本项目的账号量级是「自托管工具站的若干用户」，SQLite 的单文件、
    零配置、零外部进程特性正好匹配；且它属于标准库，不破坏本项目
    「核心依赖尽量少」的一贯取向（对比：引入 MySQL 需要额外驱动 +
    连接池 + 备份脚本）。数据库文件位于 ``STORAGE_DIR/toolkit.db``，
    备份时直接拷贝该文件即可。

并发模型：
    应用是多线程的（任务线程池 + 请求线程池），因此按线程持有连接
    （``threading.local``），并开启 WAL 让读写互不阻塞。所有写操作走
    :func:`transaction` 显式事务，避免依赖隐式提交。
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from app.core.config import DB_PATH

logger = logging.getLogger(__name__)

_local = threading.local()

# 建表语句。全部使用 IF NOT EXISTS，便于反复启动与增量升级。
_SCHEMA: str = """
CREATE TABLE IF NOT EXISTS users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    email           TEXT    NOT NULL COLLATE NOCASE,
    phone           TEXT,
    display_name    TEXT    NOT NULL DEFAULT '',
    password_hash   TEXT    NOT NULL,
    role            TEXT    NOT NULL DEFAULT 'user',
    status          TEXT    NOT NULL DEFAULT 'active',
    email_verified  INTEGER NOT NULL DEFAULT 0,
    token_version   INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT    NOT NULL,
    last_login_at   TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email ON users(email COLLATE NOCASE);
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_phone ON users(phone) WHERE phone IS NOT NULL;

CREATE TABLE IF NOT EXISTS verify_codes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    target      TEXT    NOT NULL COLLATE NOCASE,
    channel     TEXT    NOT NULL,
    purpose     TEXT    NOT NULL,
    code_hash   TEXT    NOT NULL,
    expires_at  REAL    NOT NULL,
    attempts    INTEGER NOT NULL DEFAULT 0,
    consumed    INTEGER NOT NULL DEFAULT 0,
    ip          TEXT,
    created_at  REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_codes_lookup ON verify_codes(target, purpose, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_codes_ip ON verify_codes(ip, created_at DESC);
"""


def _configure(conn: sqlite3.Connection) -> None:
    """设置连接级 PRAGMA。

    Args:
        conn: 已建立的连接。
    """
    conn.row_factory = sqlite3.Row
    # WAL：读写并发不互斥，适合「请求线程读 + 后台写」的混合负载
    conn.execute("PRAGMA journal_mode=WAL")
    # 外键约束（当前表无外键，保留以便后续扩展）
    conn.execute("PRAGMA foreign_keys=ON")
    # 写锁等待上限：宁可短暂阻塞，也不要立刻抛 database is locked
    conn.execute("PRAGMA busy_timeout=10000")
    # 断电安全性 vs 速度的折中：WAL 下 NORMAL 已足够安全
    conn.execute("PRAGMA synchronous=NORMAL")


def get_connection(path: Path | None = None) -> sqlite3.Connection:
    """取当前线程的数据库连接（惰性建立并复用）。

    Args:
        path: 数据库路径；默认 ``STORAGE_DIR/toolkit.db``。

    Returns:
        已配置好 PRAGMA 的连接。
    """
    if path is not None:
        # 显式路径（测试用）不进入线程缓存，避免污染默认库
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(target, timeout=10, isolation_level=None, check_same_thread=False)
        _configure(conn)
        return conn

    conn = getattr(_local, "conn", None)
    if conn is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None, check_same_thread=False)
        _configure(conn)
        _local.conn = conn
    return conn


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    """显式事务上下文（BEGIN IMMEDIATE / COMMIT / ROLLBACK）。

    ``BEGIN IMMEDIATE`` 立刻获取写锁，避免「先读后写」场景下的
    ``SQLITE_BUSY_SNAPSHOT`` 升级失败。

    Yields:
        处于事务中的连接。

    Raises:
        BaseException: 块内异常会回滚后原样抛出。
    """
    conn = get_connection()
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def init_db(path: Path | None = None) -> None:
    """建表（幂等）。

    Args:
        path: 数据库路径；默认使用配置中的 DB_PATH。
    """
    conn = get_connection(path)
    conn.executescript(_SCHEMA)
    logger.info("账号库已就绪：%s", path or DB_PATH)


def close_connection() -> None:
    """关闭当前线程的连接（测试与优雅关闭时使用）。"""
    conn = getattr(_local, "conn", None)
    if conn is not None:
        conn.close()
        _local.conn = None


__all__ = ["close_connection", "get_connection", "init_db", "transaction"]
