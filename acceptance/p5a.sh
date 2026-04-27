#!/usr/bin/env bash
# P5a acceptance: observability + cost guardrails (the autonomous half of P5).
#
# What this exercises (BUILD §20, §22, EXECUTION P5 items 4 + 5):
#   1. agents pytest (P0+P1+P2+P3+P4a+P4b+P5a suites green).
#   2. webview build + ingester tests.
#   3. fixture pipeline → derived.sqlite (so agents have something to do).
#   4. cost_usd lands on every agent_runs row that opted into the guard.
#   5. /observability/summary returns today's spend, latency P50/P95,
#      schema-fail/retry rate.
#   6. /observability/cost_trajectory returns 7 days, agent series populated.
#   7. /observability/budget echoes the config file (so the UI can render it).
#   8. /observability/log_tail returns the rotating-file events for the
#      runs we just made.
#   9. Per-task cap refusal: lower the extract cap to ~0 via a temp
#      pipeline.yml and verify /extract/run surfaces 500 with the
#      `per_task_cap_exceeded` error code in the body.
#  10. /observability/anomalies returns shape (empty list when nothing
#      spikes — that's the happy path).
#  11. Append-only audit chain still verifies (P4 invariant must hold
#      after the cost-guard wiring).
#
# All Gemini calls remain mocked under MAILMIND_STUB_RESPONSES — no real
# network. The point of P5a is to prove cost-tracking + guards + the
# Observability surface, without flipping anything user-facing.
#
# Usage: acceptance/p5a.sh

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

export MAILMIND_DATA_DIR="${MAILMIND_DATA_DIR:-$(mktemp -d -t mailmind-p5a)}"
export MAILMIND_LOGS_DIR="$MAILMIND_DATA_DIR/logs"
mkdir -p "$MAILMIND_LOGS_DIR"

INGESTER_PORT="${INGESTER_PORT:-8889}"
AGENTS_PORT="${AGENTS_PORT:-8890}"

cleanup() {
  local code=$?
  for pid in "${INGESTER_PID:-}" "${AGENTS_PID:-}"; do
    [[ -n "$pid" ]] || continue
    kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
  done
  sleep 0.5
  for pid in "${INGESTER_PID:-}" "${AGENTS_PID:-}"; do
    [[ -n "$pid" ]] || continue
    kill -KILL -- "-$pid" 2>/dev/null || kill -KILL "$pid" 2>/dev/null || true
  done
  for port in "$INGESTER_PORT" "$AGENTS_PORT"; do
    local pids
    pids=$(lsof -ti tcp:"$port" 2>/dev/null || true)
    [[ -n "$pids" ]] && kill -KILL $pids 2>/dev/null || true
  done
  exit "$code"
}
trap cleanup EXIT INT TERM

step() { printf "\n\033[1;34m== %s ==\033[0m\n" "$*"; }
ok()   { printf "\033[1;32m✓\033[0m %s\n" "$*"; }
fail() { printf "\033[1;31m✗\033[0m %s\n" "$*"; exit 1; }

# ---------------- 1. install + tests ----------------
step "ingester: install + tests"
(cd ingester && npm install --silent --no-fund --no-audit)
(cd ingester && npm test) > "$MAILMIND_LOGS_DIR/ingester-test.log" 2>&1 \
  || { tail -40 "$MAILMIND_LOGS_DIR/ingester-test.log"; fail "ingester tests failed"; }
ok "ingester tests pass"

step "agents: install + pytest (all suites incl. P5a)"
(cd agents && uv venv --quiet --python 3.12 .venv 2>/dev/null || true)
(cd agents && uv pip install --quiet -e ".[dev,agents]" >/dev/null)
(cd agents && source .venv/bin/activate && pytest -q) > "$MAILMIND_LOGS_DIR/agents-test.log" 2>&1 \
  || { tail -120 "$MAILMIND_LOGS_DIR/agents-test.log"; fail "agents tests failed"; }
ok "agents tests pass (P0+P1+P2+P3+P4a+P4b+P5a suites)"

step "webview: install + typecheck + build"
(cd webview && npm install --silent --no-fund --no-audit)
(cd webview && npm run build) > "$MAILMIND_LOGS_DIR/webview-build.log" 2>&1 \
  || { tail -40 "$MAILMIND_LOGS_DIR/webview-build.log"; fail "webview build failed"; }
ok "webview builds (Observability tab compiled)"

# ---------------- 2. seed fixtures ----------------
step "fixtures: load seed.json into raw.sqlite"
node fixtures/load_fixtures.mjs > "$MAILMIND_LOGS_DIR/fixtures.log" 2>&1 \
  || { tail -20 "$MAILMIND_LOGS_DIR/fixtures.log"; fail "fixture load failed"; }
ok "fixtures loaded"

step "fixtures: build stub responses"
(cd agents && source .venv/bin/activate && python ../fixtures/build_stub_responses.py "$MAILMIND_DATA_DIR/stub.json") \
  > "$MAILMIND_LOGS_DIR/stub-build.log" 2>&1 \
  || { tail -20 "$MAILMIND_LOGS_DIR/stub-build.log"; fail "stub build failed"; }
