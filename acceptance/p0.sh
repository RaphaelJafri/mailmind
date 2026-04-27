#!/usr/bin/env bash
# P0 acceptance: builds everything, runs unit tests, smoke-tests both sidecars
# and the webview. Exits 0 only if every step succeeds.
#
# Usage: acceptance/p0.sh
# Env:   MAILMIND_DATA_DIR (override; defaults to a tmp dir for this run)
#        MAILMIND_HEALTH_SKIP_GEMINI=1 (skip the live Vertex round-trip; useful
#                                       in CI without GCP credentials)

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# Use an isolated data dir so the acceptance run never touches real Gmail data.
export MAILMIND_DATA_DIR="${MAILMIND_DATA_DIR:-$(mktemp -d -t mailmind-p0)}"
export MAILMIND_LOGS_DIR="$MAILMIND_DATA_DIR/logs"
mkdir -p "$MAILMIND_LOGS_DIR"

INGESTER_PORT="${INGESTER_PORT:-8866}"
AGENTS_PORT="${AGENTS_PORT:-8865}"
WEBVIEW_PORT="${WEBVIEW_PORT:-5183}"

cleanup() {
  local code=$?
  if [[ -n "${INGESTER_PID:-}" ]]; then kill "$INGESTER_PID" 2>/dev/null || true; fi
  if [[ -n "${AGENTS_PID:-}" ]];   then kill "$AGENTS_PID"   2>/dev/null || true; fi
  if [[ -n "${VITE_PID:-}" ]];     then kill "$VITE_PID"     2>/dev/null || true; fi
  exit "$code"
}
trap cleanup EXIT

step() { printf "\n\033[1;34m== %s ==\033[0m\n" "$*"; }
ok()   { printf "\033[1;32m✓\033[0m %s\n" "$*"; }
fail() { printf "\033[1;31m✗\033[0m %s\n" "$*"; exit 1; }

# ---------------- 1. ingester unit tests ----------------
step "ingester: install + tests"
(cd ingester && npm install --silent --no-fund --no-audit)
(cd ingester && npm test) > "$MAILMIND_LOGS_DIR/ingester-test.log" 2>&1 \
  || { tail -40 "$MAILMIND_LOGS_DIR/ingester-test.log"; fail "ingester tests failed"; }
ok "ingester tests pass"

# ---------------- 2. agents unit tests ----------------
step "agents: install + pytest"
(cd agents && uv venv --quiet --python 3.12 .venv 2>/dev/null || true)
(cd agents && uv pip install --quiet -e ".[dev]" >/dev/null)
(cd agents && source .venv/bin/activate && pytest -q) > "$MAILMIND_LOGS_DIR/agents-test.log" 2>&1 \
  || { tail -40 "$MAILMIND_LOGS_DIR/agents-test.log"; fail "agents tests failed"; }
ok "agents tests pass"

# ---------------- 3. webview install + typecheck + build ----------------
step "webview: install + typecheck + build"
(cd webview && npm install --silent --no-fund --no-audit)
(cd webview && npm run build) > "$MAILMIND_LOGS_DIR/webview-build.log" 2>&1 \
  || { tail -40 "$MAILMIND_LOGS_DIR/webview-build.log"; fail "webview build failed"; }
ok "webview builds"

# ---------------- 4. ingester /health smoke ----------------
step "ingester: /health smoke on :$INGESTER_PORT"
(cd ingester && PORT=$INGESTER_PORT node src/index.mjs) \
  > "$MAILMIND_LOGS_DIR/ingester.log" 2>&1 &
INGESTER_PID=$!
for _ in $(seq 1 20); do
  curl -sf "http://127.0.0.1:$INGESTER_PORT/health" >/dev/null && break
  sleep 0.25
done
RES=$(curl -sf "http://127.0.0.1:$INGESTER_PORT/health") || fail "ingester /health unreachable"
echo "$RES" | grep -q '"sidecar":"mailmind-ingester"' || fail "ingester sidecar identity wrong"
echo "$RES" | grep -q '"status":"ok"' || fail "ingester status not ok"
ok "ingester /health responds: $(echo "$RES" | head -c 80)..."

# ---------------- 5. agents /health smoke ----------------
step "agents: /health smoke on :$AGENTS_PORT"
(cd agents && source .venv/bin/activate && PORT=$AGENTS_PORT python service.py) \
  > "$MAILMIND_LOGS_DIR/agents.log" 2>&1 &
AGENTS_PID=$!
for _ in $(seq 1 40); do
  curl -sf "http://127.0.0.1:$AGENTS_PORT/health" >/dev/null && break
  sleep 0.25
done
RES=$(curl -sf "http://127.0.0.1:$AGENTS_PORT/health") || fail "agents /health unreachable"
echo "$RES" | grep -q '"sidecar":"mailmind-agents"' || fail "agents sidecar identity wrong"
GEMINI_STATUS=$(echo "$RES" | python3 -c "import sys,json; print(json.loads(sys.stdin.read())['gemini']['status'])")
case "$GEMINI_STATUS" in
  ok)         ok "agents /health: gemini ok (real Vertex round-trip)" ;;
  skipped)    ok "agents /health: gemini skipped (MAILMIND_HEALTH_SKIP_GEMINI=1)" ;;
  error)
    if [[ "${ALLOW_GEMINI_ERROR:-0}" == "1" ]]; then
      ok "agents /health: gemini error tolerated (ALLOW_GEMINI_ERROR=1)"
    else
      echo "$RES" | python3 -m json.tool >&2
      fail "agents /health: gemini status=error (set ALLOW_GEMINI_ERROR=1 to bypass during pre-billing)"
    fi
    ;;
  *)          fail "agents /health: unexpected gemini status '$GEMINI_STATUS'" ;;
esac

# ---------------- 6. Tauri shell builds ----------------
step "shell: cargo build --release smoke"
(cd shell && cargo build --release) > "$MAILMIND_LOGS_DIR/shell-build.log" 2>&1 \
  || { tail -40 "$MAILMIND_LOGS_DIR/shell-build.log"; fail "tauri shell build failed"; }
ok "tauri shell builds (release)"

# ---------------- summary ----------------
step "P0 acceptance: ALL GREEN"
echo "Logs: $MAILMIND_LOGS_DIR"
exit 0
