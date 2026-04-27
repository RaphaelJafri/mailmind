"""End-to-end stub tests for the P3 query agent + MCP server.

Same fixture pattern as `test_p2_agents.py` — fresh tmp data dir, seeded raw
DB, stub Gemini responses keyed by hash. The query stubs require derived.sqlite
to be populated *before* they can be built deterministically (the prompt at
each ReAct step contains the previous step's tool result), so each test calls
`_seed_pipeline()` to walk extract → relationship → reconcile, then rebuilds
the stub file with `--include-query`.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


def _load_mcp_server():
    """Load `<repo>/mcp/server.py` as a module by path.

    The `mcp/` directory is intentionally not a Python package — its name
    clashes with the installed Anthropic MCP SDK (`pip install mcp`), so
    `from mcp import server` resolves to the SDK, not our local file. Loading
    by path side-steps the collision.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "mailmind_mcp_server", REPO / "mcp" / "server.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def isolated_stub_env(monkeypatch: pytest.MonkeyPatch) -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="mailmind-p3-"))
    monkeypatch.setenv("MAILMIND_DATA_DIR", str(tmp))
    monkeypatch.setenv("GOOGLE_GENAI_API_KEY", "fake-key-for-stub")

    subprocess.run(
        ["node", "fixtures/load_fixtures.mjs"],
        cwd=REPO,
        check=True,
        env={**os.environ, "MAILMIND_DATA_DIR": str(tmp)},
        capture_output=True,
    )

    stub_path = tmp / "stub.json"
    subprocess.run(
        [sys.executable, "fixtures/build_stub_responses.py", str(stub_path)],
        cwd=REPO,
        check=True,
        capture_output=True,
    )
    monkeypatch.setenv("MAILMIND_STUB_RESPONSES", str(stub_path))
    return tmp


def _seed_pipeline(stub_path: Path) -> None:
    """Run extract → relationship → reconcile so derived.sqlite has rollups +
    pending next_steps for the query agent to read. Then rebuild the stub
    file with `--include-query` so query-agent prompts hash-match."""
    import extract_agent
    import reconcile
    import relationship_agent

    for tid in ("fix-thread-101", "fix-thread-102", "fix-thread-103"):
        extract_agent.run(tid)
    relationship_agent.run("morgan@northstar-talent.com")
    relationship_agent.run("adam@vectorlabs.io")
    reconcile.run()

    subprocess.run(
        [sys.executable, "fixtures/build_stub_responses.py", str(stub_path), "--include-query"],
        cwd=REPO,
        check=True,
        capture_output=True,
    )


# ---------------- query_tools (read-only sandbox) -------------------------

def test_query_tools_search_contacts(isolated_stub_env: Path) -> None:
    _seed_pipeline(isolated_stub_env / "stub.json")
    from lib import query_tools

    rows = query_tools.search_contacts("recruiter")
    assert any(r["contact_email"] == "morgan@northstar-talent.com" for r in rows)
    # tag-only match (no substring overlap with the query): vector-labs tag
    # appears on Adam's rollup; query "vector" hits the relationship_summary.
    rows2 = query_tools.search_contacts("vector")
    assert any(r["contact_email"] == "adam@vectorlabs.io" for r in rows2)


def test_query_tools_get_rollup(isolated_stub_env: Path) -> None:
    _seed_pipeline(isolated_stub_env / "stub.json")
    from lib import query_tools

    morgan = query_tools.get_rollup("morgan@northstar-talent.com")
    assert morgan is not None
    assert morgan["status"] == "awaiting_them"
    assert len(morgan["pending_next_steps"]) == 1
    assert query_tools.get_rollup("nobody@nowhere.test") is None


def test_query_tools_list_overdue(isolated_stub_env: Path) -> None:
    _seed_pipeline(isolated_stub_env / "stub.json")
    from lib import query_tools

    report = query_tools.list_overdue()
    you_owe = {(e["contact_email"], e["urgency"]) for e in report["you_owe_them"]}
    they_owe = {(e["contact_email"], e["urgency"]) for e in report["they_owe_you"]}
    assert ("adam@vectorlabs.io", "overdue") in you_owe
    assert ("morgan@northstar-talent.com", "overdue") in they_owe


def test_query_tools_pending_drafts(isolated_stub_env: Path) -> None:
    _seed_pipeline(isolated_stub_env / "stub.json")
    from lib import query_tools

    rows = query_tools.get_pending_drafts()
    assert {r["contact_email"] for r in rows} == {
        "morgan@northstar-talent.com",
        "adam@vectorlabs.io",
    }


def test_query_tools_manifest_shape() -> None:
    from lib import query_tools

    names = {t["name"] for t in query_tools.manifest()}
    expected = {
        "search_contacts",
        "get_rollup",
        "list_overdue",
        "get_thread",
        "get_pending_drafts",
        "list_threads_by_tag",
    }
    assert names == expected


# ---------------- query agent ReAct loop ----------------------------------

