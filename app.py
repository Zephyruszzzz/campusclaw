"""WSGI / gunicorn 入口。

严格模式（``CAMPUSCLAW_STRICT_CONFIG=1``）下 ``SECRET_KEY`` 缺失、数据库不完整
或上传残留无法清理时，进程必须非零退出且不监听端口。

默认（开箱即用）模式下，未设置的环境变量使用内置测试凭据，启动时打印账号清单。
"""

from __future__ import annotations

import os
import sys

from campusclaw import StartupError, create_app
from campusclaw.config import SEED_ACCOUNTS, ConfigError

ROLE_LABEL = {"teacher": "教师", "student": "学生"}
MASKED_PASSWORD = "********"


def build():
    try:
        return create_app()
    except (ConfigError, StartupError) as exc:
        print(f"[campusclaw] 启动失败：{exc}", file=sys.stderr)
        sys.exit(2)


app = build()


def print_startup_banner() -> None:
    """打印可登录账号，省去每次翻文档。"""
    settings = app.extensions["campusclaw"]["settings"]
    host = os.environ.get("HOST", "0.0.0.0")
    port = os.environ.get("PORT", "8080")
    shown = "127.0.0.1" if host in ("0.0.0.0", "::") else host

    lines = [
        "",
        "=" * 64,
        "  CampusClaw 已启动",
        f"  登录页    http://{shown}:{port}/login",
        f"  健康检查  http://{shown}:{port}/health",
        f"  数据目录  {settings.data_dir}",
        f"  上传目录  {settings.upload_dir}",
        "-" * 64,
        "  预置账号（用户名 / 口令）",
    ]
    for account in SEED_ACCOUNTS:
        role = ROLE_LABEL.get(account.role, account.role)
        # 仅回显内置演示口令；来自环境变量的口令属于服务端密钥，一律不落日志。
        if account.username in settings.dev_defaults:
            password = settings.seed_passwords.get(account.username, "<未设置>")
        else:
            password = MASKED_PASSWORD
        lines.append(
            f"    {account.username:<12}{password:<15}{role} · {account.class_name} 班"
        )
    if settings.using_dev_defaults():
        lines.append("-" * 64)
        lines.append("  以上为内置测试凭据，仅供本机演示，请勿用于线上环境。")
        lines.append("  如需强制从环境变量读取：设置 CAMPUSCLAW_STRICT_CONFIG=1")
    lines.append("=" * 64)
    lines.append("")
    print("\n".join(lines), flush=True)


if __name__ == "__main__":
    from werkzeug.serving import run_simple

    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8080"))
    print_startup_banner()
    run_simple(host, port, app, use_reloader=False)
