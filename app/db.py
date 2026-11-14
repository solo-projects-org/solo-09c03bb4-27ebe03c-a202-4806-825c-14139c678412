"""SQLite 映射库。

四张表:
- patients       : 原始患者号 -> 稳定代号(全局唯一映射,跨批共享,失败批不删除)
- uid_map        : (批次, 原始UID) -> 新UID(批次独占,失败/中断批整体清除)
- batches        : 批次状态机;cleanup_* 三列记录清理进度,与处理结局相互独立
- cleanup_targets: 待清理依据(删除清单)。每条残件一行,删除成功才标记 DONE;
                   删除失败保留 PENDING/FAILED,由后台补清线程持续重试,
                   中途退出也不丢失。路径即白名单,补清绝不会碰清单外的在途文件。

单连接 + 全局锁 + WAL,保证并发批与重启后的一致性。
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from typing import Iterator

from .config import DB_PATH

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS patients (
    original_patient_id TEXT PRIMARY KEY,
    codename            TEXT NOT NULL,
    created_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS uid_map (
    batch_id      TEXT NOT NULL,
    original_uid  TEXT NOT NULL,
    new_uid       TEXT NOT NULL,
    uid_type      TEXT NOT NULL,           -- STUDY / SERIES / INSTANCE
    created_at    TEXT NOT NULL,
    PRIMARY KEY (batch_id, original_uid)
);
CREATE INDEX IF NOT EXISTS idx_uid_map_batch ON uid_map(batch_id);

CREATE TABLE IF NOT EXISTS batches (
    batch_id          TEXT PRIMARY KEY,
    status            TEXT NOT NULL,       -- PENDING/PROCESSING/COMPLETED/REJECTED/FAILED
    original_filename TEXT,
    file_count        INTEGER,
    error             TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    completed_at      TEXT,
    cleanup_status    TEXT NOT NULL DEFAULT 'NOT_REQUIRED',
                                               -- NOT_REQUIRED/PENDING/IN_PROGRESS/DONE/FAILED
    cleanup_total     INTEGER NOT NULL DEFAULT 0,
    cleanup_remaining INTEGER NOT NULL DEFAULT 0,
    cleanup_error     TEXT,
    cleaned_at        TEXT
);

CREATE TABLE IF NOT EXISTS cleanup_targets (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id   TEXT NOT NULL,
    path       TEXT NOT NULL,               -- 待删残件的绝对路径(白名单)
    kind       TEXT NOT NULL,               -- FILE / DIR
    status     TEXT NOT NULL DEFAULT 'PENDING',  -- PENDING/IN_PROGRESS/DONE/FAILED
    attempts   INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (batch_id, path)
);
CREATE INDEX IF NOT EXISTS idx_cleanup_status ON cleanup_targets(status);
CREATE INDEX IF NOT EXISTS idx_cleanup_batch ON cleanup_targets(batch_id);
"""

# 旧数据卷(无 cleanup_* 列)在线迁移
_COLUMN_MIGRATIONS = {
    "cleanup_status": "ALTER TABLE batches ADD COLUMN cleanup_status TEXT NOT NULL DEFAULT 'NOT_REQUIRED'",
    "cleanup_total": "ALTER TABLE batches ADD COLUMN cleanup_total INTEGER NOT NULL DEFAULT 0",
    "cleanup_remaining": "ALTER TABLE batches ADD COLUMN cleanup_remaining INTEGER NOT NULL DEFAULT 0",
    "cleanup_error": "ALTER TABLE batches ADD COLUMN cleanup_error TEXT",
    "cleaned_at": "ALTER TABLE batches ADD COLUMN cleaned_at TEXT",
}


def init() -> None:
    global _conn
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    _conn = sqlite3.connect(str(DB_PATH), check_same_thread=False, isolation_level=None)
    _conn.row_factory = sqlite3.Row
    with _lock:
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA busy_timeout=10000")
        _conn.execute("PRAGMA synchronous=NORMAL")
        _conn.executescript(SCHEMA)
        existing = {r["name"] for r in _conn.execute("PRAGMA table_info(batches)")}
        for col, ddl in _COLUMN_MIGRATIONS.items():
            if col not in existing:
                _conn.execute(ddl)


def close() -> None:
    """关闭连接(主要供进程内重启测试使用)。"""
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
            _conn = None


def conn() -> sqlite3.Connection:
    assert _conn is not None, "db.init() 未调用"
    return _conn


@contextmanager
def tx() -> Iterator[sqlite3.Connection]:
    """短事务:同一时刻只有一个写事务,配合全局锁保证并发批互不干扰。"""
    with _lock:
        c = conn()
        c.execute("BEGIN IMMEDIATE")
        try:
            yield c
            c.execute("COMMIT")
        except BaseException:
            c.execute("ROLLBACK")
            raise


def query(sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    with _lock:
        return conn().execute(sql, params).fetchall()


def query_one(sql: str, params: tuple = ()) -> sqlite3.Row | None:
    with _lock:
        return conn().execute(sql, params).fetchone()