def test_query_agent_who_am_i_ghosting(isolated_stub_env: Path) -> None:
    _seed_pipeline(isolated_stub_env / "stub.json")
    import query_agent

    result = query_agent.run_collected("who am I ghosting?")
    assert result["answer"], result
    assert "Adam" in result["answer"]
    assert result["truncated"] is False
    tool_names = [tc["tool"] for tc in result["tool_calls"]]
    assert tool_names == ["list_overdue"], tool_names
    assert result["stats"]["stubbed"] is True


def test_query_agent_pending_with_recruiter(isolated_stub_env: Path) -> None:
    _seed_pipeline(isolated_stub_env / "stub.json")
    import query_agent

    result = query_agent.run_collected("what's pending with my recruiter?")
    assert result["answer"], result
    assert "Morgan" in result["answer"]
    tool_names = [tc["tool"] for tc in result["tool_calls"]]
    assert tool_names == ["search_contacts", "get_rollup"], tool_names


def test_query_agent_enforces_tool_call_cap(isolated_stub_env: Path) -> None:
    """Drop the cap to 1 — the recruiter question wants 2 tool calls so the
    driver must truncate and synthesize a fallback answer.
    """
    _seed_pipeline(isolated_stub_env / "stub.json")
    import query_agent

    result = query_agent.run_collected(
        "what's pending with my recruiter?", max_tool_calls=1
    )
    assert result["truncated"] is True
    assert result["reason"] == "tool_call_cap"
    assert result["answer"]  # always emits something


def test_query_agent_records_agent_run(isolated_stub_env: Path) -> None:
    _seed_pipeline(isolated_stub_env / "stub.json")
    import query_agent
    from lib import db

    query_agent.run_collected("who am I ghosting?")

    with db.agent_runs() as conn:
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT agent_name, result_status, tools_called_json, stubbed "
                "FROM agent_runs WHERE agent_name = 'query_agent' "
                "ORDER BY started_at DESC LIMIT 1"
            ).fetchall()
        ]
    assert rows, "expected a query_agent row in agent_runs"
    row = rows[0]
    assert row["result_status"] == "success"
    tools = json.loads(row["tools_called_json"]) if row["tools_called_json"] else []
    assert tools[0]["tool"] == "list_overdue"
    assert row["stubbed"] == 1


# ---------------- service endpoints (smoke) -------------------------------

def test_query_tools_endpoint(isolated_stub_env: Path) -> None:
    _seed_pipeline(isolated_stub_env / "stub.json")
    from fastapi.testclient import TestClient

    import service

    client = TestClient(service.app)
    res = client.get("/query/tools")
    assert res.status_code == 200
    body = res.json()
    assert body["count"] == 6
    assert {t["name"] for t in body["tools"]} >= {"search_contacts", "list_overdue"}


def test_query_endpoint_streams_answer(isolated_stub_env: Path) -> None:
    _seed_pipeline(isolated_stub_env / "stub.json")
    from fastapi.testclient import TestClient

    import service

    client = TestClient(service.app)
    with client.stream(
        "POST", "/query", json={"question": "who am I ghosting?"}
    ) as res:
        assert res.status_code == 200
        events: list[dict] = []
        for chunk in res.iter_text():
            for line in chunk.splitlines():
                if line.startswith("data: "):
                    events.append(json.loads(line[len("data: "):]))
    kinds = [e["kind"] for e in events]
    assert "answer" in kinds
    assert kinds[-1] == "done"
    answer = next(e for e in events if e["kind"] == "answer")
    assert "Adam" in answer["answer"]


# ---------------- MCP server (in-process stdio) ---------------------------

def test_mcp_server_dispatches_tools(isolated_stub_env: Path) -> None:
    """Drive `mcp.server.serve()` over fake stdin/stdout buffers. Verifies
    initialize → tools/list → tools/call all return correct JSON-RPC."""
    _seed_pipeline(isolated_stub_env / "stub.json")

    mcp_server = _load_mcp_server()

    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "search_contacts", "arguments": {"query": "recruiter"}},
        },
    ]
    stdin = io.StringIO("\n".join(json.dumps(r) for r in requests) + "\n")
    stdout = io.StringIO()
    mcp_server.serve(stdin=stdin, stdout=stdout)

    lines = [json.loads(l) for l in stdout.getvalue().splitlines() if l.strip()]
    assert len(lines) == 3
    assert lines[0]["result"]["serverInfo"]["name"] == "mailmind"
    tool_names = [t["name"] for t in lines[1]["result"]["tools"]]
    assert "search_contacts" in tool_names
    assert "get_rollup" in tool_names
    # tools/call returns text content with JSON inside.
    content = lines[2]["result"]["content"]
    payload = json.loads(content[0]["text"])
    assert any(r["contact_email"] == "morgan@northstar-talent.com" for r in payload)


def test_mcp_server_rejects_write_tool(isolated_stub_env: Path) -> None:
    mcp_server = _load_mcp_server()

    req = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "send_email", "arguments": {"to": "x@y.com", "body": ""}},
    }
    stdin = io.StringIO(json.dumps(req) + "\n")
    stdout = io.StringIO()
    mcp_server.serve(stdin=stdin, stdout=stdout)
    [resp] = [json.loads(l) for l in stdout.getvalue().splitlines() if l.strip()]
    assert "error" in resp
    assert "unknown tool" in resp["error"]["message"]
