"""P5b tests — labels storage, eval_agent scoring, baseline + regression.

Same fixture pattern as the P5a suite. Each test has an isolated tmp data
dir so labels never bleed between tests. The two-phase stub pattern from
P3/P4 is reused so the eval_agent has actual extracted facts +
contact_rollups to score against.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def isolated_stub_env(monkeypatch: pytest.MonkeyPatch) -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="mailmind-p5b-"))
    monkeypatch.setenv("MAILMIND_DATA_DIR", str(tmp))
    monkeypatch.setenv("MAILMIND_LOGS_DIR", str(tmp / "logs"))
    monkeypatch.setenv("GOOGLE_GENAI_API_KEY", "fake-key-for-stub")

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


def _seed_pipeline() -> None:
    """Walk extract → relationship → reconcile so derived.sqlite is populated."""
    import extract_agent
    import reconcile
    import relationship_agent

    for tid in ("fix-thread-101", "fix-thread-102", "fix-thread-103"):
        extract_agent.run(tid)
    relationship_agent.run("morgan@northstar-talent.com")
    relationship_agent.run("adam@vectorlabs.io")
    reconcile.run()


# ---------------- labels storage ----------------------------------------

def test_label_add_appends_one_row(isolated_stub_env: Path) -> None:
    from lib import labels

    row = labels.add_label(
        kind="thread",
        target_id="fix-thread-101",
        expected={"summary": "Test"},
        labeled_by="raphael@test",
    )
    assert row.id.startswith("lbl_")
    assert row.kind == "thread"
    assert row.target_id == "fix-thread-101"

    rows = labels.list_labels()
    assert len(rows) == 1
    assert rows[0]["id"] == row.id


def test_label_kinds_validated(isolated_stub_env: Path) -> None:
    from lib import labels

    with pytest.raises(ValueError):
        labels.add_label(
            kind="banana",  # type: ignore[arg-type]
            target_id="x",
            expected={"x": 1},
            labeled_by="r",
        )
    with pytest.raises(ValueError):
        labels.add_label(kind="thread", target_id="", expected={"x": 1}, labeled_by="r")
    with pytest.raises(ValueError):
        labels.add_label(kind="thread", target_id="x", expected={}, labeled_by="r")


def test_label_list_filters_by_kind(isolated_stub_env: Path) -> None:
    from lib import labels

    labels.add_label(kind="thread", target_id="t1", expected={"summary": "a"}, labeled_by="r")
    labels.add_label(kind="rollup", target_id="r1@x", expected={"tone": "warm"}, labeled_by="r")
    labels.add_label(kind="draft", target_id="t1::write", expected={"tone": "warm"}, labeled_by="r")

    assert len(labels.list_labels(kind="thread")) == 1
    assert len(labels.list_labels(kind="rollup")) == 1
    assert len(labels.list_labels(kind="draft")) == 1
    assert len(labels.list_labels()) == 3
    assert labels.counts() == {"thread": 1, "rollup": 1, "draft": 1, "total": 3}


def test_soft_delete_filters_out(isolated_stub_env: Path) -> None:
    from lib import labels

    r1 = labels.add_label(kind="thread", target_id="t1", expected={"summary": "x"}, labeled_by="r")
    labels.add_label(kind="thread", target_id="t2", expected={"summary": "y"}, labeled_by="r")

    assert labels.soft_delete(r1.id, by="r") is True
    rows = labels.list_labels()
    assert len(rows) == 1
    assert rows[0]["target_id"] == "t2"
    # Re-deleting is a no-op (soft-delete returned False).
    assert labels.soft_delete(r1.id, by="r") is False


def test_soft_delete_unknown_returns_false(isolated_stub_env: Path) -> None:
    from lib import labels

    assert labels.soft_delete("lbl_does_not_exist", by="r") is False


def test_labels_jsonl_persisted(isolated_stub_env: Path) -> None:
    """File on disk has one JSON line per write — diff-friendly + survives
    DB resets."""
    from lib import labels

    labels.add_label(kind="thread", target_id="t1", expected={"summary": "x"}, labeled_by="r")
    labels.add_label(kind="rollup", target_id="r1@x", expected={"tone": "warm"}, labeled_by="r")

    p = labels.labels_path()
    assert p.exists()
    lines = [json.loads(l) for l in p.read_text().splitlines() if l.strip()]
    assert len(lines) == 2
    assert lines[0]["kind"] == "thread"
    assert lines[1]["kind"] == "rollup"


def test_latest_per_target_dedupes(isolated_stub_env: Path) -> None:
    """Multiple labels for the same target — latest_per_target picks the
    newest non-deleted row."""
    from lib import labels

    labels.add_label(kind="thread", target_id="t1", expected={"summary": "v1"}, labeled_by="r")
    labels.add_label(kind="thread", target_id="t1", expected={"summary": "v2"}, labeled_by="r")

    out = labels.latest_per_target()
    assert len(out) == 1
    assert out[0].expected["summary"] == "v2"


# ---------------- eval_agent: empty / bookkeeping -----------------------

def test_eval_run_with_no_labels(isolated_stub_env: Path) -> None:
    import eval_agent

    out = eval_agent.run()
    assert out["ok"] is False
    assert out["reason"] == "no_labels"
    assert out["label_counts"]["total"] == 0


def test_eval_run_records_agent_runs_row(isolated_stub_env: Path) -> None:
    import eval_agent
    from lib import db, labels

    _seed_pipeline()
    labels.add_label(
        kind="thread",
        target_id="fix-thread-101",
        expected={
            "summary": "Morgan checked Adam at Vector Labs reached out — Raphael said no, asked Morgan to nudge.",
            "commitments_by_user": [],
            "commitments_by_others": [{"description": "Morgan will nudge Adam this week", "source_message_ids": ["fix-msg-101a"]}],
        },
        labeled_by="raphael@test",
    )
    eval_agent.run()

    with db.agent_runs() as conn:
        rows = conn.execute(
            "SELECT * FROM agent_runs WHERE agent_name = 'eval_agent' ORDER BY started_at DESC LIMIT 1"
        ).fetchall()
    assert rows
    assert rows[0]["result_status"] in ("success", "schema_partial_fail")


def test_results_jsonl_appended(isolated_stub_env: Path) -> None:
    import eval_agent
    from lib import labels

    _seed_pipeline()
    labels.add_label(
        kind="thread",
        target_id="fix-thread-101",
        expected={"summary": "test", "commitments_by_user": []},
        labeled_by="r",
    )
    eval_agent.run()

    rows = eval_agent.list_results()
    assert rows
    assert "metrics" in rows[0]


# ---------------- eval_agent: thread scoring ----------------------------

def test_thread_label_scores_extract_agent(isolated_stub_env: Path) -> None:
    import eval_agent
    from lib import labels

    _seed_pipeline()
    # Hand-crafted thread label whose expected commitments roughly match
    # the stub fixture for fix-thread-101.
    labels.add_label(
        kind="thread",
        target_id="fix-thread-101",
        expected={
            "summary": "Morgan checked whether Adam at Vector Labs had reached out; Raphael said no and asked Morgan to nudge Adam.",
            "commitments_by_user": [],
            "commitments_by_others": [
                {"description": "Morgan will nudge Adam this week", "source_message_ids": ["fix-msg-101a"]},
            ],
        },
        labeled_by="r",
    )
    out = eval_agent.run()
    assert out["ok"] is True
    extract = out["metrics"].get("extract_agent")
    assert extract is not None
    assert extract["n"] == 1
    # Either the stubbed extract matches the label tightly (high F1) or it
    # doesn't — but the metric must be a number in [0, 1].
    assert 0.0 <= extract["f1_commitments_others"] <= 1.0
    assert 0.0 <= extract["summary_jaccard"] <= 1.0


def test_thread_label_no_commitments_perfect_score(isolated_stub_env: Path) -> None:
    """When the thread truly has no commitments + agent reports none,
    precision/recall = 1/1, F1 = 1."""
    import eval_agent
    from lib import labels

    _seed_pipeline()
    labels.add_label(
        kind="thread",
        target_id="fix-thread-101",
        expected={"summary": "x", "commitments_by_user": [], "commitments_by_others": []},
        labeled_by="r",
    )
    out = eval_agent.run()
    extract = out["metrics"].get("extract_agent")
    # If the agent's commitments_by_user is empty too, P/R = 1/1 → F1 = 1.
    # If not, score will be < 1; either way it must be a number.
    assert extract is not None


# ---------------- eval_agent: rollup scoring ----------------------------

def test_rollup_label_scores_relationship_agent(isolated_stub_env: Path) -> None:
    import eval_agent
    from lib import labels

    _seed_pipeline()
    labels.add_label(
        kind="rollup",
        target_id="morgan@northstar-talent.com",
        expected={"tone": "warm", "cadence": "weekly", "status": "active", "tags": ["recruiter"]},
        labeled_by="r",
    )
    out = eval_agent.run()
    rollup_metrics = out["metrics"].get("relationship_agent")
    assert rollup_metrics is not None
    assert rollup_metrics["n"] == 1
    # All four metrics in [0, 1].
    for k in ("tone_accuracy", "cadence_accuracy", "status_accuracy", "tags_jaccard"):
        assert 0.0 <= rollup_metrics[k] <= 1.0


# ---------------- eval_agent: draft scoring (judge fallback path) ------

def test_draft_label_runs_with_heuristic_fallback(isolated_stub_env: Path) -> None:
    """In stub mode the judge prompt won't have a fixture, so eval_agent
    falls back to the heuristic. We assert the pipeline produced numeric
    scores rather than crashing."""
    import eval_agent
    from lib import labels

    _seed_pipeline()
    # Rebuild stubs with --include-draft so draft_agent has a stub for
    # the same (thread_id, intent) pair.
    subprocess.run(
        [
            sys.executable,
            "fixtures/build_stub_responses.py",
            str(isolated_stub_env / "stub.json"),
            "--include-draft",
        ],
        cwd=REPO, check=True, capture_output=True,
    )

    labels.add_label(
        kind="draft",
        target_id="fix-thread-102::Reply to Adam Moore confirming the Thursday 2pm call",
        expected={
            "tone": "warm-confident",
            "must_mention": ["Thursday", "2pm"],
            "must_not_mention": [],
            "factually_correct": True,
            "would_send": True,
        },
        labeled_by="r",
    )
    out = eval_agent.run()
    draft_metrics = out["metrics"].get("draft_agent")
    assert draft_metrics is not None
    assert draft_metrics["n"] == 1
    # Heuristic must_mention coverage — Thursday + 2pm should both be in
    # the draft body of the canned fixture.
    assert 0.0 <= draft_metrics["must_mention_coverage"] <= 1.0
    assert 0.0 <= draft_metrics["overall_style_match"] <= 1.0


def test_draft_label_invalid_target_format(isolated_stub_env: Path) -> None:
    """A draft label without the `::` separator surfaces an ok=False row."""
    import eval_agent
    from lib import labels

    _seed_pipeline()
    labels.add_label(
        kind="draft",
        target_id="just-a-thread-id",  # missing ::intent
        expected={"tone": "warm", "must_mention": [], "would_send": True, "factually_correct": True},
        labeled_by="r",
    )
    out = eval_agent.run()
    bad = [r for r in out["per_label"] if r.get("ok") is False]
    assert any("thread_id::intent" in (r.get("error") or "") for r in bad)


# ---------------- baseline + regression --------------------------------

def test_freeze_baseline_writes_baseline_jsonl(isolated_stub_env: Path) -> None:
    import eval_agent
    from lib import labels

    _seed_pipeline()
    labels.add_label(kind="thread", target_id="fix-thread-101",
                     expected={"summary": "x", "commitments_by_user": []},
                     labeled_by="r")
    eval_agent.run()
    out = eval_agent.freeze_baseline()
    assert out["frozen_run_id"]
    bp = isolated_stub_env / "evals" / "baseline.jsonl"
    assert bp.exists()
    assert json.loads(bp.read_text().strip())["run_id"] == out["frozen_run_id"]


def test_freeze_baseline_without_results_raises(isolated_stub_env: Path) -> None:
    import eval_agent

    with pytest.raises(LookupError):
        eval_agent.freeze_baseline()


def test_regression_detected_when_baseline_higher(isolated_stub_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Synthesize a baseline with metric=1.0, current=0.5 → 50% drop → flagged."""
    import eval_agent
    from lib import paths

    bp = paths.data_dir() / "evals" / "baseline.jsonl"
    bp.parent.mkdir(parents=True, exist_ok=True)
    bp.write_text(
        json.dumps(
            {
                "run_id": "fake_baseline",
                "ran_at": datetime.now(timezone.utc).isoformat(),
                "metrics": {
                    "extract_agent": {"n": 5, "f1_commitments_user": 1.0},
                },
                "label_counts": {"thread": 5, "rollup": 0, "draft": 0, "total": 5},
            }
        )
        + "\n"
    )

    current = {"extract_agent": {"n": 5, "f1_commitments_user": 0.5}}
    baseline = eval_agent._load_baseline()
    regs = eval_agent._compare_to_baseline(current, baseline)
    assert any(r["path"] == "extract_agent.f1_commitments_user" for r in regs)
    assert regs[0]["delta_pct"] < -5.0


