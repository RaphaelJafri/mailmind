"""End-to-end stub tests for the three P1 agents.

These run entirely against MAILMIND_STUB_RESPONSES — no Vertex calls. The
acceptance script `acceptance/p1.sh` runs these as the unit-test gate before
spawning the sidecars.
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
    """Build a fresh stub-responses file in a tmp dir, seed raw.sqlite, and
    point both env vars at the tmp dir."""
    tmp = Path(tempfile.mkdtemp(prefix="mailmind-p1-"))
    monkeypatch.setenv("MAILMIND_DATA_DIR", str(tmp))
    monkeypatch.setenv("GOOGLE_GENAI_API_KEY", "fake-key-for-stub")

    # Seed raw.sqlite via the Node loader. We assume the ingester is already
    # `npm install`ed by the caller (acceptance/p1.sh handles that).
    subprocess.run(
        ["node", "fixtures/load_fixtures.mjs"],
        cwd=REPO,
        check=True,
        env={**os.environ, "MAILMIND_DATA_DIR": str(tmp)},
        capture_output=True,
    )

    # Build the stub-response file (live prompt → hash).
    stub_path = tmp / "stub.json"
    subprocess.run(
        [sys.executable, "fixtures/build_stub_responses.py", str(stub_path)],
        cwd=REPO,
        check=True,
        capture_output=True,
    )
    monkeypatch.setenv("MAILMIND_STUB_RESPONSES", str(stub_path))
    return tmp


def test_triage_agent_writes_three_proposals() -> None:
    import triage_agent

    result = triage_agent.run(min_thread_count=1, limit=30)
    assert result["senders_considered"] == 3
    assert result["proposals_written"] == 3
    assert result["schema_invalid"] == 0
    assert result["stubbed"] is True

    proposals = triage_agent.list_proposals(status="pending")
    assert len(proposals) == 3
    by_sender = {p["sender_email"]: p for p in proposals}
    assert by_sender["morgan@northstar-talent.com"]["proposed_disposition"] == "keep"
    assert by_sender["newsletter@stratechery.com"]["proposed_disposition"] == "newsletter"
    assert by_sender["deals@example-retailer.com"]["proposed_disposition"] == "skip"


def test_triage_decide_marks_proposal() -> None:
    import triage_agent

    triage_agent.run()
    pending = triage_agent.list_proposals()
    assert pending, "must have at least one pending proposal"

    decided = triage_agent.decide(pending[0]["id"], "approve")
    assert decided["user_decision"] == "approve"
    assert decided["reviewed_at"] is not None

    # Re-deciding fails.
    with pytest.raises(LookupError):
        triage_agent.decide(pending[0]["id"], "reject")


def test_extract_agent_round_trip() -> None:
    import extract_agent

    r = extract_agent.run("fix-thread-001")
    assert r["skipped"] is False
    assert r["confidence"] in {"high", "med", "low"}
    assert r["stubbed"] is True
    assert "Morgan" in r["summary"] or "intro" in r["summary"].lower()

    # Idempotency — second call hits content-hash skip.
    r2 = extract_agent.run("fix-thread-001")
    assert r2["skipped"] is True


def test_extract_agent_provenance_check_blocks_fabrication() -> None:
    import json
    import sqlite3
    from importlib import reload

    import extract_agent
    from lib import gemini_runner, prompts as pmod, vertex_config

    # Build a stub response with a fabricated source_message_id.
    bad = {
        "thread_id": "fix-thread-001",
        "participants": ["morgan@northstar-talent.com", "raphaeljafri@gmail.com"],
        "summary": "Test",
        "commitments_by_user": [
            {
                "description": "fake",
                "due_date": None,
                "source_message_ids": ["fix-msg-NONEXISTENT"],
            }
        ],
        "commitments_by_others": [],
        "open_questions": [],
        "last_message_from": "user",
        "last_message_date": "2026-04-22T15:10:00Z",
        "sentiment": "positive",
        "topic_tags": ["recruiting"],
        "confidence": "high",
    }

    # Override stub for this thread.
    import hashlib
    from lib import raw_reader

    thread = raw_reader.get_thread("fix-thread-001")
    user_prompt = (
        "Extract facts from the following thread. Return one ThreadFacts JSON object.\n\n"
        f"<thread>\n{json.dumps(thread, indent=2)}\n</thread>"
    )
    system = pmod.compose_system_prompt(
        "extract.md", schemas={"THREAD_FACTS_SCHEMA": "thread-facts.schema.json"}
    )
    full = f"{system}\n---\n{user_prompt}"
    h = hashlib.sha256(f"{vertex_config.GEMINI_FLASH}::{full}".encode()).hexdigest()

    stub_path = Path(os.environ["MAILMIND_STUB_RESPONSES"])
    stub = json.loads(stub_path.read_text())
    stub[h] = {"output": json.dumps(bad), "input_tokens": 100, "output_tokens": 50, "latency_ms": 10}
    stub_path.write_text(json.dumps(stub))

    # Drop the prior valid extract row so the agent re-runs.
    from lib import paths

    derived = sqlite3.connect(paths.derived_db_path())
    derived.execute(
        "CREATE TABLE IF NOT EXISTS thread_facts ("
        "thread_id TEXT PRIMARY KEY, content_hash TEXT, extracted_at TIMESTAMP,"
        "model_version TEXT, facts_json TEXT, confidence TEXT, worker_log_path TEXT)"
    )
    derived.execute("DELETE FROM thread_facts WHERE thread_id = 'fix-thread-001'")
    derived.commit()
    derived.close()

    from lib.schema import SchemaValidationError

    with pytest.raises(SchemaValidationError) as exc:
        extract_agent.run("fix-thread-001", force=True)
    assert "fabricated" in str(exc.value).lower()


def test_tagger_agent_persists_tags_for_each_message() -> None:
    import tagger_agent

    rows = []
    for mid in ["fix-msg-001a", "fix-msg-001b", "fix-msg-002a", "fix-msg-003a"]:
        r = tagger_agent.run(mid)
        assert r["stubbed"] is True
        assert r["rows_written"] >= 2  # urgency + category at minimum
        rows.append(r)

    summary = tagger_agent.list_tag_summary()
    by_kv = {(s["tag_kind"], s["tag_value"]): s["message_count"] for s in summary}
    assert by_kv[("urgency", "followup")] == 1
    assert by_kv[("urgency", "informational")] == 2
    assert by_kv[("urgency", "ignore")] == 1
    assert by_kv[("category", "recruiting")] == 2
    assert by_kv[("category", "newsletter")] == 2
    assert by_kv[("project", "jobsearch")] == 2


def test_agent_runs_table_records_each_call() -> None:
    import triage_agent
    from lib import db

    triage_agent.run()

    with db.agent_runs() as conn:
        rows = conn.execute(
            "SELECT * FROM agent_runs WHERE agent_name = 'triage_agent'"
        ).fetchall()
    assert len(rows) >= 1
    r = dict(rows[0])
    assert r["result_status"] == "success"
    assert r["stubbed"] == 1
    assert r["latency_ms"] is not None
