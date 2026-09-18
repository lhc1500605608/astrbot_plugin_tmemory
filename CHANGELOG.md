# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [v0.11.1] - 2026-09-18

线上故障修复（TMEAAA-387）。无配置迁移，直接替换安装即可。

### Fixed

- **蒸馏 worker 解包崩溃** — `_distill_worker_loop` 新增 `_coerce_cycle_result`
  容错解包：上游返回元组长度漂移时截断/补零并告警，不再抛
  `too many values to unpack`，蒸馏不再中断。
- **sqlite-vec `vec0` 缺失** — 根因是仅 `import sqlite_vec` 从未在连接上
  `load_extension`。现在 `DatabaseManager.db()` 对每个连接统一加载扩展，并在建
  `memory_vectors` / `profile_item_vectors` 前用 `pragma_module_list` 校验 `vec0`；
  不可用时清晰降级且**不建表、不抛错、不留 "no such table"**。`_vec_available`
  改为以连接探测结果为准，消除“已加载但 vec0 不可用”的矛盾日志。
- **中文 FTS5 不可用（no such tokenizer: jieba）** — 内置 SQLite 无 jieba
  tokenizer。改为 `memories_fts` 索引 Python jieba 预处理后的
  `tokenized_memory`（内置 `unicode61`），查询端同样 jieba 分词；`profile_items_fts`
  / `memory_episodes_fts` 使用内置 `trigram`（不可用时回退 `unicode61`）。旧
  jieba 结构表在启动时自动检测并重建索引；FTS5 完全不可用时才降级 LIKE。
- **维度迁移重建失败（no such table: memory_vectors）** — 重建前先在连接上确认
  `vec0` 可用，失败则保留旧索引并清晰降级，不再先 DROP 后失败导致索引丢失。
- **默认维度错配（2048 → 1024）** — `vector_dim` 默认值由 2048 改为 1024，与主流
  Provider（硅基流动 bge-m3/Qwen3-Embedding）对齐，避免每次启动触发 `2048 -> 1024`
  迁移；启动仍会自动以 Provider 实际维度为准。

### Added

- 安全：检索最终落库查询补充 `canonical_user_id` 过滤，杜绝跨用户召回。
- 回归测试：`tests/test_v0111_hardening.py`（vec0 逐连接加载/降级、中文 FTS、
  旧 FTS 结构迁移、解包容错）。

## [v0.11.0] - 2026-09-18

### ⚠️ Breaking Changes / 迁移指南

1. **嵌入来源变更（BC-1）** — 默认优先使用 AstrBot Provider/知识库嵌入；设置
   `embedding_source=standalone` 可恢复 v0.10.0 的独立配置行为。
2. **主动性默认关闭（BC-3）** — 新增 `proactive_enabled`（默认 `false`），需显式开启
   并配置限流/预算/opt-in 策略。
3. **蒸馏规则分级（BC-4）** — 新增规则分级路径；`distill_rule_gating=false` 恢复纯 LLM 路径。
4. **导入写接口（BC-5）** — `/tm_import` 默认 dry-run，写入前自动备份并支持回滚。

### Added（开发中）

- **B1 平台 Provider 嵌入接入** (TMEAAA-380) — 复用平台 Embedding/Rerank Provider，
  保留独立配置回退与维度变更重建索引。
- **B2 主动性记忆** (TMEAAA-381) — Proactive / `send_message`，带开关、限流、预算与
  用户级策略，默认关闭。
- **B3 蒸馏降本** (TMEAAA-382) — 规则分级 + 本地 embedding + prompt 缓存。
- **B6 质量基准与数据 API** (TMEAAA-383) — LongMemEval 类基准 + 导入/导出
  （dry-run + 备份）。
- **v0.11.0 控制面板** (TMEAAA-384) — 主动性/导入导出/嵌入来源/成本视图。

## [v0.10.0] - 2026-09-17

### ⚠️ Breaking Changes / 迁移指南

