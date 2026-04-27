#!/usr/bin/env bash
# P2 acceptance: P1 surface + Relationship rollup, deterministic cadence,
# reconcile (with §19 dismiss-bug fix) running end-to-end against the
# stub-Gemini fixture and exposed over the agent service's HTTP API.
#
# Always uses MAILMIND_STUB_RESPONSES (no Vertex). The acceptance script
# does not need a billable Gemini call to validate P2.
#
# Usage: acceptance/p2.sh
# Env:   MAILMIND_DATA_DIR (override; defaults to a tmp dir)

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

export MAILMIND_DATA_DIR="${MAILMIND_DATA_DIR:-$(mktemp -d -t mailmind-p2)}"
export MAILMIND_LOGS_DIR="$MAILMIND_DATA_DIR/logs"
mkdir -p "$MAILMIND_LOGS_DIR"

INGESTER_PORT="${INGESTER_PORT:-8877}"
AGENTS_PORT="${AGENTS_PORT:-8878}"

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

step "agents: install + pytest (P0+P1+P2)"
(cd agents && uv venv --quiet --python 3.12 .venv 2>/dev/null || true)
(cd agents && uv pip install --quiet -e ".[dev,agents]" >/dev/null)
(cd agents && source .venv/bin/activate && pytest -q) > "$MAILMIND_LOGS_DIR/agents-test.log" 2>&1 \
  || { tail -80 "$MAILMIND_LOGS_DIR/agents-test.log"; fail "agents tests failed"; }
ok "agents tests pass (P0+P1+P2 suites)"

step "webview: install + typecheck + build"
(cd webview && npm install --silent --no-fund --no-audit)
(cd webview && npm run build) > "$MAILMIND_LOGS_DIR/webview-build.log" 2>&1 \
  || { tail -40 "$MAILMIND_LOGS_DIR/webview-build.log"; fail "webview build failed"; }
ok "webview builds"

# ---------------- 2. seed fixtures ----------------
step "fixtures: load seed.json into raw.sqlite"
node fixtures/load_fixtures.mjs > "$MAILMIND_LOGS_DIR/fixtures.log" 2>&1 \
  || { tail -20 "$MAILMIND_LOGS_DIR/fixtures.log"; fail "fixture load failed"; }
ok "fixtures loaded"

step "fixtures: build stub Gemini responses"
(cd agents && source .venv/bin/activate && python ../fixtures/build_stub_responses.py "$MAILMIND_DATA_DIR/stub.json") \
  > "$MAILMIND_LOGS_DIR/stub-build.log" 2>&1 \
  || { tail -20 "$MAILMIND_LOGS_DIR/stub-build.log"; fail "stub build failed"; }
export MAILMIND_STUB_RESPONSES="$MAILMIND_DATA_DIR/stub.json"
ok "stub responses built ($MAILMIND_STUB_RESPONSES)"

# ---------------- 3. spawn sidecars ----------------
step "ingester: start on :$INGESTER_PORT"
(cd ingester && PORT=$INGESTER_PORT node src/index.mjs) \
  > "$MAILMIND_LOGS_DIR/ingester.log" 2>&1 &
INGESTER_PID=$!
for _ in $(seq 1 20); do
  curl -sf "http://127.0.0.1:$INGESTER_PORT/health" >/dev/null && break
  sleep 0.25
done

step "agents: start on :$AGENTS_PORT (stub mode + skipped Gemini health)"
(cd agents && source .venv/bin/activate && \
   MAILMIND_HEALTH_SKIP_GEMINI=1 PORT=$AGENTS_PORT python service.py) \
  > "$MAILMIND_LOGS_DIR/agents.log" 2>&1 &
AGENTS_PID=$!
for _ in $(seq 1 40); do
  curl -sf "http://127.0.0.1:$AGENTS_PORT/health" >/dev/null && break
  sleep 0.25
done

# ---------------- 4. /health smokes ----------------
RES=$(curl -sf "http://127.0.0.1:$INGESTER_PORT/health") || fail "ingester /health unreachable"
echo "$RES" | grep -q '"sidecar":"mailmind-ingester"' || fail "ingester sidecar identity wrong"
ok "ingester /health ok"

RES=$(curl -sf "http://127.0.0.1:$AGENTS_PORT/health") || fail "agents /health unreachable"
echo "$RES" | grep -q '"sidecar":"mailmind-agents"' || fail "agents sidecar identity wrong"
ok "agents /health ok"

