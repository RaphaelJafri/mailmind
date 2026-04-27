#!/usr/bin/env bash
# P4b acceptance: send capability via gmail.send + send_agent.
#
# What this exercises (BUILD §16, EXECUTION P4b):
#   1. agents pytest (P0+P1+P2+P3+P4a+P4b suites green).
#   2. webview build + ingester tests.
#   3. fixture pipeline → derived.sqlite.
#   4. /permissions {gmail_send:true} without gmail.compose → 400
#      compose_required (additive UX requirement).
#   5. Grant compose then grant send → 200 OK both calls.
#   6. /draft/generate writes a pending draft for fix-thread-102.
#   7. POST /drafts/{id}/approve {action:'send'} creates an approval row
#      with undo_window_seconds=30.
#   8. POST /drafts/{id}/send executes against the mock gmail.send seam,
#      draft.status=sent, gmail_message_id assigned, audit row lands.
#   9. Tamper-after-approve flow: edit body via raw SQLite then attempt
#      /send → 400 hash_mismatch.
#  10. send_agent recorded in agent_runs.
#  11. /audit_log chain_ok=true with `send` event present.
#  12. Approve-without-send-scope refuses (revoke send, retry approve →
#      403 send_not_permitted).
#
# All Gmail calls remain mocked under MAILMIND_GMAIL_MOCK=1 — no real
# network. The point of P4b is unlocking the *code path*, not flipping
# the user's actual Gmail account on.
#
# Usage: acceptance/p4b.sh

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

export MAILMIND_DATA_DIR="${MAILMIND_DATA_DIR:-$(mktemp -d -t mailmind-p4b)}"
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

step "agents: install + pytest (P0+P1+P2+P3+P4a+P4b suites)"
(cd agents && uv venv --quiet --python 3.12 .venv 2>/dev/null || true)
(cd agents && uv pip install --quiet -e ".[dev,agents]" >/dev/null)
(cd agents && source .venv/bin/activate && pytest -q) > "$MAILMIND_LOGS_DIR/agents-test.log" 2>&1 \
  || { tail -120 "$MAILMIND_LOGS_DIR/agents-test.log"; fail "agents tests failed"; }
ok "agents tests pass (P0+P1+P2+P3+P4a+P4b suites)"

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

step "fixtures: build phase-1 stubs"
(cd agents && source .venv/bin/activate && python ../fixtures/build_stub_responses.py "$MAILMIND_DATA_DIR/stub.json") \
  > "$MAILMIND_LOGS_DIR/stub-build-1.log" 2>&1 \
  || { tail -20 "$MAILMIND_LOGS_DIR/stub-build-1.log"; fail "phase-1 stub build failed"; }
export MAILMIND_STUB_RESPONSES="$MAILMIND_DATA_DIR/stub.json"
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

step "agents: start on :$AGENTS_PORT"
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

# ---------------- 5. phase-2 stub build (draft stubs) ----------
step "fixtures: rebuild stubs --include-draft"
(cd agents && source .venv/bin/activate && \
   python ../fixtures/build_stub_responses.py "$MAILMIND_DATA_DIR/stub.json" --include-draft) \
   > "$MAILMIND_LOGS_DIR/stub-build-2.log" 2>&1 \
   || { tail -30 "$MAILMIND_LOGS_DIR/stub-build-2.log"; fail "phase-2 stub build failed"; }
ok "stubs rebuilt with draft entries"

# ---------------- 6. permissions: send-without-compose refused -----
step "agents: POST /permissions {gmail_send:true} without compose → 400"
CODE=$(curl -s -o "$MAILMIND_LOGS_DIR/perm-400.json" -w "%{http_code}" \
  -X POST -H 'Content-Type: application/json' \
  -d '{"gmail_send":true}' "http://127.0.0.1:$AGENTS_PORT/permissions")
[[ "$CODE" == "400" ]] || fail "expected 400 send-without-compose, got $CODE"
python3 -c "
import json
d = json.load(open('$MAILMIND_LOGS_DIR/perm-400.json'))['detail']
assert d['code'] == 'compose_required', d
" || fail "wrong code on send-without-compose"
ok "/permissions refuses send without compose (additive UX)"