export MAILMIND_STUB_RESPONSES="$MAILMIND_DATA_DIR/stub.json"
ok "stubs built"

# ---------------- 3. spawn sidecars ----------------
step "ingester: start on :$INGESTER_PORT"
(cd ingester && PORT=$INGESTER_PORT node src/index.mjs) \
  > "$MAILMIND_LOGS_DIR/ingester.log" 2>&1 &
INGESTER_PID=$!
for _ in $(seq 1 20); do
  curl -sf "http://127.0.0.1:$INGESTER_PORT/health" >/dev/null && break
  sleep 0.25
done

step "agents: start on :$AGENTS_PORT"
(cd agents && source .venv/bin/activate && \
   MAILMIND_HEALTH_SKIP_GEMINI=1 PORT=$AGENTS_PORT python service.py) \
  > "$MAILMIND_LOGS_DIR/agents.log" 2>&1 &
AGENTS_PID=$!
for _ in $(seq 1 40); do
  curl -sf "http://127.0.0.1:$AGENTS_PORT/health" >/dev/null && break
  sleep 0.25
done

# ---------------- 4. seed pipeline (extract → relationship → reconcile) -----
step "agents: seed pipeline so cost rows land in agent_runs"
curl -sf -X POST -H 'Content-Type: application/json' \
  -d '{"thread_ids":["fix-thread-101","fix-thread-102","fix-thread-103"]}' \
  "http://127.0.0.1:$AGENTS_PORT/extract/run" > "$MAILMIND_LOGS_DIR/extract.json" \
  || fail "/extract/run failed"
curl -sf -X POST -H 'Content-Type: application/json' -d '{}' \
  "http://127.0.0.1:$AGENTS_PORT/relationship/run" > "$MAILMIND_LOGS_DIR/relationship.json" \
  || fail "/relationship/run failed"
curl -sf -X POST -H 'Content-Type: application/json' -d '{}' \
  "http://127.0.0.1:$AGENTS_PORT/reconcile/run" > "$MAILMIND_LOGS_DIR/reconcile.json" \
  || fail "/reconcile/run failed"
ok "pipeline seeded"

# ---------------- 5. cost_usd column populated --------------------------
step "agents: cost_usd lands on agent_runs (extract_agent has at least one row)"
RUNS=$(curl -sf "http://127.0.0.1:$AGENTS_PORT/agent_runs?agent_name=extract_agent")
echo "$RUNS" | python3 -c "
import sys, json
runs = json.loads(sys.stdin.read())['runs']
assert runs, 'no extract_agent runs recorded'
# cost_usd should be present (and not None) on every row
for r in runs:
    assert r.get('cost_usd') is not None, f'NULL cost_usd on run {r[\"id\"]}'
