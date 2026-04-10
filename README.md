# astrbot_plugin_tmemory

AstrBot 记忆插件：把用户对话信息蒸馏为长期记忆，并支持跨适配器账号合并。

## 功能概览

- 用户记忆存储：基于 SQLite 存储长期记忆与短期上下文（默认落在 `data/plugin_data/astrbot_plugin_tmemory/`）。
- 记忆蒸馏：输入文本会被规范化、提取关键词并生成可复用记忆条目。
- 上下文总结优化：短期缓存超阈值时自动压缩，避免上下文无限膨胀。
- 用户合并记忆：通过统一 `canonical_user_id` 合并来自不同适配器的同一用户记忆。

## 数据表

- `identity_bindings`：`adapter + adapter_user_id -> canonical_user_id` 映射。
- `memories`：长期记忆库（含 `memory_type/importance/confidence/reinforce_count`）。
- `conversation_cache`：短期上下文缓存（可压缩）。

## 指令

- `/tm_remember <内容>`：手动写入一条记忆。
- `/tm_cache <内容>`：写入短期上下文缓存（用于后续总结）。
- `/tm_summary`：将最近上下文缓存蒸馏并写入长期记忆。
- `/tm_memory`：查看当前账号映射下的记忆。
- `/tm_context <问题>`：预览“召回后的记忆上下文块”。
- `/tm_bind <canonical_id>`：将当前账号绑定到统一用户 ID。
- `/tm_merge <from_id> <to_id>`：合并两个统一用户 ID 的记忆。
- `/tm_forget <memory_id>`：删除指定记忆。

## 使用建议

- 第一次使用时，先在每个平台执行 `/tm_bind <同一个ID>`，即可实现跨平台共享记忆。
- 你可以在业务流程中主动调用 `/tm_summary`（例如每轮对话结束后），提升上下文质量。
- 目前蒸馏策略为轻量规则版，后续可以替换为 LLM 版本以获得更高质量记忆。
- 插件已提供 `build_memory_context(canonical_user_id, query)`，可在模型调用前直接注入。

## 下一步可扩展

- 按群聊/私聊维度拆分记忆作用域。
- 增加记忆过期与遗忘曲线。
- 增加基于向量检索的召回层。

## WebUI 配置

本插件已提供 `_conf_schema.json`，可在 AstrBot WebUI 的插件配置页面直接调整参数。

主要配置项：

- `enable_auto_capture`：是否自动采集用户消息。
- `capture_assistant_reply`：是否采集助手回复。
- `enable_memory_injection`：是否在 LLM 请求前注入记忆到 `system_prompt`。
- `inject_memory_limit`：每次注入的记忆条数上限。
- `distill_interval_sec`：后台定时蒸馏间隔（秒）。
- `distill_min_batch_count`：达到最小批量才触发蒸馏，避免实时蒸馏浪费 token。
- `distill_batch_limit`：单次蒸馏最大处理条数。
- `distill_provider_id`：蒸馏使用的模型提供商（支持 WebUI 选择 provider）。
- `cache_max_rows`：会话缓存保留行数（超出后自动做规则摘要压缩）。
- `memory_max_chars`：规则蒸馏 fallback 的最大字符长度。
