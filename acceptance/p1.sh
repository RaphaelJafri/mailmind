#!/usr/bin/env bash
# P1 acceptance: P0 surface + Triage / Extract / Tagger run end-to-end against
# the stub-Gemini fixture, persist to SQLite, and respond over the agent
# service's HTTP API. Webview build is verified.
#
# Always uses MAILMIND_STUB_RESPONSES (no Vertex). The acceptance script does
# not need a billable Gemini call to validate P1 — that's what stub mode is
# for. Real-Vertex P0 round-trip stays in p0.sh.
#
# Usage: acceptance/p1.sh
# Env:   MAILMIND_DATA_DIR (override; defaults to a tmp dir)

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

export MAILMIND_DATA_DIR="${MAILMIND_DATA_DIR:-$(mktemp -d -t mailmind-p1)}"
export MAILMIND_LOGS_DIR="$MAILMIND_DATA_DIR/logs"
mkdir -p "$MAILMIND_LOGS_DIR"

INGESTER_PORT="${INGESTER_PORT:-8867}"
AGENTS_PORT="${AGENTS_PORT:-8868}"

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

# ---------------- 1. install + unit tests ----------------
step "ingester: install + tests"
(cd ingester && npm install --silent --no-fund --no-audit)
(cd ingester && npm test) > "$MAILMIND_LOGS_DIR/ingester-test.log" 2>&1 \
  || { tail -40 "$MAILMIND_LOGS_DIR/ingester-test.log"; fail "ingester tests failed"; }
ok "ingester tests pass"

step "agents: install + pytest"
(cd agents && uv venv --quiet --python 3.12 .venv 2>/dev/null || true)
(cd agents && uv pip install --quiet -e ".[dev,agents]" >/dev/null)
(cd agents && source .venv/bin/activate && pytest -q) > "$MAILMIND_LOGS_DIR/agents-test.log" 2>&1 \
  || { tail -60 "$MAILMIND_LOGS_DIR/agents-test.log"; fail "agents tests failed"; }
ok "agents tests pass (P0 + P1 suites)"

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

# ---------------- 5. POST /triage/run ----------------
step "agents: POST /triage/run"
RES=$(curl -sf -X POST -H 'Content-Type: application/json' \
  -d '{"min_thread_count":1,"limit":30}' \
  "http://127.0.0.1:$AGENTS_PORT/triage/run") || fail "/triage/run failed"
WROTE=$(echo "$RES" | python3 -c "import sys,json; print(json.loads(sys.stdin.read())['proposals_written'])")
[[ "$WROTE" == "3" ]] || fail "expected 3 proposals written, got $WROTE — body=$RES"
ok "/triage/run wrote 3 proposals"

# ---------------- 6. GET /triage/proposals ----------------
RES=$(curl -sf "http://127.0.0.1:$AGENTS_PORT/triage/proposals?status=pending")
COUNT=$(echo "$RES" | python3 -c "import sys,json; print(json.loads(sys.stdin.read())['count'])")
[[ "$COUNT" == "3" ]] || fail "expected 3 pending proposals, got $COUNT"

# Verify each proposal validates against the JSON schema.
# Use the venv's python so jsonschema + lib.schema are importable.
"$ROOT/agents/.venv/bin/python" - <<'PY' "$AGENTS_PORT" "$ROOT"
import json, sys, urllib.request
sys.path.insert(0, sys.argv[2] + "/agents")
from lib import schema
res = urllib.request.urlopen(f"http://127.0.0.1:{sys.argv[1]}/triage/proposals?status=pending").read()
data = json.loads(res)
for p in data["proposals"]:
    body = {k: p[k] for k in ("sender_email","proposed_disposition","confidence","rationale","sample_subjects","thread_count")}
    if p.get("cited_user_context_section"):
        body["cited_user_context_section"] = p["cited_user_context_section"]
    schema.validate(body, "triage-proposal.schema.json")
print(f"validated {len(data['proposals'])} proposals against schema")
PY
ok "/triage/proposals returns 3 valid proposals"

# ---------------- 7. POST /triage/proposals/{id}/decide ----------------
PID=$(echo "$RES" | python3 -c "import sys,json; print(json.loads(sys.stdin.read())['proposals'][0]['id'])")
DECIDE=$(curl -sf -X POST -H 'Content-Type: application/json' \
  -d '{"decision":"approve"}' \
  "http://127.0.0.1:$AGENTS_PORT/triage/proposals/$PID/decide") || fail "/decide failed"
