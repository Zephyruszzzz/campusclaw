# CampusClaw 登录、权限、班级隔离与材料入库需求规约

## Purpose

本规格是本变更的业务验收依据。教师管理本班教学材料，学生只读本班材料，任何身份、角色或班级校验均由服务端执行；上传成功必须意味着原文件、材料元数据和解析全文均已持久化。需求来源与追加范围见 [proposal.md](../../proposal.md)，精确接口和技术方案见 [design.md](../../design.md)，实现任务见 [tasks.md](../../tasks.md)。

版本：1.0；日期：2026-09-18；状态：待实现、待业务验收。MUST/MUST NOT 表示强制要求；只有对应场景全部满足才判定通过。

## 验收基线与证据约定

1. 验收在独立测试数据目录运行，不在真实教学数据上注入异常。除专门的无状态、失效状态场景外，使用有效 Cookie，写请求携带有效 CSRF token；测试开始前确保没有无关并发请求。
2. 固定用户 `teacher_a`、`student_a1` 归属 A，`teacher_b`、`student_b1` 归属 B。两班样本标题分别为“A 班示例讲义”“B 班示例讲义”，正文含 `A_ONLY_SEED`、`B_ONLY_SEED`。测试 ID 从数据库或相应班级列表获取，不假定从 1 开始。
3. “业务数据不变”指 classes/users/materials/knowledge_entries 和原文件的业务字段、数量、内容不变；认证表中创建/撤销会话、维护清理任务等须按对应场景单独核对，不把认证变化误判为材料写入。
4. 每个场景取证三项：H 为脱敏 HTTP 请求/状态码/响应及必要页面截图；D 为数据库前后记录、关联和状态检查；F 为文件清单、字节数及 SHA-256 前后比较。未涉及文件变化时仍记录 F 不变；Cookie、口令和 CSRF token 在报告中遮盖。
5. 每个场景记录编号、关联需求/任务、执行时间、前置条件、实际结果、H/D/F 证据位置和“待执行/通过/失败/阻塞”。本文件不预填通过。参数化场景的每一行均须执行，任一行失败则该场景不通过。
6. 标题/正文/路径“不得泄露”包括成功或错误响应、页面和文件下载；只隐藏 UI 元素不能替代 API 验证。统一 404 为 `MATERIAL_NOT_FOUND`；学生写入统一 403 `ROLE_FORBIDDEN`。

## ADDED Requirements

### Requirement: R01 预置数据与六类核心实体

系统 MUST 建立班级、用户、讲义、作业、助手、技能六类实体，其中讲义由 materials 表承载；MUST 预置双班账号、可区分的材料、关联知识全文及原文件，MUST 支持安全的重复初始化。来源：S2；补充教师 B 及具体样本为 D1。

#### Scenario: AC01 首次初始化产生可验收双班数据

- **GIVEN** 空测试数据目录，必需密钥和四个种子口令已设置。
- **WHEN** 执行后续实现的初始化入口。
- **THEN** MUST 存在 A/B 两班及四个约定账号，其角色、班级匹配基线；两班各有一条 active 样本材料、一条非空全文记录及原文件。
- **AND** D MUST 显示材料、知识条目的 class_id 一致且上传者归属正确；F MUST 与解析正文对应；两班账号均能正常登录。
- **证据与任务：** H 登录响应，D 账号/材料/知识关联查询，F 两份原文件；T2.1、T2.2。

#### Scenario: AC02 重复初始化不得覆盖已有数据

- **GIVEN** 初始化完成，已有新增材料，且一份样本已按合法流程删除。
- **WHEN** 再次执行初始化，然后重启应用。
- **THEN** MUST 不重复建班、建用户、建样本，不重置已存密码哈希，不复活已删除材料；已有新增材料仍可读取。
- **AND** D MUST 保留原 ID、密码哈希、删除状态和关联；F MUST 不重建被删文件，也不覆盖上传文件。
- **证据与任务：** H 重启后列表，D 前后业务快照，F 前后哈希；T2.2。

#### Scenario: AC03 核心结构与外键有效

