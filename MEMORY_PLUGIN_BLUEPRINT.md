# AstrBot 记忆插件蓝图（调研 + 计划）

更新时间：2026-04-08

## 1. 需求对齐

目标：实现一个 AstrBot 记忆插件，具备以下能力：

- 将多轮对话沉淀为长期记忆并持久化存储。
- 对对话进行精简提炼（蒸馏），降低上下文冗余。
- 做短期上下文压缩与总结优化。
- 支持“同一用户跨适配器账号”的统一身份与记忆合并。

## 2. GitHub 同类项目调研结论

### 2.1 参考项目（高相关）

1. mem0ai/mem0  
   - 关键点：强调多层记忆（User/Session/Agent）、记忆检索后再拼接 prompt、低 token 开销。  
   - 对我们可复用：分层记忆模型、`add -> search -> inject prompt` 的标准闭环。

2. memodb-io/memobase  
   - 关键点：强调 “Memory for User, not Agent”、时间感知事件（event timeline）、批处理缓冲降低成本。  
   - 对我们可复用：以“用户画像 + 事件流”为核心，而不是仅堆对话文本；异步批处理蒸馏。

3. CaviraOSS/OpenMemory  
   - 关键点：local-first、SQLite/Postgres、多记忆分区（episodic/semantic/...）、衰减与强化、可解释召回。  
   - 对我们可复用：本地优先部署、记忆衰减模型、召回打分可解释化。

4. getzep/zep  
   - 关键点：上下文工程（context engineering）、时间图谱、关系感知检索、低延迟上下文组装。  
   - 对我们可复用：将“记忆存储”和“上下文组装”解耦，做可配置的上下文块。

5. uezo/chatmemory  
   - 关键点：极简实现 + PostgreSQL + pgvector，支持按 channel 管理跨渠道历史，支持 session 切换后总结。  
   - 对我们可复用：channel 维度隔离/共享策略、会话切换触发总结。

### 2.2 AstrBot 官方约束

- 官方建议插件大文件存储在 `data/plugin_data/{plugin_name}/`；插件内也可用简单 KV（版本 >= 4.9.2）。
- 结论：生产环境建议把 `tmemory.db` 放到插件数据目录，不放源码目录。

## 3. 目标架构蓝图

## 3.1 逻辑分层

1. Ingest（采集层）
- 采集每轮 user/assistant 消息。
- 标准化消息结构：`adapter`, `channel`, `adapter_user_id`, `role`, `content`, `timestamp`。

2. Identity（身份层）
- 维护 `adapter_user_id -> canonical_user_id` 映射。
- 提供绑定、冲突检测、批量迁移、审计日志。

3. Distill（蒸馏层）
- 轻量规则蒸馏（当前可用）+ LLM 蒸馏（后续可插拔）。
- 抽取偏好、事实、任务状态、禁忌信息、长期目标等结构化记忆。

4. Store（存储层）
- 短期上下文缓存（conversation cache）。
- 长期记忆库（memory facts/events）。
- 向量索引（`sqlite-vec` 扩展）与倒排全文检索 (`SQLite FTS5`)。

5. Retrieve（召回层）
- 混合召回：向量检索 (KNN) + 倒排搜索 (FTS5) -> `Reciprocal Rank Fusion` (RRF) 融合。
- 排序分数：RRF 基础语义相关度 + 时效性 + 重要性 + 置信度 + 强化次数。

6. Compose（上下文组装层）
- 把召回结果装配成固定模板上下文块注入模型。
- 控制 token 预算，超长时做二次压缩。

## 3.2 数据模型（建议）

1. `identity_bindings`
- `adapter`
- `adapter_user_id`
- `canonical_user_id`
- `verified_level`（manual/auto）
- `updated_at`

2. `conversation_cache`
- `canonical_user_id`
- `channel`
- `session_id`
- `role`
- `content`
- `created_at`

3. `memories`
- `canonical_user_id`
- `memory_type`（preference/fact/task/style/restriction）
- `memory`
- `memory_hash`
- `importance`
- `confidence`
- `last_seen_at`
- `reinforce_count`
- `source_adapter`
- `source_channel`

4. `memory_events`（可选，但建议）
- `canonical_user_id`
- `event_type`（created/updated/merged/forgotten）
- `payload_json`
- `created_at`

## 4. 核心流程设计

## 4.1 写入流程（每轮对话）

1. 解析并确定 `canonical_user_id`。
2. 写入 `conversation_cache`。
3. 触发蒸馏策略：
- 实时轻量蒸馏（低延迟）
- 或异步批处理蒸馏（低成本）
4. 对蒸馏结果去重（`memory_hash`）并 upsert 到 `memories`。
5. 若 cache 超阈值，生成 summary 并裁剪旧上下文。

