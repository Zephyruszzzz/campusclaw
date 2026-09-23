"""服务端渲染页面。上传目录不挂载为静态目录。"""

from __future__ import annotations

from flask import Blueprint, redirect, render_template, url_for

from campusclaw import materials as svc
from campusclaw import request_context as rc
from campusclaw.errors import raise_api

bp = Blueprint("pages", __name__, url_prefix="")


@bp.get("/")
def index():
    return redirect(url_for("pages.materials_page"))


@bp.get("/materials")
def materials_page():
    user = rc.require_page_auth()
    conn = rc.get_conn()
    items = svc.list_materials(conn, int(user["class_id"]))
    token = rc.ensure_csrf_token()
    return render_template(
        "materials.html",
        user=user,
        items=items,
        csrf_token=token,
    )


# 上传目录不对外提供静态访问；显式 404，避免误以为存在公开路径。
@bp.get("/uploads/<path:storage_key>")
def uploads_static_denied(storage_key: str):  # noqa: ARG001
    raise_api("NOT_FOUND", 404)


@bp.get("/static/uploads/<path:storage_key>")
def static_uploads_denied(storage_key: str):  # noqa: ARG001
    raise_api("NOT_FOUND", 404)
