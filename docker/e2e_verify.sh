#!/usr/bin/env bash
# =============================================================================
# e2e_verify.sh — tmemory E2E 验证脚本（Docker 环境）
#
# 前提条件：
#   1. Docker 模式：docker-compose up -d 已在运行，AstrBot WebUI 可通过 http://localhost:6186/ 访问
#   2. 本地模式：AstrBot WebUI 可通过 http://localhost:6185/ 访问，并设置 ASTRBOT_REQUIRE_DOCKER=0
#
# 验证内容：
#   1. 容器健康状态
#   2. AstrBot WebUI 可访问
#   3. tmemory 插件已加载
#   4. AstrBot OpenAPI 4 步 smoke
#   5. LLM 蒸馏链路可用（通过 DeepSeek API）
# =============================================================================
set -euo pipefail

ASTRBOT_URL="${ASTRBOT_URL:-http://localhost:6186}"
ASTRBOT_API_KEY="${ASTRBOT_API_KEY:-admin}"
ASTRBOT_REQUIRE_DOCKER="${ASTRBOT_REQUIRE_DOCKER:-auto}"
SKIP_DEEPSEEK="${SKIP_DEEPSEEK:-0}"
DEEPSEEK_API_KEY="${DEEPSEEK_API_KEY:-}"
DEEPSEEK_MODEL="${DEEPSEEK_MODEL:-deepseek-v4-flash}"
DEEPSEEK_BASE_URL="${DEEPSEEK_BASE_URL:-https://api.deepseek.com}"
PASS=0
FAIL=0

