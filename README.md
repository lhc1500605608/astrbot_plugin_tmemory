# astrbot_plugin_tmemory

`astrbot_plugin_tmemory` 是一个面向 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 的长期记忆插件。它会把对话写入本地 SQLite，按需通过 LLM 蒸馏成结构化长期记忆，并在后续请求前自动召回、注入相关记忆，让机器人在多轮、多会话、跨平台场景下持续理解用户偏好、事实、任务、限制和沟通风格。

> 当前版本：`v0.4.0`。插件仍在快速迭代，请在生产环境开启 WebUI、向量检索或跨账号合并前先备份数据库。

## 功能概览

- **自动采集**：监听用户消息，并可选采集助手回复，写入短期对话缓存。
- **LLM 蒸馏**：后台 worker 按用户批量处理未蒸馏缓存，生成结构化长期记忆。
- **主动记忆工具**：提供 `remember` / `recall` LLM tool，支持模型主动保存和检索记忆。
- **记忆注入**：在 `on_llm_request` 阶段召回相关记忆，注入到 `system_prompt` 或指定占位符。
- **混合检索**：默认使用 SQLite FTS5；可启用 `sqlite-vec` + Embedding 做向量召回与混合排序。
- **冲突与质量维护**：支持记忆强化、衰减、固定、提纯、合并、拆分和失活。
- **隐私保护**：内置敏感信息脱敏、群聊私聊记忆隔离、采集跳过规则和 no-memory 标记。
- **跨适配器身份合并**：通过 `canonical_user_id` 合并同一用户在不同平台的记忆。
- **WebUI 管理面板**：可选启用独立 Web 面板，支持记忆管理、思维导图、审计日志和手动蒸馏。

## 适用场景

- 需要机器人记住用户长期偏好，例如称呼、语言风格、兴趣、饮食禁忌。
- 需要沉淀跨会话事实，例如身份背景、长期项目、目标和待办。
- 需要在多个 AstrBot 适配器账号之间合并同一用户记忆。
- 需要本地优先、轻依赖的记忆存储方案，不希望引入外部向量数据库。

## 工作流程

```text
用户/助手消息
  ↓ 自动采集
conversation_cache 短期缓存
  ↓ 定时或手动蒸馏
memories 长期记忆
  ↓ FTS5 / sqlite-vec / rerank 召回
LLM 请求前注入记忆上下文
  ↓
模型生成更个性化的回复
```

插件有两条记忆写入路径：

1. **蒸馏路径**：对话先进入 `conversation_cache`，达到阈值后由后台 worker 调 LLM 批量蒸馏。
2. **主动工具路径**：模型在对话中调用 `remember(content, memory_type)` 直接保存重要记忆。

`memory_mode` 控制两条路径的启用方式：

| 模式 | 行为 |
|------|------|
| `hybrid` | 默认模式，同时启用后台蒸馏和主动工具记忆。 |
| `distill_only` | 仅使用缓存 + 蒸馏，禁用 `remember` 工具写入。 |
| `active_only` | 仅使用主动工具写入，后台蒸馏 worker 不自动蒸馏。 |

## 安装

### 方式一：AstrBot 插件市场 / 插件目录

将本仓库放入 AstrBot 插件目录，例如：

```bash
cd /path/to/AstrBot/data/plugins
git clone https://github.com/lhc1500605608/astrbot_plugin_tmemory.git
```

重启 AstrBot 后，在插件配置页按需调整配置。

### 方式二：手动安装依赖

插件依赖列在 `requirements.txt`：

```bash
pip install -r requirements.txt
```

主要依赖：

| 依赖 | 用途 |
|------|------|
| `aiohttp` | WebUI 独立 HTTP 服务。 |
| `jieba` | SQLite FTS5 中文分词辅助。 |
| `numpy` | 向量处理。 |
| `sqlite-vec` | 本地向量检索，启用向量检索时需要。 |

