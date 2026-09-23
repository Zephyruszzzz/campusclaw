# CampusClaw 技术设计与验收约定

## Context

本设计服务于 [proposal.md](proposal.md) 的首个变更，约束后续实现，不包含已运行系统的证明。需求与场景编号以 [spec.md](specs/auth-upload/spec.md) 为准。来源标记 S1/S2/S3/U1/D1 的含义见 proposal。

## Goals / Non-Goals

目标：形成登录 → 服务端身份与班级判断 → 教师上传 → 文件及解析全文入库 → 本班查询的闭环，并支持标题修改和删除。所有结果可通过 HTTP、SQL 和文件系统证据交叉验证。

非目标完全继承 proposal；不因为产品介绍提到 RAG、智能体或作业流程而增加相关实现。

## Decisions

### D-01 技术栈与边界

采用 Flask 3.x、SQLite 3、bcrypt、服务端模板页面与同进程 JSON API。Compose 内使用 gunicorn，默认单 worker，监听 `0.0.0.0:8080`。后续实现时锁定具体依赖版本；本规约不强制某个补丁版本。

浏览器仅负责展示和发起请求；App 完成认证、授权、验证、解析与数据库事务；SQLite 保存业务状态；私有上传目录保存原始字节。不存在浏览器直连数据库或未鉴权静态文件服务。

```text
Browser ── Cookie / HTTP ──> App：认证 → 角色 → 班级 → 输入验证
                              ├─ SQLite：用户、会话、材料、知识内容
                              └─ 私有 uploads：原文件、暂存文件
```

### D-02 身份、会话与密码

- Cookie 为 Flask 签名 Cookie，保存不可预测的 `sid`；不保存密码或密码哈希。签名提供防篡改能力，不提供内容加密。
- `auth_sessions` 保存会话标识的 SHA-256 摘要、用户 ID、创建/到期/撤销时间。每次业务请求验证 Cookie 签名、会话有效性并读取用户，形成服务端 `user_id/role/class_id` 上下文；不能信任表单或未签名 Cookie 中的这些值。
- 登录成功生成新 sid，固定有效期为登录后 8 小时；Cookie 设置 HttpOnly、SameSite=Lax、Path=/。本地 HTTP 演示默认不设置 Secure，HTTPS 部署必须设置 Secure；无自动续期。
- 退出撤销当前 sid 对应的服务端记录并清除 Cookie；复制退出前的 Cookie 再请求也不得恢复权限。其他独立登录会话不被本次退出撤销。
- 错误用户名和错误密码均返回 401 与统一“账号或密码错误”，不得返回哈希或区分账号是否存在。已有会话发起新的登录尝试时先撤销旧会话，失败后不得保留旧登录权限。
- bcrypt 使用库提供的 hash/check 接口，成本因子至少 12；盐由库生成。预置口令从环境变量读取，校验其 UTF-8 字节长度为 8～72，超限拒绝初始化，禁止静默截断。
- `SECRET_KEY` 必须为非空白的服务端环境变量；缺失时进程非零退出，不提供内置默认值。真实 `.env` 不入库，`.env.example` 仅列变量及占位说明。
- 写接口验证与会话绑定的 CSRF token；登录表单使用匿名会话 token，成功登录后轮换。对已认证学生的材料写请求先执行角色校验，确保返回 `ROLE_FORBIDDEN`。其余有效会话缺失/错误 token 返回 403 `CSRF_INVALID`，无写入副作用。

### D-03 角色权限和服务端隔离

| 操作 | 未登录 | 本班学生 | 本班教师 | 已登录跨班请求 |
| --- | --- | --- | --- | --- |
| 登录页、健康检查 | 允许 | 允许 | 允许 | 不涉及班级资源 |
| 材料页面 | 302 到登录页 | 200 | 200 | 按 ID 返回 404 |
| 列表、详情、下载 API | 401 | 200，只含本班 | 200，只含本班 | 404；列表不能切换到他班 |
| 上传、修改标题、删除 API | 401 | 403 | 允许操作本班 | 教师针对他班 ID 为 404；学生优先 403 |

请求判定顺序：认证 → 写操作的角色检查 → CSRF → 路径 ID 所属班级/有效状态 → 输入验证 → 业务操作。带有效 CSRF token 的跨班教师操作固定返回 404。未知路由不属于受保护接口验收范围。

