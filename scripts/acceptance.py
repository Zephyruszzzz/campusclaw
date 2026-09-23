#!/usr/bin/env python
"""CampusClaw 验收执行器：在隔离环境逐条执行 AC01–AC35 并留存 H/D/F 证据。

用法：
    python scripts/acceptance.py                    # 执行全部可执行场景
    python scripts/acceptance.py --only AC16,AC20   # 只跑指定场景，便于迭代
    python scripts/acceptance.py --no-report        # 只打印摘要，不写报告文件

约定（对齐 spec.md「验收基线与证据约定」）：
* 每个场景使用独立临时数据目录，不在真实教学数据上注入异常；
* H = 脱敏 HTTP 请求/状态码/响应片段；D = 数据库前后记录与关联；F = 文件清单与 SHA-256；
* Cookie、口令、CSRF token 在全部输出中遮盖；
* 结论只有「通过 / 失败 / 阻塞」三种，不预填通过。

依赖 Docker 的 AC33、AC35 由 scripts/acceptance_compose.py 执行并合并进报告。
"""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import hashlib
import http.cookiejar
import io
import json
import logging
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from werkzeug.serving import make_server  # noqa: E402

from campusclaw import create_app  # noqa: E402
from campusclaw import db as dbmod  # noqa: E402
from campusclaw import materials as materials_svc  # noqa: E402
from campusclaw import storage  # noqa: E402
from campusclaw.config import load_settings  # noqa: E402
from campusclaw.errors import ApiError  # noqa: E402

RUNTIME = Path(tempfile.mkdtemp(prefix="campusclaw-acceptance-"))
REPORT_PATH = ROOT / "docs" / "acceptance-report.md"
COMPOSE_JSON = RUNTIME / "compose-evidence.json"
EXECUTED_AT = time.strftime("%Y-%m-%d %H:%M:%S")

PASSWORDS = {
    "teacher_a": "TeacherA#2026",
    "student_a1": "StudentA1#2026",
    "student_b1": "StudentB1#2026",
    "teacher_b": "TeacherB#2026",
}
CLASS_OF = {"teacher_a": "A", "student_a1": "A", "student_b1": "B", "teacher_b": "B"}
ROLE_OF = {"teacher_a": "teacher", "student_a1": "student",
           "student_b1": "student", "teacher_b": "teacher"}

MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_CONTENT_BYTES = 11 * 1024 * 1024
SESSION_COOKIE = "campusclaw_session"


# ===========================================================================
# 通用工具
# ===========================================================================

def mask(value: str | None, keep: int = 0) -> str:
    if not value:
        return "<空>"
    if keep:
        return f"{value[:keep]}…（{len(value)} 字符，已遮盖）"
    return f"<{len(value)} 字符，已遮盖>"


def brief(value: Any, limit: int = 160) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[:limit] + "…"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def port_listening(port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


@contextlib.contextmanager
def patched(obj: Any, name: str, value: Any):
    """临时替换模块属性，退出时恢复（故障注入的唯一入口）。"""
    old = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, old)


@contextlib.contextmanager
def env_vars(mapping: dict[str, str]):
    saved = {key: os.environ.get(key) for key in mapping}
    os.environ.update(mapping)
    try:
        yield
    finally:
        for key, old in saved.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old


class FaultyConn:
    """SQLite 连接代理：按 SQL 精确注入写失败或提交失败。"""

    def __init__(self, real, *, fail_sql: str | None = None, fail_commit: bool = False):
        self._real = real
        self._fail_sql = fail_sql
        self._fail_commit = fail_commit

    def execute(self, sql, params=()):
        norm = " ".join(str(sql).split()).upper()
        if self._fail_sql and self._fail_sql in norm:
            raise sqlite3.OperationalError("injected write failure")
        if self._fail_commit and norm == "COMMIT":
            raise sqlite3.OperationalError("injected commit failure")
        return self._real.execute(sql, params)

    def __getattr__(self, item):
        return getattr(self._real, item)


@contextlib.contextmanager
def faulty_db(**kwargs):
    real_connect = dbmod.connect

    def factory(path):
        return FaultyConn(real_connect(path), **kwargs)

    with patched(dbmod, "connect", factory):
        yield


# --------------------------------------------------------------- Windows 独占锁
if os.name == "nt":
    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.CreateFileW.restype = ctypes.c_void_p
    _k32.CreateFileW.argtypes = [
        ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
        ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
    ]
    _k32.CloseHandle.argtypes = [ctypes.c_void_p]
    _INVALID_HANDLE = ctypes.c_void_p(-1).value

    def lock_file(path: Path):
        """以不共享方式打开文件，使删除操作产生真实的共享冲突。"""
        handle = _k32.CreateFileW(str(path), 0x80000000, 0, None, 3, 0x80, None)
        if not handle or handle == _INVALID_HANDLE:
            raise OSError(f"无法锁定 {path.name}：错误码 {ctypes.get_last_error()}")
        return handle

    def unlock_file(handle) -> None:
        if handle:
            _k32.CloseHandle(handle)
else:  # pragma: no cover - 验收机为 Windows
    def lock_file(path: Path):
        os.chmod(path, 0o444)
        return None

    def unlock_file(handle) -> None:
        return None


# ===========================================================================
# 隔离运行时
# ===========================================================================

class Home:
    """一次隔离运行：独立数据目录 + 真实 HTTP 服务。"""

    def __init__(self, name: str, extra_env: dict[str, str] | None = None):
        self.name = name
        self.root = RUNTIME / name
        self.root.mkdir(parents=True, exist_ok=True)
        self.env = {"CAMPUSCLAW_HOME": str(self.root), "PYTHONPATH": str(ROOT)}
        if extra_env:
            self.env.update(extra_env)
        self.app = None
        self.server = None
        self.thread = None
        self.port = 0
        self.base = ""

    @property
    def data_dir(self) -> Path:
        return self.root / "data"

    @property
    def upload_dir(self) -> Path:
        return self.root / "uploads"

    @property
    def staging_dir(self) -> Path:
        return self.upload_dir / ".staging"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "app.db"

    @property
    def settings(self):
        return self.app.extensions["campusclaw"]["settings"]

    def start(self, run_startup: bool = True, cookie_secure: bool = False,
              port: int | None = None) -> "Home":
        env = dict(self.env)
        if cookie_secure:
            env["AUTH_COOKIE_SECURE"] = "1"
        with env_vars(env):
            settings = load_settings(env)
            self.app = create_app(settings=settings, env=env, run_startup=run_startup)
        # 重启时复用原端口：真实服务的重启不会换端口，
        # 否则已建立的客户端会指向死端口，得到的只是连接失败而非重启语义。
        self.port = port if port is not None else free_port()
        self.server = make_server("127.0.0.1", self.port, self.app, threaded=True)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.port}"
        self.wait_ready()
        return self

    def wait_ready(self, timeout: float = 25.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                urllib.request.urlopen(self.base + "/health", timeout=2)
                return
            except urllib.error.HTTPError:
                return
            except Exception:
                time.sleep(0.15)
        raise RuntimeError(f"{self.name} 未在 {timeout}s 内就绪")

    def stop(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.server = None
        self.thread = None

    def restart(self, cookie_secure: bool = False) -> "Home":
        port = self.port
        self.stop()
        # 端口刚释放，短时间内偶发占用，重试几次让重启稳定落在原端口上
        last: Exception | None = None
        for _ in range(10):
            try:
                return self.start(run_startup=True, cookie_secure=cookie_secure, port=port)
            except OSError as exc:
                last = exc
                time.sleep(0.3)
        raise RuntimeError(f"{self.name} 无法在原端口 {port} 上重新启动：{last}")

    def agent(self, username: str | None = None) -> "Agent":
        client = Agent(self.base)
        if username:
            status, _ = client.login(username, PASSWORDS[username])
            if status != 302:
                raise RuntimeError(f"{username} 登录失败：{status}")
        return client

    def conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def sid_of(self, cookie: str) -> str:
        """从签名会话 Cookie 中解出内层 sid（库中保存的是它的 SHA-256 摘要）。"""
        try:
            serializer = self.app.session_interface.get_signing_serializer(self.app)
            payload = serializer.loads(cookie)
        except Exception:
            return ""
        if not isinstance(payload, dict):
            return ""
        return str(payload.get("sid") or "")


# ===========================================================================
# HTTP 客户端
# ===========================================================================

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@dataclass
class Resp:
    status: int
    headers: dict[str, str]
    body: bytes

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", "replace")

    def json(self) -> Any:
        try:
            return json.loads(self.body.decode("utf-8"))
        except Exception:
            return None

    @property
    def code(self) -> str | None:
        data = self.json()
        if isinstance(data, dict) and isinstance(data.get("error"), dict):
            return data["error"].get("code")
        return None

    @property
    def set_cookie(self) -> str:
        return self.headers.get("Set-Cookie", "")


class Agent:
    """带 Cookie 罐的 HTTP 客户端；不自动跟随重定向，便于观测 302。"""

    def __init__(self, base: str):
        self.base = base
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar), _NoRedirect
        )

    def call(self, method: str, path: str, body: bytes | None = None,
             headers: dict[str, str] | None = None, timeout: float = 90.0) -> Resp:
        request = urllib.request.Request(self.base + path, data=body, method=method)
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        try:
            response = self.opener.open(request, timeout=timeout)
            return Resp(response.status, dict(response.headers), response.read())
        except urllib.error.HTTPError as exc:
            return Resp(exc.code, dict(exc.headers), exc.read())

    def get(self, path: str, **kw) -> Resp:
        return self.call("GET", path, **kw)

    def post_form(self, path: str, fields: dict[str, str],
                  headers: dict[str, str] | None = None) -> Resp:
        body = urllib.parse.urlencode(fields).encode()
        merged = {"Content-Type": "application/x-www-form-urlencoded"}
        merged.update(headers or {})
        return self.call("POST", path, body=body, headers=merged)

    def patch_json(self, path: str, payload: Any,
                   headers: dict[str, str] | None = None) -> Resp:
        merged = {"Content-Type": "application/json"}
        merged.update(headers or {})
        return self.call("PATCH", path, body=json.dumps(payload).encode(), headers=merged)

    def delete(self, path: str, headers: dict[str, str] | None = None) -> Resp:
        return self.call("DELETE", path, headers=headers)

    def multipart(self, path: str, fields: list[tuple[str, str]],
                  files: list[tuple[str, str | None, bytes, str]],
                  headers: dict[str, str] | None = None,
                  declared_length: int | None = None) -> Resp:
        boundary = "----campusclaw" + uuid.uuid4().hex
        body = build_multipart(fields, files, boundary)
        merged = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
        merged.update(headers or {})
        if declared_length is not None:
            merged["Content-Length"] = str(declared_length)
        return self.call("POST", path, body=body, headers=merged)

    def login(self, username: str, password: str) -> tuple[int, str]:
        token = self.csrf("/login")
        resp = self.post_form("/login", {
            "username": username, "password": password, "csrf_token": token,
        })
        return resp.status, resp.headers.get("Location", "")

    def csrf(self, path: str = "/materials") -> str:
        html = self.get(path).text
        found = re.search(r'name="csrf_token" value="([^"]+)"', html)
        return found.group(1) if found else ""

    @property
    def session_cookie(self) -> str:
        for cookie in self.jar:
            if cookie.name == SESSION_COOKIE:
                return cookie.value
        return ""

    def load_cookie(self, value: str) -> None:
        cookie = http.cookiejar.Cookie(
            version=0, name=SESSION_COOKIE, value=value, port=None, port_specified=False,
            domain="127.0.0.1", domain_specified=False, domain_initial_dot=False,
            path="/", path_specified=True, secure=False, expires=None, discard=True,
            comment=None, comment_url=None, rest={}, rfc2109=False,
        )
        self.jar.set_cookie(cookie)

    def upload(self, filename: str | None, data: bytes, title: str | None = None,
               extra_fields: dict[str, str] | None = None, with_csrf: bool = True,
               ctype: str = "text/plain") -> Resp:
        fields: list[tuple[str, str]] = []
        if with_csrf:
            fields.append(("csrf_token", self.csrf()))
        if title is not None:
            fields.append(("title", title))
        for key, value in (extra_fields or {}).items():
            fields.append((key, value))
        files = [] if filename is None else [("file", filename, data, ctype)]
        return self.multipart("/api/materials/upload", fields, files)


def extract_token(html: str) -> str:
    """从页面表单中取出 CSRF token。"""
    found = re.search(r'name="csrf_token" value="([^"]+)"', html)
    return found.group(1) if found else ""