# ---------------- 7. grant compose, then grant send ----------------
step "agents: grant gmail.compose then gmail.send"
curl -sf -X POST -H 'Content-Type: application/json' \
  -d '{"gmail_compose":true}' "http://127.0.0.1:$AGENTS_PORT/permissions" >/dev/null \
  || fail "compose grant failed"

PERMS=$(curl -sf -X POST -H 'Content-Type: application/json' \
  -d '{"gmail_send":true}' "http://127.0.0.1:$AGENTS_PORT/permissions")
echo "$PERMS" | python3 -c "
import sys, json
p = json.loads(sys.stdin.read())
assert p['gmail.compose'] is True, p
assert p['gmail.send'] is True, p
" || fail "send grant didn't stick"
ok "gmail.compose + gmail.send both granted"

# ---------------- 8. generate draft ----------------
step "agents: POST /draft/generate"
GEN=$(curl -sf -X POST -H 'Content-Type: application/json' \
  -d '{"thread_id":"fix-thread-102","intent":"Reply to Adam Moore confirming the Thursday 2pm call"}' \
  "http://127.0.0.1:$AGENTS_PORT/draft/generate")
echo "$GEN" > "$MAILMIND_LOGS_DIR/draft-generate.json"
DRAFT_ID=$(echo "$GEN" | python3 -c "import sys,json; print(json.loads(sys.stdin.read())['draft_id'])")
[[ -n "$DRAFT_ID" ]] || fail "no draft_id"
ok "draft generated: $DRAFT_ID"

# ---------------- 9. approve action=send → 30s undo --------------
step "agents: POST /drafts/{id}/approve {action:'send'} → 30s undo"
AP=$(curl -sf -X POST -H 'Content-Type: application/json' \
  -d '{"action":"send"}' \
  "http://127.0.0.1:$AGENTS_PORT/drafts/$DRAFT_ID/approve")
echo "$AP" | python3 -c "
import sys, json
a = json.loads(sys.stdin.read())
assert a['action'] == 'send', a
assert a['undo_window_seconds'] == 30, a
" || fail "approve(send) shape wrong"
APPROVAL_ID=$(echo "$AP" | python3 -c "import sys,json; print(json.loads(sys.stdin.read())['approval_id'])")
ok "send approval created (undo_window_seconds=30)"

# ---------------- 10. execute send via /drafts/{id}/send ---------
step "agents: POST /drafts/{id}/send → mock gmail.send"
SEND=$(curl -sf -X POST -H 'Content-Type: application/json' \
  -d "{\"approval_id\":\"$APPROVAL_ID\"}" \
  "http://127.0.0.1:$AGENTS_PORT/drafts/$DRAFT_ID/send")
