# 会话生命周期：`/new` / `/reset` 语义（Plan TMEAAA-354 Phase 4a）

- Status: Implemented（TMEAAA-358）
- 关联：`core/session_lifecycle.py`、`adapters/conversation.py`、`adapters/version.py`、`search/retrieval.py`
- 上游依据：AstrBot 4.28.1 `/new` 与 `/reset` 统一为「开启新对话」；`ConversationManager.register_on_session_deleted` 提供级联清理回调；`filter.on_agent_begin` / `on_agent_done` 提供 Agent 运行观测。

## 1. 问题

`conversation_cache` 以 UMO（`platform:message_type:session_id`）作为 `session_key` 存储。用户在
`/new` `/reset` 后，AstrBot 会新建/删除对话，但插件此前**不会感知**该事件，导致：

- reset 后工作上下文（working turns）仍可能召回重置前的会话内容；
- 没有可配置的「归档 / 清空」语义，会话缓存与长期记忆边界不清。

## 2. 接入的上游钩子

| 钩子 | 上游路径 | 版本 | 降级 |
|------|----------|------|------|
| 会话删除 | `Context.conversation_manager.register_on_session_deleted(cb)` | 4.28+ | 缺失时跳过并在启动日志提示 |
| 会话轮转检测 | `Context.conversation_manager.get_curr_conversation_id(umo)` 变化 | 4.16+ | 失败返回空 → 跳过 |
| Agent 开始 | `filter.on_agent_begin()` → `(event, run_context)` | 4.28+ | `_optional_filter_hook` 退化为 no-op 装饰器 |
| Agent 完成 | `filter.on_agent_done()` → `(event, run_context, response)` | 4.28+ | 同上 |

能力探测集中在 `adapters/version.py`：`has_session_hooks()` / `has_agent_hooks()`，
并进入 `GET /api/capabilities` 的 `to_dict()` 快照。

### 上游事实校正（重要）

对照 AstrBot v4.28.1 源码：`/new` 与 `/reset` 由
`builtin_commands` 统一调用 `ConversationManager.new_conversation(...)`，
**不会**触发 `register_on_session_deleted`（该回调仅在
`delete_conversations_by_user_id` 即整会话删除时触发）。

因此本实现采用**双通道**：

1. `register_on_session_deleted` —— 覆盖整会话删除（如 Dashboard 清理会话）；
2. 对话 ID 变化检测（`on_any_message` 中调用 `_maybe_handle_session_rotation`）——
   覆盖 `/new` / `/reset`，且对 4.16–4.28 全版本可用（不依赖 4.28 钩子）。

首次见到某 UMO 仅建立基线，避免重启后误判。

## 3. 三态策略（`session_reset_policy`）

默认 `keep`。会话删除回调收到 UMO 后，按策略处理该 UMO 的 `conversation_cache` 行：

| 取值 | 数据动作 | 工作上下文注入 | 蒸馏资格 | 长期记忆 / 证据 |
|------|----------|----------------|----------|-----------------|
| `keep` | 无 | 保留 | 保留 | 保留 |
| `archive` | `UPDATE conversation_cache SET archived_at=?` | **不再召回**（`archived_at=''` 过滤） | 保留 | 保留 |
| `clear` | `DELETE`（排除被 `profile_item_evidence` / `episode_sources` 引用的行） | 不适用（行已删除） | 不适用 | 保留 |

设计要点：

- **长期记忆默认保留**：`profile_items`、蒸馏产物不随会话重置变化。
- `archive` 为软归档，可审计、可复原；仅影响 `retrieve_working_context`。
- `clear` 保护证据外键，避免 `profile_item_evidence` 悬空。
- 空 UMO / 非法策略均安全降级（不抛异常）。

## 4. 可观测性

- 会话删除（INFO）：
  `[tmemory] 会话删除观测 umo=<umo> policy=<policy> archived=<n> cleared=<n>`
- 会话轮转（INFO）：`[tmemory] 检测到会话轮转（/new /reset）umo=... cid=...→...`
- 钩子注册（INFO）：`[tmemory] 会话删除钩子已注册（session_reset_policy=...）` /
  `[tmemory] 会话删除钩子不可用（需 AstrBot >= 4.28 ...）`。
- Agent 运行（DEBUG）：`[tmemory] on_agent_begin umo=... total=...` /
  `[tmemory] on_agent_done umo=... total=...`。

## 5. 验证

```bash
.venv/bin/python -m pytest tests/test_session_lifecycle.py -q
.venv/bin/python -m pytest -q
```

- 回归用例：`tests/test_session_lifecycle.py`（配置解析、`archived_at` 列迁移、钩子注册、
  三态行为、日志观测、Agent 观测、能力快照）。
- 真实 AstrBot 4.16 / 4.23 / 4.28 契约矩阵由 T7（QA）覆盖。

## 6. 回滚

将 `session_reset_policy` 置回 `keep` 即可恢复旧行为（零数据迁移）。