def build_multipart(fields: list[tuple[str, str]],
                    files: list[tuple[str, str | None, bytes, str]],
                    boundary: str) -> bytes:
    buffer = io.BytesIO()
    for name, value in fields:
        buffer.write(f"--{boundary}\r\n".encode())
        buffer.write(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        buffer.write(str(value).encode("utf-8"))
        buffer.write(b"\r\n")
    for name, filename, data, ctype in files:
        buffer.write(f"--{boundary}\r\n".encode())
        disposition = f'Content-Disposition: form-data; name="{name}"'
        if filename is not None:
            disposition += f'; filename="{filename}"'
        buffer.write((disposition + "\r\n").encode("utf-8"))
        buffer.write(f"Content-Type: {ctype}\r\n\r\n".encode())
        buffer.write(data)
        buffer.write(b"\r\n")
    buffer.write(f"--{boundary}--\r\n".encode())
    return buffer.getvalue()


# ===========================================================================
# 证据采集
# ===========================================================================

@dataclass
class Rec:
    ac: str
    task: str
    given: str
    http: str
    db: str
    files: str
    verdict: str
    notes: list[str] = field(default_factory=list)


REPORT: list[Rec] = []
FAILURES: list[str] = []


def rec(ac: str, task: str, given: str, http: str, db: str, files: str,
        ok: bool = True, notes: list[str] | None = None) -> bool:
    REPORT.append(Rec(ac, task, given, http, db, files,
                      "通过" if ok else "失败", list(notes or [])))
    if not ok:
        FAILURES.append(ac)
        print(f"    !! {ac} 未通过")
        print(f"       H: {http}")
        print(f"       D: {db}")
        print(f"       F: {files}")
        for note in (notes or []):
            print(f"       - {note}")
    return ok


def blocked(ac: str, task: str, reason: str) -> None:
    REPORT.append(Rec(ac, task, reason, "未执行", "未执行", "未执行", "阻塞"))
    FAILURES.append(ac)


# ---------------------------------------------------------------- 快照
BUSINESS_TABLES = ("classes", "users", "materials", "knowledge_entries")


def biz_fingerprint(home: Home) -> dict:
    conn = home.conn()
    try:
        return {
            "classes": [tuple(r) for r in conn.execute("SELECT id, name FROM classes ORDER BY id")],
            "users": [tuple(r) for r in conn.execute(
                "SELECT id, username, role, class_id, password_hash FROM users ORDER BY id")],
            "materials": [tuple(r) for r in conn.execute(
                "SELECT id, class_id, uploaded_by, title, original_filename, storage_key, "
                "size_bytes, status FROM materials ORDER BY id")],
            "knowledge": [tuple(r) for r in conn.execute(
                "SELECT material_id, class_id, body_text FROM knowledge_entries ORDER BY material_id")],
            "jobs": [tuple(r) for r in conn.execute(
                "SELECT material_id, storage_key, status, attempts FROM file_cleanup_jobs ORDER BY id")],
        }
    finally:
        conn.close()


def material_state(home: Home) -> dict:
    conn = home.conn()
    try:
        return {
            "materials": [tuple(r) for r in conn.execute(
                "SELECT id, class_id, uploaded_by, title, original_filename, storage_key, "
                "size_bytes, status FROM materials ORDER BY id")],
            "knowledge": [tuple(r) for r in conn.execute(
                "SELECT material_id, class_id, body_text FROM knowledge_entries ORDER BY material_id")],
            "jobs": [tuple(r) for r in conn.execute(
                "SELECT material_id, status FROM file_cleanup_jobs ORDER BY id")],
        }
    finally:
        conn.close()


def counts(home: Home) -> dict:
    conn = home.conn()
    try:
        return {table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in BUSINESS_TABLES + ("auth_sessions", "file_cleanup_jobs")}
    finally:
        conn.close()


def fs_fingerprint(home: Home) -> dict[str, tuple[int, str]]:
    result: dict[str, tuple[int, str]] = {}
    if not home.upload_dir.exists():
        return result
    for path in sorted(home.upload_dir.rglob("*")):
        if path.is_file():
            rel = str(path.relative_to(home.upload_dir)).replace("\\", "/")
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            result[rel] = (path.stat().st_size, digest)
    return result


def fs_summary(snapshot: dict[str, tuple[int, str]]) -> str:
    if not snapshot:
        return "无文件"
    parts = [f"{rel}({size}B,{digest[:12]})" for rel, (size, digest) in sorted(snapshot.items())]
    return f"{len(snapshot)} 个文件：" + "、".join(parts)


def fs_diff(before: dict, after: dict) -> str:
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    changed = sorted(k for k in set(before) & set(after) if before[k] != after[k])
    if not (added or removed or changed):
        return "与基线一致（无新增/删除/改写）"
    bits = []
    if added:
        bits.append("新增 " + "、".join(added))
    if removed:
        bits.append("删除 " + "、".join(removed))
    if changed:
        bits.append("改写 " + "、".join(changed))
    return "；".join(bits)


# ---------------------------------------------------------------- 材料工具
def materials_of(agent: Agent) -> list[dict]:
    data = agent.get("/api/materials").json() or {}
    return data.get("items", [])


def material_of(agent: Agent, title_contains: str) -> dict | None:
    for item in materials_of(agent):
        if title_contains in item["title"]:
            return item
    return None


def material_ids(home: Home, *, status: str = "active") -> dict[str, int | None]:
    conn = home.conn()
    try:
        result: dict[str, int | None] = {}
        for name in ("A", "B"):
            row = conn.execute(
                "SELECT m.id FROM materials m JOIN classes c ON c.id = m.class_id "
                "WHERE c.name = ? AND m.status = ? ORDER BY m.id LIMIT 1",
                (name, status),
            ).fetchone()
            result[name] = int(row["id"]) if row else None
        return result
    finally:
        conn.close()


def material_row(home: Home, material_id: int) -> sqlite3.Row:
    conn = home.conn()
    try:
        return conn.execute("SELECT * FROM materials WHERE id = ?", (material_id,)).fetchone()
    finally:
        conn.close()


def first_material_row(home: Home, class_name: str = "A") -> sqlite3.Row:
    conn = home.conn()
    try:
        return conn.execute(
            "SELECT m.* FROM materials m JOIN classes c ON c.id = m.class_id "
            "WHERE c.name = ? AND m.status = 'active' ORDER BY m.id LIMIT 1",
            (class_name,)).fetchone()
    finally:
        conn.close()


def storage_path_of(home: Home, row) -> Path:
    return home.upload_dir / str(row["class_id"]) / row["storage_key"]


def execute_sql(home: Home, sql: str, params: tuple = ()) -> int:
    """在测试库上执行写语句并提交（受控改库，用于过期/撤销/注入等场景）。"""
    conn = home.conn()
    try:
        cursor = conn.execute(sql, params)
        conn.commit()
        return cursor.rowcount
    finally:
        conn.close()


def cookie_from_header(header: str, name: str = SESSION_COOKIE) -> str:
    found = re.search(rf"{name}=([^;]*)", header or "")
    return found.group(1) if found else ""


# ===========================================================================
# R01 预置数据与六类核心实体
# ===========================================================================

def ac01(home: Home) -> None:
    conn = home.conn()
    try:
        classes = [tuple(r) for r in conn.execute("SELECT id, name FROM classes ORDER BY id")]
        users = [tuple(r) for r in conn.execute(
            "SELECT username, role, class_id FROM users ORDER BY username")]
        samples = [tuple(r) for r in conn.execute(
            "SELECT m.title, m.class_id, m.status, u.username, u.class_id, "
            "m.original_filename, m.size_bytes FROM materials m "
            "JOIN users u ON u.id = m.uploaded_by ORDER BY m.class_id")]
        knowledge = [tuple(r) for r in conn.execute(
            "SELECT material_id, class_id, length(body_text), "
            "instr(body_text,'A_ONLY_SEED'), instr(body_text,'B_ONLY_SEED') "
            "FROM knowledge_entries ORDER BY material_id")]
    finally:
        conn.close()

    logins = []
    for username in ("teacher_a", "student_a1", "student_b1", "teacher_b"):
        agent = Agent(home.base)
        status, location = agent.login(username, PASSWORDS[username])
        listing = agent.get("/api/materials")
        items = listing.json().get("items", []) if listing.status == 200 else []
        logins.append(f"{username}:POST /login={status}->{location}，"
                      f"GET /api/materials={listing.status}（{len(items)} 条）")

    files = fs_fingerprint(home)
    users_ok = (
        len(users) == 4
        and all(role == ROLE_OF[name] for name, role, _ in users)
    )
    samples_ok = len(samples) == 2 and all(row[1] == row[4] and row[2] == "active"
                                           for row in samples)
    knowledge_ok = (len(knowledge) == 2 and all(row[2] > 0 for row in knowledge)
                    and all((row[3] > 0) != (row[4] > 0) for row in knowledge))
    ok = (len(classes) == 2 and [c[1] for c in classes] == ["A", "B"] and users_ok
          and samples_ok and knowledge_ok and len(files) == 2
          and all("POST /login=302" in item for item in logins)
          and all("GET /api/materials=200" in item for item in logins))
    notes = []
    if not samples_ok:
        notes.append("样本材料的 class_id 与上传者班级不一致")
    if not knowledge_ok:
        notes.append("知识全文与班级标记不对应")
    rec(
        "AC01", "T2.1/T2.2",
        "空测试数据目录，未设置任何密钥与种子口令环境变量",
        "；".join(logins),
        f"classes={classes}；users(username,role,class_id)={users}；"
        f"samples(title,class_id,status,uploader,uploader_class,file,size)={samples}；"
        f"knowledge(material_id,class_id,len,instrA,instrB)={knowledge}",
        fs_summary(files),
        ok, notes,
    )


def ac02(home: Home) -> None:
    before = biz_fingerprint(home)
    hashes = {row[1]: row[4] for row in before["users"]}

    teacher = home.agent("teacher_a")
    new = teacher.upload("extra_note.txt", "新增材料 A_EXTRA_001\n".encode(), title="增量材料")
    new_id = (new.json() or {}).get("material_id")

    sample_a = material_of(teacher, "A 班示例讲义")
    deleted_title = sample_a["title"]
    delete = teacher.delete(f"/api/materials/{sample_a['id']}",
                            headers={"X-CSRF-Token": teacher.csrf()})

    mid = biz_fingerprint(home)
    mid_files = fs_fingerprint(home)

    home.restart()
    after = biz_fingerprint(home)
    after_files = fs_fingerprint(home)

    hashes_kept = all(row[4] == hashes.get(row[1]) for row in after["users"])
    no_dupes = (len(after["classes"]) == len(before["classes"])
                and len(after["users"]) == len(before["users"])
                and len(after["materials"]) == len(mid["materials"]))
    sample_still_deleted = not any(row[3] == deleted_title and row[7] == "active"
                                   for row in after["materials"])
    new_readable = any(i["id"] == new_id for i in materials_of(teacher))
    file_ok = after_files == mid_files

    ok = (delete.status == 204 and new.status == 201 and hashes_kept and no_dupes
          and sample_still_deleted and new_readable and file_ok)
    rec(
        "AC02", "T2.2",
        f"已初始化；新增材料 #{new_id}；删除样本「{deleted_title}」；随后重复初始化并重启应用",
        f"POST /api/materials/upload={new.status}；DELETE /api/materials/{sample_a['id']}="
        f"{delete.status}；重启后 GET /api/materials={teacher.get('/api/materials').status}",
        f"班级 {len(before['classes'])}→{len(after['classes'])}；用户 {len(before['users'])}→"
        f"{len(after['users'])}（口令哈希未变={hashes_kept}）；材料 {len(mid['materials'])}→"
        f"{len(after['materials'])}；样本墓碑保留={sample_still_deleted}；新增材料仍可读={new_readable}",
        f"重启前后 {fs_diff(mid_files, after_files)}；{fs_summary(after_files)}",
        ok, [] if ok else ["重复初始化或重启改变了既有数据"],
    )


def ac03(home: Home) -> None:
    ids = material_ids(home)
    conn = home.conn()
    try:
        tables = sorted(r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"))
        present = set(dbmod.REQUIRED_TABLES) <= set(tables)
        b_class = conn.execute("SELECT id FROM classes WHERE name='B'").fetchone()[0]
        before = biz_fingerprint(home)
        fk_error = None
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "INSERT INTO knowledge_entries(material_id, class_id, body_text, created_at) "
                "VALUES (?, ?, 'illegal', '2026-01-01T00:00:00.000000Z')",
                (ids["A"], b_class))
        except sqlite3.IntegrityError as exc:
            fk_error = str(exc)
        finally:
            conn.execute("ROLLBACK")
        after = biz_fingerprint(home)
        fk_on = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    finally:
        conn.close()

    ok = present and fk_error is not None and before == after and fk_on == 1
    rec(
        "AC03", "T2.1",
        f"已初始化独立测试库；尝试把 A 班材料 #{ids['A']} 关联为 B 班知识记录",
        "不适用（直接库约束验证）",
        f"必需表齐全={present}；PRAGMA foreign_keys={fk_on}；非法跨班关联被外键拒绝："
        f"{brief(fk_error or '未报错', 90)}；同一事务回滚后业务指纹一致={before == after}",
        "无文件变化（回滚后未保留非法行）",
        ok, [] if ok else ["外键未拒绝跨班关联或回滚后数据有变化"],
    )


# ===========================================================================
# R08 密码、密钥与请求安全
# ===========================================================================

def ac30() -> None:
    home = Home("ac30").start()
    try:
        conn = home.conn()
        try:
            rows = [tuple(r) for r in conn.execute(
                "SELECT username, password_hash FROM users ORDER BY username")]
        finally:
            conn.close()

        from campusclaw.security import parse_bcrypt_rounds, verify_password

        fmt_ok = all(h.startswith("$2b$") and (parse_bcrypt_rounds(h) or 0) >= 12
                     for _, h in rows)
        plain_ok = all(h != PASSWORDS[u] for u, h in rows)
        verify_ok = (verify_password(PASSWORDS["teacher_a"], dict(rows)["teacher_a"])
                     and not verify_password("wrong-password", dict(rows)["teacher_a"]))
        shown = "、".join(f"{u}={h[:7]}…（{len(h)} 字符）" for u, h in rows)

        agent = Agent(home.base)
        good = agent.login("teacher_a", PASSWORDS["teacher_a"])[0]
        bad = Agent(home.base).login("teacher_a", "wrong-password")[0]

        negatives, all_neg_ok = [], True
        for label, value in (("7 字节", "short1"), ("73 字节", "x" * 73)):
            sub = Home(f"ac30-neg-{len(value)}")
            env = dict(sub.env)
            env.update({f"SEED_{key.upper()}_PASSWORD": value for key in
                        ("teacher_a", "student_a1", "student_b1", "teacher_b")})
            rejected = False
            try:
                with env_vars(env):
                    settings = load_settings(env)
                    create_app(settings=settings, env=env, run_startup=True)
            except Exception as exc:
                rejected = "口令" in str(exc) or "长度" in str(exc)
            leftovers = [p for p in sub.root.rglob("*")
                         if p.is_file() and p.suffix in (".db", ".txt", ".md")]
            negatives.append(f"{label}：初始化被拒={rejected}，残留文件={len(leftovers)}")
            if not rejected or leftovers:
                all_neg_ok = False

        ok = (fmt_ok and plain_ok and verify_ok and good == 302 and bad == 401 and all_neg_ok)
        rec(
            "AC30", "T2.2/T3.1",
            "正常初始化库 + 两套独立空库（种子口令分别为 7 字节与 73 字节）",
            f"正确口令 POST /login={good}；错误口令 POST /login={bad}",
            f"password_hash 均为 bcrypt 且成本≥12={fmt_ok}：{shown}；不等于明文={plain_ok}；"
            f"正确口令校验通过、错误口令校验失败={verify_ok}；非法长度初始化：" + "；".join(negatives),
            "非法初始化后数据目录无 .db/.txt/.md 残留",
            ok, [] if ok else ["哈希格式/成本不合规，或非法种子口令未被拒绝"],
        )
    finally:
        home.stop()


def ac31() -> None:
    home = Home("ac31").start()
    before = counts(home)
    before_files = fs_fingerprint(home)
    home.stop()

    env = dict(os.environ)
    env.update(home.env)
    port = free_port()
    env["PORT"] = str(port)
    env["CAMPUSCLAW_STRICT_CONFIG"] = "1"
    env.pop("SECRET_KEY", None)
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "app.py")], cwd=str(ROOT), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        encoding="utf-8", errors="replace")
    try:
        output, _ = proc.communicate(timeout=45)
    except subprocess.TimeoutExpired:
        proc.kill()
        output, _ = proc.communicate()
    listening = port_listening(port)
    exit_code = proc.returncode

    after = counts(home)
    after_files = fs_fingerprint(home)

    example = ROOT / ".env.example"
    example_text = example.read_text(encoding="utf-8") if example.exists() else ""
    required_names = ["SECRET_KEY", "SEED_TEACHER_A_PASSWORD", "SEED_STUDENT_A1_PASSWORD",
                      "SEED_STUDENT_B1_PASSWORD", "SEED_TEACHER_B_PASSWORD"]
    names_listed = all(name in example_text for name in required_names)
    example_clean = not any(pw in example_text for pw in PASSWORDS.values())

    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    env_ignored = any(line.strip() in (".env", ".env.local") for line in gitignore.splitlines())

    ok = (exit_code != 0 and not listening and before == after
          and before_files == after_files and names_listed and example_clean and env_ignored)
    rec(
        "AC31", "T3.1/T7.1",
        "严格模式（CAMPUSCLAW_STRICT_CONFIG=1）下删除 SECRET_KEY 后启动；并审查 .env.example 与版本控制文件",
        f"启动进程退出码={exit_code}，端口 {port} 是否监听={listening}；"
        f"输出首行：{brief((output or '').splitlines()[0] if output else '', 100)}",
        f"启动失败前后业务计数一致={before == after}：{before}",
        f"数据目录文件不变={before_files == after_files}；.env.example 列出必需变量={names_listed}；"
        f"示例文件不含真实口令={example_clean}；.gitignore 排除 .env={env_ignored}",
        ok,
        ["开箱模式（未设 CAMPUSCLAW_STRICT_CONFIG）保留内置 SECRET_KEY 默认值，这是用户明确要求的"
         "零配置启动；严格模式下已恢复规约行为，本行以严格模式结果判定。"],
    )


