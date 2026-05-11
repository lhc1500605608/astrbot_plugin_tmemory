# MemoryForge v0.8.5 — 路线图重置与下一阶段优化方向

> 编制日期: 2026-05-08 | 当前版本: v0.8.5 | 审计依据: 仓库代码、`CHANGELOG.md`、`README.md`、ADR、测试目录
>
> 说明: 本文替代 2026-05-01 编制的旧版 `v0.4.0 → v0.5.0` 规划。文件名暂未改动，仅为保持既有引用稳定；内容已按 `v0.8.5` 实际状态重置。

---

## 一、结论先行

1. **用户画像模型可以作为 v0.9.x 的稳定优化基线。**
   当前主链路已经围绕 `user_profiles`、`profile_items`、`profile_item_evidence`、`profile_relations` 建立了完整的入库、检索、注入和 WebUI 管理闭环，不再适合继续以旧的三层 `memories` 模型作为产品叙事中心。

2. **旧路线图最关键的两项已经完成，但复杂度转移了位置。**
   `main.py` 已从旧文档中的 1892 行收敛到 315 行；蒸馏全链路集成测试和真实 AstrBot 环境验证也已进入 `v0.8.5`。当前维护压力主要转移到 `core/utils.py`、`core/consolidation.py`、`core/admin_service.py`、`web_server.py`。

3. **v0.9.0 不应再围绕“继续拆 `main.py`”组织。**
   下一阶段的优先级应改为: 画像链路硬化、兼容/术语清理、WebUI/API 安全性、成本与可观测性、历史债务边界收敛。

4. **`style_distill` 不再是主插件的阻塞项。**
   主仓库应继续保持解耦，不再为独立插件承担内嵌实现债；只保留清晰的接口边界和文档说明。旧任务 [TMEAAA-163](/TMEAAA/issues/TMEAAA-163) 维持取消结论，不应重开。

5. **CTO 综合判断：`v0.9.0` 应定义为“质量与收敛版本”，不是“新能力扩张版本”。**
   三方专项结论一致指向同一件事：现阶段最该做的是先恢复绿色自动化基线、补齐 WebUI/OpenAPI 验证面、收紧蒸馏/注入成本与边界，再决定是否推进 `memories` 退役、`core/utils.py` 拆分等一向门重构。

---

## 二、当前实际状态（v0.8.5）

### 2.1 代码与测试基线

| 指标 | 当前状态 |
|------|----------|
| 插件入口 `main.py` | 315 行 |
| Web 服务 `web_server.py` | 656 行 |
| `core/admin_service.py` | 973 行 |
| `core/consolidation.py` | 1016 行 |
| `core/utils.py` | 1506 行 |
| 仓库测试函数数 | 303 个 |
| 真实环境验证 | `tests/test_real_astrbot_integration.py` |
| 画像存储/状态机测试 | `tests/test_profile_storage.py` |
| WebUI/Profile API 测试 | `tests/test_profile_admin_api.py`、`tests/test_web_server.py` |
| 蒸馏链路集成测试 | `tests/test_distill_integration.py` |

### 2.2 版本演进摘要

| 版本 | 日期 | 已落地的核心变化 |
|------|------|------------------|
| v0.5.0 | 2026-05-01 | `remember` / `recall` 主动工具、style_distill 完全剥离、Docker LLM 切至 DeepSeek |
| v0.6.0 | 2026-05-02 | Attention Decay、双通道注入、Prompt Prefix Cache 友好注入 |
| v0.7.0 / v0.7.1 | 2026-05-02 | MemoryForge 品牌迁移、WebUI 视觉升级、tab 切换修复 |
| v0.8.3 | 2026-05-04 | 三层记忆主链路切换为用户画像模型 |
| v0.8.4 | 2026-05-04 | AstrBot v4.24.2 兼容、`extra_user_temp` 注入位置 |
| v0.8.5 | 2026-05-04 | 蒸馏全链路集成测试、真实 AstrBot 多轮验证 |

