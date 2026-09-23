"""空库初始化与幂等种子数据。

规则：
* 已有库缺必需表 → 启动失败并提示，不静默当作空库覆盖；
* 空库 → 先在 一次性事务 内建立结构与种子，任何前置校验失败都不写入；
* 已播种 → 重复执行不新增、不重置口令哈希、不复活已删除材料。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from campusclaw import db as dbmod
from campusclaw import security, storage
from campusclaw.config import SEED_ACCOUNTS, ConfigError, Settings, collect_seed_passwords
from campusclaw.timeutil import now_iso

SAMPLES: tuple[tuple[str, str, str, str], ...] = (
    (
        "A",
        "A 班示例讲义",
        "a_sample.txt",
        "A 班示例讲义\n\n本材料是 CampusClaw 初始化样本，正文包含标记 A_ONLY_SEED。\n"
        "本班教师与学生可以读取；其他班级不可读取。\n",
    ),
    (
        "B",
        "B 班示例讲义",
        "b_sample.txt",
        "B 班示例讲义\n\n本材料是 CampusClaw 初始化样本，正文包含标记 B_ONLY_SEED。\n"
        "本班教师与学生可以读取；其他班级不可读取。\n",
    ),
)


@dataclass
class SeedReport:
    created_classes: list[str]
    created_users: list[str]
    created_samples: list[str]
    skipped: bool


def is_seeded(conn: sqlite3.Connection) -> bool:
    count = dbmod.query_one(conn, "SELECT COUNT(*) AS n FROM users")["n"]
    return count > 0


def prepare_database(settings: Settings, conn: sqlite3.Connection) -> None:
    tables = dbmod.existing_tables(conn)
    if tables:
        missing = dbmod.missing_tables(conn)
        if missing:
            raise ConfigError(
                "数据库缺少必需的表：" + "、".join(missing)
                + "。请修复或使用空数据目录重新初始化，不会静默覆盖现有数据。"
            )
    if not tables or "materials" not in tables:
        dbmod.apply_schema(conn)


def seed_database(
    conn: sqlite3.Connection, settings: Settings, env: dict[str, str] | None = None
) -> SeedReport:
    """在一个事务内写入班级、账号、样本材料、知识全文与原文件。"""
    passwords = collect_seed_passwords(env, required=True)
    storage.ensure_directories(settings.upload_dir, settings.staging_dir)

    written_files: list[Any] = []
    created_classes: list[str] = []
    created_users: list[str] = []
    created_samples: list[str] = []

    try:
        with dbmod.transaction(conn):
            class_ids: dict[str, int] = {}
            for name in ("A", "B"):
                existing = dbmod.query_one(
                    conn, "SELECT id FROM classes WHERE name = ?", (name,)
                )
                if existing is None:
                    cursor = conn.execute("INSERT INTO classes(name) VALUES (?)", (name,))
                    class_ids[name] = int(cursor.lastrowid)
                    created_classes.append(name)
                else:
                    class_ids[name] = int(existing["id"])

            user_ids: dict[str, int] = {}
            for account in SEED_ACCOUNTS:
                password = passwords[account.username]
                existing = dbmod.query_one(
                    conn, "SELECT id, password_hash FROM users WHERE username = ?",
                    (account.username,),
                )
                if existing is not None:
                    user_ids[account.username] = int(existing["id"])
                    continue
                password_hash = security.hash_password(password, settings.bcrypt_rounds)
                cursor = conn.execute(
                    "INSERT INTO users(username, password_hash, role, class_id) "
                    "VALUES (?, ?, ?, ?)",
                    (account.username, password_hash, account.role,
                     class_ids[account.class_name]),
                )
                user_ids[account.username] = int(cursor.lastrowid)
                created_users.append(account.username)

            for class_name, title, filename, body in SAMPLES:
                class_id = class_ids[class_name]
                exists = dbmod.query_one(
                    conn,
                    "SELECT id FROM materials WHERE class_id = ? AND title = ? "
                    "AND original_filename = ?",
                    (class_id, title, filename),
                )
                if exists is not None:
                    continue

                data = body.encode("utf-8")
                extension = filename.rsplit(".", 1)[1].lower()
                storage_key = storage.new_storage_key(extension)
                path = storage.material_file_path(
                    settings.upload_dir, class_id, storage_key
                )
                path.parent.mkdir(parents=True, exist_ok=True)
                with open(path, "wb") as handle:
                    handle.write(data)
                written_files.append(path)

                stamp = now_iso()
                uploader = user_ids[
                    "teacher_a" if class_name == "A" else "teacher_b"
                ]
                cursor = conn.execute(
                    "INSERT INTO materials(class_id, uploaded_by, title, original_filename, "
                    "storage_key, size_bytes, status, created_at, updated_at, deleted_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, NULL)",
                    (class_id, uploader, title, filename, storage_key, len(data),
                     stamp, stamp),
                )
                material_id = int(cursor.lastrowid)
                conn.execute(
                    "INSERT INTO knowledge_entries(material_id, class_id, body_text, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (material_id, class_id, body, stamp),
                )
                created_samples.append(title)

        return SeedReport(created_classes, created_users, created_samples, skipped=False)
    except BaseException:
        # 事务未提交：移除本次写入的样本文件，不留下部分初始化结果。
        for path in written_files:
            storage.try_delete_file(path)
        raise


def initialize(
    settings: Settings, env: dict[str, str] | None = None
) -> SeedReport | None:
    """幂等初始化入口。返回 None 表示本次未播种（已有数据）。"""
    if not dbmod.table_exists(settings.db_path):
        # 空库：先验证全部种子口令，失败则完全不触碰数据目录，不留下部分结果。
        collect_seed_passwords(env, required=True)

    conn = dbmod.connect(settings.db_path)
    try:
        prepare_database(settings, conn)
        if is_seeded(conn):
            return None
        return seed_database(conn, settings, env=env)
    finally:
        conn.close()
