"""End-to-end stub tests for the P4a draft agent + approval gate.

Pattern matches test_p3_agents.py: tmp data dir, fixtures loaded via the Node
loader, two-phase stub build (`--include-draft` requires derived.sqlite to be
populated first). The mock gmail.compose seam is exercised via
`MAILMIND_GMAIL_MOCK=1`, which the autouse fixture sets — no network calls.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def isolated_stub_env(monkeypatch: pytest.MonkeyPatch) -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="mailmind-p4-"))
    monkeypatch.setenv("MAILMIND_DATA_DIR", str(tmp))
    monkeypatch.setenv("GOOGLE_GENAI_API_KEY", "fake-key-for-stub")
    # Use the in-process mock for save_as_draft. No HTTP, deterministic
    # gmail_draft_id derived from sha256(thread_id+body).
    monkeypatch.setenv("MAILMIND_GMAIL_MOCK", "1")

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
    """Run extract → relationship → reconcile so the draft agent's prompt
    payload (which embeds the thread facts + rollup) matches what we hash
    into the stub file. Then rebuild stubs with `--include-draft`."""
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
        cwd=REPO,
        check=True,
        capture_output=True,
    )


def _grant_compose() -> None:
    """Write the OAuth-scope grants file the approval gate reads. Without
    this, every approve() call in P4a refuses with SendNotPermitted."""
    from lib import paths

    p = paths.config_dir() / "oauth_scopes.json"
    p.write_text(json.dumps({"gmail.compose": True, "gmail.send": False}))


# ---------------- schema + hashing primitives --------------------------

def test_canonicalize_is_stable_across_whitespace() -> None:
    from lib import approval

    a = {
        "to_emails": ["adam@vectorlabs.io"],
        "cc_emails": [],
        "bcc_emails": [],
        "subject": " Re: Quick call this week? ",
        "body": "Thursday 2pm works.\n\n",
    }
    b = {
        "to_emails": ["ADAM@VectorLabs.io"],
        "cc_emails": [],
        "bcc_emails": [],
        "subject": "Re: Quick call this week?",
        "body": "Thursday 2pm works.",
    }
    assert approval.hash_draft(a) == approval.hash_draft(b)


def test_hash_changes_when_body_changes() -> None:
    from lib import approval

    a = {"to_emails": ["x@y.com"], "subject": "S", "body": "v1"}
    b = {"to_emails": ["x@y.com"], "subject": "S", "body": "v2"}
    assert approval.hash_draft(a) != approval.hash_draft(b)


# ---------------- draft generation --------------------------------------

def test_generate_draft_writes_pending_row(isolated_stub_env: Path) -> None:
    _seed_pipeline(isolated_stub_env / "stub.json")
    import draft_agent
    from lib import db

    res = draft_agent.run(
        "fix-thread-102",
        "Reply to Adam Moore confirming the Thursday 2pm call",
    )
    assert res["stubbed"] is True
    assert res["draft"]["confidence"] == "high"
    assert "Thursday" in res["draft"]["body"]
    assert res["draft"]["thread_id"] == "fix-thread-102"
    assert any(
        f["fact_id"] == "thursday_2pm_proposal"
        for f in res["draft"]["cited_facts"]
    )

    with db.derived() as conn:
        rows = conn.execute(
            "SELECT * FROM drafts WHERE id = ?", (res["draft_id"],)
        ).fetchall()
    assert len(rows) == 1
    row = dict(rows[0])
    assert row["status"] == "pending"
    assert row["draft_hash"] == res["draft_hash"]
    assert row["intent"] == "Reply to Adam Moore confirming the Thursday 2pm call"


def test_generate_draft_records_agent_run(isolated_stub_env: Path) -> None:
    _seed_pipeline(isolated_stub_env / "stub.json")
    import draft_agent
    from lib import db

    draft_agent.run("fix-thread-102", "Reply to Adam Moore confirming the Thursday 2pm call")

    with db.agent_runs() as conn:
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT agent_name, result_status, stubbed FROM agent_runs "
                "WHERE agent_name = 'draft_agent' "
                "ORDER BY started_at DESC LIMIT 1"
            ).fetchall()
        ]
    assert rows and rows[0]["result_status"] == "success"
    assert rows[0]["stubbed"] == 1


def test_generate_refuses_unknown_thread(isolated_stub_env: Path) -> None:
    _seed_pipeline(isolated_stub_env / "stub.json")
    import draft_agent

    with pytest.raises(LookupError):
        draft_agent.run("does-not-exist", "intent")


# ---------------- edits + hash recompute -------------------------------

def test_edit_recomputes_hash(isolated_stub_env: Path) -> None:
    _seed_pipeline(isolated_stub_env / "stub.json")
    import draft_agent

    res = draft_agent.run(
        "fix-thread-102",
        "Reply to Adam Moore confirming the Thursday 2pm call",
    )
    original_hash = res["draft_hash"]

    edited = draft_agent.update_draft_body(res["draft_id"], body="Different body now.\n")
    assert edited["draft_hash"] != original_hash
    # Update is idempotent — calling with the same edit produces the same hash.
    edited2 = draft_agent.update_draft_body(res["draft_id"], body="Different body now.\n")
    assert edited2["draft_hash"] == edited["draft_hash"]


def test_edit_refuses_when_not_pending(isolated_stub_env: Path) -> None:
    _seed_pipeline(isolated_stub_env / "stub.json")
    _grant_compose()
    import draft_agent
    from lib import approval

    res = draft_agent.run("fix-thread-102", "Reply to Adam Moore confirming the Thursday 2pm call")
    approval.approve(res["draft_id"], action="save_as_draft", approved_by="raphael@test")

    with pytest.raises(draft_agent.DraftEditError):
        draft_agent.update_draft_body(res["draft_id"], body="late edit")


# ---------------- approval gate ----------------------------------------

def test_approve_save_happy_path(isolated_stub_env: Path) -> None:
    _seed_pipeline(isolated_stub_env / "stub.json")
    _grant_compose()
    import draft_agent
    from lib import approval, db, gmail_writer

    res = draft_agent.run(
        "fix-thread-102",
        "Reply to Adam Moore confirming the Thursday 2pm call",
    )
    ap = approval.approve(res["draft_id"], action="save_as_draft", approved_by="raphael@test")
    assert ap["action"] == "save_as_draft"
    assert ap["undo_window_seconds"] == 10

    # Skip the wall-clock undo window (the driver waits client-side).
    out = approval.execute_save_as_draft(ap["approval_id"], gmail_writer=gmail_writer.save_as_draft)
    assert out["next_status"] == "saved_as_draft"
    assert out["gmail_draft_id"].startswith("draft-")

    with db.derived() as conn:
        d = conn.execute("SELECT status, gmail_draft_id FROM drafts WHERE id = ?", (res["draft_id"],)).fetchone()
        ap_row = conn.execute("SELECT result_status, executed_at FROM approvals WHERE id = ?", (ap["approval_id"],)).fetchone()
    assert d["status"] == "saved_as_draft"
    assert d["gmail_draft_id"] == out["gmail_draft_id"]
    assert ap_row["result_status"] == "executed"
    assert ap_row["executed_at"] is not None


def test_approve_send_refused_in_p4a(isolated_stub_env: Path) -> None:
    _seed_pipeline(isolated_stub_env / "stub.json")
    _grant_compose()  # only compose granted, not send
    import draft_agent
    from lib import approval

    res = draft_agent.run("fix-thread-102", "Reply to Adam Moore confirming the Thursday 2pm call")
    with pytest.raises(approval.SendNotPermitted):
        approval.approve(res["draft_id"], action="send", approved_by="raphael@test")


def test_cancel_during_undo_window(isolated_stub_env: Path) -> None:
    _seed_pipeline(isolated_stub_env / "stub.json")
    _grant_compose()
    import draft_agent
    from lib import approval, db

    res = draft_agent.run("fix-thread-102", "Reply to Adam Moore confirming the Thursday 2pm call")
    ap = approval.approve(res["draft_id"], action="save_as_draft", approved_by="raphael@test")

    cancel = approval.cancel(ap["approval_id"], reason="user clicked cancel")
    assert cancel["draft_id"] == res["draft_id"]

    with db.derived() as conn:
        d = conn.execute("SELECT status, approval_id FROM drafts WHERE id = ?", (res["draft_id"],)).fetchone()
        a = conn.execute("SELECT result_status FROM approvals WHERE id = ?", (ap["approval_id"],)).fetchone()
    assert d["status"] == "pending"
    assert d["approval_id"] is None
    assert a["result_status"] == "cancelled"


def test_hash_mismatch_after_edit(isolated_stub_env: Path) -> None:
    """Edit a draft after approval but before execution → execute should refuse."""
    _seed_pipeline(isolated_stub_env / "stub.json")
    _grant_compose()
    import draft_agent
    from lib import approval, db, gmail_writer

    res = draft_agent.run("fix-thread-102", "Reply to Adam Moore confirming the Thursday 2pm call")
    ap = approval.approve(res["draft_id"], action="save_as_draft", approved_by="raphael@test")

    # Force-edit the draft body directly (bypassing the not-pending guard) to
    # simulate a tamper. Realistically the user would have to cancel first;
    # this test covers the defense-in-depth path.
    with db.derived() as conn:
        conn.execute(
            "UPDATE drafts SET body = ?, draft_hash = ? WHERE id = ?",
            ("Tampered body.", "deadbeef" * 8, res["draft_id"]),
        )

    with pytest.raises(approval.HashMismatch):
        approval.execute_save_as_draft(ap["approval_id"], gmail_writer=gmail_writer.save_as_draft)


def test_approval_expired_path(isolated_stub_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Roll the approval's expires_at backwards in the DB and confirm
    execute() refuses with ApprovalExpired and the draft drops to pending."""
    _seed_pipeline(isolated_stub_env / "stub.json")
    _grant_compose()
    import draft_agent
    from lib import approval, db, gmail_writer

    res = draft_agent.run("fix-thread-102", "Reply to Adam Moore confirming the Thursday 2pm call")
    ap = approval.approve(res["draft_id"], action="save_as_draft", approved_by="raphael@test")
    with db.derived() as conn:
        conn.execute(
            "UPDATE approvals SET expires_at = ? WHERE id = ?",
            ("2020-01-01T00:00:00+00:00", ap["approval_id"]),
        )

    with pytest.raises(approval.ApprovalExpired):
        approval.execute_save_as_draft(ap["approval_id"], gmail_writer=gmail_writer.save_as_draft)

    with db.derived() as conn:
        d = conn.execute("SELECT status, approval_id FROM drafts WHERE id = ?", (res["draft_id"],)).fetchone()
    assert d["status"] == "pending"
    assert d["approval_id"] is None