## 4.2 召回流程（模型调用前）

1. 输入：当前 query + `canonical_user_id` + channel。
2. 从 `memories` 中检索 Top-K（规则检索起步）。
3. 时间衰减重排：近期且高 importance 优先。
4. 组装上下文块：
- `User Profile`
- `Recent Session Summary`
- `Relevant Long-Term Memories`
5. 注入系统提示词。

## 4.3 跨适配器合并流程

1. 用户执行绑定：`/tm_bind <canonical_id>`。
2. 管理员执行迁移/合并：`/tm_merge <from> <to>`。
3. 数据迁移：`memories`, `conversation_cache`, `identity_bindings`。
4. 去重冲突处理：按 `memory_hash` 去重，保留更高 `importance/confidence`。
5. 记录审计事件：`memory_events`。

## 5. 蒸馏策略路线

## 5.1 v1（立即可用）

- 规则蒸馏：关键词 + 句子截断 + 去重。
- 分类规则：
  - “喜欢/偏好/习惯” -> `preference`
  - “计划/待办/提醒” -> `task`
  - “不要/禁忌” -> `restriction`

## 5.2 v2（建议尽快）

- LLM 结构化蒸馏，输出 JSON：
  - `memory_type`, `memory_text`, `importance`, `confidence`, `expires_at`。
- 引入反事实更新：新记忆与旧记忆冲突时标记旧记忆失效（参考 Zep/Memobase 的时间演进思想）。

## 5.3 v3（增强）

- 向量检索（SQLite Vector 或 pgvector）。
- 记忆强化/衰减：访问一次 `reinforce_count +1`，长期不命中则降权。

## 6. 上下文优化策略

1. 双层上下文：
- 短期：最近 N 轮原文
- 长期：压缩后的事实记忆

2. 预算控制：
- 例如：总预算 1200 tokens
- Profile 20%，近期摘要 30%，长期记忆 50%

3. 摘要触发：
- `conversation_cache` 超阈值（如 12 条）
- 会话切换
- 时间窗口（如每 30 分钟）

## 7. 安全与隐私

1. 默认最小化存储：避免保存身份证号/手机号等敏感信息（可配置脱敏规则）。
2. 记忆导出与删除：支持按用户 ID 全量删除（合规需求）。
3. 审计日志：记录绑定、合并、删除等关键操作。
4. 可选加密：数据库文件加密（后续）。

## 8. 实施计划（里程碑）

## M1（1-2 天）基础可用

- 完成身份绑定、记忆写入、记忆查看、记忆删除。
- 加入上下文缓存与阈值压缩。
- 将 DB 路径迁移到 `plugin_data` 目录。
- 验收：命令可完整跑通。

## M2（2-4 天）体验升级

- 接入自动消息采集（非手动 `/tm_cache`）。
- 增加结构化 `memory_type` 与规则分类。
- 增加召回上下文模板输出（可直接拼 prompt）。
- 验收：连续多轮后回复能稳定引用历史偏好。

## M3（4-7 天）智能蒸馏

- 引入 LLM 蒸馏模块（可配置开关）。
- 引入冲突检测与旧记忆失效标记。
- 增加 `memory_events` 审计。
- 验收：同一偏好变化时能更新而非重复堆积。

## M4（7-14 天）生产增强

- 引入向量检索（可选后端）。
- 引入强化/衰减打分策略。
- 增加观测指标（命中率、召回延迟、token 节省率）。
- 验收：在高并发对话下，召回性能与准确度达标。

## 9. 关键指标（KPI）

- 记忆命中率：> 70%（在包含已知偏好问题的评测集上）。
- 错误召回率：< 10%。
- 平均召回延迟：< 150ms（规则检索）/ < 300ms（含向量）。
- token 节省率：相较“全量历史拼接”节省 > 40%。

## 10. 下一步建议（针对当前仓库）

1. 把当前 `main.py` 中 `db_path` 切到 AstrBot 规范目录（`plugin_data`）。
2. 增加 `memory_type/importance/confidence/reinforce_count` 字段迁移脚本。
3. 加一个统一接口：`build_memory_context(canonical_user_id, query)`，用于模型调用前注入。
4. 将 `/tm_bind` 增强为“验证码/双端确认”模式，降低误绑风险。

---

## 参考链接

- Mem0: https://github.com/mem0ai/mem0
- Memobase: https://github.com/memodb-io/memobase
- OpenMemory: https://github.com/CaviraOSS/OpenMemory
- Zep: https://github.com/getzep/zep
- ChatMemory: https://github.com/uezo/chatmemory
- AstrBot 插件存储规范: https://docs.astrbot.app/dev/star/guides/storage.html
- AstrBot 插件集合: https://github.com/AstrBotDevs/AstrBot_Plugins_Collection