材料列表查询必须含服务端 `class_id` 和 `status='active'` 条件；详情、标题修改、删除使用 `id + class_id + active` 联合条件，不能先无条件修改再检查。知识内容读取通过本班 active 材料联表校验。公开列表不做搜索和分页，本版返回本班全部有效材料，按 `created_at DESC, id DESC` 排序。

客户端在 query、form、JSON 中提交 `class_id`、`role`、`user_id`、`uploaded_by` 均返回 400 `IDENTITY_FIELD_FORBIDDEN`，不能将其用作身份依据。伪造 Cookie 签名视为未认证。用户使用材料 ID 访问跨班资源与访问不存在资源均返回相同的 404 错误结构。

上传目录不得挂载为静态资源，也不得返回服务器路径或存储键。下载仅经受保护端点读取，响应使用附件方式和安全文件名；正文在页面中转义展示，Markdown 按普通文本展示，不执行 HTML 或脚本。

### D-04 最小接口合同

所有 JSON 错误统一为 `{"error":{"code":"...","message":"..."}}`。错误信息不得包含口令、哈希、SQL、异常堆栈、绝对路径或他班内容。除指定 302 外，不将 API 错误重定向为 HTML 登录页。

| 方法与路径 | 输入 | 成功响应 | 主要失败响应 |
| --- | --- | --- | --- |
| `GET /login` | 无 | 200 HTML，含登录表单及匿名 CSRF token | — |
| `POST /login` | form：username、password、csrf_token | 302 `/materials`，设置新会话 | 400 INVALID_INPUT；401 INVALID_CREDENTIALS；403 CSRF_INVALID |
| `POST /logout` | 有效 Cookie 和 CSRF token | 302 `/login`，撤销当前会话 | 401 AUTH_REQUIRED；403 CSRF_INVALID |
| `GET /materials` | 有效 Cookie | 200 HTML，列表及当前会话 CSRF token | 未认证为 302 `/login` |
| `GET /api/materials` | 有效 Cookie；无业务查询参数 | 200 `{"items":[材料摘要]}` | 401；400 IDENTITY_FIELD_FORBIDDEN |
| `GET /api/materials/{id}` | 正整数材料 ID | 200 `{"material":材料摘要,"body_text":"解析全文"}` | 401；404 MATERIAL_NOT_FOUND |
| `GET /api/materials/{id}/file` | 正整数材料 ID | 200 原文件字节，附件响应 | 401；404 MATERIAL_NOT_FOUND；500 STORAGE_ERROR |
| `POST /api/materials/upload` | multipart：file（恰好 1 个）、title（可选）；`X-CSRF-Token` | 201 `{"material_id":id,"status":"active"}` | 401；403；400；413；415；422；500 |
| `PATCH /api/materials/{id}` | JSON：仅 `title`；`X-CSRF-Token` | 200 `{"material_id":id,"title":"新标题"}` | 401；403；404；400 INVALID_TITLE/INVALID_FIELDS；500 DATABASE_ERROR |
| `DELETE /api/materials/{id}` | 无业务请求体；`X-CSRF-Token` | 文件清理完成为 204，无响应体 | 清理待重试为 202；401；403；404；500 DATABASE_ERROR |
| `GET /health` | 无，允许匿名 | 200 `{"status":"ok"}` | 数据库不可查询为 503 `{"status":"unavailable"}` |

材料摘要字段固定为 `id,title,class_id,uploaded_by,original_filename,size_bytes,created_at,updated_at`，时间为带 `Z` 的 UTC ISO 8601。标题修改和上传接口不接受额外字段；只返回上述公开字段，不返回存储路径。无效格式的资源 ID 与不存在 ID 一律 404。后续若增加 API 必须先修改规约。

### D-05 数据模型与种子

SQLite 启用外键。所有业务 ID 为内部主键，不是授权凭据；所有时间采用 UTC。

| 表/实体 | 必需字段或约束 | 本次用途 |
| --- | --- | --- |
| `classes` | id、name；name 唯一 | 班级实体，预置 A/B。 |
| `users` | id、username 唯一、password_hash、role、class_id 外键 | 用户仅 teacher/student，每人恰好一个班级。 |
| `materials`（兼讲义实体） | id、class_id、uploaded_by、title、original_filename、storage_key 唯一、size_bytes、status、created_at、updated_at、deleted_at | 讲义由此实体承担，不重复建立一张无意义的 lectures 表。status 为 active/deleted。 |
| `knowledge_entries` | id、material_id 唯一、class_id、body_text、created_at | 每份 active 材料恰好一条解析全文；与材料班级一致。 |
| `assignments` | id、class_id、title | 作业预留，空表即可。 |
| `assistants` | id、class_id、name | 助手预留，空表即可。 |
| `skills` | id、name | 技能预留，空表即可，不开放 API。 |
| `auth_sessions` | sid_hash 唯一、user_id、created_at、expires_at、revoked_at | 可撤销登录态。 |
| `file_cleanup_jobs` | id、material_id、storage_key、status、attempts、last_error、updated_at | 删除清理任务；status 为 pending/done；错误仅在服务端保留。 |

