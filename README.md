# CampusClaw

按班级统一管理教学材料。

**场景**：教师上传，本班师生读取，教师修改标题与删除。

**不做**：注册、找回密码、验证码、OAuth/SSO、管理员角色、跨班代操作、PDF/Word/图片/压缩包解析、OCR、批量上传、断点续传、版本覆盖、向量化、全文搜索、RAG 检索、AI 问答、Agent 编排、技能开关、MCP 运行时、作业布置与批改、成绩、回收站恢复、跨班分享、公开文件链接、Kubernetes 与 CI/CD。

技术栈：Python 3.11+ / Flask / SQLite / 私有文件目录，无外部服务依赖。

---

## 一、三步跑起来（开箱即用）

**不需要设置任何环境变量**，测试账号与密钥已内置。

```bash
git clone https://github.com/Zephyruszzzz/campusclaw.git
cd campusclaw

python -m venv .venv
# Windows (PowerShell)
.\.venv\Scripts\pip install -r requirements.txt
# macOS / Linux
source .venv/bin/activate && pip install -r requirements.txt

# 启动
# Windows
.\.venv\Scripts\python.exe app.py
# macOS / Linux
python app.py
```

然后浏览器打开 <http://127.0.0.1:8080/login>。

启动后终端会直接打印账号清单：

```
================================================================
  CampusClaw 已启动
  登录页    http://127.0.0.1:8080/login
  健康检查  http://127.0.0.1:8080/health
  数据目录  .../campusClaw/data
  上传目录  .../campusClaw/uploads
----------------------------------------------------------------
  预置账号（用户名 / 口令）
    teacher_a   TeacherA#2026  教师 · A 班
    student_a1  StudentA1#2026 学生 · A 班
    student_b1  StudentB1#2026 学生 · B 班
    teacher_b   TeacherB#2026  教师 · B 班
================================================================
```

首次启动会在项目根目录自动创建 `data/app.db` 与 `uploads/`，并写入两个班各一份示例讲义。

## 二、预置账号

| 用户名 | 口令 | 角色 | 班级 | 能做什么 |
| --- | --- | --- | --- | --- |
| `teacher_a` | `TeacherA#2026` | 教师 | A | 上传、改标题、删除 |
| `student_a1` | `StudentA1#2026` | 学生 | A | 只读本班 |
| `student_b1` | `StudentB1#2026` | 学生 | B | 只读本班 |
| `teacher_b` | `TeacherB#2026` | 教师 | B | 上传、改标题、删除 |

两个班各预置一份示例讲义（正文分别含 `A_ONLY_SEED` / `B_ONLY_SEED`），互相不可见。

用 `student_a1` 登录看不到 B 班材料，用来演示班级隔离；用 `teacher_b` 访问 A 班的材料 ID 会拿到 404。

## 三、Docker Compose 运行

```bash
docker compose up --build -d
docker compose ps        # 等待 STATUS 变为 healthy
```

- 登录页：<http://localhost:8080/login>
- 健康检查：<http://localhost:8080/health>

> `.env` **不是必需的**。Compose 只在它存在时才读取其中的 `SECRET_KEY` / `SEED_*`，
> 不存在则应用使用内置测试凭据。想覆盖默认值就 `cp .env.example .env` 再填。

> 首次构建需要拉取 `python:3.12-slim`。若报 `auth.docker.io ... failed to respond`，
> 参考第五节配置镜像源。

改宿主机映射端口（容器内固定 8080）：

```bash
CAMPUSCLAW_PORT=8090 docker compose up -d     # Windows PowerShell: $env:CAMPUSCLAW_PORT = "8090"
```

### 停止与重建（数据不丢失）

```bash
docker compose down
docker compose up -d
```

`./data`（SQLite）与 `./uploads`（原文件）为宿主机目录挂载，容器重建后账号、材料、标题修改和删除结果全部保留；重复初始化不会重置口令或复活已删除材料。**不要删除这两个目录**。

