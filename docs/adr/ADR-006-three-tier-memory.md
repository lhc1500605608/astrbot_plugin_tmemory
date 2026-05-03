# ADR-006: SQLite-native 三层记忆架构

- Status: Accepted for 0.8.0 implementation
- Date: 2026-05-03
- Deciders: CTO, Software Architect
- Related: `TMEAAA-247`, `TMEAAA-248`, `TMEAAA-253`, `TMEAAA-254`, `TMEAAA-255`, `TMEAAA-256`, `TMEAAA-257`
- Supersedes: early `topic_clusters` / `episode_summaries` split-table sketch in `docs/design/TMEAAA-250-consolidation-pipeline-layered-injection.md`

## Context

`tmemory` 0.7.x 的核心记忆链路是平面的：

```text
conversation_cache -> distill -> memories -> FTS/vector/RRF -> prompt injection
```

这条链路已经可用，但它把短期上下文、阶段性事件和长期事实压到同一个抽象里，带来几个架构问题：

- `conversation_cache` 只承担未蒸馏素材缓存，缺少会话/阶段边界。
- `memories` 只适合长期稳定事实，不适合保存“这段时间正在发生什么”。
- 注入链路只能召回平面事实，难以表达当前 episode、稳定 semantic memory、风格指导之间的优先级。
- 如果直接引入外部数据库或完整图谱，会破坏当前 AstrBot 插件的本地优先、低运维交付模型。

0.8.0 的目标不是重写记忆系统，而是在现有 SQLite 内增加一层可回退的 episodic consolidation，使系统从“平面记忆列表”演进为“Working / Episodic / Semantic”三层模型。

## Decision Drivers

- 保持本地优先：默认仍只依赖单个 SQLite 文件。
- 保持兼容：旧配置、旧 `memories` 数据和旧平面注入路径继续可用。
- 控制热路径延迟：`on_llm_request` 禁止触发 consolidation LLM 调用。
- 控制实现复杂度：0.8.0 只新增最小必要 episode 表，不引入独立 topic aggregate。
- 支持演进：schema 能记录 episode provenance，为后续关系图谱或外部数据库迁移留下锚点。

## Options Considered

### Option A: 保持平面 `conversation_cache -> memories`

优点：

- 变更最小，没有新迁移风险。
- 现有测试和运维路径完全复用。

代价：

- 无法显式表达当前 episode 和长期 semantic memory 的差异。
- 注入仍只能按平面列表拼接，无法稳定提供阶段背景。
- 后续任何深层记忆能力都会继续挤压 `memories` 表语义。

结论：不满足 0.8.0 对短期/长期分层和深层调用体系的目标。

### Option B: 新增 `topic_clusters` + `episode_summaries` 双表

优点：

- Topic clustering 与 episode summarization 概念分离清晰。
- 适合未来做跨 session 主题合并和 topic 生命周期管理。

代价：

- 对 0.8.0 初始目标过重：需要维护 topic、summary、source mapping 三组状态。
- topic 与 episode 的边界容易漂移，增加迁移、QA 和后台重试复杂度。
- 当前产品需求只需要“可注入的阶段性摘要”，不需要独立 topic aggregate。

结论：作为研究草案保留，但不作为 0.8.0 初始实现边界。

### Option C: SQLite-native `memory_episodes` + `episode_sources`

优点：

- 一个 episode aggregate 同时承载标题、摘要、topic tags、entities、状态和注意力分数。
- `episode_sources` 负责 provenance，能从摘要回溯到原始 `conversation_cache` rows。
- `memories` 继续作为 semantic fact source，不需要第二张 semantic 表。
- 与现有 SQLite 事务、FTS、备份、purge 和测试模型兼容。

代价：

- topic 不是一等 aggregate，跨 session topic merge 能力有限。
- episode 表会承担较多字段，需要严格状态机和索引约束防止语义膨胀。
- 后续若需要全局 topic graph，仍需新增关系层。

结论：采纳。

### Option D: PostgreSQL / Supabase + pgvector

优点：

- 适合多实例共享、云端管理和未来托管版。
- 远端数据库更适合跨设备同步和运营分析。

代价：

- 改变插件交付模型，引入网络、鉴权、租户隔离和运维要求。
- 对当前单机 AstrBot 插件阶段属于过早基础设施化。

结论：保留为未来部署形态，不进入 0.8.0 默认架构。

## Decision

0.8.0 采用 **SQLite-native、增量分层、向后兼容** 的三层记忆架构：

```text
Working layer       Episodic layer                 Semantic layer             Injection
conversation_cache -> memory_episodes + sources -> memories + FTS/vector/RRF -> structured context block
```

### Working Memory

- 继续复用 `conversation_cache`，不新增独立 hot-path working table。
- 通过 additive columns 补足 `session_key`、`turn_index`、`topic_hint`、`episode_id`、`captured_at`。
- 负责保存当前会话/近期 turn 原始素材，供后台 consolidation 和 request-time working context 使用。

### Episodic Memory

