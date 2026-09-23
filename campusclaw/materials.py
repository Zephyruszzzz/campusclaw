"""材料业务规则：列表、详情、上传事务、标题修改、删除与清理。

所有查询都带上服务端验证过的 class_id，客户端提供的身份字段不参与查询。
"""

from __future__ import annotations

import os
import sqlite3
import unicodedata
from typing import Any

from campusclaw import db as dbmod, storage
from campusclaw.config import ALLOWED_EXTENSIONS, Settings
from campusclaw.errors import MESSAGES, ApiError
from campusclaw.timeutil import now_iso

TITLE_MIN_CODEPOINTS = 1
TITLE_MAX_CODEPOINTS = 200

SUMMARY_COLUMNS = (
    "id, title, class_id, uploaded_by, original_filename, size_bytes, created_at, updated_at"
)
SUMMARY_ORDER = "created_at DESC, id DESC"

UPLOAD_ALLOWED_FIELDS = frozenset({"file", "title", "csrf_token"})
UPLOAD_FIELD_NAME = "file"
PATCH_ALLOWED_FIELDS = frozenset({"title"})


def err(code: str, status: int) -> ApiError:
    return ApiError(status, code, MESSAGES.get(code, code))


# --------------------------------------------------------------------------
# 读取
# --------------------------------------------------------------------------

SUMMARY_ORDER_SQL = f"ORDER BY {SUMMARY_ORDER}"


def list_materials(conn: sqlite3.Connection, class_id: int) -> list[dict[str, Any]]:
    """本班 active 材料；过滤在 SQL 中完成，不返回全集后由前端隐藏。"""
    rows = dbmod.query_all(
        conn,
        "SELECT {columns} FROM materials WHERE class_id = ? AND status = 'active' {order}".format(
            columns=SUMMARY_COLUMNS, order=SUMMARY_ORDER_SQL
        ),
        (class_id,),
    )
    return [dict(row) for row in rows]


def get_active_material(conn: sqlite3.Connection, material_id: int, class_id: int):
    """按 id + class + active 联合条件定位，跨班与不存在统一为未找到。"""
    return dbmod.query_one(
        conn,
        f"SELECT {SUMMARY_COLUMNS}, storage_key FROM materials "
        "WHERE id = ? AND class_id = ? AND status = 'active'",
        (material_id, class_id),
    )


def get_material_body(conn: sqlite3.Connection, material_id: int, class_id: int) -> str:
    row = dbmod.query_one(
        conn,
        "SELECT body_text FROM knowledge_entries WHERE material_id = ? AND class_id = ?",
        (material_id, class_id),
    )
    return row["body_text"] if row else ""


# --------------------------------------------------------------------------
# 输入校验
# --------------------------------------------------------------------------

def has_control_characters(value: str) -> bool:
    for char in value:
        if unicodedata.category(char) == "Cc":
            return True
    return False


def validate_title(value: str) -> str:
    """去首尾空白后校验 1～200 个 Unicode 码点且无控制字符。"""
    title = value.strip()
    if not (TITLE_MIN_CODEPOINTS <= len(title) <= TITLE_MAX_CODEPOINTS):
        raise err("INVALID_TITLE", 400)
    if has_control_characters(title):
        raise err("INVALID_TITLE", 400)
    return title


def sanitize_filename(filename: str) -> str:
    """仅保留 basename，去除目录段、NUL 与控制字符。"""
    candidate = (filename or "").replace("\\", "/")
    candidate = candidate.rsplit("/", 1)[-1]
    candidate = candidate.replace("\x00", "")
    candidate = "".join(char for char in candidate if char.isprintable())
    return candidate.strip()


def detect_extension(filename: str) -> str | None:
    lowered = filename.lower()
    if "." not in lowered:
        return None
    extension = lowered.rsplit(".", 1)[1]
    if extension in ALLOWED_EXTENSIONS:
        return extension
    return None


def main_filename(filename: str) -> str:
    if "." in filename:
        return filename.rsplit(".", 1)[0].strip()
    return filename.strip()


def decode_text(data: bytes) -> str:
    """严格 UTF-8 解码并去掉 BOM；失败、含 NUL 或纯空白均视为解析失败。"""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise err("PARSE_FAILED", 422) from exc
    if text.startswith("\ufeff"):
        text = text[1:]
    if "\x00" in text:
        raise err("PARSE_FAILED", 422)
    if text.strip() == "":
        raise err("PARSE_FAILED", 422)
    return text


# --------------------------------------------------------------------------
# 上传事务
# --------------------------------------------------------------------------

