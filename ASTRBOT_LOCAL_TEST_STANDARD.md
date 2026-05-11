# AstrBot OpenAPI Local Test Standard

本文件固化 tmemory 插件对 AstrBot OpenAPI 的集成契约，作为 E2E smoke 测试 (`docker/e2e_verify.sh`) 的参照标准。

## 前置条件

- AstrBot 实例可通过 `ASTRBOT_URL` 访问（默认 `http://localhost:6186`）
- `ASTRBOT_API_KEY` 已配置（默认 `admin`）
- Docker 模式：`docker-compose up -d` 已启动 `astrbot_tmemory_test` 容器
- 本地模式：设置 `ASTRBOT_REQUIRE_DOCKER=0`

## 4 步 Smoke 验证

### 步骤 1: `/api/v1/chat` SSE 流

**请求**:
```
POST {ASTRBOT_URL}/api/v1/chat
Headers:
  Authorization: Bearer {ASTRBOT_API_KEY}
  Content-Type: application/json
Body:
  {
    "message": "你好，请介绍一下你自己",
    "session_id": "tmemory-smoke-{timestamp}"
  }
```

**预期 SSE 事件类型**（AstrBot v4.x 现行协议）:
- `session_id` — 会话 ID 确认
- `user_message_saved` — 用户消息已持久化
- `plain` — LLM 文本输出片段
- `tool_call` — 工具调用（如有）
- `tool_call_result` — 工具调用结果（如有）

**注意**: AstrBot v4.x 不再下发 `type=response` 事件。历史版本中的该事件已被 `plain` 逐片段输出替代。E2E smoke 断言兼容 `plain` 和 `response` 两种类型以保持向后兼容。

**验证断言**:
```bash
# 断言 SSE 流中包含有效的 type 字段
echo "$CHAT_RESPONSE" | grep -Eq 'data:.*"type"[[:space:]]*:[[:space:]]*"(plain|response)"'
```

### 步骤 2: `/api/v1/chat/sessions` 会话列表

**请求**:
```
GET {ASTRBOT_URL}/api/v1/chat/sessions
Headers:
  Authorization: Bearer {ASTRBOT_API_KEY}
```

**预期**: 返回 JSON 数组，包含步骤 1 创建的 session_id。

### 步骤 3: `/api/v1/configs` 配置查询

**请求**:
```
GET {ASTRBOT_URL}/api/v1/configs
Headers:
  Authorization: Bearer {ASTRBOT_API_KEY}
```

**预期**: 返回 JSON 对象，包含 AstrBot 当前配置。

### 步骤 4: `/api/v1/im/message` 消息发送

**请求**:
```
POST {ASTRBOT_URL}/api/v1/im/message
Headers:
  Authorization: Bearer {ASTRBOT_API_KEY}
  Content-Type: application/json
Body:
  {
    "platform": "ai-chat",
    "user_id": "tmemory-smoke",
    "message": "/tm_worker"
  }
```

**预期**: 返回 200 OK，tmemory 插件已加载时可正确响应 `/tm_worker` 命令。

## 变更历史

| 日期 | 变更 | 原因 |
|------|------|------|
| 2026-05-11 | 初始版本 | TMEAAA-350: 固化现行 AstrBot v4.x SSE 事件类型，明确 `type=response` 不再下发 |