### 文件清理命令

删除材料时若底层文件删除失败（如权限不足），会保留待重试任务。恢复权限后执行：

```bash
docker compose exec app python scripts/cleanup_files.py --once
```

退出码 0 表示全部清理完成，非零表示仍有失败任务。

## 四、改用自定义凭据 / 严格模式

内置凭据是公开的，仅适合本机演示。需要覆盖时设置对应环境变量：

```bash
export SECRET_KEY="换成随机字符串"
export SEED_TEACHER_A_PASSWORD="TeacherA#2026"
export SEED_STUDENT_A1_PASSWORD="StudentA1#2026"
export SEED_STUDENT_B1_PASSWORD="StudentB1#2026"
export SEED_TEACHER_B_PASSWORD="TeacherB#2026"
export PORT=8090                          # 可选，默认 8080
python app.py
```

Windows PowerShell 用 `$env:SECRET_KEY = "..."` 的写法。

- 单个变量留空 → 该账号回落到内置口令；显式填写但长度不在 **8～72 个 UTF-8 字节**内 → 拒绝初始化（不截断、不部分写入）。
- **严格模式**：`CAMPUSCLAW_STRICT_CONFIG=1` 后，`SECRET_KEY` 或种子口令缺失即非零退出、不监听端口。正式环境请开启。
- 数据库首次建好后再改口令无效——口令哈希只在空库初始化时写入，需删掉 `data/` 重新初始化。

## 五、Docker 拉不到基础镜像怎么办

若 `docker compose up --build` 报错形如：

```
failed to fetch oauth token: Post "https://auth.docker.io/token":
dial tcp [2a03:2880:...]:443: connectex: ... connection attempt failed
```

这不是项目代码的问题——Docker 守护进程出不了网，拉不到 `python:3.12-slim`。
`docker compose ps` 表为空也是同一个原因：build 失败，容器压根没建出来。

两种修法，任选其一：

**A. 配国内镜像源**

编辑 `~/.docker/daemon.json`（Windows 为 `C:\Users\<你>\.docker\daemon.json`），追加：

```json
{
  "registry-mirrors": [
    "https://docker.1panel.live",
    "https://docker.1panel.top"
  ]
}
```

也可以在 Docker Desktop → Settings → Docker Engine 里直接改这段 JSON，然后 **Apply & restart**。
启动后用下面的命令确认已生效：

```bash
docker info | grep -A2 "Registry Mirrors"      # PowerShell: docker info | Select-String "Registry Mirrors" -Context 0,2
```

> 公共镜像源寿命不稳定，上面的地址未必长期可用。自测方法：
> `curl https://<源>/v2/` 返回 200 或 401 就算活着。

**B. 让 Docker 走本机代理**

本机若已有代理（如 `127.0.0.1:10809`），在 Docker Desktop → Settings → Resources → Proxies
选 Manual，HTTP 与 HTTPS 都填代理地址，Apply & restart。

**C. 不折腾 Docker**

直接走第一节的单机方式 `python app.py`，功能完全一致。

## 六、常见操作验证

```bash
# 健康检查（无需登录）
curl http://127.0.0.1:8080/health            # {"status":"ok"}

# 未登录访问受保护页面会被重定向
curl -i http://127.0.0.1:8080/materials      # 302 -> /login

# 一键自检：临时目录起服务 + 四个账号依次登录 + 核对班级隔离
python scripts/selfcheck.py

# 全量验收：AC01–AC36 逐条执行并生成 docs/acceptance-report.md
python scripts/acceptance.py

# 只跑单个场景（按需排查）
python scripts/acceptance.py --only AC07
```

`scripts/selfcheck.py` 与 `scripts/acceptance.py` 都不会污染项目目录，跑完自动清理。
`acceptance.py` 中依赖 Docker 的 AC33/AC35 会调用 `scripts/acceptance_compose.py`；
若本机 Docker 未运行或仓库不可达，这两条会如实记为阻塞/降级，不会伪装成通过。