- **GIVEN** 已初始化的独立测试库。
- **WHEN** 检查表结构，并在可回滚测试事务内尝试将 A 班材料关联为 B 班知识记录。
- **THEN** 六类实体及会话、知识、清理任务结构 MUST 存在；作业、助手、技能允许空表；错误班级关联 MUST 被外键拒绝。
- **AND** 事务回滚后 D/F MUST 不变，不能为了通过测试保留非法行。
- **证据与任务：** H 不适用（直接库约束验证），D schema/外键错误/回滚查询，F 不变；T2.1。

### Requirement: R02 登录、认证与会话撤销

系统 MUST 支持教师和学生账号密码登录；MUST 通过签名 Cookie、可撤销会话及用户记录形成服务端身份上下文；无有效身份不得读取业务数据；退出后当前会话即使被复制重放也 MUST 失效。来源：S2；有效期和撤销机制为 D1。

#### Scenario: AC04 教师和学生正确登录

- **GIVEN** 四个预置账号及其正确口令，各自使用无登录态客户端取得登录表单 token。
- **WHEN** 分别提交正确 username/password 至 `POST /login`。
- **THEN** MUST 返回 302 到 `/materials`，创建不同新 sid；使用各自 Cookie 请求列表 MUST 为 200，身份角色和班级与 users 表一致。
- **AND** D MUST 新增有效 auth_sessions 记录，固定到期时间为登录后 8 小时；业务表和 F MUST 不变。
- **证据与任务：** H 登录/列表及脱敏 Cookie 属性，D sid 摘要关联及时间，F 不变；T3.1。

#### Scenario: AC05 错误凭据和缺少凭据

- **GIVEN** 未登录客户端及有效登录表单 token。
- **WHEN** 分别提交不存在用户名、存在用户名但错误密码、缺少用户名、缺少密码。
- **THEN** 前两项 MUST 返回相同的 401 `INVALID_CREDENTIALS` 和“账号或密码错误”；缺字段 MUST 返回 400 `INVALID_INPUT`；响应不含密码或哈希。
- **AND** D MUST 不新增有效认证会话，业务表/F 不变；随后 GET 受保护 API MUST 仍为 401。
- **证据与任务：** H 各错误响应和后续请求，D 有效会话数及业务快照，F 不变；T3.1。

#### Scenario: AC06 未登录访问页面和 API

- **GIVEN** 无 Cookie 的客户端；已知有效材料 ID。
- **WHEN** 请求 `/materials`，以及列表、详情、下载、上传、修改和删除各受保护 API。
- **THEN** 页面 MUST 返回 302 `/login`；所有已定义的受保护 API MUST 返回 401 `AUTH_REQUIRED`，不能执行上传解析或写入。
- **AND** 所有响应 MUST 无材料内容；D/F MUST 不变。
- **证据与任务：** H 逐接口响应，D 业务快照，F 清单；T3.2。

#### Scenario: AC07 过期、撤销和篡改会话均无效

- **GIVEN** 三组测试 Cookie：会话已超过 8 小时、对应记录已撤销、签名被修改。
- **WHEN** 分别请求 `/materials` 和 `/api/materials`。
- **THEN** 页面 MUST 为 302 `/login`，API MUST 为 401；仅修改 Cookie 中的角色或班级不得取得任何权限。
- **AND** D MUST 无新增有效会话或业务改动；F MUST 不变。
- **证据与任务：** H 六组响应，D 到期/撤销状态（测试时钟或测试库设置），F 不变；T3.2。

#### Scenario: AC08 退出后重放旧 Cookie 不恢复权限

- **GIVEN** 教师 A 有有效会话，在测试客户端保留一份退出前 Cookie。
- **WHEN** 使用有效 CSRF token 调用 `POST /logout`，再分别用浏览器当前 Cookie 和保留 Cookie 请求材料 API。
- **THEN** 退出 MUST 为 302 `/login` 且清除 Cookie；两次材料请求 MUST 均为 401。
- **AND** D MUST 将当前 sid 标记撤销，其他独立会话不被撤销，业务数据和 F 不变。
- **证据与任务：** H 退出及重放响应（不记录 Cookie 原文），D 会话撤销记录，F 不变；T3.3。

