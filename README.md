# astrbot_plugin_tmemory

AstrBot 用户长期记忆插件：自动采集对话、定时 LLM 结构化蒸馏、冲突检测更新、跨适配器账号合并。

## 核心功能

- **自动消息采集**：每条用户/助手消息自动写入短期缓存，无需手动操作。
- **定时 LLM 蒸馏**：后台定时对积累的对话批量蒸馏，输出结构化记忆（类型/重要度/置信度）。
- **记忆注入**：LLM 调用前自动将相关记忆注入 `system_prompt`，让模型"记住"用户偏好。
- **冲突检测**：新记忆与旧记忆高度重叠时，自动标记旧记忆失效，避免矛盾堆积。
- **记忆强化/衰减**：被召回的记忆权重自动提升；长期未命中的记忆自动降权或归档。
- **敏感信息脱敏**：手机号、邮箱、身份证等自动替换为占位符再存储。
- **跨适配器合并**：同一用户在不同平台的记忆可合并到统一 `canonical_user_id`。
- **WebUI 面板**：可选启用独立 Web 管理界面，支持登录鉴权与 IP 白名单。

## 数据表

| 表名 | 说明 |
|------|------|
| `identity_bindings` | `adapter + adapter_user_id → canonical_user_id` 映射 |
| `memories` | 长期记忆库（含 `memory_type/importance/confidence/reinforce_count/is_active/is_pinned`） |
| `conversation_cache` | 短期对话缓存（蒸馏后标记，超阈值自动压缩） |
| `memory_events` | 审计日志（绑定/合并/删除/蒸馏等关键事件） |
| `distill_history` | 蒸馏历史记录（触发类型/处理用户数/生成记忆数/耗时） |

## 记忆类型

| 类型 | 含义 |
|------|------|
| `preference` | 用户偏好（喜欢/讨厌/习惯） |
| `fact` | 用户事实信息（职业/身份/背景） |
| `task` | 待办/计划/长期目标 |
| `restriction` | 约束/禁忌（不要做某事） |
| `style` | 沟通风格（简洁/详细/语气） |

## 指令（均需 ADMIN 权限）

| 指令 | 说明 |
|------|------|
| `/tm_memory` | 查看当前用户的记忆列表 |
| `/tm_context <问题>` | 预览当前问题触发的记忆召回上下文 |
| `/tm_forget <记忆ID>` | 删除指定记忆 |
| `/tm_pin <记忆ID>` | 常驻一条记忆（不会被衰减/冲突覆盖） |
| `/tm_unpin <记忆ID>` | 取消常驻 |
| `/tm_export` | 导出当前用户所有记忆（JSON） |
| `/tm_purge` | 删除当前用户所有记忆和缓存 |
| `/tm_bind <canonical_id>` | 将当前账号绑定到统一用户 ID |
| `/tm_merge <from_id> <to_id>` | 合并两个统一用户 ID 的记忆 |
| `/tm_distill_now` | 手动触发一次批量蒸馏 |
| `/tm_worker` | 查看蒸馏 Worker 运行状态 |
| `/tm_stats` | 查看全局统计（记忆数/事件数/待蒸馏行数/向量索引行数） |
| `/tm_vec_rebuild` | 为已有记忆补全向量索引（仅向量检索开启时可用） |

