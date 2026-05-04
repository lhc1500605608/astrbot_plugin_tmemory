# TMemory v0.5.0 — 项目现状与优化方向大纲

> 编制日期: 2026-05-01 | 当前版本: v0.4.0 | 编制依据: 全代码库审计 + ADR审查 + 竞品分析

---

## 一、项目现状总览

### 1.1 架构概览

```
AstrBot Framework (hooks)
        │
        ▼
main.py (1892行, 插件入口) ─── 委托 ───► core/ (14个模块)
        │                                    ├── config.py     配置解析
        │                                    ├── db.py         数据库管理 (SQLite+WAL)
        │                                    ├── capture.py    消息采集过滤
        │                                    ├── identity.py   用户身份合并
        │                                    ├── memory_ops.py 记忆CRUD+蒸馏调度
        │                                    ├── distill.py    蒸馏提示词+LLM调用
        │                                    ├── distill_validator.py 输出校验
        │                                    ├── maintenance.py 衰减/清理/统计/导出
        │                                    ├── vector.py     向量嵌入/重建/重排
        │                                    ├── llm_helpers.py LLM输出解析
        │                                    ├── admin_service.py WebUI业务逻辑
        │                                    └── utils.py      日志工具
        │
        ├── web_server.py (541行) 独立 aiohttp WebUI + JWT认证
        ├── hybrid_search.py (148行) FTS5+KNN+RRF混合检索
        ├── embeddingProvider.py (221行) 嵌入提供商抽象
        ├── search/retrieval.py (178行) 检索编排+去重+打分
        └── tests/ (196个测试, 100%通过)
```

### 1.2 关键数据

| 指标 | 数值 |
|------|------|
| 核心代码行数 | ~8,700 行 (含测试) |
| 核心模块数 | 14 个 |
| 测试用例数 | 196 个 (全部通过) |
| main.py 规模 | 1,892 行 (从 3,472 行重构减半) |
| 配置文件键数 | ~35 个 (嵌套分组) |
| 管理命令数 | 18 个 `/tm_*` 命令 |

### 1.3 近期重大变更 (v0.3.x → v0.4.0)

1. **风格蒸馏完全剥离** (TMEAAA-179/180/181) — style_distill 功能已独立为单独项目，零功能耦合残留
2. **Docker LLM 从 Ollama 迁移到 DeepSeek** (TMEAAA-166/170) — 含 E2E 验证脚本
3. **main.py 模块化拆分** (ADR-002) — 14个核心模块从单体中抽离
4. **Qdrant 残留清除** (ADR-003) — 向量后端收敛为仅 sqlite-vec
5. **配置架构现代化** (ADR-004) — 平面配置 → 嵌套对象 (vector_retrieval.* 等)
6. **WebUI 应用服务边界** (ADR-005) — AdminService 隔离前后端

---

## 二、当前优势

1. **零外部依赖部署** — SQLite + sqlite-vec，不需要 MySQL/Redis/Qdrant/Milvus
2. **混合检索质量** — FTS5(jieba分词) + KNN向量 + RRF融合，Recall@3=1.0 (baseline)
3. **跨平台身份合并** — `/tm_bind` 统一不同聊天平台上的同一用户记忆
4. **冲突检测+衰减机制** — 新记忆自动检测旧记忆冲突并标记失效，未召回记忆自动降权
5. **敏感信息脱敏** — 手机号/邮箱/身份证等自动替换
6. **蒸馏效率** — 前置过滤 skip_ratio 达 48.8%，大量减少无效LLM调用
7. **可选的独立 WebUI** — JWT认证 + IP白名单，管理记忆/用户/蒸馏

---

## 三、当前短板与技术债务

### 3.1 关键问题 (Critical)

| # | 问题 | 影响 |
|---|------|------|
| C1 | **main.py 仍是 God Object (1892行)** — 包含~50个薄委托方法、18个命令处理、HTTP服务加载 | 维护成本高，新人难以理解 |
| C2 | **蒸馏全流程无集成测试** — 最重要的代码路径(capture→distill→insert→inject)从未端到端验证 | 核心功能回归风险 |

### 3.2 高优先级问题 (High)

| # | 问题 |
|---|------|
| H1 | **AdminService 与 main.py 存在重复逻辑** — `_fetch_memory_by_id` 等在两处独立实现 |
| H2 | **配置向后兼容包袱** — `refine_quality_*` / `manual_refine_*` 旧键别名混乱 |
| H3 | **废弃 `conversations` 表未清理** — 与活跃表 `conversation_cache` 共存 |
| H4 | **蒸馏异常处理过于宽泛** — 通用 try/except + 日志，无结构化错误类型和重试策略 |
| H5 | **WebUI 写接口缺乏输入校验** — add_memory/update_memory/merge_users 无类型/存在性检查 |

### 3.3 文档问题

| # | 问题 |
|---|------|
| D1 | **README.md 严重过时** — 配置表仍引用已删除的旧键名 (embed_model, vector_weight, refine_quality_* 等) |
| D2 | README 仅记录 13/18 个管理命令 |
| D3 | README 未提及嵌套配置对象 (vector_retrieval.*, webui_settings.* 等) |
| D4 | "style" 记忆类型残留在文档和代码注释中（功能已移除） |

---

## 四、v0.5.0 优化方向

