"""Cost guardrails — per-task and per-day USD caps for every agent.
Plus a USD pricing table that turns Gemini token counts into real money.

Loaded on every call from `config/pipeline.yml` (BUILD §20). Sensible
defaults if the file is missing so no agent ever crashes for lack of
config.

Two enforcement points:

1. **Per-task** — the agent (via `gemini_runner.generate_structured`) does
   a pre-flight estimate (input chars/4 + max_output_tokens at full output
   rate) and refuses if the estimate exceeds the cap for its agent_name.
   Cheap to compute, catches a runaway prompt before it hits Vertex.
2. **Per-day** — before any agent invocation, sum today's `cost_usd` from
   `agent_runs.sqlite`. If the total ≥ daily hard-stop, refuse with
   `per_day_cap_exceeded`. Soft-warn at 70% is reported but doesn't block.

Tests + stub mode: real Gemini calls have non-zero cost, but stubbed
responses have `input_tokens=0, output_tokens=0` (the fixtures don't set
them), so cost is ~0 and caps never trigger. Tests that *want* to
exercise the cap path can:
- monkeypatch `cost_guard._load_caps` to lower numbers, OR
- set `MAILMIND_DISABLE_COST_GUARD=1` to bypass entirely (used by some
  acceptance scripts that don't care).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import yaml

from . import db


# ---- defaults (used when config/pipeline.yml is absent) ------------------
# Numbers match config/pipeline.yml so on-disk and in-code stay aligned.

_DEFAULT_PRICING: dict[str, dict[str, float]] = {
    "gemini-2.5-flash": {"input": 0.30, "output": 2.50},
    "gemini-2.5-pro": {"input": 1.25, "output": 10.00},
}

_DEFAULT_PER_TASK_USD: dict[str, float] = {
    "triage_agent": 0.05,
    "extract_agent": 0.02,
    "tagger_agent": 0.005,
    "relationship_agent": 0.05,
    "cadence": 0.00,
    "reconcile": 0.00,
    "query_agent": 0.10,
    "draft_agent": 0.20,
    "eval_agent": 0.02,
    "eval_judge": 0.02,
    "review_agent": 0.02,
    "send_agent": 0.00,
}

_DEFAULT_PER_DAY_USD = {
    "total": 5.00,
    "soft_warn_pct": 70,
    "hard_stop_pct": 100,
}

_DEFAULT_ANOMALIES = {
    "latency_outlier_x_p95": 3.0,
    "cost_outlier_x_p95": 2.0,
    "rolling_window_days": 7,
}


# ---- exceptions ----------------------------------------------------------


class CostBudgetExceeded(Exception):
    """Raised when an agent's pre-flight or daily-rollup total would exceed
    the configured cap. Has a `code` and a `meta` dict so the API layer can
    return structured errors to the UI."""

    def __init__(self, code: str, message: str, *, meta: dict | None = None):
        super().__init__(message)
        self.code = code
        self.meta = meta or {}


# ---- config ---------------------------------------------------------------


def _config_path() -> Path:
    if env := os.environ.get("MAILMIND_PIPELINE_CONFIG"):
        return Path(env).expanduser().resolve()
    # config/pipeline.yml at repo root. agents/lib/cost_guard.py → ../../config
    return Path(__file__).resolve().parents[2] / "config" / "pipeline.yml"


def _load_raw() -> dict:
    p = _config_path()
    if not p.exists():
        return {}
    try:
        return yaml.safe_load(p.read_text()) or {}
    except Exception:  # noqa: BLE001 — never let bad YAML break agents
        return {}


@dataclass(frozen=True)
class Caps:
    pricing: dict[str, dict[str, float]]
    per_task_usd: dict[str, float]
    per_day_usd: dict[str, float]
    anomalies: dict[str, float]


def _load_caps() -> Caps:
    """Always-fresh load — no caching. The config file is small and tests
    monkeypatch env vars between calls. If perf becomes an issue we can
    swap for a 5s TTL cache."""
    raw = _load_raw()
    pricing = raw.get("gemini_pricing_usd_per_million") or _DEFAULT_PRICING
    cost_caps = raw.get("cost_caps") or {}
    per_task_yaml = cost_caps.get("per_task_usd") or {}
    # Merge defaults with YAML — YAML's per_task entries are keyed by short
    # name (`triage`, `extract`), but agent_name in code is `triage_agent`.
    # Translate via a suffix probe: try exact match, then `<name>_agent`.
    per_task: dict[str, float] = dict(_DEFAULT_PER_TASK_USD)
    for k, v in per_task_yaml.items():
        if not isinstance(v, (int, float)):
            continue
        # Accept both `triage` and `triage_agent` as keys.
        per_task[k] = float(v)
        per_task[f"{k}_agent"] = float(v)
    per_day = cost_caps.get("per_day_usd") or {}
    per_day_merged = {**_DEFAULT_PER_DAY_USD, **{k: float(v) for k, v in per_day.items() if isinstance(v, (int, float))}}
    anomalies = raw.get("anomalies") or {}
    anomalies_merged = {**_DEFAULT_ANOMALIES, **{k: float(v) for k, v in anomalies.items() if isinstance(v, (int, float))}}
    return Caps(
        pricing=pricing,
        per_task_usd=per_task,
        per_day_usd=per_day_merged,
        anomalies=anomalies_merged,
    )


# ---- pricing math ---------------------------------------------------------


def actual_cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    """Compute the real USD cost of one Gemini call.

    Returns 0.0 for unknown models — we'd rather under-report than crash on
    a model rename. The Observability tab surfaces "unknown model" as a
    sentinel so the user notices.
    """
    if not input_tokens and not output_tokens:
        return 0.0
    p = _load_caps().pricing.get(model)
    if not p:
        return 0.0
    return ((input_tokens or 0) * p.get("input", 0.0)
            + (output_tokens or 0) * p.get("output", 0.0)) / 1_000_000


def estimate_request_cost_usd(
    *,
    model: str,
    prompt_chars: int,
    system_chars: int = 0,
    max_output_tokens: int,
) -> float:
    """Rough pre-flight estimate. Used by `check_per_task` to refuse a call
    *before* it goes out to Vertex. The token estimate is `chars / 4` —
    deliberately overcounts for English-leaning prompts so a too-loose cap
    surfaces as an estimated overspend, not a surprise on the bill.
    """
    estimated_input_tokens = max(1, (prompt_chars + system_chars) // 4)
    return actual_cost_usd(model, estimated_input_tokens, max_output_tokens)


# ---- guards ---------------------------------------------------------------


def _enforcement_disabled() -> bool:
    return os.environ.get("MAILMIND_DISABLE_COST_GUARD") == "1"


def cap_for(agent_name: str) -> float | None:
    """Lookup the per-task cap. Returns None if no cap is configured —
    callers treat that as "no enforcement"."""
    caps = _load_caps().per_task_usd
    if agent_name in caps:
        return caps[agent_name]
    # Strip `_agent` suffix and retry.
    if agent_name.endswith("_agent"):
        short = agent_name[: -len("_agent")]
        if short in caps:
            return caps[short]
    return None


def check_per_task(agent_name: str, estimate_usd: float) -> None:
    """Raise CostBudgetExceeded if the pre-flight estimate exceeds the
    per-task cap for `agent_name`. No-op if enforcement is disabled or no
    cap is configured."""
    if _enforcement_disabled():
        return
    cap = cap_for(agent_name)
    if cap is None or cap <= 0:
        return
    if estimate_usd > cap:
        raise CostBudgetExceeded(
            "per_task_cap_exceeded",
            f"{agent_name}: estimated ${estimate_usd:.4f} exceeds per-task cap ${cap:.4f}",
            meta={
                "agent_name": agent_name,
                "estimate_usd": round(estimate_usd, 6),
                "cap_usd": cap,
            },
        )


def daily_total_usd(*, day: date | None = None) -> float:
    """Sum cost_usd from agent_runs for `day` (default today, UTC)."""
    day = day or datetime.now(timezone.utc).date()
    start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc).isoformat()
    end = (datetime(day.year, day.month, day.day, tzinfo=timezone.utc) + timedelta(days=1)).isoformat()
    with db.agent_runs() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0.0) AS total FROM agent_runs "
            "WHERE started_at >= ? AND started_at < ?",
            (start, end),
        ).fetchone()
    return float(row["total"] or 0.0) if row else 0.0


def check_per_day(agent_name: str) -> None:
    """Raise CostBudgetExceeded if today's spend ≥ daily hard-stop. No-op
    if enforcement is disabled."""
    if _enforcement_disabled():
        return
    caps = _load_caps().per_day_usd
    total_cap = float(caps.get("total", 0.0))
    if total_cap <= 0:
        return
    spent = daily_total_usd()
    hard_stop = total_cap * float(caps.get("hard_stop_pct", 100.0)) / 100.0
    if spent >= hard_stop:
        raise CostBudgetExceeded(
            "per_day_cap_exceeded",
            f"{agent_name}: today's spend ${spent:.4f} ≥ daily cap ${hard_stop:.4f}",
            meta={
                "agent_name": agent_name,
                "spent_usd": round(spent, 6),
                "cap_usd": hard_stop,
            },
        )


def daily_summary(*, day: date | None = None) -> dict:
    """Aggregate today's spend for the Observability tab.

    Returns:
      total_usd          float — today's total
      cap_usd            float — per_day_usd.total from config
      soft_warn_usd      float — cap × soft_warn_pct%
      hard_stop_usd      float — cap × hard_stop_pct%
      pct_of_cap         float — total / cap × 100 (clipped at 999)
      bucket             "ok" | "warn" | "stop"
      per_agent          [{agent_name, total_usd, runs}]
      day                ISO date
    """
    day = day or datetime.now(timezone.utc).date()
    start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc).isoformat()
    end = (datetime(day.year, day.month, day.day, tzinfo=timezone.utc) + timedelta(days=1)).isoformat()
    caps = _load_caps().per_day_usd
    cap_total = float(caps.get("total", 0.0))
    soft_pct = float(caps.get("soft_warn_pct", 70.0))
    hard_pct = float(caps.get("hard_stop_pct", 100.0))

    with db.agent_runs() as conn:
        total = float(
            conn.execute(
                "SELECT COALESCE(SUM(cost_usd), 0.0) AS t FROM agent_runs "
                "WHERE started_at >= ? AND started_at < ?",
                (start, end),
            ).fetchone()["t"]
            or 0.0
        )
        per_agent_rows = conn.execute(
            "SELECT agent_name, COALESCE(SUM(cost_usd), 0.0) AS total_usd, COUNT(*) AS runs "
            "FROM agent_runs WHERE started_at >= ? AND started_at < ? "
            "GROUP BY agent_name ORDER BY total_usd DESC",
            (start, end),
        ).fetchall()

    soft_warn_usd = cap_total * soft_pct / 100.0
    hard_stop_usd = cap_total * hard_pct / 100.0
    bucket = "ok"
    if cap_total > 0:
        if total >= hard_stop_usd:
            bucket = "stop"
        elif total >= soft_warn_usd:
            bucket = "warn"
    pct = round(total / cap_total * 100.0, 2) if cap_total > 0 else 0.0
    return {
        "day": day.isoformat(),
        "total_usd": round(total, 6),
        "cap_usd": cap_total,
        "soft_warn_usd": round(soft_warn_usd, 6),
        "hard_stop_usd": round(hard_stop_usd, 6),
        "pct_of_cap": min(pct, 999.0),
        "bucket": bucket,
        "per_agent": [
            {"agent_name": r["agent_name"], "total_usd": round(float(r["total_usd"] or 0.0), 6), "runs": r["runs"]}
            for r in per_agent_rows
        ],
    }


# ---- weekly trajectory + anomalies (used by Observability tab) -----------


def cost_trajectory(*, days: int = 7) -> dict:
    """Per-day, per-agent cost over the trailing `days` days (inclusive).

    Returns:
      days        [ISO date]
      agents      [agent_name]
      series      {agent_name: [usd_per_day, ...]}  — same length as `days`
      totals      [usd_per_day]
    """
    end = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    start = end - timedelta(days=days - 1)
    iso_days = [(start + timedelta(days=i)).date().isoformat() for i in range(days)]
    with db.agent_runs() as conn:
        rows = conn.execute(
            """
            SELECT
              substr(started_at, 1, 10) AS day,
              agent_name,
              COALESCE(SUM(cost_usd), 0.0) AS total_usd
            FROM agent_runs
            WHERE started_at >= ? AND started_at < ?
            GROUP BY day, agent_name
            """,
            (start.isoformat(), (end + timedelta(days=1)).isoformat()),
        ).fetchall()

    agents: dict[str, list[float]] = {}
    for r in rows:
        agents.setdefault(r["agent_name"], [0.0] * days)
    for r in rows:
        if r["day"] in iso_days:
            idx = iso_days.index(r["day"])
            agents[r["agent_name"]][idx] = round(float(r["total_usd"] or 0.0), 6)
    totals = [round(sum(s[i] for s in agents.values()), 6) for i in range(days)]
    return {
        "days": iso_days,
        "agents": sorted(agents.keys()),
        "series": {k: agents[k] for k in sorted(agents.keys())},
        "totals": totals,
    }


def latency_distribution() -> list[dict]:
    """Per-agent P50/P95 latency over the rolling anomaly window.

    Uses sqlite ROW_NUMBER + COUNT to compute percentiles; cheap on agents
    runs of any realistic size.
    """
    caps = _load_caps()
    window = int(caps.anomalies.get("rolling_window_days", 7))
    cutoff = (datetime.now(timezone.utc) - timedelta(days=window)).isoformat()
    with db.agent_runs() as conn:
        rows = conn.execute(
            """
            WITH ranked AS (
              SELECT agent_name, latency_ms,
                     ROW_NUMBER() OVER (PARTITION BY agent_name ORDER BY latency_ms) AS rn,
                     COUNT(*)     OVER (PARTITION BY agent_name) AS cnt
              FROM agent_runs
              WHERE latency_ms IS NOT NULL AND started_at >= ?
            )
            SELECT agent_name,
                   MAX(CASE WHEN rn = MAX(1, CAST(cnt * 0.5  AS INTEGER)) THEN latency_ms END) AS p50,
                   MAX(CASE WHEN rn = MAX(1, CAST(cnt * 0.95 AS INTEGER)) THEN latency_ms END) AS p95,
                   COUNT(*) AS runs
            FROM ranked
            GROUP BY agent_name
            ORDER BY agent_name
            """,
            (cutoff,),
        ).fetchall()
    return [
        {
            "agent_name": r["agent_name"],
            "p50_ms": int(r["p50"] or 0),
            "p95_ms": int(r["p95"] or 0),
            "runs": int(r["runs"] or 0),
        }
        for r in rows
    ]


def anomalies(*, limit: int = 50) -> list[dict]:
    """Runs whose latency exceeds Nx the rolling P95 for their agent, or
    whose cost exceeds Mx the rolling P95. Window length and multipliers
    come from config (`anomalies.*`).
    """
    caps = _load_caps()
    window = int(caps.anomalies.get("rolling_window_days", 7))
    lat_x = float(caps.anomalies.get("latency_outlier_x_p95", 3.0))
    cost_x = float(caps.anomalies.get("cost_outlier_x_p95", 2.0))
    cutoff = (datetime.now(timezone.utc) - timedelta(days=window)).isoformat()

    out: list[dict] = []
    with db.agent_runs() as conn:
        agent_rows = conn.execute(
            "SELECT DISTINCT agent_name FROM agent_runs WHERE started_at >= ?",
            (cutoff,),
        ).fetchall()
        for ar in agent_rows:
            agent = ar["agent_name"]
            ranked = conn.execute(
                """
                SELECT id, started_at, latency_ms, cost_usd, result_status
                FROM agent_runs
                WHERE agent_name = ? AND started_at >= ?
                """,
                (agent, cutoff),
            ).fetchall()
            if not ranked:
                continue
            lats = sorted([r["latency_ms"] for r in ranked if r["latency_ms"] is not None])
            costs = sorted([r["cost_usd"] for r in ranked if r["cost_usd"] is not None])
            if not lats and not costs:
                continue
            p95_lat = lats[max(0, int(len(lats) * 0.95) - 1)] if lats else 0
            p95_cost = costs[max(0, int(len(costs) * 0.95) - 1)] if costs else 0.0
            for r in ranked:
                reasons: list[str] = []
                if r["latency_ms"] is not None and p95_lat > 0 and r["latency_ms"] > p95_lat * lat_x:
                    reasons.append(f"latency_{round(r['latency_ms'] / p95_lat, 2)}x_p95")
                if r["cost_usd"] is not None and p95_cost > 0 and r["cost_usd"] > p95_cost * cost_x:
                    reasons.append(f"cost_{round(r['cost_usd'] / p95_cost, 2)}x_p95")
                if reasons:
                    out.append(
                        {
                            "id": r["id"],
                            "agent_name": agent,
                            "started_at": r["started_at"],
                            "latency_ms": r["latency_ms"],
                            "cost_usd": r["cost_usd"],
                            "result_status": r["result_status"],
                            "reasons": reasons,
                        }
                    )
    out.sort(key=lambda r: r["started_at"], reverse=True)
    return out[:limit]


def error_taxonomy(*, days: int | None = None) -> dict:
    """Histogram of `result_status` over the rolling window.

    Includes a derived `schema_fail_rate` (schema_fail / total) and
    `retry_rate` (retry_count > 0 / total) so the Observability tab can
    show them as headline metrics.
    """
    days = days or int(_load_caps().anomalies.get("rolling_window_days", 7))
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with db.agent_runs() as conn:
        rows = conn.execute(
            "SELECT result_status, COUNT(*) AS cnt FROM agent_runs "
            "WHERE started_at >= ? GROUP BY result_status",
            (cutoff,),
        ).fetchall()
        retried = conn.execute(
            "SELECT COUNT(*) AS cnt FROM agent_runs "
            "WHERE started_at >= ? AND COALESCE(retry_count, 0) > 0",
            (cutoff,),
        ).fetchone()
        total_row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM agent_runs WHERE started_at >= ?",
            (cutoff,),
        ).fetchone()

    total = int(total_row["cnt"] or 0)
    by_status = {r["result_status"]: int(r["cnt"]) for r in rows}
    schema_fail = by_status.get("schema_fail", 0) + by_status.get("schema_partial_fail", 0)
    retried_count = int(retried["cnt"] or 0) if retried else 0
    return {
        "window_days": days,
        "total": total,
        "by_status": by_status,
        "schema_fail_rate": round(schema_fail / total, 4) if total else 0.0,
        "retry_rate": round(retried_count / total, 4) if total else 0.0,
    }
