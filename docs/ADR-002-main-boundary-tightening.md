# ADR-002 `main.py` 模块边界收紧方案

- 状态：Accepted
- 日期：2026-04-21
- 关联：`main.py`、`web_server.py`、`hybrid_search.py`、`vector_manager.py`

## 背景

当前 `TMemoryPlugin` 集中承担了 AstrBot 插件入口之外的大部分业务职责：

- 生命周期与可选依赖装配：数据库初始化、Schema 迁移、WebUI 加载、向量管理器初始化、后台 worker 启停。
- 事件适配：`on_any_message`、`on_llm_request`、`on_llm_response`。
- 管理命令：`/tm_*` 系列命令解析与结果格式化。
- 领域逻辑：身份绑定/合并、会话缓存、蒸馏、提纯、召回、注入、导出、清理。
- 基础设施细节：SQLite DDL、FTS5 迁移、Embedding/Rerank HTTP 调用、向量索引维护。

这导致 `main.py` 同时扮演 plugin shell、application service、repository、search orchestration 和 maintenance job coordinator。现状的问题不是“单文件太大”本身，而是边界不清导致后续改动很难局部验证：

- WebUI、命令、后台蒸馏都直接依赖 `TMemoryPlugin` 内部方法，形成默认的“全公开内部 API”。
- 测试基线主要覆盖 `_init_db`、`_migrate_schema`、`_insert_memory`、`_bind_identity`、`_merge_identity`、`terminate` 等直接方法，说明后续重构必须保持这些行为稳定。
- `vector_manager.py` 已经尝试引入单独模块，但检索主流程仍在 `main.py` 中通过 `HybridMemorySystem` 直接拼接，表明模块边界尚未真正收口。

本 ADR 的目标不是立即把 `main.py` 拆成很多文件，而是先建立近期可执行的窄边界，让后续实现任务能在不重写插件外部行为的前提下逐步迁移。

## 决策

采用“壳层保留 + 领域窄接口先行”的收紧策略。

短期内保留 `main.py` 作为唯一 AstrBot 插件入口与默认编排层，但把最容易外溢、最常被复用、最适合独立测试的职责先收敛到 3 个窄接口族：

1. `repository`：封装 identity / memory / conversation / history 的 SQLite 读写。
2. `distill`：封装蒸馏、提纯、质量裁剪等“记忆演化”逻辑。
3. `retrieval`：封装记忆召回、注入块构建、向量/FTS 混合检索协调。

`main.py` 在第一阶段仍保留命令和 AstrBot hooks，但这些入口只负责：

- 从 `event` / `req` / `resp` 提取输入。
- 调用窄接口。
- 将返回值转换为 AstrBot 输出或生命周期副作用。

## 第一阶段边界

### 保留在 `main.py` 的职责

第一阶段继续保留以下职责在 `main.py`，不主动拆出：

1. AstrBot 入口壳层。
原因：`@register`、`@filter` 装饰器要求方法仍挂在 `Star` 子类上，强行迁移只会引入间接层。

2. 生命周期编排。
原因：`initialize()` / `terminate()` 需要串联 DB、worker、WebUI、VectorManager 的启动顺序，属于插件装配逻辑，不是领域逻辑。

3. 命令参数解析与 AstrBot 输出格式化。
原因：`/tm_bind`、`/tm_merge`、`/tm_context` 等命令主要是适配层，短期价值在于保持外部行为稳定，而不是抽象出 command bus。

4. WebUI 降级加载。
原因：`_safe_load_web_server()` 与 `TMemoryPlugin` 的运行时实例绑定很强，且目前 `web_server.py` 直接回调 plugin 方法，第一阶段先稳定边界，不先改装配方式。

### 第一阶段优先抽离的职责

第一阶段只抽离下列 3 类职责，且必须通过窄接口暴露：

1. Repository 职责。
范围：`_init_db`、`_migrate_schema`、`_ensure_columns` 之外的主要数据访问，包括 `_bind_identity`、`_merge_identity`、`_insert_memory`、`_delete_memory`、`_list_memories`、`_insert_conversation`、`_fetch_pending_rows`、`_record_distill_history`、`_export_user_data`、`_purge_user_data`。

收紧原因：

