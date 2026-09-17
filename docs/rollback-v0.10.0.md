# v0.10.0 回滚预案

## 场景
v0.10.0 将默认 WebUI 从旧 9966 独立端口面板切换为 AstrBot Dashboard Plugin Pages。
如上线后发现 Plugin Pages 不可用或行为异常，可按以下步骤回滚。

## 回滚步骤

### 1. 启用 legacy 面板（最小回滚）

在 AstrBot 插件配置中设置：

```yaml
webui_legacy_enabled: true
webui_enabled: true          # 必须同时开启
webui_password: "<your_pwd>" # 必须设置
webui_port: 9966             # 确认端口未被占用
```

保存后重启 AstrBot。legacy 面板将在 9966 端口恢复。

### 2. 元数据还原（完整回滚到 v0.9.0 行为）

如需完全回退到 v0.9.0：

```bash
# 回退 metadata.yaml
git show v0.6.0:metadata.yaml > metadata.yaml

# 回退代码
git checkout v0.6.0 -- .

# 重启 AstrBot
docker compose restart astrbot
```

> ⚠️ v0.6.0 → v0.10.0 之间有画像模型变更（v0.8.3），回退可能丢失新数据结构。
> 建议先备份 `data/` 目录。

### 3. 会话生命周期回退

`session_reset_policy` 默认为 `keep`，行为与旧版本一致。如已改为 `archive` 或 `clear`
且出现异常，改回 `keep` 即可。

## 监控与触发条件

| 指标 | 阈值 | 动作 |
|------|------|------|
| Plugin Pages 500 率 | > 5% 请求 | 检查 `_probe_plugin_pages()` 日志；若持续，启用 legacy 回滚 |
| 会话缓存异常增长 | archived_at 行数 > 正常 2x | 检查 `session_reset_policy` 配置 |
| Bridge 注册失败日志 | 出现 `ModuleNotFoundError` | 确认 AstrBot 版本 ≥4.28；否则启用 legacy |
| 蒸馏跳过告警 | 连续 3 个周期被跳过 | 检查 token budget 配置 |

## 回滚命令速查

```bash
# 最小回滚：仅启用 legacy 面板（需在 AstrBot 配置界面操作）
# webui_legacy_enabled=true

# 完整回滚
git checkout v0.6.0 -- .
docker compose restart astrbot

# 回滚后恢复
git checkout master -- .
docker compose restart astrbot
```
