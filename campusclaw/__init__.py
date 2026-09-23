"""CampusClaw 应用工厂。

浏览器 ←→ Flask App（认证、角色、班级、校验）←→ SQLite + 私有上传目录。
上传目录不作为静态目录对外暴露。
"""

from __future__ import annotations

import os
from typing import Any

from flask import Flask

from campusclaw import bootstrap, db as dbmod, storage
from campusclaw.config import ConfigError, Settings, load_settings
from campusclaw.errors import register_error_handlers
from campusclaw.request_context import close_conn, register_page_redirect

SESSION_COOKIE_NAME = "campusclaw_session"


class StartupError(Exception):
    """启动前置条件不满足：密钥缺失、数据库不完整或存储残留无法清理。"""


def prepare_storage_and_db(settings: Settings, env: dict[str, str] | None = None) -> None:
    """建库、幂等种子、启动恢复、待清理任务重试。全部在接收业务流量前完成。"""
    scenario = (env or {}).get("CAMPUSCLAW_TEST_SCENARIO", "") or os.environ.get(
        "CAMPUSCLAW_TEST_SCENARIO", ""
    )
    storage.ensure_directories(settings.upload_dir, settings.staging_dir)
    try:
        bootstrap.initialize(settings, env=env)
    except ConfigError:
        raise

    conn = dbmod.connect(settings.db_path)
    try:
        if scenario == "recovery_staging":
            staging = settings.staging_dir / "orphan.staging"
            staging.parent.mkdir(parents=True, exist_ok=True)
            staging.write_bytes(b"orphan staging bytes")
        if scenario == "recovery_orphan_final":
            class_row = conn.execute("SELECT id FROM classes ORDER BY id LIMIT 1").fetchone()
            if class_row is not None:
                target = settings.upload_dir / str(class_row["id"]) / "orphan-final.txt"
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"orphan final bytes")

        result = storage.recover_storage(conn, settings.upload_dir, settings.staging_dir)
        if not result.ok:
            raise StartupError(
                "无法清理无引用上传残留：" + "、".join(result.blocked)
                + "。请恢复上传目录的读写权限后重新启动。"
            )
        if scenario == "recovery_blocked":
            raise StartupError("注入：上传目录清理权限被阻止，禁止对外提供服务。")
        # 仍有 pending 任务不影响启动：对应资源已不可访问。
        storage.cleanup_once(conn, settings.upload_dir)
    finally:
        conn.close()


def create_app(
    settings: Settings | None = None,
    env: dict[str, str] | None = None,
    run_startup: bool = True,
) -> Flask:
    if settings is None:
        settings = load_settings(env)
    if run_startup:
        prepare_storage_and_db(settings, env=env)

    app = Flask(
        __name__,
        template_folder="templates",
        static_folder=None,  # 不提供任何公开静态目录
    )
    app.config.update(
        SECRET_KEY=settings.secret_key,
        SESSION_COOKIE_NAME=SESSION_COOKIE_NAME,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=settings.cookie_secure,
        SESSION_COOKIE_PATH="/",
        SESSION_REFRESH_EACH_REQUEST=False,
        MAX_CONTENT_LENGTH=settings.max_content_bytes,
        JSON_SORT_KEYS=False,
    )
    app.extensions["campusclaw"] = {"settings": settings}

    app.teardown_appcontext(close_conn)

    from campusclaw.views.auth_views import bp as auth_bp
    from campusclaw.views.health_views import bp as health_bp
    from campusclaw.views.material_views import bp as materials_bp
    from campusclaw.views.page_views import bp as pages_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(pages_bp)
    app.register_blueprint(materials_bp)
    app.register_blueprint(health_bp)

    register_error_handlers(app)
    # 覆盖 ApiError 处理器以支持受保护页面的 302 重定向。
    register_page_redirect(app)
    return app
