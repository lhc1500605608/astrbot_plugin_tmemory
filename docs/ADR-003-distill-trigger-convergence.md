# ADR-003 蒸馏触发收敛与批处理优化方案

- 状态：Accepted
- 日期：2026-04-21
- 关联：`main.py`、`README.md`、`tests/test_plugin_baseline.py`、`docs/ADR-002-main-boundary-tightening.md`

## 背景

当前插件的蒸馏链路已经从“每条消息实时蒸馏”收敛为后台批处理，但在触发与批次边界上仍然偏粗：

- 所有自动蒸馏统一由 `initialize()` 启动的单个 worker 驱动，按固定周期执行 `_run_distill_cycle(force=False, trigger="auto")`。
- 候选用户仅由 `_pending_distill_users()` 基于 `conversation_cache` 中 `distilled=0` 的行数筛选，条件是“某用户待蒸馏条数 >= distill_min_batch_count”。
- 选中用户后，`_fetch_pending_rows()` 按 `canonical_user_id` 取最早的一批消息，批次上限由 `distill_batch_limit` 控制。
- 蒸馏完成后统一 `_mark_rows_distilled()`，随后执行 `_optimize_context()` 做规则摘要压缩。

这比实时蒸馏已经节省很多，但仍有几个结构性问题：

1. 触发信号过于单一。
只看“未蒸馏条数”，不区分这些消息是否重复、是否只是寒暄、是否已经足够形成稳定记忆。

2. 缺少每用户冷却窗口。
当前只有全局 worker 周期，没有“同一用户上次蒸馏后至少等待多久”这一层收敛条件。热点用户只要持续累积新消息，就可能在每一轮 worker 中重复入选。

3. 批次边界只按用户聚合，不按主题或时间窗口收敛。
如果一个用户在较短时间内连续多轮闲聊，系统仍可能把整段原样送入 LLM，而不是先在缓存层做“窗口内合并后再蒸馏”。

4. 低价值消息过滤只发生在蒸馏输出后。
当前 `_validate_distill_output()` 会过滤低质量记忆，但在此之前，低价值消息已经进入候选批次，仍然消耗蒸馏机会。

本 ADR 的目标不是重写蒸馏系统，而是在保持 AstrBot 外部行为基本不变的前提下，给近期实现提供最小可执行的收敛策略。

## 现状分析

### 现有触发链路

当前自动蒸馏的关键行为如下：

1. `on_any_message()` / `on_llm_response()` 采集消息并写入 `conversation_cache`。
2. `_distill_worker_loop()` 每次 sleep 醒来后执行一轮 `_run_distill_cycle()`。
3. `_pending_distill_users()` 用 `COUNT(*)` 聚合待蒸馏行，返回待处理用户列表。
4. `_fetch_pending_rows()` 取某个用户最早的一批消息。
5. `_distill_rows_with_llm()` 将整批 transcript 交给 LLM，失败时回退到规则蒸馏。
6. `_validate_distill_output()` 在结果侧做垃圾过滤、类型修正、低置信度和低重要度剪枝。

### 为什么会显得“触发过频或低效”

这里的“过频”不一定表现为 worker 调度频率高，而是表现为“同一类低价值消息被过于容易地组成蒸馏批次”。根因主要有 4 个：

1. 候选消息进入门槛低。
除了 `capture_skip_prefixes` / `capture_skip_regex` 和命令过滤外，普通聊天内容几乎都会进入 `conversation_cache`。

2. 候选批次没有价值密度判断。
只要条数达标，哪怕 20 条消息中大部分是确认语、重复问法、短回复，也会进入蒸馏。

3. 用户粒度批处理会放大热点用户。
`_pending_distill_users()` 当前按未蒸馏条数倒序挑选用户，热点用户更容易持续占满每轮处理名额，其他用户延迟增加，同时热点用户自己的新消息也会反复被切成多批送入 LLM。

4. 缺少“已处理窗口”的显式边界。
当前用 `distilled` 布尔位表示“这一行是否已经进过蒸馏”，但没有“这批消息属于哪个收敛窗口”或“为什么此时值得蒸馏”的元信息，导致调优主要只能围绕全局阈值做粗调。

## 决策

采用“前置收敛 + 用户级节流 + 小窗口批处理”的三段式优化策略。

第一阶段不改插件的核心生命周期，不引入外部消息队列、不更换数据库、不拆成独立 distill service。只在现有 SQLite + 单 worker 模型上增加更细的候选筛选与批次边界。

## 第一阶段优先落地项

以下 4 项中，前 3 项为必须优先落地项，第 4 项为建议同阶段落地项。

### 1. 增加“低价值消息前置跳过”

目标：不要让明显不值得蒸馏的消息进入候选批次。

做法：

- 在写入 `conversation_cache` 前增加轻量级 `should_skip_distill_candidate` 判定。
- 该判定不改变“是否保留原始聊天记录”的总策略，但至少要能给后续蒸馏筛选提供标记。
- 第一阶段优先使用规则法，而不是引入 LLM 分类。

建议识别的低价值消息：

