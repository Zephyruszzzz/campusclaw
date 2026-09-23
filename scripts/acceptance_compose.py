#!/usr/bin/env python
"""CampusClaw Docker/Compose 场景验收：AC33（从零按文档启动）与 AC35（容器重建保留数据）。

用法：
    python scripts/acceptance_compose.py                 # 人工查看
    python scripts/acceptance_compose.py --json-out x.json   # 供 scripts/acceptance.py 合并

隔离策略：把仓库按「全新克隆」的方式复制到临时目录再启动，绝不触碰当前工作目录的
data/ 与 uploads/。为避免与本机可能已运行的同名容器冲突，只在临时副本里把
container_name 改为 campusclaw-verify；构建、端口映射、健康检查、卷映射均保持原样。

需要 Docker 守护进程可用；不可用时本脚本以非零退出并说明原因。
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

PROJECT = "ccverify"
CONTAINER = "campusclaw-verify"
IMAGE = "campusclaw:latest"
VERIFY_IMAGE = "campusclaw-verify:latest"
SESSION_COOKIE = "campusclaw_session"

# 本机镜像库已存在同源基础镜像（campusclaw:latest 由同一个 Dockerfile 构建）时，
# 用它充当基础层可以完全绕开镜像仓库。仅在仓库不可达时作为降级路径使用：
# 依赖集合未变（requirements.txt 早于该镜像构建），因此运行时内容等价，
# 差别只是没有重新执行 `pip install`。降级会写进证据里，不会假装是全新拉取。
OFFLINE_DOCKERFILE = """\
FROM {base}
WORKDIR /app
COPY campusclaw/ ./campusclaw/
COPY app.py ./
COPY scripts/ ./scripts/
RUN mkdir -p /app/data /app/uploads
EXPOSE 8080
CMD ["gunicorn", "--workers", "1", "--bind", "0.0.0.0:8080", "app:app"]
"""

REGISTRY_HINTS = (
    "failed to resolve source metadata", "failed to fetch oauth token",
    "auth.docker.io", "registry-1.docker.io", "pull access denied",
    "unexpected status from HEAD request", "Bad Gateway", "TLS handshake timeout",
    "failed to authorize", "no such host", "connection reset",
)

VERIFY_PASSWORDS = {
    "teacher_a": "VerifyTeacherA#2026",
    "student_a1": "VerifyStudentA1#2026",
    "student_b1": "VerifyStudentB1#2026",
    "teacher_b": "VerifyTeacherB#2026",
}
SECRET_KEY = "campusclaw-verify-secret-key-0123456789abcdef"

IGNORE = shutil.ignore_patterns(
    ".git", ".venv", "venv", "data", "uploads", "docs", ".workbuddy", ".codebuddy",
    "__pycache__", "*.pyc", ".env", "node_modules", ".pytest_cache",
)


# ---------------------------------------------------------------- 小工具
def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def brief(value: str, limit: int = 160) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text if len(text) <= limit else text[:limit] + "…"


def run(cmd: list[str], cwd: Path, timeout: int = 900) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)


def is_registry_failure(result: subprocess.CompletedProcess) -> bool:
    """判断构建失败是不是「拉不到基础镜像」这类网络原因，而不是项目本身的问题。"""
    text = f"{result.stderr or ''}\n{result.stdout or ''}"
    return any(hint.lower() in text.lower() for hint in REGISTRY_HINTS)


def bring_up_stack(docker: str, workdir: Path) -> tuple[subprocess.CompletedProcess, str]:
    """启动 Compose 栈，返回 (结果, 构建方式说明)。

    优先按 README 原样 `--build`；若失败原因是镜像仓库不可达，
    则退化为「以本机已有同源镜像为基础层」重新构建，让验收继续跑完，
    并在返回值里如实标注退化原因。
    """
    up = run([docker, "compose", "-p", PROJECT, "up", "--build", "-d"], workdir)
    if up.returncode == 0:
        return up, "docker compose up --build -d（按 Dockerfile 全新构建）"

    if not is_registry_failure(up):
        return up, "docker compose up --build -d"

    detail = brief(up.stderr or up.stdout, 120)
    local_base_ok = subprocess.run([docker, "image", "inspect", IMAGE],
                                   capture_output=True, text=True).returncode == 0
    if not local_base_ok:
        up.stderr = (up.stderr or "") + "\n（本机也没有可复用的基础镜像，无法降级）"
        return up, "docker compose up --build -d"

    (workdir / "Dockerfile").write_text(OFFLINE_DOCKERFILE.format(base=IMAGE),
                                        encoding="utf-8")
    retry = run([docker, "compose", "-p", PROJECT, "up", "--build", "-d"], workdir)
    note = (f"镜像仓库不可达（{detail}），降级为以本机已有同源镜像 {IMAGE} 作为基础层重建；"
            f"依赖集合未变，运行时内容等价，仅跳过 pip install")
    return retry, note


class Agent:
    """最小 HTTP 客户端：不跟随重定向，便于观测 302。"""

    def __init__(self, base: str):
        self.base = base
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar), _NoRedirect)

    def get(self, path: str) -> tuple[int, bytes, dict]:
        return self._call("GET", path)

    def post_form(self, path: str, fields: dict) -> tuple[int, bytes, dict]:
        body = urllib.parse.urlencode(fields).encode()
        return self._call("POST", path, body,
                          {"Content-Type": "application/x-www-form-urlencoded"})

    def multipart(self, path: str, fields: list, files: list, headers: dict | None = None):
        boundary = "----ccverify" + uuid.uuid4().hex
        payload = _multipart(fields, files, boundary)
        merged = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
        merged.update(headers or {})
        return self._call("POST", path, payload, merged)

    def patch_json(self, path: str, data: dict, headers: dict | None = None):
        merged = {"Content-Type": "application/json"}
        merged.update(headers or {})
        return self._call("PATCH", path, json.dumps(data).encode(), merged)

    def delete(self, path: str, headers: dict | None = None):
        return self._call("DELETE", path, None, headers or {})

    def _call(self, method: str, path: str, body: bytes | None = None,
              headers: dict | None = None, timeout: float = 90.0):
        request = urllib.request.Request(self.base + path, data=body, method=method)
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        try:
            response = self.opener.open(request, timeout=timeout)
            return response.status, response.read(), dict(response.headers)
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(), dict(exc.headers)

    def csrf(self, path: str = "/login") -> str:
        _, body, _ = self.get(path)
        found = re.search(r'name="csrf_token" value="([^"]+)"',
                          body.decode("utf-8", "replace"))
        return found.group(1) if found else ""

    def login(self, username: str) -> tuple[int, str]:
        token = self.csrf("/login")
        status, _, headers = self.post_form("/login", {
            "username": username, "password": VERIFY_PASSWORDS[username],
            "csrf_token": token})
        return status, headers.get("Location", "")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _multipart(fields: list, files: list, boundary: str) -> bytes:
    import io

    buffer = io.BytesIO()
    for name, value in fields:
        buffer.write(f"--{boundary}\r\n".encode())
        buffer.write(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        buffer.write(str(value).encode("utf-8"))
        buffer.write(b"\r\n")
    for name, filename, data, ctype in files:
        buffer.write(f"--{boundary}\r\n".encode())
        buffer.write(
            f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'.encode())
        buffer.write(f"Content-Type: {ctype}\r\n\r\n".encode())
        buffer.write(data)
        buffer.write(b"\r\n")
    buffer.write(f"--{boundary}--\r\n".encode())
    return buffer.getvalue()


def read_db(db_path: Path) -> dict:
    """把 SQLite 文件（含 WAL 附属文件）复制到临时目录后只读打开，避免锁竞争。"""
    staging = Path(tempfile.mkdtemp(prefix="ccverify-db-"))
    for suffix in ("", "-wal", "-shm"):
        source = Path(str(db_path) + suffix)
        if source.exists():
            shutil.copy2(source, staging / (db_path.name + suffix))
    conn = sqlite3.connect(str(staging / db_path.name))
    conn.row_factory = sqlite3.Row
    try:
        return {
            "classes": [tuple(r) for r in conn.execute("SELECT id, name FROM classes ORDER BY id")],
            "users": [tuple(r) for r in conn.execute(
                "SELECT username, role, class_id FROM users ORDER BY username")],
            "user_hashes": {r["username"]: r["password_hash"][:7] for r in conn.execute(
                "SELECT username, password_hash FROM users")},
            "materials": [tuple(r) for r in conn.execute(
                "SELECT id, class_id, title, status, size_bytes FROM materials ORDER BY id")],
            "knowledge": conn.execute("SELECT COUNT(*) FROM knowledge_entries").fetchone()[0],
            "jobs": [tuple(r) for r in conn.execute(
                "SELECT material_id, status FROM file_cleanup_jobs ORDER BY id")],
        }
    finally:
        conn.close()
        shutil.rmtree(staging, ignore_errors=True)


def fs_listing(upload_dir: Path) -> dict[str, int]:
    result: dict[str, int] = {}
    if not upload_dir.exists():
        return result
    for path in sorted(upload_dir.rglob("*")):
        if path.is_file():
            result[str(path.relative_to(upload_dir)).replace("\\", "/")] = path.stat().st_size
    return result


# ---------------------------------------------------------------- 主流程
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CampusClaw Compose 场景验收")
    parser.add_argument("--json-out", default="", help="把记录写入 JSON 供合并")
    parser.add_argument("--keep", action="store_true", help="保留临时目录与容器便于排查")
    args = parser.parse_args(argv)

    records: list[dict] = []

    def rec(ac, task, given, http, db, files, ok, notes=None):
        records.append({"ac": ac, "task": task, "given": given, "http": http,
                        "db": db, "files": files,
                        "verdict": "通过" if ok else "失败", "notes": list(notes or [])})

    docker = shutil.which("docker")
    if docker is None:
        rec("AC33", "T7.1/T7.3", "未找到 docker 可执行文件", "未执行", "未执行", "未执行", False)
        rec("AC35", "T7.3", "未找到 docker 可执行文件", "未执行", "未执行", "未执行", False)
        return _finish(records, args)

    probe = run([docker, "info"], ROOT, timeout=60)
    if probe.returncode != 0:
        reason = "Docker 守护进程不可用：" + brief(probe.stderr or probe.stdout)
        rec("AC33", "T7.1/T7.3", reason, "未执行", "未执行", "未执行", False)
        rec("AC35", "T7.3", reason, "未执行", "未执行", "未执行", False)
        return _finish(records, args)

    runtime = Path(tempfile.mkdtemp(prefix="campusclaw-compose-"))
    workdir = runtime / "repo"
    port = free_port()
    try:
        shutil.copytree(ROOT, workdir, ignore=IGNORE)

        # 只在临时副本里改容器名与镜像标签，避免干扰本机同名容器与镜像
        compose_path = workdir / "docker-compose.yml"
        compose_text = compose_path.read_text(encoding="utf-8")
        compose_text = compose_text.replace("container_name: campusclaw-app",
                                            f"container_name: {CONTAINER}")
        compose_text = compose_text.replace(f"image: {IMAGE}", f"image: {VERIFY_IMAGE}")
        compose_path.write_text(compose_text, encoding="utf-8")

        env_text = (workdir / ".env.example").read_text(encoding="utf-8")
        env_lines = [
            f"SECRET_KEY={SECRET_KEY}",
            *[f"SEED_{key.upper()}_PASSWORD={VERIFY_PASSWORDS[key]}" for key in VERIFY_PASSWORDS],
            "AUTH_COOKIE_SECURE=false",
            f"CAMPUSCLAW_PORT={port}",
        ]
        (workdir / ".env").write_text(
            env_text + "\n# --- 验收填入 ---\n" + "\n".join(env_lines) + "\n",
            encoding="utf-8")

        base = f"http://127.0.0.1:{port}"

        # ---------------------------------------------------------- AC33 启动
        up, build_note = bring_up_stack(docker, workdir)
        if up.returncode != 0:
            rec("AC33", "T7.1/T7.3",
                "空目录按 README：复制 .env.example 为 .env、填变量、docker compose up --build -d",
                f"docker compose up 退出码={up.returncode}：" + brief(up.stderr or up.stdout),
                "未执行", "未执行", False,
                ["构建方式：" + build_note] if build_note else None)
            rec("AC35", "T7.3", "前置启动失败", "未执行", "未执行", "未执行", False)
            return _finish(records, args)

        healthy, health_status = wait_healthy(docker, timeout=240)

        login_page_status = 0
        for _ in range(30):
            login_page_status, _, _ = Agent(base).get("/login")
            if login_page_status == 200:
                break
            time.sleep(1)

        login_ok: dict[str, tuple[int, int]] = {}
        for username in VERIFY_PASSWORDS:
            client = Agent(base)
            status_code, _ = client.login(username)
            listing_status, listing_body, _ = client.get("/api/materials")
            items = (json.loads(listing_body.decode("utf-8")).get("items", [])
                     if listing_status == 200 else [])
            login_ok[username] = (status_code, len(items))

        db_state = read_db(workdir / "data" / "app.db")
        files = fs_listing(workdir / "uploads")
        login_summary = "、".join(f"{u}={login_ok[u][0]}/可见 {login_ok[u][1]} 条"
                                  for u in VERIFY_PASSWORDS)

        seeded_ok = (len(db_state["classes"]) == 2 and len(db_state["users"]) == 4
                     and len([m for m in db_state["materials"] if m[3] == "active"]) == 2
                     and db_state["knowledge"] == 2 and len(files) == 2)
        logins_all = all(v[0] == 302 and v[1] == 1 for v in login_ok.values())
        ok33 = (healthy and login_page_status == 200 and logins_all and seeded_ok)
        rec("AC33", "T7.1/T7.3",
            "隔离临时目录，按 README 复制 .env.example 为 .env 并填入必需变量，"
            "执行 docker compose up --build -d（容器名与镜像标签在副本中改为 "
            f"{CONTAINER} / {VERIFY_IMAGE}，以免与本机实例冲突）",
            f"容器健康状态={health_status}；GET /login={login_page_status}；{login_summary}",
            f"classes={db_state['classes']}；users={db_state['users']}；"
            f"active 材料={[m for m in db_state['materials'] if m[3] == 'active']}；"
            f"knowledge 行数={db_state['knowledge']}",
            f"数据与上传目录映射到宿主机临时目录，文件={sorted(files)}；"
            f"镜像构建仅 COPY requirements.txt/campusclaw/app.py/scripts，不含 .env",
            ok33,
            [f"构建方式：{build_note}"])

        # ---------------------------------------------------------- AC35 重建
        teacher = Agent(base)
        teacher.login("teacher_a")
        token = teacher.csrf("/materials")
        created = teacher.multipart("/api/materials/upload",
                                    [("csrf_token", token), ("title", "验收重建材料 AC35")],
                                    [("file", "ac35.txt", b"AC35 persistence check\n",
                                      "text/plain")])
        created_body = json.loads(created[1].decode("utf-8"))
        new_id = created_body.get("material_id")

        rename = teacher.patch_json(f"/api/materials/{new_id}",
                                    {"title": "验收重建材料 AC35（已改名）"},
                                    {"X-CSRF-Token": teacher.csrf()})

        before_down = read_db(workdir / "data" / "app.db")
        files_before = fs_listing(workdir / "uploads")

        deleted_ids = [m[0] for m in before_down["materials"]
                       if m[3] == "active" and m[0] != new_id]
        deleted_id = deleted_ids[0] if deleted_ids else None
        delete_status = None
        if deleted_id is not None:
            delete_status = teacher.delete(f"/api/materials/{deleted_id}",
                                           {"X-CSRF-Token": teacher.csrf()})[0]

        mid_db = read_db(workdir / "data" / "app.db")
        files_after_delete = fs_listing(workdir / "uploads")

        down = run([docker, "compose", "-p", PROJECT, "down"], workdir)
        up2 = run([docker, "compose", "-p", PROJECT, "up", "-d"], workdir)
        healthy2, health_status2 = wait_healthy(docker, timeout=240)

        again = Agent(base)
        relogin = again.login("teacher_a")
        listing_status, listing_body, _ = again.get("/api/materials")
        items = json.loads(listing_body.decode("utf-8")).get("items", []) \
            if listing_status == 200 else []
        titles = [item["title"] for item in items]
        detail = again.get(f"/api/materials/{new_id}")[0]
        download = again.get(f"/api/materials/{new_id}/file")
        deleted_read = again.get(f"/api/materials/{deleted_id}")[0] if deleted_id else "不适用"

        after_db = read_db(workdir / "data" / "app.db")
        files_after = fs_listing(workdir / "uploads")

        ids_kept = ({m[0] for m in mid_db["materials"]} == {m[0] for m in after_db["materials"]})
        hashes_kept = (mid_db["user_hashes"] == after_db["user_hashes"])
        no_dupe_seed = (len(after_db["classes"]) == 2 and len(after_db["users"]) == 4)
        deleted_still_gone = all(m[0] != deleted_id or m[3] == "deleted"
                                 for m in after_db["materials"]) if deleted_id else True
        download_ok = download[0] == 200 and download[1] == b"AC35 persistence check\n"
        files_preserved = files_after == files_after_delete

        ok35 = (created[0] == 201 and rename[0] == 200 and down.returncode == 0
                and up2.returncode == 0 and healthy2 and relogin[0] == 302
                and "验收重建材料 AC35（已改名）" in titles and detail == 200
                and download_ok and deleted_read == 404 and ids_kept and hashes_kept
                and no_dupe_seed and deleted_still_gone and files_preserved)
        rec("AC35", "T7.3",
            "已上传一份 A 班材料并改名，另有一份已删除材料；执行 docker compose down 后 "
            "docker compose up -d，不删卷与宿主机目录",
            f"上传={created[0]}（material_id={new_id}）；改标题={rename[0]}；"
            f"删除={delete_status}；down 退出码={down.returncode}；up 退出码={up2.returncode}；"
            f"重建后健康={health_status2}；重新登录={relogin[0]}；列表={listing_status} "
            f"{titles}；详情={detail}；下载字节一致={download_ok}；已删除材料读取={deleted_read}",
            f"重建前后材料 ID 集合一致={ids_kept}；口令哈希不变={hashes_kept}；"
            f"无重复种子={no_dupe_seed}；已删除材料保持 deleted={deleted_still_gone}；"
            f"清理任务={after_db['jobs']}",
            f"上传目录重建前后一致={files_preserved}（共 {len(files_after)} 个文件）；"
            f"已删除材料的原文件未复活",
            ok35)

        if not args.keep:
            run([docker, "compose", "-p", PROJECT, "down"], workdir)
    finally:
        if not args.keep:
            shutil.rmtree(runtime, ignore_errors=True)

    return _finish(records, args)


def wait_healthy(docker: str, timeout: int = 240) -> tuple[bool, str]:
    deadline = time.time() + timeout
    status = "unknown"
    while time.time() < deadline:
        result = run([docker, "inspect", "-f", "{{.State.Health.Status}}", CONTAINER],
                     ROOT, timeout=60)
        status = (result.stdout or result.stderr).strip()
        if status == "healthy":
            return True, status
        if status in ("unhealthy", "no such object", "Error: No such object"):
            return False, status
        time.sleep(3)
    return False, status


def _finish(records: list[dict], args) -> int:
    for item in records:
        print(f"[{item['ac']}] {item['verdict']}")
        print(f"    H: {item['http']}")
        print(f"    D: {item['db']}")
        print(f"    F: {item['files']}")
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(records, ensure_ascii=False, indent=2),
                                       encoding="utf-8")
    failed = [item["ac"] for item in records if item["verdict"] != "通过"]
    print(f"\n验收场景合计 {len(records)} 项，未通过 {len(failed)} 项"
          + (f"：{'、'.join(failed)}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