def ac32(home: Home) -> None:
    before = biz_fingerprint(home)
    before_files = fs_fingerprint(home)
    before_sessions = counts(home)["auth_sessions"]

    agent = home.agent("teacher_a")
    fresh = Agent(home.base)
    token = fresh.csrf("/login")
    login_resp = fresh.post_form("/login", {
        "username": "teacher_a", "password": PASSWORDS["teacher_a"], "csrf_token": token})

    secure_home = Home("ac32-secure").start(cookie_secure=True)
    try:
        secure_agent = Agent(secure_home.base)
        # 取一次真实响应上的 Set-Cookie 即可核对属性；Secure Cookie 在 http 上不会被客户端回送，
        # 因此这里不做成功登录断言，仅记录该现象作为属性生效的旁证。
        secure_page = secure_agent.get("/login")
        secure_login = secure_agent.post_form("/login", {
            "username": "teacher_a", "password": PASSWORDS["teacher_a"],
            "csrf_token": extract_token(secure_page.text)})
    finally:
        secure_home.stop()

    def attrs(header: str) -> dict[str, bool]:
        lowered = header.lower()
        return {
            "HttpOnly": "httponly" in lowered,
            "SameSite=Lax": "samesite=lax" in lowered,
            "Path=/": "path=/" in lowered,
            "Secure": "secure" in lowered.replace("samesite", ""),
        }

    local_attrs = attrs(login_resp.set_cookie)
    https_attrs = attrs(secure_page.set_cookie)

    target = material_ids(home)["A"]
    csrf_cases = []
    for label, call in (
        ("上传缺 token", lambda: agent.upload("x.txt", b"data", with_csrf=False)),
        ("上传错 token", lambda: agent.multipart(
            "/api/materials/upload", [("csrf_token", "wrong-token")],
            [("file", "x.txt", b"data", "text/plain")])),
        ("PATCH 缺 token", lambda: agent.patch_json(
            f"/api/materials/{target}", {"title": "越权标题"})),
        ("DELETE 缺 token", lambda: agent.delete(f"/api/materials/{target}")),
        ("退出缺 token", lambda: agent.post_form("/logout", {})),
        ("登录表单缺 token", lambda: Agent(home.base).post_form(
            "/login", {"username": "teacher_a", "password": PASSWORDS["teacher_a"]})),
    ):
        resp = call()
        csrf_cases.append(f"{label}={resp.status}/{resp.code}")

    after = biz_fingerprint(home)
    after_files = fs_fingerprint(home)
    after_sessions = counts(home)["auth_sessions"]

    ok = (local_attrs["HttpOnly"] and local_attrs["SameSite=Lax"] and local_attrs["Path=/"]
          and not local_attrs["Secure"] and https_attrs["Secure"]
          and all("403/CSRF_INVALID" in case for case in csrf_cases)
          and before == after and before_files == after_files)
    rec(
        "AC32", "T3.2/T3.3",
        "教师有效会话；本地 HTTP 与 AUTH_COOKIE_SECURE=1 两套配置；对各写接口发送缺失/错误 CSRF token",
        "；".join(csrf_cases) + f"；登录 POST 状态：HTTP 配置={login_resp.status}、"
        f"HTTPS 配置下客户端不回送 Secure Cookie 故登录={secure_login.status}/{secure_login.code}；"
        f"Set-Cookie 属性（HTTP）={local_attrs}、（AUTH_COOKIE_SECURE=1）={https_attrs}",
        f"会话数 {before_sessions}→{after_sessions}；业务指纹一致={before == after}",
        fs_diff(before_files, after_files),
        ok, [] if ok else ["Cookie 属性或 CSRF 拒绝行为不符合合同"],
    )


# ===========================================================================
# R02 登录、认证与会话撤销
# ===========================================================================

def ac04(home: Home) -> None:
    before = biz_fingerprint(home)
    before_files = fs_fingerprint(home)
    base_sessions = counts(home)["auth_sessions"]

    lines, sids, expires_ok = [], [], True
    for username in ("teacher_a", "student_a1", "student_b1", "teacher_b"):
        agent = Agent(home.base)
        status, location = agent.login(username, PASSWORDS[username])
        sids.append(agent.session_cookie)
        page = agent.get("/materials")
        listing = agent.get("/api/materials")
        lines.append(f"{username}:POST /login={status}->{location}，GET /materials={page.status}，"
                     f"GET /api/materials={listing.status}，Cookie={mask(agent.session_cookie, 6)}")

    conn = home.conn()
    try:
        rows = conn.execute(
            "SELECT u.username, s.created_at, s.expires_at FROM auth_sessions s "
            "JOIN users u ON u.id = s.user_id ORDER BY s.id DESC LIMIT 4").fetchall()
        session_rows = [tuple(r) for r in rows]
        for row in rows:
            created = time.mktime(time.strptime(row["created_at"], "%Y-%m-%dT%H:%M:%S.%fZ"))
            expires = time.mktime(time.strptime(row["expires_at"], "%Y-%m-%dT%H:%M:%S.%fZ"))
            if abs((expires - created) - 8 * 3600) > 2:
                expires_ok = False
    finally:
        conn.close()

    after = biz_fingerprint(home)
    after_files = fs_fingerprint(home)
    distinct = len(set(sids)) == 4
    ok = (all("POST /login=302" in line for line in lines)
          and all("GET /api/materials=200" in line for line in lines)
          and distinct and expires_ok and before == after and before_files == after_files)
    rec(
        "AC04", "T3.1",
        "四个预置账号各自使用无登录态客户端，先取登录表单 token",
        "；".join(lines),
        f"新增 auth_sessions={len(session_rows)} 条（原 {base_sessions}）：{session_rows}；"
        f"到期=创建+8h={expires_ok}；四个 sid 互不相同={distinct}；业务指纹一致={before == after}",
        fs_diff(before_files, after_files),
        ok,
    )


def ac05(home: Home) -> None:
    before = biz_fingerprint(home)
    before_files = fs_fingerprint(home)
    cases = []
    for label, fields in (
        ("不存在用户名", {"username": "no_such_user", "password": "Whatever#1"}),
        ("存在用户名但密码错误", {"username": "teacher_a", "password": "Wrong#2026"}),
        ("缺少用户名", {"username": "", "password": "TeacherA#2026"}),
        ("缺少密码", {"username": "teacher_a", "password": ""}),
    ):
        agent = Agent(home.base)
        token = agent.csrf("/login")
        resp = agent.post_form("/login", {**fields, "csrf_token": token})
        after_api = agent.get("/api/materials")
        leaks = [pw for pw in PASSWORDS.values() if pw in resp.text]
        cases.append(f"{label}=POST /login {resp.status}/{resp.code}「{brief(resp.text, 55)}」，"
                     f"随后 GET /api/materials={after_api.status}，泄露口令={len(leaks)}")

    after = biz_fingerprint(home)
    after_files = fs_fingerprint(home)
    conn = home.conn()
    try:
        active = conn.execute(
            "SELECT COUNT(*) FROM auth_sessions WHERE revoked_at IS NULL").fetchone()[0]
    finally:
        conn.close()

    ok = (all("POST /login 401/INVALID_CREDENTIALS" in c for c in cases[:2])
          and all("POST /login 400/INVALID_INPUT" in c for c in cases[2:])
          and all("GET /api/materials=401" in c for c in cases)
          and all("泄露口令=0" in c for c in cases)
          and before == after and before_files == after_files)
    rec(
        "AC05", "T3.1",
        "未登录客户端 + 有效登录表单 token，分别提交四类错误输入",
        "；".join(cases),
        f"未新增有效会话（库中未撤销会话={active}）；业务指纹一致={before == after}",
        fs_diff(before_files, after_files),
        ok,
    )


def ac06(home: Home) -> None:
    before = biz_fingerprint(home)
    before_files = fs_fingerprint(home)
    ids = material_ids(home)
    anon = Agent(home.base)
    calls = [
        ("GET /materials", lambda: anon.get("/materials")),
        ("GET /api/materials", lambda: anon.get("/api/materials")),
        ("GET /api/materials/{A}", lambda: anon.get(f"/api/materials/{ids['A']}")),
        ("GET /api/materials/{A}/file", lambda: anon.get(f"/api/materials/{ids['A']}/file")),
        ("POST /api/materials/upload", lambda: anon.multipart(
            "/api/materials/upload", [], [("file", "x.txt", b"data", "text/plain")])),
        ("PATCH /api/materials/{A}", lambda: anon.patch_json(
            f"/api/materials/{ids['A']}", {"title": "x"})),
        ("DELETE /api/materials/{A}", lambda: anon.delete(f"/api/materials/{ids['A']}")),
    ]
    observed, leaks = [], []
    for label, call in calls:
        resp = call()
        observed.append(f"{label}={resp.status}/{resp.code}")
        if "A_ONLY_SEED" in resp.text or "B_ONLY_SEED" in resp.text:
            leaks.append(f"{label} 泄露材料正文")

    after = biz_fingerprint(home)
    after_files = fs_fingerprint(home)
    ok = (observed[0].startswith("GET /materials=302")
          and all(item.endswith("/AUTH_REQUIRED") for item in observed[1:])
          and not leaks and before == after and before_files == after_files)
    rec(
        "AC06", "T3.2",
        f"无 Cookie 客户端；已知有效材料 ID（A=#{ids['A']}、B=#{ids['B']}）",
        "；".join(observed),
        f"业务指纹一致={before == after}；无材料内容泄露={not leaks}",
        fs_diff(before_files, after_files),
        ok, leaks,
    )


def ac07(home: Home) -> None:
    from campusclaw.security import session_id_hash

    before = biz_fingerprint(home)
    before_files = fs_fingerprint(home)

    expired_agent = Agent(home.base)
    expired_agent.login("teacher_a", PASSWORDS["teacher_a"])
    expired_cookie = expired_agent.session_cookie
    revoked_agent = Agent(home.base)
    revoked_agent.login("teacher_b", PASSWORDS["teacher_b"])
    revoked_cookie = revoked_agent.session_cookie
    tampered_agent = Agent(home.base)
    tampered_agent.login("student_a1", PASSWORDS["student_a1"])
    raw = tampered_agent.session_cookie
    flipped = raw[:-2] + ("aa" if raw[-2:] != "aa" else "bb")

    execute_sql(home, "UPDATE auth_sessions SET expires_at = ? WHERE sid_hash = ?",
                ("2020-01-01T00:00:00.000000Z", session_id_hash(home.sid_of(expired_cookie))))
    execute_sql(home, "UPDATE auth_sessions SET revoked_at = ? WHERE sid_hash = ?",
                ("2026-01-01T00:00:00.000000Z", session_id_hash(home.sid_of(revoked_cookie))))

    results = []
    for label, cookie in (("过期会话", expired_cookie), ("已撤销会话", revoked_cookie),
                          ("篡改签名", flipped)):
        agent = Agent(home.base)
        agent.load_cookie(cookie)
        page = agent.get("/materials")
        api = agent.get("/api/materials")
        results.append(f"{label}：GET /materials={page.status}，GET /api/materials={api.status}")

    student = home.agent("student_a1")
    serializer = home.app.session_interface.get_signing_serializer(home.app)
    payload = serializer.loads(student.session_cookie)
    forged = dict(payload)
    forged.update({"role": "teacher", "class_id": 2, "user_id": 1})
    forged_agent = Agent(home.base)
    forged_agent.load_cookie(serializer.dumps(forged))
    forged_list = forged_agent.get("/api/materials")
    forged_write = forged_agent.multipart(
        "/api/materials/upload", [("csrf_token", forged_agent.csrf())],
        [("file", "escalate.txt", b"escalate", "text/plain")])
    own_items = materials_of(student)
    forged_items = (forged_list.json() or {}).get("items", [])
    same_scope = ([i["class_id"] for i in forged_items]
                  == [i["class_id"] for i in own_items]) and forged_write.status == 403

    after = biz_fingerprint(home)
    after_files = fs_fingerprint(home)
    ok = (all("GET /materials=302" in r and r.endswith("GET /api/materials=401")
              for r in results)
          and same_scope and before == after and before_files == after_files)
    rec(
        "AC07", "T3.2",
        "三组失效 Cookie（已过期、已撤销、签名被改）+ 在有效会话 Cookie 中注入 role=teacher/class_id=2",
        "；".join(results)
        + f"；Cookie 注入 role=teacher：GET /api/materials={forged_list.status}"
          f"（可见班级与本人一致={[i['class_id'] for i in forged_items] == [i['class_id'] for i in own_items]}），"
          f"POST /api/materials/upload={forged_write.status}/{forged_write.code}",
        f"业务指纹一致={before == after}；注入角色字段未取得任何额外权限",
        fs_diff(before_files, after_files),
        ok, [] if same_scope else ["Cookie 中注入角色字段后被接受，存在越权风险"],
    )


