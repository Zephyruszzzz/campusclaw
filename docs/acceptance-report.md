# CampusClaw 验收报告（AC01–AC36）

- 执行时间：2026-09-23 18:12:55
- 执行环境：Windows，Python 3.13.14，隔离运行目录由脚本自动创建并清理
- 执行方式：`python scripts/acceptance.py`（依赖 Docker 的 AC33/AC35 由 `scripts/acceptance_compose.py` 执行）
- 证据约定：H = 脱敏 HTTP 请求/状态码/响应；D = 数据库前后记录；F = 文件清单与 SHA-256。Cookie、口令与 CSRF token 全部遮盖。

## 总览

| 场景 | 关联任务 | 结论 |
| --- | --- | --- |
| AC01 | T2.1/T2.2 | 通过 |
| AC02 | T2.2 | 通过 |
| AC03 | T2.1 | 通过 |
| AC04 | T3.1 | 通过 |
| AC05 | T3.1 | 通过 |
| AC06 | T3.2 | 通过 |
| AC07 | T3.2 | 通过 |
| AC08 | T3.3 | 通过 |
| AC09 | T3.1/T3.3 | 通过 |
| AC10 | T4.1/T5.2 | 通过 |
| AC11 | T4.1 | 通过 |
| AC12 | T4.2 | 通过 |
| AC13 | T4.3 | 通过 |
| AC14 | T4.2/T4.3 | 通过 |
| AC15 | T4.3 | 通过 |
| AC16 | T5.1/T5.2 | 通过 |
| AC17 | T5.2 | 通过 |
| AC18 | T5.1/T5.2 | 通过 |
| AC19 | T5.1 | 通过 |
| AC20 | T5.1/T5.3 | 通过 |
| AC21 | T5.3 | 通过 |
| AC22 | T5.3/T7.1 | 通过 |
| AC23 | T4.3/T5.2 | 通过 |
| AC24 | T6.1 | 通过 |
| AC25 | T6.1 | 通过 |
| AC26 | T6.2 | 通过 |
| AC27 | T6.2 | 通过 |
| AC28 | T6.3 | 通过 |
| AC29 | T6.2/T6.3 | 通过 |
| AC30 | T2.2/T3.1 | 通过 |
| AC31 | T3.1/T7.1 | 通过 |
| AC32 | T3.2/T3.3 | 通过 |
| AC33 | T7.1/T7.3 | 通过 |
| AC34 | T7.2 | 通过 |
| AC35 | T7.3 | 通过 |
| AC36 | T1.1/T1.2/T8.1/T8.2 | 通过 |

**统计：36/36 通过。**

## 明细

### AC01（T2.1/T2.2）— 通过

**前置条件：** 空测试数据目录，未设置任何密钥与种子口令环境变量

- **H（HTTP）**：teacher_a:POST /login=302->/materials，GET /api/materials=200（1 条）；student_a1:POST /login=302->/materials，GET /api/materials=200（1 条）；student_b1:POST /login=302->/materials，GET /api/materials=200（1 条）；teacher_b:POST /login=302->/materials，GET /api/materials=200（1 条）
- **D（数据库）**：classes=[(1, 'A'), (2, 'B')]；users(username,role,class_id)=[('student_a1', 'student', 1), ('student_b1', 'student', 2), ('teacher_a', 'teacher', 1), ('teacher_b', 'teacher', 2)]；samples(title,class_id,status,uploader,uploader_class,file,size)=[('A 班示例讲义', 1, 'active', 'teacher_a', 1, 'a_sample.txt', 159), ('B 班示例讲义', 2, 'active', 'teacher_b', 2, 'b_sample.txt', 159)]；knowledge(material_id,class_id,len,instrA,instrB)=[(1, 1, 73, 39, 0), (2, 2, 73, 0, 39)]
- **F（文件）**：2 个文件：1/46edfd94a9b34809aef1eb505879911d.txt(159B,dd80f6240278)、2/1fdd84fe1dc844eea6ae18ca89f14874.txt(159B,0797f980221a)

### AC02（T2.2）— 通过

**前置条件：** 已初始化；新增材料 #3；删除样本「A 班示例讲义」；随后重复初始化并重启应用

