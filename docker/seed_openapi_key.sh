#!/usr/bin/env bash
# =============================================================================
# seed_openapi_key.sh — 为已运行的测试容器预置 / 修复 OpenAPI key
#
# 用途：不重建容器时手动执行（等价于 docker/astrbot_init.sh 的自动预置步骤）。
#
# 使用：
#   ASTRBOT_API_KEY=admin bash docker/seed_openapi_key.sh
#   ASTRBOT_CONTAINER=astrbot_tmemory_test ASTRBOT_API_KEY=admin bash docker/seed_openapi_key.sh
# =============================================================================
set -euo pipefail

CONTAINER="${ASTRBOT_CONTAINER:-astrbot_tmemory_test}"
KEY="${ASTRBOT_API_KEY:-admin}"

if ! docker ps --format '{{.Names}}' | grep -qx "$CONTAINER"; then
  echo "❌ 容器未运行: $CONTAINER" >&2
  exit 1
fi

docker exec -e "ASTRBOT_API_KEY=${KEY}" "$CONTAINER" python3 /docker/seed_openapi_key.py
echo "[seed] AstrBot OpenAPI key 就绪：X-API-Key: ${KEY}"
