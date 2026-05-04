# ADR-004 配置兼容边界与规范形态

- 状态：Accepted
- 日期：2026-04-22
- 关联：`main.py`、`vector_manager.py`、`web_server.py`、`_conf_schema.json`、`README.md`

## 背景

插件近期在配置模型上已经开始从“根级平铺字段”向“按能力分组的嵌套对象”演进，例如：

- 向量检索配置收口到 `vector_retrieval`
- WebUI 配置收口到 `webui_settings`
- 蒸馏/提纯模型配置收口到 `distill_model_settings`

但实现、Schema 和文档并未同步完成收敛，已经出现 3 类兼容性风险：

1. 规范形态不明确。
`_conf_schema.json` 已经把嵌套对象作为主配置形态，但 `README.md` 仍主要展示旧的根级字段名。

2. 读取边界不一致。
`main.py`、`VectorManager`、`web_server.py` 对“调用方传入的是完整配置还是子配置”的假设并不统一，导致局部模块可能读不到用户的真实配置。

3. 兼容策略散落在实现细节中。
目前有些路径做了旧字段回退，有些路径直接假设新结构存在。这种“半迁移状态”短期可运行，但对后续工程师不透明，容易重复引入配置漂移。

本 issue 的目标不是重做配置系统，也不是一次性清理所有旧字段，而是明确近期的最小兼容边界，避免后续继续发生“文档、Schema、实现三份契约”分裂。

## 决策

采用“嵌套对象为规范形态，根级平铺字段为兼容输入”的双轨读取策略。

### 规范形态

以下 3 组配置以嵌套对象作为唯一规范写法：

1. `vector_retrieval`
2. `webui_settings`
3. `distill_model_settings`

`_conf_schema.json` 和后续新增文档都应以嵌套对象为准，不再把根级平铺字段当作主写法继续扩散。

### 兼容形态

为避免破坏已部署实例，近期仍接受旧的根级字段输入，但只作为读取兼容层，不再作为新的设计基线：

- `enable_vector_search`、`embedding_provider`、`embedding_api_key`、`embedding_model`、`embedding_base_url`、`vector_dim` 等根级向量字段
- `webui_enabled`、`webui_host`、`webui_port`、`webui_username`、`webui_password` 等根级 WebUI 字段
- `distill_provider_id`、`distill_model_id`、`purify_provider_id`、`purify_model_id` 等根级蒸馏字段

### 优先级规则

当同一配置同时存在嵌套对象和根级字段时，优先级如下：

1. 嵌套对象中的显式值优先
2. 根级旧字段只在嵌套对象未提供该键时回退使用
3. 默认值最后兜底

这保证迁移中的用户可以逐步搬迁配置，而不会因为同时保留旧字段而被旧值反向覆盖。

## 实现约束

近期实现任务必须遵守以下边界：

1. 配置读取必须在边界层做规范化。
例如 `main.py` 可以提供 `_get_vector_retrieval_config()` 一类方法，把“新结构 + 旧字段 + 默认值”先收口，再传给下游模块。

2. 下游模块不应重复猜测上游传入形态。
如果某模块同时要兼容“完整配置”和“子配置”，兼容逻辑必须在模块入口集中处理，而不是在多个方法里反复 `get("vector_retrieval", {})`。

3. 新增能力优先进入嵌套对象。
后续新增向量/WebUI/蒸馏相关配置时，只加入相应嵌套对象，不再新增新的根级同义字段，除非另开 issue 明确说明。

4. 文档和 Schema 必须先对齐规范形态，再考虑是否保留旧字段示例。
若文档为了迁移需要提到旧字段，必须明确标注“兼容旧写法”，不能与规范写法并列呈现为同等级主配置。

## 当前审计结论

### 已确认兼容的区域

1. `distill_model_settings`
`main.py` 已支持从嵌套对象读取，并对旧的 `distill_*` / `purify_*` 根级字段做回退。

2. `webui_settings`
`main.py` 在加载 WebUI 时会先复制完整配置，再将 `webui_settings` 展开覆盖到传入 `web_server.py` 的配置对象上，因此新旧写法都能被 `TMemoryWebServer` 识别。

### 已确认并已修复的缺陷

1. `vector_retrieval` 与 `VectorManager` 的配置边界不一致。
问题表现：`main.py` 传入的是 `vector_retrieval` 子配置，但 `VectorManager` 内部再次按完整配置读取 `vector_retrieval`，导致用户配置丢失。

修复策略：

- 在 `main.py` 对向量配置做集中规范化
- 在 `VectorManager` 入口兼容“完整配置 / 子配置”两种传参形态
- 补充回归测试锁住该行为

## Trade-off

### 选择该方案得到什么

- 不需要本 issue 内重写配置系统，也能先把最危险的契约漂移收住。
- 给后续模块拆分提供稳定前提：下游只消费规范化后的配置，而不是自行猜配置结构。
- 用户已有配置短期不必一次性迁移，降低升级阻力。

### 明确放弃什么

- 不在本阶段移除所有旧根级字段。
- 不引入 dataclass / pydantic 级的完整配置模型重构。
- 不保证 README 在本次之后已经覆盖所有迁移示例，只要求后续文档以嵌套对象为准。

## 后续约束

后续若要继续推进配置收敛，应按以下顺序进行：

1. 先补齐规范化读取函数和回归测试
2. 再修正文档与示例配置
3. 最后才考虑移除旧字段兼容，并单独发布迁移说明

若未来引入 PostgreSQL / Supabase / 外部检索服务，也必须继续遵守本 ADR 的原则：

- 外部能力配置进入相应嵌套对象
- 兼容层集中在边界入口
- 下游模块只消费规范化配置，不直接承担迁移逻辑
