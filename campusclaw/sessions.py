"""服务端会话：sid 仅以 SHA-256 摘要入库，可撤销、可过期。"""

from __future__ import annotations

import sqlite3
from datetime import datetime

from campusclaw import db as dbmod, security
from campusclaw.timeutil import now_iso, now_utc, parse_iso, plus_hours, to_iso

USER_COLUMNS = (
    "SELECT s.id AS session_pk, s.expires_at, s.revoked_at, "
    "s.created_at AS session_created_at, c.name AS class_name, "
    "u.id AS user_id, u.username, u.role, u.class_id "
    "FROM auth_sessions s "
    "JOIN users u ON u.id = s.user_id "
    "JOIN classes c ON c.id = u.class_id "
    "WHERE s.sid_hash = ?"
)


def create_session(
    conn: sqlite3.Connection, user_id: int, moment: datetime, hours: int
) -> str:
    sid = security.new_session_id()
    created = to_iso(moment)
    expires = to_iso(plus_hours(moment, hours))
    conn.execute(
        "INSERT INTO auth_sessions(sid_hash, user_id, created_at, expires_at, revoked_at) "
        "VALUES (?, ?, ?, ?, NULL)",
        (security.session_id_hash(sid), user_id, created, expires),
    )
    return sid


def revoke_session(conn: sqlite3.Connection, sid: str | None) -> bool:
    """撤销当前 sid 对应的会话记录；其他独立会话不受影响。"""
    if not sid:
        return False
    cursor = conn.execute(
        "UPDATE auth_sessions SET revoked_at = ? WHERE sid_hash = ? AND revoked_at IS NULL",
        (now_iso(), security.session_id_hash(sid)),
    )
    return cursor.rowcount > 0


def load_session_user(
    conn: sqlite3.Connection, sid: str | None, moment: datetime | None = None
):
    """验证签名 Cookie 中的 sid 是否在服务端仍有效，并返回用户上下文。"""
    if not sid:
        return None
    row = dbmod.query_one(conn, USER_COLUMNS, (security.session_id_hash(sid),))
    if row is None:
        return None
    if row["revoked_at"] is not None:
        return None
    moment = moment or now_utc()
    if parse_iso(row["expires_at"]) <= moment:
        return None
    return row