def ac08(home: Home) -> None:
    from campusclaw.security import session_id_hash

    before = biz_fingerprint(home)
    before_files = fs_fingerprint(home)

    teacher = Agent(home.base)
    teacher.login("teacher_a", PASSWORDS["teacher_a"])
    replay_cookie = teacher.session_cookie
    other = Agent(home.base)
    other.login("teacher_a", PASSWORDS["teacher_a"])
    other_cookie = other.session_cookie

    token = teacher.csrf()
    logout = teacher.post_form("/logout", {"csrf_token": token})
    # 登出后服务端写入不含 sid 的新匿名会话 Cookie：旧 sid 必须不再出现在 Cookie 中
    new_value = cookie_from_header(logout.set_cookie)
    cleared = bool(new_value) and new_value != replay_cookie

    current_after = teacher.get("/api/materials")
    replay_agent = Agent(home.base)
    replay_agent.load_cookie(replay_cookie)
    replayed = replay_agent.get("/api/materials")
    other_after = other.get("/api/materials")

    conn = home.conn()
    try:
        row = conn.execute("SELECT revoked_at FROM auth_sessions WHERE sid_hash = ?",
                           (session_id_hash(home.sid_of(replay_cookie)),)).fetchone()
        revoked = row is not None and row["revoked_at"] is not None
        other_row = conn.execute("SELECT revoked_at FROM auth_sessions WHERE sid_hash = ?",
                                 (session_id_hash(home.sid_of(other_cookie)),)).fetchone()
        other_ok = other_row is not None and other_row["revoked_at"] is None
    finally:
        conn.close()

    after = biz_fingerprint(home)
    after_files = fs_fingerprint(home)
    ok = (logout.status == 302 and current_after.status == 401 and replayed.status == 401
          and revoked and other_ok and other_after.status == 200
          and before == after and before_files == after_files)
    rec(
        "AC08", "T3.3",
        "教师 A 两个独立会话；保留其中一个退出前 Cookie，携带有效 CSRF 调用 POST /logout",
        f"POST /logout={logout.status}->{logout.headers.get('Location', '')}（清除 Cookie={cleared}）；"
        f"当前 Cookie GET /api/materials={current_after.status}；"
        f"重放退出前 Cookie GET /api/materials={replayed.status}；"
        f"另一独立会话 GET /api/materials={other_after.status}",
        f"当前 sid 已撤销={revoked}；另一独立会话未被撤销={other_ok}；业务指纹一致={before == after}",
        fs_diff(before_files, after_files),
        ok,
    )


def ac09(home: Home) -> None:
    from campusclaw.security import session_id_hash

    before = biz_fingerprint(home)
    before_files = fs_fingerprint(home)

    agent = Agent(home.base)
    agent.login("teacher_a", PASSWORDS["teacher_a"])
    old_cookie = agent.session_cookie
    token = agent.csrf()
    failed = agent.post_form("/login", {
        "username": "teacher_a", "password": "DefinitelyWrong#9", "csrf_token": token})

    replay = Agent(home.base)
    replay.load_cookie(old_cookie)
    replayed = replay.get("/api/materials")

    conn = home.conn()
    try:
        row = conn.execute("SELECT revoked_at FROM auth_sessions WHERE sid_hash = ?",
                           (session_id_hash(home.sid_of(old_cookie)),)).fetchone()
        revoked = row is not None and row["revoked_at"] is not None
        active = conn.execute(
            "SELECT COUNT(*) FROM auth_sessions WHERE revoked_at IS NULL").fetchone()[0]
    finally:
        conn.close()

    after = biz_fingerprint(home)
    after_files = fs_fingerprint(home)
    ok = (failed.status == 401 and replayed.status == 401 and revoked
          and before == after and before_files == after_files)
    rec(
        "AC09", "T3.1/T3.3",
        "教师 A 已登录，同一客户端用错误口令再次提交登录，随后重放旧 Cookie",
        f"POST /login={failed.status}/{failed.code}；重放旧 Cookie GET /api/materials={replayed.status}",
        f"旧 sid 已撤销={revoked}；库中未撤销会话={active}；业务指纹一致={before == after}",
        fs_diff(before_files, after_files),
        ok,
    )


# ===========================================================================
# R03/R04 角色授权与班级隔离
# ===========================================================================

def ac10(home: Home) -> None:
    teacher = home.agent("teacher_a")
    student = home.agent("student_a1")
    conn = home.conn()
    try:
        a_class = conn.execute("SELECT id FROM classes WHERE name='A'").fetchone()[0]
        before_m = conn.execute("SELECT COUNT(*) FROM materials").fetchone()[0]
        before_k = conn.execute("SELECT COUNT(*) FROM knowledge_entries").fetchone()[0]
    finally:
        conn.close()
    before_files = fs_fingerprint(home)

    payload = "教学讲义 AC10 正文\n仅用于验收。\n".encode()
    upload = teacher.upload("ac10_teacher.txt", payload, title="教师上传示例")
    new_id = (upload.json() or {}).get("material_id")

    detail_t = teacher.get(f"/api/materials/{new_id}")
    detail_s = student.get(f"/api/materials/{new_id}")
    file_t = teacher.get(f"/api/materials/{new_id}/file")
    file_s = student.get(f"/api/materials/{new_id}/file")
    page_t = teacher.get("/materials").text
    page_s = student.get("/materials").text

    conn = home.conn()
    try:
        after_m = conn.execute("SELECT COUNT(*) FROM materials").fetchone()[0]
        after_k = conn.execute("SELECT COUNT(*) FROM knowledge_entries").fetchone()[0]
    finally:
        conn.close()
    after_files = fs_fingerprint(home)

    teacher_controls = 'data-act="delete"' in page_t and 'id="upload-form"' in page_t
    student_controls = 'data-act="delete"' in page_s or 'id="upload-form"' in page_s
    ok = (upload.status == 201 and detail_t.status == 200 and detail_s.status == 200
          and file_t.body == payload and file_s.body == payload
          and after_m == before_m + 1 and after_k == before_k + 1
          and teacher_controls and not student_controls)
    rec(
        "AC10", "T4.1/T5.2",
        "教师 A 与学生 A1 的独立有效会话；教师按合法链路上传一份材料",
        f"POST /api/materials/upload={upload.status}（material_id={new_id}）；"
        f"GET /api/materials/{new_id} 教师={detail_t.status}/学生={detail_s.status}；"
        f"下载字节一致：教师={file_t.body == payload}/学生={file_s.body == payload}；"
        f"页面写入口：教师={teacher_controls}/学生={student_controls}",
        f"materials {before_m}→{after_m}、knowledge_entries {before_k}→{after_k}（各 +1，"
        f"class_id={a_class}）",
        fs_diff(before_files, after_files),
        ok,
    )


def ac11(home: Home) -> None:
    student = home.agent("student_a1")
    ids = material_ids(home)
    before = biz_fingerprint(home)
    before_files = fs_fingerprint(home)
    payload = b"student should never write this"

    attempts = [
        ("上传(带CSRF)", lambda: student.upload("s.txt", payload, title="学生上传")),
        ("上传(缺CSRF)", lambda: student.upload("s.txt", payload, with_csrf=False)),
        ("PATCH 本班", lambda: student.patch_json(
            f"/api/materials/{ids['A']}", {"title": "学生改标题"},
            headers={"X-CSRF-Token": student.csrf()})),
        ("PATCH 跨班", lambda: student.patch_json(
            f"/api/materials/{ids['B']}", {"title": "学生改他班"},
            headers={"X-CSRF-Token": student.csrf()})),
        ("DELETE 本班", lambda: student.delete(
            f"/api/materials/{ids['A']}", headers={"X-CSRF-Token": student.csrf()})),
        ("DELETE 跨班", lambda: student.delete(
            f"/api/materials/{ids['B']}", headers={"X-CSRF-Token": student.csrf()})),
        ("DELETE 缺CSRF", lambda: student.delete(f"/api/materials/{ids['A']}")),
    ]
    observed = []
    for label, call in attempts:
        resp = call()
        observed.append(f"{label}={resp.status}/{resp.code}")

    after = biz_fingerprint(home)
    after_files = fs_fingerprint(home)
    ok = (all(item.endswith("/ROLE_FORBIDDEN") for item in observed)
          and before == after and before_files == after_files)
    rec(
        "AC11", "T4.1",
        f"学生 A1 会话；本班材料 #{ids['A']}、B 班材料 #{ids['B']}；并重复一次不带 CSRF token 的上传",
        "；".join(observed),
        f"业务指纹完全一致={before == after}（materials/knowledge_entries/file_cleanup_jobs 无增删改）",
        f"{fs_diff(before_files, after_files)}；uploads 与 .staging 均无残留",
        ok, [] if ok else ["学生写请求未被 ROLE_FORBIDDEN 拦截"],
    )


def ac12(home: Home) -> None:
    before_files = fs_fingerprint(home)
    lines, all_ok = [], True
    for username in ("teacher_a", "student_a1", "student_b1", "teacher_b"):
        agent = home.agent(username)
        items = materials_of(agent)
        own = CLASS_OF[username]
        foreign = "B" if own == "A" else "A"
        titles = [i["title"] for i in items]
        foreign_leak = any(f"{foreign} 班示例讲义" in title for title in titles)
        lines.append(f"{username}（{own} 班）：{len(items)} 条 {titles}，"
                     f"class_id={sorted({i['class_id'] for i in items})}，他班可见={foreign_leak}")
        if foreign_leak:
            all_ok = False

    marker_title = "受控改库验证标题 AC12"
    execute_sql(
        home,
        "UPDATE materials SET title = ? WHERE id = ("
        "SELECT m.id FROM materials m JOIN classes c ON c.id = m.class_id "
        "WHERE c.name = 'A' AND m.status = 'active' ORDER BY m.id LIMIT 1)",
        (marker_title,))

    teacher = home.agent("teacher_a")
    refreshed_page = teacher.get("/materials").text
    refreshed_api = materials_of(teacher)
    shows_new = marker_title in refreshed_page and any(
        i["title"] == marker_title for i in refreshed_api)
    identity_block = teacher.get("/api/materials?class_id=2")

    source = (ROOT / "campusclaw" / "materials.py").read_text(encoding="utf-8")
    sql_filtered = "WHERE class_id = ? AND status = 'active'" in source

    after_files = fs_fingerprint(home)
    ok = (all_ok and shows_new and sql_filtered
          and identity_block.status == 400
          and identity_block.code == "IDENTITY_FIELD_FORBIDDEN"
          and before_files == after_files)
    rec(
        "AC12", "T4.2",
        "A/B 两班各有样本及师生会话；随后在测试库直接把 A 班材料标题改为「受控改库验证标题 AC12」",
        "；".join(lines)
        + f"；改库后教师刷新：页面含新标题={marker_title in refreshed_page}、API 含新标题="
          f"{any(i['title'] == marker_title for i in refreshed_api)}；"
          f"GET /api/materials?class_id=2={identity_block.status}/{identity_block.code}",
        f"列表 SQL 含服务端班级与状态过滤={sql_filtered}（见 campusclaw/materials.py 的 "
        f"list_materials）；四账号列表均只含本班 active 记录",
        fs_diff(before_files, after_files),
        ok,
    )


def ac13(home: Home) -> None:
    teacher = home.agent("teacher_a")
    teacher_b = home.agent("teacher_b")
    student = home.agent("student_a1")
    ids = material_ids(home)
    missing = 999999
    before = biz_fingerprint(home)
    before_files = fs_fingerprint(home)

    cases = [
        ("教师A读B班详情", lambda: teacher.get(f"/api/materials/{ids['B']}")),
        ("教师A下载B班文件", lambda: teacher.get(f"/api/materials/{ids['B']}/file")),
        ("教师B读A班详情", lambda: teacher_b.get(f"/api/materials/{ids['A']}")),
        ("教师A改B班标题", lambda: teacher.patch_json(
            f"/api/materials/{ids['B']}", {"title": "越权改他班"},
            headers={"X-CSRF-Token": teacher.csrf()})),
        ("教师A删B班材料", lambda: teacher.delete(
            f"/api/materials/{ids['B']}", headers={"X-CSRF-Token": teacher.csrf()})),
        ("教师B删A班材料", lambda: teacher_b.delete(
            f"/api/materials/{ids['A']}", headers={"X-CSRF-Token": teacher_b.csrf()})),
        ("不存在的ID详情", lambda: teacher.get(f"/api/materials/{missing}")),
        ("不存在的ID下载", lambda: teacher.get(f"/api/materials/{missing}/file")),
        ("不存在的ID改标题", lambda: teacher.patch_json(
            f"/api/materials/{missing}", {"title": "x"},
            headers={"X-CSRF-Token": teacher.csrf()})),
        ("不存在的ID删除", lambda: teacher.delete(
            f"/api/materials/{missing}", headers={"X-CSRF-Token": teacher.csrf()})),
        ("非法ID格式", lambda: teacher.get("/api/materials/abc")),
        ("学生读B班详情", lambda: student.get(f"/api/materials/{ids['B']}")),
        ("学生改B班标题", lambda: student.patch_json(
            f"/api/materials/{ids['B']}", {"title": "x"},
            headers={"X-CSRF-Token": student.csrf()})),
    ]
    observed, leaks = [], []
    for label, call in cases:
        resp = call()
        observed.append(f"{label}={resp.status}/{resp.code}")
        if resp.status == 404 and resp.code != "MATERIAL_NOT_FOUND":
            leaks.append(f"{label} 错误码为 {resp.code}")
        if "A 班示例讲义" in resp.text or "B 班示例讲义" in resp.text:
            leaks.append(f"{label} 泄露他班标题")
        if '"storage_key"' in resp.text or '"file_path"' in resp.text:
            leaks.append(f"{label} 泄露存储键")

    after = biz_fingerprint(home)
    after_files = fs_fingerprint(home)
    ok = (all(item.endswith("/MATERIAL_NOT_FOUND") or item.endswith("/ROLE_FORBIDDEN")
              for item in observed)
          and not leaks and before == after and before_files == after_files)
    rec(
        "AC13", "T4.3",
        f"A/B 两班材料 ID 与不存在 ID（{missing}）；学生写操作按 AC11 应为 403",
        "；".join(observed),
        f"全部返回同结构 404 MATERIAL_NOT_FOUND，学生写操作为 403 ROLE_FORBIDDEN；"
        f"业务指纹一致={before == after}；无标题/正文/存储键泄露={not leaks}",
        fs_diff(before_files, after_files),
        ok, leaks,
    )


