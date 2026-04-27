#!/usr/bin/env bash
# P3 acceptance: P2 surface + ReAct query agent + MCP server.
#
# Stub-Gemini only — the canned ReAct scripts in fixtures/stub_outputs.json
# (under "query:..." keys) are replayed deterministically via
# build_stub_responses.py --include-query.
#
# Two new surfaces this script verifies:
#   1. POST /query (SSE) — answers two canned questions within budget,
#      streams the expected tool calls, lands the right contact name in the
#      final answer.
#   2. mcp/server.py — speaks JSON-RPC over stdio, lists exactly the
#      documented tool set, all read-only.
#
# Usage: acceptance/p3.sh

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

export MAILMIND_DATA_DIR="${MAILMIND_DATA_DIR:-$(mktemp -d -t mailmind-p3)}"
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

step "agents: install + pytest (P0+P1+P2+P3)"
(cd agents && uv venv --quiet --python 3.12 .venv 2>/dev/null || true)
(cd agents && uv pip install --quiet -e ".[dev,agents]" >/dev/null)
(cd agents && source .venv/bin/activate && pytest -q) > "$MAILMIND_LOGS_DIR/agents-test.log" 2>&1 \
  || { tail -120 "$MAILMIND_LOGS_DIR/agents-test.log"; fail "agents tests failed"; }
ok "agents tests pass (P0+P1+P2+P3 suites)"

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

step "fixtures: build stub Gemini responses (phase 1 — no query stubs yet)"
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

step "agents: start on :$AGENTS_PORT (stub mode + skipped Gemini health)"
(cd agents && source .venv/bin/activate && \
   MAILMIND_HEALTH_SKIP_GEMINI=1 PORT=$AGENTS_PORT python service.py) \
  > "$MAILMIND_LOGS_DIR/agents.log" 2>&1 &
AGENTS_PID=$!
for _ in $(seq 1 40); do
  curl -sf "http://127.0.0.1:$AGENTS_PORT/health" >/dev/null && break
  sleep 0.25
done

# ---------------- 4. populate derived.sqlite (P2 pipeline) ---------------
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

# ---------------- 5. phase-2 stub build (now with query stubs) ----------
step "fixtures: rebuild stubs --include-query (with populated derived.sqlite)"
(cd agents && source .venv/bin/activate && \
   python ../fixtures/build_stub_responses.py "$MAILMIND_DATA_DIR/stub.json" --include-query) \
   > "$MAILMIND_LOGS_DIR/stub-build-2.log" 2>&1 \
   || { tail -30 "$MAILMIND_LOGS_DIR/stub-build-2.log"; fail "phase-2 stub build failed"; }
STUB_COUNT=$(python3 -c "import json; print(len(json.load(open('$MAILMIND_DATA_DIR/stub.json'))))")
[[ "$STUB_COUNT" -ge 14 ]] || fail "expected ≥14 stub entries, got $STUB_COUNT"
ok "stubs rebuilt with query entries (count=$STUB_COUNT)"

# ---------------- 6. /query/tools manifest -------------------------------
RES=$(curl -sf "http://127.0.0.1:$AGENTS_PORT/query/tools")
COUNT=$(echo "$RES" | python3 -c "import sys,json; print(json.loads(sys.stdin.read())['count'])")
[[ "$COUNT" == "6" ]] || fail "expected 6 tools, got $COUNT: $RES"
echo "$RES" | python3 -c "
import sys, json
names = {t['name'] for t in json.loads(sys.stdin.read())['tools']}
expected = {'search_contacts','get_rollup','list_overdue','get_thread','get_pending_drafts','list_threads_by_tag'}
assert names == expected, f'tools mismatch: {names} vs {expected}'
print('manifest verified')
" || fail "tool manifest mismatch"
ok "/query/tools returned 6 read-only tools"

# ---------------- 7. POST /query — Q1: who am I ghosting? ---------------
step "agents: POST /query 'who am I ghosting?' (SSE stream)"
curl -sf -N -X POST -H 'Content-Type: application/json' \
  -d '{"question":"who am I ghosting?"}' \
  "http://127.0.0.1:$AGENTS_PORT/query" > "$MAILMIND_LOGS_DIR/query-q1.sse" \
  || fail "/query Q1 failed"

python3 - <<'PY' "$MAILMIND_LOGS_DIR/query-q1.sse" || fail "Q1 verification failed"
import json, sys, pathlib
events = []
for line in pathlib.Path(sys.argv[1]).read_text().splitlines():
    if line.startswith('data: '):
        events.append(json.loads(line[6:]))
kinds = [e['kind'] for e in events]
assert kinds[0] == 'start', kinds
assert kinds[-1] == 'done', kinds
tool_calls = [e for e in events if e['kind'] == 'tool_call']
assert [t['tool'] for t in tool_calls] == ['list_overdue'], tool_calls
answer = next(e for e in events if e['kind'] == 'answer')
assert 'Adam' in answer['answer'], answer
done = next(e for e in events if e['kind'] == 'done')
assert done['stubbed'] is True, done
assert done['truncated'] is False, done
assert done['cost_usd'] < 0.10, done
print(f'Q1 OK: 1 tool call, answer mentions Adam, cost=${done["cost_usd"]:.4f}')
PY
ok "Q1 'who am I ghosting?' streamed correctly"

