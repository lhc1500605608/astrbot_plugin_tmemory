---
name: tmemory
description: AstrBot 用户长期记忆管理 — 自动采集对话、LLM 蒸馏提取、向量检索召回、冲突检测消解、跨适配器身份合并。当需要记忆用户偏好/事实/任务/限制/风格，或检索历史记忆注入上下文时使用。
---

## 描述

TMemory (MemoryForge) 是 AstrBot 的用户长期记忆插件，为 LLM 会话提供持久化记忆能力。

## 核心能力

### 自动记忆采集
- 自动采集每条用户消息（`on_any_message` hook）
- 可选采集模型回复作为蒸馏素材（`on_llm_response` hook）
- 支持正则过滤敏感/无意义消息（`capture_filter_rules`）

### LLM 蒸馏
- 定时批量蒸馏（默认 5 分钟间隔），将原始对话提炼为结构化记忆
- 每批次限制 token 消耗（`distill_budget_tokens`）
- 自然衰减机制：记分随时间递减，低分记忆被剪枝
- 记忆提纯（purify）：检测并消解冲突记忆

### 向量检索与注入
- 在 LLM 请求前自动注入相关记忆到系统提示（`on_llm_request` hook）
- 使用 embedding 向量相似度检索，支持 SQLite-vec / Qdrant
- FTS5 全文搜索作为降级回退

### AI 工具模式
- `remember` 工具：LLM 主动保存用户重要信息
- `recall` 工具：LLM 主动检索记忆

### 跨适配器身份合并
- 支持将不同平台账号绑定到统一用户 ID
- 合并统一用户的记忆，去重冲突记忆

### 管理指令
- 18 个管理指令（`/tm_*`），覆盖蒸馏、检索、统计、导出、清理等操作
- 仅管理员可执行（`filter.permission_type(ADMIN)`）

## 使用边界

- **不负责**：本插件不处理短期对话记忆（AstrBot 的 conversation_cache 负责），仅管理长期持久化记忆。
- **不负责**：不提供 LLM 推理或对话生成能力，仅作为记忆存储和检索组件。
- **依赖**：需要上游 LLM provider 提供 embedding 模型（用于向量检索）和 chat 模型（用于蒸馏/提纯）。
- **成本**：蒸馏和提纯操作会消耗 LLM token，建议在生产环境配置 `distill_budget_tokens` 上限。
- **数据**：所有数据存储在插件目录下的 SQLite 数据库中，未提供自动云同步或备份。
- **兼容**：支持 AstrBot v4.16+，需要 Python 3.10+。

## 恢复与降级

- 向量索引不可用时自动降级为 FTS5 全文搜索
- 配置解析失败时使用安全默认值（`PluginConfig()`）
- LLM 调用失败时返回友好错误信息，不阻断对话主流程