> 如果只使用默认 FTS5 检索，也建议保留 `sqlite-vec` 依赖安装；若目标环境无法安装，可关闭 `enable_vector_search`。

## 快速开始

1. 安装插件并重启 AstrBot。
2. 保持默认配置即可开始自动采集和注入记忆。
3. 与机器人对话累计到 `distill_min_batch_count` 条未蒸馏消息后，后台 worker 会在 `distill_interval_sec` 间隔内尝试蒸馏。
4. 管理员可执行 `/tm_distill_now` 立即触发一次蒸馏。
5. 使用 `/tm_memory` 查看当前用户记忆，使用 `/tm_context <问题>` 预览某个问题会召回哪些记忆。

推荐先用默认配置运行一段时间，再根据实际 token 成本和召回质量调整蒸馏、向量检索和注入参数。

## 配置说明

配置由 `_conf_schema.json` 和 `core/config.py` 定义。部分旧版平铺字段仍被兼容，但建议优先使用当前配置页展示的字段。

### 基础采集与注入

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `enable_auto_capture` | `true` | 是否自动采集用户消息。 |
| `capture_assistant_reply` | `true` | 是否采集助手回复。 |
| `capture_skip_prefixes` | `""` | 逗号分隔的跳过前缀；插件会额外内置跳过 `提醒 #`。 |
| `capture_skip_regex` | `""` | 跳过采集的正则表达式，配置错误会回退为空。 |
| `capture_min_content_len` | `5` | 低于该长度的消息不进入缓存。 |
| `capture_dedup_window` | `10` | 采集去重窗口，降低重复写入。 |
| `enable_memory_injection` | `true` | 是否在 LLM 请求前注入召回记忆。 |
| `inject_memory_limit` | `5` | 单次最多注入的记忆条数。 |
| `inject_max_chars` | `0` | 注入文本最大字符数；`0` 表示不额外限制。 |
| `inject_position` | `system_prompt` | 注入位置，默认追加到 `system_prompt`。 |
| `inject_slot_marker` | `{{tmemory}}` | 当使用占位符注入时，用该标记替换记忆块。 |
| `private_memory_in_group` | `false` | 群聊中是否注入用户私聊记忆；开启可能泄露隐私。 |
| `memory_scope` | `user` | 记忆作用域，影响用户、群聊和人格维度的隔离策略。 |
| `memory_mode` | `hybrid` | 记忆写入模式：`hybrid` / `distill_only` / `active_only`。 |

### 蒸馏与缓存

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `distill_interval_sec` | `17280` | 后台 worker 轮询间隔；代码最小限制为 4 小时。 |
| `distill_min_batch_count` | `20` | 单用户未蒸馏消息达到该数量才触发蒸馏；代码最小限制为 8。 |
| `distill_batch_limit` | `80` | 单次蒸馏最多处理的缓存消息数；代码最小限制为 20。 |
| `distill_pause` | `false` | 暂停后台自动蒸馏；手动命令仍可用于检查。 |
| `distill_user_throttle_sec` | `0` | 单用户蒸馏节流时间，避免高频重复蒸馏。 |
| `distill_model_settings.use_independent_distill_model` | `false` | 是否为蒸馏使用独立模型配置。 |
| `distill_model_settings.distill_provider_id` | `""` | 独立蒸馏 provider ID，留空使用当前 AstrBot provider。 |
| `distill_model_settings.distill_model_id` | `""` | 独立蒸馏模型 ID。 |
| `cache_max_rows` | `20` | 短期缓存超过阈值后保留的最近行数。 |
| `memory_max_chars` | `220` | 规则 fallback 生成单条记忆的最大字符数。 |