1. **旧 9966 独立端口面板下线** — 默认改用 AstrBot Dashboard 内嵌的 Plugin Pages
   （需 AstrBot ≥4.28）。如需回滚，设置 `webui_legacy_enabled=true`（同时保持
   `webui_enabled=true` 并设置 `webui_password`、确认端口未被占用）。
2. **`support_platforms` 声明变更** — `metadata.yaml` 平台列表更新为
   `aiocqhttp, qq_official, telegram, weixin_oc, weixin_official_account, wecom`。
3. **`astrbot_version` 声明变更** — 最低要求从 `>=4.16` 提升至 `>=4.16,<5`，
   插件页与会话生命周期功能需 AstrBot ≥4.28。
4. **配置项 `webui_legacy_enabled`** — 新增回滚开关，默认 `false`。
5. **配置项 `session_reset_policy`** — 新增 `/new` `/reset` 记忆策略
   （`keep` / `archive` / `clear`，默认 `keep`），需 AstrBot ≥4.28 钩子。

### Fixed

- **Plugin Pages bridge 全路由 500** (TMEAAA-369) — `_probe_plugin_pages()`
  现在同时校验 `Context.register_web_api` 与 `astrbot.api.web`
  (`json_response` / `error_response` / `request`) 契约。AstrBot 4.23.2 虽已有
  `register_web_api` 但缺 `astrbot.api.web`，此前被误判为可用，注册 bridge 后
  请求即 `ModuleNotFoundError` → HTTP 500。现在不满足时跳过注册并记日志；
  `web/bridge.py:register()` 也做同样的运行时契约校验（能力探测与运行时一致）。

### Added

- **会话生命周期对齐** (Phase 4a) — 接入 `ConversationManager.register_on_session_deleted`
  与 `on_agent_begin` / `on_agent_done`（AstrBot ≥4.28）：`/new` `/reset` 时按
  `session_reset_policy`（`keep` / `archive` / `clear`，默认 `keep`）处理会话缓存；
  长期记忆默认保留。新增 `conversation_cache.archived_at` 软归档列，工作上下文
  召回过滤归档行。详见 `docs/session-lifecycle.md`。(TMEAAA-358)
- **Plugin Pages Bridge** — 新增 `web/bridge.py` 替代 legacy 独立端口面板，
  通过 AstrBot Dashboard 插件页直接访问记忆管理 UI。(TMEAAA-354 Phase 3)
- **Adapter 层** — 新增 `adapters/` 目录，统一跨适配器接口。(TMEAAA-354)

### Changed

- **配置安全标记** — `embedding_api_key` 和 `webui_password` 在 `_conf_schema.json`
  中标记为 `secret: true`。
- **注入位置提示更新** — `inject_position` 的 `extra_user_temp` 选项文档明确
  需 AstrBot ≥4.28，低版本回退为 `system_prompt`。

## [v0.9.0] - 2026-05-11

### Added

- **WebUI/API 安全加固** (P0-3) — 写接口参数校验、HTTP 级错误码与负路径覆盖。画像编辑/归档/合并/配置更新写接口加入校验。`extra_user_temp` 回退兼容矩阵文档化。注入热路径零 LLM 调用回归验证。(TMEAAA-332)
- **混合召回注入** (P0-5) — `on_llm_request` 注入路径从纯 FTS5 升级为可选向量+混合召回（可配置），零 LLM 调用热路径。query embedding 缓存与回退策略。修复 FTS5 UPDATE 触发器静默一致性风险。(TMEAAA-333)
- **蒸馏 token 预算** (P0-6) — 新增 `distill_daily_token_budget` 配置项。超预算自动跳过蒸馏周期并告警。`/tm_distill_history` 暴露预算消耗视图。跨日自动重置。配置变更无需重启。(TMEAAA-334)

### Changed

