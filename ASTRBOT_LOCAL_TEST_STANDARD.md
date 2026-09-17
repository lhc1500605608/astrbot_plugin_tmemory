# AstrBot OpenAPI Local Test Standard

本文件固化 tmemory 插件对 AstrBot OpenAPI 的集成契约，作为 E2E smoke 测试 (`docker/e2e_verify.sh`) 的参照标准。

> **契约来源**：AstrBot ≥4.28 的实测契约，核对自运行中的 `soulter/astrbot:v4.28.0` 容器
> （源码 `dashboard/services/open_api_service.py`、`dashboard/schemas.py`、`dashboard/services/chat_service.py`，
> 实测输出见下文各步骤）。可执行参照为 `docker/e2e_verify.sh:107-141`。

## AstrBot 版本适用范围

| 契约项 | `< 4.28`（旧契约） | `>= 4.28`（现行契约） |
|--------|--------------------|------------------------|
| `/api/v1/chat` 请求体 | `message` + `session_id` | `message` **+ 必填 `username`** + 可选 `session_id` |
| `/api/v1/chat/sessions` | 无需 `username` 查询参数 | API key 鉴权时 **必填 `username`** 查询参数 |
| `/api/v1/im/message` 请求体 | `platform` + `user_id` + `message` | **`umo`** + `message`（旧字段报 `Missing key: umo`） |
| `/api/v1/chat` SSE 事件集 | v1 文档记录：`session_id` / `user_message_saved` / `plain` / `tool_call` / `tool_call_result` | 见下方「步骤 1 · SSE 事件类型表」 |
| `type=response` 事件 | 已移除（由 `plain` 逐片段替代） | 不存在 |

> 判定方法：`docker exec astrbot_tmemory_test python3 -c "import astrbot; print(astrbot.__version__)"`，
> 或在容器内读 `astrbot/core/config/default.py`。滚动 tag（`nightly-latest`）可能命中缓存旧镜像，请显式使用 `v4.28.0`。

## 前置条件

- AstrBot 实例可通过 `ASTRBOT_URL` 访问（默认 `http://localhost:6186`）
- `ASTRBOT_API_KEY` 已配置（默认 `admin`）
- 鉴权头：首选 `X-API-Key: {ASTRBOT_API_KEY}`；`Authorization: Bearer {ASTRBOT_API_KEY}` 亦被接受
  （`docs/astrbot-openapi.yaml:250-255`）

### API key 来源（≥4.28，易踩坑）

`dashboard.api_key` 在 AstrBot ≥4.28 **已不存在**（`dashboard` 配置段无该字段）。OpenAPI key 是
`data_v4.db` 中 `api_keys` 表的 PBKDF2 哈希记录（`astrbot/dashboard/services/api_key_service.py`），
通过 Dashboard 的 `POST /api/api-keys` 或 `POST /api/apikey/create` 创建。

- **自动预置**：`docker/astrbot_init.sh` 启动 AstrBot 后，后台运行
  `docker/seed_openapi_key.py`，等 `api_keys` 表建好后写入原始值 = `ASTRBOT_API_KEY`（默认
  `admin`）、scopes = `["*"]` 的 key。`docker compose up -d --force-recreate` 即可复现。
- **手动修复**（容器已在运行）：`ASTRBOT_API_KEY=admin bash docker/seed_openapi_key.sh`。
- **校验**：`curl -s -H "X-API-Key: admin" http://localhost:6186/api/v1/configs` 应返回
  `{"status":"ok",...}`。

> **坑位**：`api_keys.scopes` 是 JSON 列，必须是合法 JSON 数组。写入原始字符串 `*`（非 JSON）
> 会让 SQLAlchemy 反序列化抛 `Expecting value: line 1 column 1 (char 0)`，导致**所有**已鉴权路由
> 返回该 400，而 `X-API-Key` 本身其实已通过校验（缺 key 时报 `Missing API key`，key 错误时报
> `Invalid API key`）。`docker/seed_openapi_key.py` 会修复此状态。
- Docker 模式：`docker-compose up -d` 已启动 `astrbot_tmemory_test` 容器
- 本地模式：设置 `ASTRBOT_REQUIRE_DOCKER=0`
- **出网（≥4.28 实测约束）**：本地容器访问 `api.deepseek.com` 返回 `000`（`APIConnectionError`）。
  步骤 1 因此**只能断言 SSE 管线与事件帧**，不能断言 LLM 文本内容是否合理。

## 4 步 Smoke 验证

### 步骤 1: `/api/v1/chat` SSE 流

**请求**（≥4.28）:
```
POST {ASTRBOT_URL}/api/v1/chat
Headers:
  X-API-Key: {ASTRBOT_API_KEY}
  Content-Type: application/json
Body:
  {
    "message": "你好，请介绍一下你自己",
    "username": "tester",
    "session_id": "tmemory-smoke-{timestamp}",
    "enable_streaming": true
  }
```

- `username`（必填）：缺失时返回 `{"status":"error","message":"Missing key: username"}`（HTTP 200 错误体），
  源码 `dashboard/services/open_api_service.py:68-72`。
- `session_id`（可选）：省略时服务端生成 UUID。
- 其他可选字段：`selected_provider`、`selected_model`、`config_id`、`config_name`。