#### Scenario: AC09 已登录客户端重新登录失败

- **GIVEN** 教师 A 已登录并持有有效登录表单 token。
- **WHEN** 在同一客户端提交错误密码登录，然后重放旧 Cookie 请求列表 API。
- **THEN** 登录 MUST 返回 401，旧会话 MUST 已撤销；后续请求 MUST 为 401，不能继续保持教师权限。
- **AND** D MUST 无新有效会话，业务表/F 不变。
- **证据与任务：** H 登录和重放响应，D 会话前后状态，F 不变；T3.1、T3.3。

### Requirement: R03 服务端角色授权

系统 MUST 仅允许教师写入本班材料；学生 MUST 对本班材料只读。学生上传、修改和删除请求 MUST 在服务端返回 403，无业务数据或文件副作用。来源：S2；修改/删除为 U1。

#### Scenario: AC10 教师允许、本班学生可读

- **GIVEN** 教师 A 与学生 A1 的独立有效会话。
- **WHEN** 教师 A 按 AC16 上传合法材料，两人读取列表、详情和下载。
- **THEN** 上传 MUST 为 201，读取 MUST 为 200，下载字节与原文件相同；教师页面有管理入口，学生页面无可用写入口。
- **AND** D/F MUST 仅产生本次合法上传的增量，读取不得额外改变材料或文件。
- **证据与任务：** H 页面及 API，D 上传前后差量，F 哈希；T4.1、T5.2。

#### Scenario: AC11 学生绕过页面直接调用全部写接口

- **GIVEN** 学生 A1 有效会话，准备合法上传文件、本班材料 ID 和 B 班材料 ID。
- **WHEN** 分别上传，及对上述两类 ID 调用 PATCH、DELETE；再以缺少 CSRF token 的学生会话重复。
- **THEN** 每个请求 MUST 返回 403 `ROLE_FORBIDDEN`，优先于资源存在性、文件解析和写入；不能靠隐藏按钮通过本项。
- **AND** D MUST 无材料、知识、清理任务增删改；F MUST 无暂存、新文件、覆盖或删除。
- **证据与任务：** H 请求矩阵，D 全部相关表快照，F 前后清单；T4.1。

### Requirement: R04 班级隔离与资源不可绕过

系统 MUST 对材料和知识内容的列表、详情、下载、修改、删除在服务端按已验证用户班级过滤；跨班和不存在资源 MUST 统一 404；客户端身份字段不得改变授权。来源：S2；统一状态码等为 D1。

#### Scenario: AC12 双向列表隔离与查询来源

- **GIVEN** A/B 各有样本及师生会话。
- **WHEN** 四个账号分别请求页面和列表 API，并检查实际数据库查询条件。
- **THEN** 每份列表 MUST 仅含该用户所属班级的 active 记录，不出现他班标题；查询 MUST 有服务端班级过滤而不是返回全集后前端隐藏。
- **AND** D/F MUST 不变；在测试库修改本班材料标题后刷新，页面 MUST 显示新标题，证明列表查库。
- **证据与任务：** H 四账号结果及刷新，D 查询条件/受控改库记录，F 不变；T4.2。

#### Scenario: AC13 跨班 ID、不存在 ID 与下载隔离

- **GIVEN** A/B 师生会话，已知两班材料 ID、一个不存在 ID；教师写请求 token 有效。
- **WHEN** 两班互相请求对方材料的详情、下载；两班教师分别 PATCH/DELETE 对方材料；再对不存在 ID 执行同类请求。
- **THEN** 全部 MUST 返回同结构 404 `MATERIAL_NOT_FOUND`，无对方标题、正文、路径或存储键；学生写操作仍按 AC11 为 403。
- **AND** D MUST 无材料、知识、清理任务变化，F MUST 不变。
- **证据与任务：** H 对照矩阵，D 前后快照，F 哈希；T4.3。