def test_reject_terminates_pending(isolated_stub_env: Path) -> None:
    _seed_pipeline(isolated_stub_env / "stub.json")
    import draft_agent
    from lib import approval, db

    res = draft_agent.run("fix-thread-102", "Reply to Adam Moore confirming the Thursday 2pm call")
    approval.reject(res["draft_id"], reason="not the right tone")

    with db.derived() as conn:
        d = conn.execute("SELECT status FROM drafts WHERE id = ?", (res["draft_id"],)).fetchone()
    assert d["status"] == "rejected"


# ---------------- audit log ---------------------------------------------

def test_audit_chain_integrity(isolated_stub_env: Path) -> None:
    """Walk through generate → approve → cancel → reject and verify the
    audit chain stays intact."""
    _seed_pipeline(isolated_stub_env / "stub.json")
    _grant_compose()
    import draft_agent
    from lib import approval, db, gmail_writer

    res = draft_agent.run("fix-thread-102", "Reply to Adam Moore confirming the Thursday 2pm call")
    ap = approval.approve(res["draft_id"], action="save_as_draft", approved_by="raphael@test")
    approval.cancel(ap["approval_id"], reason="changed mind")

    res2 = draft_agent.run("fix-thread-102", "Reply to Adam Moore confirming the Thursday 2pm call")
    ap2 = approval.approve(res2["draft_id"], action="save_as_draft", approved_by="raphael@test")
    approval.execute_save_as_draft(ap2["approval_id"], gmail_writer=gmail_writer.save_as_draft)

    chain = approval.verify_audit_chain()
    assert chain["ok"], chain

    with db.derived() as conn:
        events = [r["event_type"] for r in conn.execute(
            "SELECT event_type FROM audit_log ORDER BY rowid ASC"
        ).fetchall()]
    # We should see: approve, cancel, approve, save_as_draft (at minimum).
    # The grant from _grant_compose() doesn't go through the audit_log
    # helper (it writes the file directly), so it's absent — that's fine,
    # the chain is still complete.
    assert events.count("approve") == 2
    assert "cancel" in events
    assert "save_as_draft" in events


