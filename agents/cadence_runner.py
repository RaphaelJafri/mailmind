"""Deterministic follow-up tracker. No LLM.

Reads ``raw.sqlite`` (Node-owned), ``derived.sqlite`` (Python-owned), and
``user-context.md`` to classify open threads as overdue / waiting / cold and
flag stale pending next_steps.

Exposed two ways:
- ``compute_followups()`` returns the structured report — called by the
  ``/followups`` and ``/cadence/run`` FastAPI endpoints.
- ``run()`` records an ``agent_runs`` row so the cadence sweep is visible in
  the Observability tab alongside the LLM-driven agents.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from lib import agent_run, cadence, db, prompts, raw_reader  # noqa: F401


def _parse_json_list(blob: str | None) -> list:
    if not blob:
        return []
    try:
        v = json.loads(blob)
    except Exception:
        return []
    return v if isinstance(v, list) else []


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def compute_followups(*, now: str | None = None, user_context_md: str | None = None) -> dict:
    """Build the follow-up report. Read-only."""
    now = now or _now_iso()
    ctx = user_context_md if user_context_md is not None else prompts.load_user_context()

    thresholds = cadence.resolve_thresholds(None)  # config-driven later
    latencies = cadence.parse_reply_latencies(ctx)
    overrides = cadence.parse_important_contact_overrides(ctx)
    ignore_list = cadence.parse_ignore_list(ctx)

    raw = raw_reader._open_ro()  # noqa: SLF001
    derived = db.open_derived()
    try:
        thread_meta: dict[str, dict] = {}
        if raw is not None:
            thread_meta = {
                r["thread_id"]: dict(r)
                for r in raw.execute(
                    """
                    SELECT t.thread_id, t.subject, t.last_message_date, td.disposition
                    FROM threads t
                    JOIN thread_dispositions td USING (thread_id)
                    WHERE td.disposition IN ('keep', 'newsletter')
                    """
                ).fetchall()
            }
        contact_meta: dict[str, dict] = {}
        if raw is not None:
            contact_meta = {
                r["email"]: dict(r)
                for r in raw.execute(
                    "SELECT email, display_name FROM contacts"
                ).fetchall()
            }

        thread_facts: dict[str, dict] = {
            r["thread_id"]: json.loads(r["facts_json"]) if r["facts_json"] else {}
            for r in derived.execute(
                "SELECT thread_id, facts_json FROM thread_facts"
            ).fetchall()
        }

        rollups = [
            {
                "contact_email": r["contact_email"],
                "tags": _parse_json_list(r["tags"]),
                "status": r["status"],
                "source_thread_ids": _parse_json_list(r["source_thread_ids"]),
            }
            for r in derived.execute(
                "SELECT contact_email, tags, status, source_thread_ids FROM contact_rollups"
            ).fetchall()
        ]

        they_owe_you: list[dict] = []
        you_owe_them: list[dict] = []
        seen: set[str] = set()

        for rollup in rollups:
            email = rollup["contact_email"]
            if cadence.is_ignored(email, ignore_list):
                continue
            category = cadence.infer_category(rollup["tags"])
            expected = cadence.pick_latency_days(
                contact_email=email,
                category=category,
                latencies=latencies,
                contact_overrides=overrides,
                thresholds=thresholds,
            )

            for tid in rollup["source_thread_ids"]:
                key = f"{tid}::{email}"
                if key in seen:
                    continue
                seen.add(key)
                meta = thread_meta.get(tid)
                if meta is None:
                    continue
                facts = thread_facts.get(tid)
                if not facts or not facts.get("last_message_from") or not facts.get("last_message_date"):
                    continue
                days_stale = cadence.days_between(facts["last_message_date"], now)
                if days_stale is None:
                    continue
                urgency = cadence.classify_urgency(days_stale, expected, thresholds)
                contact_info = contact_meta.get(email, {})
                entry = {
                    "contact_email": email,
                    "display_name": contact_info.get("display_name"),
                    "thread_id": tid,
                    "subject": meta.get("subject") or "",
                    "last_message_date": facts["last_message_date"],
                    "days_stale": days_stale,
                    "urgency": urgency,
                    "expected_latency_days": expected,
                    "category": category or "default",
                    "rollup_status": rollup["status"],
                }
                if facts["last_message_from"] == "user":
                    they_owe_you.append(entry)
                elif facts["last_message_from"] == "other":
                    you_owe_them.append(entry)

        they_owe_you.sort(key=cadence.compare_for_display)
        you_owe_them.sort(key=cadence.compare_for_display)

        # Stale pending next_steps.
        stale_pending: list[dict] = []
        for r in derived.execute(
            """
            SELECT id, contact_email, description, priority, created_at,
                   updated_at, due_date, confidence, status
            FROM next_steps
            WHERE status = 'pending'
            ORDER BY created_at ASC
            """
        ).fetchall():
            row = dict(r)
            if cadence.is_ignored(row["contact_email"], ignore_list):
                continue
            since = cadence.days_between(row["created_at"], now)
            if since is None or since < thresholds["stale_pending_step_days"]:
                continue
            stale_pending.append({**row, "days_since_created": since})
        stale_pending.sort(key=lambda s: -s["days_since_created"])

        metadata = {
            "they_owe_count": len(they_owe_you),
            "they_owe_overdue": sum(1 for e in they_owe_you if e["urgency"] == "overdue"),
            "you_owe_count": len(you_owe_them),
            "you_owe_overdue": sum(1 for e in you_owe_them if e["urgency"] == "overdue"),
            "stale_pending_steps": len(stale_pending),
            "thresholds_used": thresholds,
            "latencies_used": latencies,
            "trusted_overrides_applied": len(overrides),
            "ignore_list_size": len(ignore_list.emails) + len(ignore_list.domains),
        }

        return {
            "generated_at": now,
            "metadata": metadata,
            "they_owe_you": they_owe_you,
            "you_owe_them": you_owe_them,
            "stale_pending_next_steps": stale_pending,
        }
    finally:
        if raw is not None:
            raw.close()
        derived.close()


def run() -> dict:
    """Public entry point. Records an agent_runs row, returns the report."""
    with agent_run.record(agent_name="cadence_runner", model="deterministic") as ar:
        report = compute_followups()
        ar.result_status = "success"
        return {
            "agent_run_id": ar.id,
            "stubbed": False,
            "report": report,
        }