### 记忆提纯

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `purify_interval_days` | `0` | 自动提纯间隔天数；`0` 表示关闭。 |
| `purify_provider_id` | `""` | 提纯使用的 provider ID。 |
| `purify_model_id` | `""` | 提纯使用的模型 ID。 |
| `purify_min_score` | `0.0` | 规则层最低综合分阈值，低于阈值可失活。 |
| `manual_purify_default_mode` | `both` | `/tm_refine` 默认模式：`merge` / `split` / `both`。 |
| `manual_purify_default_limit` | `20` | `/tm_refine` 默认处理条数，最大限制为 200。 |

兼容旧字段：`refine_quality_interval_days`、`refine_quality_min_score`、`manual_refine_default_mode`、`manual_refine_default_limit`。

### 向量检索

当前推荐使用嵌套配置 `vector_retrieval`：

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `vector_retrieval.enable_vector_search` | `false` | 开启 `sqlite-vec` 向量检索。 |
| `vector_retrieval.embedding_provider` | `volc` | Embedding 提供商，可选 `volc` / `openai`。 |
| `vector_retrieval.embedding_api_key` | `""` | Embedding API Key。 |
| `vector_retrieval.embedding_model` | `doubao-embedding-vision-251215` | Embedding 模型名。OpenAI 兼容服务常用 `text-embedding-3-small`。 |
| `vector_retrieval.embedding_base_url` | `""` | 自定义 API Base URL。 |
| `vector_retrieval.vector_dim` | `2048` | Embedding 输出维度，必须与模型一致。 |
| `vector_retrieval.auto_rebuild_on_dim_change` | `true` | 维度变化时自动清空并重建向量索引。 |
| `vector_weight` | `0.4` | 混合检索中向量相似度权重。 |
| `min_vector_sim` | `0.15` | 向量候选最低相似度阈值。 |

注意事项：

- 首次开启向量检索后，建议执行 `/tm_vec_rebuild` 为已有记忆补向量。
- 变更 `vector_dim` 或模型后，需要重建向量索引。
- 向量检索不可用时，插件会继续使用 SQLite FTS5 检索。

### Reranker

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `enable_reranker` | `false` | 是否启用重排模型。 |
| `rerank_provider_id` | `""` | 重排 provider ID。 |
| `rerank_model_id` | `""` | 重排模型 ID。 |
| `rerank_top_n` | `5` | 重排后保留候选数。 |
| `rerank_base_url` | `""` | 重排服务 Base URL。 |

### WebUI

WebUI 默认关闭。必须设置 `webui_password` 后才建议对外启用。

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `webui_settings.webui_enabled` | `false` | 是否启动 WebUI 独立服务。 |
| `webui_settings.webui_host` | `0.0.0.0` | 监听地址；仅本机访问建议设为 `127.0.0.1`。 |
| `webui_settings.webui_port` | `9966` | 监听端口。 |
| `webui_settings.webui_username` | `admin` | 管理员用户名。 |
| `webui_settings.webui_password` | `""` | 管理员密码；留空不应暴露面板。 |
| `webui_settings.webui_ip_whitelist` | `""` | IP 白名单，逗号分隔，留空表示不限制。 |
| `webui_settings.webui_trust_proxy` | `false` | 是否信任 `X-Forwarded-For` / `X-Real-IP`。 |
| `webui_settings.webui_token_expire_hours` | `24` | 登录 token 有效期。 |

安全建议：

- 公网部署时使用反向代理和 HTTPS。
- 设置强密码，并尽量配置 IP 白名单。
- 只有在可信反向代理后面时才开启 `webui_trust_proxy`。

## 管理命令

以下命令均要求 AstrBot `ADMIN` 权限。