### 优先级排序: 按影响/投入比从高到低

---

### P0 — 必须完成 (质量地基)

#### 1. main.py 进一步拆分 (目标 ≤600行)
- 将 18 个 `/tm_*` 命令提取到 `core/commands.py`
- 将 WebUI 加载逻辑移至 `web_server.py` 工厂函数
- 消除 50+ 个薄委托方法(调用方直接 import core/ 模块)
- **投入**: 3-5天 | **影响**: 极高 — 后续所有开发效率提升

#### 2. 蒸馏全流程集成测试
- 编写: 写入 conversation_cache → mock LLM → 调用 run_distill_cycle → 验证记忆入库
- 覆盖: 正常蒸馏 / 规则降级 / 冲突检测 / 衰减触发
- **投入**: 2-3天 | **影响**: 极高 — 守住核心价值主张

#### 3. README.md 全面更新
- 配置表改为当前 `_conf_schema.json` 结构 (嵌套分组)
- 补全 18 个 `/tm_*` 命令文档
- 移除所有 "style" 记忆类型引用
- 添加快速开始指南 (含 Docker 方式)
- **投入**: 0.5天 | **影响**: 高 — 消除新用户配置障碍

---

### P1 — 应该完成 (消除技术债)

#### 4. 消除 AdminService/main.py 重复代码
- 抽取 `_fetch_memory_by_id`, `_fetch_memories_by_ids`, `_update_memory_text`, `_auto_merge_memory_text` 到 `core/memory_access.py`
- AdminService 和 main.py 统一 import
- **投入**: 1天 | **影响**: 中高 — 消除双写bug风险

#### 5. 配置命名统一 (purify 替代 refine)
- 废弃旧别名: `/tm_refine` → `/tm_purify`, `manual_refine_*` → `manual_purify_*`
- 保留一版本向后兼容，输出 deprecation warning
- **投入**: 0.5天 | **影响**: 中 — 消除用户困惑

#### 6. 清理废弃 conversations 表
- 移除 DDL 定义
- purge/export 逻辑仅操作 conversation_cache
- 添加迁移: 如有旧数据则合并
- **投入**: 0.5天 | **影响**: 低 — 减少存储浪费

---

### P2 — 锦上添花 (体验与健壮性)

#### 7. WebUI 接口集成测试
- 测试 10+ API 端点: CRUD, merge, distill触发, config读写
- 利用已有 AdminService 单元测试，仅加 HTTP 薄层
- **投入**: 1-2天 | **影响**: 中 — 防止 WebUI 回归

#### 8. 蒸馏管线结构化错误处理
- 定义: `DistillProviderError`, `DistillParseError`, `DistillValidationError`
- 添加: 每用户错误计数 + 退避重试
- **投入**: 1-2天 | **影响**: 中 — 生产可诊断性

#### 9. 配置 schema 启动校验
- 启动时警告无法识别的配置键
- 对类型不匹配做显式拒绝(可选严格模式)
- **投入**: 0.5天 | **影响**: 中 — 配置问题前置暴露

---

### P3 — 探索性 (为 0.6.0 铺路)

#### 10. 轻量知识图谱层 (ADR-0001 提案)
- `memory_entities` + `memory_edges` 邻接表
- 纯 SQLite，不引入外部图数据库
- 记忆关联查询: "张三喜欢吃什么" → 遍历实体边
- **投入**: 5-8天 | **影响**: 高(长期) — 差异化竞争力

#### 11. 人格/情感维度
- 竞品分析指出的最显著差距
- 从蒸馏输出中提取用户人格特征
- 注入时附带人格上下文
- **投入**: 5-8天 | **影响**: 高 — 追上 Mnemosyne/ATRI 的情感能力

#### 12. 主动交互工具增强
- 扩展 remember/recall 主动工具模式
- 增加: forget(主动遗忘), fact_check(记忆一致性核查)
- **投入**: 2-3天 | **影响**: 中高 — 提升智能体能力

---

## 五、建议发版节奏

```
v0.4.0 (当前) ────────────────────────────────────────────────
    │
    ▼ 2-3周
v0.5.0 — P0全部 + P1部分
    ├── main.py 拆分 (≤800行)
    ├── 蒸馏集成测试
    ├── README 重写
    ├── 重复代码消除
    └── 配置命名统一
    │
    ▼ 3-4周
v0.5.1 — P1收尾 + P2全部
    ├── conversations 表清理
    ├── WebUI 集成测试
    ├── 蒸馏错误处理
    └── 配置校验
    │
    ▼ 4-6周
v0.6.0 — P3 选1-2项
    └── 知识图谱层 / 人格维度 / 主动工具
```

---

## 六、风险与注意事项

1. **不要引入外部依赖** — 保持"零外部依赖"是 tmemory 的核心竞争力
2. **避免过度抽象** — 竞品分析显示用户更看重可靠性而非功能数量
3. **生产验证优先** — 任何新功能必须先有集成测试再合并
4. **README 是门面** — 当前文档状态正在损伤新用户的第一印象
5. **蒸馏管线是生命线** — 这是最复杂的代码路径，也是最需要测试覆盖的

---

*本大纲基于 2026-05-01 全代码库审计，涵盖 14 个核心模块、8 份 ADR、35+ 配置键、196 个测试用例和竞品分析数据。*