### 2.3 当前主链路

```text
conversation_cache
  -> profile extraction / distill runtime
  -> profile_items + profile_item_evidence + profile_relations
  -> retrieval / injection
  -> WebUI profile workbench + remember/recall 工具
```

当前代码中仍保留 `memories`、`memory_episodes`、`episode_sources` 等旧层与过渡层，但它们已经不应再主导产品路线图。

---

## 三、v0.8.5 架构审计

### 3.1 可以视为稳定基线的部分

以下事实支持“用户画像模型已足够稳定到可以作为后续优化基线”这一判断:

1. **画像 schema 已成为一等公民。**
   `core/db.py` 中已定义 `user_profiles`、`profile_items`、`profile_item_evidence`、`profile_relations` 四张主表及约束。

2. **画像提取链路已经落到运行时代码。**
   `core/consolidation.py` 中 `ProfileExtractionRuntimeMixin` 已执行 `conversation_cache -> profile_items` 的提取、校验、证据挂接与向量写入。

3. **注入与检索已经转向画像条目。**
   `core/injection.py`、`core/utils.py` 当前以 `retrieve_profile_items(...)` 为主要注入来源，而非旧 `memories` 平面列表。

4. **WebUI 管理面已经支持画像工作台。**
   `web_server.py` 暴露 `/api/profile/*` 路由，`core/admin_service.py` 提供画像摘要、条目查询、证据查看、归档与合并。

5. **画像链路已有专门测试面。**
   `tests/test_profile_storage.py`、`tests/test_profile_admin_api.py`、`tests/test_injection_chain.py` 已覆盖 schema、状态机、证据链与注入行为。

结论: **可以把画像模型视为 v0.9.x 的稳定产品基线，但不应误判为架构已经完全收敛。**

### 3.2 当前最主要的架构风险

#### R1. 版本与对外契约存在漂移

- `metadata.yaml`、`README.md`、`CHANGELOG.md` 已声明 `v0.8.5`
- 但 `main.py` 中 `@register(..., "0.8.4")` 仍未同步

影响:
- 插件元数据、运行时注册信息、文档三处版本口径不一致
- 这类“表层不一致”会直接损害发布可信度

#### R2. 画像主链路与旧层/过渡层并存，叙事边界不清

当前仓库同时保留:

- `profile_items` / `profile_relations` 画像主链路
- `memory_episodes` / `episode_sources` 阶段性聚合层
- `memories` 旧长期事实层
- `conversations` 历史表 DDL

影响:
- 路线图若继续同时推动三套概念，会放大维护成本
- `ADR-006` 的三层记忆叙事与 `v0.8.3` 的画像主链路之间需要重新定边界

#### R3. 复杂度已经从 `main.py` 迁移到其他大模块

当前大文件集中在:

- `core/utils.py` 1506 行
- `core/consolidation.py` 1016 行
- `core/admin_service.py` 973 行
- `web_server.py` 656 行

影响:
- 继续强调“拆 `main.py`”会产生错误优化目标
- 真正的维护热点已是命令处理、注入辅助、画像提取、Web 管理逻辑

#### R4. WebUI 写接口仍然是高风险面

`web_server.py` 和 `core/admin_service.py` 已覆盖大量读写能力，但当前更像“功能可用”而不是“边界硬化完成”:

- 写接口的参数校验、负路径行为、错误语义仍需补强
- 应优先覆盖手工编辑、合并、归档、配置更新等高风险动作

#### R5. 旧文档与遗留术语尚未完全清理

仓库里仍可见:

- `style_distill` 的旧测试拓扑文档与命令示例
- `/tm_quality_refine`、`/tm_refine` 等兼容命令仍在对外暴露
- 旧 `conversations` 表仍存在 DDL

影响:
- 新用户难以判断“哪些是现行能力，哪些只是兼容层”
- 后续若做 v1.0.0，将放大发版和迁移说明成本