- 纯寒暄或确认：如「好的」「收到」「嗯嗯」「哈哈」。
- 长度很短且无实体词的消息。
- 与上一条同角色消息高度重复的消息。
- 纯表情、纯标点、纯数字编号之类内容。

推荐实现方式：

1. 为 `conversation_cache` 增加 `distill_candidate` 字段，默认 `1`。
2. `_insert_conversation()` 写入时同步写入该标记。
3. `_pending_distill_users()` 和 `_fetch_pending_rows()` 改为只统计 `distill_candidate=1 AND distilled=0`。

Trade-off：

- 收益：直接减少低价值消息把批次凑满的概率，是最便宜的频率优化。
- 代价：存在把少量短但重要的消息误判为低价值的风险。
- 控制措施：第一阶段规则保守，只过滤“极高把握的低信息量消息”，并保留原始行，不做物理删除。

### 2. 增加“每用户蒸馏冷却窗口”

目标：防止热点用户在每轮 worker 中被重复蒸馏。

做法：

- 为每个 `canonical_user_id` 记录最近一次自动蒸馏完成时间。
- 自动蒸馏选用户时，除了满足最小条数，还必须满足距上次自动蒸馏已超过冷却窗口。
- 手动命令 `/tm_distill_now` 保持 `force=True`，可绕过冷却窗口。

推荐实现方式：

1. 复用 `distill_history` 或新增用户级蒸馏状态表。
2. 第一阶段更推荐新增轻量表，例如 `distill_user_state(canonical_user_id, last_auto_distill_at, last_auto_distill_row_id)`，避免从全局历史反推每用户状态。
3. 新增配置项 `distill_user_cooldown_sec`，默认建议 6 小时，最小值建议 1 小时。

Trade-off：

- 收益：能显著压制单一用户连续高频蒸馏，提升系统整体吞吐稳定性。
- 代价：热点用户的新偏好进入长期记忆的时间会略延后。
- 控制措施：保留手动触发入口；只对自动蒸馏生效，不影响管理员紧急补蒸馏。

### 3. 将“按条数触发”改为“条数 + 时间窗口”双门槛

目标：让批次更像“一个相对完整的话题窗口”，而不是单纯攒够行数就送去蒸馏。

做法：

- 自动蒸馏时同时检查：
  - 候选消息条数达到 `distill_min_batch_count`。
  - 最早未蒸馏候选消息距离当前时间达到最小等待窗口。
- 如果条数已经达标但最早消息还很新，则继续等待，让同一段对话自然收口。

推荐实现方式：

1. 新增配置项 `distill_batch_window_sec`，默认建议 1800 秒到 3600 秒。
2. `_pending_distill_users()` 在聚合时同时取 `MIN(created_at)`，只返回“数量达标且窗口成熟”的用户。
3. `force=True` 时跳过该限制。

Trade-off：

- 收益：减少把一个还在进行中的话题拆成多个小批次反复蒸馏。
- 代价：记忆生成实时性下降，从“够条数就能进批次”变成“够条数且窗口成熟”。
- 控制措施：窗口默认设置为中等值，不追求严格实时；管理员仍可手动强制触发。

### 4. 限制单用户连续吃满批次的行为

目标：降低热点用户对整轮蒸馏的垄断，改善公平性。

做法：

- 自动蒸馏每轮只处理每个用户的一个窗口批次。
- 若该用户仍有剩余 `distill_candidate=1 AND distilled=0` 的消息，留待下一个 worker 周期，而不是当前循环内继续追批。
- 用户选择顺序从“按待蒸馏总条数倒序”调整为“优先成熟窗口 + 次级按上次蒸馏时间最久”。

Trade-off：

- 收益：避免热点用户持续占满批次，整体延迟更平滑。
- 代价：单用户积压在高峰期消化更慢。
- 控制措施：先不实现复杂优先级队列，只做简单排序调整。

## 第一阶段最小实现边界

AI Engineer 在第一阶段只需要做以下范围：

1. `conversation_cache` 增加候选标记字段。
2. 自动采集路径增加轻量低价值消息判定。
3. 自动蒸馏查询增加用户冷却与窗口成熟判断。
4. 保持 `tm_distill_now` 的强制语义不变。
5. 扩展 `tm_worker` 或 `tm_stats`，暴露以下至少 3 个观测值：
   - `pending_candidate_rows`
   - `pending_candidate_users`
   - `cooled_down_users` 或 `skipped_by_cooldown`
   - `skipped_low_value_rows`
6. 为上述新门槛补充基线测试。

第一阶段明确不做：

- 不重写 `_distill_worker_loop()` 为事件驱动或消息队列模型。
- 不把蒸馏逻辑拆到独立进程或外部任务系统。
- 不引入 LLM 级消息价值分类器。
- 不改长期记忆 schema 或召回排序逻辑。
- 不改变 AstrBot 命令名称、权限模型和 WebUI 基本行为。

## 明确暂不做项

以下 3 项属于刻意延后，而不是遗漏：

1. 不做外部数据库或 Supabase 迁移。