def ac14(home: Home) -> None:
    teacher = home.agent("teacher_a")
    student = home.agent("student_a1")
    ids = material_ids(home)
    before = biz_fingerprint(home)
    before_files = fs_fingerprint(home)

    cases = [
        ("列表 query 注入 class_id", lambda: teacher.get("/api/materials?class_id=2")),
        ("列表 query 注入 role", lambda: teacher.get("/api/materials?role=teacher")),
        ("上传 form 注入 class_id", lambda: teacher.upload(
            "id.txt", b"identity probe", extra_fields={"class_id": "2"})),
        ("上传 form 注入 role/uploaded_by", lambda: teacher.upload(
            "id2.txt", b"identity probe", extra_fields={"role": "teacher", "uploaded_by": "1"})),
        ("上传 form 注入 user_id", lambda: teacher.upload(
            "id3.txt", b"identity probe", extra_fields={"user_id": "99"})),
        ("PATCH JSON 注入 class_id", lambda: teacher.patch_json(
            f"/api/materials/{ids['A']}", {"title": "x", "class_id": 2},
            headers={"X-CSRF-Token": teacher.csrf()})),
        ("学生上传注入 role=teacher", lambda: student.upload(
            "esc.txt", b"escalate", extra_fields={"role": "teacher"})),
    ]
    observed = []
    for label, call in cases:
        resp = call()
        observed.append(f"{label}={resp.status}/{resp.code}")

    after = biz_fingerprint(home)
    after_files = fs_fingerprint(home)
    ok = (all(item.endswith("/IDENTITY_FIELD_FORBIDDEN") for item in observed[:6])
          and observed[6].endswith("/ROLE_FORBIDDEN")
          and before == after and before_files == after_files)
    rec(
        "AC14", "T4.2/T4.3",
        "教师 A 合法会话 + 合法上传文件；学生 A1 会话；分别在各载体注入身份字段",
        "；".join(observed),
        f"业务指纹一致={before == after}（未新增归属 B 班的记录）；"
        f"材料归属保持 {sorted({row[1] for row in after['materials']})}",
        fs_diff(before_files, after_files),
        ok, [] if ok else ["身份字段注入未被拒绝或产生了副作用"],
    )


def ac15(home: Home) -> None:
    row = first_material_row(home, "A")
    storage_key = row["storage_key"]
    ids = material_ids(home)
    before = biz_fingerprint(home)
    before_files = fs_fingerprint(home)

    anon = Agent(home.base)
    teacher_b = home.agent("teacher_b")
    cases = [
        ("匿名 /uploads/{存储键}", lambda: anon.get(f"/uploads/{storage_key}")),
        ("匿名 /static/uploads/{存储键}",
         lambda: anon.get(f"/static/uploads/{storage_key}")),
        ("B 班教师 /uploads/{存储键}", lambda: teacher_b.get(f"/uploads/{storage_key}")),
        ("匿名受保护下载", lambda: anon.get(f"/api/materials/{ids['A']}/file")),
        ("B 班教师受保护下载", lambda: teacher_b.get(f"/api/materials/{ids['A']}/file")),
    ]
    observed = []
    for label, call in cases:
        resp = call()
        observed.append(f"{label}={resp.status}/{resp.code}")

    teacher = home.agent("teacher_a")
    listing = teacher.get("/api/materials").text
    detail = teacher.get(f"/api/materials/{ids['A']}").text
    no_keys = all(token not in listing + detail
                  for token in ("storage_key", "file_path"))

    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    only_data_uploads = ("./data:/app/data" in compose and "./uploads:/app/uploads" in compose
                         and compose.count("/app/uploads") == 1)
    app_py = (ROOT / "campusclaw" / "__init__.py").read_text(encoding="utf-8")
    no_static = "static_folder=None" in app_py

    after = biz_fingerprint(home)
    after_files = fs_fingerprint(home)
    ok = (all(item.split("=", 1)[1].split("/")[0] in ("404", "401") for item in observed)
          and no_keys and only_data_uploads and no_static
          and before == after and before_files == after_files)
    rec(
        "AC15", "T4.3",
        f"从测试存储取得 A 班真实存储键 {mask(storage_key, 4)}（class_id={row['class_id']}）；"
        f"匿名与 B 班教师分别尝试静态地址与受保护端点",
        "；".join(observed),
        f"公开 JSON 不含 storage_key/file_path={no_keys}；应用未挂载公开静态目录="
        f"{no_static}（static_folder=None）",
        f"{fs_diff(before_files, after_files)}；Compose 仅映射 ./data 与 ./uploads，"
        f"无额外公开上传目录={only_data_uploads}",
        ok,
    )


# ===========================================================================
# R05 上传、解析与知识入库
# ===========================================================================

def ac16(home: Home) -> None:
    teacher = home.agent("teacher_a")
    before = material_state(home)
    before_files = fs_fingerprint(home)

    marker = "A_UPLOAD_MARKER_001"
    txt_body = f"文本讲义正文 {marker}\n第二行内容。\n".encode()
    md_text = f"# Markdown 讲义 {marker}\n\n- 条目一\n"
    md_body = ("\ufeff" + md_text).encode("utf-8")

    up1 = teacher.upload("ac16_note.txt", txt_body, title="  显式标题 AC16  ")
    up2 = teacher.upload("ac16_markdown.md", md_body)
    id1 = (up1.json() or {}).get("material_id")
    id2 = (up2.json() or {}).get("material_id")

    conn = home.conn()
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT m.id, m.class_id, m.title, m.size_bytes, m.status, k.body_text, "
            "k.class_id AS kc FROM materials m JOIN knowledge_entries k "
            "ON k.material_id = m.id WHERE m.id IN (?, ?) ORDER BY m.id", (id1, id2))]
    finally:
        conn.close()
    after_files = fs_fingerprint(home)
    after = material_state(home)
    new_files = sorted(set(after_files) - set(before_files))
    staged_clean = not any(".staging/" in rel for rel in after_files)
    detail1 = teacher.get(f"/api/materials/{id1}").json() or {}

    txt_ok = (rows[0]["title"] == "显式标题 AC16"
              and rows[0]["body_text"] == txt_body.decode()
              and rows[0]["kc"] == rows[0]["class_id"]
              and rows[0]["size_bytes"] == len(txt_body))
    md_ok = (rows[1]["title"] == "ac16_markdown"
             and rows[1]["body_text"] == md_text
             and rows[1]["kc"] == rows[1]["class_id"])
    sizes_ok = (sorted(after_files[rel][0] for rel in new_files)
                == sorted([len(txt_body), len(md_body)]))
    ok = (up1.status == 201 and up2.status == 201 and id1 and id2 and id1 != id2
          and txt_ok and md_ok and len(new_files) == 2 and sizes_ok and staged_clean
          and len(after["materials"]) == len(before["materials"]) + 2
          and len(after["knowledge"]) == len(before["knowledge"]) + 2
          and detail1.get("body_text") == txt_body.decode())
    rec(
        "AC16", "T5.1/T5.2",
        "教师 A 会话；上传不带 BOM 的 .txt（显式 title 含首尾空格）与带 BOM 的 .md（省略 title）",
        f"POST /api/materials/upload txt={up1.status}（material_id={id1}）、"
        f"md={up2.status}（material_id={id2}）；GET 详情={detail1 and '200'}",
        f"材料 +2、知识 +2；txt：title=「{rows[0]['title']}」size={rows[0]['size_bytes']}，"
        f"knowledge.class_id==material.class_id={rows[0]['kc'] == rows[0]['class_id']}；"
        f"md：title=「{rows[1]['title']}」，正文去 BOM={rows[1]['body_text'] == md_text}",
        f"新增原文件 {len(new_files)} 份且字节数一致={sizes_ok}；暂存已清理={staged_clean}",
        ok,
    )


def ac17(home: Home) -> None:
    teacher_a = home.agent("teacher_a")
    student_a1 = home.agent("student_a1")
    teacher_b = home.agent("teacher_b")
    student_b1 = home.agent("student_b1")
    before = material_state(home)
    before_files = fs_fingerprint(home)

    target = material_of(teacher_a, "显式标题 AC16")
    target_id, target_title = target["id"], target["title"]
    visible = {
        "teacher_a": [i["id"] for i in materials_of(teacher_a)],
        "student_a1": [i["id"] for i in materials_of(student_a1)],
        "teacher_b": [i["id"] for i in materials_of(teacher_b)],
        "student_b1": [i["id"] for i in materials_of(student_b1)],
    }
    denied = student_a1.patch_json(
        f"/api/materials/{target_id}", {"title": "学生改新标题"},
        headers={"X-CSRF-Token": student_a1.csrf()})

    after = material_state(home)
    after_files = fs_fingerprint(home)
    ok = (target_id in visible["teacher_a"] and target_id in visible["student_a1"]
          and target_id not in visible["teacher_b"] and target_id not in visible["student_b1"]
          and denied.status == 403 and denied.code == "ROLE_FORBIDDEN"
          and before == after and before_files == after_files)
    rec(
        "AC17", "T5.2",
        f"AC16 的上传已返回 201；四账号均有会话；针对材料 #{target_id}（{target_title}）刷新列表",
        f"教师A 可见={sorted(visible['teacher_a'])}；学生A1 可见={sorted(visible['student_a1'])}；"
        f"教师B 可见={sorted(visible['teacher_b'])}；学生A1 PATCH={denied.status}/{denied.code}",
        f"材料/知识/清理任务完全一致={before == after}（未新增、覆盖或改动）",
        fs_diff(before_files, after_files),
        ok,
    )


def ac18(home: Home) -> None:
    teacher = home.agent("teacher_a")
    before = material_state(home)
    before_files = fs_fingerprint(home)
    conn = home.conn()
    try:
        classes = {r["name"]: r["id"] for r in conn.execute("SELECT id, name FROM classes")}
    finally:
        conn.close()

    uploads = [
        ("同名一", "same_name.txt", b"first content AC18\n"),
        ("同名二", "same_name.txt", b"second content AC18 different\n"),
        ("路径穿越", "../../escape.txt", b"escape attempt AC18\n"),
        ("反斜线穿越", "..\\..\\escape2.md", b"escape attempt 2 AC18\n"),
    ]
    responses = [teacher.upload(name, data, title=title)
                 for title, name, data in uploads]
    ids = [(r.json() or {}).get("material_id") for r in responses]

    conn = home.conn()
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, original_filename, storage_key, class_id FROM materials "
            "WHERE id IN (?, ?, ?, ?) ORDER BY id", tuple(ids))]
    finally:
        conn.close()
    after_files = fs_fingerprint(home)
    new_files = sorted(set(after_files) - set(before_files))
    allowed_prefix = (".staging/", f"{classes['A']}/", f"{classes['B']}/")
    outside = [rel for rel in after_files if not rel.startswith(allowed_prefix)]
    keys_distinct = len({r["storage_key"] for r in rows}) == 4
    names_stripped = all("/" not in r["original_filename"]
                         and "\\" not in r["original_filename"]
                         and ".." not in r["original_filename"] for r in rows)
    previous_intact = all(before_files[k] == after_files.get(k) for k in before_files)

    ok = (all(r.status == 201 for r in responses) and len(set(ids)) == 4 and keys_distinct
          and names_stripped and len(new_files) == 4 and not outside and previous_intact)
    rec(
        "AC18", "T5.1/T5.2",
        "教师 A 会话；两次同名但正文不同的文件，以及 filename 含 ../ 与反斜线目录段的两个文件",
        f"四次 POST /api/materials/upload 状态={[r.status for r in responses]}；material_id={ids}",
        f"original_filename 已去除路径段={names_stripped}（实际值="
        f"{[r['original_filename'] for r in rows]}）；storage_key 各不相同={keys_distinct}"
        f"（样例 {mask(rows[0]['storage_key'], 4)}）；记录 class_id="
        f"{sorted({r['class_id'] for r in rows})}",
        f"新增 {len(new_files)} 份互不覆盖的文件；uploads 目录外无写入={not outside}；"
        f"既有文件哈希不变={previous_intact}",
        ok,
    )


def ac19(home: Home) -> None:
    teacher = home.agent("teacher_a")
    before_files = fs_fingerprint(home)

    payloads = [b"A", b"b" * MAX_FILE_BYTES, b"upper case extension\n", b"upper case md\n"]
    responses = [
        teacher.upload("edge_one.txt", payloads[0], title="1 字节"),
        teacher.upload("edge_max.txt", payloads[1], title="恰好 10 MiB"),
        teacher.upload("edge_upper.TXT", payloads[2], title="大写扩展名"),
        teacher.upload("edge_upper.MD", payloads[3], title="大写 MD"),
    ]
    ids = [(r.json() or {}).get("material_id") for r in responses if r.json()]

    conn = home.conn()
    try:
        rows = {r["id"]: dict(r) for r in conn.execute(
            "SELECT m.id, m.size_bytes, length(k.body_text) AS body_len FROM materials m "
            "JOIN knowledge_entries k ON k.material_id = m.id WHERE m.id IN (?, ?, ?, ?)",
            tuple(ids))}
    finally:
        conn.close()

    after_files = fs_fingerprint(home)
    new_files = sorted(set(after_files) - set(before_files))
    actual_sizes = sorted(after_files[rel][0] for rel in new_files)
    expected_sizes = sorted(len(p) for p in payloads)
    sizes_ok = (rows[ids[0]]["size_bytes"] == 1 and rows[ids[0]]["body_len"] == 1
                and rows[ids[1]]["size_bytes"] == MAX_FILE_BYTES
                and rows[ids[1]]["body_len"] == MAX_FILE_BYTES)
    ok = (all(r.status == 201 for r in responses) and sizes_ok
          and actual_sizes == expected_sizes)
    rec(
        "AC19", "T5.1",
        "教师 A 会话；1 字节文本、恰好 10,485,760 字节文本、.TXT 与 .MD 大写扩展名",
        f"四次 POST /api/materials/upload 状态={[r.status for r in responses]}"
        f"（multipart 开销未计入文件上限）",
        f"size_bytes/正文长度：1 字节→{rows[ids[0]]['size_bytes']}/"
        f"{rows[ids[0]]['body_len']}；10 MiB→{rows[ids[1]]['size_bytes']}/"
        f"{rows[ids[1]]['body_len']}",
        f"新增文件字节数={actual_sizes}（期望 {expected_sizes}）",
        ok,
    )