echo "$DECIDE" | grep -q '"user_decision":"approve"' || fail "decide didn't persist"
ok "/triage/proposals/.../decide persists user_decision"

# ---------------- 8. POST /extract/run x3 ----------------
step "agents: POST /extract/run for 3 fixture threads"
RES=$(curl -sf -X POST -H 'Content-Type: application/json' \
  -d '{"thread_ids":["fix-thread-001","fix-thread-002","fix-thread-003"]}' \
  "http://127.0.0.1:$AGENTS_PORT/extract/run") || fail "/extract/run failed"
EXTRACTED=$(echo "$RES" | python3 -c "import sys,json; print(json.loads(sys.stdin.read())['count'])")
FAILED=$(echo "$RES" | python3 -c "import sys,json; print(json.loads(sys.stdin.read())['failed'])")
[[ "$EXTRACTED" == "3" ]] || fail "expected 3 extracts, got $EXTRACTED (failed=$FAILED): $RES"
ok "/extract/run extracted 3 threads"

# ---------------- 9. POST /tag/run x4 ----------------
step "agents: POST /tag/run for 4 fixture messages"
RES=$(curl -sf -X POST -H 'Content-Type: application/json' \
  -d '{"message_ids":["fix-msg-001a","fix-msg-001b","fix-msg-002a","fix-msg-003a"]}' \
  "http://127.0.0.1:$AGENTS_PORT/tag/run") || fail "/tag/run failed"
TAGGED=$(echo "$RES" | python3 -c "import sys,json; print(json.loads(sys.stdin.read())['count'])")
[[ "$TAGGED" == "4" ]] || fail "expected 4 tagged, got $TAGGED: $RES"
ok "/tag/run tagged 4 messages"

# ---------------- 10. GET /tags summary ----------------
RES=$(curl -sf "http://127.0.0.1:$AGENTS_PORT/tags")
echo "$RES" | python3 -c "
import sys,json
data = json.loads(sys.stdin.read())
summary = {(s['tag_kind'], s['tag_value']): s['message_count'] for s in data['summary']}
assert summary[('urgency','followup')] == 1, summary
assert summary[('category','recruiting')] == 2, summary
assert summary[('project','jobsearch')] == 2, summary
print('tag summary correct')
" || fail "tag summary wrong"
ok "/tags summary matches expectations"

# ---------------- 11. GET /agent_runs ----------------
RES=$(curl -sf "http://127.0.0.1:$AGENTS_PORT/agent_runs")
RUN_COUNT=$(echo "$RES" | python3 -c "import sys,json; print(len(json.loads(sys.stdin.read())['runs']))")
# Should have at least: 1 triage + 3 extract + 4 tag = 8 runs.
[[ "$RUN_COUNT" -ge 8 ]] || fail "expected ≥8 agent_runs, got $RUN_COUNT"
ok "/agent_runs returned $RUN_COUNT rows"

# ---------------- 12. ingester /threads + /contacts ----------------
step "ingester: /threads + /contacts read views"
THREADS=$(curl -sf "http://127.0.0.1:$INGESTER_PORT/threads")
echo "$THREADS" | grep -q '"raw_db_present":true' || fail "ingester reports no raw db"
echo "$THREADS" | python3 -c "
import sys,json
d = json.loads(sys.stdin.read())
# 3 unclassified (the P1 triage targets) + 3 pre-classified for P2 rollup.
assert d['count'] >= 3, d
" || fail "expected ≥3 fixture threads"
ok "ingester /threads returned $(echo "$THREADS" | python3 -c "import sys,json;print(json.loads(sys.stdin.read())['count'])") fixture threads"

CONTACTS=$(curl -sf "http://127.0.0.1:$INGESTER_PORT/contacts")
CCNT=$(echo "$CONTACTS" | python3 -c "import sys,json; print(json.loads(sys.stdin.read())['count'])")
[[ "$CCNT" -ge 4 ]] || fail "expected ≥4 contacts, got $CCNT"
ok "ingester /contacts returned $CCNT contacts"

# ---------------- summary ----------------
step "P1 acceptance: ALL GREEN"
echo "Logs: $MAILMIND_LOGS_DIR"
exit 0
