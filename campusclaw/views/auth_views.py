"""登录/退出视图：服务端会话的创建与撤销。"""

from __future__ import annotations

from flask import Blueprint, redirect, render_template, request, session, url_for

from campusclaw import request_context as rc
from campusclaw import security, sessions
from campusclaw.config import DEFAULT_SEED_PASSWORDS, SEED_ACCOUNTS
from campusclaw.errors import MESSAGES, ApiError

bp = Blueprint("auth", __name__)

#: 登录页的预置账号速填卡片。只用于展示内置演示凭据，不参与任何鉴权判定。
_AVATARS = {
    ("teacher", "A"): "👩‍🏫",
    ("student", "A"): "🧑‍🎓",
    ("student", "B"): "👧",
    ("teacher", "B"): "👨‍🏫",
}

DEMO_ACCOUNTS = [
    {
        "username": account.username,
        "password": DEFAULT_SEED_PASSWORDS[account.username],
        "role": "教师" if account.role == "teacher" else "学生",
        "class_name": account.class_name,
        "emoji": _AVATARS.get((account.role, account.class_name), "👤"),
    }
    for account in SEED_ACCOUNTS
]


@bp.get("/login")
def login_page():
    token = rc.ensure_csrf_token()
    return render_template("login.html", csrf_token=token, accounts=DEMO_ACCOUNTS)


@bp.post("/login")
def login():
    # 顺序：CSRF → 撤销旧会话 → 输入校验 → 口令校验 → 建立新会话。
    rc.reject_identity_fields()
    if not security.tokens_equal(
        str(session.get(rc.CSRF_FIELD) or ""), str(rc.submitted_csrf_token() or "")
    ):
        raise ApiError(403, "CSRF_INVALID", MESSAGES["CSRF_INVALID"])

    conn = rc.get_conn()
    rc.revoke_current_session(conn)

    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""
    if username == "" or password == "":
        raise ApiError(400, "INVALID_INPUT", MESSAGES["INVALID_INPUT"])

    user = rc.get_conn().execute(
        "SELECT id, username, password_hash, role, class_id FROM users WHERE username = ?",
        (username,),
    ).fetchone()

    if user is None or not security.verify_password(password, user["password_hash"]):
        # 不区分账号是否存在，统一 401 提示。
        raise ApiError(401, "INVALID_CREDENTIALS", "账号或密码错误")

    from campusclaw.timeutil import now_utc

    sid = sessions.create_session(
        conn, int(user["id"]), now_utc(), rc.get_settings().session_hours
    )
    session.clear()
    session["sid"] = sid
    rc.rotate_csrf_token()
    return redirect(url_for("pages.materials_page"), code=302)


@bp.post("/logout")
def logout():
    rc.require_api_auth()
    rc.require_csrf()
    conn = rc.get_conn()
    sessions.revoke_session(conn, session.get("sid"))
    session.clear()
    rc.ensure_csrf_token()
    return redirect(url_for("auth.login_page"), code=302)