---

## 四、旧路线图 vs 实际进度

| 旧规划项 | 当前判断 | 说明 |
|----------|----------|------|
| `main.py` 进一步拆分 | **已完成精神目标，原目标失效** | `main.py` 已降到 315 行；复杂度已迁移到 `core/*` 与 `web_server.py` |
| 蒸馏全流程集成测试 | **已完成** | `tests/test_distill_integration.py` 与 `v0.8.5` 发布记录已覆盖这项债务 |
| README 全面更新 | **部分完成** | 版本、工作流、命令集已更新，但配置/术语/兼容层说明仍不够收敛 |
| 消除 AdminService/main.py 重复代码 | **目标需重写** | 现在的主要问题不是 `main.py` 重复，而是 `admin_service` / `web_server` / `utils` 的职责过重 |
| `purify` 统一命名，废弃 `refine` | **未完成** | `/tm_quality_refine`、`/tm_refine` 仍保留对外兼容 |
| 清理废弃 `conversations` 表 | **未完成** | `core/db.py` 仍保留 DDL |
| WebUI 接口集成测试 | **基础完成，仍需扩展** | 已有 `tests/test_web_server.py` 与 `tests/test_profile_admin_api.py`，但负路径和输入校验面仍不足 |
| 蒸馏管线结构化错误处理 | **未完成** | 当前仍以通用 warning/fallback 为主 |
| 配置 schema 启动校验 | **部分完成** | 有安全默认值与兼容解析，但缺少未知键警告/严格模式 |
| 轻量知识图谱层（ADR-0001） | **延后并需重审** | 该 ADR 早于画像模型，以 `memories` 为中心，已不再适合作为直接实施稿 |
| 人格/情感维度 | **未启动独立建设** | 画像中已有 `style` 面，但尚未形成独立的人格/情感能力层 |
| 主动工具增强 | **部分完成** | `remember` / `recall` 已交付；`forget` / `fact_check` 等未进入实现 |

结论: **旧路线图不是“没做”，而是已经被连续版本演进部分完成、部分绕开、部分淘汰。**

---

## 五、v0.9.0+ Roadmap

### 5.1 路线图原则

1. **先收敛主链路，再追求新能力。**
   在画像模型完全站稳前，不应并行推进大规模新抽象。

2. **优先处理两向门。**
   命名统一、模块边界、API 校验、成本控制都属于可逆决策，应尽快落地。

3. **推迟一向门。**
   新关系层、人格层、style_distill 插件级集成接口，都是更适合在 v1.0.0 之后再锁定的方向。

4. **图谱/关系层必须重做 ADR。**
   现有 `ADR-0001` 以 `memories` 为中心，已不再对应主产品模型；若未来推进，应以 `profile_items` / `profile_relations` 为中心重新定义。

---

### 5.2 v0.9.0 — 画像基线硬化（建议 1-2 周）

> 2026-05-08 已吸收 [TMEAAA-313](/TMEAAA/issues/TMEAAA-313)、[TMEAAA-314](/TMEAAA/issues/TMEAAA-314)、[TMEAAA-315](/TMEAAA/issues/TMEAAA-315) 的专项审计结论。

#### P0-1. 对外契约与版本口径收敛

- 对齐运行时注册版本、插件元数据、README、CHANGELOG
- 明确当前主产品模型是“用户画像”，而不是旧的三层 `memories` 叙事
- 梳理 `style_distill`、`refine/purify`、旧表等兼容层文档

预估投入: 0.5-1 天  
价值: 高  
决策类型: **两向门**

#### P0-2. 画像提取/蒸馏运行时硬化

- 为 profile extraction / distill 增加结构化错误分类
- 为 provider 失败、解析失败、空结果、fallback 路径建立可观测记录
- 增加 per-user / per-cycle 成本与节流可见性
- 在 `profile_extraction`、legacy flat distill、旧 consolidation 之间增加互斥门控，避免同一批 `conversation_cache` 数据被重复送入 LLM