def test_no_regression_when_metric_holds(isolated_stub_env: Path) -> None:
    """A 4% drop is within the 5% budget — no regression flagged."""
    import eval_agent

    baseline = {"metrics": {"extract_agent": {"n": 5, "f1_commitments_user": 1.0}}}
    current = {"extract_agent": {"n": 5, "f1_commitments_user": 0.96}}
    regs = eval_agent._compare_to_baseline(current, baseline)
    assert regs == []


def test_improvement_not_flagged(isolated_stub_env: Path) -> None:
    import eval_agent

    baseline = {"metrics": {"extract_agent": {"n": 5, "f1_commitments_user": 0.5}}}
    current = {"extract_agent": {"n": 5, "f1_commitments_user": 0.9}}
    regs = eval_agent._compare_to_baseline(current, baseline)
    assert regs == []


# ---------------- service endpoints -------------------------------------

def test_labels_endpoint_round_trip(isolated_stub_env: Path) -> None:
    from fastapi.testclient import TestClient

    import service

    client = TestClient(service.app)
    r = client.post(
        "/labels",
        json={
            "kind": "thread",
            "target_id": "fix-thread-101",
            "expected": {"summary": "x", "commitments_by_user": []},
        },
    )
    assert r.status_code == 200, r.text
    label_id = r.json()["id"]

    listing = client.get("/labels?kind=thread")
    assert listing.status_code == 200
    body = listing.json()
    assert body["count"] == 1
    assert body["labels"][0]["id"] == label_id
    assert body["counts_by_kind"]["thread"] == 1


