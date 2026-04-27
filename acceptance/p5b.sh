#!/usr/bin/env bash
# P5b acceptance: eval + labeling UI + LLM-as-judge.
#
# What this exercises (BUILD §23 + EXECUTION P5 items 1–3):
#   1. agents pytest (P0–P5b suites green).
#   2. webview build + ingester tests.
#   3. fixture pipeline → derived.sqlite + draft stubs.
#   4. POST /labels happy paths for thread + rollup + draft.
#   5. GET /labels?kind=…  filters correctly.
#   6. GET /labels  totals match.
#   7. POST /eval/run with seeded labels → returns metrics with
#      extract_agent, relationship_agent, draft_agent blocks.
#   8. POST /eval/baseline/freeze → baseline.jsonl written.
#   9. Second /eval/run with no agent changes → regressions=[].
#  10. DELETE /labels/{id} → soft delete; subsequent delete 404.
#  11. /eval/baseline reflects frozen state.
#  12. /thread_facts/{id} returns the persisted extraction.
#
# All Gemini calls remain mocked under MAILMIND_STUB_RESPONSES — eval
# runs end-to-end against the canned fixtures + the heuristic LLM-judge
# fallback (no API key needed).
#
# Usage: acceptance/p5b.sh

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

export MAILMIND_DATA_DIR="${MAILMIND_DATA_DIR:-$(mktemp -d -t mailmind-p5b)}"
export MAILMIND_LOGS_DIR="$MAILMIND_DATA_DIR/logs"
mkdir -p "$MAILMIND_LOGS_DIR"

# Force stub-only mode for the LLM judge. With a fake key the SDK fails
# fast on the actual HTTP request, which lets eval_agent's heuristic
# fallback (tone=0.5, would_send=false, overall=0.4 + 0.4 * must_cov) take
# over deterministically. Without this override, a real
# GOOGLE_GENAI_API_KEY in the developer's shell would route the judge
# through live Gemini, and Gemini's non-deterministic verdicts would make
# the "no regression on no-change re-run" assertion flap. Pytest fixtures
# do the same thing.
export GOOGLE_GENAI_API_KEY="fake-key-for-stub"

INGESTER_PORT="${INGESTER_PORT:-8891}"
AGENTS_PORT="${AGENTS_PORT:-8892}"

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

step "agents: install + pytest (all suites incl. P5b)"
(cd agents && uv venv --quiet --python 3.12 .venv 2>/dev/null || true)
(cd agents && uv pip install --quiet -e ".[dev,agents]" >/dev/null)
(cd agents && source .venv/bin/activate && pytest -q) > "$MAILMIND_LOGS_DIR/agents-test.log" 2>&1 \
  || { tail -120 "$MAILMIND_LOGS_DIR/agents-test.log"; fail "agents tests failed"; }
ok "agents tests pass (P0+P1+P2+P3+P4a+P4b+P5a+P5b suites)"

step "webview: install + typecheck + build"
(cd webview && npm install --silent --no-fund --no-audit)
(cd webview && npm run build) > "$MAILMIND_LOGS_DIR/webview-build.log" 2>&1 \
  || { tail -40 "$MAILMIND_LOGS_DIR/webview-build.log"; fail "webview build failed"; }
ok "webview builds (Eval tab compiled)"

# ---------------- 2. seed fixtures ----------------
step "fixtures: load seed.json into raw.sqlite"
node fixtures/load_fixtures.mjs > "$MAILMIND_LOGS_DIR/fixtures.log" 2>&1 \
  || { tail -20 "$MAILMIND_LOGS_DIR/fixtures.log"; fail "fixture load failed"; }
ok "fixtures loaded"

step "fixtures: build phase-1 stubs"
(cd agents && source .venv/bin/activate && python ../fixtures/build_stub_responses.py "$MAILMIND_DATA_DIR/stub.json") \
  > "$MAILMIND_LOGS_DIR/stub-build-1.log" 2>&1 \
  || { tail -20 "$MAILMIND_LOGS_DIR/stub-build-1.log"; fail "phase-1 stub build failed"; }
export MAILMIND_STUB_RESPONSES="$MAILMIND_DATA_DIR/stub.json"
ok "phase-1 stubs built"

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

# ---------------- 4. seed pipeline ----------------
step "agents: seed pipeline (extract → relationship → reconcile)"
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