#### Scenario: AC14 客户端不能选择班级或冒充角色

- **GIVEN** 教师 A 会话与合法上传文件，学生 A1 会话。
- **WHEN** 教师分别在列表 query、上传 form、标题修改 JSON 注入 `class_id=B`，并分别注入 role/user_id/uploaded_by；学生在上传 form 注入 `role=teacher`。
- **THEN** 教师请求 MUST 为 400 `IDENTITY_FIELD_FORBIDDEN`；学生请求 MUST 仍为 403；伪造签名 Cookie 按 AC07 处理。
- **AND** D/F MUST 不变，不得新增归属 B 的记录或改变 A 的会话身份。
- **证据与任务：** H 每种载体和字段的响应，D 归属与会话查询，F 清单；T4.2、T4.3。

#### Scenario: AC15 原文件不得通过静态路径公开

- **GIVEN** 已有上传文件，验收者从测试存储取得其真实存储键。
- **WHEN** 匿名客户端及 B 班用户尝试 `/uploads/{存储键}`、`/static/uploads/{存储键}`，并尝试受保护下载端点读取 A 班文件。
- **THEN** 两类静态地址 MUST 不提供文件，返回 404；受保护端点匿名为 401、他班为 404；公开 JSON MUST 无 storage_key/file_path。
- **AND** D/F MUST 不变；检查应用与 Compose MUST 未配置其他公开上传目录映射。
- **证据与任务：** H 访问响应，D/F 不变，路由与存储映射检查；T4.3。

### Requirement: R05 上传、解析与知识入库

教师上传合法文本后，系统 MUST 保留原文件字节并保存完整解析正文，以同班材料主键关联知识条目；全部就绪后才 MUST 返回 201。失败 MUST 不留下不完整业务记录，文件异常须按恢复协议处理。来源：S2；限制和具体状态码为 D1。

#### Scenario: AC16 txt 和 md 成功完成整条链路

- **GIVEN** 教师 A 会话，分别准备 UTF-8 `.txt`、`.md`，正文含 `A_UPLOAD_MARKER_001`，其中一份带 BOM。
- **WHEN** 逐份向上传 API 提交单文件；一份显式给 title，一份省略 title。
- **THEN** 每次 MUST 返回 201 和不同 material_id，status 为 active；标题分别为去首尾空白的输入及文件主名。
- **AND** D MUST 每次恰好增加一条 A 班材料和一条同班关联知识记录，body_text 是完整解码正文（去 BOM）；F MUST 每次增加一份字节完全相同的私有原文件，暂存已清理。
- **证据与任务：** H 上传/详情，D 双表关联与正文，F 大小和哈希；T5.1、T5.2。

#### Scenario: AC17 上传后刷新与学生只读

- **GIVEN** AC16 的上传已返回成功，A/B 师生均有会话。
- **WHEN** 教师 A、学生 A1、教师 B、学生 B1 分别刷新列表；学生 A1 再尝试修改新材料标题。
- **THEN** A 班两人 MUST 可见并可读新记录；B 班两人 MUST 不可见；学生写请求 MUST 为 403。
- **AND** D/F MUST 保持上传完成后的状态，不新增、覆盖或改动。
- **证据与任务：** H 四列表和拒绝响应，D 记录归属，F 不变；T5.2。

#### Scenario: AC18 同名文件与恶意文件名不覆盖

- **GIVEN** 教师 A 会话，两个同名文件的正文不同，另有带 `../` 或反斜线目录段的 filename。
- **WHEN** 分三次上传合法文本。
- **THEN** MUST 均返回 201，存储位置受服务端 UUID 与班级控制；路径段 MUST 被去除，不能写出 uploads 目录；同名材料按不同 ID 保存。
- **AND** D MUST 增加三组材料/知识记录；F MUST 增加三份互不覆盖文件，目录外无写入，先前文件哈希不变。
- **证据与任务：** H 三次返回，D ID/storage_key，F 路径归属和哈希；T5.1、T5.2。

#### Scenario: AC19 上传合法边界

