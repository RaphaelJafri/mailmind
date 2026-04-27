#!/usr/bin/env bash
# P4a acceptance: drafts + approval gate, no Gmail send.
#
# What this exercises (BUILD §16):
#   1. agents pytest (P0+P1+P2+P3+P4 suites green).
#   2. webview build + ingester tests.
#   3. fixture pipeline → derived.sqlite.
#   4. /draft/generate writes a pending draft for fix-thread-102 with the
#      cited fact "thursday_2pm_proposal".
#   5. POST /permissions {gmail_compose:true} grants the scope.
#   6. POST /permissions {gmail_send:true} (without compose) is refused with
#      400 compose_required — additive UX gate that survives into P4b. The
#      P4a-era hard-403 was lifted in P4b; the safety guarantee ("no send
#      until drafts work") is now enforced by the compose-required check.
#   7. /drafts/{id}/approve creates an approval row, draft.status=approved.
#   8. /drafts/{id}/save_as_gmail_draft executes against the mock gmail.compose
#      seam (MAILMIND_GMAIL_MOCK=1), draft.status=saved_as_draft, audit row
#      lands.
#   9. Edit-after-approve is rejected by the hash-mismatch check.
#  10. Cancel-during-undo reverts the draft to pending and audit-logs cancel.
#  11. /audit_log reports chain_ok=true and ≥3 events; SQLite triggers refuse
#      UPDATE/DELETE on audit_log.
#  12. Send seam refuses outright in P4a (no gmail.send scope grant).
#
# Stub-Gemini only — no network calls.
#
# Usage: acceptance/p4a.sh

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

export MAILMIND_DATA_DIR="${MAILMIND_DATA_DIR:-$(mktemp -d -t mailmind-p4a)}"
export MAILMIND_LOGS_DIR="$MAILMIND_DATA_DIR/logs"
mkdir -p "$MAILMIND_LOGS_DIR"

INGESTER_PORT="${INGESTER_PORT:-8887}"
AGENTS_PORT="${AGENTS_PORT:-8888}"

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

step "agents: install + pytest (P0+P1+P2+P3+P4 suites)"
(cd agents && uv venv --quiet --python 3.12 .venv 2>/dev/null || true)
(cd agents && uv pip install --quiet -e ".[dev,agents]" >/dev/null)
(cd agents && source .venv/bin/activate && pytest -q) > "$MAILMIND_LOGS_DIR/agents-test.log" 2>&1 \
  || { tail -120 "$MAILMIND_LOGS_DIR/agents-test.log"; fail "agents tests failed"; }
ok "agents tests pass (P0+P1+P2+P3+P4 suites)"

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

step "fixtures: build stub Gemini responses (phase 1 — no draft stubs yet)"
(cd agents && source .venv/bin/activate && python ../fixtures/build_stub_responses.py "$MAILMIND_DATA_DIR/stub.json") \
  > "$MAILMIND_LOGS_DIR/stub-build-1.log" 2>&1 \
  || { tail -20 "$MAILMIND_LOGS_DIR/stub-build-1.log"; fail "phase-1 stub build failed"; }
export MAILMIND_STUB_RESPONSES="$MAILMIND_DATA_DIR/stub.json"
# Mock the gmail.compose seam end-to-end in this acceptance run.
export MAILMIND_GMAIL_MOCK=1
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

step "agents: start on :$AGENTS_PORT (stub mode + skipped Gemini health + mock gmail)"
(cd agents && source .venv/bin/activate && \
   MAILMIND_HEALTH_SKIP_GEMINI=1 MAILMIND_GMAIL_MOCK=1 PORT=$AGENTS_PORT python service.py) \
  > "$MAILMIND_LOGS_DIR/agents.log" 2>&1 &
AGENTS_PID=$!
for _ in $(seq 1 40); do
  curl -sf "http://127.0.0.1:$AGENTS_PORT/health" >/dev/null && break
  sleep 0.25
done

# ---------------- 4. populate derived.sqlite ----------------
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

# ---------------- 5. phase-2 stub build (now with draft stubs) ----------
step "fixtures: rebuild stubs --include-draft"
(cd agents && source .venv/bin/activate && \
   python ../fixtures/build_stub_responses.py "$MAILMIND_DATA_DIR/stub.json" --include-draft) \
   > "$MAILMIND_LOGS_DIR/stub-build-2.log" 2>&1 \
   || { tail -30 "$MAILMIND_LOGS_DIR/stub-build-2.log"; fail "phase-2 stub build failed"; }
STUB_COUNT=$(python3 -c "import json; print(len(json.load(open('$MAILMIND_DATA_DIR/stub.json'))))")
[[ "$STUB_COUNT" -ge 13 ]] || fail "expected ≥13 stub entries, got $STUB_COUNT"
ok "stubs rebuilt with draft entries (count=$STUB_COUNT)"

# ---------------- 6. /draft/generate produces a pending draft -----------
step "agents: POST /draft/generate (Adam intro reply)"
GEN=$(curl -sf -X POST -H 'Content-Type: application/json' \
  -d '{"thread_id":"fix-thread-102","intent":"Reply to Adam Moore confirming the Thursday 2pm call"}' \
  "http://127.0.0.1:$AGENTS_PORT/draft/generate")