预估投入: 2-3 天  
价值: 极高  
决策类型: **两向门**

#### P0-3. WebUI / API 安全性与验证补齐

- 强化写接口参数校验、错误码与负路径行为
- 增加画像编辑/归档/合并/配置更新的 HTTP 级验证
- 明确 `extra_user_temp` 回退行为的兼容矩阵
- 追加“注入热路径零 LLM 调用”回归验证，保护现有 plugin 生命周期边界

`extra_user_temp` 兼容矩阵:

| AstrBot 能力 | 注入落点 | 历史写入语义 | 当前验证 |
|------|------|------|------|
| `TextPart.mark_as_temp()` 可用 | `req.extra_user_content_parts` | 临时片段，不写入历史 | `tests/test_injection_chain.py` |
| `TextPart.mark_as_temp()` 不可用 | 回退到 `system_prompt` | 避免把普通 extra part 误写入历史 | `tests/test_injection_chain.py` |
| `enable_memory_injection = false` | 不注入 | 无额外 prompt 变更 | `tests/test_plugin_baseline.py` |
| `on_llm_request` 热路径 | 仅 SQLite 检索 + 拼装 | 零 LLM 调用 | `tests/test_plugin_baseline.py::test_hot001_on_llm_request_zero_llm_calls` |

预估投入: 2-3 天  
价值: 极高  
决策类型: **两向门**

#### P0-4. 画像时代的复杂度重切分

- 以当前真实复杂区为目标拆分，而不是继续围绕 `main.py`
- 优先考虑:
  - `core/utils.py` 按命令处理 / 注入辅助 / 运行时工具拆分
  - `core/admin_service.py` 按读操作 / 写操作 / 投影逻辑拆分
  - `core/consolidation.py` 明确 episode 逻辑与 profile extraction 逻辑边界
- 架构专项结论已明确 `core/utils.py` 是新的头号复杂度热点，应优先于其他结构整理工作

预估投入: 3-5 天  
价值: 高  
决策类型: **两向门**

#### P0-5. 注入热路径的语义召回补齐

- 当前 `on_llm_request` 注入路径应从“纯 FTS5/静态排序退化路径”补齐为“可选查询向量 + 混合召回”
- 保持热路径零 LLM 调用不变，只允许使用已有 embedding / SQLite 检索能力
- 明确 query embedding 的生成策略、缓存策略和回退策略

预估投入: 2-3 天  
价值: 极高  
决策类型: **两向门**

#### P0-6. 蒸馏 token 预算上限

- 新增日级 token 预算阈值，例如 `distill_daily_token_budget`
- 超预算后跳过后续蒸馏周期并记录告警/统计，而不是静默继续消耗
- 在 `/tm_distill_history` 或统计接口中暴露预算消耗视图

预估投入: 1-2 天  
价值: 高  
决策类型: **两向门**

#### P0-7. 自动化基线恢复为绿色

- 先修复当前 pytest 基线不绿的问题，再把其余 `v0.9.0` 工作挂到稳定测试门禁上
- 优先修复:
  - CFG-003 画像注入路径测试失败
  - pytest-asyncio strict / Python 3.9 下的 `asyncio.Semaphore` 事件循环初始化错误
  - 真实 AstrBot 自动化测试对运行时环境的硬依赖
- 验收应至少包括一条稳定的“全量 pytest 绿”路径，以及一条最小可复现的 AstrBot/OpenAPI smoke 路径

预估投入: 1-2 天  
价值: 极高  
决策类型: **两向门**

---

### 5.3 v0.9.1 — 兼容层与技术债收口（建议 1-2 周）

#### P1-1. 兼容命令与旧表清理策略