`materials` 对 `(id,class_id)` 建唯一约束；`knowledge_entries` 以 `(material_id,class_id)` 复合外键保证班级一致。上传者必须属于材料班级，服务端写入前校验。本版标题不在知识表重复存储，知识内容关联材料读取当前标题，避免改标题产生不同步副本。

初始化预置 `teacher_a → A/teacher`、`student_a1 → A/student`、`student_b1 → B/student`。为 B 班样本建立 `teacher_b → B/teacher`，同时用于反向隔离验证。所有账号初始口令来自 `SEED_TEACHER_A_PASSWORD`、`SEED_STUDENT_A1_PASSWORD`、`SEED_STUDENT_B1_PASSWORD`、`SEED_TEACHER_B_PASSWORD`。空库初始化须先验证全部种子变量和口令长度，再写用户、材料和样本文件；验证失败不能留下部分初始化结果。

两班各有一份标题分别为“A 班示例讲义”“B 班示例讲义”的材料，正文分别含 `A_ONLY_SEED` 和 `B_ONLY_SEED`，均具备原文件与知识库记录。测试新增文件使用独立标记 `A_UPLOAD_MARKER_001`。数据库为空时一次事务建立结构和种子；重复执行初始化不得增加重复数据、重置口令或复活已删除材料。已有库缺必需表时启动失败并提示处理，不能静默当作空库覆盖。

### D-06 文件验证与上传事务

- 一个请求只接收一个 `.txt` 或 `.md` 文件，扩展名大小写不敏感；MIME 头仅作辅助，不能替代解码与扩展名验证。
- 文件大小按原始字节计算，合法范围为 1～10,485,760 字节（10 MiB），含上限。HTTP multipart 请求总大小上限为 11 MiB；文件上限必须另行检查，不把 multipart 开销算进文件大小。
- 严格 UTF-8 解码，允许 UTF-8 BOM 并在解析正文中去掉 BOM；原文件按原字节保存。空字节文件、正文仅空白、NUL 字符均不合法；不做 Markdown 渲染或摘要生成，保存完整解析文本。
- 标题取可选 title 去首尾空白的值，否则取安全原文件名的主文件名；长度按 Unicode 码点为 1～200，禁止控制字符。原文件名仅取 basename；存储使用服务端 UUID 键，不沿用客户端路径，同名不同次上传产生不同材料。

| 异常 | HTTP / error.code | 业务结果 |
| --- | --- | --- |
| 缺少 file、多个文件、零字节 | 400 INVALID_FILE | 不新增材料、知识记录或原文件。 |
| 标题非法、字段不在合同内 | 400 INVALID_TITLE / INVALID_FIELDS | 不写业务数据。 |
| 注入身份字段 | 400 IDENTITY_FIELD_FORBIDDEN | 不改变用户上下文、不写数据。 |
| 超文件或请求上限 | 413 FILE_TOO_LARGE | 不留下上传残留。 |
| 扩展名不支持 | 415 UNSUPPORTED_FILE_TYPE | 不写业务数据。 |
| UTF-8 解码失败、空白正文、NUL、受控解析失败 | 422 PARSE_FAILED | 清理已写暂存，不新增业务记录。 |
| 文件保存、移动或读取故障 | 500 STORAGE_ERROR | 不宣告入库成功，回滚并清理。 |
| 数据库写入或提交故障 | 500 DATABASE_ERROR | 双表事务回滚，移除该请求暂存和最终文件。 |

处理顺序：鉴权和校验 → 写入私有 `uploads/.staging/{uuid}` → 解析 → 开启 SQLite 事务 → 插入材料与全文 → 将暂存移动至同卷 `uploads/{class_id}/{uuid}.{ext}` → 提交事务 → 返回 201。材料列表仅查已提交 active 行。回滚时移除本次文件；数据库事务不能被描述成“同时原子回滚文件系统”。

