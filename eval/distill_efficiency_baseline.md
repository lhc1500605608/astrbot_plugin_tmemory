# Distill Efficiency Baseline

- Sample: `distill-efficiency-baseline`
- Scenarios: `5`
- total_input_rows: `41`
- total_inserted_rows: `35`
- total_skipped_rows: `20`
- total_distill_runs: `5`
- total_memories_created: `5`
- skip_ratio: `0.488`
- distill_reduction_ratio: `0.488`

## Scenarios

| Scenario | Input Rows | Inserted Rows | Deduped Rows | Prefiltered Rows | Skipped Rows | LLM Rows | Distill Runs | Memories Created | Description |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| high_frequency_short_messages | 6 | 6 | 0 | 6 | 6 | 6 | 1 | 0 | 高频短消息：全部进入缓存，但在蒸馏前被低信息量过滤。 |
| repeated_low_value_messages | 6 | 2 | 4 | 2 | 2 | 2 | 1 | 0 | 重复低价值消息：capture 去重先拦掉重复，剩余候选在蒸馏前继续过滤。 |
| hot_user_burst_then_high_value_fact | 7 | 7 | 0 | 4 | 4 | 7 | 1 | 1 | 热点用户突发短消息后补充关键事实：高频噪声应被过滤，但稳定事实需要被保留。 |
| retained_memories | - | - | - | - | - | - | - | - | 用户下周开始需要控制糖分摄入，早餐改成无糖酸奶。 |
| long_session_mixed_signal_preserves_key_memories | 14 | 14 | 0 | 6 | 6 | 14 | 1 | 2 | 长会话夹杂低价值消息与关键信息：噪声应被过滤，但关键偏好与例行安排需要稳定保留。 |
| retained_memories | - | - | - | - | - | - | - | - | 用户每周二晚上会和产品团队做复盘。; 用户希望答复先列风险，再给执行建议。 |
| normal_effective_dialogue | 8 | 6 | 2 | 2 | 2 | 6 | 1 | 2 | 正常有效对话：仍允许有效上下文进入蒸馏，并产出稳定记忆。 |
| retained_memories | - | - | - | - | - | - | - | - | 用户每周三晚上会去游泳训练，并在准备铁三比赛。; 用户希望回答先给结论，再展开细节。 |

## 局限性

- 这是离线轻量基线，只验证当前门控与去重是否减少无效蒸馏，不代表真实线上吞吐或端到端时延。
- 样本规模很小，场景由人工构造，主要用于优化前后回归对比，而不是得出普适性能结论。
- 结果没有接入真实外部模型服务，因此不能说明不同模型、网络抖动或 provider 限速下的真实成本。