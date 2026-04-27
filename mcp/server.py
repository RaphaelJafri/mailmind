"""mailmind MCP server — stdio JSON-RPC 2.0.

Exposes the same six read-only tools as `agents/lib/query_tools.py` so external
clients (Claude Code, Cursor, ChatGPT desktop) can consume the mailmind
knowledge graph directly. Read-only by design — no `send`, no `archive`, no
`approve`. P4's write-scope flow lives in `agents/service.py` behind explicit
user consent and is **not** surfaced over MCP.

Transport is stdin → JSON-RPC request lines, stdout → JSON-RPC response
lines. We avoid the official `mcp` Python SDK to keep the dep tree tight; the
wire format here is the MCP Streamable-stdio subset that Claude Desktop /
Cursor / Claude Code all speak.

Run manually:

    cd <repo>/agents && source .venv/bin/activate \
        && MAILMIND_DATA_DIR=/path/to/data python ../mcp/server.py

…or wire it up in a client config (see `mcp/README.md`).
"""

from __future__ import annotations

import json
import logging
import os
import sys
import traceback
from pathlib import Path
from typing import Any

# Allow `python mcp/server.py` to import from agents/ without needing
# `pip install -e ./agents`. This mirrors what fixtures/build_stub_responses
# does so the dev loop is consistent.
REPO = Path(__file__).resolve().parents[1]
AGENTS = REPO / "agents"
if str(AGENTS) not in sys.path:
    sys.path.insert(0, str(AGENTS))

from lib import query_tools  # noqa: E402

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "mailmind"
SERVER_VERSION = "0.3.0"

log = logging.getLogger("mailmind.mcp")


# ---- JSON-RPC helpers -----------------------------------------------------

def _resp(id_: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": id_, "result": result}


def _err(id_: Any, code: int, message: str, data: Any = None) -> dict:
    out: dict[str, Any] = {
        "jsonrpc": "2.0",
        "id": id_,
        "error": {"code": code, "message": message},
    }
    if data is not None:
        out["error"]["data"] = data
    return out


# ---- request handlers -----------------------------------------------------

def _handle_initialize(_params: dict) -> dict:
    return {
        "protocolVersion": PROTOCOL_VERSION,
        "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        "capabilities": {
            "tools": {"listChanged": False},
        },
    }


def _handle_tools_list(_params: dict) -> dict:
    return {"tools": query_tools.manifest()}


def _handle_tools_call(params: dict) -> dict:
    name = params.get("name")
    args = params.get("arguments") or {}
    if not isinstance(name, str) or name not in query_tools.TOOLS_BY_NAME:
        raise McpError(-32602, f"unknown tool: {name!r}")
    if not isinstance(args, dict):
        raise McpError(-32602, "tool arguments must be an object")

    try:
        result = query_tools.call_tool(name, args)
    except TypeError as exc:
        raise McpError(-32602, f"bad arguments for {name}: {exc}") from exc
    except Exception as exc:  # noqa: BLE001
        # MCP wants tool errors as `result` content with `isError=true`, not
        # transport errors. That way the client can show the failure to the
        # model without aborting the session.
        return {
            "isError": True,
            "content": [
                {"type": "text", "text": f"{type(exc).__name__}: {exc}"}
            ],
        }

    return {
        "content": [
            {"type": "text", "text": json.dumps(result, default=str, indent=2)}
        ],
    }


HANDLERS: dict[str, Any] = {
    "initialize": _handle_initialize,
    "initialized": None,           # client → server notification, no response
    "notifications/initialized": None,
    "tools/list": _handle_tools_list,
    "tools/call": _handle_tools_call,
    "ping": lambda _p: {},
}


class McpError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# ---- main loop ------------------------------------------------------------

def serve(stdin: Any = sys.stdin, stdout: Any = sys.stdout) -> None:
    """Read JSON-RPC frames from stdin, dispatch, write to stdout. One JSON
    object per line — newline-delimited (NDJSON) is the simplest framing
    Claude Desktop / Cursor / Claude Code all accept."""
    log.info(
        "mailmind MCP server starting — data dir: %s",
        os.environ.get("MAILMIND_DATA_DIR", "(default)"),
    )

    for raw_line in stdin:
        line = raw_line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError as exc:
            _emit(stdout, _err(None, -32700, f"parse error: {exc}"))
            continue

        method = req.get("method")
        params = req.get("params") or {}
        rid = req.get("id")

        if method not in HANDLERS:
            if rid is not None:
                _emit(stdout, _err(rid, -32601, f"method not found: {method!r}"))
            continue

        handler = HANDLERS[method]
        if handler is None:
            # Notification — no response.
            continue

        try:
            result = handler(params)
        except McpError as exc:
            _emit(stdout, _err(rid, exc.code, exc.message))
            continue
        except Exception as exc:  # noqa: BLE001
            log.error("handler error", exc_info=True)
            _emit(
                stdout,
                _err(rid, -32603, f"internal error: {type(exc).__name__}: {exc}",
                     data={"trace": traceback.format_exc()}),
            )
            continue

        if rid is not None:
            _emit(stdout, _resp(rid, result))


def _emit(out: Any, payload: dict) -> None:
    out.write(json.dumps(payload) + "\n")
    out.flush()


def main() -> None:
    log_path = os.environ.get("MAILMIND_MCP_LOG")
    handlers: list[logging.Handler] = []
    if log_path:
        handlers.append(logging.FileHandler(log_path))
    else:
        handlers.append(logging.StreamHandler(sys.stderr))
    logging.basicConfig(level=logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")

    serve()


if __name__ == "__main__":
    main()