- **GIVEN** 教师 A 会话，分别准备 1 字节非空白 UTF-8 文本和恰好 10,485,760 字节合法文本，请求总大小不超过 11 MiB。
- **WHEN** 分别上传，另以 `.TXT` 和 `.MD` 验证扩展名大小写。
- **THEN** 每次 MUST 返回 201；不能将 multipart 开销算入文件字节上限而错误拒绝。
- **AND** D MUST 正确记录 size_bytes 及完整解析正文；F MUST 与各原文件字节一致。
- **证据与任务：** H 边界请求，D 大小与全文长度，F 字节计数；T5.1。

#### Scenario: AC20 非法输入矩阵与固定错误

- **GIVEN** 教师 A 有效会话和 CSRF token，各子用例前记录 D/F 基线。
- **WHEN** 分别执行下表输入。
- **THEN** MUST 返回表中状态码/error.code，不得返回成功 material_id。
- **AND** 每个子用例后 D MUST 无材料/知识/清理任务新增，F MUST 无新原文件或暂存残留。
- **证据与任务：** H 全部子用例响应，D/F 差量为零；T5.1、T5.3。

| 输入 | 预期 HTTP | error.code |
| --- | --- | --- |
| 缺 file、同请求两个文件、0 字节文件 | 400 | INVALID_FILE |
| `.exe` 等非允许扩展名，即使伪造 text/plain | 415 | UNSUPPORTED_FILE_TYPE |
| 10,485,761 字节文件；或总请求大于 11 MiB | 413 | FILE_TOO_LARGE |
| 非 UTF-8、仅空白、含 NUL、受控解析失败 | 422 | PARSE_FAILED |
| 显式空白 title、201 个码点标题、标题含控制字符 | 400 | INVALID_TITLE |
| 上传未知字段或尝试传 material_id 覆盖已有文件 | 400 | INVALID_FIELDS |

#### Scenario: AC21 文件和数据库故障不产生部分成功

- **GIVEN** 教师 A 会话和合法文件，在独立环境分别注入暂存写失败、最终移动失败、知识记录写失败、事务提交失败；清理权限正常。
- **WHEN** 发起一次上传。
- **THEN** 文件故障 MUST 返回 500 `STORAGE_ERROR`，数据库故障 MUST 返回 500 `DATABASE_ERROR`；不得返回 201 或暴露堆栈。
- **AND** D MUST 保持上传前业务状态，不能只增加 materials；F MUST 无该请求暂存或最终文件，既有文件不变。
- **证据与任务：** H 故障响应，D 事务回滚查询，F 清理结果；T5.3。

#### Scenario: AC22 上传中断后启动恢复

- **GIVEN** 独立环境分别在写暂存后、移动最终文件但提交前终止进程；同时保留一份此前已提交的材料。仅在测试环境注入中断。
- **WHEN** 重启应用，先完成恢复再接收请求；再用阻止清理的权限故障重复一次。
- **THEN** 正常恢复 MUST 清理未提交残留并保留已提交材料；清理被阻止时 MUST 不启动为可用状态，恢复权限后再次启动 MUST 可完成恢复。
- **AND** D MUST 无部分提交的材料/知识组合；F MUST 无失联残留，已提交文件哈希不变。恢复失败期间不得虚报健康。
- **证据与任务：** H 恢复后的列表/健康检查，D 双表对照，F 暂存/最终文件及启动日志；T5.3、T7.1。

#### Scenario: AC23 文本展示不会执行脚本

- **GIVEN** 教师上传合法 UTF-8 文本，其中包含 HTML/script 字样，标题也含可显示的 HTML 字样。
- **WHEN** 本班用户读取页面、详情并下载。
- **THEN** 内容 MUST 作为文本展示，不能执行脚本；下载 MUST 为附件，字节保持原样；不能因扩展名为 md 自动执行 HTML。
- **AND** D MUST 保存原解析文字，F MUST 与上传字节一致，读取无额外写入。
- **证据与任务：** H 页面转义、下载头及浏览器无脚本执行证据，D 正文，F 哈希；T4.3、T5.2。

