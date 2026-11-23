#!/usr/bin/env bash
# =============================================================================
# weknora-ctl.sh — On-demand controller for Track 2 Multimodal RAG:
#   Local MinerU 4.2+ API (:8000) + Local Qwen3-Embedding (:8001) + WeKnora (:8880)
# Designed for 16GB laptops (start on demand, stop to reclaim RAM cleanly).
# =============================================================================
set -euo pipefail

BASE_DIR="${WEKNORA_RAG_DIR:-$HOME/weknora-rag}"
WEKNORA_DIR="$BASE_DIR/WeKnora"
MINERU_BIN="$BASE_DIR/mineru-venv/bin"
SCRIPTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MINERU_ENV="$BASE_DIR/.mineru.env"
MINERU_PID_FILE="$BASE_DIR/.mineru-api.pid"
MINERU_LOG_FILE="$BASE_DIR/mineru-api.log"
EMBED_ENV="$BASE_DIR/.embed.env"
EMBED_PID_FILE="$BASE_DIR/.local-embed.pid"
EMBED_LOG_FILE="$BASE_DIR/local-embed.log"

mkdir -p "$BASE_DIR"

ensure_mineru_env() {
  if [[ ! -f "$MINERU_ENV" ]]; then
    local api_key
    api_key="$(openssl rand -hex 24)"
    cat > "$MINERU_ENV" <<EOF
MINERU_API_HOST=0.0.0.0
MINERU_API_PORT=8000
MINERU_API_TIER=auto
MINERU_SERVER_API_KEY=${api_key}
MINERU_TELEMETRY_DISABLED=1
EOF
    chmod 600 "$MINERU_ENV"
    echo "[weknora-ctl] Created local MinerU env (chmod 600): $MINERU_ENV"
  fi
  # shellcheck disable=SC1090
  source "$MINERU_ENV"
}

mineru_api_running() {
  if [[ -f "$MINERU_PID_FILE" ]]; then
    local pid
    pid="$(cat "$MINERU_PID_FILE" 2>/dev/null || true)"
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      return 0
    fi
  fi
  return 1
}

cmd_mineru_start() {
  ensure_mineru_env
  if mineru_api_running; then
    echo "[weknora-ctl] mineru-api already running (PID $(cat "$MINERU_PID_FILE"), port ${MINERU_API_PORT:-8000})"
    return 0
  fi
  echo "[weknora-ctl] Starting local MinerU API (port ${MINERU_API_PORT:-8000}, tier=${MINERU_API_TIER:-auto})..."
  MINERU_TELEMETRY_DISABLED=1 nohup "$MINERU_BIN/mineru-api" \
    --host "${MINERU_API_HOST:-0.0.0.0}" \
    --port "${MINERU_API_PORT:-8000}" \
    --tier "${MINERU_API_TIER:-auto}" \
    --api-key "${MINERU_SERVER_API_KEY}" \
    --concurrency 1 \
    >"$MINERU_LOG_FILE" 2>&1 &
  echo $! > "$MINERU_PID_FILE"

  for _ in $(seq 1 40); do
    if curl --noproxy '*' -fsS -H "Authorization: Bearer ${MINERU_SERVER_API_KEY}" "http://127.0.0.1:${MINERU_API_PORT:-8000}/v1/health" >/dev/null 2>&1 \
      || curl --noproxy '*' -fsS "http://127.0.0.1:${MINERU_API_PORT:-8000}/v1/health" >/dev/null 2>&1; then
      echo "[weknora-ctl] mineru-api ready: http://127.0.0.1:${MINERU_API_PORT:-8000}/v1/health"
      return 0
    fi
    sleep 0.5
  done
  echo "[weknora-ctl] Warning: mineru-api not ready yet; check $MINERU_LOG_FILE" >&2
  return 1
}