对进程在移动文件与提交之间退出的窗口，启动恢复须在接收业务流量前清理 `.staging` 及无材料/清理任务引用的文件；已提交材料引用的文件必须保留。清理期间不并发接受上传。删除权限被阻止时不得静默报告恢复成功：进程保持未就绪并提示维护者恢复存储权限后重启。常规可处理失败必须在响应前完成清理；不可清理故障属于恢复验收范围，不能伪称“物理文件已删除”。

### D-07 标题修改与删除

修改标题只更新材料 title 和 updated_at。正文、上传者、班级、存储键、原文件名和原文件字节不变。提交同一标题允许 200，不新增知识记录；未知字段及尝试更新正文返回 400。

删除分两步，避免把文件系统和数据库假装成一个事务：

1. 在一个数据库事务内按 id/class/active 定位材料，标记 deleted、记录 deleted_at、删除关联知识行、插入 pending 文件清理任务。事务失败返回 500，原材料仍 active、正文与文件保留，不启动物理删除。
2. 提交后删除私有原文件；删除成功或文件已不存在时将任务置 done，返回 204。删除失败或完成状态写回失败返回 202 `{"material_id":id,"status":"deleted","cleanup_status":"pending"}`。此时列表、详情、下载均不可读取该材料；材料墓碑和 pending 任务保留用于恢复，不对用户暴露路径。

重复删除已 deleted 材料返回 404，不新增任务。清理任务按材料唯一，重复执行必须幂等。应用启动时重试 pending 任务；后续提供维护命令 `python scripts/cleanup_files.py --once` 重试 pending 任务。恢复文件权限后执行一次，应无 pending 任务且目标文件不存在；退出码 0 表示全部完成，非零表示仍有失败。该命令仅清理任务指向的已 deleted 材料，不用于在线扫描活跃上传目录。

逻辑删除不是回收站产品功能；不提供恢复端点。对 active 材料的原文件，不允许后台清理任务误删。

### D-08 部署与运行

- 后续创建 Dockerfile、`docker-compose.yml`、`.env.example`，服务名为 app，宿主机端口 8080。
- `./data:/app/data` 保存 SQLite 文件 `app.db`，`./uploads:/app/uploads` 保存暂存和原文件，均在容器重建后保留。
- 启动先检查 `SECRET_KEY`，空库才要求种子口令；完成建库、种子、启动恢复与待清理任务处理，再启动 Web。删除 pending 清理仍失败可保留任务并启动，因其业务资源已不可访问；无引用上传残留无法清理时必须启动失败。
- healthcheck 通过容器内 Python 标准库请求 `http://127.0.0.1:8080/health`，不依赖容器额外安装 curl。间隔 10 秒、超时 3 秒、启动宽限 30 秒、连续失败 3 次标记 unhealthy。
- `/health` 不依赖登录，执行只读 SQLite 探测并确认必需表可查询；正常 200，失败 503，不返回异常细节。它不代表每份材料均已完成物理清理。
- README 后续写入三行：价值为“按班级统一管理教学材料”；场景为“教师上传，本班师生读取，教师修改标题与删除”；不做项与 proposal 一致。
- README 后续提供 PowerShell `Copy-Item .env.example .env`、填写变量、`docker compose up --build -d`、登录 URL、测试账号名称及口令来源、health URL、清理命令和验收步骤。不得将真实口令写入 README。
- 持久化验收使用 `docker compose down` 后 `docker compose up -d`，不得删除数据卷或宿主机目录。重新初始化不能覆盖用户上传或复活已删除内容。

## Risks / Trade-offs

- 单库与单 worker 足以完成课程演示，不能据此宣称生产高并发或高可用。
- 服务端会话记录增加一张表，换取退出后重放旧 Cookie 也失效的可判定行为。
- 文件和 SQLite 无跨系统事务，因此分别规定常规回滚、启动恢复和删除重试，验收中须注入故障验证。
- UTF-8 文本和 10 MiB 是本规约的范围选择；PDF、OCR、向量化需另建变更。

## Migration Plan

从空应用起步，按 tasks 的数据 → 登录 → 隔离 → 上传 → 管理 → 部署顺序实现。本次不运行迁移、Compose、业务测试或归档。新增规范以变更目录保存，后续完成验收再 Archive。

## Open Questions

无阻塞项。来源未固定的参数、接口和错误语义已在本设计明确为 D1 默认决策；更改时必须同步 proposal/spec/tasks，不能仅在代码里改行为。