### Requirement: R06 教师修改本班材料标题

系统 MUST 允许教师仅修改本班 active 材料标题，标题去首尾空白后为 1～200 个 Unicode 码点且无控制字符；MUST NOT 借此替换正文、班级、上传者或原文件。来源：U1；输入约束为 D1。

#### Scenario: AC24 标题更新且正文和原文件不变

- **GIVEN** 教师 A 的 active 材料、学生 A1 会话和更新前 D/F 快照。
- **WHEN** 教师提交合法新标题，然后师生刷新列表和详情；再分别验证 1 字符、200 字符标题及重复提交同一标题。
- **THEN** PATCH MUST 为 200，师生读取 MUST 显示最终标题；前后空白须去除。
- **AND** D MUST 仅允许 title/updated_at 变化，知识记录数量及 body_text 不变；F MUST 哈希与数量不变。
- **证据与任务：** H PATCH/刷新，D 字段差异，F 哈希；T6.1。

#### Scenario: AC25 非法更新与数据库失败

- **GIVEN** 教师 A 的本班材料、有效 token、D/F 基线。
- **WHEN** 分别提交缺 title、空白 title、201 字符、控制字符、额外 body_text/file 字段；另对合法标题注入数据库提交失败。
- **THEN** 标题错误 MUST 为 400 `INVALID_TITLE`，未知字段 MUST 为 400 `INVALID_FIELDS`，数据库故障 MUST 为 500 `DATABASE_ERROR`。
- **AND** D/F MUST 完全保持操作前业务状态；学生和跨班教师更新另由 AC11/AC13 验证。
- **证据与任务：** H 错误矩阵，D 字段快照，F 不变；T6.1。

### Requirement: R07 教师删除及关联清理

系统 MUST 允许教师删除本班 active 材料；事务提交后材料和关联知识内容 MUST 立即不可读取。系统 MUST 清理原文件，清理失败 MUST 保留可重试任务并明确返回 pending，不能谎报已完成物理清理。来源：U1；状态和恢复协议为 D1。

#### Scenario: AC26 正常删除完成

- **GIVEN** 教师 A 的完整材料，文件可删除，本班学生有读取会话。
- **WHEN** 教师 DELETE 材料，再由师生请求列表、详情及下载。
- **THEN** DELETE MUST 为 204 空响应；列表 MUST 无该材料，详情/下载 MUST 为 404。
- **AND** D MUST 保留 deleted 材料墓碑、删除其知识记录、清理任务为 done；F MUST 删除对应原文件，其他材料不变。
- **证据与任务：** H DELETE 和后续读取，D 三表状态，F 目标不存在及其他文件哈希；T6.2。

#### Scenario: AC27 删除数据库事务失败

- **GIVEN** 本班 active 材料，注入删除事务提交失败，原文件正常。
- **WHEN** 教师 DELETE 后再次读取该材料。
- **THEN** DELETE MUST 为 500 `DATABASE_ERROR`；随后读取 MUST 仍为 200。
- **AND** D MUST 保留 active 材料和知识条目，不新增清理任务；F MUST 保留完整原文件，不允许先删文件再回滚数据库。
- **证据与任务：** H 错误与读取，D 回滚状态，F 哈希；T6.2。

#### Scenario: AC28 文件清理失败后重试

- **GIVEN** 本班 active 材料，在独立环境阻止目标文件删除，数据库可写。
- **WHEN** 教师 DELETE，随后恢复删除权限并运行 `python scripts/cleanup_files.py --once`；另对相同流程验证重启重试。
- **THEN** 首次 MUST 返回 202，body 为 material_id、status=deleted、cleanup_status=pending；即刻读取详情/下载 MUST 为 404；恢复后命令 MUST 退出 0，或启动处理完成。
- **AND** 首次 D MUST 是 deleted 墓碑、知识行已移除、唯一 pending 任务，F 可暂存该私有文件但不可访问；重试后 D MUST 为 done、F MUST 无目标文件。未恢复权限时重试 MUST 非零退出且保留 pending。
- **证据与任务：** H 202/404，D pending→done，F 删除前后与命令退出码；T6.3。

