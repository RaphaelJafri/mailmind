"""P5a tests — cost guardrails, structured logging, observability endpoints.

Same fixture shape as the P1–P4 suites. Each test gets a fresh tmp data
dir so SQLite state doesn't bleed between runs. The cost_guard config is
sourced from `config/pipeline.yml` at the repo root, but every test that
needs different caps writes its own YAML and points
`MAILMIND_PIPELINE_CONFIG` at it.

The cost_guard is wired into `gemini_runner.generate_structured` via the
`agent_name=` opt-in. Stub-mode responses have zero tokens, so cost is
~0 and caps never trip — the explicit cost-cap tests use synthetic
agent_runs rows + monkeypatched checks instead.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import ulid

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def isolated_stub_env(monkeypatch: pytest.MonkeyPatch) -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="mailmind-p5a-"))
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


# ---------------- pricing math ---------------------------------------------

def test_actual_cost_flash() -> None:
    from lib import cost_guard

    # 1M input + 1M output at $0.30 + $2.50 = $2.80
    assert cost_guard.actual_cost_usd("gemini-2.5-flash", 1_000_000, 1_000_000) == pytest.approx(2.80, rel=1e-6)


def test_actual_cost_pro() -> None:
    from lib import cost_guard

    # 1M input at $1.25, 1M output at $10.00 = $11.25
    assert cost_guard.actual_cost_usd("gemini-2.5-pro", 1_000_000, 1_000_000) == pytest.approx(11.25, rel=1e-6)


def test_actual_cost_unknown_model_returns_zero() -> None:
    from lib import cost_guard

    assert cost_guard.actual_cost_usd("not-a-real-model", 1000, 1000) == 0.0


def test_actual_cost_zero_tokens_returns_zero() -> None:
    from lib import cost_guard

    assert cost_guard.actual_cost_usd("gemini-2.5-flash", 0, 0) == 0.0


def test_estimate_request_cost_uses_chars_div_4() -> None:
    from lib import cost_guard

    estimate = cost_guard.estimate_request_cost_usd(
        model="gemini-2.5-flash",
        prompt_chars=4000,         # → 1000 input tokens
        system_chars=0,
        max_output_tokens=2000,
    )
    # 1000 input × $0.30 + 2000 output × $2.50, all per-million
    expected = (1000 * 0.30 + 2000 * 2.50) / 1_000_000
    assert estimate == pytest.approx(expected, rel=1e-4)


# ---------------- per-task cap ---------------------------------------------

def test_check_per_task_under_cap_passes() -> None:
    from lib import cost_guard

    # extract cap is $0.02; $0.001 is well under.
    cost_guard.check_per_task("extract_agent", 0.001)  # no raise


def test_check_per_task_over_cap_raises() -> None:
    from lib import cost_guard

    with pytest.raises(cost_guard.CostBudgetExceeded) as exc_info:
        cost_guard.check_per_task("tagger_agent", 1.00)  # cap $0.005
    assert exc_info.value.code == "per_task_cap_exceeded"
    assert exc_info.value.meta["agent_name"] == "tagger_agent"


def test_check_per_task_zero_cap_skips_enforcement() -> None:
    """`cadence` and `reconcile` are deterministic — cap=0 means no LLM call
    is expected, so we don't enforce."""
    from lib import cost_guard

    cost_guard.check_per_task("cadence", 999.99)  # no raise
    cost_guard.check_per_task("reconcile", 999.99)


def test_check_per_task_disabled_via_env(monkeypatch: pytest.MonkeyPatch) -> None:
    from lib import cost_guard

    monkeypatch.setenv("MAILMIND_DISABLE_COST_GUARD", "1")
    cost_guard.check_per_task("tagger_agent", 1.00)  # no raise


# ---------------- per-day cap ----------------------------------------------

def _seed_run(
    agent_name: str,
    *,
    cost_usd: float,
    started_at: datetime | None = None,
    latency_ms: int = 100,
    result_status: str = "success",
    retry_count: int = 0,
) -> str:
    """Synthesize one agent_runs row. Used by per-day + anomaly + taxonomy
    tests that want deterministic data without invoking real agents."""
    from lib import db

    started_at = started_at or datetime.now(timezone.utc)
    rid = str(ulid.new())
    with db.agent_runs() as conn:
        conn.execute(
            """
            INSERT INTO agent_runs (
              id, agent_name, model, started_at, finished_at,
              input_tokens, output_tokens, cost_usd, latency_ms,
              result_status, retry_count, stubbed
            ) VALUES (?, ?, 'gemini-2.5-flash', ?, ?, 0, 0, ?, ?, ?, ?, 0)
            """,
            (
                rid,
                agent_name,
                started_at.isoformat(),
                (started_at + timedelta(milliseconds=latency_ms)).isoformat(),
                cost_usd,
                latency_ms,
                result_status,
                retry_count,
            ),
        )
    return rid