原因：当前问题是触发收敛，不是存储后端能力不足。把 SQLite 换成 PostgreSQL / Supabase 只会扩大改动面，不能直接解决“哪些消息值得蒸馏”的问题。

2. 不做完整的事件驱动蒸馏管线。

原因：将单 worker 改成消息队列、消费者、重试和死信机制，工程收益短期不匹配当前插件体量，也超出本 issue 范围。

3. 不做基于 embedding 的消息聚类批处理。

原因：虽然从主题一致性上更先进，但需要额外嵌入成本和更复杂的窗口划分逻辑，不适合作为近期最小方案。

## 数据模型建议

第一阶段数据层优先选择最小增量：

### 方案 A：在 `conversation_cache` 上加候选标记，在新表维护用户蒸馏状态

新增字段：

- `conversation_cache.distill_candidate INTEGER NOT NULL DEFAULT 1`

新增表：

- `distill_user_state`
  - `canonical_user_id TEXT PRIMARY KEY`
  - `last_auto_distill_at TEXT NOT NULL DEFAULT ''`
  - `last_auto_distill_row_id INTEGER NOT NULL DEFAULT 0`
  - `updated_at TEXT NOT NULL`

推荐该方案，原因：

- 对现有 `distill_history` 侵入最小。
- 用户级冷却判断可以直接查状态表，不需要从全局历史做聚合反推。
- 后续如果要增加 `skipped_low_value_rows`、`skipped_by_cooldown` 之类观测，也有明确落点。

### 方案 B：完全复用 `distill_history`

优点：少一张表。

缺点：

- 当前 `distill_history` 是“整轮历史”，不是“每用户历史”。
- 若强行扩展成每用户明细，会让全局历史和用户状态两个概念混在一起。

因此本 ADR 不推荐方案 B 作为第一阶段主实现。

## 对 AstrBot 生命周期的影响

第一阶段不改变现有生命周期主干：

- `initialize()` 仍然负责 DB 初始化、迁移和 worker 启动。
- `terminate()` 仍然只负责停止 worker、关闭向量管理器和 WebUI。
- `tm_distill_now` 仍然是管理员手动兜底入口。

变化只发生在 worker 选用户和取批次的内部判断上，因此不会改变 AstrBot 对插件的装配方式，也不会新增额外后台线程或独立服务。

## 对消息完整性与记忆遗漏风险的判断

### 消息完整性

- 第一阶段不删除任何原始消息。
- 即使判定为 `distill_candidate=0`，消息仍然可以留在 `conversation_cache` 中，供短期上下文和排障查看使用。
- 因此这不是“丢消息”，而是“降低进入长期记忆蒸馏候选集的概率”。

### 记忆遗漏风险

主要风险有两类：

1. 误把短但关键的消息过滤掉。
例如「以后都别叫我本名」虽然很短，但属于高价值 restriction。

2. 冷却窗口导致新偏好进入长期记忆变慢。
在高频对话用户上，这种延迟会更明显。

控制策略：

- 低价值过滤保持保守，只过滤高确定性废话。
- 对 restriction / task 类典型模式增加白名单优先级，避免被长度规则误伤。
- 手动蒸馏命令保留 `force` 语义。
- 通过 `tm_worker` / `tm_stats` 暴露跳过原因，便于调参。

## Trade-off

### 选择该方案得到什么

- 在不改外部行为的前提下，优先压缩无效蒸馏触发。
- 把“是否值得蒸馏”的判断前移到候选阶段，而不是继续把优化压力放在 LLM 输出后处理。
- 通过用户级冷却和时间窗口，让蒸馏更像“成段收敛”，而不是被动按条数攒批。

### 明确放弃什么

- 不追求近实时记忆沉淀。
- 不在第一阶段解决所有批处理公平性问题。
- 不同时推进外部数据库、消息队列和聚类蒸馏等更大改造。

## 实施顺序建议

1. 先补 schema 与查询边界。
内容：增加 `distill_candidate` 和 `distill_user_state`，改造 `_pending_distill_users()` / `_fetch_pending_rows()`。

2. 再补前置过滤与冷却判定。
内容：在 `_insert_conversation()` 之前或之中计算候选标记；蒸馏成功后写入用户级状态。

3. 最后补观测与测试。
内容：扩展 `tm_worker` / `tm_stats`；补充“低价值消息不会凑批次”“冷却期不会重复选中同一用户”“force 模式仍可执行”的测试。

## 验收口径

本 ADR 对后续实现的验收口径如下：

1. 至少落地 3 个频率优化点：
   - 低价值消息前置跳过
   - 每用户蒸馏冷却窗口
   - 条数 + 时间窗口双门槛
2. 至少明确 2 个暂不做项，并在实现中不顺带扩容。
3. 第一阶段不改变 AstrBot 生命周期和手动蒸馏命令的外部行为。
4. 新增观测能解释“为什么某用户当前没有被蒸馏”。
5. 测试至少覆盖：
   - 候选消息筛选
   - 自动蒸馏冷却
   - `force=True` 绕过限制