## 配置项

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `enable_auto_capture` | `true` | 是否自动采集用户消息 |
| `capture_assistant_reply` | `true` | 是否同时采集助手回复 |
| `capture_skip_prefixes` | `""` | 跳过采集的消息前缀（逗号分隔） |
| `capture_skip_regex` | `""` | 跳过采集的消息正则（高级） |
| `enable_memory_injection` | `true` | 是否在 LLM 调用前注入记忆 |
| `inject_memory_limit` | `5` | 每次注入的最大记忆条数 |
| `distill_interval_sec` | `17280` | 后台蒸馏间隔（秒），最小 4 小时 |
| `distill_min_batch_count` | `20` | 触发蒸馏的最小未蒸馏消息数 |
| `distill_batch_limit` | `80` | 单次蒸馏最大处理条数 |
| `distill_pause` | `false` | 暂停后台自动蒸馏 |
| `cache_max_rows` | `20` | 短期缓存保留行数（超出后规则压缩） |
| `memory_max_chars` | `220` | 规则蒸馏 fallback 的最大字符数 |
| `enable_vector_search` | `false` | 启用轻量化向量检索（基于 `sqlite-vec` 扩展，无需部署外部数据库） |
| `embed_provider_base_url` | `""` | OpenAI 兼容的 Embedding API 地址（如 `https://api.openai.com`） |
| `embed_provider_api_key` | `""` | Embedding API key |
| `embed_model` | `text-embedding-3-small` | Embedding 模型名 |
| `embed_dim` | `1536` | 向量维度（须与模型一致，如 nomic-embed-text=768） |
| `vector_weight` | `0.4` | 混合检索中向量相似度的权重（0~1） |
| `min_vector_sim` | `0.15` | 向量候选最低相似度阈值，低于阈值的结果会被过滤 |
| `refine_quality_interval_days` | `0` | 自动提馆间隔天数（0=关闭） |
| `refine_quality_min_score` | `0.0` | 提馆规则层最低综合分阈值（低于阈值直接失活） |
| `manual_refine_default_mode` | `both` | 手动精馏默认模式（`merge`/`split`/`both`） |
| `manual_refine_default_limit` | `20` | 手动精馏默认处理条数 |

### WebUI 配置

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `webui_enabled` | `false` | 是否启用 WebUI 面板 |
| `webui_host` | `0.0.0.0` | WebUI 监听地址 |
| `webui_port` | `9966` | WebUI 监听端口 |
| `webui_username` | `admin` | 登录用户名 |
| `webui_password` | `""` | 登录密码（留空则禁用登录） |
| `webui_trust_proxy` | `false` | 信任反向代理 X-Forwarded-For |
| `webui_ip_whitelist` | `""` | IP 白名单（逗号分隔，留空不限制） |
| `webui_token_expire_hours` | `24` | JWT token 有效期（小时） |

## 内部实现架构简述
自版本升级后，移除了所有厚重的 FAISS / Elasticsearch / Langchain 依赖，实现了纯本地化存储与检索：
- **向量检索 (KNN)**：基于 `sqlite-vec` 扩展，使用 `memory_vectors` 虚拟表进行高维向量空间 L2 距离召回。
- **全文检索 (FTS5)**：基于 SQLite 内置的 `FTS5` 引擎，配合 `jieba` 中文分词，通过数据库触发器实现与主记忆表的实时同步。
- **混合重排 (RRF)**：在 Python 侧实现了 `Reciprocal Rank Fusion` (RRF) 算法，将双路召回结果平滑融合，再叠加记忆的新鲜度、重要性等属性，提供极为精确的语义上下文注入。
- **存储方案**：仅依赖单个 SQLite `.db` 文件，启用 `WAL` 模式保证高并发情况下的读写性能。

## 使用建议

- **跨平台共享**：在每个平台执行 `/tm_bind <同一个ID>`，即可实现跨平台共享记忆。
- **手动蒸馏**：如果刚导入大量历史，可用 `/tm_distill_now` 立即触发蒸馏，无需等定时器。
- **常驻重要记忆**：对不想被覆盖的关键记忆使用 `/tm_pin`。
- **跳过采集**：其他插件可在消息中嵌入 `\x00[astrbot:no-memory]\x00` 标记，tmemory 会自动跳过该消息的采集。
- **向量检索**：安装 `pip install sqlite-vec` 后，在配置中填写 Embedding API 信息并开启 `enable_vector_search`。对已有记忆可用 `/tm_vec_rebuild` 一次性补全向量索引；之后每次蒸馏会自动为新记忆生成向量。由于采用混合检索，即使不开启向量，系统也将自动采用基于 FTS5 的倒排索引。

## 离线检索评估

- 评估样本位于 `eval/retrieval_samples.json`，采用人工可读、可维护的 `query + memory` 格式。
- 运行基线命令：`python retrieval_eval.py eval/retrieval_samples.json eval/retrieval_baseline.md`
- 当前首版基线聚焦 FTS 词面召回，输出 `Recall@K` 作为后续检索改动的回归对照。
- `eval/retrieval_baseline.md` 是最近一次运行结果，可直接用于前后版本对比。
- 该基线不代表真实线上效果，也不代表外部 embedding / 向量召回质量；样本规模与查询复杂度都刻意保持很小，只用于离线回归验证。

## 数据存储位置

默认落在 `data/plugin_data/astrbot_plugin_tmemory/tmemory.db`，符合 AstrBot 插件数据存储规范。
