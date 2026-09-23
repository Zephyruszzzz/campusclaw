"""私有上传目录的文件操作、删除清理任务与启动恢复。

上传目录不对外暴露静态路由；所有读写都经由本模块。
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from campusclaw import db as dbmod
from campusclaw.timeutil import now_iso


class StorageRecoveryError(Exception):
    """无引用残留在清理时被阻止，进程不得进入可用状态。"""


@dataclass
class RecoveryResult:
    removed: list[str] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.blocked


def ensure_directories(upload_dir: Path, staging_dir: Path) -> None:
    upload_dir.mkdir(parents=True, exist_ok=True)
    staging_dir.mkdir(parents=True, exist_ok=True)


def new_storage_key(extension: str) -> str:
    return f"{uuid.uuid4().hex}.{extension.lower()}"


def staging_path(staging_dir: Path, name: str) -> Path:
    return staging_dir / name


def material_file_path(upload_dir: Path, class_id: int, storage_key: str) -> Path:
    return upload_dir / str(class_id) / storage_key


def write_staging(data: bytes, staging_dir: Path, name: str) -> Path:
    """写入暂存文件并返回路径。失败时抛出存储异常。"""
    staging_dir.mkdir(parents=True, exist_ok=True)
    target = staging_path(staging_dir, name)
    with open(target, "wb") as handle:
        handle.write(data)
    return target


def move_to_final(src: Path, upload_dir: Path, class_id: int, storage_key: str) -> Path:
    """同卷移动暂存文件到最终私有位置。"""
    target = material_file_path(upload_dir, class_id, storage_key)
    target.parent.mkdir(parents=True, exist_ok=True)
    os.replace(src, target)
    return target


def read_bytes(path: Path) -> bytes:
    with open(path, "rb") as handle:
        return handle.read()


def try_delete_file(path: Path) -> tuple[bool, str | None]:
    """删除文件；不存在视为完成。返回 ``(是否成功, 错误类型)``。"""
    try:
        if path.exists():
            os.remove(path)
        return True, None
    except OSError as exc:          # 权限不足、被占用等均可重试
        return False, type(exc).__name__


def referenced_storage_keys(conn) -> set[str]:
    """被材料或清理任务引用的存储键；其余文件均视为无引用残留。"""
    keys = {
        row["storage_key"]
        for row in dbmod.query_all(conn, "SELECT storage_key FROM materials")
    }
    keys |= {
        row["storage_key"]
        for row in dbmod.query_all(conn, "SELECT storage_key FROM file_cleanup_jobs")
    }
    return keys


def _iter_final_files(upload_dir: Path) -> Iterable[Path]:
    if not upload_dir.exists():
        return []
    for child in sorted(upload_dir.iterdir()):
        if child.name == ".staging" or not child.is_dir():
            continue
        for target in sorted(child.iterdir()):
            if target.is_file():
                yield target


def recover_storage(conn, upload_dir: Path, staging_dir: Path) -> RecoveryResult:
    """接收业务流量前清理暂存与无引用文件，保留已提交材料引用的文件。

    清理被阻止时不虚报成功，返回 blocked 由调用方决定是否退出。
    """
    result = RecoveryResult()
    ensure_directories(upload_dir, staging_dir)

    if staging_dir.exists():
        for target in sorted(staging_dir.iterdir()):
            if not target.is_file():
                continue
            ok, error = try_delete_file(target)
            if ok:
                result.removed.append(str(target.relative_to(upload_dir)))
            else:
                result.blocked.append(f"{target.relative_to(upload_dir)}（{error}）")

    known = referenced_storage_keys(conn)
    for target in _iter_final_files(upload_dir):
        if target.name in known:
            continue
        ok, error = try_delete_file(target)
        if ok:
            result.removed.append(str(target.relative_to(upload_dir)))
        else:
            result.blocked.append(f"{target.relative_to(upload_dir)}（{error}）")

    return result


def attempt_removal(upload_dir: Path, class_id: int, storage_key: str) -> tuple[bool, str | None]:
    return try_delete_file(material_file_path(upload_dir, class_id, storage_key))


def process_cleanup_jobs(conn, upload_dir: Path) -> tuple[list[int], list[int]]:
    """推进全部 pending 清理任务。

    返回 ``(已完成的任务 material_id 列表, 仍失败的任务列表)``。
    仅处理指向已删除材料的任务，活跃材料文件不会被误删。
    """
    rows = dbmod.query_all(
        conn, "SELECT * FROM file_cleanup_jobs WHERE status = 'pending' ORDER BY id"
    )
    succeeded: list[int] = []
    failed: list[int] = []

    for row in rows:
        material = dbmod.query_one(
            conn,
            "SELECT id, class_id, status FROM materials WHERE id = ?",
            (row["material_id"],),
        )
        if material is None or material["status"] != "deleted":
            # 任务与材料状态不一致，保守保留任务供人工核对。
            failed.append(row["material_id"])
            continue

        removed, error = attempt_removal(upload_dir, material["class_id"], row["storage_key"])
        stamp = now_iso()
        if removed:
            conn.execute(
                "UPDATE file_cleanup_jobs SET status = 'done', attempts = attempts + 1, "
                "last_error = NULL, updated_at = ? WHERE id = ?",
                (stamp, row["id"]),
            )
            succeeded.append(row["material_id"])
        else:
            conn.execute(
                "UPDATE file_cleanup_jobs SET attempts = attempts + 1, last_error = ?, "
                "updated_at = ? WHERE id = ?",
                (error or "UNKNOWN", stamp, row["id"]),
            )
            failed.append(row["material_id"])

    return succeeded, failed


def cleanup_once(conn, upload_dir: Path) -> tuple[list[int], list[int]]:
    """单次清理推进：供启动流程与 `scripts/cleanup_files.py --once` 共用。"""
    return process_cleanup_jobs(conn, upload_dir)