# ---------------- 5. extract keep-disposition threads (rollup prereq) ----------------
step "agents: POST /extract/run for keep-disposition fixtures"
RES=$(curl -sf -X POST -H 'Content-Type: application/json' \
  -d '{"thread_ids":["fix-thread-101","fix-thread-102","fix-thread-103"]}' \
  "http://127.0.0.1:$AGENTS_PORT/extract/run") || fail "/extract/run failed"
EXTRACTED=$(echo "$RES" | python3 -c "import sys,json; print(json.loads(sys.stdin.read())['count'])")
[[ "$EXTRACTED" == "3" ]] || fail "expected 3 extracts, got $EXTRACTED: $RES"
ok "/extract/run extracted 3 keep-threads"

# ---------------- 6. POST /relationship/run ----------------
step "agents: POST /relationship/run (sweep)"
RES=$(curl -sf -X POST -H 'Content-Type: application/json' -d '{}' \
  "http://127.0.0.1:$AGENTS_PORT/relationship/run") || fail "/relationship/run failed"
ROLLED=$(echo "$RES" | python3 -c "import sys,json; print(json.loads(sys.stdin.read())['count'])")
[[ "$ROLLED" == "2" ]] || fail "expected 2 rollups (Morgan+Adam), got $ROLLED: $RES"
ok "/relationship/run produced 2 rollups"

# ---------------- 7. GET /contact_rollups + schema validation ----------------
RES=$(curl -sf "http://127.0.0.1:$AGENTS_PORT/contact_rollups")
COUNT=$(echo "$RES" | python3 -c "import sys,json; print(json.loads(sys.stdin.read())['count'])")
[[ "$COUNT" == "2" ]] || fail "expected 2 rollups in /contact_rollups, got $COUNT"

"$ROOT/agents/.venv/bin/python" - <<'PY' "$AGENTS_PORT" "$ROOT"
import json, sys, urllib.request
sys.path.insert(0, sys.argv[2] + "/agents")
from lib import schema
res = urllib.request.urlopen(f"http://127.0.0.1:{sys.argv[1]}/contact_rollups").read()
rollups = json.loads(res)["rollups"]
required = {"contact_email","relationship_summary","tone","cadence","status",
            "tags","draft_next_steps","source_thread_ids","confidence"}
for r in rollups:
    body = {
      "contact_email": r["contact_email"],
      "relationship_summary": r["relationship_summary"],
      "tone": r["tone"], "cadence": r["cadence"], "status": r["status"],
      "tags": r["tags"],
      "source_thread_ids": r["source_thread_ids"],
      "confidence": r["confidence"],
      "draft_next_steps": [],  # rollup endpoint elides drafts; full payload validated in unit tests
    }
    schema.validate(body, "contact-rollup.schema.json")
print(f"validated {len(rollups)} rollups against schema")
PY
ok "/contact_rollups returns 2 schema-valid rollups"

# ---------------- 8. POST /reconcile/run ----------------
step "agents: POST /reconcile/run (initial)"
RES=$(curl -sf -X POST -H 'Content-Type: application/json' -d '{}' \
  "http://127.0.0.1:$AGENTS_PORT/reconcile/run") || fail "/reconcile/run failed"
INSERTED=$(echo "$RES" | python3 -c "import sys,json; print(json.loads(sys.stdin.read())['totals']['inserted'])")
PENDING=$(echo "$RES" | python3 -c "import sys,json; print(json.loads(sys.stdin.read())['pending_total'])")
[[ "$INSERTED" == "2" ]] || fail "expected inserted=2, got $INSERTED: $RES"
[[ "$PENDING" == "2" ]] || fail "expected pending_total=2, got $PENDING"
ok "/reconcile/run inserted 2 pending steps"