# ---------------- 5. phase-2 stubs (drafts) ----------------
step "fixtures: rebuild stubs --include-draft"
(cd agents && source .venv/bin/activate && \
   python ../fixtures/build_stub_responses.py "$MAILMIND_DATA_DIR/stub.json" --include-draft) \
   > "$MAILMIND_LOGS_DIR/stub-build-2.log" 2>&1 \
   || { tail -30 "$MAILMIND_LOGS_DIR/stub-build-2.log"; fail "phase-2 stub build failed"; }
ok "draft stubs added"

# ---------------- 6. add labels ----------------
step "agents: POST /labels (thread + rollup + draft)"
T_LABEL=$(curl -sf -X POST -H 'Content-Type: application/json' \
  -d '{
    "kind": "thread",
    "target_id": "fix-thread-101",
    "expected": {
      "summary": "Morgan checked whether Adam at Vector Labs had reached out; Raphael said no and asked Morgan to nudge Adam.",
      "commitments_by_user": [],
      "commitments_by_others": [{"description": "Morgan will nudge Adam this week", "source_message_ids": ["fix-msg-101a"]}]
    },
    "notes": "fixture thread-101 baseline"
  }' \
  "http://127.0.0.1:$AGENTS_PORT/labels")
T_LABEL_ID=$(echo "$T_LABEL" | python3 -c "import sys,json; print(json.loads(sys.stdin.read())['id'])")
[[ -n "$T_LABEL_ID" ]] || fail "no thread label id returned"

R_LABEL=$(curl -sf -X POST -H 'Content-Type: application/json' \
  -d '{
    "kind": "rollup",
    "target_id": "morgan@northstar-talent.com",
    "expected": {"tone": "warm", "cadence": "weekly", "status": "active", "tags": ["recruiter"]}
  }' \
  "http://127.0.0.1:$AGENTS_PORT/labels")
R_LABEL_ID=$(echo "$R_LABEL" | python3 -c "import sys,json; print(json.loads(sys.stdin.read())['id'])")
[[ -n "$R_LABEL_ID" ]] || fail "no rollup label id returned"

D_LABEL=$(curl -sf -X POST -H 'Content-Type: application/json' \
  -d '{
    "kind": "draft",
    "target_id": "fix-thread-102::Reply to Adam Moore confirming the Thursday 2pm call",
    "expected": {
      "tone": "warm-confident",
      "must_mention": ["Thursday", "2pm"],
      "must_not_mention": [],
      "factually_correct": true,
      "would_send": true
    }
  }' \
  "http://127.0.0.1:$AGENTS_PORT/labels")
D_LABEL_ID=$(echo "$D_LABEL" | python3 -c "import sys,json; print(json.loads(sys.stdin.read())['id'])")
[[ -n "$D_LABEL_ID" ]] || fail "no draft label id returned"
ok "3 labels added (1 thread + 1 rollup + 1 draft)"

# ---------------- 7. listing + filtering ----------------
step "agents: GET /labels totals + filters"
curl -sf "http://127.0.0.1:$AGENTS_PORT/labels" | python3 -c "
import sys, json
d = json.loads(sys.stdin.read())
assert d['count'] == 3, d
assert d['counts_by_kind'] == {'thread': 1, 'rollup': 1, 'draft': 1, 'total': 3}, d['counts_by_kind']
" || fail "/labels totals wrong"

curl -sf "http://127.0.0.1:$AGENTS_PORT/labels?kind=thread" | python3 -c "
import sys, json
d = json.loads(sys.stdin.read())
assert d['count'] == 1, d
assert d['labels'][0]['kind'] == 'thread'
" || fail "/labels?kind=thread filter broken"
ok "/labels listing + filtering correct"

# ---------------- 8. /thread_facts read-only -----------------
step "agents: GET /thread_facts/fix-thread-101"
curl -sf "http://127.0.0.1:$AGENTS_PORT/thread_facts/fix-thread-101" | python3 -c "
import sys, json
d = json.loads(sys.stdin.read())
assert d['thread_id'] == 'fix-thread-101', d
assert 'facts' in d and ('commitments_by_user' in d['facts'] or 'commitments_by_others' in d['facts']), d
" || fail "/thread_facts shape wrong"
ok "/thread_facts returns persisted extraction"