cmd_mineru_stop() {
  if mineru_api_running; then
    local pid
    pid="$(cat "$MINERU_PID_FILE")"
    echo "[weknora-ctl] Stopping mineru-api (PID $pid)..."
    kill "$pid" 2>/dev/null || true
    sleep 1
    kill -9 "$pid" 2>/dev/null || true
  fi
  rm -f "$MINERU_PID_FILE"
  if [[ -x "$MINERU_BIN/mineru" ]]; then
    "$MINERU_BIN/mineru" server stop >/dev/null 2>&1 || true
  fi
  echo "[weknora-ctl] Local MinerU services stopped."
}

ensure_embed_env() {
  if [[ ! -f "$EMBED_ENV" ]]; then
    local api_key
    api_key="sk-local-embed-$(openssl rand -hex 20)"
    cat > "$EMBED_ENV" <<EOF
LOCAL_EMBED_BACKEND=qwen3
LOCAL_EMBED_HOST=0.0.0.0
LOCAL_EMBED_PORT=8001
LOCAL_EMBED_MODEL_NAME=Qwen/Qwen3-Embedding-0.6B
LOCAL_EMBED_MODEL_DIR=${BASE_DIR}/models/Qwen3-Embedding-0.6B
LOCAL_EMBED_API_KEY=${api_key}
EOF
    chmod 600 "$EMBED_ENV"
    echo "[weknora-ctl] Created local Embedding env (chmod 600): $EMBED_ENV"
  fi
  # shellcheck disable=SC1090
  source "$EMBED_ENV"
}

embed_running() {
  if [[ -f "$EMBED_PID_FILE" ]]; then
    local pid
    pid="$(cat "$EMBED_PID_FILE" 2>/dev/null || true)"
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      return 0
    fi
  fi
  return 1
}

cmd_embed_start() {
  ensure_embed_env
  if embed_running; then
    echo "[weknora-ctl] local-embed already running (PID $(cat "$EMBED_PID_FILE"), port ${LOCAL_EMBED_PORT:-8001})"
    return 0
  fi
  echo "[weknora-ctl] Starting local Embedding API (${LOCAL_EMBED_MODEL_NAME:-Qwen/Qwen3-Embedding-0.6B}, port ${LOCAL_EMBED_PORT:-8001})..."
  nohup "$MINERU_BIN/python" "$SCRIPTS_DIR/local_embed_server.py" \
    --backend "${LOCAL_EMBED_BACKEND:-qwen3}" \
    --host "${LOCAL_EMBED_HOST:-0.0.0.0}" \
    --port "${LOCAL_EMBED_PORT:-8001}" \
    --model-name "${LOCAL_EMBED_MODEL_NAME:-Qwen/Qwen3-Embedding-0.6B}" \
    --model-dir "${LOCAL_EMBED_MODEL_DIR:-$BASE_DIR/models/Qwen3-Embedding-0.6B}" \
    --api-key "${LOCAL_EMBED_API_KEY}" \
    >"$EMBED_LOG_FILE" 2>&1 &
  echo $! > "$EMBED_PID_FILE"

  for _ in $(seq 1 90); do
    if curl --noproxy '*' -fsS "http://127.0.0.1:${LOCAL_EMBED_PORT:-8001}/health" >/dev/null 2>&1; then
      echo "[weknora-ctl] local-embed ready: http://127.0.0.1:${LOCAL_EMBED_PORT:-8001}/health"
      return 0
    fi
    sleep 0.5
  done
  echo "[weknora-ctl] Warning: local-embed not ready yet; check $EMBED_LOG_FILE" >&2
  return 1
}

cmd_embed_stop() {
  if embed_running; then
    local pid
    pid="$(cat "$EMBED_PID_FILE")"
    echo "[weknora-ctl] Stopping local-embed (PID $pid)..."
    kill "$pid" 2>/dev/null || true
    sleep 1
    kill -9 "$pid" 2>/dev/null || true
  fi
  rm -f "$EMBED_PID_FILE"
  echo "[weknora-ctl] Local Embedding API stopped."
}