echo "$SEND" | python3 -c "
import sys, json
s = json.loads(sys.stdin.read())
assert s['next_status'] == 'sent', s
assert s['gmail_message_id'].startswith('msg-'), s
print(f\"sent gmail_message_id={s['gmail_message_id']}\")
" || fail "send response shape wrong"
ok "send executed against mock gmail.send seam"

# Verify draft row.
DD=$(curl -sf "http://127.0.0.1:$AGENTS_PORT/drafts/$DRAFT_ID")
echo "$DD" | python3 -c "
import sys, json
d = json.loads(sys.stdin.read())
assert d['status'] == 'sent', d
assert d['gmail_message_id'].startswith('msg-'), d
" || fail "draft.status didn't move to sent"
ok "draft.status=sent + gmail_message_id persisted"

# ---------------- 11. hash-mismatch on send ----------------------
step "agents: tamper after approve(send) → /send returns 400 hash_mismatch"
GEN2=$(curl -sf -X POST -H 'Content-Type: application/json' \
  -d '{"thread_id":"fix-thread-102","intent":"Reply to Adam Moore confirming the Thursday 2pm call"}' \
  "http://127.0.0.1:$AGENTS_PORT/draft/generate")
DRAFT_ID2=$(echo "$GEN2" | python3 -c "import sys,json; print(json.loads(sys.stdin.read())['draft_id'])")
AP2=$(curl -sf -X POST -H 'Content-Type: application/json' -d '{"action":"send"}' \
  "http://127.0.0.1:$AGENTS_PORT/drafts/$DRAFT_ID2/approve")
APPROVAL_ID2=$(echo "$AP2" | python3 -c "import sys,json; print(json.loads(sys.stdin.read())['approval_id'])")

# Tamper directly in SQLite (bypassing the API edit gate which would refuse
# post-approval edits). Simulates external write.
python3 - <<PY
import sqlite3, os
p = os.path.join(os.environ['MAILMIND_DATA_DIR'], 'db', 'derived.sqlite')
with sqlite3.connect(p) as c:
    c.execute("UPDATE drafts SET body = 'tampered', draft_hash = 'deadbeef' WHERE id = ?", ("$DRAFT_ID2",))
    c.commit()
PY

CODE=$(curl -s -o "$MAILMIND_LOGS_DIR/send-mismatch.json" -w "%{http_code}" \
  -X POST -H 'Content-Type: application/json' \
  -d "{\"approval_id\":\"$APPROVAL_ID2\"}" \
  "http://127.0.0.1:$AGENTS_PORT/drafts/$DRAFT_ID2/send")
[[ "$CODE" == "400" ]] || fail "expected 400 on hash mismatch, got $CODE"
python3 -c "
import json
d = json.load(open('$MAILMIND_LOGS_DIR/send-mismatch.json'))['detail']
assert d['code'] == 'hash_mismatch', d
" || fail "wrong error code on send hash-mismatch"
ok "/send refuses tampered draft with 400 hash_mismatch"

# ---------------- 12. send_agent recorded in agent_runs ----------
step "agents: GET /agent_runs?agent_name=send_agent"
RES=$(curl -sf "http://127.0.0.1:$AGENTS_PORT/agent_runs?agent_name=send_agent")
echo "$RES" | python3 -c "
import sys, json
runs = json.loads(sys.stdin.read())['runs']
assert any(r['result_status'] == 'success' for r in runs), runs
print(f'send_agent runs recorded: {len(runs)}')
" || fail "no successful send_agent run in agent_runs"
ok "send_agent invocation recorded in agent_runs"

# ---------------- 13. audit chain integrity + `send` event ------
step "agents: GET /audit_log — chain ok + send event present"
AL=$(curl -sf "http://127.0.0.1:$AGENTS_PORT/audit_log?limit=200")
echo "$AL" | python3 -c "
import sys, json
b = json.loads(sys.stdin.read())
assert b['chain_ok'] is True, b
events = [e['event_type'] for e in b['events']]
print(f'audit log: {events}')
assert 'send' in events, events
assert 'approve' in events, events
" || fail "audit chain or events missing"
ok "audit_log: chain ok, send + approve events present"

# ---------------- 14. revoke send → approve(send) refuses ------
step "agents: revoke send, retry approve(send) → 403"
curl -sf -X POST -H 'Content-Type: application/json' \
  -d '{"gmail_send":false}' \
  "http://127.0.0.1:$AGENTS_PORT/permissions" >/dev/null \
  || fail "revoke send failed"

GEN3=$(curl -sf -X POST -H 'Content-Type: application/json' \
  -d '{"thread_id":"fix-thread-102","intent":"Reply to Adam Moore confirming the Thursday 2pm call"}' \
  "http://127.0.0.1:$AGENTS_PORT/draft/generate")
DRAFT_ID3=$(echo "$GEN3" | python3 -c "import sys,json; print(json.loads(sys.stdin.read())['draft_id'])")

CODE=$(curl -s -o "$MAILMIND_LOGS_DIR/no-send-scope.json" -w "%{http_code}" \
  -X POST -H 'Content-Type: application/json' -d '{"action":"send"}' \
  "http://127.0.0.1:$AGENTS_PORT/drafts/$DRAFT_ID3/approve")
[[ "$CODE" == "403" ]] || fail "expected 403 after revoke, got $CODE"
python3 -c "
import json
d = json.load(open('$MAILMIND_LOGS_DIR/no-send-scope.json'))['detail']
assert d['code'] == 'send_not_permitted', d
" || fail "wrong code on no-send-scope"
ok "approve(send) refuses with 403 send_not_permitted after revoke"

# ---------------- summary ----------------
step "P4b acceptance: ALL GREEN"
echo "Logs: $MAILMIND_LOGS_DIR"
exit 0
