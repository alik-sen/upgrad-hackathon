#!/usr/bin/env bash
# Start/stop the full stack: state-store MCP (streamable-http :8081) + FastAPI (:8000, WORKERS uvicorn workers, each spawning its own rag/graphify MCP workers).
# usage: scripts/run_stack.sh start|stop|restart|status   (env: WORKERS=1 PORT=8000 MODEL_PROFILE=dev ORT_THREADS=2)
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"; cd "$ROOT"
PY="$ROOT/.venv/bin/python"; RUN="$ROOT/.cache/run"; mkdir -p "$RUN"
WORKERS="${WORKERS:-1}"; PORT="${PORT:-8000}"
export PYTHONPATH="$ROOT/src"
stop() {
  for f in api state; do [ -f "$RUN/$f.pid" ] && kill "$(cat "$RUN/$f.pid")" 2>/dev/null; rm -f "$RUN/$f.pid"; done
  sleep 1; pkill -f "mcp_servers/[r]ag_server.py" 2>/dev/null; pkill -f "mcp_servers/[g]raphify_server.py" 2>/dev/null; pkill -f "mcp_servers/[s]tate_server.py" 2>/dev/null; pkill -f "[u]vicorn unreliable.api" 2>/dev/null; true
}
start() {
  stop
  [ -n "${FRESH_DB:-}" ] && rm -f "$ROOT/.cache/state.db"*
  nohup "$PY" mcp_servers/state_server.py 8081 > "$RUN/state.log" 2>&1 & echo $! > "$RUN/state.pid"
  for i in $(seq 1 50); do (echo > /dev/tcp/127.0.0.1/8081) 2>/dev/null && break; sleep 0.2; done
  SPAWN_STATE=0 nohup "$ROOT/.venv/bin/uvicorn" unreliable.api:app --host 127.0.0.1 --port "$PORT" --workers "$WORKERS" --log-level warning > "$RUN/api.log" 2>&1 & echo $! > "$RUN/api.pid"
  for i in $(seq 1 180); do
    h=$(curl -s "localhost:$PORT/health" 2>/dev/null || true)
    echo "$h" | grep -q '"status":"ok"' && ! echo "$h" | grep -q '"up":false' && { echo "stack up: $h"; return 0; }
    sleep 1
  done
  echo "stack failed to come up; see $RUN/api.log"; tail -20 "$RUN/api.log"; return 1
}
case "${1:-start}" in
  start) start ;; stop) stop ;; restart) start ;;
  status) curl -s "localhost:$PORT/health" || echo down ;;
esac
