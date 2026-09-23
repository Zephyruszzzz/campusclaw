"""SQLite 连接、事务与最小表层基础。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence


class SchemaError(Exception):
    """数据库结构不符合预期，不得被静默当作空库覆盖。"""


SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS classes (
        id   INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS users (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        username      TEXT NOT NULL UNIQUE,
        password_hash TEXT NOT NULL,
        role          TEXT NOT NULL CHECK (role IN ('teacher', 'student')),
        class_id      INTEGER NOT NULL REFERENCES classes(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS materials (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        class_id          INTEGER NOT NULL REFERENCES classes(id),
        uploaded_by       INTEGER NOT NULL REFERENCES users(id),
        title             TEXT NOT NULL,
        original_filename TEXT NOT NULL,
        storage_key       TEXT NOT NULL UNIQUE,
        size_bytes        INTEGER NOT NULL,
        status            TEXT NOT NULL CHECK (status IN ('active', 'deleted')),
        created_at        TEXT NOT NULL,
        updated_at        TEXT NOT NULL,
        deleted_at        TEXT,
        UNIQUE (id, class_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS knowledge_entries (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        material_id INTEGER NOT NULL UNIQUE,
        class_id    INTEGER NOT NULL,
        body_text   TEXT NOT NULL,
        created_at  TEXT NOT NULL,
        FOREIGN KEY (material_id, class_id) REFERENCES materials(id, class_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS assignments (
        id       INTEGER PRIMARY KEY AUTOINCREMENT,
        class_id INTEGER NOT NULL REFERENCES classes(id),
        title    TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS assistants (
        id       INTEGER PRIMARY KEY AUTOINCREMENT,
        class_id INTEGER NOT NULL REFERENCES classes(id),
        name     TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS skills (
        id   INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS auth_sessions (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        sid_hash   TEXT NOT NULL UNIQUE,
        user_id    INTEGER NOT NULL REFERENCES users(id),
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        revoked_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS file_cleanup_jobs (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        material_id INTEGER NOT NULL UNIQUE REFERENCES materials(id),
        storage_key TEXT NOT NULL,
        status      TEXT NOT NULL CHECK (status IN ('pending', 'done')),
        attempts    INTEGER NOT NULL DEFAULT 0,
        last_error  TEXT,
        updated_at  TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_materials_class_status "
    "ON materials(class_id, status, created_at DESC, id DESC)",
)

#: 业务与健康检查依赖的必需表；缺表不得被当作空库覆盖。
REQUIRED_TABLES: tuple[str, ...] = (
    "classes",
    "users",
    "materials",
    "knowledge_entries",
    "assignments",
    "assistants",
    "skills",
    "auth_sessions",
    "file_cleanup_jobs",
)

ENTITY_ROLE_TABLES = {
    "班级": "classes",
    "用户": "users",
    "讲义": "materials",
    "作业": "assignments",
    "助手": "assistants",
    "技能": "skills",
}


def connect(db_path: Path | str) -> sqlite3.Connection:
    """建立连接并强制启用外键约束。"""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def existing_tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    ).fetchall()
    return {row["name"] for row in rows}


def missing_tables(conn: sqlite3.Connection) -> list[str]:
    tables = existing_tables(conn)
    return [name for name in REQUIRED_TABLES if name not in tables]


def apply_schema(conn: sqlite3.Connection) -> None:
    with transaction(conn):
        for statement in SCHEMA_STATEMENTS:
            conn.execute(statement)


def table_exists(db_path: Path | str) -> bool:
    path = Path(db_path)
    return path.exists() and path.stat().st_size > 0


def _safe_rollback(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("ROLLBACK")
    except sqlite3.Error:
        pass


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """显式 BEGIN/COMMIT/ROLLBACK，便于注入提交故障并保持可回滚语义。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        _safe_rollback(conn)
        raise
    try:
        conn.execute("COMMIT")
    except BaseException:
        _safe_rollback(conn)
        raise


def query_one(conn: sqlite3.Connection, sql: str, params: Sequence[Any] = ()):
    return conn.execute(sql, params).fetchone()


def query_all(conn: sqlite3.Connection, sql: str, params: Sequence[Any] = ()):
    return conn.execute(sql, params).fetchall()


def health_probe(conn: sqlite3.Connection) -> bool:
    """只读探测必需表是否可查询，不写入任何数据。"""
    try:
        missing = missing_tables(conn)
        if missing:
            return False
        conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type = 'table'").fetchone()
        for name in REQUIRED_TABLES:
            conn.execute(f"SELECT 1 FROM {name} LIMIT 1").fetchone()
        return True
    except sqlite3.Error:
        return False