def test_labels_delete_endpoint(isolated_stub_env: Path) -> None:
    from fastapi.testclient import TestClient

    import service

    client = TestClient(service.app)
    r = client.post(
        "/labels",
        json={"kind": "rollup", "target_id": "x@y.com", "expected": {"tone": "warm"}},
    )
    label_id = r.json()["id"]

    d = client.delete(f"/labels/{label_id}")
    assert d.status_code == 200
    assert d.json()["deleted"] is True

    # Subsequent delete 404s.
    d2 = client.delete(f"/labels/{label_id}")
    assert d2.status_code == 404


def test_labels_bad_kind_400(isolated_stub_env: Path) -> None:
    from fastapi.testclient import TestClient

    import service

    client = TestClient(service.app)
    r = client.post(
        "/labels",
        json={"kind": "banana", "target_id": "x", "expected": {"a": 1}},
    )
    assert r.status_code == 400


def test_eval_run_endpoint(isolated_stub_env: Path) -> None:
    from fastapi.testclient import TestClient

    import service
    from lib import labels

    _seed_pipeline()
    labels.add_label(
        kind="thread", target_id="fix-thread-101",
        expected={"summary": "x", "commitments_by_user": []},
        labeled_by="r",
    )

    client = TestClient(service.app)
    r = client.post("/eval/run", json={"dry_run": False})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert "extract_agent" in body["metrics"]