界面预览（自有视觉骨架：靛紫主色 + 顶部导航 + 居中内容壳）：

- 登录页 [`docs/screenshots/08-ui-login.png`](docs/screenshots/08-ui-login.png)
- 材料页 [`docs/screenshots/09-ui-materials.png`](docs/screenshots/09-ui-materials.png)

版式说明：页面骨架自成一套，**不沿用**早期参考站的做法。

- 设计令牌：主色靛紫 `#4a3f8f`（深 `#372e6d`）、强调青 `#0f7b7d`、圆角 10px、内容壳宽 1160px。
- 布局：**顶部 appbar**（logo + 页签导航 + 账号区）+ 下方 banner（页面标题 + 统计条）+ 居中内容壳；**不使用侧边栏**。
- 登录页：渐变底 + 居中卡片，附带两个演示账号的一键填充按钮（仅填表单，不绕过认证）。
- 样式全部内联在模板中，页面无需额外静态资源即可正常渲染；模板内的接口调用与交互脚本与后端契约保持一致。

## 七、目录结构

```
app.py                     启动入口，打印账号横幅
campusclaw/
  config.py                配置与内置测试默认值、严格模式开关
  security.py              bcrypt 哈希、sid、会话绑定 CSRF token
  db.py                    SQLite 连接、事务、九张表的 DDL
  bootstrap.py             空库单事务初始化与幂等种子
  sessions.py              可撤销会话：到期/撤销/签名校验
  materials.py             隔离查询、上传事务、改标题、删除清理
  storage.py               私有上传目录、清理任务、启动恢复
  request_context.py       认证→角色→CSRF→班级→校验的判定链
  views/                   登录页、材料页、材料 API、健康检查
  templates/               页面模板
scripts/
  cleanup_files.py         重试待清理文件
  selfcheck.py             一键自检脚本（非业务代码）
  acceptance.py            AC01–AC36 全量验收执行器（隔离数据目录 + 真实 HTTP + 故障注入）
  acceptance_compose.py    AC33/AC35 的 Docker Compose 验收执行器（子进程调用）
docs/                      上传 / 下载 / 后端接口的实现说明与实测截图
  acceptance-report.md     验收报告：逐条 H/D/F 脱敏证据与结论
openspec/                  OpenSpec 工作区
  changes/add-auth-rbac-class-knowledge/   proposal / design / spec / tasks
```

## 八、进一步阅读

- [`文件上传下载与后端接口说明.md`](文件上传下载与后端接口说明.md) —— 上传、下载、API 暴露三个问题的完整问答
- [`docs/01-文件上传逻辑与支持类型.md`](docs/01-文件上传逻辑与支持类型.md) —— 支持哪些类型、校验规则、错误码
- [`docs/02-文件下载权限与实现逻辑.md`](docs/02-文件下载权限与实现逻辑.md) —— 下载权限模型，含未授权访问的实测截图
- [`docs/03-后端API暴露与浏览器访问.md`](docs/03-后端API暴露与浏览器访问.md) —— 接口清单与浏览器直连方式
- [`docs/acceptance-report.md`](docs/acceptance-report.md) —— AC01–AC36 验收结果，逐条 HTTP / 数据库 / 文件三类脱敏证据

## 九、已知限制

- 单机与 Docker 两条路径均已实测跑通：四个账号登录、班级隔离、上传/下载/删除、`down` 后 `up` 数据保留。
- 内置凭据属于公开信息，**仅供本机演示**；对外部署必须设置 `CAMPUSCLAW_STRICT_CONFIG=1` 并提供真实密钥与口令。
- 单库单 worker 的演示架构，不提供高并发与高可用保证。
- 仅支持单个 UTF-8 编码的 `.txt` / `.md`，单文件 ≤ 10 MiB。
