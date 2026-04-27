"""Draft agent — one thread + one human-language intent → one DraftReply row.

P4a: produces drafts and writes them to the local `drafts` table only. No
Gmail call here. The user reviews in the dashboard and clicks Save as Gmail
Draft (which goes through `lib.approval` + `lib.gmail_writer`).

Worker pattern matches extract/relationship/tagger:

  1. Build a payload (thread + facts + rollup + user_style).
  2. Render the prompt via prompts.compose_system_prompt('draft.md', ...).
  3. gemini_runner.generate_structured() — JSON mode.
  4. Validate against `schemas/draft.schema.json`.
  5. Provenance check (no fabricated message_ids in cited_facts).
  6. Compute draft_hash (canonical body sha256) and persist.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

import ulid

from lib import (
    agent_run,
    approval,
    db,
    gemini_runner,
    paths,
    prompts,
    raw_reader,
    schema,
    vertex_config,
)

AGENT_NAME = "draft_agent"


# ---- payload assembly ----------------------------------------------------

def _build_payload(thread_id: str, intent: str) -> dict:
    """Assemble the input payload for the draft prompt."""
    thread = raw_reader.get_thread(thread_id)
    if thread is None:
        raise LookupError(f"thread {thread_id!r} not found in raw.sqlite")

    derived = db.open_derived()
    try:
        facts_row = derived.execute(
            "SELECT facts_json FROM thread_facts WHERE thread_id = ?",
            (thread_id,),
        ).fetchone()
        facts = json.loads(facts_row["facts_json"]) if facts_row and facts_row["facts_json"] else None

        # Pick the rollup of the thread's primary correspondent (the most recent
        # non-user from). Falls back to None — the prompt handles that case.
        recipient = _primary_correspondent(thread)
        rollup = None
        if recipient:
            r = derived.execute(
                "SELECT relationship_summary, tone, cadence, status, tags "
                "FROM contact_rollups WHERE contact_email = ?",
                (recipient,),
            ).fetchone()
            if r is not None:
                rollup = {
                    "contact_email": recipient,
                    "relationship_summary": r["relationship_summary"],
                    "tone": r["tone"],
                    "cadence": r["cadence"],
                    "status": r["status"],
                    "tags": json.loads(r["tags"] or "[]"),
                }
    finally:
        derived.close()

    raw_p = paths.raw_db_path()
    disposition = "unclassified"
    if raw_p.exists():
        raw = sqlite3.connect(f"file:{raw_p}?mode=ro", uri=True)
        raw.row_factory = sqlite3.Row
        try:
            d = raw.execute(
                "SELECT disposition FROM thread_dispositions WHERE thread_id = ?",
                (thread_id,),
            ).fetchone()
            if d is not None:
                disposition = d["disposition"]
        finally:
            raw.close()

    return {
        "intent": intent.strip(),
        "thread": {
            "thread_id": thread["thread_id"],
            "subject": thread["subject"],
            "disposition": disposition,
            "facts": facts,
            "messages": thread["messages"],
        },
        "rollup": rollup,
        "user_style": _load_user_style(),
    }


def _primary_correspondent(thread: dict) -> str | None:
    """Most recent non-user `from`. None if every message is from the user."""
    for m in reversed(thread["messages"]):
        if not m.get("is_from_user"):
            return m.get("from")
    return None


def _load_user_style() -> dict | None:
    """Read `user-style.md` if the user has authored one. Returns None if
    absent so the prompt can fall back to inferred-neutral defaults."""
    style_path = paths.config_dir() / "user-style.md"
    if not style_path.exists():
        return None
    text = style_path.read_text()
    # Cheap key:value parser so this stays a Markdown file the user edits by
    # hand. Lines like `tone: warm` become {tone: warm}.
    out: dict[str, str] = {}
    for line in text.splitlines():
        if ":" not in line or line.strip().startswith("#"):
            continue
        k, v = line.split(":", 1)
        k = k.strip().lower().replace(" ", "_")
        v = v.strip()
        if k and v and not v.startswith("<"):
            out[k] = v
    return out or None


# ---- main entry ----------------------------------------------------------

def run(thread_id: str, intent: str, *, model: str | None = None) -> dict:
    """Generate one draft for `thread_id` informed by `intent`. Returns the
    persisted row + the parsed model output.

    Each invocation creates a fresh `drafts` row even if the same thread was
    drafted before — drafts are versioned implicitly via `created_at`. The
    user can reject the old one in the UI; we don't auto-supersede.
    """
    if not intent or not intent.strip():
        raise ValueError("intent must be non-empty")

    payload = _build_payload(thread_id, intent)
    user_prompt = (
        "Write one DraftReply JSON object for the request below.\n\n"
        f"<request>\n{json.dumps(payload, indent=2)}\n</request>"
    )
    system_instruction = prompts.compose_system_prompt(
        "draft.md",
        schemas={"DRAFT_SCHEMA": "draft.schema.json"},
    )

    model = model or vertex_config.GEMINI_FLASH

    with agent_run.record(AGENT_NAME, model, task_id=thread_id) as run_row:
        result = gemini_runner.generate_structured(
            prompt=user_prompt,
            system_instruction=system_instruction,
            model=model,
            max_output_tokens=2048,
            temperature=0.3,
            agent_name=AGENT_NAME,
        )
        run_row.input_tokens = result.input_tokens
        run_row.output_tokens = result.output_tokens
        run_row.latency_ms = result.latency_ms
        run_row.cost_usd = result.cost_usd
        run_row.stubbed = result.stubbed

        draft = result.parsed
        try:
            schema.validate(draft, "draft.schema.json")
        except schema.SchemaValidationError:
            run_row.result_status = "schema_fail"
            raise

        _check_provenance(draft, payload["thread"])

        if draft["thread_id"] != thread_id:
            raise schema.SchemaValidationError(
                [f"  thread_id mismatch: model returned {draft['thread_id']!r} for input {thread_id!r}"],
                schema_name="draft.schema.json",
            )

        persisted = _persist_draft(
            draft=draft,
            intent=intent,
            model=model,
            agent_run_id=run_row.id,
        )
        run_row.result_status = "success"

    return {
        "draft_id": persisted["id"],
        "thread_id": thread_id,
        "intent": intent,
        "draft": draft,
        "draft_hash": persisted["draft_hash"],
        "stubbed": result.stubbed,
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
        "latency_ms": result.latency_ms,
        "agent_run_id": run_row.id,
    }


# ---- provenance ----------------------------------------------------------

def _check_provenance(draft: dict, thread: dict) -> None:
    valid_ids = {m["message_id"] for m in thread["messages"]}
    problems: list[str] = []
    for i, fact in enumerate(draft.get("cited_facts", [])):
        for mid in fact.get("source_message_ids", []):
            if mid not in valid_ids:
                problems.append(
                    f"  cited_facts[{i}]: fabricated source_message_id {mid!r}"
                )
    in_reply = draft.get("in_reply_to_message_id")
    if in_reply is not None and in_reply not in valid_ids:
        problems.append(
            f"  in_reply_to_message_id {in_reply!r} not in thread"
        )
    if problems:
        raise schema.SchemaValidationError(
            problems, schema_name="draft.schema.json"
        )


# ---- persistence ---------------------------------------------------------

def _persist_draft(
    *,
    draft: dict,
    intent: str,
    model: str,
    agent_run_id: str,
) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    draft_id = str(ulid.new())
    draft_hash = approval.hash_draft(draft)

    with db.derived() as conn:
        conn.execute(
            """
            INSERT INTO drafts (
              id, thread_id, in_reply_to_message_id,
              to_emails, cc_emails, bcc_emails, subject, body,
              draft_hash, rationale, cited_facts_json, confidence,
              intent, created_at, updated_at, status,
              model_version, agent_run_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
            """,
            (
                draft_id,
                draft["thread_id"],
                draft.get("in_reply_to_message_id"),
                json.dumps(draft.get("to_emails", [])),
                json.dumps(draft.get("cc_emails", [])),
                json.dumps(draft.get("bcc_emails", [])),
                draft["subject"],
                draft["body"],
                draft_hash,
                draft["rationale"],
                json.dumps(draft.get("cited_facts", [])),
                draft["confidence"],
                intent,
                now,
                now,
                model,
                agent_run_id,
            ),
        )

    return {"id": draft_id, "draft_hash": draft_hash}


# ---- read APIs (for the service layer) -----------------------------------

def list_drafts(*, status: str | None = None, limit: int = 100) -> list[dict]:
    with db.derived() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM drafts WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                (status, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM drafts ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_draft(draft_id: str) -> dict | None:
    with db.derived() as conn:
        row = conn.execute(
            "SELECT * FROM drafts WHERE id = ?", (draft_id,)
        ).fetchone()
    return _row_to_dict(row) if row else None


class DraftEditError(Exception):
    """Raised when an edit hits a constraint (status not pending, missing fields)."""


def update_draft_body(draft_id: str, *, subject: str | None = None, body: str | None = None,
                      to_emails: list[str] | None = None,
                      cc_emails: list[str] | None = None,
                      bcc_emails: list[str] | None = None) -> dict:
    """Apply user-edits to a draft. Recomputes draft_hash. Refuses unless
    status='pending'."""
    row = _get_draft_row_dict(draft_id)
    if row is None:
        raise LookupError(f"no draft with id={draft_id!r}")
    if row["status"] != "pending":
        raise DraftEditError(
            f"draft {draft_id!r} is in status={row['status']!r}; only pending drafts can be edited."
        )

    new_to = to_emails if to_emails is not None else json.loads(row["to_emails"] or "[]")
    new_cc = cc_emails if cc_emails is not None else json.loads(row["cc_emails"] or "[]")
    new_bcc = bcc_emails if bcc_emails is not None else json.loads(row["bcc_emails"] or "[]")
    new_subject = subject if subject is not None else row["subject"]
    new_body = body if body is not None else row["body"]

    if not new_to:
        raise DraftEditError("to_emails must remain non-empty")
    if not new_subject.strip():
        raise DraftEditError("subject must remain non-empty")
    if not new_body.strip():
        raise DraftEditError("body must remain non-empty")

    new_hash = approval.hash_draft(
        {
            "to_emails": new_to,
            "cc_emails": new_cc,
            "bcc_emails": new_bcc,
            "subject": new_subject,
            "body": new_body,
        }
    )
    now = datetime.now(timezone.utc).isoformat()
    with db.derived() as conn:
        conn.execute(
            "UPDATE drafts SET to_emails = ?, cc_emails = ?, bcc_emails = ?, "
            "subject = ?, body = ?, draft_hash = ?, updated_at = ? "
            "WHERE id = ?",
            (
                json.dumps(new_to),
                json.dumps(new_cc),
                json.dumps(new_bcc),
                new_subject,
                new_body,
                new_hash,
                now,
                draft_id,
            ),
        )
    refreshed = _get_draft_row_dict(draft_id)
    assert refreshed is not None
    return _row_to_dict(refreshed)


def _get_draft_row_dict(draft_id: str) -> dict | None:
    with db.derived() as conn:
        row = conn.execute(
            "SELECT * FROM drafts WHERE id = ?", (draft_id,)
        ).fetchone()
    return dict(row) if row else None


def _row_to_dict(row: Any) -> dict:
    d = dict(row)
    return {
        **d,
        "to_emails": json.loads(d.get("to_emails") or "[]"),
        "cc_emails": json.loads(d.get("cc_emails") or "[]"),
        "bcc_emails": json.loads(d.get("bcc_emails") or "[]"),
        "cited_facts": json.loads(d.get("cited_facts_json") or "[]"),
    }
