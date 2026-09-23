"""一键自检：不设置任何密钥/口令环境变量，直接用内置测试凭据启动并登录。

用途：克隆仓库后确认环境可用。脚本会自行在临时目录起一个服务、依次登录四个
预置账号并打印各自可见的材料数，结束后自动清理临时数据，不污染项目目录。

用法：
    python scripts/selfcheck.py
"""

import http.cookiejar
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PORT = int(os.environ.get("SELFCHECK_PORT", "8081"))
HOST = "127.0.0.1"

home = Path(tempfile.mkdtemp(prefix="campusclaw-selfcheck-")) / "runtime"
home.mkdir(parents=True)

env = dict(os.environ)
# 刻意清空所有敏感配置，验证内置默认值确实生效
for key in ("SECRET_KEY", "CAMPUSCLAW_STRICT_CONFIG", "PORT", "HOST"):
    env.pop(key, None)
for key in list(env):
    if key.startswith("SEED_"):
        env.pop(key, None)
env.update({"CAMPUSCLAW_HOME": str(home), "PYTHONPATH": str(ROOT), "PORT": str(PORT)})

proc = subprocess.Popen(
    [sys.executable, str(ROOT / "app.py")],
    cwd=str(ROOT),
    env=env,
    stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT,
    text=True,
    encoding="utf-8",
    errors="replace",
)

base = f"http://{HOST}:{PORT}"
deadline = time.time() + 40
ready = False
while time.time() < deadline:
    try:
        urllib.request.urlopen(base + "/health", timeout=2)
        ready = True
        break
    except Exception:
        time.sleep(0.3)

if not ready:
    proc.kill()
    print("启动失败：", file=sys.stderr)
    print(proc.stdout.read() if proc.stdout else "", file=sys.stderr)
    sys.exit(1)

op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
print("health :", op.open(base + "/health").status, op.open(base + "/health").read().decode())

EXPECTED = {
    # 用户名: 期望可见的材料数（A 班 1 条、B 班 1 条，用于核对班级隔离）
    "teacher_a": 1,
    "student_a1": 1,
    "student_b1": 1,
    "teacher_b": 1,
}

failures = 0
for username in ("teacher_a", "student_a1", "student_b1", "teacher_b"):
    password = {
        "teacher_a": "TeacherA#2026",
        "student_a1": "StudentA1#2026",
        "student_b1": "StudentB1#2026",
        "teacher_b": "TeacherB#2026",
    }[username]
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
    )
    html = opener.open(base + "/login").read().decode()
    token = re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)
    req = urllib.request.Request(
        base + "/login",
        data=urllib.parse.urlencode(
            {"username": username, "password": password, "csrf_token": token}
        ).encode(),
    )
    try:
        resp = opener.open(req)
        items = json.loads(opener.open(base + "/api/materials").read().decode())["items"]
        titles = "、".join(item["title"] for item in items)
        ok = len(items) == EXPECTED[username]
        failures += 0 if ok else 1
        print(
            f"login  : {username:<12} {resp.status} -> {resp.geturl()}  "
            f"可见 {len(items)} 条 [{titles}]  {'OK' if ok else '数量不符'}"
        )
    except urllib.error.HTTPError as exc:
        failures += 1
        print(f"login  : {username:<12} ERROR {exc.code} {exc.read().decode()[:120]}")

proc.terminate()
try:
    proc.wait(timeout=8)
except subprocess.TimeoutExpired:
    proc.kill()
out = proc.stdout.read() if proc.stdout else ""
print("--- 进程原始输出（含启动横幅）---")
print(out)
shutil.rmtree(home.parent, ignore_errors=True)

if failures:
    print(f"自检未通过：{failures} 个账号异常", file=sys.stderr)
    sys.exit(1)
print("自检全部通过。")
