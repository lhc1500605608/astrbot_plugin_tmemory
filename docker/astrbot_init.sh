#!/bin/bash
# =============================================================================
# astrbot_init.sh — AstrBot + Ollama 本地 LLM 初始化入口
#
# 职责：
# 1. 等待 Ollama 服务就绪
# 2. 拉取对话模型和 Embedding 模型
# 3. 生成 AstrBot cmd_config.json（含 Ollama provider）
# 4. 启动 AstrBot
#
# 使用方式（在 docker-compose.yml 中）：
#   entrypoint: ["/bin/bash", "/docker/astrbot_init.sh"]
# =============================================================================
set -euo pipefail

# ── 环境变量（含默认值） ──────────────────────────────────────────────────────────
OLLAMA_BASE_URL="${OLLAMA_BASE_URL:-http://ollama:11434}"
OLLAMA_CHAT_MODEL="${OLLAMA_CHAT_MODEL:-qwen2.5:0.5b}"
OLLAMA_EMBED_MODEL="${OLLAMA_EMBED_MODEL:-nomic-embed-text}"
CMD_CONFIG="/AstrBot/data/cmd_config.json"

echo "[init] === AstrBot Docker Init ==="
echo "[init] OLLAMA_BASE_URL=$OLLAMA_BASE_URL"
echo "[init] OLLAMA_CHAT_MODEL=$OLLAMA_CHAT_MODEL"
echo "[init] OLLAMA_EMBED_MODEL=$OLLAMA_EMBED_MODEL"
echo "[init] CMD_CONFIG=$CMD_CONFIG"

# ── 步骤 1: 等待 Ollama 就绪 ────────────────────────────────────────────────────
echo "[init] 等待 Ollama 服务就绪..."
for i in $(seq 1 60); do
  if curl -sf "${OLLAMA_BASE_URL}/api/tags" > /dev/null 2>&1; then
    echo "[init] ✅ Ollama 服务已就绪（第 ${i}s）"
    break
  fi
  if [ "$i" -eq 60 ]; then
    echo "[init] ❌ Ollama 未能在 60s 内就绪，请检查 docker-compose logs ollama"
    exit 1
  fi
  sleep 1
done

# ── 步骤 2: 拉取对话模型 ────────────────────────────────────────────────────────
echo "[init] 拉取对话模型: ${OLLAMA_CHAT_MODEL}..."
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST \
  "${OLLAMA_BASE_URL}/api/pull" \
  -H "Content-Type: application/json" \
  -d "{\"name\": \"${OLLAMA_CHAT_MODEL}\", \"stream\": false}")
if [ "$HTTP_CODE" = "200" ]; then
  echo "[init] ✅ 对话模型已就绪: ${OLLAMA_CHAT_MODEL}"
else
  echo "[init] ⚠️ 对话模型拉取返回 HTTP ${HTTP_CODE}，首次请求时可能会自动拉取"
fi

# ── 步骤 3: 拉取 Embedding 模型 ──────────────────────────────────────────────────
echo "[init] 拉取 Embedding 模型: ${OLLAMA_EMBED_MODEL}..."
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST \
  "${OLLAMA_BASE_URL}/api/pull" \
  -H "Content-Type: application/json" \
  -d "{\"name\": \"${OLLAMA_EMBED_MODEL}\", \"stream\": false}")
if [ "$HTTP_CODE" = "200" ]; then
  echo "[init] ✅ Embedding 模型已就绪: ${OLLAMA_EMBED_MODEL}"
else
  echo "[init] ⚠️ Embedding 模型拉取返回 HTTP ${HTTP_CODE}"
fi

# ── 步骤 4: 生成 AstrBot 配置 ────────────────────────────────────────────────────
if [ -f "$CMD_CONFIG" ]; then
  echo "[init] 配置已存在: ${CMD_CONFIG}，跳过生成"
else
  echo "[init] 首次启动，生成 AstrBot 配置..."
  mkdir -p "$(dirname "$CMD_CONFIG")"

  python3 -c "
import json
import sys
import os

# 从 AstrBot 默认配置继承，保证配置完整性
sys.path.insert(0, '/AstrBot')
from astrbot.core.config.default import DEFAULT_CONFIG

config = dict(DEFAULT_CONFIG)

# 用 Ollama provider 替换空的 provider 列表
ollama_provider = {
    'type': 'openai_chat_completion',
    'id': 'ollama_local',
    'enable': True,
    'api_base': '${OLLAMA_BASE_URL}/v1',
    'key': ['ollama'],
    'model': '${OLLAMA_CHAT_MODEL}',
}
config['provider'] = [ollama_provider]
config['provider_settings']['default_provider_id'] = 'ollama_local'

# 确保 dashboard 密码哈希（用于 WebUI）
dashboard = config.get('dashboard', {})
if not dashboard.get('password'):
    dashboard['password'] = '77b90590a8945a7d36c963981a307dc9'

with open('${CMD_CONFIG}', 'w') as f:
    json.dump(config, f, ensure_ascii=False, indent=2)

print('[init] ✅ 配置已写入: ${CMD_CONFIG}')
"
  echo "[init] ✅ AstrBot 配置生成完成"
fi

# ── 步骤 5: 启动 AstrBot ────────────────────────────────────────────────────────
echo "[init] === 启动 AstrBot ==="
exec python /AstrBot/main.py