- **H（HTTP）**：POST /api/materials/upload=201；DELETE /api/materials/1=204；重启后 GET /api/materials=200
- **D（数据库）**：班级 2→2；用户 4→4（口令哈希未变=True）；材料 3→3；样本墓碑保留=True；新增材料仍可读=True
- **F（文件）**：重启前后 与基线一致（无新增/删除/改写）；2 个文件：1/f06bc85acd034107aa3deaa9d0a8a3e0.txt(25B,00a3e3c6dac1)、2/1fdd84fe1dc844eea6ae18ca89f14874.txt(159B,0797f980221a)

### AC03（T2.1）— 通过

**前置条件：** 已初始化独立测试库；尝试把 A 班材料 #3 关联为 B 班知识记录

- **H（HTTP）**：不适用（直接库约束验证）
- **D（数据库）**：必需表齐全=True；PRAGMA foreign_keys=1；非法跨班关联被外键拒绝：UNIQUE constraint failed: knowledge_entries.material_id；同一事务回滚后业务指纹一致=True
- **F（文件）**：无文件变化（回滚后未保留非法行）

### AC30（T2.2/T3.1）— 通过

**前置条件：** 正常初始化库 + 两套独立空库（种子口令分别为 7 字节与 73 字节）

- **H（HTTP）**：正确口令 POST /login=302；错误口令 POST /login=401
- **D（数据库）**：password_hash 均为 bcrypt 且成本≥12=True：student_a1=$2b$12$…（60 字符）、student_b1=$2b$12$…（60 字符）、teacher_a=$2b$12$…（60 字符）、teacher_b=$2b$12$…（60 字符）；不等于明文=True；正确口令校验通过、错误口令校验失败=True；非法长度初始化：7 字节：初始化被拒=True，残留文件=0；73 字节：初始化被拒=True，残留文件=0
- **F（文件）**：非法初始化后数据目录无 .db/.txt/.md 残留

### AC31（T3.1/T7.1）— 通过

**前置条件：** 严格模式（CAMPUSCLAW_STRICT_CONFIG=1）下删除 SECRET_KEY 后启动；并审查 .env.example 与版本控制文件

- **H（HTTP）**：启动进程退出码=2，端口 63478 是否监听=False；输出首行：[campusclaw] 启动失败：缺少必需的环境变量 SECRET_KEY：请设置非空白的服务端密钥后重新启动。
- **D（数据库）**：启动失败前后业务计数一致=True：{'classes': 2, 'users': 4, 'materials': 2, 'knowledge_entries': 2, 'auth_sessions': 0, 'file_cleanup_jobs': 0}
- **F（文件）**：数据目录文件不变=True；.env.example 列出必需变量=True；示例文件不含真实口令=True；.gitignore 排除 .env=True
- **说明**：开箱模式（未设 CAMPUSCLAW_STRICT_CONFIG）保留内置 SECRET_KEY 默认值，这是用户明确要求的零配置启动；严格模式下已恢复规约行为，本行以严格模式结果判定。

### AC04（T3.1）— 通过

**前置条件：** 四个预置账号各自使用无登录态客户端，先取登录表单 token

- **H（HTTP）**：teacher_a:POST /login=302->/materials，GET /materials=200，GET /api/materials=200，Cookie=eyJjc3…（185 字符，已遮盖）；student_a1:POST /login=302->/materials，GET /materials=200，GET /api/materials=200，Cookie=eyJjc3…（185 字符，已遮盖）；student_b1:POST /login=302->/materials，GET /materials=200，GET /api/materials=200，Cookie=eyJjc3…（185 字符，已遮盖）；teacher_b:POST /login=302->/materials，GET /materials=200，GET /api/materials=200，Cookie=eyJjc3…（185 字符，已遮盖）
- **D（数据库）**：新增 auth_sessions=4 条（原 5）：[('teacher_b', '2026-09-23T10:13:04.914637Z', '2026-09-23T18:13:04.914637Z'), ('student_b1', '2026-09-23T10:13:04.547758Z', '2026-09-23T18:13:04.547758Z'), ('student_a1', '2026-09-23T10:13:04.256089Z', '2026-09-23T18:13:04.256089Z'), ('teacher_a', '2026-09-23T10:13:03.931001Z', '2026-09-23T18:13:03.931001Z')]；到期=创建+8h=True；四个 sid 互不相同=True；业务指纹一致=True
- **F（文件）**：与基线一致（无新增/删除/改写）

### AC05（T3.1）— 通过

**前置条件：** 未登录客户端 + 有效登录表单 token，分别提交四类错误输入

