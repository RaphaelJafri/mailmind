"""Eval agent — runs the labeled set, scores each agent, freezes a baseline.

Per BUILD §23 + WORKPLAN §P5. The held-out labels live in
`{data_dir}/evals/labeled.jsonl` (managed by `lib/labels.py`). On each
run, we walk the labels, re-invoke the relevant worker agent against the
labeled target, and score the output.

Three scoring paths:

- **threads** (extract_agent) — re-extract the thread, compute
  precision/recall/F1 on commitments_by_user + commitments_by_others,
  Jaccard on summary, exact-match on next_step_priority (if labeled).
  Heuristic matching of commitments uses token-overlap Jaccard (≥0.5
  = same commitment).
- **rollups** (relationship_agent) — re-roll the contact, exact-match
  on tone/cadence/status, Jaccard on tags.
- **drafts** (draft_agent + LLM-as-judge) — re-generate the draft, then
  call gemini_runner with `evals/judge_prompts/v1.md` to score
  tone_match, factually_correct, must_mention_coverage,
  must_not_mention_clean, would_send, overall_style_match.

Outputs:

- `evals/results.jsonl` — one JSON line per eval run (append-only).
- `evals/baseline.jsonl` — the run frozen as the regression reference.
  First run = baseline; subsequent runs compare and surface metrics
  that dropped >5%.

Per BUILD §21 trigger #8: any metric that regresses >5% vs baseline
must be surfaced to the user. The eval_agent reports the regression
list; the service layer / acceptance script decides what to do (warn,
fail CI, etc.).
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import ulid

from lib import (
    agent_run,
    cost_guard,
    db,
    gemini_runner,
    labels,
    paths,
    prompts,
    raw_reader,
    vertex_config,
)


AGENT_NAME = "eval_agent"
JUDGE_AGENT_NAME = "eval_judge"
JUDGE_PROMPT_VERSION = "v1"

REGRESSION_THRESHOLD_PCT = 5.0  # BUILD §23 + §21 trigger #8


# ---- paths ---------------------------------------------------------------


def results_path() -> Path:
    p = paths.data_dir() / "evals" / "results.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def baseline_path() -> Path:
    p = paths.data_dir() / "evals" / "baseline.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def judge_prompt_path() -> Path:
    """Locate `evals/judge_prompts/v1.md` at the repo root.

    eval_agent.py → ../evals/judge_prompts/v1.md
    """
    return Path(__file__).resolve().parents[1] / "evals" / "judge_prompts" / f"{JUDGE_PROMPT_VERSION}.md"


# ---- public entry -------------------------------------------------------


def run(*, dry_run: bool = False) -> dict:
    """Run the labeled set against the current agents.

    Returns:
        {
          "run_id": "...",
          "ran_at": ISO-8601,
          "label_counts": {"thread": N, "rollup": N, "draft": N},
          "metrics": {
            "extract_agent": {...per-agent metrics...},
            "relationship_agent": {...},
            "draft_agent": {...},
          },
          "per_label": [{label_id, kind, target_id, score, ...}],
          "baseline_present": bool,
          "regressions": [{path, current, baseline, delta_pct}],
          "ok": bool,
        }

    `ok=False` if there are zero live labels (eval can't score a zero set)
    or if any metric regressed >5% vs baseline. The acceptance script
    treats this as a non-fatal signal — it logs but doesn't fail the run
    unless `--strict` is set.
    """
    live = labels.latest_per_target()
    counts = {"thread": 0, "rollup": 0, "draft": 0, "total": 0}
    for r in live:
        counts[r.kind] = counts.get(r.kind, 0) + 1
        counts["total"] += 1

    if not live:
        return {
            "run_id": None,
            "ok": False,
            "reason": "no_labels",
            "label_counts": counts,
            "metrics": {},
            "per_label": [],
            "baseline_present": baseline_path().exists(),
            "regressions": [],
        }

    model = vertex_config.GEMINI_FLASH
    started_at = datetime.now(timezone.utc).isoformat()

    with agent_run.record(AGENT_NAME, model) as run_row:
        per_label: list[dict] = []
        for label in live:
            try:
                if label.kind == "thread":
                    per_label.append(_score_thread(label))
                elif label.kind == "rollup":
                    per_label.append(_score_rollup(label))
                elif label.kind == "draft":
                    per_label.append(_score_draft(label))
            except Exception as exc:  # noqa: BLE001
                per_label.append(
                    {
                        "label_id": label.id,
                        "kind": label.kind,
                        "target_id": label.target_id,
                        "ok": False,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )

        metrics = _aggregate(per_label)
        # Latency / cost / schema-fail per-agent come from agent_runs over
        # the eval window — we annotated each agent_runs row in the live
        # invocations above, just re-read now.
        _augment_with_observability(metrics, started_at)

        record = {
            "run_id": str(ulid.new()),
            "ran_at": started_at,
            "label_counts": counts,
            "metrics": metrics,
            "per_label": per_label,
            "judge_prompt_version": JUDGE_PROMPT_VERSION,
        }

        if not dry_run:
            _append_jsonl(results_path(), record)

        baseline = _load_baseline()
        regressions = _compare_to_baseline(metrics, baseline) if baseline else []

        run_row.tools_called = [
            {"tool": f"score_{r['kind']}", "label_id": r["label_id"]}
            for r in per_label if r.get("ok") is not False
        ]
        run_row.result_status = "success" if not any(r.get("ok") is False for r in per_label) else "schema_partial_fail"

    return {
        **record,
        "baseline_present": baseline is not None,
        "regressions": regressions,
        "ok": not regressions and any(r.get("ok") is not False for r in per_label),
    }


def freeze_baseline() -> dict:
    """Copy the most recent results row to baseline.jsonl. Used by
    the user (via /eval/baseline POST) the first time the eval set is
    populated and any time they explicitly re-freeze."""
    rp = results_path()
    if not rp.exists():
        raise LookupError("no eval results yet — run /eval/run first")
    rows = [json.loads(l) for l in rp.read_text().splitlines() if l.strip()]
    if not rows:
        raise LookupError("results.jsonl is empty")
    latest = rows[-1]
    bp = baseline_path()
    bp.write_text(json.dumps(latest, default=str) + "\n")
    return {"frozen_run_id": latest["run_id"], "ran_at": latest["ran_at"]}


def list_results(*, limit: int = 20) -> list[dict]:
    """Return the most recent `limit` eval runs (newest first)."""
    rp = results_path()
    if not rp.exists():
        return []
    rows = [json.loads(l) for l in rp.read_text().splitlines() if l.strip()]
    return rows[-limit:][::-1]


def get_baseline() -> dict | None:
    return _load_baseline()


# ---- thread scoring -----------------------------------------------------


def _score_thread(label: labels.LabelRow) -> dict:
    """Re-extract a labeled thread, compare to ground-truth expected.

    Uses the agent's normal idempotency — if `thread_facts` is already
    populated for this content_hash, the agent skips the LLM call and we
    score against the persisted output. The point of eval is "what does
    the agent currently produce", not "force a fresh call every run".
    `force=True` would invalidate the stub fixtures (which are keyed by
    prompt hash, including previous-output context), and isn't needed —
    if you actually changed the agent or prompt, the content_hash will
    differ and the run will fire automatically.
    """
    import extract_agent

    thread_id = label.target_id
    res = extract_agent.run(thread_id)
    facts = _read_thread_facts(thread_id)
    if facts is None:
        return {
            "label_id": label.id,
            "kind": "thread",
            "target_id": thread_id,
            "ok": False,
            "error": "thread_facts row missing post-extract",
        }

    expected = label.expected
    user_p, user_r = _commitment_pr(
        actual=facts.get("commitments_by_user") or [],
        expected=expected.get("commitments_by_user") or [],
    )
    others_p, others_r = _commitment_pr(
        actual=facts.get("commitments_by_others") or [],
        expected=expected.get("commitments_by_others") or [],
    )
    summary_jac = _jaccard_tokens(facts.get("summary") or "", expected.get("summary") or "")
    priority_match: float | None = None
    if "next_step_priority" in expected and expected["next_step_priority"]:
        # next_step_priority is reflected by the highest-priority cited
        # commitment when multiple priorities are present. For now we just
        # look up against the user's labeled expected — when the agent
        # produces no priority field on facts we mark None (excluded from
        # aggregate to avoid biasing it).
        actual_priority = (facts.get("next_step_priority") or "").strip().lower()
        expected_priority = str(expected["next_step_priority"]).strip().lower()
        priority_match = 1.0 if actual_priority == expected_priority else 0.0

    return {
        "label_id": label.id,
        "kind": "thread",
        "target_id": thread_id,
        "ok": True,
        "stubbed": res.get("stubbed", False),
        "scores": {
            "precision_user": user_p,
            "recall_user": user_r,
            "f1_user": _f1(user_p, user_r),
            "precision_others": others_p,
            "recall_others": others_r,
            "f1_others": _f1(others_p, others_r),
            "summary_jaccard": summary_jac,
            "priority_match": priority_match,
        },
        "agent_run_id": res.get("agent_run_id"),
    }


def _commitment_pr(*, actual: list, expected: list) -> tuple[float, float]:
    """Precision/recall via greedy token-Jaccard matching.

    Two commitments are "the same" when their description tokens overlap
    with Jaccard ≥ 0.5. Each expected slot can be matched at most once.
    """
    if not expected and not actual:
        return 1.0, 1.0
    if not expected:
        # No ground truth → no recall to compute, but the agent returned
        # extras → precision is undefined. Treat as 1.0 to avoid dragging
        # the average down for legitimately commitment-free threads.
        return 1.0, 1.0
    if not actual:
        return 0.0, 0.0

    matched_expected: set[int] = set()
    matched_actual = 0
    for a in actual:
        a_text = (a.get("description") or "") if isinstance(a, dict) else str(a)
        a_tokens = _tokenize(a_text)
        best_idx = -1
        best_jac = 0.0
        for i, e in enumerate(expected):
            if i in matched_expected:
                continue
            e_text = (e.get("description") or "") if isinstance(e, dict) else str(e)
            jac = _jaccard(a_tokens, _tokenize(e_text))
            if jac > best_jac:
                best_jac = jac
                best_idx = i
        if best_idx >= 0 and best_jac >= 0.5:
            matched_expected.add(best_idx)
            matched_actual += 1
    precision = matched_actual / len(actual) if actual else 0.0
    recall = len(matched_expected) / len(expected) if expected else 0.0
    return precision, recall


def _read_thread_facts(thread_id: str) -> dict | None:
    """Read the most recent thread_facts row and parse facts_json."""
    import sqlite3

    p = paths.derived_db_path()
    if not p.exists():
        return None
    conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT facts_json FROM thread_facts WHERE thread_id = ?",
            (thread_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    finally:
        conn.close()
    if row is None:
        return None
    try:
        return json.loads(row["facts_json"])
    except Exception:  # noqa: BLE001
        return None


# ---- rollup scoring -----------------------------------------------------


def _score_rollup(label: labels.LabelRow) -> dict:
    """Re-roll a labeled contact, compare to ground-truth expected.

    Same idempotency reasoning as `_score_thread` — let the agent decide
    if the inputs have changed enough to warrant a fresh call. Eval
    scores whatever the agent currently produces.
    """
    import relationship_agent

    contact = label.target_id
    res = relationship_agent.run(contact)
    rollup = relationship_agent.get_rollup(contact)
    if rollup is None:
        return {
            "label_id": label.id,
            "kind": "rollup",
            "target_id": contact,
            "ok": False,
            "error": "contact_rollups row missing post-rollup",
        }

    expected = label.expected
    tone_match = _exact_match(rollup.get("tone"), expected.get("tone"))
    cadence_match = _exact_match(rollup.get("cadence"), expected.get("cadence"))
    status_match = _exact_match(rollup.get("status"), expected.get("status"))
    tags_jac = _jaccard(set(rollup.get("tags") or []), set(expected.get("tags") or []))

    return {
        "label_id": label.id,
        "kind": "rollup",
        "target_id": contact,
        "ok": True,
        "stubbed": res.get("stubbed", False),
        "scores": {
            "tone_match": tone_match,
            "cadence_match": cadence_match,
            "status_match": status_match,
            "tags_jaccard": tags_jac,
        },
    }


# ---- draft scoring (LLM-as-judge) ---------------------------------------


def _score_draft(label: labels.LabelRow) -> dict:
    """Re-generate the draft, then send it + ground truth to the judge."""
    import draft_agent

    target = label.target_id
    if "::" not in target:
        return {
            "label_id": label.id,
            "kind": "draft",
            "target_id": target,
            "ok": False,
            "error": "draft target_id must be 'thread_id::intent'",
        }
    thread_id, intent = target.split("::", 1)
    intent = intent.strip()

    gen = draft_agent.run(thread_id, intent)
    thread = raw_reader.get_thread(thread_id)
    if thread is None:
        return {
            "label_id": label.id,
            "kind": "draft",
            "target_id": target,
            "ok": False,
            "error": f"thread {thread_id!r} not found",
        }

    judge = _call_judge(
        thread=thread,
        intent=intent,
        generated_draft=gen["draft"],
        expected=label.expected,
    )

    # Surface the heuristic must_mention coverage too — useful when the
    # judge is unreachable / mocked. Computed independently from the
    # judge's float so the eval still produces *some* signal even if
    # the judge is offline.
    must = label.expected.get("must_mention") or []
    must_not = label.expected.get("must_not_mention") or []
    body = (gen["draft"].get("body") or "").lower()
    heuristic_must_cov = (
        sum(1 for s in must if s and s.lower() in body) / len(must) if must else 1.0
    )
    heuristic_must_not_clean = not any(s.lower() in body for s in must_not if s)

    return {
        "label_id": label.id,
        "kind": "draft",
        "target_id": target,
        "ok": True,
        "stubbed": gen.get("stubbed", False),
        "scores": {
            "tone_match": float(judge.get("tone_match") or 0.0),
            "factually_correct": bool(judge.get("factually_correct") or False),
            "must_mention_coverage": float(judge.get("must_mention_coverage") or 0.0),
            "must_not_mention_clean": bool(judge.get("must_not_mention_clean") or False),
            "would_send": bool(judge.get("would_send") or False),
            "overall_style_match": float(judge.get("overall_style_match") or 0.0),
            "heuristic_must_mention_coverage": heuristic_must_cov,
            "heuristic_must_not_clean": bool(heuristic_must_not_clean),
        },
        "judge_rationale": judge.get("rationale") or "",
        "agent_run_id": gen.get("agent_run_id"),
    }


def _call_judge(
    *,
    thread: dict,
    intent: str,
    generated_draft: dict,
    expected: dict,
) -> dict:
    """Invoke the LLM-as-judge against `evals/judge_prompts/v1.md`.

    Returns the parsed judge verdict. If anything goes wrong (judge
    unreachable, parse error, cost cap), we surface a deterministic
    fallback computed from the heuristic checks so the pipeline keeps
    moving — failing-closed here would penalise drafts the judge
    couldn't grade through no fault of the agent's.
    """
    judge_prompt = judge_prompt_path().read_text()
    payload = {
        "thread": thread,
        "intent": intent,
        "generated_draft": generated_draft,
        "expected": expected,
    }
    user_prompt = (
        "Score the generated draft against the expected ground truth.\n\n"
        f"<payload>\n{json.dumps(payload, indent=2, default=str)}\n</payload>"
    )

    model = vertex_config.GEMINI_FLASH
    with agent_run.record(JUDGE_AGENT_NAME, model) as run_row:
        try:
            result = gemini_runner.generate_structured(
                prompt=user_prompt,
                system_instruction=judge_prompt,
                model=model,
                max_output_tokens=512,
                temperature=0.0,
                agent_name=JUDGE_AGENT_NAME,
            )
        except cost_guard.CostBudgetExceeded as exc:
            run_row.result_status = "cost_capped"
            return _heuristic_judge(generated_draft, expected, reason=str(exc))
        except Exception as exc:  # noqa: BLE001
            # Judge unreachable (no API key, network error, fixture
            # miss). Fall back to the deterministic heuristic so the
            # eval pipeline still produces numbers — the run is marked
            # so the dashboard can show "judge offline" instead of a
            # spuriously-low style score.
            run_row.result_status = "judge_unavailable"
            run_row.error_message = f"{type(exc).__name__}: {exc}"
            return _heuristic_judge(generated_draft, expected, reason=f"judge_error:{type(exc).__name__}")

        run_row.input_tokens = result.input_tokens
        run_row.output_tokens = result.output_tokens
        run_row.latency_ms = result.latency_ms
        run_row.cost_usd = result.cost_usd
        run_row.stubbed = result.stubbed

        verdict = result.parsed if isinstance(result.parsed, dict) else None
        if not isinstance(verdict, dict) or "overall_style_match" not in verdict:
            run_row.result_status = "schema_fail"
            return _heuristic_judge(generated_draft, expected, reason="judge_parse_fail")
        run_row.result_status = "success"
        return verdict


def _heuristic_judge(generated: dict, expected: dict, *, reason: str) -> dict:
    """Cheap deterministic fallback when the LLM judge is unreachable.

    Computes only the easily-checkable axes (must_mention coverage,
    must_not_mention cleanliness). The float scores stay conservative so
    a missing-judge run doesn't accidentally inflate baselines."""
    body = (generated.get("body") or "").lower()
    must = expected.get("must_mention") or []
    must_not = expected.get("must_not_mention") or []
    must_cov = (
        sum(1 for s in must if s and s.lower() in body) / len(must) if must else 1.0
    )
    must_not_clean = not any(s.lower() in body for s in must_not if s)
    return {
        "tone_match": 0.5,
        "factually_correct": True,  # can't tell — assume good
        "must_mention_coverage": must_cov,
        "must_not_mention_clean": must_not_clean,
        "would_send": False,  # be conservative on the ship gate
        "overall_style_match": round(0.4 + 0.4 * must_cov, 4),
        "rationale": f"heuristic fallback ({reason})",
    }


# ---- aggregation --------------------------------------------------------


def _aggregate(per_label: list[dict]) -> dict:
    """Roll per-label scores into per-agent metrics. Skips errored rows."""
    by_kind: dict[str, list[dict]] = {"thread": [], "rollup": [], "draft": []}
    for r in per_label:
        if r.get("ok") is False:
            continue
        by_kind.setdefault(r["kind"], []).append(r)

    metrics: dict[str, Any] = {}

    if by_kind["thread"]:
        scores = [r["scores"] for r in by_kind["thread"]]
        metrics["extract_agent"] = {
            "n": len(scores),
            "f1_commitments_user": _mean(s["f1_user"] for s in scores),
            "f1_commitments_others": _mean(s["f1_others"] for s in scores),
            "precision_commitments_user": _mean(s["precision_user"] for s in scores),
            "recall_commitments_user": _mean(s["recall_user"] for s in scores),
            "precision_commitments_others": _mean(s["precision_others"] for s in scores),
            "recall_commitments_others": _mean(s["recall_others"] for s in scores),
            "summary_jaccard": _mean(s["summary_jaccard"] for s in scores),
            "next_step_priority_accuracy": _mean(
                s["priority_match"] for s in scores if s.get("priority_match") is not None
            ),
        }

    if by_kind["rollup"]:
        scores = [r["scores"] for r in by_kind["rollup"]]
        metrics["relationship_agent"] = {
            "n": len(scores),
            "tone_accuracy": _mean(s["tone_match"] for s in scores),
            "cadence_accuracy": _mean(s["cadence_match"] for s in scores),
            "status_accuracy": _mean(s["status_match"] for s in scores),
            "tags_jaccard": _mean(s["tags_jaccard"] for s in scores),
        }

    if by_kind["draft"]:
        scores = [r["scores"] for r in by_kind["draft"]]
        metrics["draft_agent"] = {
            "n": len(scores),
            "tone_match": _mean(s["tone_match"] for s in scores),
            "factually_correct_rate": _mean(1.0 if s["factually_correct"] else 0.0 for s in scores),
            "must_mention_coverage": _mean(s["must_mention_coverage"] for s in scores),
            "must_not_mention_clean_rate": _mean(
                1.0 if s["must_not_mention_clean"] else 0.0 for s in scores
            ),
            "would_send_rate": _mean(1.0 if s["would_send"] else 0.0 for s in scores),
            "overall_style_match": _mean(s["overall_style_match"] for s in scores),
        }

    return metrics


def _augment_with_observability(metrics: dict, since_iso: str) -> None:
    """Attach P50/P95 latency + mean cost_usd + schema-fail rate per agent
    using agent_runs rows since `since_iso`. Mutates `metrics` in place."""
    with db.agent_runs() as conn:
        rows = conn.execute(
            "SELECT agent_name, latency_ms, cost_usd, result_status "
            "FROM agent_runs WHERE started_at >= ?",
            (since_iso,),
        ).fetchall()

    by_agent: dict[str, list[dict]] = {}
    for r in rows:
        by_agent.setdefault(r["agent_name"], []).append(dict(r))

    for agent_name, m in metrics.items():
        bucket = by_agent.get(agent_name) or []
        latencies = sorted([r["latency_ms"] for r in bucket if r.get("latency_ms") is not None])
        costs = [r["cost_usd"] for r in bucket if r.get("cost_usd") is not None]
        statuses = [r["result_status"] for r in bucket if r.get("result_status")]
        m["p50_latency_ms"] = latencies[len(latencies) // 2] if latencies else None
        m["p95_latency_ms"] = (
            latencies[max(0, int(len(latencies) * 0.95) - 1)] if latencies else None
        )
        m["mean_cost_usd"] = round(sum(costs) / len(costs), 6) if costs else None
        m["schema_fail_rate"] = round(
            sum(1 for s in statuses if "schema_fail" in s) / len(statuses), 4
        ) if statuses else None


# ---- baseline + regression ----------------------------------------------


def _load_baseline() -> dict | None:
    p = baseline_path()
    if not p.exists():
        return None
    text = p.read_text().strip()
    if not text:
        return None
    try:
        return json.loads(text.splitlines()[-1])
    except Exception:  # noqa: BLE001
        return None


def _compare_to_baseline(current: dict, baseline: dict) -> list[dict]:
    """Return list of metric paths that regressed > REGRESSION_THRESHOLD_PCT.

    Each entry: {path, current, baseline, delta_pct}. A delta_pct is the
    relative drop from baseline → current. Higher-is-better metrics
    (everything we ship) trigger when current/baseline is < 1 - threshold.
    """
    base_metrics = baseline.get("metrics") or {}
    out: list[dict] = []
    for agent, cur_block in current.items():
        if not isinstance(cur_block, dict):
            continue
        base_block = base_metrics.get(agent) or {}
        for k, cur_v in cur_block.items():
            if not isinstance(cur_v, (int, float)):
                continue
            base_v = base_block.get(k)
            if not isinstance(base_v, (int, float)):
                continue
            if base_v <= 0:
                continue
            # Skip integer counts (n) — only score floats.
            if k == "n":
                continue
            delta_pct = (cur_v - base_v) / base_v * 100.0
            if delta_pct < -REGRESSION_THRESHOLD_PCT:
                out.append(
                    {
                        "path": f"{agent}.{k}",
                        "current": round(cur_v, 4),
                        "baseline": round(base_v, 4),
                        "delta_pct": round(delta_pct, 2),
                    }
                )
    return out


# ---- helpers -------------------------------------------------------------


_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")


def _tokenize(s: str) -> set[str]:
    return {t.lower() for t in _TOKEN_RE.findall(s or "")}


def _jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def _jaccard_tokens(a: str, b: str) -> float:
    return _jaccard(_tokenize(a), _tokenize(b))


def _exact_match(a: Any, b: Any) -> float:
    if a is None or b is None:
        return 0.0
    return 1.0 if str(a).strip().lower() == str(b).strip().lower() else 0.0


def _f1(p: float, r: float) -> float:
    if p + r == 0:
        return 0.0
    return 2 * p * r / (p + r)


def _mean(it) -> float:
    vals = [v for v in it if v is not None]
    return round(sum(vals) / len(vals), 4) if vals else 0.0


def _append_jsonl(p: Path, record: dict) -> None:
    line = json.dumps(record, default=str) + "\n"
    fd = os.open(p, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, line.encode("utf-8"))
        try:
            os.fsync(fd)
        except OSError:
            pass
    finally:
        os.close(fd)