# ---------------- 9. GET /next_steps ----------------
STEPS=$(curl -sf "http://127.0.0.1:$AGENTS_PORT/next_steps")
COUNT=$(echo "$STEPS" | python3 -c "import sys,json; print(json.loads(sys.stdin.read())['count'])")
[[ "$COUNT" == "2" ]] || fail "expected 2 next_steps, got $COUNT"
ADAM_ID=$(echo "$STEPS" | python3 -c "
import sys,json
data = json.loads(sys.stdin.read())
adam = next(s for s in data['next_steps'] if s['contact_email'] == 'adam@vectorlabs.io')
print(adam['id'])
")
ok "/next_steps returned 2 pending; Adam step = ${ADAM_ID:0:8}…"

# ---------------- 10. GET /followups (cadence classification) ----------------
RES=$(curl -sf "http://127.0.0.1:$AGENTS_PORT/followups")
echo "$RES" | python3 -c "
import sys, json
r = json.loads(sys.stdin.read())
they = {(e['thread_id'], e['urgency']) for e in r['they_owe_you']}
you  = {(e['thread_id'], e['urgency']) for e in r['you_owe_them']}
assert ('fix-thread-101','overdue') in they, f'missing 101/overdue in they_owe_you: {they}'
assert ('fix-thread-103','cold')    in they, f'missing 103/cold in they_owe_you: {they}'
assert ('fix-thread-102','overdue') in you,  f'missing 102/overdue in you_owe_them: {you}'
print('cadence classification correct')
" || fail "cadence classification wrong"
ok "/followups classifies overdue + cold correctly"

# ---------------- 11. POST /next_steps/{id}/dismiss + §19 dismiss-bug fix ----------------
step "agents: dismiss Adam's step + verify reconcile drops re-proposal"
DISMISSED=$(curl -sf -X POST -H 'Content-Type: application/json' \
  -d '{"user_note":"not pursuing"}' \
  "http://127.0.0.1:$AGENTS_PORT/next_steps/$ADAM_ID/dismiss") || fail "/dismiss failed"
echo "$DISMISSED" | grep -q '"correction_id"' || fail "dismiss didn't return correction_id"
ok "next_step dismissed + correction recorded"

# Re-run reconcile against the SAME draft batch: dismissed step must not resurrect.
RES=$(curl -sf -X POST -H 'Content-Type: application/json' -d '{}' \
  "http://127.0.0.1:$AGENTS_PORT/reconcile/run") || fail "/reconcile/run (2nd) failed"
DROPPED=$(echo "$RES" | python3 -c "import sys,json; print(json.loads(sys.stdin.read())['totals']['dropped_dismissed'])")
[[ "$DROPPED" -ge 1 ]] || fail "expected dropped_dismissed≥1, got $DROPPED: $RES"

PENDING_AFTER=$(curl -sf "http://127.0.0.1:$AGENTS_PORT/next_steps" \
  | python3 -c "
import sys,json
data = json.loads(sys.stdin.read())
adam_pending = [s for s in data['next_steps'] if s['contact_email'] == 'adam@vectorlabs.io']
print(len(adam_pending))
")
[[ "$PENDING_AFTER" == "0" ]] || fail "§19 regression: Adam pending after dismiss = $PENDING_AFTER, want 0"
ok "§19 dismiss-bug fix: dropped=$DROPPED, Adam pending=0"

# ---------------- 12. POST /cadence/run (deterministic agent_runs row) ----------------
RES=$(curl -sf -X POST "http://127.0.0.1:$AGENTS_PORT/cadence/run") || fail "/cadence/run failed"
echo "$RES" | grep -q '"stubbed":false' || fail "cadence run should report stubbed=false"
ok "/cadence/run records deterministic agent_run"

# ---------------- 13. /agent_runs reflects all P2 work ----------------
RES=$(curl -sf "http://127.0.0.1:$AGENTS_PORT/agent_runs?limit=200")
RUN_COUNT=$(echo "$RES" | python3 -c "import sys,json; print(len(json.loads(sys.stdin.read())['runs']))")
# Should have at least: 3 extract + 2 relationship + 1 cadence_runner = 6 P2 runs.
[[ "$RUN_COUNT" -ge 6 ]] || fail "expected ≥6 agent_runs, got $RUN_COUNT"
ok "/agent_runs returned $RUN_COUNT rows"

# ---------------- 14. /corrections trail ----------------
RES=$(curl -sf "http://127.0.0.1:$AGENTS_PORT/corrections")
echo "$RES" | python3 -c "
import sys, json
c = json.loads(sys.stdin.read())['corrections']
assert any(r['entity_type'] == 'next_step' and r['new_value'] == 'dismissed' for r in c), c
print(f'{len(c)} correction(s) on file')
" || fail "/corrections missing dismiss entry"
ok "/corrections audit trail intact"

# ---------------- summary ----------------
step "P2 acceptance: ALL GREEN"
echo "Logs: $MAILMIND_LOGS_DIR"
exit 0