echo "$GEN" > "$MAILMIND_LOGS_DIR/draft-generate.json"
DRAFT_ID=$(echo "$GEN" | python3 -c "import sys,json; print(json.loads(sys.stdin.read())['draft_id'])")
echo "$GEN" | python3 -c "
import sys, json
g = json.loads(sys.stdin.read())
d = g['draft']
assert d['confidence'] == 'high', d
assert 'Thursday' in d['body'], d
assert d['thread_id'] == 'fix-thread-102', d
assert any(f['fact_id'] == 'thursday_2pm_proposal' for f in d['cited_facts']), d
assert g['stubbed'] is True, g
print(f\"draft_id={g['draft_id']} confidence=high cited={len(d['cited_facts'])} stubbed=True\")
" || fail "generated draft failed structural checks"
ok "/draft/generate wrote a pending draft for fix-thread-102"

# ---------------- 7. /permissions: send refused (compose-required), compose grant ---
step "agents: POST /permissions {gmail_send:true} without compose → 400"
CODE=$(curl -s -o "$MAILMIND_LOGS_DIR/perm-400.json" -w "%{http_code}" \
  -X POST -H 'Content-Type: application/json' \
  -d '{"gmail_send":true}' "http://127.0.0.1:$AGENTS_PORT/permissions")
[[ "$CODE" == "400" ]] || fail "expected 400 compose_required, got $CODE"
python3 -c "
import json
d = json.load(open('$MAILMIND_LOGS_DIR/perm-400.json'))['detail']
assert d['code'] == 'compose_required', d
" || fail "wrong code on send-without-compose"
ok "/permissions refuses gmail_send without compose first (additive UX gate)"

step "agents: POST /permissions {gmail_compose:true} (grant the scope)"
PERMS=$(curl -sf -X POST -H 'Content-Type: application/json' \
  -d '{"gmail_compose":true}' "http://127.0.0.1:$AGENTS_PORT/permissions")
echo "$PERMS" | python3 -c "
import sys, json
p = json.loads(sys.stdin.read())
assert p['gmail.compose'] is True, p
assert p['gmail.send'] is False, p
" || fail "compose grant didn't stick"
ok "gmail.compose granted"

# ---------------- 8. approve → save_as_gmail_draft happy path ----------
step "agents: approve draft → execute save_as_gmail_draft"
AP=$(curl -sf -X POST -H 'Content-Type: application/json' \
  -d '{"action":"save_as_draft"}' \
  "http://127.0.0.1:$AGENTS_PORT/drafts/$DRAFT_ID/approve")
APPROVAL_ID=$(echo "$AP" | python3 -c "import sys,json; print(json.loads(sys.stdin.read())['approval_id'])")
[[ -n "$APPROVAL_ID" ]] || fail "no approval_id returned"

# Acceptance script doesn't sleep through the undo window — execute is
# synchronous on the API side, the webview is what waits.
SAVE=$(curl -sf -X POST -H 'Content-Type: application/json' \
  -d "{\"approval_id\":\"$APPROVAL_ID\"}" \
  "http://127.0.0.1:$AGENTS_PORT/drafts/$DRAFT_ID/save_as_gmail_draft")
echo "$SAVE" | python3 -c "
import sys, json
s = json.loads(sys.stdin.read())
assert s['next_status'] == 'saved_as_draft', s
assert s['gmail_draft_id'].startswith('draft-'), s
print(f\"saved gmail_draft_id={s['gmail_draft_id']}\")
" || fail "save_as_gmail_draft response shape wrong"
ok "save_as_gmail_draft executed against mock seam"

# Verify the draft row is now saved_as_draft.
DD=$(curl -sf "http://127.0.0.1:$AGENTS_PORT/drafts/$DRAFT_ID")
echo "$DD" | python3 -c "
import sys, json
d = json.loads(sys.stdin.read())
assert d['status'] == 'saved_as_draft', d
assert d['gmail_draft_id'].startswith('draft-'), d
" || fail "draft status didn't move to saved_as_draft"
ok "draft.status=saved_as_draft"

# ---------------- 9. hash-mismatch refusal -----------------------------
step "agents: cancel + new draft + edit-after-approve → hash mismatch"
GEN2=$(curl -sf -X POST -H 'Content-Type: application/json' \
  -d '{"thread_id":"fix-thread-102","intent":"Reply to Adam Moore confirming the Thursday 2pm call"}' \
  "http://127.0.0.1:$AGENTS_PORT/draft/generate")
DRAFT_ID2=$(echo "$GEN2" | python3 -c "import sys,json; print(json.loads(sys.stdin.read())['draft_id'])")
AP2=$(curl -sf -X POST -H 'Content-Type: application/json' -d '{"action":"save_as_draft"}' \
  "http://127.0.0.1:$AGENTS_PORT/drafts/$DRAFT_ID2/approve")
APPROVAL_ID2=$(echo "$AP2" | python3 -c "import sys,json; print(json.loads(sys.stdin.read())['approval_id'])")

# Tamper the draft body directly via the SQLite file (simulating a defense-in-
# depth scenario — the API edit endpoint refuses post-approval edits, so we
# go through sqlite to test the hash gate itself).
python3 - <<PY
import sqlite3, os
p = os.path.join(os.environ['MAILMIND_DATA_DIR'], 'db', 'derived.sqlite')
with sqlite3.connect(p) as c:
    c.execute("UPDATE drafts SET body = 'tampered', draft_hash = 'deadbeef' WHERE id = ?", ("$DRAFT_ID2",))
    c.commit()
PY

CODE=$(curl -s -o "$MAILMIND_LOGS_DIR/hash-mismatch.json" -w "%{http_code}" \
  -X POST -H 'Content-Type: application/json' \
  -d "{\"approval_id\":\"$APPROVAL_ID2\"}" \
  "http://127.0.0.1:$AGENTS_PORT/drafts/$DRAFT_ID2/save_as_gmail_draft")
[[ "$CODE" == "400" ]] || fail "expected 400 hash-mismatch, got $CODE: $(cat $MAILMIND_LOGS_DIR/hash-mismatch.json)"
python3 -c "
import json
d = json.load(open('$MAILMIND_LOGS_DIR/hash-mismatch.json'))['detail']
assert d['code'] == 'hash_mismatch', d
" || fail "wrong error code on hash-mismatch"
ok "hash-mismatch refused with 400 hash_mismatch"

# ---------------- 10. cancel-during-undo flow --------------------------
step "agents: approve + cancel reverts draft to pending"
GEN3=$(curl -sf -X POST -H 'Content-Type: application/json' \
  -d '{"thread_id":"fix-thread-102","intent":"Reply to Adam Moore confirming the Thursday 2pm call"}' \
  "http://127.0.0.1:$AGENTS_PORT/draft/generate")
DRAFT_ID3=$(echo "$GEN3" | python3 -c "import sys,json; print(json.loads(sys.stdin.read())['draft_id'])")
AP3=$(curl -sf -X POST -H 'Content-Type: application/json' -d '{"action":"save_as_draft"}' \
  "http://127.0.0.1:$AGENTS_PORT/drafts/$DRAFT_ID3/approve")
APPROVAL_ID3=$(echo "$AP3" | python3 -c "import sys,json; print(json.loads(sys.stdin.read())['approval_id'])")

curl -sf -X POST -H 'Content-Type: application/json' \
  -d "{\"approval_id\":\"$APPROVAL_ID3\",\"reason\":\"acceptance test\"}" \
  "http://127.0.0.1:$AGENTS_PORT/drafts/$DRAFT_ID3/cancel" >/dev/null \
  || fail "/cancel failed"

DD3=$(curl -sf "http://127.0.0.1:$AGENTS_PORT/drafts/$DRAFT_ID3")
echo "$DD3" | python3 -c "
import sys, json
d = json.loads(sys.stdin.read())
assert d['status'] == 'pending', d
assert d['approval_id'] is None, d
" || fail "cancel didn't revert draft to pending"
ok "cancel reverts draft to pending and clears approval_id"

# ---------------- 11. /audit_log integrity -----------------------------
step "agents: GET /audit_log — chain ok + ≥3 events"
AL=$(curl -sf "http://127.0.0.1:$AGENTS_PORT/audit_log?limit=200")
echo "$AL" | python3 -c "
import sys, json
b = json.loads(sys.stdin.read())
assert b['chain_ok'] is True, b
events = [e['event_type'] for e in b['events']]
print(f'audit log has {b[\"count\"]} events: {events}')
assert b['count'] >= 5, b
for needed in ('approve','save_as_draft','cancel','auth_grant'):
    assert needed in events, f'missing {needed} in {events}'
" || fail "audit log integrity check failed"
ok "audit_log chain_ok=true, all expected event types present"

# Trigger-level immutability check (raw SQLite, bypassing API).
step "audit_log: SQLite triggers refuse UPDATE/DELETE"
python3 - <<'PY'
import os, sqlite3, sys
p = os.path.join(os.environ['MAILMIND_DATA_DIR'], 'db', 'derived.sqlite')
con = sqlite3.connect(p)
try:
    try:
        con.execute("UPDATE audit_log SET event_type='tampered' WHERE rowid=1")
    except sqlite3.IntegrityError as e:
        if 'append-only' not in str(e):
            sys.exit(f"unexpected error on UPDATE: {e}")
    else:
        sys.exit("UPDATE did NOT raise — trigger missing")
    try:
        con.execute("DELETE FROM audit_log")
    except sqlite3.IntegrityError as e:
        if 'append-only' not in str(e):
            sys.exit(f"unexpected error on DELETE: {e}")
    else:
        sys.exit("DELETE did NOT raise — trigger missing")
    print("triggers verified: audit_log refuses UPDATE and DELETE")
finally:
    con.close()
PY
ok "audit_log triggers reject UPDATE/DELETE at the schema level"

# ---------------- 12. summary ----------------
step "P4a acceptance: ALL GREEN"
echo "Logs: $MAILMIND_LOGS_DIR"
exit 0