#### Scenario: AC29 重复删除与幂等清理

- **GIVEN** 分别准备已完成删除和 pending 删除的材料；另准备原文件已不存在但任务仍 pending 的故障恢复状态。
- **WHEN** 对前两者再次 DELETE；对第三者执行清理命令两次。
- **THEN** 再次 DELETE MUST 为 404，不新增任务；已不存在文件 MUST 被视作清理完成，两次命令均能安全结束。
- **AND** D MUST 不产生重复清理任务、任务最终为 done；F MUST 不误删任何 active 材料文件。
- **证据与任务：** H 重复请求，D 唯一任务及最终状态，F 其他文件哈希；T6.2、T6.3。

### Requirement: R08 密码、密钥与请求安全

系统 MUST 使用 bcrypt 存储和验证密码，MUST 禁止明文密码持久化；签名密钥仅来自服务端环境变量，缺失 MUST 启动失败；Cookie 和写请求 MUST 满足设计中的安全合同。来源：S2；具体参数及 CSRF 为 D1。

#### Scenario: AC30 密码哈希及种子口令边界

- **GIVEN** 正常初始化库及一套独立空库测试环境。
- **WHEN** 检查 password_hash，使用正确/错误口令验证；空库分别使用低于 8 或高于 72 UTF-8 字节的种子口令初始化。
- **THEN** 库中 MUST 为可验证的 bcrypt 哈希且成本至少 12，不等于明文；错误口令验证失败；非法长度 MUST 拒绝初始化而非截断。
- **AND** 正常库 D/F MUST 不变；非法初始化 MUST 不产生可用的部分用户/材料或样本文件；日志不得打印口令。
- **证据与任务：** H 登录验证，D 脱敏哈希格式与失败初始化快照，F 无残留，日志检查；T2.2、T3.1。

#### Scenario: AC31 密钥缺失和示例环境文件

- **GIVEN** 已准备有效持久化数据，分别删除或置空白 `SECRET_KEY`。
- **WHEN** 启动应用；再检查 `.env.example`、源码及版本控制文件。
- **THEN** 启动 MUST 非零退出且不监听业务端口；示例 MUST 列必需变量名，不含可投入使用的真实密钥或口令；源码 MUST 无签名密钥默认值。
- **AND** D/F MUST 不变，不能因启动失败重置数据。
- **证据与任务：** H 端口不可用，D/F 不变，退出码和配置审查；T3.1、T7.1。

#### Scenario: AC32 Cookie 属性和跨站请求保护

- **GIVEN** 登录成功客户端，本地 HTTP 与测试 HTTPS 配置各一套。
- **WHEN** 检查 Set-Cookie；教师对上传/PATCH/DELETE/退出分别发送缺失或错误 CSRF token；登录表单也执行缺 token 用例。
- **THEN** Cookie MUST 为 HttpOnly、SameSite=Lax、Path=/，HTTPS MUST 增加 Secure；缺失/错误 token MUST 为 403 `CSRF_INVALID`，教师合法 token 请求可按相应场景执行。
- **AND** 被拒请求 D MUST 不新增或撤销有效认证会话、不修改业务数据；F MUST 不变；学生写请求优先按 AC11 拒绝。
- **证据与任务：** H Cookie 属性及请求矩阵，D 会话/业务快照，F 不变；T3.2、T3.3。

### Requirement: R09 Compose、健康检查与持久化

系统 MUST 按 README 用 Docker Compose 启动；MUST 提供无需登录的 GET /health；MUST 持久化数据库和上传目录，保留卷重建容器后不丢失已提交数据。来源：S2；健康检查细节为 D1。

#### Scenario: AC33 从零按文档启动