def test_audit_log_rejects_update_via_trigger(isolated_stub_env: Path) -> None:
    _seed_pipeline(isolated_stub_env / "stub.json")
    _grant_compose()
    import draft_agent
    from lib import approval, db

    res = draft_agent.run("fix-thread-102", "Reply to Adam Moore confirming the Thursday 2pm call")
    approval.reject(res["draft_id"], reason="for testing")

    with db.derived() as conn:
        with pytest.raises(sqlite3.IntegrityError, match="audit_log is append-only"):
            conn.execute(
                "UPDATE audit_log SET event_type = 'tampered' WHERE rowid = 1"
            )


def test_audit_log_rejects_delete_via_trigger(isolated_stub_env: Path) -> None:
    _seed_pipeline(isolated_stub_env / "stub.json")
    _grant_compose()
    import draft_agent
    from lib import approval, db

    res = draft_agent.run("fix-thread-102", "Reply to Adam Moore confirming the Thursday 2pm call")
    approval.reject(res["draft_id"], reason="for testing")

    with db.derived() as conn:
        with pytest.raises(sqlite3.IntegrityError, match="audit_log is append-only"):
            conn.execute("DELETE FROM audit_log")


def test_chain_ok_when_empty() -> None:
    from lib import approval

    chain = approval.verify_audit_chain()
    assert chain == {"ok": True, "total": 0, "broken_at": None}