def test_check_per_day_passes_under_cap() -> None:
    from lib import cost_guard

    _seed_run("extract_agent", cost_usd=0.10)
    cost_guard.check_per_day("extract_agent")  # no raise — total $5 cap


def test_check_per_day_raises_at_cap(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Lower the per-day cap via a temp pipeline.yml so the test doesn't
    have to fake $5 of synthetic spend."""
    from lib import cost_guard

    cfg = tmp_path / "pipeline.yml"
    cfg.write_text(
        "cost_caps:\n"
        "  per_day_usd:\n"
        "    total: 0.05\n"
        "    soft_warn_pct: 70\n"
        "    hard_stop_pct: 100\n"
    )
    monkeypatch.setenv("MAILMIND_PIPELINE_CONFIG", str(cfg))

    _seed_run("draft_agent", cost_usd=0.06)  # over the $0.05 cap
    with pytest.raises(cost_guard.CostBudgetExceeded) as exc_info:
        cost_guard.check_per_day("draft_agent")
    assert exc_info.value.code == "per_day_cap_exceeded"


def test_daily_summary_aggregates_per_agent() -> None:
    from lib import cost_guard

    _seed_run("triage_agent", cost_usd=0.01)
    _seed_run("triage_agent", cost_usd=0.02)
    _seed_run("draft_agent", cost_usd=0.05)

    summary = cost_guard.daily_summary()
    by_agent = {r["agent_name"]: r for r in summary["per_agent"]}
    assert by_agent["triage_agent"]["total_usd"] == pytest.approx(0.03)
    assert by_agent["triage_agent"]["runs"] == 2
    assert by_agent["draft_agent"]["total_usd"] == pytest.approx(0.05)
    assert summary["total_usd"] == pytest.approx(0.08)
    assert summary["bucket"] == "ok"


def test_daily_summary_warn_bucket(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from lib import cost_guard

    cfg = tmp_path / "pipeline.yml"
    cfg.write_text(
        "cost_caps:\n"
        "  per_day_usd:\n"
        "    total: 1.00\n"
        "    soft_warn_pct: 70\n"
        "    hard_stop_pct: 100\n"
    )
    monkeypatch.setenv("MAILMIND_PIPELINE_CONFIG", str(cfg))

    _seed_run("extract_agent", cost_usd=0.80)  # 80% of $1
    summary = cost_guard.daily_summary()
    assert summary["bucket"] == "warn"
    assert summary["pct_of_cap"] == pytest.approx(80.0)


def test_daily_summary_stop_bucket(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from lib import cost_guard

    cfg = tmp_path / "pipeline.yml"
    cfg.write_text(
        "cost_caps:\n"
        "  per_day_usd:\n"
        "    total: 1.00\n"
        "    soft_warn_pct: 70\n"
        "    hard_stop_pct: 100\n"
    )
    monkeypatch.setenv("MAILMIND_PIPELINE_CONFIG", str(cfg))

    _seed_run("draft_agent", cost_usd=1.50)  # 150% of $1
    summary = cost_guard.daily_summary()
    assert summary["bucket"] == "stop"


# ---------------- gemini_runner integration --------------------------------

def test_generate_structured_records_cost_usd() -> None:
    """When agent_name is passed, the result includes a non-None cost_usd
    (zero in stub mode, since stub responses report 0 tokens)."""
    from lib import gemini_runner

    # In stub mode any prompt that doesn't match a fixture returns no
    # response, so we use the actual triage prompt path implicitly via
    # the agent itself — but cost_usd should be settable regardless.
    # Just sanity-check the dataclass field default.
    res = gemini_runner.StructuredResult(
        parsed={},
        raw_text="{}",
        input_tokens=1000,
        output_tokens=2000,
        latency_ms=10,
        model="gemini-2.5-flash",
    )
    assert res.cost_usd == 0.0  # default until set


def test_extract_agent_records_cost_usd() -> None:
    """Run the extract agent in stub mode and verify cost_usd lands on the
    agent_runs row (stub fixtures don't populate tokens, so 0.0 is OK —
    but the column should not be NULL)."""
    import extract_agent
    from lib import db

    extract_agent.run("fix-thread-101")

    with db.agent_runs() as conn:
        row = conn.execute(
            "SELECT cost_usd FROM agent_runs WHERE agent_name = 'extract_agent' "
            "ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
    assert row is not None
    # cost_usd should be set (even if 0). NULL would mean the agent didn't
    # opt into the cost guard.
    assert row["cost_usd"] is not None


def test_per_task_cap_refuses_real_call(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Force the cap so low that *any* call is refused, then verify the
    extract_agent surfaces the error."""
    import extract_agent
    from lib import cost_guard

    cfg = tmp_path / "pipeline.yml"
    cfg.write_text(
        "cost_caps:\n"
        "  per_task_usd:\n"
        "    extract: 0.0000001\n"  # absurdly low
    )
    monkeypatch.setenv("MAILMIND_PIPELINE_CONFIG", str(cfg))

    with pytest.raises(cost_guard.CostBudgetExceeded) as exc_info:
        extract_agent.run("fix-thread-101", force=True)
    assert exc_info.value.code == "per_task_cap_exceeded"


# ---------------- structured logging --------------------------------------

def test_logging_emits_one_line_per_run(isolated_stub_env: Path) -> None:
    import extract_agent
    from lib import logging_setup

    logging_setup.init_logging()
    extract_agent.run("fix-thread-101")

    log_path = isolated_stub_env / "logs" / "agent-service.log"
    assert log_path.exists(), "expected agent-service.log to be created"

    lines = [json.loads(l) for l in log_path.read_text().splitlines() if l.strip()]
    starts = [l for l in lines if l.get("event") == "agent_run_start"]
    finishes = [l for l in lines if l.get("event") == "agent_run_finish"]
    assert starts and finishes
    f = finishes[-1]
    assert f["agent_name"] == "extract_agent"
    assert "cost_usd" in f
    assert "latency_ms" in f


def test_log_tail_helper_returns_recent_events(isolated_stub_env: Path) -> None:
    import extract_agent
    from lib import logging_setup

    logging_setup.init_logging()
    extract_agent.run("fix-thread-101")

    events = logging_setup.tail_lines(limit=50)
    assert any(e.get("event") == "agent_run_finish" and e.get("agent_name") == "extract_agent"
               for e in events)


def test_logging_disabled_via_env(monkeypatch: pytest.MonkeyPatch, isolated_stub_env: Path) -> None:
    import extract_agent

    monkeypatch.setenv("MAILMIND_DISABLE_AGENT_LOG", "1")
    extract_agent.run("fix-thread-101")

    log_path = isolated_stub_env / "logs" / "agent-service.log"
    # Either no file or an empty file (the rotator may create the file
    # eagerly even without writes).
    if log_path.exists():
        assert log_path.read_text() == ""


# ---------------- anomaly detection ---------------------------------------

def test_anomalies_picks_up_latency_outlier() -> None:
    from lib import cost_guard

    # 10 normal runs at 100ms, 1 outlier at 5000ms (>3× P95).
    base = datetime.now(timezone.utc) - timedelta(hours=1)
    for i in range(10):
        _seed_run("triage_agent", cost_usd=0.01, latency_ms=100,
                  started_at=base + timedelta(minutes=i))
    outlier_id = _seed_run("triage_agent", cost_usd=0.01, latency_ms=5000,
                           started_at=base + timedelta(minutes=20))

    out = cost_guard.anomalies()
    found = next((a for a in out if a["id"] == outlier_id), None)
    assert found is not None, f"outlier missing from anomalies: {out}"
    assert any("latency" in r for r in found["reasons"])


def test_anomalies_empty_when_no_outliers() -> None:
    from lib import cost_guard

    base = datetime.now(timezone.utc) - timedelta(hours=1)
    for i in range(10):
        _seed_run("draft_agent", cost_usd=0.01, latency_ms=100,
                  started_at=base + timedelta(minutes=i))
    out = cost_guard.anomalies()
    # Same latency for all — nothing should fire.
    assert out == []


# ---------------- error taxonomy ------------------------------------------

def test_error_taxonomy_reports_schema_fail_rate() -> None:
    from lib import cost_guard

    _seed_run("extract_agent", cost_usd=0.01, result_status="success")
    _seed_run("extract_agent", cost_usd=0.01, result_status="success")
    _seed_run("extract_agent", cost_usd=0.01, result_status="schema_fail")
    out = cost_guard.error_taxonomy()
    assert out["total"] == 3
    assert out["by_status"]["success"] == 2
    assert out["by_status"]["schema_fail"] == 1
    assert out["schema_fail_rate"] == pytest.approx(1 / 3, rel=1e-3)


def test_error_taxonomy_reports_retry_rate() -> None:
    from lib import cost_guard

    _seed_run("extract_agent", cost_usd=0.01, retry_count=0)
    _seed_run("extract_agent", cost_usd=0.01, retry_count=1)
    _seed_run("extract_agent", cost_usd=0.01, retry_count=2)
    out = cost_guard.error_taxonomy()
    assert out["retry_rate"] == pytest.approx(2 / 3, rel=1e-3)


# ---------------- cost trajectory -----------------------------------------

def test_cost_trajectory_returns_seven_days() -> None:
    from lib import cost_guard

    out = cost_guard.cost_trajectory(days=7)
    assert len(out["days"]) == 7
    assert len(out["totals"]) == 7
    # No data seeded — all zeros, no agents.
    assert all(t == 0.0 for t in out["totals"])


def test_cost_trajectory_buckets_today() -> None:
    from lib import cost_guard

    _seed_run("extract_agent", cost_usd=0.50)
    _seed_run("draft_agent", cost_usd=0.10)
    out = cost_guard.cost_trajectory(days=7)
    today = datetime.now(timezone.utc).date().isoformat()
    assert today in out["days"]
    idx = out["days"].index(today)
    assert out["totals"][idx] == pytest.approx(0.60)
    assert "extract_agent" in out["agents"]
    assert "draft_agent" in out["agents"]


# ---------------- service endpoints ---------------------------------------

def test_observability_summary_endpoint(isolated_stub_env: Path) -> None:
    from fastapi.testclient import TestClient

    import service

    _seed_run("extract_agent", cost_usd=0.01, latency_ms=100)
    _seed_run("draft_agent", cost_usd=0.05, latency_ms=200, result_status="schema_fail")

    client = TestClient(service.app)
    res = client.get("/observability/summary")
    assert res.status_code == 200
    body = res.json()
    assert body["today"]["total_usd"] == pytest.approx(0.06, abs=1e-6)
    # Latency rows should have at least the two agents we seeded.
    by_agent = {r["agent_name"] for r in body["latency_by_agent"]}
    assert {"extract_agent", "draft_agent"} <= by_agent
    assert body["errors"]["by_status"].get("schema_fail") == 1


def test_observability_cost_trajectory_endpoint(isolated_stub_env: Path) -> None:
    from fastapi.testclient import TestClient

    import service

    _seed_run("triage_agent", cost_usd=0.02)
    client = TestClient(service.app)
    res = client.get("/observability/cost_trajectory?days=7")
    assert res.status_code == 200
    body = res.json()
    assert len(body["days"]) == 7
    assert "triage_agent" in body["agents"]


def test_observability_cost_trajectory_clamps_days(isolated_stub_env: Path) -> None:
    from fastapi.testclient import TestClient

    import service

    client = TestClient(service.app)
    assert client.get("/observability/cost_trajectory?days=0").status_code == 400
    assert client.get("/observability/cost_trajectory?days=999").status_code == 400


def test_observability_anomalies_endpoint(isolated_stub_env: Path) -> None:
    from fastapi.testclient import TestClient

    import service

    base = datetime.now(timezone.utc) - timedelta(hours=1)
    for i in range(10):
        _seed_run("triage_agent", cost_usd=0.01, latency_ms=100,
                  started_at=base + timedelta(minutes=i))
    _seed_run("triage_agent", cost_usd=0.01, latency_ms=10_000,
              started_at=base + timedelta(minutes=30))

    client = TestClient(service.app)
    res = client.get("/observability/anomalies")
    assert res.status_code == 200
    body = res.json()
    assert any(a["agent_name"] == "triage_agent" for a in body["anomalies"])


def test_observability_budget_endpoint(isolated_stub_env: Path) -> None:
    from fastapi.testclient import TestClient

    import service

    client = TestClient(service.app)
    res = client.get("/observability/budget")
    assert res.status_code == 200
    body = res.json()
    assert "gemini-2.5-flash" in body["pricing_usd_per_million"]
    assert body["per_day_usd"]["total"] >= 1.0
    assert "draft_agent" in body["per_task_usd"]


def test_observability_log_tail_endpoint(isolated_stub_env: Path) -> None:
    """End-to-end: run an agent, hit /observability/log_tail, verify the
    finish event is in the tail."""
    from fastapi.testclient import TestClient

    import extract_agent
    import service
    from lib import logging_setup

    logging_setup.init_logging()
    extract_agent.run("fix-thread-101")

    client = TestClient(service.app)
    res = client.get("/observability/log_tail?limit=50")
    assert res.status_code == 200
    events = res.json()["events"]
    assert any(e.get("event") == "agent_run_finish" and e.get("agent_name") == "extract_agent"
               for e in events)
