# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
