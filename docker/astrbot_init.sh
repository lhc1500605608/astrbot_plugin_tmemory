#!/bin/bash
# =============================================================================
# astrbot_init.sh — AstrBot + DeepSeek LLM provider 初始化入口
#
# 职责：
# 1. 验证 DeepSeek API 连通性
# 2. 生成 AstrBot cmd_config.json（含 DeepSeek provider）
# 3. 启动 AstrBot
#
# 使用方式（在 docker-compose.yml 中）：
#   entrypoint: ["/bin/bash", "/docker/astrbot_init.sh"]
# =============================================================================
set -euo pipefail

# ── 环境变量（含默认值） ──────────────────────────────────────────────────────────
DEEPSEEK_API_KEY="${DEEPSEEK_API_KEY:-}"
DEEPSEEK_MODEL="${DEEPSEEK_MODEL:-deepseek-v4-flash}"
DEEPSEEK_BASE_URL="${DEEPSEEK_BASE_URL:-https://api.deepseek.com}"
CMD_CONFIG="/AstrBot/data/cmd_config.json"

echo "[init] === AstrBot Docker Init ==="
echo "[init] DEEPSEEK_BASE_URL=$DEEPSEEK_BASE_URL"
echo "[init] DEEPSEEK_MODEL=$DEEPSEEK_MODEL"
echo "[init] CMD_CONFIG=$CMD_CONFIG"

# ── 步骤 1: 验证 API Key 并检查 DeepSeek API 连通性 ───────────────────────────────
if [ -z "$DEEPSEEK_API_KEY" ]; then
  echo "[init] ❌ DEEPSEEK_API_KEY 未设置"
  exit 1
fi
echo "[init] API Key 已配置（${#DEEPSEEK_API_KEY} 字符）"

echo "[init] 验证 DeepSeek API 连通性..."
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" \
  "${DEEPSEEK_BASE_URL}/v1/models" \
  -H "Authorization: Bearer ${DEEPSEEK_API_KEY}" \
  --connect-timeout 10 --max-time 15 2>/dev/null || echo "000")
if [ "$HTTP_CODE" = "200" ]; then
  echo "[init] ✅ DeepSeek API 连接正常"
else
  echo "[init] ⚠️ DeepSeek API 返回 HTTP ${HTTP_CODE}，将跳过连通性检测继续启动"
fi

# ── 步骤 2: 生成 AstrBot 配置（始终注入 DeepSeek provider） ─────────────────────
# 无论配置是否存在，都执行注入：读取现有配置或在默认配置基础上添加 DeepSeek provider。
# 这样可以确保即使 CMD_CONFIG 已存在（来自 repo 或持久化 volume），DeepSeek
# provider 也能被正确配置，而不会破坏用户的其他配置项。
mkdir -p "$(dirname "$CMD_CONFIG")"

python3 <<'PY'
import json
import os

cmd_config = os.environ.get('CMD_CONFIG', '/AstrBot/data/cmd_config.json')
deepseek_base_url = os.environ.get('DEEPSEEK_BASE_URL', 'https://api.deepseek.com')
deepseek_model = os.environ.get('DEEPSEEK_MODEL', 'deepseek-v4-flash')

# 尝试读取现有配置，否则从默认配置继承
if os.path.exists(cmd_config):
    with open(cmd_config, encoding='utf-8-sig') as f:
        config = json.load(f)
    print('[init] 读取现有配置: ' + cmd_config)
else:
    import sys
    sys.path.insert(0, '/AstrBot')
    from astrbot.core.config.default import DEFAULT_CONFIG
    config = dict(DEFAULT_CONFIG)
    # 确保 dashboard 密码哈希（用于 WebUI）
    dashboard = config.get('dashboard', {})
    if not dashboard.get('password'):
        dashboard['password'] = '77b90590a8945a7d36c963981a307dc9'

# 配置 DeepSeek 作为 LLM provider（OpenAI 兼容接口）。
# AstrBot v4.23+ 使用 provider_sources，并只在 provider_type=chat_completion 时解析
# key 字段中的 $ENV_NAME。保留环境变量占位，避免真实 key 落盘。
deepseek_source = {
    'id': 'deepseek_source',
    'type': 'openai_chat_completion',
    'provider_type': 'chat_completion',
    'api_base': deepseek_base_url.rstrip('/') + '/v1',
    'key': ['$DEEPSEEK_API_KEY'],
}
deepseek_provider = {
    'id': 'deepseek',
    'enable': True,
    'model': deepseek_model,
    'provider_source_id': 'deepseek_source',
    'modalities': [],
    'custom_extra_body': {},
}
config['provider_sources'] = [
    source for source in config.get('provider_sources', [])
    if source.get('id') != 'deepseek_source'
]
config['provider_sources'].append(deepseek_source)
config['provider'] = [deepseek_provider]
config.setdefault('provider_settings', {})
config['provider_settings']['default_provider_id'] = 'deepseek'

with open(cmd_config, 'w', encoding='utf-8') as f:
    json.dump(config, f, ensure_ascii=False, indent=2)

print('[init] ✅ DeepSeek provider 已注入: ' + cmd_config)
PY
echo "[init] ✅ AstrBot 配置就绪"

# ── 步骤 3: 启动 AstrBot ────────────────────────────────────────────────────────
echo "[init] === 启动 AstrBot ==="
exec python /AstrBot/main.py
