# ADR-0001: 输出图+向量混合记忆架构

- Status: Proposed
- Date: 2026-04-22
- Deciders: Software Architect
- Related: `TMEAAA-61`, `TMEAAA-51`

## Context

`astrbot_plugin_tmemory` 当前已经具备一个可工作的本地优先记忆内核：

- 主存储为单文件 `SQLite`。
- 词面召回依赖 `FTS5`，并通过 `memories -> memories_fts` 的 content-sync 触发器保持同步。
- 语义召回依赖可选的 `sqlite-vec` `memory_vectors` 虚拟表。
- 检索层已经采用双路召回 + `RRF` 融合，而不是单一路径。
- 数据模型已经区分 `memories`、`conversation_cache`、`memory_events`，能够承载事实、偏好、任务、审计事件等不同语义。

这意味着项目已经不是“是否需要混合检索”的问题，而是“是否要在现有结构之上增加一个轻量图层，用来表达实体关系、记忆之间的连接和可解释扩展”。

来自竞品调研和领域观察的主要信号：

- 纯向量召回容易把“语义相近但关系不对”的记忆一起带回。
- 纯 FTS5 适合精确词面命中，但难以表达“用户 -> 喜欢 -> 饮品 -> 黑咖啡”这类关系路径。
- 记忆产品的下一阶段价值，不只是存更多文本，而是更稳定地回答“为什么召回这条记忆”和“能否顺着关系扩一跳”。

但当前项目仍以 AstrBot 插件形态交付，约束同样明确：

- 用户偏好开箱即用，本地部署成本要低。
- 当前代码、测试、运维模型都围绕单个 SQLite 文件展开。
- 该方向属于中期演进，不要求马上进入实现，也不希望为未验证需求引入新基础设施。

## Decision Drivers

- 保持本地优先和低运维成本。
- 与现有 `SQLite + FTS5 + sqlite-vec` 兼容，避免重写主存储。
- 能表达最小必要的关系结构，而不是提前建设完整知识图谱平台。
- 可逆，失败时可以退回当前架构，迁移成本可控。
- 支持后续讨论 PostgreSQL / Supabase 远端后端，但不把当前架构绑定到外部数据库。

## Options Considered

### Option A: 引入 Kuzu 作为嵌入式图库

做法：

- 保留现有 SQLite 作为事实和审计源。
- 新增 Kuzu 存储 `entity`, `memory_node`, `relation`。
- 写入链路在蒸馏完成后同步写两份数据：SQLite 一份、Kuzu 一份。
- 召回时先做向量/FTS5 初筛，再用图扩展一跳或两跳进行重排。

优点：

- 图模型表达力最强，适合复杂关系遍历。
- 对多跳查询、关系解释、路径输出比较自然。
- 在“用户事实图谱”方向上，比自己拼 SQL 邻接表更清晰。

代价和风险：

- 引入第二个嵌入式数据库，部署、打包、跨平台兼容和故障排查都更复杂。
- Python 生态和插件分发层面对原生依赖更敏感，失败模式比纯 SQLite 多。
- 写路径变成双写，必须处理一致性、补偿和重建工具。
- 对当前规模的记忆量和查询复杂度来说，表达力可能超前于真实需求。

结论：

- 技术上可行，但当前阶段不推荐作为首个图层方案。

### Option B: 在现有 SQLite 上增加轻量关系层（邻接表 / 边表）

做法：

- 继续以 `memories` 为长期记忆事实源。
- 新增少量关系表，例如：
  - `memory_entities(entity_id, canonical_user_id, entity_type, entity_value, normalized_value, created_at, updated_at)`
  - `memory_edges(from_kind, from_id, relation_type, to_kind, to_id, weight, evidence_memory_id, created_at)`
  - `memory_edge_snapshots` 或复用 `memory_events` 记录重建和删除事件
- 图层只承担“关系索引”和“局部扩展”职责，不替代主事实表。
- 检索流程保持不变：先 `FTS5/sqlite-vec` 初筛，再按边表做 one-hop 扩展或重排。

优点：

- 完全复用当前 SQLite 文件、事务、备份、测试和运维习惯。
- 引入复杂度最小，可按需增加，不影响当前无图模式。
- 可直接复用 `memory_events` 做重建和审计，失败时删表即可回退。
- 对 PostgreSQL / Supabase 也最容易迁移，因为邻接表模型天然可平移到关系库。