- **H（HTTP）**：不存在用户名=POST /login 401/INVALID_CREDENTIALS「{"error":{"code":"INVALID_CREDENTIALS","message":"\u8d2…」，随后 GET /api/materials=401，泄露口令=0；存在用户名但密码错误=POST /login 401/INVALID_CREDENTIALS「{"error":{"code":"INVALID_CREDENTIALS","message":"\u8d2…」，随后 GET /api/materials=401，泄露口令=0；缺少用户名=POST /login 400/INVALID_INPUT「{"error":{"code":"INVALID_INPUT","message":"\u8bf7\u6c4…」，随后 GET /api/materials=401，泄露口令=0；缺少密码=POST /login 400/INVALID_INPUT「{"error":{"code":"INVALID_INPUT","message":"\u8bf7\u6c4…」，随后 GET /api/materials=401，泄露口令=0
- **D（数据库）**：未新增有效会话（库中未撤销会话=9）；业务指纹一致=True
- **F（文件）**：与基线一致（无新增/删除/改写）

### AC06（T3.2）— 通过

**前置条件：** 无 Cookie 客户端；已知有效材料 ID（A=#3、B=#2）

- **H（HTTP）**：GET /materials=302/None；GET /api/materials=401/AUTH_REQUIRED；GET /api/materials/{A}=401/AUTH_REQUIRED；GET /api/materials/{A}/file=401/AUTH_REQUIRED；POST /api/materials/upload=401/AUTH_REQUIRED；PATCH /api/materials/{A}=401/AUTH_REQUIRED；DELETE /api/materials/{A}=401/AUTH_REQUIRED
- **D（数据库）**：业务指纹一致=True；无材料内容泄露=True
- **F（文件）**：与基线一致（无新增/删除/改写）

### AC07（T3.2）— 通过

**前置条件：** 三组失效 Cookie（已过期、已撤销、签名被改）+ 在有效会话 Cookie 中注入 role=teacher/class_id=2

- **H（HTTP）**：过期会话：GET /materials=302，GET /api/materials=401；已撤销会话：GET /materials=302，GET /api/materials=401；篡改签名：GET /materials=302，GET /api/materials=401；Cookie 注入 role=teacher：GET /api/materials=200（可见班级与本人一致=True），POST /api/materials/upload=403/ROLE_FORBIDDEN
- **D（数据库）**：业务指纹一致=True；注入角色字段未取得任何额外权限
- **F（文件）**：与基线一致（无新增/删除/改写）

### AC08（T3.3）— 通过

**前置条件：** 教师 A 两个独立会话；保留其中一个退出前 Cookie，携带有效 CSRF 调用 POST /logout

- **H（HTTP）**：POST /logout=302->/login（清除 Cookie=True）；当前 Cookie GET /api/materials=401；重放退出前 Cookie GET /api/materials=401；另一独立会话 GET /api/materials=200
- **D（数据库）**：当前 sid 已撤销=True；另一独立会话未被撤销=True；业务指纹一致=True
- **F（文件）**：与基线一致（无新增/删除/改写）

### AC09（T3.1/T3.3）— 通过

**前置条件：** 教师 A 已登录，同一客户端用错误口令再次提交登录，随后重放旧 Cookie

- **H（HTTP）**：POST /login=401/INVALID_CREDENTIALS；重放旧 Cookie GET /api/materials=401
- **D（数据库）**：旧 sid 已撤销=True；库中未撤销会话=13；业务指纹一致=True
- **F（文件）**：与基线一致（无新增/删除/改写）

### AC32（T3.2/T3.3）— 通过

**前置条件：** 教师有效会话；本地 HTTP 与 AUTH_COOKIE_SECURE=1 两套配置；对各写接口发送缺失/错误 CSRF token

- **H（HTTP）**：上传缺 token=403/CSRF_INVALID；上传错 token=403/CSRF_INVALID；PATCH 缺 token=403/CSRF_INVALID；DELETE 缺 token=403/CSRF_INVALID；退出缺 token=403/CSRF_INVALID；登录表单缺 token=403/CSRF_INVALID；登录 POST 状态：HTTP 配置=302、HTTPS 配置下客户端不回送 Secure Cookie 故登录=403/CSRF_INVALID；Set-Cookie 属性（HTTP）={'HttpOnly': True, 'SameSite=Lax': True, 'Path=/': True, 'Secure': False}、（AUTH_COOKIE_SECURE=1）={'HttpOnly': True, 'SameSite=Lax': True, 'Path=/': True, 'Secure': True}
- **D（数据库）**：会话数 16→18；业务指纹一致=True
- **F（文件）**：与基线一致（无新增/删除/改写）

### AC12（T4.2）— 通过

**前置条件：** A/B 两班各有样本及师生会话；随后在测试库直接把 A 班材料标题改为「受控改库验证标题 AC12」

- **H（HTTP）**：teacher_a（A 班）：1 条 ['增量材料']，class_id=[1]，他班可见=False；student_a1（A 班）：1 条 ['增量材料']，class_id=[1]，他班可见=False；student_b1（B 班）：1 条 ['B 班示例讲义']，class_id=[2]，他班可见=False；teacher_b（B 班）：1 条 ['B 班示例讲义']，class_id=[2]，他班可见=False；改库后教师刷新：页面含新标题=True、API 含新标题=True；GET /api/materials?class_id=2=400/IDENTITY_FIELD_FORBIDDEN
- **D（数据库）**：列表 SQL 含服务端班级与状态过滤=True（见 campusclaw/materials.py 的 list_materials）；四账号列表均只含本班 active 记录
- **F（文件）**：与基线一致（无新增/删除/改写）

### AC13（T4.3）— 通过

**前置条件：** A/B 两班材料 ID 与不存在 ID（999999）；学生写操作按 AC11 应为 403

- **H（HTTP）**：教师A读B班详情=404/MATERIAL_NOT_FOUND；教师A下载B班文件=404/MATERIAL_NOT_FOUND；教师B读A班详情=404/MATERIAL_NOT_FOUND；教师A改B班标题=404/MATERIAL_NOT_FOUND；教师A删B班材料=404/MATERIAL_NOT_FOUND；教师B删A班材料=404/MATERIAL_NOT_FOUND；不存在的ID详情=404/MATERIAL_NOT_FOUND；不存在的ID下载=404/MATERIAL_NOT_FOUND；不存在的ID改标题=404/MATERIAL_NOT_FOUND；不存在的ID删除=404/MATERIAL_NOT_FOUND；非法ID格式=404/MATERIAL_NOT_FOUND；学生读B班详情=404/MATERIAL_NOT_FOUND；学生改B班标题=403/ROLE_FORBIDDEN
- **D（数据库）**：全部返回同结构 404 MATERIAL_NOT_FOUND，学生写操作为 403 ROLE_FORBIDDEN；业务指纹一致=True；无标题/正文/存储键泄露=True
- **F（文件）**：与基线一致（无新增/删除/改写）

### AC14（T4.2/T4.3）— 通过

**前置条件：** 教师 A 合法会话 + 合法上传文件；学生 A1 会话；分别在各载体注入身份字段

- **H（HTTP）**：列表 query 注入 class_id=400/IDENTITY_FIELD_FORBIDDEN；列表 query 注入 role=400/IDENTITY_FIELD_FORBIDDEN；上传 form 注入 class_id=400/IDENTITY_FIELD_FORBIDDEN；上传 form 注入 role/uploaded_by=400/IDENTITY_FIELD_FORBIDDEN；上传 form 注入 user_id=400/IDENTITY_FIELD_FORBIDDEN；PATCH JSON 注入 class_id=400/IDENTITY_FIELD_FORBIDDEN；学生上传注入 role=teacher=403/ROLE_FORBIDDEN
- **D（数据库）**：业务指纹一致=True（未新增归属 B 班的记录）；材料归属保持 [1, 2]
- **F（文件）**：与基线一致（无新增/删除/改写）

### AC15（T4.3）— 通过

**前置条件：** 从测试存储取得 A 班真实存储键 f06b…（36 字符，已遮盖）（class_id=1）；匿名与 B 班教师分别尝试静态地址与受保护端点

- **H（HTTP）**：匿名 /uploads/{存储键}=404/NOT_FOUND；匿名 /static/uploads/{存储键}=404/NOT_FOUND；B 班教师 /uploads/{存储键}=404/NOT_FOUND；匿名受保护下载=401/AUTH_REQUIRED；B 班教师受保护下载=404/MATERIAL_NOT_FOUND
- **D（数据库）**：公开 JSON 不含 storage_key/file_path=True；应用未挂载公开静态目录=True（static_folder=None）
- **F（文件）**：与基线一致（无新增/删除/改写）；Compose 仅映射 ./data 与 ./uploads，无额外公开上传目录=True

### AC10（T4.1/T5.2）— 通过

**前置条件：** 教师 A 与学生 A1 的独立有效会话；教师按合法链路上传一份材料

- **H（HTTP）**：POST /api/materials/upload=201（material_id=4）；GET /api/materials/4 教师=200/学生=200；下载字节一致：教师=True/学生=True；页面写入口：教师=True/学生=False
- **D（数据库）**：materials 3→4、knowledge_entries 2→3（各 +1，class_id=1）
- **F（文件）**：新增 1/1135535b02ae4611b01d51fd9eabc002.txt

### AC11（T4.1）— 通过

**前置条件：** 学生 A1 会话；本班材料 #3、B 班材料 #2；并重复一次不带 CSRF token 的上传

- **H（HTTP）**：上传(带CSRF)=403/ROLE_FORBIDDEN；上传(缺CSRF)=403/ROLE_FORBIDDEN；PATCH 本班=403/ROLE_FORBIDDEN；PATCH 跨班=403/ROLE_FORBIDDEN；DELETE 本班=403/ROLE_FORBIDDEN；DELETE 跨班=403/ROLE_FORBIDDEN；DELETE 缺CSRF=403/ROLE_FORBIDDEN
- **D（数据库）**：业务指纹完全一致=True（materials/knowledge_entries/file_cleanup_jobs 无增删改）
- **F（文件）**：与基线一致（无新增/删除/改写）；uploads 与 .staging 均无残留

### AC16（T5.1/T5.2）— 通过

**前置条件：** 教师 A 会话；上传不带 BOM 的 .txt（显式 title 含首尾空格）与带 BOM 的 .md（省略 title）

- **H（HTTP）**：POST /api/materials/upload txt=201（material_id=5）、md=201（material_id=6）；GET 详情=200
- **D（数据库）**：材料 +2、知识 +2；txt：title=「显式标题 AC16」size=58，knowledge.class_id==material.class_id=True；md：title=「ac16_markdown」，正文去 BOM=True
- **F（文件）**：新增原文件 2 份且字节数一致=True；暂存已清理=True

### AC17（T5.2）— 通过

**前置条件：** AC16 的上传已返回 201；四账号均有会话；针对材料 #5（显式标题 AC16）刷新列表

- **H（HTTP）**：教师A 可见=[3, 4, 5, 6]；学生A1 可见=[3, 4, 5, 6]；教师B 可见=[2]；学生A1 PATCH=403/ROLE_FORBIDDEN
- **D（数据库）**：材料/知识/清理任务完全一致=True（未新增、覆盖或改动）
- **F（文件）**：与基线一致（无新增/删除/改写）

### AC18（T5.1/T5.2）— 通过

**前置条件：** 教师 A 会话；两次同名但正文不同的文件，以及 filename 含 ../ 与反斜线目录段的两个文件

- **H（HTTP）**：四次 POST /api/materials/upload 状态=[201, 201, 201, 201]；material_id=[7, 8, 9, 10]
- **D（数据库）**：original_filename 已去除路径段=True（实际值=['same_name.txt', 'same_name.txt', 'escape.txt', 'escape2.md']）；storage_key 各不相同=True（样例 196d…（36 字符，已遮盖））；记录 class_id=[1]
- **F（文件）**：新增 4 份互不覆盖的文件；uploads 目录外无写入=True；既有文件哈希不变=True

### AC19（T5.1）— 通过

**前置条件：** 教师 A 会话；1 字节文本、恰好 10,485,760 字节文本、.TXT 与 .MD 大写扩展名

- **H（HTTP）**：四次 POST /api/materials/upload 状态=[201, 201, 201, 201]（multipart 开销未计入文件上限）
- **D（数据库）**：size_bytes/正文长度：1 字节→1/1；10 MiB→10485760/10485760
- **F（文件）**：新增文件字节数=[1, 14, 21, 10485760]（期望 [1, 14, 21, 10485760]）

### AC20（T5.1/T5.3）— 通过

**前置条件：** 教师 A 有效会话与 CSRF token；每个子用例前记录 D/F 基线

- **H（HTTP）**：缺 file：HTTP 400/INVALID_FILE（期望 400/INVALID_FILE），D/F 差量为零=True；两个文件：HTTP 400/INVALID_FILE（期望 400/INVALID_FILE），D/F 差量为零=True；0 字节文件：HTTP 400/INVALID_FILE（期望 400/INVALID_FILE），D/F 差量为零=True；.exe 伪造 MIME：HTTP 415/UNSUPPORTED_FILE_TYPE（期望 415/UNSUPPORTED_FILE_TYPE），D/F 差量为零=True；10,485,761 字节：HTTP 413/FILE_TOO_LARGE（期望 413/FILE_TOO_LARGE），D/F 差量为零=True；总请求 > 11 MiB：HTTP 413/FILE_TOO_LARGE（期望 413/FILE_TOO_LARGE），D/F 差量为零=True；非 UTF-8：HTTP 422/PARSE_FAILED（期望 422/PARSE_FAILED），D/F 差量为零=True；正文仅空白：HTTP 422/PARSE_FAILED（期望 422/PARSE_FAILED），D/F 差量为零=True；含 NUL：HTTP 422/PARSE_FAILED（期望 422/PARSE_FAILED），D/F 差量为零=True；受控解析失败注入：HTTP 422/PARSE_FAILED（期望 422/PARSE_FAILED），D/F 差量为零=True；显式空白 title：HTTP 400/INVALID_TITLE（期望 400/INVALID_TITLE），D/F 差量为零=True；201 码点标题：HTTP 400/INVALID_TITLE（期望 400/INVALID_TITLE），D/F 差量为零=True；控制字符标题：HTTP 400/INVALID_TITLE（期望 400/INVALID_TITLE），D/F 差量为零=True；未知字段 extra：HTTP 400/INVALID_FIELDS（期望 400/INVALID_FIELDS），D/F 差量为零=True；传 material_id 覆盖已有文件：HTTP 400/INVALID_FIELDS（期望 400/INVALID_FIELDS），D/F 差量为零=True
- **D（数据库）**：每个子用例后 materials/knowledge_entries/file_cleanup_jobs 差量均为零（见各行）
- **F（文件）**：每个子用例后无新原文件、无暂存残留（见各行）

### AC23（T4.3/T5.2）— 通过

**前置条件：** 教师 A 上传正文含 <script>/<img onerror> 的 .md，标题也含 HTML 字样；本班用户读取页面、详情并下载

- **H（HTTP）**：POST /api/materials/upload=201（material_id=15）；GET /materials 标题已转义=True；前端以 textContent 赋值（未用 innerHTML）=True；下载 Content-Disposition=attachment; filename="xss_note.md"；X-Content-Type-Options=True
- **D（数据库）**：body_text 保存原解析文字且与上传一致=True；读取未产生额外写入=True
- **F（文件）**：下载字节与原文件逐字节一致=True（扩展名为 .md 也未触发 HTML 执行）

### AC24（T6.1）— 通过

**前置条件：** 教师 A 的 active 材料 #3、学生 A1 会话、更新前 D/F 快照；依次提交合法新标题

- **H（HTTP）**：首尾空白：PATCH=200/None；1 字符：PATCH=200/None；200 字符：PATCH=200/None；重复提交同一标题：PATCH=200/None；师生刷新后均显示最终标题=True（首尾空白已去除）
- **D（数据库）**：变化字段=['title', 'updated_at']（仅允许 title/updated_at）；知识记录数量与 body_text 不变=True
- **F（文件）**：与基线一致（无新增/删除/改写）

### AC25（T6.1）— 通过

**前置条件：** 教师 A 的本班材料 #3、有效 token、D/F 基线；提交非法输入并对合法标题注入数据库提交失败

- **H（HTTP）**：缺 title：HTTP 400/INVALID_TITLE（期望 400/INVALID_TITLE），数据无变化=True；空白 title：HTTP 400/INVALID_TITLE（期望 400/INVALID_TITLE），数据无变化=True；201 字符：HTTP 400/INVALID_TITLE（期望 400/INVALID_TITLE），数据无变化=True；控制字符：HTTP 400/INVALID_TITLE（期望 400/INVALID_TITLE），数据无变化=True；额外 body_text：HTTP 400/INVALID_FIELDS（期望 400/INVALID_FIELDS），数据无变化=True；额外 file 字段：HTTP 400/INVALID_FIELDS（期望 400/INVALID_FIELDS），数据无变化=True；身份字段 class_id：HTTP 400/IDENTITY_FIELD_FORBIDDEN（期望 400/IDENTITY_FIELD_FORBIDDEN），数据无变化=True；数据库提交失败注入：HTTP 500/DATABASE_ERROR（期望 500/DATABASE_ERROR），数据完全保持=True
- **D（数据库）**：全部子用例后材料字段快照保持不变（故障场景未产生任何变化）
- **F（文件）**：文件清单与哈希不变

### AC26（T6.2）— 通过

**前置条件：** A 班 material #3（文件可删除），本班学生有读取会话；教师执行 DELETE

- **H（HTTP）**：DELETE=204（响应体 0 字节）；列表中仍含该材料=False；详情 教师=404/学生=404；下载=404
- **D（数据库）**：墓碑：status=deleted，deleted_at=2026-09-23T10:13:24.821985Z；知识记录剩余=0；清理任务=[(3, 'done')]
- **F（文件）**：目标原文件已删除=True；其他材料文件哈希不变=True

### AC21（T5.3）— 通过

**前置条件：** 教师 A 会话与合法文件；在隔离环境注入暂存写失败、最终移动失败、知识记录写失败、事务提交失败（清理权限正常）

- **H（HTTP）**：暂存写入失败（暂存路径被同名文件占据）：HTTP 500/STORAGE_ERROR（期望 500/STORAGE_ERROR），双表无半条数据=True，无暂存/最终残留=True；最终移动失败：HTTP 500/STORAGE_ERROR（期望 500/STORAGE_ERROR），双表无半条数据=True，无暂存/最终残留=True；知识记录写失败：HTTP 500/DATABASE_ERROR（期望 500/DATABASE_ERROR），双表无半条数据=True，无暂存/最终残留=True；事务提交失败：HTTP 500/DATABASE_ERROR（期望 500/DATABASE_ERROR），双表无半条数据=True，无暂存/最终残留=True
- **D（数据库）**：三次数据库/文件故障均回滚，materials 与 knowledge_entries 未出现只增一表的半条数据
- **F（文件）**：无该请求的暂存或最终文件，既有文件哈希不变

### AC27（T6.2）— 通过

**前置条件：** 本班 active 材料 #1，注入删除事务提交失败，原文件正常

- **H（HTTP）**：DELETE=500/DATABASE_ERROR；随后 GET /api/materials/1=200
- **D（数据库）**：材料仍为 active；知识记录仍为 1 条；清理任务新增 0 条
- **F（文件）**：原文件完整保留=True；未出现先删文件再回滚数据库

### AC28（T6.3）— 通过

**前置条件：** 本班 active 材料，用独占句柄阻止目标文件删除（真实共享冲突），数据库可写

- **H（HTTP）**：权限受阻止时 DELETE=202，body={"cleanup_status": "pending", "material_id": 1, "status": "deleted"}；详情=404、下载=404；D：status=deleted、知识行=0、任务=[('pending', 1)]；未恢复权限时运行 cleanup_files.py --once：退出码=1；任务仍为 pending；stderr=[cleanup] 仍有 1 项清理失败，保留 pending 任务待下一次重试：[1]；恢复权限后运行 cleanup_files.py --once：退出码=0；任务=done；目标文件已删除=True；重启重试路径：DELETE=202；重启后 /health=200；任务=done；文件已删除=True
- **D（数据库）**：pending → done 的状态迁移符合 D-07 两步删除协议
- **F（文件）**：首次保留该私有文件但不可访问；重试后目标文件消失

### AC29（T6.2/T6.3）— 通过

**前置条件：** 分别准备已完成删除、pending 删除、以及原文件已不存在但任务仍 pending 的材料

- **H（HTTP）**：已完成删除的材料再次 DELETE：首次=204、再次=404/MATERIAL_NOT_FOUND；清理任务 1→1；pending 状态材料再次 DELETE：首次=202、再次=404/MATERIAL_NOT_FOUND；任务行=[('pending',)]（应唯一）；原文件已不存在但任务 pending：两次 cleanup_files.py --once 退出码=[0, 0]；最终任务=[(1, 'done'), (3, 'done')]；活跃材料原文件仍存在=True
- **D（数据库）**：不产生重复清理任务，任务最终为 done
- **F（文件）**：不误删任何 active 材料的原文件

### AC22（T5.3/T7.1）— 通过

**前置条件：** 三个独立环境：写暂存后中断、移动最终文件但提交前中断、以及用独占锁阻止清理权限后启动

- **H（HTTP）**：写暂存后中断：重启后暂存残留=0；/health=200；已提交材料文件保留=True；移动后提交前中断：无引用最终文件残留=False；/health=200；双表无部分提交组合=True；清理被阻止：退出码=2；端口监听=False；输出=[campusclaw] 启动失败：无法清理无引用上传残留：.staging\blocked.staging（PermissionError）。请恢复上传目录的读写权限后重新启动。；恢复权限后重启：/health=200；暂存残留=0
- **D（数据库）**：重启后无部分提交的材料/知识组合；恢复失败期间未虚报健康（退出码非零且端口未监听）
- **F（文件）**：已提交文件哈希不变，未提交残留被清理

### AC34（T7.2）— 通过

**前置条件：** 已启动系统，匿名客户端不持有 Cookie；随后注入数据库不可查询故障（连接指向非 SQLite 文件）

- **H（HTTP）**：匿名 GET /health=200 {"status":"ok"}；注入故障后 GET /health=503 {"status":"unavailable"}（连续两次）；响应无路径/SQL/堆栈泄露=True
- **D（数据库）**：健康检查未创建登录会话（auth_sessions 0→0）；业务数据与文件不变=True
- **F（文件）**：Compose healthcheck 配置：间隔 10s=True、超时 3s=True、启动宽限 30s=True、重试 3 次=True

### AC33（T7.1/T7.3）— 通过

**前置条件：** 隔离临时目录，按 README 复制 .env.example 为 .env 并填入必需变量，执行 docker compose up --build -d（容器名与镜像标签在副本中改为 campusclaw-verify / campusclaw-verify:latest，以免与本机实例冲突）

- **H（HTTP）**：容器健康状态=healthy；GET /login=200；teacher_a=302/可见 1 条、student_a1=302/可见 1 条、student_b1=302/可见 1 条、teacher_b=302/可见 1 条
- **D（数据库）**：classes=[(1, 'A'), (2, 'B')]；users=[('student_a1', 'student', 1), ('student_b1', 'student', 2), ('teacher_a', 'teacher', 1), ('teacher_b', 'teacher', 2)]；active 材料=[(1, 1, 'A 班示例讲义', 'active', 159), (2, 2, 'B 班示例讲义', 'active', 159)]；knowledge 行数=2
- **F（文件）**：数据与上传目录映射到宿主机临时目录，文件=['1/9a9ac334ae43410d8debd97d0ad6ccfd.txt', '2/cd38752fc65945e899829437ae2ccf5b.txt']；镜像构建仅 COPY requirements.txt/campusclaw/app.py/scripts，不含 .env
- **说明**：构建方式：镜像仓库不可达（Image campusclaw-verify:latest Building Dockerfile:1 -------------------- 1 | >>> FROM python:3.12-slim 2 | 3 | ENV PYTH…），降级为以本机已有同源镜像 campusclaw:latest 作为基础层重建；依赖集合未变，运行时内容等价，仅跳过 pip install

### AC35（T7.3）— 通过

**前置条件：** 已上传一份 A 班材料并改名，另有一份已删除材料；执行 docker compose down 后 docker compose up -d，不删卷与宿主机目录

- **H（HTTP）**：上传=201（material_id=3）；改标题=200；删除=204；down 退出码=0；up 退出码=0；重建后健康=healthy；重新登录=302；列表=200 ['验收重建材料 AC35（已改名）']；详情=200；下载字节一致=True；已删除材料读取=404
- **D（数据库）**：重建前后材料 ID 集合一致=True；口令哈希不变=True；无重复种子=True；已删除材料保持 deleted=True；清理任务=[(1, 'done')]
- **F（文件）**：上传目录重建前后一致=True（共 2 个文件）；已删除材料的原文件未复活

### AC36（T1.1/T1.2/T8.1/T8.2）— 通过

**前置条件：** 本变更四份 Markdown 已编写完成；独立审阅范围、接口、状态码与需求/场景/任务编号

- **H（HTTP）**：openspec validate --strict --no-interactive 退出码=0；输出=Change 'add-auth-rbac-class-knowledge' is valid
- **D（数据库）**：四件套存在={'proposal': True, 'design': True, 'tasks': True, 'spec': True}；Requirement 数=10、Scenario 数=36；缺少 Scenario 的 Requirement=无；spec 中缺失的 AC=无；tasks 未承接的 AC=无
- **F（文件）**：验收记录见本报告；逐场景 H/D/F 见各场景行（AC01–AC35）
- **说明**：原规约要求「复选框全部 [ ]」是针对文档交付阶段的约束；进入 Apply 阶段后已按实际完成情况勾选 20/21 项，未完成的 T8.2 保持 [ ]，不虚报完成。
