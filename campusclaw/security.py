"""密码哈希、会话管理与写请求 CSRF 令牌。"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass

import bcrypt

MIN_PASSWORD_BYTES = 8      # bcrypt 安全上下限，超过 72 字节不得静默截断
MAX_PASSWORD_BYTES = 72
BCRYPT_ROUNDS = 12          # 成本因子下限


@dataclass(frozen=True)
class SeedAccount:
    username: str
    password_env: str
    class_name: str
    role: str


def password_byte_length(password: str) -> int:
    return len(password.encode("utf-8"))


def validate_seed_password(password: str) -> bool:
    if not isinstance(password, str):
        return False
    length = password_byte_length(password)
    return MIN_PASSWORD_BYTES <= length <= MAX_PASSWORD_BYTES


def hash_password(password: str, rounds: int = BCRYPT_ROUNDS) -> str:
    """生成 bcrypt 哈希。超长口令在调用前已被拒绝，此处不截断。"""
    if rounds < BCRYPT_ROUNDS:
        rounds = BCRYPT_ROUNDS
    data = password.encode("utf-8")
    return bcrypt.hashpw(data, bcrypt.gensalt(rounds=rounds)).decode("ascii")


def verify_password(password: str, password_hash: str) -> bool:
    """校验口令。任何异常都按校验失败处理，不泄露细节。"""
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("ascii"))
    except (ValueError, TypeError, UnicodeEncodeError):
        return False


def parse_bcrypt_rounds(password_hash: str) -> int | None:
    """从 bcrypt 哈希字符串中解析成本因子，便于验收核对。"""
    parts = (password_hash or "").split("$")
    if len(parts) < 3:
        return None
    try:
        return int(parts[2])
    except ValueError:
        return None


def new_session_id() -> str:
    return secrets.token_urlsafe(32)


def session_id_hash(sid: str) -> str:
    """会话标识入库前做 SHA-256，库中不保存原始 sid。"""
    return hashlib.sha256(sid.encode("utf-8")).hexdigest()


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def tokens_equal(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    return secrets.compare_digest(a, b)