- **代码复杂度重切分** (P0-4) — 以真实复杂区为目标拆分模块：
  - `core/utils.py` 拆分为命令处理/注入辅助/运行时工具
  - `core/admin_service.py` 分离读/写/投影逻辑
  - `core/consolidation.py` 明确 episode 与 profile extraction 边界
  - `web_server.py` 分离路由与 handler
  验收：各模块 <500 行 + 无循环导入 + 全量测试绿。(TMEAAA-335)
- **版本口径收敛** (P0-1) — 对外契约与版本标记对齐。(TMEAAA-330)
- **蒸馏运行时硬化** (P0-2) — 运行时稳定性加固。(TMEAAA-331)
- **自动化基线恢复** (P0-7) — 恢复全量测试为绿色基线。426 tests pass, 3 skipped。(TMEAAA-336)

### Documentation

- 更新 README 反映 v0.9.0 新增能力（安全加固、混合召回、预算控制）。
- 补充 ADR-007（用户画像模型边界）、ADR-008（旧表退役计划）。

### Compatibility

- AstrBot 兼容层保持 v4.16–v4.24.2 不变。
- OpenAPI smoke 适配 AstrBot nightlight PBKDF2 鉴权模式（e2e_verify.sh）。

## [v0.8.5] - 2026-05-04

### Added

- **蒸馏全链路集成测试** — `tests/test_distill_integration.py`，15 用例覆盖 LLM 蒸馏、规则蒸馏、多用户、节流、历史记录、记忆标记与向量化。(TMEAAA-35, TMEAAA-53)
- **真实环境对话集成验证** — 通过本地 AstrBot 8 轮多轮对话，验证自动采集 → 缓存 → 蒸馏 → 记忆注入全链路。

### Changed

- **真实环境测试路径适配** — `test_real_astrbot_integration.py` 硬编码路径改为容器兼容路径。

### Removed

- **冗余测试文件** — 移除 `test_distill_false_empty.py`，用例已并入 `test_distill_integration.py`。

## [v0.8.4] - 2026-05-04

### Changed

- **AstrBot v4.24.2 兼容适配** — 更新插件元数据、国际化与技能支持。(TMEAAA-294)
  - `metadata.yaml` 添加 `astrbot_version`、`short_desc`、`support_platforms` 字段。
  - 新增 `.astrbot-plugin/i18n/` 中英文翻译（zh-CN / en）。
  - 新增 `skills/SKILL.md` 描述插件记忆管理能力。
  - 版本号升级至 v0.8.4。

### Added

- **extra_user_temp 注入位置** — 基于 `TextPart(...).mark_as_temp()` 的动态记忆注入，不污染会话历史。(TMEAAA-300)
  - 新增 `inject_position=extra_user_temp` 选项（默认仍为 `system_prompt`）。
  - 当运行环境不支持 `mark_as_temp()` 时自动回退到 `system_prompt` 注入。

## [v0.8.3] - 2026-05-04

### Changed

- **用户画像重构** — 将记忆系统从三层管道（Working→Episodic→Semantic）重构为用户画像模式。(TMEAAA-280)
  - 新增 `user_profiles`、`profile_items`、`profile_item_evidence`、`profile_relations` 表作为长期画像事实来源。
  - 五个画像面：`preference`、`fact`、`style`、`restriction`、`task_pattern`。
  - 检索与注入链路完全切换到画像条目，支持按画像面检索和注入。
  - `memories`、`memory_episodes`、`episode_sources` 停用主链路；`identity_bindings` 与 `conversation_cache` 保留。
  - WebUI 从思维导图切换为画像工作台，删除 mindmap.js，新增 profile.js。
  - 无旧版兼容；本版本不保留旧数据迁移路径。

### Added

- 画像条目支持证据链溯源（`profile_item_evidence`），关联原始对话与提炼上下文。
- 画像面关系表 `profile_relations` 支持轻量跨面关联。

### Removed