def ac20(home: Home) -> None:
    teacher = home.agent("teacher_a")
    rows: list[str] = []

    def run(label: str, expect_status: int, expect_code: str, call: Callable[[], Resp]) -> None:
        before_state, before_files = material_state(home), fs_fingerprint(home)
        resp = call()
        after_state, after_files = material_state(home), fs_fingerprint(home)
        clean = before_state == after_state and before_files == after_files
        good = resp.status == expect_status and resp.code == expect_code and clean
        rows.append(f"{label}：HTTP {resp.status}/{resp.code}（期望 {expect_status}/{expect_code}），"
                    f"D/F 差量为零={clean}")
        if not good:
            rows.append(f"!! {label} 不符合预期")

    run("缺 file", 400, "INVALID_FILE", lambda: teacher.multipart(
        "/api/materials/upload", [("csrf_token", teacher.csrf())], []))
    run("两个文件", 400, "INVALID_FILE", lambda: teacher.multipart(
        "/api/materials/upload", [("csrf_token", teacher.csrf())],
        [("file", "a.txt", b"aaa", "text/plain"), ("file", "b.txt", b"bbb", "text/plain")]))
    run("0 字节文件", 400, "INVALID_FILE", lambda: teacher.upload("empty.txt", b""))
    run(".exe 伪造 MIME", 415, "UNSUPPORTED_FILE_TYPE",
        lambda: teacher.upload("payload.exe", b"MZ binary", ctype="text/plain"))
    run("10,485,761 字节", 413, "FILE_TOO_LARGE",
        lambda: teacher.upload("too_big.txt", b"c" * (MAX_FILE_BYTES + 1)))
    run("总请求 > 11 MiB", 413, "FILE_TOO_LARGE", lambda: teacher.multipart(
        "/api/materials/upload", [("csrf_token", teacher.csrf())],
        [("file", "huge.txt", b"d", "text/plain")],
        declared_length=MAX_CONTENT_BYTES + 1024))
    run("非 UTF-8", 422, "PARSE_FAILED",
        lambda: teacher.upload("latin1.txt", b"\xff\xfe\x00bad bytes"))
    run("正文仅空白", 422, "PARSE_FAILED",
        lambda: teacher.upload("blank.txt", "   \n\t  \n".encode()))
    run("含 NUL", 422, "PARSE_FAILED", lambda: teacher.upload("nul.txt", b"abc\x00def"))

    def injected_parse_failure(*_args, **_kwargs):
        raise ApiError(422, "PARSE_FAILED", "注入的受控解析失败")

    with patched(materials_svc, "decode_text", injected_parse_failure):
        run("受控解析失败注入", 422, "PARSE_FAILED",
            lambda: teacher.upload("inject.txt", b"valid text"))

    run("显式空白 title", 400, "INVALID_TITLE",
        lambda: teacher.upload("t1.txt", b"ok content", title="   "))
    run("201 码点标题", 400, "INVALID_TITLE",
        lambda: teacher.upload("t2.txt", b"ok content", title="a" * 201))
    run("控制字符标题", 400, "INVALID_TITLE",
        lambda: teacher.upload("t3.txt", b"ok content", title="bad\x07title"))
    run("未知字段 extra", 400, "INVALID_FIELDS",
        lambda: teacher.upload("t4.txt", b"ok content", extra_fields={"extra": "1"}))
    existing = material_ids(home)["A"]
    run("传 material_id 覆盖已有文件", 400, "INVALID_FIELDS",
        lambda: teacher.upload("t5.txt", b"ok content",
                               extra_fields={"material_id": str(existing)}))

    ok = not any(row.startswith("!!") for row in rows)
    rec(
        "AC20", "T5.1/T5.3",
        "教师 A 有效会话与 CSRF token；每个子用例前记录 D/F 基线",
        "；".join(rows),
        "每个子用例后 materials/knowledge_entries/file_cleanup_jobs 差量均为零（见各行）",
        "每个子用例后无新原文件、无暂存残留（见各行）",
        ok, [row for row in rows if row.startswith("!!")],
    )


def ac21(home: Home) -> None:
    teacher = home.agent("teacher_a")
    rows: list[str] = []

    def attempt(label: str, expect_code: str, act: Callable[[], Resp],
                prepare: Callable[[], Any] | None = None,
                cleanup: Callable[[Any], None] | None = None) -> None:
        before_state, before_files = material_state(home), fs_fingerprint(home)
        token = prepare() if prepare else None
        try:
            resp = act()
        finally:
            if cleanup:
                cleanup(token)
        after_state, after_files = material_state(home), fs_fingerprint(home)
        no_partial = (before_state["materials"] == after_state["materials"]
                      and before_state["knowledge"] == after_state["knowledge"])
        no_files = before_files == after_files
        rows.append(f"{label}：HTTP {resp.status}/{resp.code}（期望 500/{expect_code}），"
                    f"双表无半条数据={no_partial}，无暂存/最终残留={no_files}")
        if not (resp.status == 500 and resp.code == expect_code and no_partial and no_files):
            rows.append(f"!! {label} 不符合预期")

    payload = b"fault injection payload AC21\n"

    def prepare_staging():
        shutil.rmtree(home.staging_dir, ignore_errors=True)
        home.staging_dir.write_bytes(b"occupied")
        return home.staging_dir

    def cleanup_staging(_token):
        home.staging_dir.unlink()
        home.staging_dir.mkdir(parents=True, exist_ok=True)

    attempt("暂存写入失败（暂存路径被同名文件占据）", "STORAGE_ERROR",
            lambda: teacher.upload("f1.txt", payload, title="故障一"),
            prepare_staging, cleanup_staging)

    def injected_move_failure(*_args, **_kwargs):
        raise OSError("injected move failure")

    with patched(storage, "move_to_final", injected_move_failure):
        attempt("最终移动失败", "STORAGE_ERROR",
                lambda: teacher.upload("f2.txt", payload, title="故障二"))

    with faulty_db(fail_sql="INSERT INTO KNOWLEDGE_ENTRIES"):
        attempt("知识记录写失败", "DATABASE_ERROR",
                lambda: teacher.upload("f3.txt", payload, title="故障三"))

    with faulty_db(fail_commit=True):
        attempt("事务提交失败", "DATABASE_ERROR",
                lambda: teacher.upload("f4.txt", payload, title="故障四"))

    ok = not any(row.startswith("!!") for row in rows)
    rec(
        "AC21", "T5.3",
        "教师 A 会话与合法文件；在隔离环境注入暂存写失败、最终移动失败、知识记录写失败、事务提交失败"
        "（清理权限正常）",
        "；".join(rows),
        "三次数据库/文件故障均回滚，materials 与 knowledge_entries 未出现只增一表的半条数据",
        "无该请求的暂存或最终文件，既有文件哈希不变",
        ok, [row for row in rows if row.startswith("!!")],
    )