- 决定 `/tm_quality_refine`、`/tm_refine` 的废弃窗口
- 明确 `conversations` 表的移除或只读兼容策略
- 明确 `memory_episodes` 在画像时代的定位: 保留为辅助层、实验层，还是收敛掉产品叙事
- 架构专项建议已明确：`memories` / `memory_episodes` / `episode_sources` / `conversations` 是否继续保留，需要通过正式 ADR 决策，而不是继续默认并存

预估投入: 1-2 天  
价值: 高  
决策类型: **两向门**

#### P1-2. 配置入口硬化

- 启动时对未知配置键给出 warning
- 可选严格模式拒绝错误类型
- 整理现有嵌套配置和旧平铺兼容口径
- 将蒸馏预算、profile 上限等关键保护参数纳入统一配置校验面

预估投入: 1-2 天  
价值: 中高  
决策类型: **两向门**

#### P1-3. 迁移/回滚/兼容性专项 QA

- 画像表初始化与升级验证
- AstrBot 多版本兼容面回归
- WebUI 配置更新、用户合并、画像合并的回滚/异常测试
- 明确覆盖“旧数据无迁移路径”的现实风险，先补迁移/回滚验证，再做更激进的 schema 收敛

预估投入: 2-3 天  
价值: 高  
决策类型: **两向门**

#### P1-4. FTS/画像上限等静默风险修复

- 修复 FTS5 UPDATE 触发器在记忆更新场景下的静默一致性风险
- 将 `profile_max_items_per_user` 从“仅有配置项”升级为“真正执行的保护阈值”
- 补齐这些保护逻辑的自动化回归测试

预估投入: 1-2 天  
价值: 高  
决策类型: **两向门**

#### P1-5. WebUI HTTP 薄层补齐与 OpenAPI smoke 固化

- 将当前已有的 handler/service 级覆盖，扩展成 route 级 smoke:
  - profile CRUD
  - merge
  - distill trigger
  - config read/write
  - negative auth / invalid input
- 把本地 AstrBot OpenAPI 4 步验证固化为可重复脚本，用于每票最小 smoke

预估投入: 1-2 天  
价值: 高  
决策类型: **两向门**

---

### 5.4 v1.0.0 — 差异化能力扩展（建议在基线稳定后）

#### P2-1. 关系层 / 图谱层重立项

- 以 `profile_items` / `profile_relations` 为核心重写关系型 ADR
- 只允许 SQLite-native 轻量方案进入候选
- 禁止在没有明确验证收益前引入第二数据库或远端依赖
- 该方向在架构专项中被确认不应早于主链路收敛；因此保留在 v1.0.0 之后，而不前置到 v0.9.x

预估投入: 5-8 天  
价值: 高（长期）  
决策类型: **一向门**

#### P2-2. 主动工具升级为画像原生能力

- 在已有 `remember` / `recall` 基础上评估 `forget` / `fact_check`
- 工具输出与画像 facet 直接对齐，不再依赖旧 `memory_type` 叙事

预估投入: 2-4 天  
价值: 中高  
决策类型: **两向门**

#### P2-3. `style_distill` 的插件间接口，而非回归主仓库

- 若独立项目成熟，仅定义可选集成接口与数据交换边界
- 不恢复内嵌式 style_distill 逻辑
- 主插件只承担“接入点”，不承担独立插件的功能债务

预估投入: 2-3 天  
价值: 中  
决策类型: **一向门**

### 5.5 推荐执行顺序

1. `P0-7` 自动化基线恢复为绿色
2. `P0-1` 对外契约与版本口径收敛
3. `P0-2` 蒸馏/画像提取运行时硬化
4. `P0-3` WebUI / API 安全性与验证补齐
5. `P0-5` 注入热路径语义召回补齐
6. `P0-6` 蒸馏 token 预算上限
7. `P0-4` 复杂度重切分
8. `P1-1` + `P1-3` 兼容层/迁移策略与 QA 收口
9. `P1-4` + `P1-5` 静默风险修复与 smoke 固化