# ---------------- 8. POST /query — Q2: pending with my recruiter? -------
step "agents: POST /query 'what's pending with my recruiter?'"
curl -sf -N -X POST -H 'Content-Type: application/json' \
  -d '{"question":"what'\''s pending with my recruiter?"}' \
  "http://127.0.0.1:$AGENTS_PORT/query" > "$MAILMIND_LOGS_DIR/query-q2.sse" \
  || fail "/query Q2 failed"

python3 - <<'PY' "$MAILMIND_LOGS_DIR/query-q2.sse" || fail "Q2 verification failed"
import json, sys, pathlib
events = []
for line in pathlib.Path(sys.argv[1]).read_text().splitlines():
    if line.startswith('data: '):
        events.append(json.loads(line[6:]))
tools = [e['tool'] for e in events if e['kind'] == 'tool_call']
assert tools == ['search_contacts', 'get_rollup'], tools
answer = next(e for e in events if e['kind'] == 'answer')
assert 'Morgan' in answer['answer'], answer
done = next(e for e in events if e['kind'] == 'done')
assert done['tool_calls'] == 2, done
assert done['truncated'] is False, done
print(f'Q2 OK: 2 tool calls, answer mentions Morgan, wall={done["wall_ms"]}ms')
PY
ok "Q2 'pending with recruiter' streamed correctly"

# ---------------- 9. /agent_runs reflects the query runs ----------------
RES=$(curl -sf "http://127.0.0.1:$AGENTS_PORT/agent_runs?agent_name=query_agent")
QC=$(echo "$RES" | python3 -c "import sys,json; print(len(json.loads(sys.stdin.read())['runs']))")
[[ "$QC" == "2" ]] || fail "expected 2 query_agent runs, got $QC"
echo "$RES" | python3 -c "
import sys, json
runs = json.loads(sys.stdin.read())['runs']
for r in runs:
    assert r['result_status'] == 'success', r
    tools = json.loads(r['tools_called_json']) if r['tools_called_json'] else []
    assert 1 <= len(tools) <= 8, tools
print(f'{len(runs)} query_agent runs recorded, all under tool-call cap')
" || fail "query_agent run validation failed"
ok "/agent_runs shows 2 successful query_agent runs within budget"

# ---------------- 10. MCP server smoke (stdio JSON-RPC) -----------------
step "mcp: stdio smoke (initialize + tools/list + tools/call)"
MCP_LOG="$MAILMIND_LOGS_DIR/mcp.log"
MCP_OUT=$(printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}' \
  '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"search_contacts","arguments":{"query":""}}}' \
  | (cd "$ROOT" && MAILMIND_DATA_DIR="$MAILMIND_DATA_DIR" MAILMIND_MCP_LOG="$MCP_LOG" \
       agents/.venv/bin/python mcp/server.py))

echo "$MCP_OUT" | python3 -c "
import sys, json
lines = [json.loads(l) for l in sys.stdin.read().splitlines() if l.strip()]
assert len(lines) == 3, f'expected 3 responses, got {len(lines)}: {lines}'
assert lines[0]['result']['serverInfo']['name'] == 'mailmind', lines[0]
tools = {t['name'] for t in lines[1]['result']['tools']}
expected = {'search_contacts','get_rollup','list_overdue','get_thread','get_pending_drafts','list_threads_by_tag'}
assert tools == expected, f'tools mismatch: {tools}'
content = json.loads(lines[2]['result']['content'][0]['text'])
assert isinstance(content, list), content
assert any(r['contact_email'] == 'morgan@northstar-talent.com' for r in content), content
print('MCP stdio responded with 3 valid JSON-RPC frames')
" || fail "mcp/server.py wire format failed"
ok "mcp/server.py speaks JSON-RPC and dispatches read-only tools"

# ---------------- 11. MCP refuses unknown / write-style tools -----------
MCP_OUT2=$(printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"send_email","arguments":{"to":"x@y.com","body":""}}}' \
  | (cd "$ROOT" && MAILMIND_DATA_DIR="$MAILMIND_DATA_DIR" \
       agents/.venv/bin/python mcp/server.py))
echo "$MCP_OUT2" | python3 -c "
import sys, json
[resp] = [json.loads(l) for l in sys.stdin.read().splitlines() if l.strip()]
assert 'error' in resp, resp
assert 'unknown tool' in resp['error']['message'], resp
print('write-style tool name rejected')
" || fail "mcp didn't reject send_email as unknown tool"
ok "mcp/server.py rejects unknown / write tools"

# ---------------- 12. /query rejects empty question ---------------------
step "agents: POST /query empty question → 400"
CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST -H 'Content-Type: application/json' \
  -d '{"question":""}' "http://127.0.0.1:$AGENTS_PORT/query")
[[ "$CODE" == "400" ]] || fail "expected 400 for empty question, got $CODE"
ok "/query rejects empty question with 400"

# ---------------- summary ----------------
step "P3 acceptance: ALL GREEN"
echo "Logs: $MAILMIND_LOGS_DIR"
exit 0
