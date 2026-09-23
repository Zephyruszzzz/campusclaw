"""CampusClaw 运行时配置。

所有安全相关参数仅来自服务端环境变量；缺失或空白时启动失败，不提供内置默认值。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from campusclaw.security import (
    MAX_PASSWORD_BYTES,
    MIN_PASSWORD_BYTES,
    SeedAccount,
    validate_seed_password,
)

MAX_FILE_BYTES = 10 * 1024 * 1024            # 10 MiB，含上限
MAX_CONTENT_BYTES = 11 * 1024 * 1024         # HTTP multipart 请求总上限
SESSION_HOURS = 8                            # 登录后固定有效期
BCRYPT_ROUNDS = 12                           # 成本因子下限

ALLOWED_EXTENSIONS = ("txt", "md")

# ---------------------------------------------------------------------------
# 内置开发默认值（开箱即用）
#
# 不设置任何环境变量即可 `python app.py` 直接登录。这些值是公开的测试凭据，
# 仅用于本机演示。需要严格模式（缺失即启动失败）时设置：
#     CAMPUSCLAW_STRICT_CONFIG=1
# ---------------------------------------------------------------------------
DEFAULT_SECRET_KEY = "campusclaw-local-dev-secret-key-0123456789abcdef"

DEFAULT_SEED_PASSWORDS: dict[str, str] = {
    "teacher_a": "TeacherA#2026",
    "student_a1": "StudentA1#2026",
    "student_b1": "StudentB1#2026",
    "teacher_b": "TeacherB#2026",
}

STRICT_ENV = "CAMPUSCLAW_STRICT_CONFIG"


class ConfigError(Exception):
    """必需配置缺失或不合法。进程必须非零退出，不得进入可用状态。"""


def strict_enabled(env: dict[str, str] | None = None) -> bool:
    """是否启用严格模式：缺失密钥 / 种子口令时必须启动失败。"""
    source = os.environ if env is None else env
    return (source.get(STRICT_ENV) or "").strip().lower() in ("1", "true", "yes", "on")


def require_secret_key(env: dict[str, str] | None = None) -> str:
    """返回签名密钥。

    未设置时回落到内置开发密钥；严格模式下缺失即启动失败。
    """
    source = os.environ if env is None else env
    value = source.get("SECRET_KEY")
    if value is None or value.strip() == "":
        if strict_enabled(source):
            raise ConfigError(
                "缺少必需的环境变量 SECRET_KEY：请设置非空白的服务端密钥后重新启动。"
            )
        return DEFAULT_SECRET_KEY
    return value


SEED_ACCOUNTS: tuple[SeedAccount, ...] = (
    SeedAccount("teacher_a", "SEED_TEACHER_A_PASSWORD", "A", "teacher"),
    SeedAccount("student_a1", "SEED_STUDENT_A1_PASSWORD", "A", "student"),
    SeedAccount("student_b1", "SEED_STUDENT_B1_PASSWORD", "B", "student"),
    SeedAccount("teacher_b", "SEED_TEACHER_B_PASSWORD", "B", "teacher"),
)


@dataclass
class Settings:
    secret_key: str
    home: Path
    data_dir: Path
    upload_dir: Path
    staging_dir: Path
    db_path: Path
    cookie_secure: bool = False
    session_hours: int = SESSION_HOURS
    bcrypt_rounds: int = BCRYPT_ROUNDS
    max_file_bytes: int = MAX_FILE_BYTES
    max_content_bytes: int = MAX_CONTENT_BYTES
    seed_passwords: dict[str, str] = field(default_factory=dict)
    strict: bool = False
    dev_defaults: list[str] = field(default_factory=list)

    def using_dev_defaults(self) -> bool:
        return bool(self.dev_defaults)


def _bool_env(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def load_settings(env: dict[str, str] | None = None) -> Settings:
    """构建运行配置。``env`` 用于测试隔离，缺省读取进程环境变量。"""
    source = os.environ if env is None else env
    strict = strict_enabled(source)
    secret_key = require_secret_key(source)
    seed_passwords = collect_seed_passwords(source, required=False)

    dev_defaults: list[str] = []
    if secret_key == DEFAULT_SECRET_KEY and not (source.get("SECRET_KEY") or "").strip():
        dev_defaults.append("SECRET_KEY")
    for account in SEED_ACCOUNTS:
        if not (source.get(account.password_env) or "").strip():
            dev_defaults.append(account.username)

    home_raw = source.get("CAMPUSCLAW_HOME")
    home = Path(home_raw).resolve() if home_raw else Path.cwd().resolve()

    data_dir = Path(source.get("CAMPUSCLAW_DATA_DIR") or home / "data").resolve()
    upload_dir = Path(source.get("CAMPUSCLAW_UPLOAD_DIR") or home / "uploads").resolve()

    try:
        rounds = int(source.get("BCRYPT_ROUNDS") or BCRYPT_ROUNDS)
    except ValueError:
        rounds = BCRYPT_ROUNDS
    rounds = max(rounds, BCRYPT_ROUNDS)

    return Settings(
        secret_key=secret_key,
        home=home,
        data_dir=data_dir,
        upload_dir=upload_dir,
        staging_dir=upload_dir / ".staging",
        db_path=data_dir / "app.db",
        cookie_secure=_bool_env("AUTH_COOKIE_SECURE", False),
        session_hours=SESSION_HOURS,
        bcrypt_rounds=rounds,
        seed_passwords=seed_passwords,
        strict=strict,
        dev_defaults=dev_defaults,
    )


def collect_seed_passwords(
    env: dict[str, str] | None = None, required: bool = True
) -> dict[str, str]:
    """读取四个预置账号的初始口令。

    未设置的账号使用内置开发口令；显式设置但长度非法时一律拒绝初始化，
    避免部分写入或静默截断。``required=True`` + 严格模式时要求四个变量齐备。
    """
    source = os.environ if env is None else env
    strict = strict_enabled(source)
    collected: dict[str, str] = {}
    missing: list[str] = []
    invalid: list[str] = []

    for account in SEED_ACCOUNTS:
        raw = source.get(account.password_env)
        if raw is None or raw == "":
            if required and strict:
                missing.append(account.password_env)
                continue
            collected[account.username] = DEFAULT_SEED_PASSWORDS[account.username]
            continue
        if not validate_seed_password(raw):
            invalid.append(
                f"{account.password_env}（UTF-8 字节数须为 "
                f"{MIN_PASSWORD_BYTES}～{MAX_PASSWORD_BYTES}）"
            )
            continue
        collected[account.username] = raw

    if required:
        problems = []
        if missing:
            problems.append("缺少种子口令环境变量：" + "、".join(missing))
        if invalid:
            problems.append("种子口令长度不合法：" + "、".join(invalid))
        if problems:
            raise ConfigError("；".join(problems) + "。已中止初始化，未写入任何数据。")

    return collected
