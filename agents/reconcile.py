"""Deterministic reconciler — `contact_rollup_drafts` → `next_steps`.

Ported from v1's ``scripts/reconcile-next-steps.mjs``. No LLM. For every
contact with a fresh batch of `draft_next_steps`, diff against pending
`next_steps` and:

  1. Match old pending step ↔ new draft using full source_message_id
     containment OR partial overlap + jaccard(description) ≥ 0.7. Matched
     rows are updated in place so dashboard state (id, created_at) is
     preserved.
  2. Unmatched new drafts → INSERT a new pending step.
  3. Unmatched old steps → resolve (if a later user message exists in any
     source thread) or supersede.

**Dismiss-bug fix (BUILD §19):** the dismiss-correction set is consulted
*before* matching, so a draft that overlaps a previously-dismissed step is
silently dropped. Number dropped is recorded in
``pipeline_runs.dropped_count`` so the user (and the Observability tab) can
see suppression activity.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

import ulid

from lib import db, paths

STOPWORDS = {
    "a", "an", "the", "to", "of", "in", "on", "at", "for", "and", "or", "with",
    "from", "is", "are", "was", "were", "be", "been", "being", "do", "does",
    "did", "done", "this", "that", "these", "those", "i", "you", "he", "she",
    "we", "they", "it",
}


def tokenize(s: str | None) -> set[str]:
    import re
    words = re.findall(r"[a-z0-9]+", str(s or "").lower())
    return {w for w in words if w not in STOPWORDS and len(w) > 1}


def jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 1.0
    inter = len(a & b)
    union = len(a) + len(b) - inter
    return inter / union if union else 0.0


def match_step(old: dict, new: dict, *, threshold: float = 0.7) -> bool:
    old_ids = set(old.get("source_message_ids") or [])
    new_ids = set(new.get("source_message_ids") or [])
    if not old_ids or not new_ids:
        return False
    overlap = len(old_ids & new_ids)
    full_containment = old_ids.issubset(new_ids)
    if full_containment:
        return True
    if overlap == 0:
        return False
    sim = jaccard(tokenize(old.get("description")), tokenize(new.get("description")))
    return sim >= threshold


def pair_up(old_steps: list[dict], new_drafts: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    taken: set[int] = set()
    pairs: list[dict] = []
    unmatched_old: list[dict] = []
    for o in old_steps:
        paired = None
        for i, d in enumerate(new_drafts):
            if i in taken:
                continue
            if match_step(o, d):
                paired = {"old": o, "draft": d, "draft_index": i}
                taken.add(i)
                break
        if paired:
            pairs.append(paired)
        else:
            unmatched_old.append(o)
    unmatched_new = [d for i, d in enumerate(new_drafts) if i not in taken]
    return pairs, unmatched_old, unmatched_new


# ---- dismiss-bug fix ------------------------------------------------------

@dataclass
class DismissEntry:
    contact_email: str | None
    description: str
    tokens: set[str]


def load_dismiss_set(derived: sqlite3.Connection) -> list[DismissEntry]:
    """Read every `next_step` correction whose `field=status, new_value=dismissed`.

    For each one, look up the original step's description (joining via
    `entity_id`) so we can jaccard-match new drafts against it. Falls back to
    the correction's `old_value` if the row is gone.
    """
    rows = derived.execute(
        """
        SELECT id, contact_email, entity_id, old_value
        FROM corrections
        WHERE entity_type = 'next_step'
          AND field = 'status'
          AND new_value = 'dismissed'
        """
    ).fetchall()
    out: list[DismissEntry] = []
    for r in rows:
        desc = None
        if r["entity_id"]:
            step = derived.execute(
                "SELECT description, contact_email FROM next_steps WHERE id = ?",
                (r["entity_id"],),
            ).fetchone()
            if step:
                desc = step["description"]
        if desc is None:
            desc = r["old_value"]
        if not desc:
            continue
        out.append(
            DismissEntry(
                contact_email=r["contact_email"],
                description=desc,
                tokens=tokenize(desc),
            )
        )
    return out


def is_dismissed(draft: dict, contact_email: str, dismiss_set: list[DismissEntry], *, threshold: float = 0.7) -> bool:
    """v2 fix: a draft Jaccard-overlapping a dismissed step ≥ threshold is
    silently dropped. Either same-contact or global (NULL contact_email)
    dismisses apply."""
    draft_tokens = tokenize(draft.get("description"))
    if not draft_tokens:
        return False
    for entry in dismiss_set:
        if entry.contact_email and entry.contact_email != contact_email:
            continue
        if jaccard(draft_tokens, entry.tokens) >= threshold:
            return True
    return False


# ---- per-contact ----------------------------------------------------------

def is_resolved_by_later_message(raw: sqlite3.Connection, step: dict) -> bool:
    threads = step.get("source_thread_ids") or []
    msgs = step.get("source_message_ids") or []
    if not threads or not msgs:
        return False
    placeholders = ",".join("?" for _ in msgs)
    rows = raw.execute(
        f"SELECT internal_date FROM messages WHERE message_id IN ({placeholders})",
        tuple(msgs),
    ).fetchall()
    if not rows:
        return False
    latest = max((r["internal_date"] for r in rows if r["internal_date"]), default=None)
    if not latest:
        return False
    placeholders_t = ",".join("?" for _ in threads)
    hit = raw.execute(
        f"SELECT 1 FROM messages "
        f"WHERE thread_id IN ({placeholders_t}) "
        f"  AND is_from_user = 1 AND internal_date > ? LIMIT 1",
        (*threads, latest),
    ).fetchone()
    return hit is not None


def reconcile_contact(
    *,
    raw: sqlite3.Connection,
    derived: sqlite3.Connection,
    contact_email: str,
    drafts: list[dict],
    dismiss_set: list[DismissEntry],
    now: str,
    dry_run: bool = False,
) -> dict:
    # Drop drafts that match a prior dismiss FIRST — before pairing. This is
    # the §19 fix: dismissed steps must never resurrect even if the rollup
    # keeps proposing them.
    kept_drafts: list[dict] = []
    dropped: list[dict] = []
    for d in drafts:
        if is_dismissed(d, contact_email, dismiss_set):
            dropped.append({"description": d.get("description")})
        else:
            kept_drafts.append(d)

    existing_rows = derived.execute(
        """
        SELECT id, contact_email, description, priority, due_date, status,
               source_thread_ids, source_message_ids, created_at, updated_at,
               confidence
        FROM next_steps
        WHERE contact_email = ? AND status = 'pending'
        ORDER BY created_at ASC
        """,
        (contact_email,),
    ).fetchall()
    existing = []
    for r in existing_rows:
        d = dict(r)
        d["source_thread_ids"] = json.loads(d["source_thread_ids"] or "[]")
        d["source_message_ids"] = json.loads(d["source_message_ids"] or "[]")
        existing.append(d)

    pairs, unmatched_old, unmatched_new = pair_up(existing, kept_drafts)

    actions = {"matched": [], "inserted": [], "resolved": [], "superseded": [], "dropped_dismissed": dropped}

    if dry_run:
        return {
            "contact_email": contact_email,
            "existing_count": len(existing),
            "draft_count": len(drafts),
            "kept_count": len(kept_drafts),
            "actions": actions,
        }

    # 1. Update matched pairs in place.
    for p in pairs:
        o = p["old"]; d = p["draft"]
        derived.execute(
            """
            UPDATE next_steps
            SET description = ?, priority = ?, due_date = ?,
                source_thread_ids = ?, source_message_ids = ?,
                updated_at = ?, confidence = ?
            WHERE id = ?
            """,
            (
                d["description"], d["priority"], d.get("due_date"),
                json.dumps(d.get("source_thread_ids") or []),
                json.dumps(d.get("source_message_ids") or []),
                now, d["confidence"], o["id"],
            ),
        )
        actions["matched"].append(
            {"id": o["id"], "description": d["description"],
             "before_priority": o["priority"], "after_priority": d["priority"]}
        )

    # 2. Insert unmatched new drafts.
    for d in unmatched_new:
        sid = str(ulid.new())
        derived.execute(
            """
            INSERT INTO next_steps (
              id, contact_email, description, priority, due_date, status,
              source_thread_ids, source_message_ids, created_at, updated_at,
              confidence
            ) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?)
            """,
            (
                sid, contact_email, d["description"], d["priority"],
                d.get("due_date"),
                json.dumps(d.get("source_thread_ids") or []),
                json.dumps(d.get("source_message_ids") or []),
                now, now, d["confidence"],
            ),
        )
        actions["inserted"].append(
            {"id": sid, "description": d["description"],
             "priority": d["priority"], "due_date": d.get("due_date")}
        )

    # 3. Unmatched old → resolve / supersede.
    for o in unmatched_old:
        resolved = is_resolved_by_later_message(raw, o)
        status = "resolved" if resolved else "superseded"
        reason = "detected_in_later_email" if resolved else "superseded_by_new_step"
        derived.execute(
            "UPDATE next_steps SET status=?, updated_at=?, resolved_at=?, "
            "resolution_reason=? WHERE id = ?",
            (status, now, now, reason, o["id"]),
        )
        bucket = "resolved" if resolved else "superseded"
        actions[bucket].append(
            {"id": o["id"], "description": o["description"], "reason": reason}
        )

    derived.commit()
    return {
        "contact_email": contact_email,
        "existing_count": len(existing),
        "draft_count": len(drafts),
        "kept_count": len(kept_drafts),
        "actions": actions,
    }


# ---- top-level run --------------------------------------------------------

def run(*, contact: str | None = None, dry_run: bool = False) -> dict:
    """Reconcile every contact with fresh rollup drafts. Records a
    pipeline_runs row with totals and the dismiss-drop counter."""
    run_id = str(ulid.new())
    started = datetime.now(timezone.utc).isoformat()
    derived = db.open_derived()
    raw_p = paths.raw_db_path()
    raw = sqlite3.connect(f"file:{raw_p}?mode=ro", uri=True) if raw_p.exists() else None
    if raw is not None:
        raw.row_factory = sqlite3.Row

    derived.execute(
        "INSERT INTO pipeline_runs (run_id, stage, started_at, status) "
        "VALUES (?, 'reconcile', ?, 'running')",
        (run_id, started),
    )
    derived.commit()

    try:
        if contact:
            rows = derived.execute(
                "SELECT contact_email, drafts_json FROM contact_rollup_drafts "
                "WHERE contact_email = ?",
                (contact,),
            ).fetchall()
        else:
            rows = derived.execute(
                "SELECT contact_email, drafts_json FROM contact_rollup_drafts "
                "ORDER BY contact_email ASC"
            ).fetchall()

        dismiss_set = load_dismiss_set(derived)
        now = datetime.now(timezone.utc).isoformat()
        per_contact: list[dict] = []
        totals = {"matched": 0, "inserted": 0, "resolved": 0, "superseded": 0, "dropped_dismissed": 0}

        for row in rows:
            drafts = json.loads(row["drafts_json"] or "[]")
            result = reconcile_contact(
                raw=raw,
                derived=derived,
                contact_email=row["contact_email"],
                drafts=drafts,
                dismiss_set=dismiss_set,
                now=now,
                dry_run=dry_run,
            )
            per_contact.append(result)
            for k in totals:
                totals[k] += len(result["actions"].get(k, []))

        pending_total = derived.execute(
            "SELECT COUNT(*) AS c FROM next_steps WHERE status = 'pending'"
        ).fetchone()["c"]

        finished = datetime.now(timezone.utc).isoformat()
        derived.execute(
            "UPDATE pipeline_runs SET finished_at=?, status=?, contacts_processed=?, "
            "dropped_count=? WHERE run_id=?",
            (finished, "completed" if not dry_run else "dry_run",
             len(rows), totals["dropped_dismissed"], run_id),
        )
        derived.commit()

        return {
            "run_id": run_id,
            "contacts_scanned": len(rows),
            "totals": totals,
            "pending_total": pending_total,
            "per_contact": per_contact,
            "dry_run": dry_run,
        }
    finally:
        if raw is not None:
            raw.close()
        derived.close()


# ---- correction recording -------------------------------------------------

def record_correction(
    *,
    contact_email: str | None,
    entity_type: str,
    entity_id: str | None,
    field: str,
    new_value: str,
    old_value: str | None = None,
    user_note: str | None = None,
) -> dict:
    """Append a correction row. Append-only — never overwrite an existing one."""
    cid = str(ulid.new())
    now = datetime.now(timezone.utc).isoformat()
    derived = db.open_derived()
    try:
        derived.execute(
            """
            INSERT INTO corrections
              (id, timestamp, contact_email, entity_type, entity_id,
               field, old_value, new_value, user_note, applied_to_runs)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '[]')
            """,
            (cid, now, contact_email, entity_type, entity_id, field,
             old_value, new_value, user_note),
        )
        derived.commit()
    finally:
        derived.close()
    return {
        "id": cid,
        "timestamp": now,
        "contact_email": contact_email,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "field": field,
        "old_value": old_value,
        "new_value": new_value,
        "user_note": user_note,
    }


def dismiss_step(step_id: str, *, user_note: str | None = None) -> dict:
    """Convenience: dismiss a pending next_step.

    Marks the row's status as 'dismissed' AND records a correction so the
    next reconcile run drops re-proposals (see §19).
    """
    derived = db.open_derived()
    try:
        row = derived.execute(
            "SELECT id, contact_email, description, status FROM next_steps WHERE id = ?",
            (step_id,),
        ).fetchone()
        if row is None:
            raise LookupError(f"next_step {step_id!r} not found")
        if row["status"] == "dismissed":
            return {"id": step_id, "already": True}

        now = datetime.now(timezone.utc).isoformat()
        derived.execute(
            "UPDATE next_steps SET status='dismissed', updated_at=?, "
            "resolved_at=?, resolution_reason='dismissed_by_user' WHERE id = ?",
            (now, now, step_id),
        )
        derived.commit()
    finally:
        derived.close()

    correction = record_correction(
        contact_email=row["contact_email"],
        entity_type="next_step",
        entity_id=step_id,
        field="status",
        new_value="dismissed",
        old_value=row["status"],
        user_note=user_note,
    )
    return {
        "id": step_id,
        "contact_email": row["contact_email"],
        "description": row["description"],
        "correction_id": correction["id"],
        "dismissed_at": correction["timestamp"],
    }


# ---- read APIs ------------------------------------------------------------

def list_next_steps(*, status: str = "pending", limit: int = 200) -> list[dict]:
    derived = db.open_derived()
    try:
        rows = derived.execute(
            "SELECT * FROM next_steps WHERE status = ? "
            "ORDER BY created_at DESC LIMIT ?",
            (status, limit),
        ).fetchall()
    finally:
        derived.close()
    out = []
    for r in rows:
        d = dict(r)
        d["source_thread_ids"] = json.loads(d.get("source_thread_ids") or "[]")
        d["source_message_ids"] = json.loads(d.get("source_message_ids") or "[]")
        out.append(d)
    return out


def list_corrections(limit: int = 100) -> list[dict]:
    derived = db.open_derived()
    try:
        rows = derived.execute(
            "SELECT * FROM corrections ORDER BY timestamp DESC LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        derived.close()
    return [dict(r) for r in rows]
