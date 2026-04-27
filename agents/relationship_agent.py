"""Relationship agent — one external contact → one ContactRollup row.

Reads every extracted thread tied to a contact (plus prior corrections + the
last rollup) and produces a relationship summary, tone/cadence/status/tags,
and a capped list of `draft_next_steps` that will be reconciled into
`next_steps` by `reconcile.py`.

Worker pattern (same shape as triage/extract/tagger): structured-output
Gemini call, post-hoc jsonschema validation, provenance check that every
cited thread/message ID exists in the input payload.

Idempotency: keyed on `(thread_facts content_hashes ⊕ correction_ids ⊕
user_context_hash)`. If nothing relevant changed, skip without calling
Gemini.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone

from lib import (
    agent_run,
    db,
    gemini_runner,
    paths,
    prompts,
    raw_reader,
    schema,
    vertex_config,
)

AGENT_NAME = "relationship_agent"


# ---- candidate enumeration ------------------------------------------------

def list_candidates(*, contact: str | None = None) -> list[dict]:
    """External senders with at least one extracted thread on a keep/newsletter
    disposition. Sorted thread_count DESC, contact ASC for stable hashing.

    Each entry: ``{contact_email, thread_count, threads: [{thread_id, subject,
    last_message_date, disposition, facts_content_hash}]}``.
    """
    raw_p = paths.raw_db_path()
    if not raw_p.exists():
        return []
    raw = sqlite3.connect(f"file:{raw_p}?mode=ro", uri=True)
    raw.row_factory = sqlite3.Row
    derived = db.open_derived()
    try:
        rows = raw.execute(
            f"""
            SELECT m.from_email AS contact_email,
                   t.thread_id, t.subject, t.last_message_date,
                   t.content_hash AS thread_content_hash,
                   td.disposition
            FROM messages m
            JOIN threads t ON t.thread_id = m.thread_id
            JOIN thread_dispositions td ON td.thread_id = t.thread_id
            WHERE m.is_from_user = 0
              AND td.disposition IN ('keep','newsletter')
              {"AND m.from_email = ?" if contact else ""}
            GROUP BY m.from_email, t.thread_id
            """,
            (contact,) if contact else (),
        ).fetchall()

        by_contact: dict[str, list[dict]] = {}
        all_thread_ids: set[str] = set()
        for r in rows:
            by_contact.setdefault(r["contact_email"], []).append(
                {
                    "thread_id": r["thread_id"],
                    "subject": r["subject"],
                    "last_message_date": r["last_message_date"],
                    "thread_content_hash": r["thread_content_hash"],
                    "disposition": r["disposition"],
                }
            )
            all_thread_ids.add(r["thread_id"])
        if not all_thread_ids:
            return []

        placeholders = ",".join("?" for _ in all_thread_ids)
        facts_rows = derived.execute(
            f"SELECT thread_id, content_hash FROM thread_facts "
            f"WHERE thread_id IN ({placeholders})",
            tuple(all_thread_ids),
        ).fetchall()
        facts_by_id = {f["thread_id"]: f["content_hash"] for f in facts_rows}

        candidates: list[dict] = []
        for email, threads in by_contact.items():
            with_facts = [
                {**t, "facts_content_hash": facts_by_id[t["thread_id"]]}
                for t in threads
                if t["thread_id"] in facts_by_id
            ]
            if not with_facts:
                continue
            with_facts.sort(key=lambda t: t["last_message_date"] or "")
            candidates.append(
                {
                    "contact_email": email,
                    "thread_count": len(with_facts),
                    "threads": with_facts,
                }
            )
        candidates.sort(key=lambda c: (-c["thread_count"], c["contact_email"]))
        return candidates
    finally:
        raw.close()
        derived.close()


def _hash_user_context() -> str:
    return hashlib.sha256(prompts.load_user_context().encode("utf-8")).hexdigest()


def _content_hash_for(candidate: dict, correction_ids: list[str], user_context_hash: str) -> str:
    h = hashlib.sha256()
    for t in sorted(candidate["threads"], key=lambda t: t["thread_id"]):
        h.update(b"\x00")
        h.update(t["thread_id"].encode())
        h.update(b"::")
        h.update((t["facts_content_hash"] or "").encode())
    for cid in sorted(correction_ids):
        h.update(b"\x01")
        h.update(cid.encode())
    h.update(b"\x02")
    h.update(user_context_hash.encode())
    return h.hexdigest()


# ---- per-contact run ------------------------------------------------------

def run(contact_email: str, *, force: bool = False) -> dict:
    candidates = list_candidates(contact=contact_email)
    if not candidates:
        raise LookupError(f"no extractable threads for {contact_email!r}")
    candidate = candidates[0]

    correction_ids = _correction_ids_for(contact_email)
    user_context_hash = _hash_user_context()
    content_hash = _content_hash_for(candidate, correction_ids, user_context_hash)

    existing = _existing_rollup_hash(contact_email)
    if not force and existing == content_hash:
        return {
            "contact_email": contact_email,
            "skipped": True,
            "reason": "content_hash unchanged",
        }

    payload = _build_payload(candidate, correction_ids)
    user_prompt = (
        "Roll up this contact. Return one ContactRollup JSON object.\n\n"
        f"<contact>\n{json.dumps(payload, indent=2)}\n</contact>"
    )
    system_instruction = prompts.compose_system_prompt(
        "rollup.md",
        schemas={"CONTACT_ROLLUP_SCHEMA": "contact-rollup.schema.json"},
    )

    model = vertex_config.GEMINI_FLASH

    with agent_run.record(AGENT_NAME, model, task_id=contact_email) as run_row:
        result = gemini_runner.generate_structured(
            prompt=user_prompt,
            system_instruction=system_instruction,
            model=model,
            max_output_tokens=4096,
        )
        run_row.input_tokens = result.input_tokens
        run_row.output_tokens = result.output_tokens
        run_row.latency_ms = result.latency_ms
        run_row.stubbed = result.stubbed

        rollup = result.parsed
        try:
            schema.validate(rollup, "contact-rollup.schema.json")
        except schema.SchemaValidationError:
            run_row.result_status = "schema_fail"
            raise

        _check_provenance(rollup, payload)

        _persist_rollup(
            contact_email=contact_email,
            content_hash=content_hash,
            rollup=rollup,
            model=model,
            agent_run_id=run_row.id,
        )
        run_row.result_status = "success"

    return {
        "contact_email": contact_email,
        "skipped": False,
        "stubbed": result.stubbed,
        "tone": rollup["tone"],
        "cadence": rollup["cadence"],
        "status": rollup["status"],
        "draft_next_steps_count": len(rollup.get("draft_next_steps", [])),
        "confidence": rollup["confidence"],
    }


def run_all(*, force: bool = False) -> dict:
    """Sweep every candidate contact. Returns a summary."""
    results: list[dict] = []
    failures: list[dict] = []
    for c in list_candidates():
        try:
            results.append(run(c["contact_email"], force=force))
        except Exception as exc:  # noqa: BLE001
            failures.append(
                {"contact_email": c["contact_email"], "error": f"{type(exc).__name__}: {exc}"}
            )
    return {
        "rolled_up": results,
        "failures": failures,
        "count": len(results),
        "failed": len(failures),
    }


# ---- helpers --------------------------------------------------------------

def _build_payload(candidate: dict, correction_ids: list[str]) -> dict:
    raw_p = paths.raw_db_path()
    raw = sqlite3.connect(f"file:{raw_p}?mode=ro", uri=True)
    raw.row_factory = sqlite3.Row
    derived = db.open_derived()
    try:
        contact_row = raw.execute(
            "SELECT email, display_name, first_seen, last_seen, message_count "
            "FROM contacts WHERE email = ?",
            (candidate["contact_email"],),
        ).fetchone()

        thread_facts = []
        for t in candidate["threads"]:
            row = derived.execute(
                "SELECT facts_json FROM thread_facts WHERE thread_id = ?",
                (t["thread_id"],),
            ).fetchone()
            facts = json.loads(row["facts_json"]) if row else {}
            thread_facts.append(
                {
                    "thread_id": t["thread_id"],
                    "subject": t["subject"] or "",
                    "last_message_date": t["last_message_date"],
                    "disposition": t["disposition"],
                    "facts": facts,
                }
            )

        corrections = []
        if correction_ids:
            placeholders = ",".join("?" for _ in correction_ids)
            corrections = [
                dict(r)
                for r in derived.execute(
                    f"SELECT id, timestamp, entity_type, entity_id, field, "
                    f"old_value, new_value, user_note "
                    f"FROM corrections WHERE id IN ({placeholders}) "
                    f"ORDER BY timestamp ASC",
                    tuple(correction_ids),
                ).fetchall()
            ]

        previous = derived.execute(
            "SELECT relationship_summary, tone, cadence, status, tags, confidence "
            "FROM contact_rollups WHERE contact_email = ?",
            (candidate["contact_email"],),
        ).fetchone()
        previous_rollup = (
            {
                **dict(previous),
                "tags": json.loads(previous["tags"]) if previous["tags"] else [],
            }
            if previous
            else None
        )

        return {
            "contact_email": candidate["contact_email"],
            "display_name": contact_row["display_name"] if contact_row else None,
            "first_seen": contact_row["first_seen"] if contact_row else None,
            "last_seen": contact_row["last_seen"] if contact_row else None,
            "message_count": contact_row["message_count"] if contact_row else 0,
            "thread_facts": thread_facts,
            "corrections": corrections,
            "previous_rollup": previous_rollup,
        }
    finally:
        raw.close()
        derived.close()


def _correction_ids_for(contact_email: str) -> list[str]:
    derived = db.open_derived()
    try:
        rows = derived.execute(
            "SELECT id FROM corrections "
            "WHERE contact_email IS NULL OR contact_email = ? "
            "ORDER BY timestamp ASC",
            (contact_email,),
        ).fetchall()
        return [r["id"] for r in rows]
    finally:
        derived.close()


def _existing_rollup_hash(contact_email: str) -> str | None:
    derived = db.open_derived()
    try:
        row = derived.execute(
            "SELECT content_hash FROM contact_rollups WHERE contact_email = ?",
            (contact_email,),
        ).fetchone()
        return row["content_hash"] if row else None
    finally:
        derived.close()


def _check_provenance(rollup: dict, payload: dict) -> None:
    valid_thread_ids = {t["thread_id"] for t in payload["thread_facts"]}
    valid_message_ids: set[str] = set()
    for t in payload["thread_facts"]:
        for key in ("commitments_by_user", "commitments_by_others", "open_questions"):
            for entry in (t.get("facts") or {}).get(key, []) or []:
                for mid in entry.get("source_message_ids", []):
                    valid_message_ids.add(mid)

    problems: list[str] = []
    for tid in rollup.get("source_thread_ids", []):
        if tid not in valid_thread_ids:
            problems.append(f"  source_thread_id {tid!r} not in input")

    for i, step in enumerate(rollup.get("draft_next_steps", [])):
        for tid in step.get("source_thread_ids", []):
            if tid not in valid_thread_ids:
                problems.append(
                    f"  draft_next_steps[{i}]: source_thread_id {tid!r} not in input"
                )
        for mid in step.get("source_message_ids", []):
            if mid not in valid_message_ids:
                problems.append(
                    f"  draft_next_steps[{i}]: fabricated source_message_id {mid!r}"
                )

    if problems:
        raise schema.SchemaValidationError(
            problems, schema_name="contact-rollup.schema.json"
        )


def _persist_rollup(
    *,
    contact_email: str,
    content_hash: str,
    rollup: dict,
    model: str,
    agent_run_id: str,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    derived = db.open_derived()
    try:
        derived.execute(
            """
            INSERT INTO contact_rollups (
              contact_email, content_hash, rolled_up_at, model_version,
              relationship_summary, tone, cadence, status, tags,
              source_thread_ids, source_correction_ids, confidence, agent_run_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(contact_email) DO UPDATE SET
              content_hash         = excluded.content_hash,
              rolled_up_at         = excluded.rolled_up_at,
              model_version        = excluded.model_version,
              relationship_summary = excluded.relationship_summary,
              tone                 = excluded.tone,
              cadence              = excluded.cadence,
              status               = excluded.status,
              tags                 = excluded.tags,
              source_thread_ids    = excluded.source_thread_ids,
              source_correction_ids= excluded.source_correction_ids,
              confidence           = excluded.confidence,
              agent_run_id         = excluded.agent_run_id
            """,
            (
                contact_email,
                content_hash,
                now,
                model,
                rollup["relationship_summary"],
                rollup["tone"],
                rollup["cadence"],
                rollup["status"],
                json.dumps(rollup.get("tags", [])),
                json.dumps(rollup.get("source_thread_ids", [])),
                json.dumps(rollup.get("source_correction_ids", [])),
                rollup["confidence"],
                agent_run_id,
            ),
        )

        derived.execute(
            """
            INSERT INTO contact_rollup_drafts (
              contact_email, rolled_up_at, model_version, drafts_json
            ) VALUES (?, ?, ?, ?)
            ON CONFLICT(contact_email) DO UPDATE SET
              rolled_up_at  = excluded.rolled_up_at,
              model_version = excluded.model_version,
              drafts_json   = excluded.drafts_json
            """,
            (
                contact_email,
                now,
                model,
                json.dumps(rollup.get("draft_next_steps", [])),
            ),
        )
        derived.commit()
    finally:
        derived.close()


# ---- read APIs (used by service) ------------------------------------------

def list_rollups(limit: int = 100) -> list[dict]:
    derived = db.open_derived()
    try:
        rows = derived.execute(
            "SELECT contact_email, relationship_summary, tone, cadence, "
            "status, tags, source_thread_ids, confidence, rolled_up_at, model_version "
            "FROM contact_rollups ORDER BY rolled_up_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        derived.close()
    return [
        {
            **dict(r),
            "tags": json.loads(r["tags"] or "[]"),
            "source_thread_ids": json.loads(r["source_thread_ids"] or "[]"),
        }
        for r in rows
    ]


def get_rollup(contact_email: str) -> dict | None:
    derived = db.open_derived()
    try:
        row = derived.execute(
            "SELECT * FROM contact_rollups WHERE contact_email = ?",
            (contact_email,),
        ).fetchone()
    finally:
        derived.close()
    if row is None:
        return None
    return {
        **dict(row),
        "tags": json.loads(row["tags"] or "[]"),
        "source_thread_ids": json.loads(row["source_thread_ids"] or "[]"),
        "source_correction_ids": json.loads(row["source_correction_ids"] or "[]"),
    }