- 这些方法已经构成可识别的数据访问边界。
- 当前命令、WebUI、后台 worker 都直接读写它们，是最典型的“横向扩散 API”。
- 现有测试基线正好覆盖其中大部分行为，便于以行为等价方式迁移。

建议接口：

- `IdentityRepository`
- `MemoryRepository`
- `ConversationRepository`
- 或保持更保守的单个 `TMemoryRepository`

本次推荐先用单个 `TMemoryRepository`，避免在第一阶段引入过多新对象。

2. Distill / Purify 职责。
范围：`_run_distill_cycle`、`_distill_rows_with_llm`、`_build_distill_prompt`、`_validate_distill_output`、`_run_memory_purify`、`_llm_purify_judge`、`_manual_purify_memories`、`_llm_purify_operations`、`_llm_split_memory`。

收紧原因：

- 这一组逻辑是最重的领域行为，既依赖 repository，又依赖 LLM provider 解析。
- 这里经常演进 prompt、JSON 解析和质量规则；如果继续散落在 `main.py`，任何修改都容易影响 unrelated hook。
- 与命令和后台 worker 的关系天然是“服务被调用”，适合抽成 `DistillService`。

建议接口：

- `DistillService.run_cycle(force: bool, trigger: str)`
- `DistillService.manual_purify(...)`
- `DistillService.rebuild_vector_index()` 可暂缓，第一阶段不强行塞进 distill。

3. Retrieval 职责。
范围：`_build_injection_block`、`build_memory_context`、`_retrieve_memories`、`_deduplicate_results`、`_rerank_results`，以及对 `HybridMemorySystem` / `VectorManager` 的编排调用。

收紧原因：

- 它是 `on_llm_request`、`/tm_context`、未来 WebUI 调试的共同依赖。
- 目前 `main.py` 同时知道 query embedding、RRF 粗排、recency bonus、reinforce 更新和 prompt block 格式，边界过宽。
- 这是未来切换 `sqlite-vec` / 外部向量库 / Supabase pgvector 时最需要稳定的接口面。

建议接口：

- `RetrievalService.retrieve_memories(...)`
- `RetrievalService.build_injection_block(...)`
- `RetrievalService.build_debug_context(...)`

### 第一阶段明确不抽离的职责

为避免扩大改动面，以下内容短期内不拆：

1. 事件对象解析辅助。
如 `_safe_get_unified_msg_origin`、`_get_adapter_name`、`_get_adapter_user_id`、`_is_group_event`、`_get_current_persona_async`。

不拆原因：

- 它们强绑定 AstrBot runtime。
- 抽出后仍然只能被 plugin shell 使用，复用价值有限。

2. 配置字段物化方式。
即 `_set_safe_defaults` + `_parse_config` 先继续保留，不在第一阶段上 dataclass 配置树。

不拆原因：

- 虽然现在属性很多，但改动它会波及几乎所有方法。
- 当前 issue 的核心是边界收紧，不是配置模型重做。

3. WebUI 文件本身。

不拆原因：

- `web_server.py` 目前直接调用 plugin 内部方法，若同时改 WebUI 接口和主模块边界，会把一次结构收敛变成两次协议迁移。

4. `VectorManager` 的后端能力扩展。

不拆原因：

- 现有 `vector_manager.py` 还在半集成状态，第一阶段目标应是把它从 `main.py` 的细节依赖中隔离出来，而不是同步完成 Qdrant / sqlite-vec 双后端架构定稿。

## 目标模块关系

第一阶段之后，依赖方向应收紧为：

```text
TMemoryPlugin(main.py)
  -> TMemoryRepository
  -> DistillService
  -> RetrievalService
  -> VectorManager (soft dependency)
  -> WebServer loader

DistillService
  -> TMemoryRepository
  -> LLM facade (由 plugin/context 提供)

RetrievalService
  -> TMemoryRepository
  -> HybridMemorySystem / VectorManager
  -> optional reranker client
```

规则：

- `web_server.py`、命令处理、hooks 不允许再直接拼 SQL。
- `web_server.py`、命令处理不允许直接操作检索细节，只能调用 plugin 转发后的 service 方法。
- repository 不依赖 AstrBot event、`ProviderRequest`、`LLMResponse`。
- retrieval 不负责解析 `event`，只接收显式参数，如 `canonical_user_id`、`query`、`scope`、`persona_id`。