- **GIVEN** 安装 Docker Compose 的验收环境、空测试数据目录和配置占位说明。
- **WHEN** 按 README 复制 `.env.example`、填必需变量并执行 `docker compose up --build -d`。
- **THEN** app MUST 达到 healthy，`http://localhost:8080/login` MUST 为 200；四个预置账号均可登录，初始化不依赖额外手动 SQL。
- **AND** D/F MUST 符合 AC01，均位于映射到宿主机的持久化目录，镜像中不包含真实 .env。
- **证据与任务：** H 登录页与登录，D/F 首次初始化，Compose 状态和挂载配置；T7.1、T7.3。

#### Scenario: AC34 匿名健康检查与数据库不可用

- **GIVEN** 已启动系统，匿名客户端不持有 Cookie。
- **WHEN** 请求 GET /health；然后在独立环境注入数据库不可查询故障再次请求。
- **THEN** 正常 MUST 返回 200 JSON `{"status":"ok"}`，不跳登录；故障 MUST 返回 503 `{"status":"unavailable"}`，不泄露路径或 SQL；连续故障达到配置次数后 Compose MUST 标记 unhealthy。
- **AND** 健康检查本身 MUST 不创建登录会话、不写业务数据；F MUST 不变。
- **证据与任务：** H 两种响应，D 只读探测及无新增会话，F 不变，healthcheck 状态；T7.2。

#### Scenario: AC35 容器重建保留上传、修改和删除结果

- **GIVEN** 已上传一份 A 班材料并改标题，另有一份已删除材料；记录账号哈希、材料/知识 ID 和原文件哈希。
- **WHEN** 执行 `docker compose down` 后 `docker compose up -d`，不删卷和宿主机目录，再重新登录查询。
- **THEN** 账号 MUST 仍能用原口令登录；未删除材料保留新标题、正文及下载字节；已删除材料 MUST 仍不可见、不可读取。
- **AND** D MUST 保留 ID/关联/删除状态且无重复种子，F MUST 保留合法原文件而不复活删除文件。
- **证据与任务：** H 重建后登录/读取/404，D 前后比较，F 哈希和目录，Compose 命令记录；T7.3。

### Requirement: R10 文档、规约和验收证据可追溯

交付 MUST 能从需求追踪至场景、实现任务和证据。后续 README MUST 包含价值、场景、不做三行说明及启动、配置、验证方法；所有业务实现任务在本次文档交付时 MUST 保持未完成。来源：S1/S2。

#### Scenario: AC36 四件套一致且不虚报完成

- **GIVEN** 本变更四份 Markdown 已编写完成。
- **WHEN** 独立审阅范围、接口、状态码、需求/场景/任务编号；运行 `openspec validate add-auth-rbac-class-knowledge --strict --no-interactive`。
- **THEN** MUST 有 proposal/design/spec/tasks，所有 Requirement 有 Scenario，全部 AC 有任务承接，复选框全部 `[ ]`；若 CLI 可用 MUST 校验退出 0。CLI 不可用时须明确标记“未执行”，不能伪称通过。
- **AND** 后续业务验收 MUST 留存逐场景 H/D/F 与结果，README 使用说明由 T7.3 完成；本次 D/F 业务状态不适用，因为未创建应用、数据库或原文件。
- **证据与任务：** H 不适用，Markdown 审阅、CLI 输出和后续验收记录；T1.1、T1.2、T8.1、T8.2。

## 验收执行与判定

建议执行顺序为 AC01–03 → AC30–31 → AC04–15/AC32 → AC16–23 → AC24–29 → AC33–35；AC36 在文档完成时执行，并在后续业务验收前复核。破坏性测试使用隔离环境，每个子用例恢复基线。

一条可接受的验收记录格式：

| 场景 | 关联任务 | 前置条件/输入 | 实际 HTTP | 数据库结果 | 文件结果 | 证据位置 | 结论 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| AC16 | T5.1/T5.2 | 待执行时填写 | 待执行 | 待执行 | 待执行 | 待填写 | 待执行 |

**放行条件：** AC01–AC35 的所有参数化子用例均通过，AC36 文档与格式符合要求，无未解决的跨班泄露、越权写入、密码/密钥泄露、部分入库成功或删除资源仍可读取问题。无法执行、阻塞或缺证据不能当作通过。