代价和风险：

- 图遍历能力有限，复杂多跳查询不如专用图库自然。
- 需要团队自己定义关系抽取规范，避免边表沦为“另一份难维护的事实表”。
- 如果后续真的出现高频多跳分析，可能还要二次迁移到专用图库。

结论：

- 推荐作为中期方向的首选方案。

### Option C: 保持现状，只做向量 + FTS5 调优，不引入图层

做法：

- 继续强化 `sqlite-vec + FTS5 + RRF`。
- 通过更好的蒸馏、标签、重排特征解决召回质量问题。

优点：

- 最便宜，短期实现风险最低。
- 不新增数据结构，不需要迁移。

代价和风险：

- 关系表达仍然隐式存在于自然语言里，不可解释也不稳定。
- 很难支持“围绕某实体扩一跳”的上下文组装。
- 中长期仍会遇到“召回到了相似句子，但缺少因果/隶属关系”的上限。

结论：

- 可作为短期现状，但不应作为中期架构结论。

### Option D: 直接迁移到 PostgreSQL / Supabase + pgvector + 图模型

做法：

- 将当前本地 SQLite 架构整体迁到远端 Postgres。
- 向量改为 `pgvector`，关系层放入普通关系表，必要时再叠加图查询能力。

优点：

- 适合未来多实例共享、远程管理、云端工作流。
- 统一远端数据库后，数据分析和运营能力更强。

代价和风险：

- 直接改变项目交付模型，从“本地优先插件”变成“依赖外部数据库的系统”。
- 增加配置、鉴权、网络可用性、租户隔离、迁移运维等复杂度。
- 对当前项目阶段属于问题过早抽象。

结论：

- 保留为未来托管版或企业版候选，不作为当前 ADR 的推荐决策。

## Decision

我们选择：

**在现有 `SQLite + FTS5 + sqlite-vec` 体系上增加轻量关系层，形成“事实表 + 关系边表 + 混合检索”的中期演进方案；当前不引入 Kuzu，也不迁移到 PostgreSQL / Supabase。**

这不是要把 `tmemory` 立刻做成完整知识图谱系统，而是给当前检索体系补上一层可解释、可扩展、可回退的关系索引。

### 决策边界

本决策明确包含：

- 允许在 SQLite 内新增关系表和必要索引。
- 允许在检索链路中增加 one-hop 扩展或图重排步骤。
- 关系层的数据来源仍然以 `memories` 蒸馏结果为主，而不是引入独立写入入口。

本决策明确不包含：

- 现在就接入 Kuzu。
- 现在就要求 PostgreSQL / Supabase 成为运行前提。
- 现在就支持任意多跳图查询、复杂图算法或独立图库运维。

## Why This Decision

这份决策的核心权衡是：

- 我们想要图能力，但只想买“当前用得上的那一部分”。
- 我们承认图库更强，但当前项目的真实瓶颈先是召回解释性和关系扩展，而不是图库能力本身。
- 我们优先保护现有本地优先架构和测试面，避免把一个插件过早演化成多引擎系统。

换句话说，当前更像是在现有记忆系统里补一个“关系索引层”，而不是新建一个“图数据库子系统”。

## Target Shape

建议的逻辑模型：

1. `memories`
   继续作为长期记忆事实源，保留现有 `memory_type`, `importance`, `confidence`, `is_active`, `persona_id`, `scope`。

2. `memory_entities`
   从记忆文本中抽取结构化实体，例如 `person`, `food`, `project`, `location`, `constraint_topic`。

3. `memory_edges`
   表达最小必要关系，例如：
   - `user_prefers_entity`
   - `user_dislikes_entity`
   - `memory_refers_entity`
   - `task_depends_on_entity`
   - `entity_related_entity`

4. `memory_events`
   继续作为审计和重建日志，记录关系抽取、边重建、边失效。

### 检索流程

建议流程保持分层，而不是彻底改写：

1. 输入 query。
2. 使用现有 `FTS5` 和可选 `sqlite-vec` 做主召回。
3. 从 Top-K 结果中提取命中的 `memory_id` 和实体。
4. 在 `memory_edges` 上做 one-hop 扩展，补充与查询实体强相关的少量邻居记忆。
5. 最终排序时叠加：
   - 原始 `RRF` 分数
   - 关系边权重
   - `importance`
   - `confidence`
   - `last_seen_at` / `reinforce_count`

