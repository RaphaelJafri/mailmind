"""P4b: send capability — gmail.send unlocked, send_agent + /drafts/{id}/send.

Same fixture pattern as P4a (test_p4_agents.py). The mock writer in
`gmail_writer.send` returns a deterministic gmail_message_id so the audit
chain stays stable. P4b unlocks the code path; the actual network send
remains mocked under MAILMIND_GMAIL_MOCK=1.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def isolated_stub_env(monkeypatch: pytest.MonkeyPatch) -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="mailmind-p4b-"))
    monkeypatch.setenv("MAILMIND_DATA_DIR", str(tmp))
    monkeypatch.setenv("GOOGLE_GENAI_API_KEY", "fake-key-for-stub")
    monkeypatch.setenv("MAILMIND_GMAIL_MOCK", "1")

    subprocess.run(
        ["node", "fixtures/load_fixtures.mjs"],
        cwd=REPO, check=True,
        env={**os.environ, "MAILMIND_DATA_DIR": str(tmp)},
        capture_output=True,
    )
    stub_path = tmp / "stub.json"
    subprocess.run(
        [sys.executable, "fixtures/build_stub_responses.py", str(stub_path)],
        cwd=REPO, check=True, capture_output=True,
    )
    monkeypatch.setenv("MAILMIND_STUB_RESPONSES", str(stub_path))
    return tmp


def _seed_pipeline(stub_path: Path) -> None:
    import extract_agent
    import reconcile
    import relationship_agent

    for tid in ("fix-thread-101", "fix-thread-102", "fix-thread-103"):
        extract_agent.run(tid)
    relationship_agent.run("morgan@northstar-talent.com")
    relationship_agent.run("adam@vectorlabs.io")
    reconcile.run()
    subprocess.run(
        [sys.executable, "fixtures/build_stub_responses.py", str(stub_path), "--include-draft"],
        cwd=REPO, check=True, capture_output=True,
    )


def _grant(compose: bool = True, send: bool = True) -> None:
    from lib import paths

    p = paths.config_dir() / "oauth_scopes.json"
    p.write_text(json.dumps({"gmail.compose": compose, "gmail.send": send}))


# ---------------- send_agent direct ------------------------------------

def test_send_agent_happy_path(isolated_stub_env: Path) -> None:
    _seed_pipeline(isolated_stub_env / "stub.json")
    _grant()
    import draft_agent
    import send_agent
    from lib import approval, db

    res = draft_agent.run("fix-thread-102", "Reply to Adam Moore confirming the Thursday 2pm call")
    ap = approval.approve(res["draft_id"], action="send", approved_by="raphael@test")
    assert ap["undo_window_seconds"] == 30  # P4b default

    out = send_agent.run(ap["approval_id"])
    assert out["next_status"] == "sent"
    assert out["gmail_message_id"].startswith("msg-")

    with db.derived() as conn:
        d = conn.execute(
            "SELECT status, gmail_message_id FROM drafts WHERE id = ?",
            (res["draft_id"],),
        ).fetchone()
    assert d["status"] == "sent"
    assert d["gmail_message_id"] == out["gmail_message_id"]


def test_send_agent_records_run(isolated_stub_env: Path) -> None:
    _seed_pipeline(isolated_stub_env / "stub.json")
    _grant()
    import draft_agent
    import send_agent
    from lib import approval, db

    res = draft_agent.run("fix-thread-102", "Reply to Adam Moore confirming the Thursday 2pm call")
    ap = approval.approve(res["draft_id"], action="send", approved_by="raphael@test")
    send_agent.run(ap["approval_id"])

    with db.agent_runs() as conn:
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT agent_name, result_status, tools_called_json FROM agent_runs "
                "WHERE agent_name = 'send_agent' ORDER BY started_at DESC LIMIT 1"
            ).fetchall()
        ]
    assert rows, "expected a send_agent row"
    row = rows[0]
    assert row["result_status"] == "success"
    tools = json.loads(row["tools_called_json"])
    assert tools[0]["tool"] == "gmail.send"


def test_send_refused_without_scope(isolated_stub_env: Path) -> None:
    _seed_pipeline(isolated_stub_env / "stub.json")
    _grant(compose=True, send=False)
    import draft_agent
    from lib import approval

    res = draft_agent.run("fix-thread-102", "Reply to Adam Moore confirming the Thursday 2pm call")
    with pytest.raises(approval.SendNotPermitted):
        approval.approve(res["draft_id"], action="send", approved_by="raphael@test")


def test_send_hash_mismatch(isolated_stub_env: Path) -> None:
    """Tamper the draft body after approve(action='send') and verify
    send_agent.run refuses with HashMismatch."""
    _seed_pipeline(isolated_stub_env / "stub.json")
    _grant()
    import draft_agent
    import send_agent
    from lib import approval, db

    res = draft_agent.run("fix-thread-102", "Reply to Adam Moore confirming the Thursday 2pm call")
    ap = approval.approve(res["draft_id"], action="send", approved_by="raphael@test")
    with db.derived() as conn:
        conn.execute(
            "UPDATE drafts SET body = ?, draft_hash = ? WHERE id = ?",
            ("Tampered.", "deadbeef" * 8, res["draft_id"]),
        )

    with pytest.raises(approval.HashMismatch):
        send_agent.run(ap["approval_id"])


def test_send_cancel_during_undo(isolated_stub_env: Path) -> None:
    """Cancel an in-flight send approval — draft drops back to pending and
    audit_log records the cancel."""
    _seed_pipeline(isolated_stub_env / "stub.json")
    _grant()
    import draft_agent
    from lib import approval, db

    res = draft_agent.run("fix-thread-102", "Reply to Adam Moore confirming the Thursday 2pm call")
    ap = approval.approve(res["draft_id"], action="send", approved_by="raphael@test")
    approval.cancel(ap["approval_id"], reason="changed mind")

    with db.derived() as conn:
        d = conn.execute(
            "SELECT status, approval_id FROM drafts WHERE id = ?",
            (res["draft_id"],),
        ).fetchone()
        a = conn.execute(
            "SELECT result_status FROM approvals WHERE id = ?",
            (ap["approval_id"],),
        ).fetchone()
    assert d["status"] == "pending"
    assert d["approval_id"] is None
    assert a["result_status"] == "cancelled"


def test_audit_chain_after_send(isolated_stub_env: Path) -> None:
    _seed_pipeline(isolated_stub_env / "stub.json")
    _grant()
    import draft_agent
    import send_agent
    from lib import approval

    res = draft_agent.run("fix-thread-102", "Reply to Adam Moore confirming the Thursday 2pm call")
    ap = approval.approve(res["draft_id"], action="send", approved_by="raphael@test")
    send_agent.run(ap["approval_id"])

    chain = approval.verify_audit_chain()
    assert chain["ok"], chain
    assert chain["total"] >= 2  # approve + send


# ---------------- service-layer toggles --------------------------------

def test_permissions_send_toggle_now_works(isolated_stub_env: Path) -> None:
    """The toggle that 403'd in P4a should succeed once compose is granted."""
    from fastapi.testclient import TestClient

    import service

    client = TestClient(service.app)
    # Grant compose first (P4b requires it).
    r1 = client.post("/permissions", json={"gmail_compose": True})
    assert r1.status_code == 200

    # Now grant send. P4a refused this with 403; P4b lets it through.
    r2 = client.post("/permissions", json={"gmail_send": True})
    assert r2.status_code == 200, r2.text
    body = r2.json()
    assert body["gmail.send"] is True


