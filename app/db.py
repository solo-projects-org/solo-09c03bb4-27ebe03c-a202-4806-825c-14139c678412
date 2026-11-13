"""SQLite 映射库。

三张表:
- patients: 原始患者号 -> 稳定代号(全局唯一映射,跨批共享,失败批不删除)
- uid_map : (批次, 原始UID) -> 新UID(批次独占,失败批整体清除)
- batches : 批次状态机

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
    completed_at      TEXT
);
"""


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
