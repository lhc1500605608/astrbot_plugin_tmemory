# v0.11.0 回滚预案

## 场景

v0.11.0 引入四项 Breaking Change：

| 编号 | 变更 | 影响 |
|------|------|------|
| BC-1 | 嵌入来源变更 — 默认优先使用 AstrBot Provider/知识库嵌入 | 旧独立配置不再自动生效 |
| BC-3 | 主动性默认关闭 — `proactive_enabled` 默认 `false` | 需显式开启 |
| BC-4 | 蒸馏规则分级 — 新增规则分级路径 | 旧纯 LLM 路径需显式恢复 |
| BC-5 | 导入写接口 — `/tm_import` 默认 dry-run | 写入前自动备份 |

如上线后发现异常，可按以下步骤回滚。

## 回滚步骤

### 1. 最小回滚：恢复 v0.10.0 行为

在 AstrBot 插件配置中设置：

```yaml
embedding_source: standalone    # BC-1: 恢复独立嵌入配置
proactive_enabled: false        # BC-3: 保持默认（已是默认值）
distill_rule_gating: false      # BC-4: 恢复纯 LLM 蒸馏路径
```

保存后重启 AstrBot。无需回退代码即可恢复 v0.10.0 行为。

### 2. 完整回滚到 v0.10.0

如需完全回退到 v0.10.0：

```bash
# 备份当前数据
cp -r data/ data_backup_v0.11.0_$(date +%Y%m%d%H%M%S)/

# 回退代码到 v0.10.0
git checkout v0.10.0 -- .

# 重启 AstrBot
docker compose restart astrbot
```

> ⚠️ v0.10.0 → v0.11.0 之间有嵌入来源和主动性变更，回退后 v0.11.0 新增的配置项会被忽略。
> 建议先备份 `data/` 目录。

### 3. 主动性回退

`proactive_enabled` 默认为 `false`，如已开启且出现异常：

```yaml
proactive_enabled: false
```

### 4. 蒸馏规则分级回退

如规则分级路径出现异常：

```yaml
distill_rule_gating: false
```

恢复为纯 LLM 蒸馏路径。

### 5. 导入接口回退

`/tm_import` 默认 dry-run 是安全行为，无需回退。如需强制写入，使用 `force=true` 参数。

## 监控与触发条件

| 指标 | 阈值 | 动作 |
|------|------|------|
| 嵌入维度不匹配错误 | 出现 `dimension mismatch` 日志 | 检查 `embedding_source` 配置；切换为 `standalone` 并重建向量索引 |
| 主动性消息发送失败 | 连续 3 次 `send_message` 失败 | 设置 `proactive_enabled: false` |
| 蒸馏规则分级异常 | 规则路径命中率 < 10% | 设置 `distill_rule_gating: false` |
| 导入写入异常 | `/tm_import` 非预期写入 | 检查备份是否自动创建；使用 `--dry-run` 验证 |

## 回滚命令速查

```bash
# 最小回滚：仅恢复嵌入来源配置（需在 AstrBot 配置界面操作）
# embedding_source: standalone

# 完整回滚
cp -r data/ data_backup_$(date +%Y%m%d%H%M%S)/
git checkout v0.10.0 -- .
docker compose restart astrbot

# 回滚后恢复
git checkout master -- .
docker compose restart astrbot
```