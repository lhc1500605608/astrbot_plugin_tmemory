# MemoryForge

**MemoryForge** 是 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 的长期记忆插件，通过自动采集对话、LLM 蒸馏和分层注入，让机器人在多轮、跨会话、跨平台场景下持续理解用户。

> 当前版本：`v0.8.5`。0.8.5 补充蒸馏全链路集成测试与真实环境验证，移除冗余测试文件。

## 功能概览

- **用户画像架构**：以用户为中心的五维画像（偏好·事实·风格·限制·任务模式），结构化存储用户长期认知
- **自动采集**：监听用户消息，可选采集助手回复
- **画像形成**：后台 worker 从对话中提炼画像条目（profile_items），附带证据链溯源
- **主动工具**：`remember` / `recall` LLM tool，支持模型主动保存和检索记忆
- **画像注入**：在 LLM 请求前按画像面（facet）结构化注入上下文，零 LLM 热路径
- **混合检索**：FTS5 全文检索 + 可选向量检索 + RRF 融合排序，支持按画像面检索
- **记忆维护**：强化、衰减、固定、提纯、合并、拆分、失活
- **身份合并**：通过 `canonical_user_id` 合并同一用户跨平台记忆
- **WebUI 管理面板**：可选画像工作台、审计日志和手动蒸馏

## 工作流程

```text
用户/助手消息
  ↓ 自动采集
conversation_cache (原始证据)
  ↓ LLM 画像形成
profile_items (用户画像条目)
  ├─ preference  偏好
  ├─ fact        事实
  ├─ style       风格
  ├─ restriction 限制
  └─ task_pattern 任务模式
  ↓ FTS5 / 向量 / RRF 召回
LLM 请求前按画像面结构化注入
  ↓
模型生成更个性化的回复
```

`memory_mode` 控制写入路径：

| 模式 | 行为 |
|------|------|
| `hybrid` | 默认，同时启用后台蒸馏和主动工具记忆 |
| `distill_only` | 仅缓存 + 蒸馏，禁用 `remember` |
| `active_only` | 仅主动工具写入，停止后台蒸馏 |

## 快速开始

1. 在 AstrBot 插件市场安装，保持默认配置即可自动采集和注入记忆。
2. 对话累计到阈值后后台 worker 自动蒸馏。
3. 管理员可执行 `/tm_distill_now` 立即触发，`/tm_memory` 查看记忆，`/tm_context <问题>` 预览召回。

## 管理命令

以下命令均需 AstrBot `ADMIN` 权限。

| 命令 | 说明 |
|------|------|
| `/tm_memory` | 查看当前用户长期记忆 |
| `/tm_context <问题>` | 预览记忆召回上下文 |
| `/tm_distill_now` | 手动触发批量蒸馏 |
| `/tm_worker` | 查看蒸馏 worker 状态 |
| `/tm_stats` | 全局统计（含向量索引行数） |
| `/tm_distill_history` | 蒸馏历史和 token 成本 |
| `/tm_purify` | 全量记忆提纯 |
| `/tm_refine mode=both limit=20` | 手动提纯（合并/拆分/dry-run） |
| `/tm_mem_merge <id1,id2> <文本>` | 合并多条记忆 |
| `/tm_mem_split <id> [片段]` | 拆分记忆（可选 LLM 自动拆分） |
| `/tm_forget <id>` | 删除记忆 |
| `/tm_pin <id>` | 固定记忆（不受衰减/剪枝影响） |
| `/tm_unpin <id>` | 取消固定 |
| `/tm_export` | 导出当前用户记忆 JSON |
| `/tm_purge` | 删除当前用户全部记忆和缓存 |
| `/tm_bind <canonical_id>` | 绑定当前账号到统一用户 ID |
| `/tm_merge <from_id> <to_id>` | 合并两个用户 ID 的记忆 |
| `/tm_vec_rebuild [force=true]` | 重建向量索引 |

## LLM 工具

| 工具 | 参数 | 说明 |
|------|------|------|
| `remember` | `content`, `memory_type` | 保存长期记忆。type: preference/fact/task/restriction/style |
| `recall` | `query` | 检索相关记忆 |

## 画像维度

| 维度 | 含义 | 示例 |
|------|------|------|
| `preference` | 偏好 | "用户喜欢简洁回答" |
| `fact` | 事实 | "用户是 Python 开发者" |
| `task_pattern` | 任务模式 | "用户常在晚上9点后开始工作" |
| `restriction` | 约束 | "不要向用户推荐含花生的食物" |
| `style` | 风格 | "用户希望先给结论再解释" |

## 数据存储

本地 SQLite 数据库默认位置：

```text
data/plugin_data/astrbot_plugin_tmemory/tmemory.db
```

核心表：`identity_bindings`、`conversation_cache`、`user_profiles`、`profile_items`、`profile_item_evidence`、`profile_relations`、`memory_vectors`、`memory_events`、`distill_history`。

## 常见问题

**为什么没有立即生成长期记忆？** 默认需要单用户未蒸馏消息达到 `distill_min_batch_count`（默认 20），等待后台 worker。管理员可用 `/tm_distill_now` 手动触发。

**群聊为什么不注入私聊记忆？** 这是默认隐私保护。需开启 `private_memory_in_group`（注意隐私风险）。

**如何控制 LLM 成本？** 提高 `distill_min_batch_count`、增大 `distill_interval_sec`、使用 `distill_user_throttle_sec`，或切换到 `active_only` 模式完全依赖模型主动调用 `remember`。

**向量检索为空？** 确认 `sqlite-vec` 可用、API Key 有效、维度匹配，执行 `/tm_vec_rebuild`。

**WebUI 打不开？** 确认 `webui_enabled=true`、`webui_password` 已设置、端口未被占用。

