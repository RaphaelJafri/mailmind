#!/usr/bin/env bash
# mailmind dev launcher — one command to bring up the whole stack.
#
# Spawns three background processes:
#   - ingester sidecar (Node, :8766) — Gmail ingestion + read views
#   - agents sidecar (Python/FastAPI, :8765) — agent service + eval + drafts
#   - webview (Vite, :5173) — the React UI
#
# Opens http://127.0.0.1:5173 in the default browser. Press Ctrl-C to stop
# everything (the trap kills child processes + frees ports cleanly).
#
# Use the desktop Tauri window? Run `cd shell && cargo tauri dev` instead —
# it points at the same Vite server.
#
# Env:
#   MAILMIND_DATA_DIR   — where SQLite/labels/logs live (default: real Application
#                          Support dir, so labels survive across restarts).
#   GOOGLE_GENAI_API_KEY — set if you want real Gemini calls; otherwise the
#                          stub mode kicks in and the agents return canned
#                          responses for the fixture threads.
#   MAILMIND_STUB_RESPONSES — point at a stub.json to bypass Gemini entirely.
#   MAILMIND_HEALTH_SKIP_GEMINI=1 — skip the /health Gemini round-trip.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# Real persistent data dir — labels, eval results, drafts all live here.
# Override with MAILMIND_DATA_DIR=... if you want an isolated session.
if [[ -z "${MAILMIND_DATA_DIR:-}" ]]; then
  export MAILMIND_DATA_DIR="$HOME/Library/Application Support/mailmind"
fi
mkdir -p "$MAILMIND_DATA_DIR"
LOGS_DIR="${MAILMIND_LOGS_DIR:-$MAILMIND_DATA_DIR/logs}"
mkdir -p "$LOGS_DIR"

INGESTER_PORT="${INGESTER_PORT:-8766}"
AGENTS_PORT="${AGENTS_PORT:-8765}"
WEBVIEW_PORT="${WEBVIEW_PORT:-5173}"

step() { printf "\n\033[1;34m== %s ==\033[0m\n" "$*"; }
ok()   { printf "\033[1;32m✓\033[0m %s\n" "$*"; }
fail() { printf "\033[1;31m✗\033[0m %s\n" "$*"; exit 1; }

cleanup() {
  local code=$?
  printf "\n\033[1;33m→\033[0m shutting down…\n"
  for pid in "${INGESTER_PID:-}" "${AGENTS_PID:-}" "${VITE_PID:-}"; do
    [[ -n "$pid" ]] || continue
    kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
  done
  sleep 0.4
  for pid in "${INGESTER_PID:-}" "${AGENTS_PID:-}" "${VITE_PID:-}"; do
    [[ -n "$pid" ]] || continue
    kill -KILL -- "-$pid" 2>/dev/null || kill -KILL "$pid" 2>/dev/null || true
  done
  for port in "$INGESTER_PORT" "$AGENTS_PORT" "$WEBVIEW_PORT"; do
    local pids
    pids=$(lsof -ti tcp:"$port" 2>/dev/null || true)
    [[ -n "$pids" ]] && kill -KILL $pids 2>/dev/null || true
  done
  exit "$code"
}
trap cleanup EXIT INT TERM

# ---------------- ingester ----------------
step "ingester: install (if needed) + start on :$INGESTER_PORT"
if [[ ! -d "ingester/node_modules" ]]; then
  (cd ingester && npm install --silent --no-fund --no-audit)
fi
(cd ingester && PORT=$INGESTER_PORT node src/index.mjs) \
  > "$LOGS_DIR/ingester.log" 2>&1 &
INGESTER_PID=$!
for _ in $(seq 1 40); do
  curl -sf "http://127.0.0.1:$INGESTER_PORT/health" >/dev/null && break
  sleep 0.25
done
curl -sf "http://127.0.0.1:$INGESTER_PORT/health" >/dev/null \
  || { tail -30 "$LOGS_DIR/ingester.log"; fail "ingester didn't come up — see $LOGS_DIR/ingester.log"; }
ok "ingester ready  → http://127.0.0.1:$INGESTER_PORT  (logs: $LOGS_DIR/ingester.log)"

# ---------------- agents ----------------
step "agents: install (if needed) + start on :$AGENTS_PORT"
if [[ ! -d "agents/.venv" ]]; then
  (cd agents && uv venv --quiet --python 3.12 .venv)
fi
(cd agents && uv pip install --quiet -e ".[dev,agents]" >/dev/null)

(cd agents && source .venv/bin/activate && \
   PORT=$AGENTS_PORT python service.py) \
  > "$LOGS_DIR/agents.log" 2>&1 &
AGENTS_PID=$!
for _ in $(seq 1 80); do
  curl -sf "http://127.0.0.1:$AGENTS_PORT/health" >/dev/null && break
  sleep 0.25
done
curl -sf "http://127.0.0.1:$AGENTS_PORT/health" >/dev/null \
  || { tail -40 "$LOGS_DIR/agents.log"; fail "agents didn't come up — see $LOGS_DIR/agents.log"; }
ok "agents ready    → http://127.0.0.1:$AGENTS_PORT    (logs: $LOGS_DIR/agents.log)"

# ---------------- webview ----------------
step "webview: install (if needed) + start on :$WEBVIEW_PORT"
if [[ ! -d "webview/node_modules" ]]; then
  (cd webview && npm install --silent --no-fund --no-audit)
fi
(cd webview && PORT=$WEBVIEW_PORT npm run dev --silent) \
  > "$LOGS_DIR/webview.log" 2>&1 &
VITE_PID=$!
for _ in $(seq 1 40); do
  curl -sf "http://127.0.0.1:$WEBVIEW_PORT/" >/dev/null && break
  sleep 0.25
done
curl -sf "http://127.0.0.1:$WEBVIEW_PORT/" >/dev/null \
  || { tail -30 "$LOGS_DIR/webview.log"; fail "vite didn't come up — see $LOGS_DIR/webview.log"; }
ok "webview ready   → http://127.0.0.1:$WEBVIEW_PORT   (logs: $LOGS_DIR/webview.log)"

# ---------------- summary ----------------
printf "\n\033[1;32mmailmind is up.\033[0m\n"
printf "  open: \033[1mhttp://127.0.0.1:%s\033[0m\n" "$WEBVIEW_PORT"
printf "  data: %s\n" "$MAILMIND_DATA_DIR"
printf "  logs: %s\n" "$LOGS_DIR"
printf "\n  Ctrl-C to stop everything.\n\n"

# Try to open the browser. Fail silently if `open` isn't available.
sleep 0.5
open "http://127.0.0.1:$WEBVIEW_PORT" 2>/dev/null || true

# Block on the agent service so this script stays in the foreground until
# the user hits Ctrl-C. The trap above does the cleanup.
wait "$AGENTS_PID"
