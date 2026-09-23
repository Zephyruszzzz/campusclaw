# 问题三：后端（API）是否暴露给用户？如何通过浏览器直接访问后端接口？

> 适用代码版本：当前工作区 `campusClaw`（Flask + SQLite + 私有上传目录）。
> 结论均以源码为准，括号内为 `文件:行号`。

---

## 1. 是否暴露？——**是，与页面同源同端口**

API 就在同一个 Web 服务的同一个端口上，**对能访问该端口的人可见**：没有独立 API 域名、
没有 API Key、也没有「隐藏接口」设计。但「可达」不等于「可用」，访问控制靠下面四层，不靠藏起来：

| 维度 | 情况 |
| --- | --- |
| **网络可达性** | API 与页面同源同端口（默认 `0.0.0.0:8080`） |
| **访问控制** | **会话 Cookie + 角色 + CSRF + 班级隔离** |
| **匿名可访问的接口** | 只有 `GET /health`（`health_views.py:13-21`，返回 `{"status":"ok"}`） |
| **跨域调用** | 未开启 CORS，其他站点的 JS **读不到**接口返回 |
| **身份字段** | `class_id / role / user_id / uploaded_by` 一律以服务端会话为准，客户端传了直接 `400` |

---

## 2. 接口清单

| 方法 | 路径 | 权限 | 成功码 |
| --- | --- | --- | --- |
| GET | `/health` | 匿名 | 200 / 503 |
| GET | `/login`、`POST /login` | 匿名（有 CSRF 保护） | 302 |
| POST | `/logout` | 任意已登录 | 302 |
| GET | `/`、`/materials` | 已登录（未登录 302 跳登录页） | 200 |
| GET | `/api/materials/` | 已登录 | 200 |
| GET | `/api/materials/<id>` | 已登录 + 本班 | 200 |
| GET | `/api/materials/<id>/file` | 已登录 + 本班 | 200（附件） |
| POST | `/api/materials/upload` | 教师 + CSRF | 201 |
| PATCH | `/api/materials/<id>` | 教师 + CSRF | 200 |
| DELETE | `/api/materials/<id>` | 教师 + CSRF | 204 / 202 |

统一错误结构：`{"error":{"code":"...","message":"..."}}`（`errors.py:25-26`）。

**关键行为差异**：`/api/...` 与 `/health` 出错**永远返回 JSON**，不会被 302 重定向成登录页 HTML
（`request_context.py:133-145`）。也就是说，未登录访问 API 看到的是 `401` JSON，
未登录访问**页面**才会被重定向到 `/login`。

---

## 3. 用浏览器直接访问后端接口

> 核心前提：**GET 类接口靠 Cookie 认证，所以必须先在该浏览器里登录，再访问接口 URL。**

### 3.1 公开接口——地址栏直接敲

```
http://127.0.0.1:8080/health
```

无需登录，直接看到 `{"status":"ok"}`。

### 3.2 需登录的 GET 接口——先登录，再敲地址栏

先在 `http://127.0.0.1:8080/login` 登录（例如 `teacher_a` / `TeacherA#2026`），然后：

```
http://127.0.0.1:8080/api/materials/          ← 本班材料列表（JSON）
http://127.0.0.1:8080/api/materials/1         ← 材料详情 + 正文（JSON）
http://127.0.0.1:8080/api/materials/1/file    ← 直接触发浏览器下载（附件）
```

- 未登录时敲这些 URL，返回的是 `401` 的 JSON，**不会**跳登录页——这是有意设计
- 登录后同一浏览器带着会话 Cookie 再敲，就能看到 JSON / 触发下载
- 直接敲 `/api/materials/1/file` 是**唯一能真正触发「下载文件」**的浏览器方式
  （页面按钮走的是 fetch 预览，不会下载）

### 3.3 写操作（POST / PATCH / DELETE）——地址栏做不到

地址栏只能发 GET，且写接口需要 `X-CSRF-Token` 请求头。两种可用办法：

**办法一：F12 → Console**，在**已登录**状态下执行（token 从页面里取）：

```js
// 任意受保护页面里都有这个隐藏输入框
const csrf = document.querySelector('input[name=csrf_token]').value;

// 改标题
fetch('/api/materials/1', {
  method: 'PATCH',
  headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrf},
  body: JSON.stringify({title: '新标题'})
}).then(r => r.json()).then(console.log);

// 删除
fetch('/api/materials/1', {
  method: 'DELETE',
  headers: {'X-CSRF-Token': csrf}
}).then(r => console.log(r.status));
```

**办法二：curl**（自己带 Cookie 与 token）：

```bash
# 1) 登录页拿 csrf_token + 会话 Cookie
curl -i -c cookie.txt http://127.0.0.1:8080/login
# 2) 从登录页 HTML 里取出 csrf_token，然后登录
curl -i -b cookie.txt -c cookie.txt \
  -d 'username=teacher_a&password=TeacherA#2026&csrf_token=<TOKEN>' \
  http://127.0.0.1:8080/login
# 3) 带 Cookie 调受保护接口
curl -i -b cookie.txt http://127.0.0.1:8080/api/materials/
curl -i -b cookie.txt -o out.txt http://127.0.0.1:8080/api/materials/1/file
```

上传用页面表单最省事；脚本上传时带 `-F file=@x.txt -F csrf_token=<TOKEN>` 与会话 Cookie 即可。

### 3.4 需要留意的行为

- 未登录访问 `/api/...` 只会看到 `401` JSON，浏览器不会帮你跳登录页
- 会话 8 小时，过期后所有受保护接口回到 `401`，重新登录即可
- 开启 `AUTH_COOKIE_SECURE=1` 后，只有 HTTPS 才会下发 Cookie，纯 HTTP 访问会一直 401

---

## 4. 部署层面的提醒（安全相关）

- 默认 `SECRET_KEY` 与测试账号口令是**公开内置值**，仅供本机演示；对外部署务必设置
  `CAMPUSCLAW_STRICT_CONFIG=1` 并提供真实 `SECRET_KEY` 与 `SEED_*` 口令（否则进程拒绝启动）
- 服务默认监听 `0.0.0.0:8080`，即**同网段可访问**。要对外暴露应放在反向代理 / 内网之后，
  不要把带内置凭据的实例直接挂到公网

---

## 5. 小结

- **是否暴露**：是，与页面同源同端口，对用户可见；但受「会话 Cookie + 角色 + CSRF + 班级隔离」保护。
- **匿名可访问**：仅 `/health`。
- **浏览器直访**：**先登录**；GET 可直接敲地址栏（下载接口会触发下载），
  写操作需在 Console 或 curl 中带上 `X-CSRF-Token`。