# ---------------- 9. /eval/run ----------------
step "agents: POST /eval/run"
EVAL=$(curl -sf -X POST -H 'Content-Type: application/json' -d '{}' \
  "http://127.0.0.1:$AGENTS_PORT/eval/run")
echo "$EVAL" > "$MAILMIND_LOGS_DIR/eval-run.json"
echo "$EVAL" | python3 -c "
import sys, json
e = json.loads(sys.stdin.read())
assert e['ok'] is True, f'eval failed: {e}'
m = e['metrics']
assert 'extract_agent' in m, f'no extract_agent metrics: {m.keys()}'
assert 'relationship_agent' in m, f'no relationship_agent metrics: {m.keys()}'
assert 'draft_agent' in m, f'no draft_agent metrics: {m.keys()}'
# Headline numbers must be in [0, 1].
e_f1 = m['extract_agent']['f1_commitments_user']
r_tone = m['relationship_agent']['tone_accuracy']
d_style = m['draft_agent']['overall_style_match']
assert 0.0 <= e_f1 <= 1.0
assert 0.0 <= r_tone <= 1.0
assert 0.0 <= d_style <= 1.0
print(f'extract.f1_commitments_user={e_f1:.3f}, relationship.tone_accuracy={r_tone:.3f}, draft.overall_style={d_style:.3f}')
" || fail "/eval/run shape wrong"
ok "/eval/run returns metrics for all 3 agents"

# ---------------- 10. baseline freeze ----------------
step "agents: POST /eval/baseline/freeze"
FR=$(curl -sf -X POST "http://127.0.0.1:$AGENTS_PORT/eval/baseline/freeze")
echo "$FR" | python3 -c "
import sys, json
f = json.loads(sys.stdin.read())
assert 'frozen_run_id' in f and f['frozen_run_id']
" || fail "freeze response wrong"

curl -sf "http://127.0.0.1:$AGENTS_PORT/eval/baseline" | python3 -c "
import sys, json
d = json.loads(sys.stdin.read())
assert d['frozen'] is True, d
" || fail "/eval/baseline doesn't reflect frozen state"
ok "baseline frozen + readable"

# ---------------- 11. re-run → no regressions ----------------
step "agents: POST /eval/run after freeze → regressions empty"
EVAL2=$(curl -sf -X POST -H 'Content-Type: application/json' -d '{}' \
  "http://127.0.0.1:$AGENTS_PORT/eval/run")
echo "$EVAL2" | python3 -c "
import sys, json
e = json.loads(sys.stdin.read())
assert e['baseline_present'] is True, e
assert e['regressions'] == [], f'unexpected regressions on re-run: {e[\"regressions\"]}'
" || fail "regressions detected on no-change re-run"
ok "no regressions on no-change re-run (BUILD §23 5%-threshold holds)"

# ---------------- 12. DELETE /labels/{id} ----------------
step "agents: DELETE /labels/$T_LABEL_ID"
curl -sf -X DELETE "http://127.0.0.1:$AGENTS_PORT/labels/$T_LABEL_ID" | python3 -c "
import sys, json
d = json.loads(sys.stdin.read())
assert d['deleted'] is True
" || fail "DELETE /labels failed"

# Subsequent delete 404s.
CODE=$(curl -s -o /dev/null -w "%{http_code}" -X DELETE \
  "http://127.0.0.1:$AGENTS_PORT/labels/$T_LABEL_ID")
[[ "$CODE" == "404" ]] || fail "expected 404 on re-delete, got $CODE"

# Listing now shows 2.
curl -sf "http://127.0.0.1:$AGENTS_PORT/labels" | python3 -c "
import sys, json
d = json.loads(sys.stdin.read())
assert d['count'] == 2, f'after delete, count was {d[\"count\"]}'
" || fail "delete didn't reduce label count"
ok "soft-delete tombstone + idempotent (re-delete → 404)"

# ---------------- 13. /eval/results history ----------------
step "agents: GET /eval/results returns 2 runs"
curl -sf "http://127.0.0.1:$AGENTS_PORT/eval/results?limit=5" | python3 -c "
import sys, json
d = json.loads(sys.stdin.read())
assert d['count'] >= 2, d
" || fail "results history missing"
ok "/eval/results lists prior runs"

# ---------------- summary ----------------
step "P5b acceptance: ALL GREEN"
echo "Logs: $MAILMIND_LOGS_DIR"
exit 0
