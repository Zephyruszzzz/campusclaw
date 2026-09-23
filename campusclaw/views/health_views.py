"""匿名健康检查：不依赖登录，只做只读探测。"""

from __future__ import annotations

from flask import Blueprint, jsonify

from campusclaw import db as dbmod
from campusclaw import request_context as rc

bp = Blueprint("health", __name__)


@bp.get("/health")
def health():
    try:
        conn = rc.get_conn()
        ok = dbmod.health_probe(conn)
    except Exception:
        ok = False
    status = 200 if ok else 503
    return jsonify({"status": "ok" if ok else "unavailable"}), status