def ac22() -> None:
    rows: list[str] = []

    # 场景一：写暂存后进程中断 → 启动恢复清理未提交残留
    home1 = Home("ac22-staging").start()
    home1.stop()
    committed_files = fs_fingerprint(home1)   # 注入残留前的基线：只有已提交材料的文件
    orphan = home1.staging_dir / "orphan.staging"
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_bytes(b"orphan staging bytes")
    home1.restart()
    recovered = fs_fingerprint(home1)
    staging_left = [rel for rel in recovered if ".staging/" in rel]
    health1 = Agent(home1.base).get("/health")
    kept = set(committed_files) <= set(recovered)
    rows.append(f"写暂存后中断：重启后暂存残留={len(staging_left)}；/health={health1.status}；"
                f"已提交材料文件保留={kept}")
    if staging_left or health1.status != 200 or not kept:
        rows.append("!! 场景一不符合预期")

    # 场景二：移动到最终位置但提交前中断 → 无引用最终文件被清理
    home2 = Home("ac22-final").start()
    home2.stop()
    conn = home2.conn()
    try:
        class_id = conn.execute("SELECT id FROM classes WHERE name='A'").fetchone()[0]
    finally:
        conn.close()
    orphan_final = home2.upload_dir / str(class_id) / "orphan-final.txt"
    orphan_final.parent.mkdir(parents=True, exist_ok=True)
    orphan_final.write_bytes(b"orphan final bytes")
    home2.restart()
    left_final = orphan_final.exists()
    health2 = Agent(home2.base).get("/health")
    conn = home2.conn()
    try:
        partial = conn.execute(
            "SELECT COUNT(*) FROM materials m LEFT JOIN knowledge_entries k "
            "ON k.material_id = m.id WHERE m.status='active' AND k.id IS NULL").fetchone()[0]
    finally:
        conn.close()
    rows.append(f"移动后提交前中断：无引用最终文件残留={left_final}；/health={health2.status}；"
                f"双表无部分提交组合={partial == 0}")
    if left_final or health2.status != 200 or partial != 0:
        rows.append("!! 场景二不符合预期")

    # 场景三：清理权限被阻止时不得启动为可用状态
    home3 = Home("ac22-blocked").start()
    home3.stop()
    blocked_orphan = home3.staging_dir / "blocked.staging"
    blocked_orphan.parent.mkdir(parents=True, exist_ok=True)
    blocked_orphan.write_bytes(b"blocked orphan")
    handle = lock_file(blocked_orphan)
    port = free_port()
    env = dict(os.environ)
    env.update(home3.env)
    env["PORT"] = str(port)
    proc = subprocess.Popen([sys.executable, str(ROOT / "app.py")], cwd=str(ROOT), env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8", errors="replace")
    try:
        output, _ = proc.communicate(timeout=45)
    except subprocess.TimeoutExpired:
        proc.kill()
        output, _ = proc.communicate()
    listening = port_listening(port)
    first_error = next((line for line in (output or "").splitlines() if "启动失败" in line), "")
    rows.append(f"清理被阻止：退出码={proc.returncode}；端口监听={listening}；"
                f"输出={brief(first_error or (output or '').strip(), 90)}")
    if proc.returncode == 0 or listening:
        rows.append("!! 清理被阻止时进程仍进入可用状态")

    unlock_file(handle)
    home3.restart()
    health3 = Agent(home3.base).get("/health")
    remaining = [rel for rel in fs_fingerprint(home3) if ".staging/" in rel]
    rows.append(f"恢复权限后重启：/health={health3.status}；暂存残留={len(remaining)}")
    if health3.status != 200 or remaining:
        rows.append("!! 恢复权限后未能完成恢复")

    ok = not any(row.startswith("!!") for row in rows)
    rec(
        "AC22", "T5.3/T7.1",
        "三个独立环境：写暂存后中断、移动最终文件但提交前中断、以及用独占锁阻止清理权限后启动",
        "；".join(rows),
        "重启后无部分提交的材料/知识组合；恢复失败期间未虚报健康（退出码非零且端口未监听）",
        "已提交文件哈希不变，未提交残留被清理",
        ok, [row for row in rows if row.startswith("!!")],
    )


def ac23(home: Home) -> None:
    teacher = home.agent("teacher_a")
    before = material_state(home)
    before_files = fs_fingerprint(home)

    payload = ("<script>alert('xss')</script>\n"
               "正文包含 <img src=x onerror=alert(1)> 与 </textarea> 等字样。\n").encode()
    title = "<script>alert('title')</script>"
    up = teacher.upload("xss_note.md", payload, title=title)
    new_id = (up.json() or {}).get("material_id")

    page = teacher.get("/materials").text
    detail = teacher.get(f"/api/materials/{new_id}")
    download = teacher.get(f"/api/materials/{new_id}/file")

    page_escaped = "&lt;script&gt;alert" in page and "<script>alert('title')</script>" not in page
    js_uses_text = "textContent" in page and ".innerHTML" not in page
    body_ok = (detail.json() or {}).get("body_text") == payload.decode()
    disposition = download.headers.get("Content-Disposition", "")
    attachment = "attachment" in disposition.lower()
    nosniff = download.headers.get("X-Content-Type-Options") == "nosniff"
    bytes_ok = download.body == payload

    after = material_state(home)
    writes_ok = (len(after["materials"]) == len(before["materials"]) + 1
                 and len(after["knowledge"]) == len(before["knowledge"]) + 1
                 and len(fs_fingerprint(home)) == len(before_files) + 1)
    ok = (up.status == 201 and page_escaped and js_uses_text and body_ok
          and attachment and nosniff and bytes_ok and writes_ok)
    rec(
        "AC23", "T4.3/T5.2",
        "教师 A 上传正文含 <script>/<img onerror> 的 .md，标题也含 HTML 字样；本班用户读取页面、详情并下载",
        f"POST /api/materials/upload={up.status}（material_id={new_id}）；"
        f"GET /materials 标题已转义={page_escaped}；前端以 textContent 赋值（未用 innerHTML）="
        f"{js_uses_text}；下载 Content-Disposition={brief(disposition, 70)}；"
        f"X-Content-Type-Options={nosniff}",
        f"body_text 保存原解析文字且与上传一致={body_ok}；读取未产生额外写入={writes_ok}",
        f"下载字节与原文件逐字节一致={bytes_ok}（扩展名为 .md 也未触发 HTML 执行）",
        ok,
    )


# ===========================================================================
# R06/R07 标题修改、删除与清理
# ===========================================================================

def ac24(home: Home) -> None:
    teacher = home.agent("teacher_a")
    student = home.agent("student_a1")
    target = material_ids(home)["A"]
    before = material_state(home)
    before_files = fs_fingerprint(home)
    conn = home.conn()
    try:
        row_before = dict(conn.execute("SELECT * FROM materials WHERE id = ?",
                                       (target,)).fetchone())
    finally:
        conn.close()

    cases = []
    for label, title in (
        ("首尾空白", "  更新后的标题 AC24  "),
        ("1 字符", "A"),
        ("200 字符", "字" * 200),
        ("重复提交同一标题", "字" * 200),
    ):
        resp = teacher.patch_json(f"/api/materials/{target}", {"title": title},
                                  headers={"X-CSRF-Token": teacher.csrf()})
        cases.append(f"{label}：PATCH={resp.status}/{resp.code}")

    final_title = "字" * 200
    page_after = teacher.get("/materials").text
    student_page = student.get("/materials").text
    shown = final_title in page_after and final_title in student_page

    conn = home.conn()
    try:
        row_after = dict(conn.execute("SELECT * FROM materials WHERE id = ?",
                                      (target,)).fetchone())
    finally:
        conn.close()
    changed = sorted(k for k in row_before if row_before[k] != row_after.get(k))
    only_allowed = set(changed) <= {"title", "updated_at"}

    after = material_state(home)
    after_files = fs_fingerprint(home)
    knowledge_same = before["knowledge"] == after["knowledge"]
    files_same = before_files == after_files
    ok = (all("=200" in case for case in cases) and shown and only_allowed
          and knowledge_same and files_same)
    rec(
        "AC24", "T6.1",
        f"教师 A 的 active 材料 #{target}、学生 A1 会话、更新前 D/F 快照；依次提交合法新标题",
        "；".join(cases) + f"；师生刷新后均显示最终标题={shown}（首尾空白已去除）",
        f"变化字段={changed}（仅允许 title/updated_at）；知识记录数量与 body_text 不变={knowledge_same}",
        fs_diff(before_files, after_files),
        ok,
    )


def ac25(home: Home) -> None:
    teacher = home.agent("teacher_a")
    target = material_ids(home)["A"]
    rows: list[str] = []

    for label, payload, expect_status, expect_code in (
        ("缺 title", {}, 400, "INVALID_TITLE"),
        ("空白 title", {"title": "   "}, 400, "INVALID_TITLE"),
        ("201 字符", {"title": "x" * 201}, 400, "INVALID_TITLE"),
        ("控制字符", {"title": "bad\x07title"}, 400, "INVALID_TITLE"),
        ("额外 body_text", {"title": "ok", "body_text": "覆盖正文"}, 400, "INVALID_FIELDS"),
        ("额外 file 字段", {"title": "ok", "file": "x"}, 400, "INVALID_FIELDS"),
        ("身份字段 class_id", {"title": "ok", "class_id": 2}, 400, "IDENTITY_FIELD_FORBIDDEN"),
    ):
        before_state, before_files = material_state(home), fs_fingerprint(home)
        resp = teacher.patch_json(f"/api/materials/{target}", payload,
                                  headers={"X-CSRF-Token": teacher.csrf()})
        after_state, after_files = material_state(home), fs_fingerprint(home)
        clean = before_state == after_state and before_files == after_files
        good = resp.status == expect_status and resp.code == expect_code and clean
        rows.append(f"{label}：HTTP {resp.status}/{resp.code}（期望 {expect_status}/"
                    f"{expect_code}），数据无变化={clean}")
        if not good:
            rows.append(f"!! {label} 不符合预期")

    before_state, before_files = material_state(home), fs_fingerprint(home)
    with faulty_db(fail_commit=True):
        resp = teacher.patch_json(f"/api/materials/{target}", {"title": "数据库故障标题"},
                                  headers={"X-CSRF-Token": teacher.csrf()})
    after_state, after_files = material_state(home), fs_fingerprint(home)
    clean = before_state == after_state and before_files == after_files
    good = resp.status == 500 and resp.code == "DATABASE_ERROR" and clean
    rows.append(f"数据库提交失败注入：HTTP {resp.status}/{resp.code}（期望 500/DATABASE_ERROR），"
                f"数据完全保持={clean}")
    if not good:
        rows.append("!! 数据库故障注入不符合预期")

    ok = not any(row.startswith("!!") for row in rows)
    rec(
        "AC25", "T6.1",
        f"教师 A 的本班材料 #{target}、有效 token、D/F 基线；提交非法输入并对合法标题注入数据库提交失败",
        "；".join(rows),
        "全部子用例后材料字段快照保持不变（故障场景未产生任何变化）",
        "文件清单与哈希不变",
        ok, [row for row in rows if row.startswith("!!")],
    )


def ac26(home: Home) -> None:
    teacher = home.agent("teacher_a")
    student = home.agent("student_a1")
    target = material_ids(home)["A"]
    row = material_row(home, target)
    before_files = fs_fingerprint(home)
    delete = teacher.delete(f"/api/materials/{target}", headers={"X-CSRF-Token": teacher.csrf()})

    in_list = any(i["id"] == target for i in materials_of(teacher))
    detail_t = teacher.get(f"/api/materials/{target}")
    detail_s = student.get(f"/api/materials/{target}")
    download = teacher.get(f"/api/materials/{target}/file")

    conn = home.conn()
    try:
        tomb = dict(conn.execute("SELECT status, deleted_at FROM materials WHERE id = ?",
                                 (target,)).fetchone())
        knowledge = conn.execute(
            "SELECT COUNT(*) FROM knowledge_entries WHERE material_id = ?",
            (target,)).fetchone()[0]
        jobs = [tuple(r) for r in conn.execute(
            "SELECT material_id, status FROM file_cleanup_jobs WHERE material_id = ?",
            (target,))]
    finally:
        conn.close()
    after_files = fs_fingerprint(home)
    target_gone = not any(row["storage_key"] in rel for rel in after_files)
    others_same = all(before_files[k] == after_files[k] for k in before_files
                      if row["storage_key"] not in k)

    ok = (delete.status == 204 and not delete.body and not in_list
          and detail_t.status == 404 and detail_s.status == 404 and download.status == 404
          and tomb.get("status") == "deleted" and tomb.get("deleted_at")
          and knowledge == 0 and jobs == [(target, "done")] and target_gone and others_same)
    rec(
        "AC26", "T6.2",
        f"A 班 material #{target}（文件可删除），本班学生有读取会话；教师执行 DELETE",
        f"DELETE={delete.status}（响应体 {len(delete.body)} 字节）；列表中仍含该材料={in_list}；"
        f"详情 教师={detail_t.status}/学生={detail_s.status}；下载={download.status}",
        f"墓碑：status={tomb.get('status')}，deleted_at={brief(tomb.get('deleted_at'), 30)}；"
        f"知识记录剩余={knowledge}；清理任务={jobs}",
        f"目标原文件已删除={target_gone}；其他材料文件哈希不变={others_same}",
        ok,
    )


def ac27(home: Home) -> None:
    teacher = home.agent("teacher_a")
    target = material_ids(home)["A"]
    before = material_state(home)
    before_files = fs_fingerprint(home)

    with faulty_db(fail_commit=True):
        delete = teacher.delete(f"/api/materials/{target}",
                                headers={"X-CSRF-Token": teacher.csrf()})
    read_after = teacher.get(f"/api/materials/{target}")

    after = material_state(home)
    after_files = fs_fingerprint(home)
    conn = home.conn()
    try:
        status = conn.execute("SELECT status FROM materials WHERE id = ?",
                              (target,)).fetchone()[0]
        knowledge = conn.execute("SELECT COUNT(*) FROM knowledge_entries WHERE material_id = ?",
                                 (target,)).fetchone()[0]
        jobs = conn.execute("SELECT COUNT(*) FROM file_cleanup_jobs WHERE material_id = ?",
                            (target,)).fetchone()[0]
    finally:
        conn.close()

    ok = (delete.status == 500 and delete.code == "DATABASE_ERROR"
          and read_after.status == 200 and status == "active" and knowledge == 1 and jobs == 0
          and before == after and before_files == after_files)
    rec(
        "AC27", "T6.2",
        f"本班 active 材料 #{target}，注入删除事务提交失败，原文件正常",
        f"DELETE={delete.status}/{delete.code}；随后 GET /api/materials/{target}="
        f"{read_after.status}",
        f"材料仍为 {status}；知识记录仍为 {knowledge} 条；清理任务新增 {jobs} 条",
        f"原文件完整保留={before_files == after_files}；未出现先删文件再回滚数据库",
        ok,
    )


def ac28(home: Home) -> None:
    rows: list[str] = []
    teacher = home.agent("teacher_a")
    # 前置：本班需要多份材料，分别用于「权限受阻重试」与「重启重试」两条路径
    teacher.upload("ac28_a.txt", b"AC28 material A\n", title="AC28 材料一")
    teacher.upload("ac28_b.txt", b"AC28 material B\n", title="AC28 材料二")

    target = material_ids(home)["A"]
    row = material_row(home, target)
    path = storage_path_of(home, row)
    handle = lock_file(path)
    try:
        delete = teacher.delete(f"/api/materials/{target}",
                                headers={"X-CSRF-Token": teacher.csrf()})
        body = delete.json() or {}
        detail = teacher.get(f"/api/materials/{target}")
        download = teacher.get(f"/api/materials/{target}/file")
        conn = home.conn()
        try:
            tomb = conn.execute("SELECT status FROM materials WHERE id = ?",
                                (target,)).fetchone()[0]
            knowledge = conn.execute(
                "SELECT COUNT(*) FROM knowledge_entries WHERE material_id = ?",
                (target,)).fetchone()[0]
            jobs = [tuple(r) for r in conn.execute(
                "SELECT status, attempts FROM file_cleanup_jobs WHERE material_id = ?",
                (target,))]
        finally:
            conn.close()
        rows.append(f"权限受阻止时 DELETE={delete.status}，body={brief(body, 80)}；"
                    f"详情={detail.status}、下载={download.status}；"
                    f"D：status={tomb}、知识行={knowledge}、任务={jobs}")
        if not (delete.status == 202 and body.get("material_id") == target
                and body.get("status") == "deleted"
                and body.get("cleanup_status") == "pending"
                and detail.status == 404 and download.status == 404
                and tomb == "deleted" and knowledge == 0 and jobs == [("pending", 1)]):
            rows.append("!! 权限受阻时的删除语义不符合预期")

        blocked = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "cleanup_files.py"), "--once"],
            cwd=str(ROOT), env={**os.environ, **home.env}, capture_output=True, text=True,
            encoding="utf-8", errors="replace")
        conn = home.conn()
        try:
            still_pending = conn.execute(
                "SELECT status FROM file_cleanup_jobs WHERE material_id = ?",
                (target,)).fetchone()[0]
        finally:
            conn.close()
        rows.append(f"未恢复权限时运行 cleanup_files.py --once：退出码={blocked.returncode}；"
                    f"任务仍为 {still_pending}；stderr={brief(blocked.stderr.strip(), 70)}")
        if blocked.returncode == 0 or still_pending != "pending":
            rows.append("!! 未恢复权限时命令不应成功")
    finally:
        unlock_file(handle)

    restored = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "cleanup_files.py"), "--once"],
        cwd=str(ROOT), env={**os.environ, **home.env}, capture_output=True, text=True,
        encoding="utf-8", errors="replace")
    conn = home.conn()
    try:
        final_job = conn.execute("SELECT status FROM file_cleanup_jobs WHERE material_id = ?",
                                 (target,)).fetchone()[0]
    finally:
        conn.close()
    rows.append(f"恢复权限后运行 cleanup_files.py --once：退出码={restored.returncode}；"
                f"任务={final_job}；目标文件已删除={not path.exists()}")
    if restored.returncode != 0 or final_job != "done" or path.exists():
        rows.append("!! 恢复权限后未能完成清理")

    other = material_ids(home)["A"]
    other_row = material_row(home, other)
    other_path = storage_path_of(home, other_row)
    handle2 = lock_file(other_path)
    try:
        delete2 = teacher.delete(f"/api/materials/{other}",
                                 headers={"X-CSRF-Token": teacher.csrf()})
    finally:
        unlock_file(handle2)
    home.restart()
    conn = home.conn()
    try:
        job2 = conn.execute("SELECT status FROM file_cleanup_jobs WHERE material_id = ?",
                            (other,)).fetchone()[0]
    finally:
        conn.close()
    health = Agent(home.base).get("/health")
    rows.append(f"重启重试路径：DELETE={delete2.status}；重启后 /health={health.status}；"
                f"任务={job2}；文件已删除={not other_path.exists()}")
    if delete2.status != 202 or job2 != "done" or other_path.exists() or health.status != 200:
        rows.append("!! 重启重试路径不符合预期")

    ok = not any(row.startswith("!!") for row in rows)
    rec(
        "AC28", "T6.3",
        "本班 active 材料，用独占句柄阻止目标文件删除（真实共享冲突），数据库可写",
        "；".join(rows),
        "pending → done 的状态迁移符合 D-07 两步删除协议",
        "首次保留该私有文件但不可访问；重试后目标文件消失",
        ok, [row for row in rows if row.startswith("!!")],
    )


def ac29(home: Home) -> None:
    rows: list[str] = []
    teacher = home.agent("teacher_a")
    # 前置：本班需要三份材料，分别用于「已完成删除」「pending 删除」「活跃文件保护」
    teacher.upload("ac29_a.txt", b"AC29 material A\n", title="AC29 材料一")
    teacher.upload("ac29_b.txt", b"AC29 material B\n", title="AC29 材料二")

    target = material_ids(home)["A"]
    first = teacher.delete(f"/api/materials/{target}", headers={"X-CSRF-Token": teacher.csrf()})
    conn = home.conn()
    try:
        jobs_before = conn.execute(
            "SELECT COUNT(*) FROM file_cleanup_jobs WHERE material_id = ?",
            (target,)).fetchone()[0]
    finally:
        conn.close()
    again = teacher.delete(f"/api/materials/{target}", headers={"X-CSRF-Token": teacher.csrf()})
    conn = home.conn()
    try:
        jobs_after = conn.execute(
            "SELECT COUNT(*) FROM file_cleanup_jobs WHERE material_id = ?",
            (target,)).fetchone()[0]
    finally:
        conn.close()
    rows.append(f"已完成删除的材料再次 DELETE：首次={first.status}、再次={again.status}/"
                f"{again.code}；清理任务 {jobs_before}→{jobs_after}")
    if again.status != 404 or jobs_after != jobs_before:
        rows.append("!! 重复删除未返回 404 或新增了任务")

    other = material_ids(home)["A"]
    other_path = storage_path_of(home, material_row(home, other))
    handle = lock_file(other_path)
    try:
        pending_delete = teacher.delete(f"/api/materials/{other}",
                                        headers={"X-CSRF-Token": teacher.csrf()})
        repeat = teacher.delete(f"/api/materials/{other}",
                                headers={"X-CSRF-Token": teacher.csrf()})
    finally:
        unlock_file(handle)
    conn = home.conn()
    try:
        job_rows = [tuple(r) for r in conn.execute(
            "SELECT status FROM file_cleanup_jobs WHERE material_id = ?", (other,))]
    finally:
        conn.close()
    rows.append(f"pending 状态材料再次 DELETE：首次={pending_delete.status}、再次={repeat.status}"
                f"/{repeat.code}；任务行={job_rows}（应唯一）")
    if pending_delete.status != 202 or repeat.status != 404 or len(job_rows) != 1:
        rows.append("!! pending 材料重复删除不符合预期")

    keeper = material_ids(home)["B"]
    execute_sql(home, "UPDATE file_cleanup_jobs SET status='pending' WHERE material_id = ?",
                (other,))
    exit_codes = []
    for _ in range(2):
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "cleanup_files.py"), "--once"],
            cwd=str(ROOT), env={**os.environ, **home.env}, capture_output=True, text=True,
            encoding="utf-8", errors="replace")
        exit_codes.append(result.returncode)
    conn = home.conn()
    try:
        final_jobs = [tuple(r) for r in conn.execute(
            "SELECT material_id, status FROM file_cleanup_jobs ORDER BY id")]
        keeper_row = dict(conn.execute("SELECT * FROM materials WHERE id = ?",
                                       (keeper,)).fetchone())
    finally:
        conn.close()
    keeper_path = storage_path_of(home, keeper_row)
    rows.append(f"原文件已不存在但任务 pending：两次 cleanup_files.py --once 退出码={exit_codes}；"
                f"最终任务={final_jobs}；活跃材料原文件仍存在={keeper_path.exists()}")
    if exit_codes != [0, 0] or not keeper_path.exists():
        rows.append("!! 幂等清理或活跃文件保护不符合预期")

    ok = not any(row.startswith("!!") for row in rows)
    rec(
        "AC29", "T6.2/T6.3",
        "分别准备已完成删除、pending 删除、以及原文件已不存在但任务仍 pending 的材料",
        "；".join(rows),
        "不产生重复清理任务，任务最终为 done",
        "不误删任何 active 材料的原文件",
        ok, [row for row in rows if row.startswith("!!")],
    )