def upload_material(
    conn: sqlite3.Connection,
    settings: Settings,
    data: bytes,
    filename: str,
    explicit_title: str | None,
    *,
    teacher_id: int,
    class_id: int,
) -> int:
    """暂存 → 完整解析 → 双表同事务 → 同卷移动 → 提交 → 返回材料 id。"""
    safe_name = sanitize_filename(filename)
    if safe_name == "":
        raise err("INVALID_FILE", 400)

    if len(data) == 0:
        raise err("INVALID_FILE", 400)
    if len(data) > settings.max_file_bytes:
        raise err("FILE_TOO_LARGE", 413)

    extension = detect_extension(safe_name)
    if extension is None:
        raise err("UNSUPPORTED_FILE_TYPE", 415)

    text = decode_text(data)

    if explicit_title is not None:
        title = validate_title(explicit_title)
    else:
        derived = validate_title_or_none(main_filename(safe_name))
        if derived is None:
            raise err("INVALID_TITLE", 400)
        title = derived

    storage_key = storage.new_storage_key(extension)
    staging_name = f"{storage_key}.staging"
    staging_path = None
    final_path = None

    try:
        try:
            staging_path = storage.write_staging(data, settings.staging_dir, staging_name)
        except OSError as exc:
            raise err("STORAGE_ERROR", 500) from exc

        stamp = now_iso()
        conn.execute("BEGIN IMMEDIATE")
        try:
            cursor = conn.execute(
                "INSERT INTO materials(class_id, uploaded_by, title, original_filename, "
                "storage_key, size_bytes, status, created_at, updated_at, deleted_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, NULL)",
                (class_id, teacher_id, title, safe_name, storage_key, len(data),
                 stamp, stamp),
            )
            material_id = int(cursor.lastrowid)
            conn.execute(
                "INSERT INTO knowledge_entries(material_id, class_id, body_text, created_at) "
                "VALUES (?, ?, ?, ?)",
                (material_id, class_id, text, stamp),
            )
        except sqlite3.Error as exc:
            dbmod._safe_rollback(conn)
            raise err("DATABASE_ERROR", 500) from exc

        try:
            final_path = storage.move_to_final(
                staging_path, settings.upload_dir, class_id, storage_key
            )
            staging_path = None
        except OSError as exc:
            dbmod._safe_rollback(conn)
            raise err("STORAGE_ERROR", 500) from exc

        try:
            conn.execute("COMMIT")
        except sqlite3.Error as exc:
            dbmod._safe_rollback(conn)
            raise err("DATABASE_ERROR", 500) from exc

        return material_id
    except BaseException:
        if staging_path is not None:
            storage.try_delete_file(staging_path)
        if final_path is not None:
            storage.try_delete_file(final_path)
        raise


def validate_title_or_none(value: str) -> str | None:
    try:
        return validate_title(value)
    except ApiError:
        return None


# --------------------------------------------------------------------------
# 标题修改
# --------------------------------------------------------------------------

def rename_material(
    conn: sqlite3.Connection, material_id: int, class_id: int, title: str
) -> None:
    material = get_active_material(conn, material_id, class_id)
    if material is None:
        raise err("MATERIAL_NOT_FOUND", 404)
    new_title = validate_title(title)
    stamp = now_iso()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE materials SET title = ?, updated_at = ? "
            "WHERE id = ? AND class_id = ? AND status = 'active'",
            (new_title, stamp, material_id, class_id),
        )
        conn.execute("COMMIT")
    except sqlite3.Error as exc:
        dbmod._safe_rollback(conn)
        raise err("DATABASE_ERROR", 500) from exc


# --------------------------------------------------------------------------
# 删除与文件清理
# --------------------------------------------------------------------------

def mark_deleted(
    conn: sqlite3.Connection, material_id: int, class_id: int
) -> dict[str, Any]:
    """第一步：单事务内墓碑化材料、移除知识行并登记唯一清理任务。"""
    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.Error as exc:
        raise err("DATABASE_ERROR", 500) from exc

    material = dbmod.query_one(
        conn,
        "SELECT id, class_id, storage_key FROM materials "
        "WHERE id = ? AND class_id = ? AND status = 'active'",
        (material_id, class_id),
    )
    if material is None:
        dbmod._safe_rollback(conn)
        raise err("MATERIAL_NOT_FOUND", 404)

    stamp = now_iso()
    try:
        conn.execute(
            "UPDATE materials SET status = 'deleted', deleted_at = ?, updated_at = ? "
            "WHERE id = ? AND class_id = ? AND status = 'active'",
            (stamp, stamp, material_id, class_id),
        )
        conn.execute(
            "DELETE FROM knowledge_entries WHERE material_id = ? AND class_id = ?",
            (material_id, class_id),
        )
        conn.execute(
            "INSERT INTO file_cleanup_jobs(material_id, storage_key, status, attempts, "
            "last_error, updated_at) VALUES (?, ?, 'pending', 0, NULL, ?)",
            (material_id, material["storage_key"], stamp),
        )
        conn.execute("COMMIT")
    except sqlite3.Error as exc:
        dbmod._safe_rollback(conn)
        raise err("DATABASE_ERROR", 500) from exc

    return dict(material)


def finish_cleanup(
    conn: sqlite3.Connection, settings: Settings, material_id: int, class_id: int,
    storage_key: str,
) -> str:
    """第二步：提交后删除私有原文件。成功或文件已不存在均视为完成。"""
    path = storage.material_file_path(settings.upload_dir, class_id, storage_key)
    removed, error = storage.try_delete_file(path)
    stamp = now_iso()
    try:
        if removed:
            conn.execute(
                "UPDATE file_cleanup_jobs SET status = 'done', attempts = attempts + 1, "
                "last_error = NULL, updated_at = ? WHERE material_id = ?",
                (stamp, material_id),
            )
            return "done"
        conn.execute(
            "UPDATE file_cleanup_jobs SET attempts = attempts + 1, last_error = ?, "
            "updated_at = ? WHERE material_id = ?",
            (error or "UNKNOWN", stamp, material_id),
        )
        return "pending"
    except sqlite3.Error:
        return "pending"


def read_material_file(settings: Settings, class_id: int, storage_key: str) -> bytes:
    path = storage.material_file_path(settings.upload_dir, class_id, storage_key)
    if not os.path.exists(path):
        raise err("STORAGE_ERROR", 500)
    return storage.read_bytes(path)
