"""End-to-end stub tests for the three P2 components.

Run entirely against MAILMIND_STUB_RESPONSES — no Vertex calls. The
acceptance script ``acceptance/p2.sh`` runs these as the unit-test gate
before spawning the sidecars.
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
    """Fresh tmp dir + seed raw.sqlite + build stub responses. Identical to
    the P1 fixture; duplicated here so the P2 suite can run standalone."""
    tmp = Path(tempfile.mkdtemp(prefix="mailmind-p2-"))
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


def _seed_extracts() -> None:
    """Run extract on the keep-disposition fixture threads so the rollup has
    `thread_facts` to consume."""
    import extract_agent

    for tid in ("fix-thread-101", "fix-thread-102", "fix-thread-103"):
        extract_agent.run(tid)


# ---------------- Cadence parsers (pure) ----------------

def test_cadence_classifies_overdue_cold_waiting() -> None:
    from lib import cadence

    th = cadence.resolve_thresholds(None)
    assert cadence.classify_urgency(8, 1, th) == "overdue"
    assert cadence.classify_urgency(45, 1, th) == "cold"
    assert cadence.classify_urgency(2, 3, th) == "waiting"


def test_cadence_parses_user_context_template() -> None:
    from lib import cadence, prompts

    md = prompts.load_user_context()
    latencies = cadence.parse_reply_latencies(md)
    # Template ships these defaults — the parser must hit them.
    assert latencies.get("recruiting") == 1
    assert latencies.get("legal") == 3


# ---------------- Relationship agent ----------------

def test_relationship_agent_rolls_up_morgan_and_adam() -> None:
    import relationship_agent

    _seed_extracts()

    morgan = relationship_agent.run("morgan@northstar-talent.com")
    assert morgan["skipped"] is False
    assert morgan["stubbed"] is True
    assert morgan["status"] == "awaiting_them"
    assert morgan["draft_next_steps_count"] == 1

    adam = relationship_agent.run("adam@vectorlabs.io")
    assert adam["status"] == "awaiting_me"
    assert adam["draft_next_steps_count"] == 1

    rollups = relationship_agent.list_rollups()
    by_email = {r["contact_email"]: r for r in rollups}
    assert by_email["morgan@northstar-talent.com"]["tone"] == "warm"
    assert "recruiter" in by_email["adam@vectorlabs.io"]["tags"]


def test_relationship_agent_idempotency_via_content_hash() -> None:
    import relationship_agent

    _seed_extracts()
    relationship_agent.run("adam@vectorlabs.io")
    again = relationship_agent.run("adam@vectorlabs.io")
    assert again["skipped"] is True


# ---------------- Reconcile + dismiss-bug fix ----------------

def test_reconcile_inserts_pending_steps_from_drafts() -> None:
    import reconcile
    import relationship_agent

    _seed_extracts()
    relationship_agent.run("morgan@northstar-talent.com")
    relationship_agent.run("adam@vectorlabs.io")

    result = reconcile.run()
    assert result["totals"]["inserted"] == 2
    assert result["totals"]["matched"] == 0
    assert result["pending_total"] == 2

    pending = reconcile.list_next_steps()
    assert {p["contact_email"] for p in pending} == {
        "morgan@northstar-talent.com",
        "adam@vectorlabs.io",
    }


def test_dismiss_persists_across_rollups() -> None:
    """§19 regression: dismissing a step then re-reconciling against the same
    draft batch must NOT recreate an equivalent step.

    Note: we re-run *reconcile*, not *rollup*, because the rollup's
    draft_next_steps don't change just because the user clicked dismiss —
    the bug v1 exhibits is that the next reconcile pass can't see the
    correction and re-INSERTs the same step. Rolling up again here would
    just exercise the LLM stub for a different-shaped payload (with a
    populated `previous_rollup`), which is orthogonal to §19.
    """
    import reconcile
    import relationship_agent

    _seed_extracts()
    relationship_agent.run("adam@vectorlabs.io")
    reconcile.run()

    pending = reconcile.list_next_steps()
    adam_step = next(p for p in pending if p["contact_email"] == "adam@vectorlabs.io")

    dismissed = reconcile.dismiss_step(adam_step["id"], user_note="not pursuing this lead")
    assert dismissed["correction_id"]

    second = reconcile.run()

    # The Adam draft must be silently dropped; pending count must NOT grow.
    pending_after = reconcile.list_next_steps(status="pending")
    adam_pending = [p for p in pending_after if p["contact_email"] == "adam@vectorlabs.io"]
    assert adam_pending == [], f"dismiss bug regressed: {adam_pending}"
    assert second["totals"]["dropped_dismissed"] >= 1


# ---------------- Cadence runner ----------------

def test_cadence_runner_classifies_threads() -> None:
    """Today is 2026-04-27. Morgan rollup has both thread-101 (8d, recruiting,
    user-last → they_owe_you overdue) and thread-103 (45d → cold). Adam
    rollup has thread-102 (6d, recruiting, other-last → you_owe_them
    overdue).
    """
    import cadence_runner
    import relationship_agent

    _seed_extracts()
    relationship_agent.run("morgan@northstar-talent.com")
    relationship_agent.run("adam@vectorlabs.io")

    report = cadence_runner.compute_followups(now="2026-04-27T12:00:00Z")
    they = {(e["thread_id"], e["urgency"]) for e in report["they_owe_you"]}
    you = {(e["thread_id"], e["urgency"]) for e in report["you_owe_them"]}
    assert ("fix-thread-101", "overdue") in they
    assert ("fix-thread-103", "cold") in they
    assert ("fix-thread-102", "overdue") in you


# ---------------- service endpoints (smoke) ----------------

def test_followups_endpoint_filters_overdue() -> None:
    import cadence_runner
    import relationship_agent
    from fastapi.testclient import TestClient

    import service

    _seed_extracts()
    relationship_agent.run("morgan@northstar-talent.com")
    relationship_agent.run("adam@vectorlabs.io")

    client = TestClient(service.app)
    res = client.get("/followups")
    assert res.status_code == 200
    data = res.json()
    assert data["metadata"]["they_owe_count"] >= 1


def test_corrections_endpoint_returns_dismissed() -> None:
    import reconcile
    import relationship_agent
    from fastapi.testclient import TestClient

    import service

    _seed_extracts()
    relationship_agent.run("adam@vectorlabs.io")
    reconcile.run()
    pending = reconcile.list_next_steps()
    reconcile.dismiss_step(pending[0]["id"])

    client = TestClient(service.app)
    res = client.get("/corrections")
    rows = res.json()["corrections"]
    assert any(r["entity_type"] == "next_step" and r["new_value"] == "dismissed" for r in rows)