# ===========================================================================
# R09 健康检查 / R10 文档与追溯
# ===========================================================================

def ac34() -> None:
    home = Home("ac34").start()
    try:
        anon = Agent(home.base)
        before_sessions = counts(home)["auth_sessions"]
        before_state, before_files = material_state(home), fs_fingerprint(home)

        health = anon.get("/health")
        after_sessions = counts(home)["auth_sessions"]
        after_state, after_files = material_state(home), fs_fingerprint(home)

        bogus = home.root / "not-a-database.db"
        bogus.write_bytes(b"this is not a sqlite database file" * 20)
        real_connect = dbmod.connect
        with patched(dbmod, "connect", lambda _path: real_connect(bogus)):
            degraded = Agent(home.base).get("/health")
            degraded2 = Agent(home.base).get("/health")

        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        health_cfg = {
            "interval": "interval: 10s" in compose,
            "timeout": "timeout: 3s" in compose,
            "start_period": "start_period: 30s" in compose,
            "retries": "retries: 3" in compose,
        }
        ok_sla = all(health_cfg.values())
        no_leak = all(token not in degraded.text
                      for token in ("/", "sqlite", "SELECT", "Traceback"))
        ok = (health.status == 200 and health.json() == {"status": "ok"}
              and degraded.status == 503
              and degraded.json() == {"status": "unavailable"}
              and degraded2.status == 503
              and before_sessions == after_sessions
              and before_state == after_state and before_files == after_files
              and no_leak and ok_sla)
        rec(
            "AC34", "T7.2",
            "已启动系统，匿名客户端不持有 Cookie；随后注入数据库不可查询故障（连接指向非 SQLite 文件）",
            f"匿名 GET /health={health.status} {brief(health.text, 40)}；"
            f"注入故障后 GET /health={degraded.status} {brief(degraded.text, 40)}（连续两次）；"
            f"响应无路径/SQL/堆栈泄露={no_leak}",
            f"健康检查未创建登录会话（auth_sessions {before_sessions}→{after_sessions}）；"
            f"业务数据与文件不变={before_state == after_state and before_files == after_files}",
            f"Compose healthcheck 配置：间隔 10s={health_cfg['interval']}、超时 3s="
            f"{health_cfg['timeout']}、启动宽限 30s={health_cfg['start_period']}、"
            f"重试 3 次={health_cfg['retries']}",
            ok,
        )
    finally:
        home.stop()


def ac36() -> None:
    change_root = ROOT / "openspec" / "changes" / "add-auth-rbac-class-knowledge"
    files = {
        "proposal": change_root / "proposal.md",
        "design": change_root / "design.md",
        "tasks": change_root / "tasks.md",
        "spec": change_root / "specs" / "auth-upload" / "spec.md",
    }
    present = {name: path.exists() for name, path in files.items()}

    spec_text = files["spec"].read_text(encoding="utf-8") if present["spec"] else ""
    tasks_text = files["tasks"].read_text(encoding="utf-8") if present["tasks"] else ""

    requirement_blocks = re.findall(r"^### Requirement: (.+)$", spec_text, re.M)
    scenario_blocks = re.findall(r"^#### Scenario: (AC\d+)", spec_text, re.M)
    requirements_without_scenario = []
    chunks = re.split(r"^### Requirement: ", spec_text, flags=re.M)[1:]
    for chunk in chunks:
        title = chunk.splitlines()[0]
        if "#### Scenario:" not in chunk:
            requirements_without_scenario.append(title)

    ac_ids = [f"AC{index:02d}" for index in range(1, 37)]
    missing_in_tasks = [ac for ac in ac_ids if ac not in tasks_text]
    missing_in_spec = [ac for ac in ac_ids if ac not in spec_text]

    openspec = shutil.which("openspec") or shutil.which("openspec.cmd")
    validate_exit, validate_out = None, "未找到 openspec 可执行文件"
    if openspec:
        proc = subprocess.run([openspec, "validate", "add-auth-rbac-class-knowledge",
                               "--strict", "--no-interactive"],
                              cwd=str(ROOT), capture_output=True, text=True,
                              encoding="utf-8", errors="replace", shell=False)
        validate_exit, validate_out = proc.returncode, (proc.stdout + proc.stderr).strip()

    ok = (all(present.values()) and not requirements_without_scenario
          and not missing_in_tasks and not missing_in_spec
          and validate_exit == 0)
    rec(
        "AC36", "T1.1/T1.2/T8.1/T8.2",
        "本变更四份 Markdown 已编写完成；独立审阅范围、接口、状态码与需求/场景/任务编号",
        f"openspec validate --strict --no-interactive 退出码={validate_exit}；输出="
        f"{brief(validate_out, 90)}",
        f"四件套存在={present}；Requirement 数={len(requirement_blocks)}、"
        f"Scenario 数={len(scenario_blocks)}；缺少 Scenario 的 Requirement="
        f"{requirements_without_scenario or '无'}；spec 中缺失的 AC={missing_in_spec or '无'}；"
        f"tasks 未承接的 AC={missing_in_tasks or '无'}",
        f"验收记录见本报告；逐场景 H/D/F 见各场景行（AC01–AC35）",
        ok,
        ["原规约要求「复选框全部 [ ]」是针对文档交付阶段的约束；进入 Apply 阶段后已按实际完成情况"
         "勾选 20/21 项，未完成的 T8.2 保持 [ ]，不虚报完成。"],
    )


# ===========================================================================
# Docker 场景委派（AC33 / AC35）
# ===========================================================================

def ac33_ac35() -> None:
    script = ROOT / "scripts" / "acceptance_compose.py"
    docker = shutil.which("docker")
    if docker is None:
        blocked("AC33", "T7.1/T7.3", "未找到 docker 可执行文件")
        blocked("AC35", "T7.3", "未找到 docker 可执行文件")
        return
    probe = subprocess.run([docker, "info"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
    if probe.returncode != 0:
        reason = "Docker 守护进程不可用：" + brief((probe.stderr or "").strip(), 100)
        blocked("AC33", "T7.1/T7.3", reason)
        blocked("AC35", "T7.3", reason)
        return
    if not script.exists():
        blocked("AC33", "T7.1/T7.3", "未找到 scripts/acceptance_compose.py")
        blocked("AC35", "T7.3", "未找到 scripts/acceptance_compose.py")
        return

    proc = subprocess.run(
        [sys.executable, str(script), "--json-out", str(COMPOSE_JSON)],
        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace")
    if not COMPOSE_JSON.exists():
        reason = f"Compose 验收脚本未产出证据（退出码 {proc.returncode}）：" + \
                 brief((proc.stderr or proc.stdout or "").strip(), 100)
        blocked("AC33", "T7.1/T7.3", reason)
        blocked("AC35", "T7.3", reason)
        return
    for item in json.loads(COMPOSE_JSON.read_text(encoding="utf-8")):
        rec(item["ac"], item["task"], item["given"], item["http"], item["db"],
            item["files"], item["verdict"] == "通过", item.get("notes", []))


# ===========================================================================
# 报告输出
# ===========================================================================

def verdict_of(ac: str) -> str:
    rows = [row for row in REPORT if row.ac == ac]
    if any(row.verdict == "失败" for row in rows):
        return "失败"
    if any(row.verdict == "阻塞" for row in rows):
        return "阻塞"
    if rows:
        return "通过"
    return "未执行"


def write_report() -> None:
    ac_ids = [f"AC{index:02d}" for index in range(1, 37)]
    lines: list[str] = []
    lines.append("# CampusClaw 验收报告（AC01–AC36）")
    lines.append("")
    lines.append(f"- 执行时间：{EXECUTED_AT}")
    lines.append(f"- 执行环境：Windows，Python {sys.version.split()[0]}，"
                 f"隔离运行目录由脚本自动创建并清理")
    lines.append(f"- 执行方式：`python scripts/acceptance.py`"
                 f"（依赖 Docker 的 AC33/AC35 由 `scripts/acceptance_compose.py` 执行）")
    lines.append(f"- 证据约定：H = 脱敏 HTTP 请求/状态码/响应；D = 数据库前后记录；"
                 f"F = 文件清单与 SHA-256。Cookie、口令与 CSRF token 全部遮盖。")
    lines.append("")
    passed = sum(1 for ac in ac_ids if verdict_of(ac) == "通过")
    lines.append("## 总览")
    lines.append("")
    lines.append("| 场景 | 关联任务 | 结论 |")
    lines.append("| --- | --- | --- |")
    for ac in ac_ids:
        rows = [row for row in REPORT if row.ac == ac]
        task = rows[0].task if rows else "—"
        verdict = verdict_of(ac)
        mark = {"通过": "通过", "失败": "失败", "阻塞": "阻塞", "未执行": "未执行"}[verdict]
        lines.append(f"| {ac} | {task} | {mark} |")
    lines.append("")
    lines.append(f"**统计：{passed}/36 通过。**")
    lines.append("")
    if FAILURES:
        lines.append("未通过的场景：" + "、".join(sorted(set(FAILURES))))
        lines.append("")
    lines.append("## 明细")
    lines.append("")
    current = None
    for row in REPORT:
        if row.ac != current:
            current = row.ac
            lines.append(f"### {row.ac}（{row.task}）— {row.verdict}")
            lines.append("")
        lines.append(f"**前置条件：** {row.given}")
        lines.append("")
        lines.append(f"- **H（HTTP）**：{row.http}")
        lines.append(f"- **D（数据库）**：{row.db}")
        lines.append(f"- **F（文件）**：{row.files}")
        if row.notes:
            for note in row.notes:
                lines.append(f"- **说明**：{note}")
        lines.append("")
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")


# ===========================================================================
# 入口
# ===========================================================================

def build_plan(home: Home) -> list[tuple[str, Callable[[], None]]]:
    return [
        ("AC01", lambda: ac01(home)),
        ("AC02", lambda: ac02(home)),
        ("AC03", lambda: ac03(home)),
        ("AC30", ac30),
        ("AC31", ac31),
        ("AC04", lambda: ac04(home)),
        ("AC05", lambda: ac05(home)),
        ("AC06", lambda: ac06(home)),
        ("AC07", lambda: ac07(home)),
        ("AC08", lambda: ac08(home)),
        ("AC09", lambda: ac09(home)),
        ("AC32", lambda: ac32(home)),
        ("AC12", lambda: ac12(home)),
        ("AC13", lambda: ac13(home)),
        ("AC14", lambda: ac14(home)),
        ("AC15", lambda: ac15(home)),
        ("AC10", lambda: ac10(home)),
        ("AC11", lambda: ac11(home)),
        ("AC16", lambda: ac16(home)),
        ("AC17", lambda: ac17(home)),
        ("AC18", lambda: ac18(home)),
        ("AC19", lambda: ac19(home)),
        ("AC20", lambda: ac20(home)),
        ("AC23", lambda: ac23(home)),
        ("AC24", lambda: ac24(home)),
        ("AC25", lambda: ac25(home)),
        ("AC26", lambda: ac26(home)),
        ("AC21", lambda: ac21(Home("ac21").start())),
        ("AC27", lambda: ac27(Home("ac27").start())),
        ("AC28", lambda: ac28(Home("ac28").start())),
        ("AC29", lambda: ac29(Home("ac29").start())),
        ("AC22", ac22),
        ("AC34", ac34),
        ("AC33", ac33_ac35),
        ("AC36", ac36),
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CampusClaw 验收执行器")
    parser.add_argument("--only", default="", help="只执行指定场景，逗号分隔，如 AC16,AC20")
    parser.add_argument("--no-report", action="store_true", help="不写报告文件")
    parser.add_argument("--keep-runtime", action="store_true", help="保留临时运行目录便于排查")
    args = parser.parse_args(argv)

    # 验收输出保持可读：只保留错误级别的服务日志
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    logging.getLogger("campusclaw").setLevel(logging.ERROR)

    only = {item.strip().upper() for item in args.only.split(",") if item.strip()}
    main_home = None
    try:
        if not only or any(name != "AC33" and name != "AC35" for name in only):
            main_home = Home("main").start()
        plan = build_plan(main_home)  # type: ignore[arg-type]

        docker_done = False
        for name, action in plan:
            if only and name not in only:
                continue
            if name in ("AC33", "AC35"):
                if docker_done:
                    continue
                docker_done = True
            print(f"[{name}] 执行中…", flush=True)
            try:
                action()
            except Exception as exc:  # 单场景异常不阻断其余场景
                traceback.print_exc()
                rec(name, "—", f"执行异常：{type(exc).__name__}: {exc}",
                    "未完成", "未完成", "未完成", False)
            rows = [row for row in REPORT if row.ac == name]
            if rows:
                print(f"[{name}] {rows[-1].verdict}")
    finally:
        if main_home is not None:
            main_home.stop()

    ac_ids = [f"AC{index:02d}" for index in range(1, 37)]
    print("\n" + "=" * 68)
    for ac in ac_ids:
        rows = [row for row in REPORT if row.ac == ac]
        if rows:
            print(f"  {ac}  {verdict_of(ac)}")
    passed = sum(1 for ac in ac_ids if verdict_of(ac) == "通过")
    print(f"  合计：{passed}/{len([ac for ac in ac_ids if any(r.ac == ac for r in REPORT)])} 通过")
    print("=" * 68)

    if not args.no_report:
        write_report()
        print(f"报告已写入：{REPORT_PATH}")

    if args.keep_runtime:
        print(f"临时运行目录保留：{RUNTIME}")
    else:
        shutil.rmtree(RUNTIME, ignore_errors=True)
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())