cmd_start() {
  cmd_mineru_start
  cmd_embed_start
  if ! docker info >/dev/null 2>&1; then
    echo "[weknora-ctl] Docker is not running. Please start Docker Desktop first." >&2
    exit 1
  fi
  echo "[weknora-ctl] Starting WeKnora containers (frontend + app + docreader + postgres + redis)..."
  (cd "$WEKNORA_DIR" && docker compose up -d)
  echo "[weknora-ctl] WeKnora Web UI: http://localhost:8880"
  echo "[weknora-ctl] WeKnora Backend API: http://localhost:8080/health"
}

cmd_stop() {
  if docker info >/dev/null 2>&1 && [[ -d "$WEKNORA_DIR" ]]; then
    echo "[weknora-ctl] Stopping WeKnora containers..."
    (cd "$WEKNORA_DIR" && docker compose stop)
  else
    echo "[weknora-ctl] Docker not running; skipping container stop."
  fi
  cmd_embed_stop
  cmd_mineru_stop
}

cmd_status() {
  echo "=== 1. Local MinerU API (:8000) ==="
  if mineru_api_running; then
    local port="8000"
    [[ -f "$MINERU_ENV" ]] && source "$MINERU_ENV" && port="${MINERU_API_PORT:-8000}"
    echo "PID: $(cat "$MINERU_PID_FILE")"
    curl --noproxy '*' -fsS -H "Authorization: Bearer ${MINERU_SERVER_API_KEY:-}" "http://127.0.0.1:${port}/v1/health" && echo "" || echo "health check failed"
  else
    echo "stopped"
  fi

  echo ""
  echo "=== 2. Local Embedding API (:8001) ==="
  if embed_running; then
    local eport="8001"
    [[ -f "$EMBED_ENV" ]] && source "$EMBED_ENV" && eport="${LOCAL_EMBED_PORT:-8001}"
    echo "PID: $(cat "$EMBED_PID_FILE")"
    curl --noproxy '*' -fsS "http://127.0.0.1:${eport}/health" && echo "" || echo "health check failed"
  else
    echo "stopped"
  fi

  echo ""
  echo "=== 3. WeKnora Docker Stack ==="
  if docker info >/dev/null 2>&1 && [[ -d "$WEKNORA_DIR" ]]; then
    (cd "$WEKNORA_DIR" && docker compose ps)
    echo -n "WeKnora /health: "
    curl --noproxy '*' -fsS http://127.0.0.1:8080/health && echo "" || echo "not ready"
  else
    echo "Docker not running"
  fi
}

cmd_help() {
  cat <<'EOF'
Usage: weknora-ctl.sh <command> [args...]

Commands:
  start           Start MinerU API (:8000) + Local Embedding (:8001) + WeKnora (:8880)
  stop            Stop WeKnora containers, local Embedding & MinerU to free RAM
  status          Health check for MinerU, local Embedding, and WeKnora
  mineru-start    Start local MinerU API only
  mineru-stop     Stop local MinerU API only
  embed-start     Start local Qwen3-Embedding API only
  embed-stop      Stop local Qwen3-Embedding API only
  parse <path...> Parse PDF/Office/Image into <kb_root>/资料/ & refresh local index
  sync [args...]  Incrementally sync DLP-redacted notes & parsed docs to WeKnora
EOF
}

case "${1:-help}" in
  start)        cmd_start ;;
  stop)         cmd_stop ;;
  status)       cmd_status ;;
  mineru-start) cmd_mineru_start ;;
  mineru-stop)  cmd_mineru_stop ;;
  embed-start)  cmd_embed_start ;;
  embed-stop)   cmd_embed_stop ;;
  parse)        shift; exec "$MINERU_BIN/python" "$SCRIPTS_DIR/kb_parse_mineru.py" "$@" ;;
  sync)         shift; exec python3 "$SCRIPTS_DIR/kb_sync_weknora.py" "$@" ;;
  -h|--help|help) cmd_help ;;
  *)
    echo "Unknown command: $1" >&2
    cmd_help
    exit 1
    ;;
esac