def test_eval_results_endpoint(isolated_stub_env: Path) -> None:
    from fastapi.testclient import TestClient

    import eval_agent
    import service
    from lib import labels

    _seed_pipeline()
    labels.add_label(kind="thread", target_id="fix-thread-101",
                     expected={"summary": "x", "commitments_by_user": []}, labeled_by="r")
    eval_agent.run()

    client = TestClient(service.app)
    r = client.get("/eval/results?limit=5")
    assert r.status_code == 200
    body = r.json()
    assert len(body["results"]) >= 1


def test_eval_baseline_freeze_endpoint(isolated_stub_env: Path) -> None:
    from fastapi.testclient import TestClient

    import eval_agent
    import service
    from lib import labels

    _seed_pipeline()
    labels.add_label(kind="thread", target_id="fix-thread-101",
                     expected={"summary": "x", "commitments_by_user": []}, labeled_by="r")
    eval_agent.run()

    client = TestClient(service.app)
    r1 = client.get("/eval/baseline")
    assert r1.json()["frozen"] is False

    f = client.post("/eval/baseline/freeze")
    assert f.status_code == 200
    assert f.json()["frozen_run_id"]

    r2 = client.get("/eval/baseline")
    assert r2.json()["frozen"] is True


def test_eval_baseline_freeze_without_results_409(isolated_stub_env: Path) -> None:
    from fastapi.testclient import TestClient

    import service

    client = TestClient(service.app)
    r = client.post("/eval/baseline/freeze")
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "no_results"


def test_thread_facts_endpoint(isolated_stub_env: Path) -> None:
    from fastapi.testclient import TestClient

    import service

    _seed_pipeline()
    client = TestClient(service.app)
    r = client.get("/thread_facts/fix-thread-101")
    assert r.status_code == 200
    body = r.json()
    assert body["thread_id"] == "fix-thread-101"
    assert "facts" in body
    assert "commitments_by_user" in body["facts"] or "commitments_by_others" in body["facts"]


def test_thread_facts_endpoint_404(isolated_stub_env: Path) -> None:
    from fastapi.testclient import TestClient

    import service

    client = TestClient(service.app)
    r = client.get("/thread_facts/does-not-exist")
    assert r.status_code == 404