这使图层处于“重排增强器”的位置，而不是“唯一检索入口”。

## Compatibility

### 与当前 SQLite 的兼容方式

- 关系层与现有 `memories` 共用同一个 SQLite 文件。
- 可以通过普通迁移新增表，不影响已有数据路径。
- 可以通过 `memory_id` 作为证据链回指原始事实，保持删除、失活、审计语义一致。

### 与 `sqlite-vec` 的兼容方式

- 向量表仍然只负责语义召回，不承担关系存储。
- 图层不依赖 `sqlite-vec` 存在；未安装 `sqlite-vec` 时，仍可运行 `FTS5 + 邻接表扩展`。
- 若未来切换为外部向量后端，关系层仍可独立保留。

### 与 `FTS5` 的兼容方式

- `FTS5` 继续承担词面入口召回，特别适合中文偏好、专有名词、显式约束。
- 图层的最佳位置是 `FTS5` 和向量召回之后，而不是替代全文索引。

### 与 PostgreSQL / Supabase 的兼容方式

- 若未来需要远端化，可将 `memory_entities` / `memory_edges` 原样迁到 Postgres 普通表。
- 向量召回可替换为 `pgvector`，而关系表和审计表模型无需改变。
- 这让“先 SQLite，后 Postgres”成为演进路径，而不是重写路径。

## Consequences

### Positive

- 在不改交付模式的前提下，补足关系表达能力。
- 召回链路更可解释，可以输出“因命中实体 X，所以扩展到记忆 Y”。
- 本地优先架构保持不变，失败时回退简单。
- 为未来 Postgres / Supabase 远端化保留平滑迁移路径。

### Negative

- 代码复杂度会增加一个层级：抽取实体、维护边、处理失活。
- 数据一致性从“单表 + 索引”变成“事实表 + 派生关系表”。
- 需要定义有限关系类型，否则容易失控。

### Operational Cost

- 数据迁移成本低于引入 Kuzu，但高于保持现状。
- 测试面需要新增：关系抽取、边更新、删除回收、召回解释。
- WebUI 若要展示图关系，需要补充最小可视化，但这不是 ADR 的前置条件。

## Rejected For Now

以下方向本 ADR 明确拒绝在当前阶段推进：

- 直接引入 `Kuzu` 作为生产默认依赖。
- 直接把插件运行前提升级为 PostgreSQL / Supabase。
- 为“未来可能的复杂图分析”提前建设通用图基础设施。
- 把图层做成主写入源，绕开 `memories` 主事实表。

## Reversibility

该方案刻意设计为可逆：

- 关系表是派生结构，不是唯一事实源。
- 若效果不好，可以停用图重排，仅保留当前 `FTS5/sqlite-vec/RRF`。
- 若将来证明需要专用图库，可从 `memories + memory_entities + memory_edges` 导出，再迁往 Kuzu 或 Postgres。

回退成本：

- 应用层关闭关系扩展逻辑。
- 保留或删除关系表均可，不影响现有记忆主数据可读性。

## Migration Path

推荐以三个阶段推进，而不是一次性做完：

1. Stage 1: 数据模型预留
   新增 `memory_entities` 和 `memory_edges`，但默认不开启召回扩展。

2. Stage 2: 派生关系构建
   在蒸馏后对部分 `memory_type` 提取最小实体和关系，先覆盖 `preference`, `fact`, `restriction`, `task`。

3. Stage 3: 检索增强
   在现有混合检索结果基础上做 one-hop 扩展和轻量重排，并用离线评估验证收益。

进入下一阶段的条件应基于评估结果，而不是基于“图谱听起来更先进”。

## Validation Criteria

若后续进入实现立项，建议用以下标准验证是否值得继续：

- 与当前基线相比，目标查询集的 `Recall@K` 和人工相关性评价有稳定提升。
- 召回解释能明确展示“入口记忆 -> 关系 -> 扩展记忆”。
- 未安装 `sqlite-vec` 的场景下，不出现明显退化或新故障。
- 关系表损坏或为空时，系统能够无损降级到当前模式。

## Follow-up ADRs

若该方向被批准立项，后续需要至少两份补充 ADR：

- ADR: 关系类型与实体抽取边界
- ADR: 本地 SQLite 到 PostgreSQL / Supabase 的迁移触发条件