| 命令 | 说明 |
|------|------|
| `/tm_memory` | 查看当前用户的长期记忆列表。 |
| `/tm_context <问题>` | 预览某个问题会召回并注入的记忆上下文。 |
| `/tm_distill_now` | 手动触发一次批量蒸馏。 |
| `/tm_worker` | 查看后台蒸馏 worker 状态。 |
| `/tm_stats` | 查看全局统计，包括记忆、事件、待蒸馏行和向量索引。 |
| `/tm_distill_history` | 查看最近蒸馏历史和 token 成本摘要。 |
| `/tm_purify` | 手动触发一次全量记忆提纯。 |
| `/tm_quality_refine` | 兼容旧命令，等价于 `/tm_purify`。 |
| `/tm_refine mode=both limit=20 dry_run=false include_pinned=false <附加要求>` | 手动提纯已产生记忆，支持合并、拆分和 dry-run。 |
| `/tm_mem_merge <id1,id2,...> <合并后的记忆文本>` | 手动合并多条记忆，保留第一条 ID。 |
| `/tm_mem_split <id> [片段1|片段2|...]` | 手动拆分一条记忆；不提供片段时尝试调用 LLM 拆分。 |
| `/tm_forget <记忆ID>` | 删除指定记忆。 |
| `/tm_pin <记忆ID>` | 固定一条记忆，避免被衰减、剪枝或冲突覆盖。 |
| `/tm_unpin <记忆ID>` | 取消固定记忆。 |
| `/tm_export` | 导出当前用户全部记忆 JSON。 |
| `/tm_purge` | 删除当前用户全部记忆和缓存。 |
| `/tm_bind <canonical_id>` | 将当前账号绑定到统一用户 ID。 |
| `/tm_merge <from_id> <to_id>` | 合并两个统一用户 ID 的记忆。 |
| `/tm_vec_rebuild` | 重建向量索引。 |
| `/tm_vec_rebuild force=true` | 强制重建向量索引。 |

## LLM 工具

插件向模型暴露两个工具：

| 工具 | 参数 | 说明 |
|------|------|------|
| `remember` | `content`, `memory_type` | 保存一条长期记忆。`memory_type` 可为 `preference`、`fact`、`task`、`restriction`、`style`。 |
| `recall` | `query`, `limit` | 按查询检索当前用户相关记忆。 |

工具模式受 `memory_mode` 控制：`distill_only` 会禁用 `remember` 写入；`active_only` 会停止后台自动蒸馏。

## 记忆类型

| 类型 | 含义 | 示例 |
|------|------|------|
| `preference` | 用户偏好 | “用户喜欢简洁回答。” |
| `fact` | 用户事实 | “用户是一名 Python 开发者。” |
| `task` | 待办、计划、长期目标 | “用户正在准备一次产品发布。” |
| `restriction` | 约束、禁忌 | “不要向用户推荐含花生的食物。” |
| `style` | 沟通风格 | “用户希望先给结论再解释。” |

## 数据存储

默认数据库位置：

```text
data/plugin_data/astrbot_plugin_tmemory/tmemory.db
```

核心数据表：

| 表名 | 说明 |
|------|------|
| `identity_bindings` | `adapter + adapter_user_id → canonical_user_id` 身份映射。 |
| `conversation_cache` | 当前主要短期缓存表，保存待蒸馏消息。 |
| `conversations` | 历史兼容对话表。 |
| `memories` | 长期记忆表，包含类型、权重、置信度、作用域、固定状态等字段。 |
| `memories_fts` | SQLite FTS5 虚拟表，用于全文检索。 |
| `memory_vectors` | `sqlite-vec` 虚拟表，用于向量检索。 |
| `memory_events` | 审计事件表，记录绑定、合并、删除、蒸馏、WebUI 更新等操作。 |
| `distill_history` | 蒸馏历史和成本记录。 |

备份建议：在升级插件、开启向量维度变更、执行 `/tm_purge` 或跨用户 `/tm_merge` 前备份 `tmemory.db`。

## 隐私与安全

- 插件会对常见手机号、邮箱、身份证等敏感信息做脱敏后再存储。
- 其他插件或上游逻辑可在消息中加入 `\x00[astrbot:no-memory]\x00`，tmemory 会跳过该消息采集。
- 群聊默认不会注入私聊记忆；除非明确理解风险，否则不要开启 `private_memory_in_group`。
- WebUI 是独立端口服务；公网开放前必须设置密码、白名单和 HTTPS 反代。
- `/tm_export` 会输出用户记忆 JSON，请只在可信管理员上下文中使用。

