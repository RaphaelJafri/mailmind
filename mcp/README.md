# mailmind MCP server

Read-only access to the mailmind knowledge graph (rolled-up contacts, extracted
thread facts, cadence/follow-ups, message tags, pending next-steps) over the
[Model Context Protocol](https://modelcontextprotocol.io). Spawned on demand
by stdio — no daemon, no port, no extra surface to secure.

## Tools exposed

All six are read-only mirrors of `agents/lib/query_tools.py`:

| Tool | What it returns |
|---|---|
| `search_contacts(query?, limit?)` | Substring + tag match across rolled-up contacts |
| `get_rollup(email)` | Full ContactRollup + pending next-steps for one contact |
| `list_overdue(window_days?)` | Cadence report filtered to `urgency ∈ {overdue, cold}` |
| `get_thread(thread_id)` | Thread metadata + extracted facts (commitments, open_questions, deadlines) |
| `get_pending_drafts(limit?)` | All `next_steps` rows in `pending` status |
| `list_threads_by_tag(tag_kind, tag_value, limit?)` | Threads with ≥1 message tagged that way |

There are **no write tools.** P4's draft / send / approve flow lives behind an
explicit Gmail write-scope OAuth grant and is not surfaced over MCP — that
boundary is intentional.

## Running

```bash
cd ~/Desktop/mailmind/agents
source .venv/bin/activate
MAILMIND_DATA_DIR=~/Library/Application\ Support/mailmind \
    python ../mcp/server.py
```

Server speaks NDJSON-framed JSON-RPC 2.0 on stdin/stdout. One JSON object per
line; the client writes a request, the server writes back a response. Errors
go to stderr (or `MAILMIND_MCP_LOG=<path>`).

## Wiring it up

### Claude Desktop / Claude Code

Add to `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "mailmind": {
      "command": "/Users/<you>/Desktop/mailmind/agents/.venv/bin/python",
      "args": ["/Users/<you>/Desktop/mailmind/mcp/server.py"],
      "env": {
        "MAILMIND_DATA_DIR": "/Users/<you>/Library/Application Support/mailmind"
      }
    }
  }
}
```

### Cursor

`~/.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "mailmind": {
      "command": "/Users/<you>/Desktop/mailmind/agents/.venv/bin/python",
      "args": ["/Users/<you>/Desktop/mailmind/mcp/server.py"]
    }
  }
}
```

### ChatGPT desktop (custom connector)

ChatGPT desktop's MCP support reads the same NDJSON stdio frames. Point it at
the same command + args.

## Smoke test

The MCP server boots without dependencies on the FastAPI sidecar — it reads
`derived.sqlite` and `raw.sqlite` directly. To confirm it speaks correctly:

```bash
printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}' \
  '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"search_contacts","arguments":{"query":""}}}' \
| python mcp/server.py
```

Expected: three lines on stdout, one per request, each a `{"jsonrpc":"2.0","id":...,"result":{...}}`.

## Safety

- **No mutation tools.** Every handler is a SELECT.
- **No raw email bodies.** Tools return extracted facts and rollups only —
  bodies stay inside `raw.sqlite` and are never serialized over MCP.
- **No network.** The server doesn't talk to Vertex / Gmail. It's a pure
  read-through to the local SQLite databases.
- **No credentials passed.** No tokens, no OAuth refresh secrets, no GCP
  service-account JSON ever appears in a tool response.
