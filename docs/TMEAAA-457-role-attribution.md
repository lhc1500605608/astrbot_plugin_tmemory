# TMEAAA-457：蒸馏按 role 区分发言者（助手发言不得写入用户画像）

## 根因

`conversation_cache` 中用户消息 `role=user`、助手消息 `role=assistant`，两者共用同一个
`canonical_user_id`（`core/handlers.py:52` / `:79`）。蒸馏阶段把整批行拼成一个 transcript
后：

1. **规则回退**（provider 不可用 / LLM 解析失败）直接对**含 assistant 行**的 transcript 调用
   `DistillManager.distill_text()`，剥离 `user:`/`assistant:` 前缀后合并成一条 `fact` 记忆，
   助手的原话因此被写进用户记忆（最严重、可稳定复现）。
2. **LLM 路径**的 prompt 只以一句软规则要求"排除 AI 说的话"，模型仍可能把助手发言归因给用户。
3. **画像提取**（`profile_extraction`）与 **consolidation Stage C** 使用同类的混合 transcript，
   存在同样的归因风险。

## 修复

三层防护，全部按 role 结构化区分：

1. **分区 transcript**：`core/distill_ops.py`、`core/profile_extraction_runtime.py` 生成
   `【用户发言（唯一可作为用户画像依据）】` 与 `【助手发言（仅作上下文参考，禁止作为用户画像依据）】`
   两个区块；prompt（`core/distill.py`、`core/profile_extractor.py`）明确只能从用户区块提取。
2. **规则回退只用用户发言**：`distill_text()` 的输入改为 user-only transcript
   （`distill_ops.py` 中 `user_transcript`）。
3. **确定性归因护栏** `core/attribution.py`：候选记忆若能在 assistant 发言中找到原文、
   且在 user 发言中找不到依据，则在入库前丢弃。覆盖 flat distill（自动 / 手动）、
   profile 提取、consolidation Stage C 三条链路，且不依赖 LLM。缓存命中路径同样过护栏。

护栏是保守的：只要内容同时出现在 user 发言中即保留（用户确实说过）。

## 如何识别并清理已产生的错误记忆

护栏只阻止新泄漏；修复前写入的历史记忆用
`core/maintenance.py::audit_assistant_attributed_memories(plugin, canonical_id, apply)`
审计清理：

```python
from astrbot_plugin_tmemory.core.maintenance import audit_assistant_attributed_memories

# 1) 预览命中（不改数据）
audit_assistant_attributed_memories(plugin, canonical_id="<user>")     # 单个用户
audit_assistant_attributed_memories(plugin)                            # 全量
# -> {"scanned": N, "flagged": [id...], "deactivated": 0}

# 2) 停用（is_active=0，可逆，不物理删除）
audit_assistant_attributed_memories(plugin, canonical_id="<user>", apply=True)
```

- 判定与在线护栏一致：只标记能在 `conversation_cache` 的 assistant 行中找到原文、
  且 user 行中无依据的记忆。
- **局限**：被改写（paraphrase）的错误记忆无法自动识别；`conversation_cache` 被裁剪后
  也可能失去对照原文。这类需人工复核。
- 人工复核入口：`/tm_memory` 或 WebUI 记忆管理，删除/改写可疑条目。

等价的 SQL 排查（`instr` 前先去掉空白）：

```sql
SELECT m.id, m.memory
FROM memories m
WHERE m.is_active = 1
  AND EXISTS (
    SELECT 1 FROM conversation_cache c
    WHERE c.canonical_user_id = m.canonical_user_id
      AND c.role = 'assistant'
      AND replace(c.content, ' ', '') <> ''
      AND instr(replace(m.memory, ' ', ''), replace(c.content, ' ', '')) > 0
  );
```

## 验证

- `tests/test_role_attribution.py`：10 项回归，覆盖规则回退、LLM 蒸馏（自动）、
  `/tm_distill_now`、WebUI `trigger_distill`、画像提取、prompt 分区与存量审计。
- 验收场景：用户说 A、助手说 B → 蒸馏后记忆只含 A，B 不出现。
- 本机 Docker（`astrbot_tmemory_test`，`localhost:6186`）真机验证。