- 移除三层记忆思维导图可视化及相关 UI 组件。
- 停用 `memory_episodes` / `episode_sources` / `memories` 旧表在主数据链路中的角色。

## [v0.7.1] - 2026-05-02

### Fixed

- 修复 WebUI tab 切换时残留 `panelStyle` 空 DOM 引用导致非思维导图页面无响应的问题。(TMEAAA-241)

## [v0.7.0] - 2026-05-02

### Changed

- 将插件外显品牌更新为 **MemoryForge**，并按用户模板完成 WebUI 视觉体系迁移。
- 更新 README 外显名称与版本信息，同时保留 `astrbot_plugin_tmemory` 仓库名和内部技术标识。

### Fixed

- 清理迁移过程中残留的未跟踪 React/Vite `webui/` 目录，最终保持现有 AstrBot 静态 WebUI 交付结构。

## [v0.6.0] - 2026-05-02

### Added

- **Attention Decay Scoring** — 引入基于指数衰减的 `attention_score` 字段，为记忆质量评估和后续召回排序提供动态权重依据。(TMEAAA-199)
- **Dual-Channel Memory Injection** — 支持 canonical 与 persona 双通道独立注入，实现跨适配器身份记忆与当前人格记忆的分离召回与组合。(TMEAAA-199)
- **Prompt Prefix Cache-Friendly Injection** — 优化记忆注入格式，提升对 LLM prompt prefix caching 的友好度，降低长上下文场景的 token 开销。(TMEAAA-205)

### Fixed

- 补全 `core/utils.py` 中缺失的 `from __future__ import annotations`，避免在部分 Python 环境下出现类型注解前向引用异常。

### Changed

- 将 `docs/`、`eval/` 及测试报告目录加入 `.gitignore`，防止本地隐私与评估数据误入版本控制。

## [v0.5.0] - 2026-05-01

### Added

- **AI Active Tools** — 新增 `remember` / `recall` LLM 工具，支持模型在对话中主动保存和检索记忆；同时提供 `hybrid` / `distill_only` / `active_only` 三种 `memory_mode`。(TMEAAA-102)
- **Style Distill Decoupling** — 聊天风格蒸馏功能完全剥离至独立项目，记忆管道与风格蒸馏实现零耦合。(TMEAAA-179, TMEAAA-180, TMEAAA-181)
- **Docker LLM Provider Migration** — Docker 集成环境默认 LLM Provider 从 Ollama 迁移至 DeepSeek，并补充 E2E 验证脚本。(TMEAAA-166, TMEAAA-170)
- **AdminService Boundary** — 建立 `AdminService` 应用服务边界，拆分前端静态资源并升级视觉体验。(TMEAAA-114, Phase 1–3)
- **Brand Assets** — 手工生成 tmemory 品牌图标资产。(TMEAAA-116)

### Fixed

- 修复 `style_distill` 命令误污染 `conversation_cache`、采集开关及解析兼容性问题。(TMEAAA-168)
- 修复配置持久化失败（`ctx.save_config` → `config.save_config`）。(TMEAAA-161)
- 修复 `web_server` 中 `_plugin` → `plugin` 属性名不匹配及 `AdminService` 导入路径问题。
- 修复全角字符导致 SyntaxError、FTS5/purify 代码损坏等关键稳定性问题。
- 修复用户合并数据库 bug、检索命中但注入块未生成的链路断点。

### Changed

- **Core Refactoring** — 大规模重构核心架构：抽取 `capture`、`distill`、`retrieval`、`db`、`config` 子模块，持续瘦身 `main.py`。(TMEAAA-96, TMEAAA-98)
- **Search Engine** — 替换 FAISS 为 SQLite-Vec + FTS5 混合检索架构。
- **Test Baseline** — 补齐 6 类关键场景测试、真实 AstrBot 加载验证及 Docker 测试环境。(TMEAAA-105)

## [v0.4.0] - 更早版本

- 详见 git history (`git log 43cf0d0 --oneline`)。