## 成本控制建议

- 提高 `distill_min_batch_count` 可以减少 LLM 蒸馏频次，但会延迟长期记忆生成。
- 增大 `distill_interval_sec` 可以降低后台调用频率；默认约 4.8 小时一次。
- 使用 `distill_user_throttle_sec` 可避免活跃用户被频繁蒸馏。
- 开启 `active_only` 可完全依赖模型主动调用 `remember`，减少批量蒸馏成本，但要求模型工具调用质量足够稳定。
- `inject_memory_limit` 和 `inject_max_chars` 控制每次请求注入 token 量。
- 向量检索会产生 Embedding 调用成本；已有数据重建索引前先确认 API Key、模型和维度配置。

## 检索与评估

仓库提供离线评估样本：

| 文件 | 说明 |
|------|------|
| `eval/retrieval_samples.json` | 检索评估样本。 |
| `eval/retrieval_baseline.md` | 最近一次检索基线结果。 |
| `eval/distill_efficiency_samples.json` | 蒸馏效率样本。 |
| `eval/distill_efficiency_baseline.md` | 蒸馏效率基线。 |

README 中旧版命令曾提到 `retrieval_eval.py`，当前仓库未包含该脚本；如需运行评估，请先确认对应工具脚本是否已在你的工作区中提供。

## 开发与测试

本仓库测试配置见 `pytest.ini`，测试目录为 `tests/`。

常用命令：

```bash
python -m pytest
python -m pytest tests/test_plugin_baseline.py
python -m pytest tests/test_web_server.py
```

Docker / AstrBot 集成相关脚本：

| 文件 | 用途 |
|------|------|
| `docker-compose.yml` | 本地集成环境编排。 |
| `docker/astrbot_init.sh` | AstrBot 容器初始化辅助。 |
| `docker/e2e_verify.sh` | 端到端验证脚本。 |
| `tools/docker_test_env.sh` | Docker 测试环境辅助脚本。 |
| `tools/migrate_vectors.py` | 向量数据迁移辅助。 |
| `tools/download_bge_onnx.py` | 下载 BGE ONNX 相关资源。 |

## 常见问题

### 为什么没有立即生成长期记忆？

默认需要单用户未蒸馏消息达到 `distill_min_batch_count`，并等待后台 worker 到达 `distill_interval_sec` 周期。管理员可用 `/tm_distill_now` 手动触发。

### 为什么群聊里没有注入私聊记忆？

这是默认隐私保护。只有开启 `private_memory_in_group` 后，群聊才会注入该用户的私聊专属记忆。

### 开启向量检索后召回为空怎么办？

先确认 `sqlite-vec` 可用、Embedding API Key 有效、模型输出维度与 `vector_dim` 一致，然后执行 `/tm_vec_rebuild`。如果向量不可用，插件仍可回退到 FTS5。

### WebUI 打不开怎么办？

确认 `webui_enabled=true`，`webui_password` 已设置，端口未被占用，防火墙或反向代理允许访问。如果配置了白名单，请确认客户端 IP 命中规则。

### 如何彻底删除某个用户的数据？

在该用户上下文中使用 `/tm_purge`。执行前建议先 `/tm_export` 备份；删除后不可通过插件命令恢复。

## 维护状态

- 插件版本：`v0.4.0`
- Python：`3.10+`
- 存储：SQLite + FTS5，可选 `sqlite-vec`
- 主要入口：`main.py`
- 配置 schema：`_conf_schema.json`
- WebUI：`web_server.py` + `templates/`

欢迎提交 issue 或 PR 改进记忆质量、检索评估、WebUI 体验和 AstrBot 兼容性。
