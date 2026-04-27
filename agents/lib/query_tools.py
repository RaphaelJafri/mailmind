"""Read-only data tools shared by the Query agent and the MCP server.

Every function here is a pure read against `derived.sqlite` + `raw.sqlite`. No
LLM. No mutation. Each returns plain-Python dict/list payloads (JSON-safe) so
the same code services both:

- `agents/query_agent.py` ReAct loop
- `mcp/server.py` stdio JSON-RPC

If you add a new tool, register it in:
1. `TOOLS` (this file's manifest)
2. The README in `mcp/README.md`
3. The system prompt in `prompts/query.md`
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any, Callable

from . import db, paths


# ---- helpers --------------------------------------------------------------

def _open_raw_ro() -> sqlite3.Connection | None:
    p = paths.raw_db_path()
    if not p.exists():
        return None
    conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _parse_list(blob: str | None) -> list:
    if not blob:
        return []
    try:
        v = json.loads(blob)
    except Exception:
        return []
    return v if isinstance(v, list) else []


# ---- search_contacts -------------------------------------------------------

def search_contacts(query: str = "", limit: int = 20) -> list[dict]:
    """Substring + tag match across rolled-up contacts.

    `query=""` returns the most recently rolled-up contacts (top `limit`).
    Otherwise matches on `contact_email` (substring), `tags` (exact, lowercase),
    or `relationship_summary` (substring, case-insensitive).
    """
    needle = (query or "").strip().lower()
    derived = db.open_derived()
    raw = _open_raw_ro()
    try:
        rows = derived.execute(
            "SELECT contact_email, relationship_summary, tone, cadence, status, "
            "tags, source_thread_ids, confidence, rolled_up_at "
            "FROM contact_rollups ORDER BY rolled_up_at DESC LIMIT ?",
            (max(1, min(limit, 100)),),
        ).fetchall()

        contact_meta: dict[str, str | None] = {}
        if raw is not None:
            for r in raw.execute(
                "SELECT email, display_name FROM contacts"
            ).fetchall():
                contact_meta[r["email"]] = r["display_name"]
    finally:
        derived.close()
        if raw is not None:
            raw.close()

    out: list[dict] = []
    for r in rows:
        tags = _parse_list(r["tags"])
        display_name = contact_meta.get(r["contact_email"])
        if needle:
            haystack = " ".join(
                [
                    r["contact_email"] or "",
                    display_name or "",
                    r["relationship_summary"] or "",
                    " ".join(tags),
                ]
            ).lower()
            if needle not in haystack:
                continue
        out.append(
            {
                "contact_email": r["contact_email"],
                "display_name": display_name,
                "relationship_summary": r["relationship_summary"],
                "tone": r["tone"],
                "cadence": r["cadence"],
                "status": r["status"],
                "tags": tags,
                "thread_count": len(_parse_list(r["source_thread_ids"])),
                "confidence": r["confidence"],
            }
        )
    return out


# ---- get_rollup -----------------------------------------------------------

def get_rollup(email: str) -> dict | None:
    """Full rollup row + pending draft_next_steps (drafts table)."""
    if not email:
        return None
    derived = db.open_derived()
    try:
        row = derived.execute(
            "SELECT * FROM contact_rollups WHERE contact_email = ?",
            (email,),
        ).fetchone()
        if row is None:
            return None
        drafts_row = derived.execute(
            "SELECT drafts_json, rolled_up_at FROM contact_rollup_drafts "
            "WHERE contact_email = ?",
            (email,),
        ).fetchone()
        steps = derived.execute(
            "SELECT id, description, priority, due_date, status, created_at, "
            "confidence, source_thread_ids, source_message_ids "
            "FROM next_steps WHERE contact_email = ? AND status = 'pending' "
            "ORDER BY created_at DESC",
            (email,),
        ).fetchall()
    finally:
        derived.close()
    return {
        "contact_email": row["contact_email"],
        "relationship_summary": row["relationship_summary"],
        "tone": row["tone"],
        "cadence": row["cadence"],
        "status": row["status"],
        "tags": _parse_list(row["tags"]),
        "source_thread_ids": _parse_list(row["source_thread_ids"]),
        "confidence": row["confidence"],
        "rolled_up_at": row["rolled_up_at"],
        "draft_next_steps": json.loads(drafts_row["drafts_json"]) if drafts_row and drafts_row["drafts_json"] else [],
        "pending_next_steps": [
            {
                "id": s["id"],
                "description": s["description"],
                "priority": s["priority"],
                "due_date": s["due_date"],
                "created_at": s["created_at"],
                "confidence": s["confidence"],
                "source_thread_ids": _parse_list(s["source_thread_ids"]),
                "source_message_ids": _parse_list(s["source_message_ids"]),
            }
            for s in steps
        ],
    }


# ---- list_overdue ---------------------------------------------------------

def list_overdue(window_days: int | None = None) -> dict:
    """Cadence report filtered to overdue/cold entries.

    Imports here to avoid a circular import — `cadence_runner` already imports
    `lib.cadence` and `lib.db`, and `query_tools` is imported by `service.py`
    above the agents.
    """
    from cadence_runner import compute_followups  # noqa: WPS433

    report = compute_followups()
    keep = {"overdue", "cold"}

    def _filter(entries: list[dict]) -> list[dict]:
        out = [e for e in entries if e["urgency"] in keep]
        if window_days is not None:
            out = [e for e in out if e["days_stale"] <= window_days]
        return out

    # NOTE: we deliberately drop `report["generated_at"]` (wall-clock now)
    # from the tool result — it would otherwise leak nondeterminism into the
    # ReAct transcript and break stub-mode hash matching. The model doesn't
    # need it; if the UI ever wants the timestamp it should call /followups
    # directly.
    return {
        "they_owe_you": _filter(report["they_owe_you"]),
        "you_owe_them": _filter(report["you_owe_them"]),
        "stale_pending_steps": report["stale_pending_next_steps"],
        "thresholds_used": report["metadata"]["thresholds_used"],
    }


# ---- get_thread -----------------------------------------------------------

def get_thread(thread_id: str) -> dict | None:
    """Thread metadata + extracted facts. Read-only join across raw + derived."""
    if not thread_id:
        return None
    raw = _open_raw_ro()
    derived = db.open_derived()
    try:
        thread_meta = None
        if raw is not None:
            r = raw.execute(
                "SELECT t.thread_id, t.subject, t.message_count, t.last_message_date, "
                "td.disposition "
                "FROM threads t LEFT JOIN thread_dispositions td USING(thread_id) "
                "WHERE t.thread_id = ?",
                (thread_id,),
            ).fetchone()
            if r is not None:
                thread_meta = dict(r)
        facts_row = derived.execute(
            "SELECT facts_json, content_hash, extracted_at, model_version "
            "FROM thread_facts WHERE thread_id = ?",
            (thread_id,),
        ).fetchone()
    finally:
        if raw is not None:
            raw.close()
        derived.close()

    if thread_meta is None and facts_row is None:
        return None
    facts = json.loads(facts_row["facts_json"]) if facts_row and facts_row["facts_json"] else None
    return {
        "thread_id": thread_id,
        "subject": (thread_meta or {}).get("subject"),
        "message_count": (thread_meta or {}).get("message_count"),
        "last_message_date": (thread_meta or {}).get("last_message_date"),
        "disposition": (thread_meta or {}).get("disposition"),
        "facts": facts,
        "extracted_at": facts_row["extracted_at"] if facts_row else None,
    }


# ---- get_pending_drafts ---------------------------------------------------

def get_pending_drafts(limit: int = 50) -> list[dict]:
    """All `next_steps` rows in `pending` status. Maps to BUILD §10's
    `get_pending_drafts` tool name (P4 will distinguish reply-drafts from
    next-step proposals)."""
    derived = db.open_derived()
    try:
        rows = derived.execute(
            "SELECT id, contact_email, description, priority, due_date, "
            "source_thread_ids, source_message_ids, created_at, confidence "
            "FROM next_steps WHERE status = 'pending' "
            "ORDER BY created_at DESC LIMIT ?",
            (max(1, min(limit, 200)),),
        ).fetchall()
    finally:
        derived.close()
    return [
        {
            "id": r["id"],
            "contact_email": r["contact_email"],
            "description": r["description"],
            "priority": r["priority"],
            "due_date": r["due_date"],
            "source_thread_ids": _parse_list(r["source_thread_ids"]),
            "source_message_ids": _parse_list(r["source_message_ids"]),
            "created_at": r["created_at"],
            "confidence": r["confidence"],
        }
        for r in rows
    ]


# ---- list_threads_by_tag --------------------------------------------------

def list_threads_by_tag(tag_kind: str, tag_value: str, limit: int = 50) -> list[dict]:
    """Threads carrying ≥1 message tagged `(tag_kind, tag_value)`. Joins
    `message_tags` (Python-owned) → `messages` (Node-owned) → `threads`."""
    if not tag_kind or not tag_value:
        return []
    derived = db.open_derived()
    raw = _open_raw_ro()
    try:
        tag_rows = derived.execute(
            "SELECT message_id, confidence FROM message_tags "
            "WHERE tag_kind = ? AND tag_value = ?",
            (tag_kind, tag_value),
        ).fetchall()
    finally:
        derived.close()
    if not tag_rows:
        return []
    if raw is None:
        return [{"message_id": r["message_id"], "confidence": r["confidence"]} for r in tag_rows]
    placeholders = ",".join("?" for _ in tag_rows)
    msg_ids = [r["message_id"] for r in tag_rows]
    try:
        msg_rows = raw.execute(
            f"SELECT m.message_id, m.thread_id, m.from_email, m.internal_date, "
            f"t.subject "
            f"FROM messages m JOIN threads t ON t.thread_id = m.thread_id "
            f"WHERE m.message_id IN ({placeholders})",
            tuple(msg_ids),
        ).fetchall()
    finally:
        raw.close()
    confidences = {r["message_id"]: r["confidence"] for r in tag_rows}
    seen: set[str] = set()
    out: list[dict] = []
    for m in msg_rows:
        if m["thread_id"] in seen:
            continue
        seen.add(m["thread_id"])
        out.append(
            {
                "thread_id": m["thread_id"],
                "subject": m["subject"],
                "message_id": m["message_id"],
                "from_email": m["from_email"],
                "last_message_date": m["internal_date"],
                "confidence": confidences.get(m["message_id"]),
            }
        )
        if len(out) >= limit:
            break
    return out


# ---- manifest -------------------------------------------------------------

@dataclass(frozen=True)
class ToolDef:
    name: str
    description: str
    json_schema: dict  # MCP-compatible inputSchema
    fn: Callable[..., Any]


TOOLS: list[ToolDef] = [
    ToolDef(
        name="search_contacts",
        description=(
            "Substring + tag match across rolled-up contacts. Returns a list of "
            "summary cards (email, name, tone, cadence, status, tags). Empty query "
            "returns the top 20 most recent rollups."
        ),
        json_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Free-text needle"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
            },
        },
        fn=search_contacts,
    ),
    ToolDef(
        name="get_rollup",
        description=(
            "Full ContactRollup for one contact, plus draft and pending next-step "
            "lists. Returns null if no rollup on file."
        ),
        json_schema={
            "type": "object",
            "properties": {"email": {"type": "string", "format": "email"}},
            "required": ["email"],
        },
        fn=get_rollup,
    ),
    ToolDef(
        name="list_overdue",
        description=(
            "Cadence tracker output filtered to urgency in {overdue, cold}. "
            "Optional window_days clips to threads with days_stale ≤ that bound."
        ),
        json_schema={
            "type": "object",
            "properties": {
                "window_days": {"type": "integer", "minimum": 1, "maximum": 365},
            },
        },
        fn=list_overdue,
    ),
    ToolDef(
        name="get_thread",
        description=(
            "Thread metadata + extracted facts (commitments, open_questions, "
            "deadlines) for one thread_id. Read-only — no body text."
        ),
        json_schema={
            "type": "object",
            "properties": {"thread_id": {"type": "string"}},
            "required": ["thread_id"],
        },
        fn=get_thread,
    ),
    ToolDef(
        name="get_pending_drafts",
        description=(
            "All next_steps rows in 'pending' status. Each carries description, "
            "priority, due_date, source thread/message IDs, and confidence."
        ),
        json_schema={
            "type": "object",
            "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50}},
        },
        fn=get_pending_drafts,
    ),
    ToolDef(
        name="list_threads_by_tag",
        description=(
            "Threads with ≥1 message tagged (tag_kind, tag_value). tag_kind ∈ "
            "{category, urgency, project}. Returns thread_id, subject, sender, "
            "last_message_date, confidence."
        ),
        json_schema={
            "type": "object",
            "properties": {
                "tag_kind": {"type": "string", "enum": ["category", "urgency", "project"]},
                "tag_value": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50},
            },
            "required": ["tag_kind", "tag_value"],
        },
        fn=list_threads_by_tag,
    ),
]


TOOLS_BY_NAME: dict[str, ToolDef] = {t.name: t for t in TOOLS}


def call_tool(name: str, args: dict | None = None) -> Any:
    """Dispatch `name(**args)` against TOOLS_BY_NAME. Raises if unknown."""
    tool = TOOLS_BY_NAME.get(name)
    if tool is None:
        raise KeyError(f"unknown tool: {name!r}")
    return tool.fn(**(args or {}))


def manifest() -> list[dict]:
    """JSON-safe manifest — what the MCP `tools/list` returns."""
    return [
        {"name": t.name, "description": t.description, "inputSchema": t.json_schema}
        for t in TOOLS
    ]