- 新增 `memory_episodes` 作为 episode aggregate。
- 新增 `episode_sources` 记录 episode 与原始 conversation rows 的来源映射。
- episode 承载 `episode_title`、`episode_summary`、`topic_tags_json`、`key_entities_json`、`status`、`consolidation_status`、source 范围、重要性、置信度和 attention score。
- 可选 `memory_episodes_fts` 用于 request-time episode 召回，但必须保持 SQLite-only。

### Semantic Memory

- 保留 `memories` 作为长期稳定事实/偏好/限制/风格的唯一 semantic fact table。
- 通过 additive columns 增加 `episode_id`、`semantic_status`、`evidence_json`、`contradiction_of`、`derived_from`。
- 不新增第二张 semantic 表；历史 rows 默认仍有效，只是可能没有 episode provenance。

### Injection

- 默认保留现有 flat injection。
- `enable_layered_injection=false` 时不得改变当前有效路径。
- 分层注入启用后按以下顺序组装：当前 working/episode context -> semantic user memories -> style guidance。
- `on_llm_request` 只允许 SQLite/FTS/vector/reranker 等既有检索路径，不允许 consolidation LLM 调用。

## Implementation Boundary

### Schema boundary

必须符合以下边界：

- 所有 schema 变更为 additive DDL，可重复执行。
- 旧 `conversation_cache`、`memories`、`memory_events`、`distill_history` 数据不得丢失。
- 新表命名使用 `memory_episodes` / `episode_sources`，不使用 `topic_clusters` / `episode_summaries` 作为 0.8.0 初始实现表。
- 所有 user-facing 查询必须继续按 `canonical_user_id`、`scope`、`persona_id` 过滤；session 相关查询还要按 `session_key` 收敛。
- 用户 purge / cache cleanup 必须覆盖 episode/source provenance，不能留下跨用户残留。

### Consolidation boundary

必须符合以下边界：

- 复用现有 `_distill_worker_loop()`，0.8.0 不新增第二个常驻 worker。
- Pipeline stages 为 Working Capture -> Episodic Summarization -> Semantic Extraction。
- 新行为受 `enable_consolidation_pipeline` 总开关保护，默认关闭。
- `memory_mode=active_only` 和 `distill_pause=true` 必须抑制 consolidation side effects。
- LLM 成本由 batch size、per-user cycle cap、stage timeout、input token cap 和 optional independent model config 控制。
- LLM 输出必须经过 validator；失败不得把 source rows 标为完全处理。

### Config boundary

必须符合以下边界：

- 新配置字段必须有 safe defaults，并保持 v0.7.x 行为不变。
- `enable_consolidation_pipeline=false` 表示没有 episode/semantic pipeline LLM work。
- `enable_layered_injection=false` 表示当前 flat injection 仍是有效路径。
- 独立 consolidation provider/model 为空时，不得硬编码供应商；只能按既有 provider 选择模式 fallback。

### Hot-path boundary

必须符合以下边界：

- `on_llm_request` 中 consolidation LLM calls 数量必须为 0。
- 空 working/episodic/semantic/style section 必须安全省略。
- 总注入文本受 `inject_max_chars` 和 layer caps 控制。
- 现有 `inject_position` / slot marker 行为必须保持。
- 静态 system prompt 前缀不得被动态记忆块破坏。

## Consequences

### Positive

- 在不引入外部基础设施的前提下，补上当前 episode context 这一缺失层。
- 保留 `memories` 作为长期事实源，避免 semantic 数据分叉。
- 可以逐步启用：先迁移 schema，再启用 consolidation，再启用 layered injection。
- 每层都有清晰 fallback：关闭 pipeline 或 layered injection 即可回到当前行为。

### Negative

- SQLite schema 更宽，migration 和 QA 面积变大。
- Episode 状态机需要工程纪律；否则 `memory_episodes` 可能退化为另一张模糊事实表。
- Topic 能力被有意压缩到 tags，不适合复杂跨会话 topic 生命周期管理。

### Deferred

- 完整关系图谱、多跳推理和全局 topic graph。
- PostgreSQL / Supabase 远端后端。
- request-time summarization。
- 并行 consolidation worker 或外部 job system。

## Review Checklist for TMEAAA-257

最终实现审查必须逐项确认：

- `TMEAAA-253`: migration 是否只做 additive DDL，并使用 `memory_episodes` / `episode_sources` 而非 `topic_clusters` / `episode_summaries`。
- `TMEAAA-254`: consolidation 是否复用现有 worker、默认关闭、失败可重试、不会误标 source rows。
- `TMEAAA-255`: layered injection 是否默认关闭、热路径无 consolidation LLM、保持现有 `inject_position` 行为。
- `TMEAAA-256`: QA 是否覆盖 migration idempotency、用户/人格隔离、成本阈值、注入 latency 和现有 flat regression slice。

## Related Decisions

- ADR-004 的配置兼容性边界继续适用：新字段必须兼容旧平铺配置和 safe defaults。
- ADR-0001 的轻量关系层仍是中期方向；本 ADR 不实现 graph layer，只为后续 relation/provenance 留下 episode anchor。