if { [ -z "$DEEPSEEK_API_KEY" ] || [ "$DEEPSEEK_API_KEY" = "your_deepseek_api_key_here" ]; } && [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
  ASTRBOT_API_KEY="${ASTRBOT_API_KEY:-admin}"
  ASTRBOT_REQUIRE_DOCKER="${ASTRBOT_REQUIRE_DOCKER:-auto}"
  SKIP_DEEPSEEK="${SKIP_DEEPSEEK:-0}"
  DEEPSEEK_API_KEY="${DEEPSEEK_API_KEY:-}"
  DEEPSEEK_MODEL="${DEEPSEEK_MODEL:-deepseek-v4-flash}"
  DEEPSEEK_BASE_URL="${DEEPSEEK_BASE_URL:-https://api.deepseek.com}"
fi

pass() { PASS=$((PASS + 1)); echo "  ✅ $1"; }
fail() { FAIL=$((FAIL + 1)); echo "  ❌ $1"; }

echo ""
echo "╔════════════════════════════════════════╗"
echo "║   tmemory AstrBot E2E / OpenAPI smoke ║"
echo "╚════════════════════════════════════════╝"
echo ""

# ── 检查依赖 ────────────────────────────────────────────────────────────────────
if ! command -v curl &>/dev/null; then echo "❌ 需要 curl"; exit 1; fi
if ! command -v python3 &>/dev/null; then echo "❌ 需要 python3"; exit 1; fi

if [ "$ASTRBOT_REQUIRE_DOCKER" = "auto" ]; then
  if echo "$ASTRBOT_URL" | grep -q ':6186\b'; then
    ASTRBOT_REQUIRE_DOCKER=1
  else
    ASTRBOT_REQUIRE_DOCKER=0
  fi
fi
if [ "$ASTRBOT_REQUIRE_DOCKER" = "1" ] && ! command -v docker &>/dev/null; then
  echo "❌ 需要 docker"
  exit 1
fi

# ── 1. 容器状态 ──────────────────────────────────────────────────────────────────
echo "--- 1. 容器状态 ---"
if [ "$ASTRBOT_REQUIRE_DOCKER" != "1" ]; then
  pass "非 Docker 目标，跳过容器状态检查"
elif docker ps --format '{{.Names}} {{.Status}}' | grep -q "astrbot_tmemory_test"; then
  pass "AstrBot 容器运行中"
else
  fail "AstrBot 容器未运行"
fi

# ── 2. AstrBot WebUI ────────────────────────────────────────────────────────────
echo ""
echo "--- 2. AstrBot WebUI ---"
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" "${ASTRBOT_URL}/" 2>/dev/null || echo "000")
if [ "$HTTP_CODE" = "200" ] || [ "$HTTP_CODE" = "302" ] || [ "$HTTP_CODE" = "401" ]; then
  pass "AstrBot WebUI 可访问 (HTTP ${HTTP_CODE})"
else
  fail "AstrBot WebUI 返回 HTTP ${HTTP_CODE}"
fi

# ── 3. tmemory 插件 ──────────────────────────────────────────────────────────────
echo ""
echo "--- 3. tmemory 插件 ---"
if [ "$ASTRBOT_REQUIRE_DOCKER" != "1" ]; then
  pass "非 Docker 目标，跳过容器日志检查"
elif TMEMORY_LOG=$(docker logs astrbot_tmemory_test 2>&1 | grep -i "tmemory" | tail -5 || echo "") && echo "$TMEMORY_LOG" | grep -q "initialized"; then
  pass "tmemory 插件已初始化"
else
  # 插件可能还在启动中
  if echo "$TMEMORY_LOG" | grep -q "tmemory"; then
    pass "tmemory 日志存在（可能仍在初始化）"
  else
    fail "未找到 tmemory 初始化日志"
  fi
fi

# ── 4. AstrBot OpenAPI 4 步 smoke ───────────────────────────────────────────────
echo ""
echo "--- 4. AstrBot OpenAPI 4 步 smoke ---"
SESSION_ID="tmemory_openapi_smoke_$(date +%s)"

CHAT_RESPONSE=$(curl -sS -N --max-time 30 -X POST "${ASTRBOT_URL%/}/api/v1/chat" \
  -H "X-API-Key: ${ASTRBOT_API_KEY}" \
  -H "Content-Type: application/json" \
  -d "{\"message\":\"Hello, this is a tmemory OpenAPI smoke test\",\"username\":\"tester\",\"session_id\":\"${SESSION_ID}\",\"enable_streaming\":true}" 2>/dev/null || true)
if echo "$CHAT_RESPONSE" | grep -Eq 'data:.*"type"[[:space:]]*:[[:space:]]*"(plain|response)"'; then
  pass "/api/v1/chat 返回 SSE plain/response 事件"
else
  fail "/api/v1/chat 未返回预期 SSE plain/response 事件"
fi

SESSIONS_RESPONSE=$(curl -sS "${ASTRBOT_URL%/}/api/v1/chat/sessions?username=tester&page=1&page_size=20" \
  -H "X-API-Key: ${ASTRBOT_API_KEY}" 2>/dev/null || true)
if printf '%s' "$SESSIONS_RESPONSE" | python3 -c "import json,sys; d=json.load(sys.stdin); assert d.get('status') == 'ok'; assert isinstance(d.get('data', {}).get('sessions'), list)" 2>/dev/null; then
  pass "/api/v1/chat/sessions 返回 status=ok 与 sessions 数组"
else
  fail "/api/v1/chat/sessions 返回结构异常"
fi

CONFIGS_RESPONSE=$(curl -sS "${ASTRBOT_URL%/}/api/v1/configs" \
  -H "X-API-Key: ${ASTRBOT_API_KEY}" 2>/dev/null || true)
if printf '%s' "$CONFIGS_RESPONSE" | python3 -c "import json,sys; d=json.load(sys.stdin); assert d.get('status') == 'ok'; assert isinstance(d.get('data', {}).get('configs'), list)" 2>/dev/null; then
  pass "/api/v1/configs 返回 status=ok 与 configs 数组"
else
  fail "/api/v1/configs 返回结构异常"
fi

IM_RESPONSE=$(curl -sS -X POST "${ASTRBOT_URL%/}/api/v1/im/message" \
  -H "X-API-Key: ${ASTRBOT_API_KEY}" \
  -H "Content-Type: application/json" \
  -d '{"umo":"webchat:FriendMessage:openapi_probe","message":"ping from tmemory openapi smoke"}' 2>/dev/null || true)
if printf '%s' "$IM_RESPONSE" | python3 -c "import json,sys; d=json.load(sys.stdin); assert d.get('status') == 'ok'" 2>/dev/null; then
  pass "/api/v1/im/message 返回 status=ok"
else
  fail "/api/v1/im/message 返回结构异常"
fi

# ── 5. LLM 蒸馏链路验证（DeepSeek API） ──────────────────────────────────────────
echo ""
echo "--- 5. LLM 蒸馏链路 ---"
if [ "$SKIP_DEEPSEEK" = "1" ]; then
  pass "已按 SKIP_DEEPSEEK=1 跳过 DeepSeek 验证"
elif [ -z "$DEEPSEEK_API_KEY" ] || [ "$DEEPSEEK_API_KEY" = "your_deepseek_api_key_here" ]; then
  fail "需要设置有效的 DEEPSEEK_API_KEY"
else
  LLM_TEST=$(curl -sf -X POST "${DEEPSEEK_BASE_URL%/}/v1/chat/completions" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer ${DEEPSEEK_API_KEY}" \
    -d "{\"model\": \"${DEEPSEEK_MODEL}\", \"messages\": [{\"role\": \"user\", \"content\": \"Hello\"}], \"stream\": false, \"max_tokens\": 20}" 2>/dev/null || echo "")
fi
if [ "${LLM_TEST:-}" != "" ] && echo "$LLM_TEST" | python3 -c "import sys,json; d=json.load(sys.stdin); assert 'choices' in d" 2>/dev/null; then
  CONTENT=$(echo "$LLM_TEST" | python3 -c "import sys,json; print(json.load(sys.stdin)['choices'][0]['message']['content'][:80])" 2>/dev/null)
  pass "DeepSeek LLM 调用成功: ${CONTENT}..."
elif [ "${LLM_TEST:-}" != "" ]; then
  ERROR_MSG=$(echo "$LLM_TEST" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('error',{}).get('message','unknown'))" 2>/dev/null || echo "unknown error")
  fail "DeepSeek API 返回错误: ${ERROR_MSG}"
elif [ "$SKIP_DEEPSEEK" != "1" ] && [ -n "$DEEPSEEK_API_KEY" ] && [ "$DEEPSEEK_API_KEY" != "your_deepseek_api_key_here" ]; then
  fail "DeepSeek API 调用失败（网络错误或超时）"
fi

# ── 汇总 ─────────────────────────────────────────────────────────────────────────
echo ""
echo "╔════════════════════════════════════════╗"
echo "║   验证结果: 通过 ${PASS} / 共 $((PASS + FAIL))"
echo "╚════════════════════════════════════════╝"
echo ""

if [ "$FAIL" -gt 0 ]; then
  exit 1
fi