说明:
- 这是三方专项结论合并后的顺序，不再按“谁先想到谁先做”推进
- `P0-7` 必须排在最前，因为 QA 已确认当前自动化基线不绿；若不先修复，后续所有工作都会缺少可靠门禁
- `P0-4` 不再是第一步，而是在基线与验证面稳定后再做，以降低结构整理的回归风险
- 这也是 CTO 对三方结论的最终裁决：先做可逆、可验证、能快速降低风险的两向门，再决定表退役与大文件拆分这类一向门动作

---

## 六、style_distill 的路线图定位

当前结论:

1. **继续保持解耦。**
   `style_distill` 已从主插件剥离，这是正确方向。

2. **不把其状态不明视为主插件阻塞。**
   本仓库当前没有证据表明主功能依赖它才能成立；因此它不应阻塞 v0.9.0 的质量与架构收敛。

3. **只预留接口，不预埋复杂耦合。**
   若后续要重新联动，应通过清晰的数据契约或插件接口完成，而不是把 style-specific pipeline 再度塞回主仓库。

4. **文档必须清理遗留引用。**
   现存 `style_distill` 命令示例、旧 Docker 拓扑记录和兼容说明需要降级为历史文档，避免误导。

5. **当前 `StyleAnalyzer` 不视为残留债务。**
   它应继续作为零 token 成本的规则增强器存在，为画像蒸馏提示词提供风格上下文，而不是被误判为需要剔除的旧功能。

---

## 七、立即执行建议

### 本轮优先级

1. **先完成 v0.9.0 的三项硬化工作**
   - 对外契约/版本收敛
   - 画像提取与蒸馏运行时硬化
   - WebUI / API 安全性与验证补齐

2. **随后做真实复杂区拆分**
   - 优先拆 `core/utils.py`
   - 其次拆 `core/admin_service.py`
   - 最后决定 `core/consolidation.py` 中 episode / profile 的边界

3. **图谱与人格能力先暂停到 v1.0.0 以后**
   在主链路和兼容面尚未完全收口前，不建议开启新抽象扩张。

### 已创建的跟进子任务

- [TMEAAA-313](/TMEAAA/issues/TMEAAA-313) — AI/插件链路稳定性与 `style_distill` 影响评估
- [TMEAAA-314](/TMEAAA/issues/TMEAAA-314) — 架构审计与 v0.9.0 ADR 边界建议
- [TMEAAA-315](/TMEAAA/issues/TMEAAA-315) — QA 基线与路线图质量风险评估

当前状态:

- [TMEAAA-313](/TMEAAA/issues/TMEAAA-313) 已完成，其结论已并入本文
- [TMEAAA-314](/TMEAAA/issues/TMEAAA-314) 已完成，其架构结论已并入本文
- [TMEAAA-315](/TMEAAA/issues/TMEAAA-315) 已完成，其 QA 结论已并入本文

下一步不再是继续调研，而是基于三方结论拆出正式的 `v0.9.0` 实施工单。

---

## 八、风险与决策纪律

1. **不要引入外部基础设施。**
   继续守住单 SQLite、本地优先、低运维成本。

2. **不要为了“看起来更现代”而重写稳定链路。**
   画像模型已经跑通，接下来要做的是收敛和硬化，而不是第二次大迁移。

3. **不要同时推进两套产品叙事。**
   对外必须统一为“用户画像长期记忆插件”；旧 `memories` / `episodes` 只允许作为兼容层或内部实现层存在。

4. **所有扩张型能力先过 QA 与成本闸门。**
   包括图谱层、主动工具增强、人格层和独立插件集成。

---

*本路线图重置基于 2026-05-08 的仓库实际状态编制，并已吸收 [TMEAAA-313](/TMEAAA/issues/TMEAAA-313)、[TMEAAA-314](/TMEAAA/issues/TMEAAA-314)、[TMEAAA-315](/TMEAAA/issues/TMEAAA-315) 的专项结论。它现在可作为 issue `plan` 文档进入 board 确认流程。*