## 分阶段实施顺序

### 阶段 1：收口数据访问边界

内容：

- 引入单个 `TMemoryRepository`。
- 迁移 identity / memories / conversation / history 相关方法。
- `main.py` 保留同名方法作为薄代理，先不改测试入口。

收益：

- 不破坏现有测试习惯。
- 给后续 distill / retrieval 提供稳定依赖。

风险：

- 代理期会有一段“双入口”状态。

回滚成本：低。
回滚方式：删除 repository 文件，把代理内联回 `main.py`。

### 阶段 2：抽离 DistillService

内容：

- 迁移蒸馏 worker 的每用户处理、LLM 输出校验、提纯逻辑。
- `tm_distill_now`、`tm_purify`、worker loop 改为调用 service。

收益：

- 领域演化逻辑从 plugin shell 中剥离。
- 便于单独添加 prompt / JSON 解析测试。

风险：

- service 需要部分 `context` 能力，容易变成把整个 plugin 传进去。

控制措施：

- 只传递最小依赖：repository、provider resolver、llm generate facade、time helpers。

回滚成本：中低。

### 阶段 3：抽离 RetrievalService

内容：

- 迁移 `_retrieve_memories`、注入块构建、调试上下文构建。
- 把 `HybridMemorySystem` 的直接编排收口到 retrieval service。

收益：

- 为未来替换检索后端预留单一接口。
- `on_llm_request` 和 `/tm_context` 的行为可以被更稳定地回归测试。

风险：

- reinforce 更新属于“查询带副作用”，如果边界设计不清，容易遗漏。

控制措施：

- 在 service 接口中明确“retrieve 会更新 reinforce_count”。

回滚成本：中。

## 与 AstrBot 生命周期和现有基线的兼容要求

必须保持以下约束：

1. `TMemoryPlugin` 仍然是唯一 `@register` 入口，所有 `@filter` 仍挂在该类上。
2. `initialize()` 和 `terminate()` 的外部行为保持不变：DB 初始化、可选向量初始化、worker 启停、WebUI 降级处理仍然由 plugin shell 统一编排。
3. 现有测试关注的方法行为必须保持等价，尤其是：
   - `_init_db` / `_migrate_schema`
   - `_insert_memory`
   - `_bind_identity` / `_merge_identity`
   - `_insert_conversation`
   - `terminate`
4. 在阶段 1 结束前，对外仍允许通过 `plugin._xxx()` 访问旧方法名，以减少测试和 WebUI 改造冲击。

## Trade-off

### 选择该方案得到什么

- 先收紧行为边界，再做文件拆分，后续每一步都有回滚点。
- 优先把最有复用价值的 repository / distill / retrieval 收成窄接口，减少 `main.py` 的“默认内部 API”面积。
- 不要求一次性完成 dataclass 配置、command handler 拆分、WebUI 重写等高耦合工作。

### 明确放弃什么

- 不追求第一阶段把 `main.py` 迅速缩到几百行。
- 不追求立即形成理想化的 `core/*` 终态目录。
- 不解决所有技术债，例如 async SQLite 阻塞、配置模型散乱、WebUI 直接回调 plugin 方法等，除非它们直接阻碍边界收紧。

## 对后续实现任务的约束

后续 AI Engineer 若承接此设计，应遵循以下约束：

1. 第一实现任务只做 repository 收口，不同时迁移 retrieval 或 distill。
2. 新增模块优先提供窄接口，不复制一份新的“巨型 service”。
3. `main.py` 中原有方法若被保留为兼容代理，其函数体应尽量只剩参数转发。
4. 任何阶段都不允许改变 `/tm_*` 命令语义、WebUI API 路由或数据库 schema 行为，除非另开 issue。
5. 若要引入 PostgreSQL / Supabase，必须在 retrieval/repository 边界稳定后另写新 ADR，不在本 issue 内顺带推进。

## 结论

`main.py` 当前最需要的不是“大拆分”，而是先把数据访问、记忆演化、记忆召回 3 组核心职责从 AstrBot 插件壳层里收成窄接口。这样可以在不改变插件生命周期、不打断 WebUI 和现有测试基线的前提下，逐步把巨石文件转成可演进结构。