def test_permissions_send_without_compose_400(isolated_stub_env: Path) -> None:
    """Trying to grant gmail.send without first granting gmail.compose should
    be refused — additive UX."""
    from fastapi.testclient import TestClient

    import service

    client = TestClient(service.app)
    r = client.post("/permissions", json={"gmail_send": True})
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "compose_required"


def test_send_endpoint_full_flow(isolated_stub_env: Path) -> None:
    _seed_pipeline(isolated_stub_env / "stub.json")
    from fastapi.testclient import TestClient

    import service

    client = TestClient(service.app)

    # Generate.
    gen = client.post(
        "/draft/generate",
        json={"thread_id": "fix-thread-102", "intent": "Reply to Adam Moore confirming the Thursday 2pm call"},
    )
    assert gen.status_code == 200
    draft_id = gen.json()["draft_id"]

    # Grant compose + send.
    assert client.post("/permissions", json={"gmail_compose": True}).status_code == 200
    assert client.post("/permissions", json={"gmail_send": True}).status_code == 200

    # Approve action=send.
    ap = client.post(f"/drafts/{draft_id}/approve", json={"action": "send"})
    assert ap.status_code == 200
    approval_id = ap.json()["approval_id"]

    # Execute the send.
    sent = client.post(
        f"/drafts/{draft_id}/send", json={"approval_id": approval_id}
    )
    assert sent.status_code == 200, sent.text
    body = sent.json()
    assert body["next_status"] == "sent"
    assert body["gmail_message_id"].startswith("msg-")

    # Verify draft moved to sent.
    final = client.get(f"/drafts/{draft_id}")
    assert final.status_code == 200
    assert final.json()["status"] == "sent"


def test_send_endpoint_refuses_without_grant(isolated_stub_env: Path) -> None:
    _seed_pipeline(isolated_stub_env / "stub.json")
    from fastapi.testclient import TestClient

    import service

    client = TestClient(service.app)

    gen = client.post(
        "/draft/generate",
        json={"thread_id": "fix-thread-102", "intent": "Reply to Adam Moore confirming the Thursday 2pm call"},
    )
    draft_id = gen.json()["draft_id"]

    # Only compose granted, not send.
    client.post("/permissions", json={"gmail_compose": True})

    # Approve action=send is refused at the approve step (matches P4a behavior).
    ap = client.post(f"/drafts/{draft_id}/approve", json={"action": "send"})
    assert ap.status_code == 403
    assert ap.json()["detail"]["code"] == "send_not_permitted"


def test_send_endpoint_hash_mismatch(isolated_stub_env: Path) -> None:
    _seed_pipeline(isolated_stub_env / "stub.json")
    _grant()
    from fastapi.testclient import TestClient
    import draft_agent
    from lib import approval, db

    res = draft_agent.run(
        "fix-thread-102", "Reply to Adam Moore confirming the Thursday 2pm call"
    )
    ap = approval.approve(res["draft_id"], action="send", approved_by="raphael@test")

    # Tamper.
    with db.derived() as conn:
        conn.execute(
            "UPDATE drafts SET body = 'tampered', draft_hash = 'deadbeef' WHERE id = ?",
            (res["draft_id"],),
        )

    import service

    client = TestClient(service.app)
    out = client.post(
        f"/drafts/{res['draft_id']}/send",
        json={"approval_id": ap["approval_id"]},
    )
    assert out.status_code == 400
    assert out.json()["detail"]["code"] == "hash_mismatch"


# ---------------- send_agent.is_send_permitted helper -----------------

def test_is_send_permitted_helper(isolated_stub_env: Path) -> None:
    import send_agent

    assert send_agent.is_send_permitted() is False
    _grant(compose=True, send=False)
    assert send_agent.is_send_permitted() is False
    _grant(compose=True, send=True)
    assert send_agent.is_send_permitted() is True