**SSE 事件类型表**（≥4.28 实测，帧格式 `data: {json}`，字段名 `type`）:

| 事件类型 | 含义 | 说明 |
|----------|------|------|
| `session_id` | 会话 ID 确认 | 首帧，含 `session_id` |
| `user_message_saved` | 用户消息已持久化 | `data.id` / `created_at` / `llm_checkpoint_id` |
| `run_started` | 本轮生成开始 | `data.run_id` |
| `plain` | LLM 文本输出片段 | 流式逐片段；LLM 失败时承载错误文本 |
| `agent_stats` | 运行统计 | `data.token_usage` / `current_context_tokens` / `time_to_first_token` |
| `complete` | 本轮输出完成 | 与 `plain` 配套 |
| `message_saved` | 机器人消息已持久化 | `data.id` / `created_at` |
| `end` | SSE 流结束 | 流终止信号 |

可能出现的附加事件：`run_snapshot`（加入进行中的 run）、`attachment_saved`、
`tool_call` / `tool_call_result`（工具调用）、`error`（异常）；以及保活心跳行。

**注意**: AstrBot v4.x 不再下发 `type=response` 事件；`plain` 已取代其逐片段输出。

**验证断言**（只断言 SSE 管线，不断言 LLM 文本）:
```bash
# 断言 SSE 流中包含 plain 事件帧
echo "$CHAT_RESPONSE" | grep -Eq 'data:.*"type"[[:space:]]*:[[:space:]]*"plain"'
```

实测样例（`session_id` → `user_message_saved` → `run_started` → `agent_stats` → `plain` → `complete` → `message_saved` → `end`）:
```
data: {"type": "session_id", "data": null, "session_id": "cto372-probe2"}
data: {"type": "run_started", "data": {"run_id": "..."}, "streaming": false, ...}
data: {"type": "agent_stats", "data": {"token_usage": {...}, ...}, ...}
data: {"type": "plain", "data": "LLM 响应错误: All chat models failed: APIConnectionError: Connection error.", ...}
data: {"type": "end", "data": "", "streaming": false, ...}
```

### 步骤 2: `/api/v1/chat/sessions` 会话列表

**请求**（≥4.28）:
```
GET {ASTRBOT_URL}/api/v1/chat/sessions?username={username}&page=1&page_size=20
Headers:
  X-API-Key: {ASTRBOT_API_KEY}
```

- API key 鉴权时 `username`（查询参数）**必填**；缺失时返回 `{"status":"error","message":"Missing key: username"}`。
- **预期**：`{"status":"ok","data":{"sessions":[...],"page":1,"page_size":20,"total":N}}`。
- `<4.28`：无需 `username` 查询参数。

### 步骤 3: `/api/v1/configs` 配置查询

**请求**:
```
GET {ASTRBOT_URL}/api/v1/configs
Headers:
  X-API-Key: {ASTRBOT_API_KEY}
```

**预期**: `{"status":"ok","data":{"configs":[...]}}`（4.28 实测返回 `configs` 数组）。

### 步骤 4: `/api/v1/im/message` 消息发送

**前置（≥4.28 实测约束）**：目标平台必须处于运行状态。
本地测试容器通常无运行 IM 平台：`GET /api/v1/im/bots` → `{"status":"ok","data":{"bot_ids":[]}}`。
此时步骤 4 **不应作为放行断言**：`umo` 调用会返回 HTTP 200 `{"data":{}}`，但消息不会实际投递。

**请求**（≥4.28）:
```
POST {ASTRBOT_URL}/api/v1/im/message
Headers:
  X-API-Key: {ASTRBOT_API_KEY}
  Content-Type: application/json
Body:
  {
    "umo": "webchat:FriendMessage:<session_id>",
    "message": "/tm_worker"
  }
```

- `umo` 格式：`{platform}:{message_type}:{session_id}`（如 `webchat:FriendMessage:openapi_probe`）。
- 旧字段 `platform` + `user_id` 在 ≥4.28 报 `{"status":"error","message":"Missing key: umo"}`（HTTP 400，`dashboard/schemas.py:219`）。
- **预期**（存在运行中 IM 平台时）：返回 200 OK，tmemory 插件已加载时可正确响应 `/tm_worker` 命令。
- `<4.28`：使用 `platform` + `user_id` 字段。

## 变更历史

| 日期 | 变更 | 原因 |
|------|------|------|
| 2026-05-11 | 初始版本 | TMEAAA-350: 固化现行 AstrBot v4.x SSE 事件类型，明确 `type=response` 不再下发 |
| 2026-09-17 | 对齐 AstrBot 4.28.0 契约 | TMEAAA-372（证据 TMEAAA-368）：补 `username`(chat body)/`umo`(im message)/`sessions?username`，补 SSE 事件类型表，补步骤 4 IM 平台前置与出网说明，标注适用版本范围（<4.28 / ≥4.28） |
| 2026-09-17 | 补 OpenAPI key 预置说明 | TMEAAA-374：≥4.28 无 `dashboard.api_key`，改为 `api_keys` 表；新增 init 自动预置 / 手动修复脚本，记录 scopes JSON 坑位 |