# ---------------- service endpoints (smoke) -----------------------------

def test_draft_generate_endpoint(isolated_stub_env: Path) -> None:
    _seed_pipeline(isolated_stub_env / "stub.json")
    from fastapi.testclient import TestClient

    import service

    client = TestClient(service.app)
    res = client.post(
        "/draft/generate",
        json={
            "thread_id": "fix-thread-102",
            "intent": "Reply to Adam Moore confirming the Thursday 2pm call",
        },
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["draft"]["confidence"] == "high"
    assert "Thursday" in body["draft"]["body"]


def test_draft_full_flow_via_http(isolated_stub_env: Path) -> None:
    """generate → grant compose → approve → save_as_gmail_draft, all over
    the FastAPI client. Covers the wire shapes the webview depends on."""
    _seed_pipeline(isolated_stub_env / "stub.json")
    from fastapi.testclient import TestClient

    import service

    client = TestClient(service.app)

    gen = client.post(
        "/draft/generate",
        json={
            "thread_id": "fix-thread-102",
            "intent": "Reply to Adam Moore confirming the Thursday 2pm call",
        },
    )
    assert gen.status_code == 200
    draft_id = gen.json()["draft_id"]

    # Grant compose.
    perms = client.post("/permissions", json={"gmail_compose": True})
    assert perms.status_code == 200
    assert perms.json()["gmail.compose"] is True

    # Approve.
    ap = client.post(
        f"/drafts/{draft_id}/approve",
        json={"action": "save_as_draft"},
    )
    assert ap.status_code == 200, ap.text
    approval_id = ap.json()["approval_id"]

    # Execute (in real flow the webview waits the undo window — the API is
    # synchronous so the test fires immediately).
    saved = client.post(
        f"/drafts/{draft_id}/save_as_gmail_draft",
        json={"approval_id": approval_id},
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["next_status"] == "saved_as_draft"
    assert saved.json()["gmail_draft_id"].startswith("draft-")


def test_send_toggle_refused_in_p4a(isolated_stub_env: Path) -> None:
    from fastapi.testclient import TestClient

    import service

    client = TestClient(service.app)
    res = client.post("/permissions", json={"gmail_send": True})
    assert res.status_code == 403
    detail = res.json()["detail"]
    assert detail["code"] == "send_not_supported_in_p4a"


def test_audit_log_endpoint_reports_chain(isolated_stub_env: Path) -> None:
    _seed_pipeline(isolated_stub_env / "stub.json")
    _grant_compose()
    from fastapi.testclient import TestClient
    import draft_agent
    from lib import approval

    res = draft_agent.run("fix-thread-102", "Reply to Adam Moore confirming the Thursday 2pm call")
    approval.reject(res["draft_id"], reason="smoke")

    import service

    client = TestClient(service.app)
    out = client.get("/audit_log")
    assert out.status_code == 200
    body = out.json()
    assert body["chain_ok"] is True
    assert body["count"] >= 1
    assert any(e["event_type"] == "reject" for e in body["events"])