print(f\"cost_usd recorded on {len(runs)} extract_agent run(s)\")
" || fail "cost_usd missing from agent_runs"
ok "cost_usd recorded on extract_agent runs"

# ---------------- 6. /observability/summary -----------------------------
step "agents: GET /observability/summary"
SUM=$(curl -sf "http://127.0.0.1:$AGENTS_PORT/observability/summary")
echo "$SUM" > "$MAILMIND_LOGS_DIR/obs-summary.json"
echo "$SUM" | python3 -c "
import sys, json
s = json.loads(sys.stdin.read())
assert 'today' in s and 'errors' in s and 'latency_by_agent' in s, s
assert s['today']['cap_usd'] >= 1.0, s['today']
assert s['today']['bucket'] in ('ok', 'warn', 'stop'), s['today']
agents = {r['agent_name'] for r in s['latency_by_agent']}
# We just ran extract + relationship + reconcile; at least extract must have a row.
assert 'extract_agent' in agents, agents
print(f\"summary ok — today=\${s['today']['total_usd']:.4f} bucket={s['today']['bucket']} agents={sorted(agents)}\")
" || fail "summary shape wrong"
ok "/observability/summary returns headline numbers"

# ---------------- 7. /observability/cost_trajectory ---------------------
step "agents: GET /observability/cost_trajectory?days=7"
TRAJ=$(curl -sf "http://127.0.0.1:$AGENTS_PORT/observability/cost_trajectory?days=7")
echo "$TRAJ" | python3 -c "
import sys, json
t = json.loads(sys.stdin.read())
assert len(t['days']) == 7, t
# Expect at least one agent today.
assert 'extract_agent' in t['agents'], t
" || fail "cost_trajectory shape wrong"
ok "/observability/cost_trajectory returns 7-day series"

# ---------------- 8. /observability/budget -------------------------------
step "agents: GET /observability/budget echoes config"
BUD=$(curl -sf "http://127.0.0.1:$AGENTS_PORT/observability/budget")
echo "$BUD" | python3 -c "
import sys, json
b = json.loads(sys.stdin.read())
assert 'gemini-2.5-flash' in b['pricing_usd_per_million']
assert b['per_day_usd']['total'] >= 1.0, b['per_day_usd']
assert 'extract_agent' in b['per_task_usd'] or 'extract' in b['per_task_usd']
" || fail "budget shape wrong"
ok "/observability/budget returns pricing + caps"

# ---------------- 9. /observability/anomalies (shape) -------------------
step "agents: GET /observability/anomalies"
ANO=$(curl -sf "http://127.0.0.1:$AGENTS_PORT/observability/anomalies?limit=20")
echo "$ANO" | python3 -c "
import sys, json
a = json.loads(sys.stdin.read())
assert 'anomalies' in a and isinstance(a['anomalies'], list), a
" || fail "anomalies shape wrong"
ok "/observability/anomalies responds (list — happy path is empty)"

# ---------------- 10. /observability/log_tail ---------------------------
step "agents: GET /observability/log_tail returns finish events"
LOG=$(curl -sf "http://127.0.0.1:$AGENTS_PORT/observability/log_tail?limit=200")
echo "$LOG" > "$MAILMIND_LOGS_DIR/obs-log.json"
echo "$LOG" | python3 -c "
import sys, json
events = json.loads(sys.stdin.read())['events']
finishes = [e for e in events if e.get('event') == 'agent_run_finish']
assert finishes, f'no agent_run_finish events found: {events[:3]}'
# At least one of them is for extract_agent.
assert any(e.get('agent_name') == 'extract_agent' for e in finishes), finishes[:5]
" || fail "log_tail missing finish events"
ok "/observability/log_tail surfaces structured events"

# ---------------- 11. per-task cap refusal -----------------------------
step "agents: lower extract cap to ~0 → /extract/run refuses"
CAPCFG="$MAILMIND_DATA_DIR/cap-test.yml"
cat > "$CAPCFG" <<'YAML'
cost_caps:
  per_task_usd:
    extract: 0.0000001
YAML

# Restart agents with the cap config in env. Group-kill so uvicorn's
# child processes go down too; otherwise the new service can't bind the
# port.
kill -TERM -- "-$AGENTS_PID" 2>/dev/null || kill -TERM "$AGENTS_PID" 2>/dev/null || true
for _ in $(seq 1 20); do
  lsof -ti tcp:"$AGENTS_PORT" >/dev/null 2>&1 || break
  sleep 0.25
done
# Belt + braces — if anything's still on the port, force-close.
PORTPIDS=$(lsof -ti tcp:"$AGENTS_PORT" 2>/dev/null || true)
[[ -n "$PORTPIDS" ]] && kill -KILL $PORTPIDS 2>/dev/null || true

(cd agents && source .venv/bin/activate && \
   MAILMIND_HEALTH_SKIP_GEMINI=1 PORT=$AGENTS_PORT \
   MAILMIND_PIPELINE_CONFIG="$CAPCFG" python service.py) \
   > "$MAILMIND_LOGS_DIR/agents-cap.log" 2>&1 &
AGENTS_PID=$!
for _ in $(seq 1 40); do
  curl -sf "http://127.0.0.1:$AGENTS_PORT/health" >/dev/null && break
  sleep 0.25
done
# Confirm the new service actually answered (else our cap test is bogus).
curl -sf "http://127.0.0.1:$AGENTS_PORT/health" >/dev/null \
  || { tail -30 "$MAILMIND_LOGS_DIR/agents-cap.log"; fail "agents service didn't restart with cap config"; }

# /extract/run with force=true so cache won't short-circuit. The endpoint
# returns 200 with a `failures` list because we wrap each thread in a
# try/except — assert the failures list contains a per_task_cap_exceeded
# message.
EXR=$(curl -sf -X POST -H 'Content-Type: application/json' \
  -d '{"thread_ids":["fix-thread-101"],"force":true}' \
  "http://127.0.0.1:$AGENTS_PORT/extract/run")
echo "$EXR" | python3 -c "
import sys, json
r = json.loads(sys.stdin.read())
fails = r.get('failures', [])
assert fails, f'expected a failure under tight cap, got: {r}'
err = fails[0]['error']
assert 'per_task_cap_exceeded' in err or 'CostBudgetExceeded' in err, err
" || fail "per-task cap didn't refuse"
ok "per-task cap refused extract under tight config"

# ---------------- 12. audit-chain still healthy -----------------------
step "agents: /audit_log chain_ok=true (P4 invariant survives P5a)"
AUDIT=$(curl -sf "http://127.0.0.1:$AGENTS_PORT/audit_log?limit=50")
echo "$AUDIT" | python3 -c "
import sys, json
a = json.loads(sys.stdin.read())
assert a['chain_ok'] is True, a
" || fail "audit chain broken"
ok "audit chain still verifies"

# ---------------- summary ----------------
step "P5a acceptance: ALL GREEN"
echo "Logs: $MAILMIND_LOGS_DIR"
exit 0
