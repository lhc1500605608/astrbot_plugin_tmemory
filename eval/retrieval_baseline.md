# Retrieval Offline Eval Baseline

- Sample: `fts-baseline`
- Recall@3: `1.000` (4/4)

## Cases

| Query | Expected IDs | Retrieved IDs | Matched IDs | Hit | Notes |
| --- | --- | --- | --- | --- | --- |
| 黑 咖啡 | 1 | 1 | 1 | yes | 词面召回基线，按当前分词结果检查偏好类记忆是否能被 FTS 命中。 |
| 周三 游泳 | 2 | 2 | 2 | yes | 事实类记忆，检查时间信息是否被稳定召回。 |
| 结论 细节 | 3 | 3 | 3 | yes | 风格偏好类记忆，检查当前分词下的词面命中。 |
| 西班牙语 口语 | 4 | 4 | 4 | yes | 知识学习相关事实，检查词面召回。 |

## 局限性

- 样本量很小，只适合作为回归对照，不代表真实线上流量。
- 当前样本偏中文短句和单跳查询，不能说明复杂多意图查询的效果。
- 第一版基线不依赖外部 embedding 服务，因此不能代表真实向量召回质量。