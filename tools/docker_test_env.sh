#!/usr/bin/env bash
# docker_test_env.sh — reproducible AstrBot + tmemory Docker test environment
#
# Host port 6186 -> container port 6185 (avoids collision with local AstrBot on 6185)
# Usage:
#   ./tools/docker_test_env.sh start   # start container
#   ./tools/docker_test_env.sh stop    # stop and remove container
#   ./tools/docker_test_env.sh logs    # tail container logs
#   ./tools/docker_test_env.sh status  # show container status

set -euo pipefail

CONTAINER_NAME="astrbot_tmemory_test"
IMAGE="soulter/astrbot:nightly-latest"
HOST_PORT=6186          # intentionally NOT 6185 to avoid collision with local AstrBot
CONTAINER_PORT=6185
DATA_DIR="/tmp/astrbot_test_data"
PLUGIN_DIR="${DATA_DIR}/plugins/astrbot_plugin_tmemory"
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"

cmd="${1:-status}"

case "$cmd" in
  start)
    echo "[tmemory-docker] Preparing data directory: $DATA_DIR"
    mkdir -p "$DATA_DIR/plugins"

    # Copy current workspace into plugin slot (fresh copy, not symlink for Docker compat)
    if [ -d "$PLUGIN_DIR" ]; then
      rm -rf "$PLUGIN_DIR"
    fi
    cp -R "$REPO_DIR" "$PLUGIN_DIR"
    echo "[tmemory-docker] Plugin copied: $PLUGIN_DIR"

    # Remove stale container if exists
    if docker ps -a --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
      echo "[tmemory-docker] Removing stale container: $CONTAINER_NAME"
      docker rm -f "$CONTAINER_NAME"
    fi

    echo "[tmemory-docker] Starting container: $CONTAINER_NAME"
    echo "[tmemory-docker] Port mapping: host $HOST_PORT->container $CONTAINER_PORT, host 9966->container 9966"
    docker run -d \
      --name "$CONTAINER_NAME" \
      -p "${HOST_PORT}:${CONTAINER_PORT}" \
      -p "9966:9966" \
      -v "${DATA_DIR}:/AstrBot/data" \
      "$IMAGE"

    echo "[tmemory-docker] Waiting for WebUI to become ready..."
    for i in $(seq 1 30); do
      if curl -sf -o /dev/null "http://localhost:${HOST_PORT}/"; then
        echo "[tmemory-docker] ✅ AstrBot WebUI ready at http://localhost:${HOST_PORT}"
        break
      fi
      sleep 2
    done

    echo "[tmemory-docker] Container status:"
    docker ps --filter "name=${CONTAINER_NAME}" --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
    ;;

  stop)
    echo "[tmemory-docker] Stopping $CONTAINER_NAME"
    docker rm -f "$CONTAINER_NAME" 2>/dev/null || true
    echo "[tmemory-docker] Done."
    ;;

  logs)
    docker logs -f "$CONTAINER_NAME"
    ;;

  status)
    docker ps --filter "name=${CONTAINER_NAME}" --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
    ;;

  *)
    echo "Usage: $0 {start|stop|logs|status}"
    exit 1
    ;;
esac
