# ADR-003 Qdrant 残留配置边界裁定

- 状态：Accepted
- 日期：2026-04-22
- 关联：`_conf_schema.json`、`main.py`、[TMEAAA-80](/TMEAAA/issues/TMEAAA-80)

## 背景

在 [TMEAAA-68](/TMEAAA/issues/TMEAAA-68) 完成后，项目对外口径已经收敛为：

- 向量检索仅依赖 `sqlite-vec`
- 不再要求部署外部向量数据库
- `README.md` 也已按该口径描述配置与运行方式

但仓库中仍残留两类 Qdrant 痕迹：

1. `_conf_schema.json` 继续向用户暴露 `vector_backend=qdrant`、`qdrant_url`、`qdrant_api_key`、`qdrant_collection`、`fallback_to_sqlite_vec`
2. `main.py` 的 `_get_vector_retrieval_config()` 仍会兼容读取这些旧平铺键

经审查，当前运行时并不消费这些字段：

- `initialize()` 仅在 `enable_vector_search` 为真时初始化 `VectorManager`
- `VectorManager` 只读取 `embedding_provider`、`embedding_api_key`、`embedding_model`、`embedding_base_url`
- `main.py` 的向量配置解析只读取 `enable_vector_search`、Embedding 相关字段与 `vector_dim`

因此这些 Qdrant 项已不是“必要兼容边界”，而是“对用户可见但无行为效果的死配置”。

## 决策

删除当前插件中的 Qdrant 残留配置暴露与兼容读取。

保留的向量检索配置边界仅包括：

- `enable_vector_search`
- `embedding_provider`
- `embedding_api_key`
- `embedding_model`
- `embedding_base_url`
- `vector_dim`
- `auto_rebuild_on_dim_change`

删除的配置项：

- `vector_backend`
- `qdrant_url`
- `qdrant_api_key`
- `qdrant_collection`
- `fallback_to_sqlite_vec`

## 理由

1. 这些字段已无运行时语义。
2. 继续暴露会误导 QA 和用户，以为 Qdrant 仍是受支持路径或待补齐路径。
3. 继续保留“兼容读取但完全不用”的键，只会扩大配置面，而不会带来实际兼容收益。
4. 当前测试与公开文档均未把 Qdrant 视为活跃能力，删除风险低于继续模糊边界。

## 放弃了什么

- 不再尝试对旧 Qdrant 配置做静默兼容。
- 如果历史用户仍携带这些配置，插件会忽略未知键，而不是继续把它们纳入当前受支持配置集合。

这意味着旧配置文件中的 Qdrant 字段不会再被视为产品承诺的一部分。

## 结果与验收口径

- QA 不应再把“Qdrant 残留配置”视为未完成的后端能力 blocker。
- 若后续需要重新支持外部向量库，应以新的 ADR 和明确实现重新引入，而不是沿用当前残留字段。
- 本次收口标准是：用户界面/配置 schema/主配置兼容层不再声明 Qdrant 为当前支持能力。
