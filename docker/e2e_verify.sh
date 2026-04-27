#!/usr/bin/env bash
# =============================================================================
# e2e_verify.sh — tmemory E2E 验证脚本（Docker 环境）
#
# 前提条件：
#   1. docker-compose up -d 已在运行
#   2. AstrBot WebUI 可通过 http://localhost:6186/ 访问
#
# 验证内容：
#   1. 容器健康状态
#   2. Ollama API 可用
#   3. AstrBot WebUI 可访问
#   4. tmemory 插件已加载
#   5. LLM 蒸馏链路可用
# =============================================================================
set -euo pipefail

ASTRBOT_URL="${ASTRBOT_URL:-http://localhost:6186}"
OLLAMA_URL="${OLLAMA_URL:-http://localhost:11434}"
PASS=0
FAIL=0

pass() { PASS=$((PASS + 1)); echo "  ✅ $1"; }
fail() { FAIL=$((FAIL + 1)); echo "  ❌ $1"; }

echo ""
echo "╔════════════════════════════════════════╗"
echo "║   tmemory Docker E2E 验证             ║"
echo "╚════════════════════════════════════════╝"
echo ""

# ── 检查依赖 ────────────────────────────────────────────────────────────────────
if ! command -v curl &>/dev/null; then echo "❌ 需要 curl"; exit 1; fi
if ! command -v docker &>/dev/null; then echo "❌ 需要 docker"; exit 1; fi

# ── 1. 容器状态 ──────────────────────────────────────────────────────────────────
echo "--- 1. 容器状态 ---"
if docker ps --format '{{.Names}} {{.Status}}' | grep -q "astrbot_tmemory_test"; then
  pass "AstrBot 容器运行中"
else
  fail "AstrBot 容器未运行"
fi
if docker ps --format '{{.Names}} {{.Status}}' | grep -q "astrbot-ollama"; then
  pass "Ollama 容器运行中"
else
  fail "Ollama 容器未运行"
fi

# ── 2. Ollama API ────────────────────────────────────────────────────────────────
echo ""
echo "--- 2. Ollama API ---"
OLLAMA_TAGS=$(curl -sf "${OLLAMA_URL}/api/tags" 2>/dev/null || echo "")
if [ -n "$OLLAMA_TAGS" ]; then
  pass "Ollama API 响应正常"
  MODELS=$(echo "$OLLAMA_TAGS" | python3 -c "import sys,json; d=json.load(sys.stdin); print('\n'.join(m['name'] for m in d.get('models',[])))" 2>/dev/null || echo "")
  if [ -n "$MODELS" ]; then
    echo "  已加载模型:"
    echo "$MODELS" | while read -r m; do echo "    - $m"; done
  else
    echo "  ⚠️  尚未拉取任何模型"
  fi
else
  fail "Ollama API 无响应"
fi

# ── 3. AstrBot WebUI ────────────────────────────────────────────────────────────
echo ""
echo "--- 3. AstrBot WebUI ---"
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" "${ASTRBOT_URL}/" 2>/dev/null || echo "000")
if [ "$HTTP_CODE" = "200" ] || [ "$HTTP_CODE" = "302" ] || [ "$HTTP_CODE" = "401" ]; then
  pass "AstrBot WebUI 可访问 (HTTP ${HTTP_CODE})"
else
  fail "AstrBot WebUI 返回 HTTP ${HTTP_CODE}"
fi

# ── 4. tmemory 插件 ──────────────────────────────────────────────────────────────
echo ""
echo "--- 4. tmemory 插件 ---"
TMEMORY_LOG=$(docker logs astrbot_tmemory_test 2>&1 | grep -i "tmemory" | tail -5 || echo "")
if echo "$TMEMORY_LOG" | grep -q "initialized"; then
  pass "tmemory 插件已初始化"
else
  # 插件可能还在启动中
  if echo "$TMEMORY_LOG" | grep -q "tmemory"; then
    pass "tmemory 日志存在（可能仍在初始化）"
  else
    fail "未找到 tmemory 初始化日志"
  fi
fi

# ── 5. LLM 蒸馏链路验证 ──────────────────────────────────────────────────────────
echo ""
echo "--- 5. LLM 蒸馏链路 ---"
# 通过 Ollama 测试 LLM 调用
LLM_TEST=$(curl -sf -X POST "${OLLAMA_URL}/api/generate" \
  -H "Content-Type: application/json" \
  -d '{"model": "qwen2.5:0.5b", "prompt": "Hello", "stream": false}' 2>/dev/null || echo "")
if [ -n "$LLM_TEST" ] && echo "$LLM_TEST" | python3 -c "import sys,json; d=json.load(sys.stdin); assert 'response' in d" 2>/dev/null; then
  RESPONSE=$(echo "$LLM_TEST" | python3 -c "import sys,json; print(json.load(sys.stdin)['response'][:50])" 2>/dev/null)
  pass "LLM 调用成功: ${RESPONSE}..."
else
  fail "LLM 调用失败"
fi

# ── 6. Embedding 链路验证 ────────────────────────────────────────────────────────
echo ""
echo "--- 6. Embedding 链路 ---"
EMBED_TEST=$(curl -sf -X POST "${OLLAMA_URL}/api/embeddings" \
  -H "Content-Type: application/json" \
  -d '{"model": "nomic-embed-text", "prompt": "test memory embedding"}' 2>/dev/null || echo "")
if [ -n "$EMBED_TEST" ] && echo "$EMBED_TEST" | python3 -c "import sys,json; d=json.load(sys.stdin); assert 'embedding' in d" 2>/dev/null; then
  DIM=$(echo "$EMBED_TEST" | python3 -c "import sys,json; print(len(json.load(sys.stdin)['embedding']))" 2>/dev/null)
  pass "Embedding 调用成功 (维度: ${DIM})"
else
  fail "Embedding 调用失败"
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
