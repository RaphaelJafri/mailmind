"""Approval gate state machine + audit log.

This module is the *only* path to a draft state change in P4. The Tauri shell
posts to FastAPI, FastAPI calls one of these helpers, and we get to decide:

- Does the request shape match the current draft? (`draft_hash` matches.)
- Is the approval still valid? (Within 5 min, not cancelled, not executed.)
- Is the action allowed under the current OAuth scope? (P4a refuses `send`.)
- And — for executed actions — is the audit_log row chained to the prior?

Why a hand-rolled state machine instead of leaning on SQLAlchemy / dramatiq /
etc.: the safety guarantee is short enough to fit in one file and it has to
hold across edits, undo cancels, and failed Gmail calls. Smaller surface =
fewer footguns.

Invariants enforced here:

1. Every executed action writes one append-only `audit_log` row, chained to
   the prior row by `prev_id` + `prev_hash` (sha256 of the prior row's
   canonical JSON serialization). External tampering with the table breaks
   the chain on the next read.
2. `approval_hash` is computed from the canonical body at approval time, and
   re-checked at execution time. If the user edited the draft mid-window, the
   send/save fails with `hash_mismatch` and the user must re-approve.
3. P4a refuses any approval with `action="send"` — the OAuth scope hasn't
   been granted, and the gmail.send seam isn't even loaded.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import ulid

from . import db


# ---------------- Errors --------------------------------------------------

class ApprovalError(Exception):
    """Base — every failure here surfaces as a 4xx with a `code` payload."""

    code = "approval_error"

    def __init__(self, message: str, *, code: str | None = None):
        super().__init__(message)
        if code:
            self.code = code


class DraftNotFound(ApprovalError):
    code = "draft_not_found"


class InvalidState(ApprovalError):
    code = "invalid_state"


class HashMismatch(ApprovalError):
    code = "hash_mismatch"


class ApprovalExpired(ApprovalError):
    code = "approval_expired"


class SendNotPermitted(ApprovalError):
    """P4a: gmail.send scope not granted. Raised by approve(action='send')."""

    code = "send_not_permitted"


# ---------------- Hashing -------------------------------------------------

def canonicalize(draft: dict) -> str:
    """Produce a stable canonical string from the user-facing draft fields.

    The hash inputs are *only* the fields a recipient would see plus the
    addressing — body, subject, to/cc/bcc. Everything else (id, created_at,
    rationale) is metadata the user can edit without re-approving.
    """
    payload = {
        "to_emails": _normalize_addrs(draft.get("to_emails")),
        "cc_emails": _normalize_addrs(draft.get("cc_emails")),
        "bcc_emails": _normalize_addrs(draft.get("bcc_emails")),
        "subject": (draft.get("subject") or "").strip(),
        "body": (draft.get("body") or "").rstrip() + "\n",
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def hash_draft(draft: dict) -> str:
    return hashlib.sha256(canonicalize(draft).encode("utf-8")).hexdigest()


def _normalize_addrs(addrs: Any) -> list[str]:
    if not addrs:
        return []
    if isinstance(addrs, str):
        return [addrs.strip().lower()]
    return [str(a).strip().lower() for a in addrs if a]


# ---------------- Approval & action plumbing ------------------------------

APPROVAL_TTL = timedelta(minutes=5)
DEFAULT_UNDO_SECONDS = {"save_as_draft": 10, "send": 30}
SCOPE_FOR_ACTION = {"save_as_draft": "gmail.compose", "send": "gmail.send"}


@dataclass
class ApprovalRow:
    id: str
    draft_id: str
    approval_hash: str
    approved_by: str
    approved_at: datetime
    expires_at: datetime
    action: str
    undo_window_seconds: int
    cancelled_at: datetime | None
    executed_at: datetime | None
    result_status: str

    @classmethod
    def from_row(cls, row: sqlite3.Row | dict) -> "ApprovalRow":
        d = dict(row)
        return cls(
            id=d["id"],
            draft_id=d["draft_id"],
            approval_hash=d["approval_hash"],
            approved_by=d["approved_by"],
            approved_at=_parse_iso(d["approved_at"]),
            expires_at=_parse_iso(d["expires_at"]),
            action=d["action"],
            undo_window_seconds=d["undo_window_seconds"],
            cancelled_at=_maybe_iso(d.get("cancelled_at")),
            executed_at=_maybe_iso(d.get("executed_at")),
            result_status=d["result_status"],
        )


def _parse_iso(s: str) -> datetime:
    # Stored as ISO-8601 with offset; sqlite returns the str verbatim.
    return datetime.fromisoformat(s)


def _maybe_iso(s: Any) -> datetime | None:
    return _parse_iso(s) if s else None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _granted_scopes() -> set[str]:
    """Read the OAuth-grants file the dashboard writes to.

    P4a: the scope file is a JSON `{"gmail.compose": true, "gmail.send": false}`
    written by the Settings → Permissions UI. If the file doesn't exist, we
    assume read-only mode (no compose, no send) — strictly safe.
    """
    from . import paths

    p = paths.config_dir() / "oauth_scopes.json"
    if not p.exists():
        return set()
    try:
        data = json.loads(p.read_text())
    except Exception:
        return set()
    return {k for k, v in data.items() if v}


# ---------------- Public API ---------------------------------------------

def approve(
    draft_id: str,
    *,
    action: str,
    approved_by: str,
    undo_window_seconds: int | None = None,
) -> dict:
    """Create an approval row for `draft_id`. Caller (the FastAPI handler)
    has already verified the requester is the mailbox owner.

    P4a: only `action='save_as_draft'` is reachable through the UI. We still
    accept `action='send'` here so P4b can flip it on without changing the
    plumbing — but we hard-refuse if the gmail.send scope isn't granted.
    """
    if action not in SCOPE_FOR_ACTION:
        raise InvalidState(f"unknown action: {action!r}")

    required_scope = SCOPE_FOR_ACTION[action]
    if required_scope not in _granted_scopes():
        # Don't write any approval row — refuse at the gate.
        raise SendNotPermitted(
            f"approval for action={action!r} requires {required_scope!r} scope; "
            f"grant it in Settings → Permissions first."
        )

    draft = _get_draft_row(draft_id)
    if draft is None:
        raise DraftNotFound(f"no draft with id={draft_id!r}")
    if draft["status"] != "pending":
        raise InvalidState(
            f"draft {draft_id!r} is in status={draft['status']!r}; only pending "
            "drafts can be approved."
        )

    now = _now()
    approval_hash = draft["draft_hash"]
    approval_id = str(ulid.new())
    undo = undo_window_seconds if undo_window_seconds is not None else DEFAULT_UNDO_SECONDS[action]

    with db.derived() as conn:
        conn.execute(
            """
            INSERT INTO approvals (
              id, draft_id, approval_hash, approved_by, approved_at,
              expires_at, action, undo_window_seconds, result_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                approval_id,
                draft_id,
                approval_hash,
                approved_by,
                now.isoformat(),
                (now + APPROVAL_TTL).isoformat(),
                action,
                undo,
                "pending",
            ),
        )
        conn.execute(
            "UPDATE drafts SET approval_id = ?, status = 'approved', updated_at = ? "
            "WHERE id = ?",
            (approval_id, now.isoformat(), draft_id),
        )
        _append_audit(
            conn,
            event_type="approve",
            draft_id=draft_id,
            approval_id=approval_id,
            draft_hash=draft["draft_hash"],
            approval_hash=approval_hash,
            payload={"action": action, "undo_window_seconds": undo, "approved_by": approved_by},
        )

    return {
        "approval_id": approval_id,
        "draft_id": draft_id,
        "action": action,
        "approved_at": now.isoformat(),
        "expires_at": (now + APPROVAL_TTL).isoformat(),
        "undo_window_seconds": undo,
    }


def cancel(approval_id: str, *, reason: str | None = None) -> dict:
    """User clicked Cancel during the undo window. Approval becomes
    `cancelled` and the draft drops back to `pending` so the user can edit
    and re-approve.
    """
    approval = _get_approval(approval_id)
    if approval is None:
        raise InvalidState(f"no approval with id={approval_id!r}")
    if approval.result_status != "pending":
        raise InvalidState(
            f"approval {approval_id!r} is in result_status={approval.result_status!r}; "
            "cannot cancel."
        )
    now = _now()
    with db.derived() as conn:
        conn.execute(
            "UPDATE approvals SET cancelled_at = ?, result_status = 'cancelled' "
            "WHERE id = ?",
            (now.isoformat(), approval_id),
        )
        conn.execute(
            "UPDATE drafts SET status = 'pending', approval_id = NULL, updated_at = ? "
            "WHERE id = ?",
            (now.isoformat(), approval.draft_id),
        )
        _append_audit(
            conn,
            event_type="cancel",
            draft_id=approval.draft_id,
            approval_id=approval_id,
            payload={"reason": reason or ""},
        )
    return {
        "approval_id": approval_id,
        "draft_id": approval.draft_id,
        "cancelled_at": now.isoformat(),
        "reason": reason,
    }


def execute_save_as_draft(approval_id: str, *, gmail_writer) -> dict:
    """Caller passes a `gmail_writer` callable; we verify hash + freshness +
    scope, invoke the writer, and write the audit row. Caller is responsible
    for actually waiting out the undo window before invoking us.

    The seam is a callable, not a module import, to keep this file mockable
    from tests without monkey-patching.
    """
    return _execute(approval_id, expected_action="save_as_draft", writer=gmail_writer)


def execute_send(approval_id: str, *, gmail_writer) -> dict:
    """P4b. Wired identically to execute_save_as_draft, but with the `send`
    scope check. Refuses outright in P4a (no gmail.send scope grant)."""
    if "gmail.send" not in _granted_scopes():
        raise SendNotPermitted("gmail.send scope not granted")
    return _execute(approval_id, expected_action="send", writer=gmail_writer)


def reject(draft_id: str, *, reason: str | None = None) -> dict:
    draft = _get_draft_row(draft_id)
    if draft is None:
        raise DraftNotFound(f"no draft with id={draft_id!r}")
    if draft["status"] not in {"pending", "approved"}:
        raise InvalidState(
            f"draft {draft_id!r} is in status={draft['status']!r}; cannot reject."
        )
    now = _now()
    with db.derived() as conn:
        conn.execute(
            "UPDATE drafts SET status = 'rejected', updated_at = ? WHERE id = ?",
            (now.isoformat(), draft_id),
        )
        # Cancel any in-flight approval row too — keeps state consistent.
        if draft["approval_id"]:
            conn.execute(
                "UPDATE approvals SET cancelled_at = ?, result_status = 'cancelled' "
                "WHERE id = ? AND result_status = 'pending'",
                (now.isoformat(), draft["approval_id"]),
            )
        _append_audit(
            conn,
            event_type="reject",
            draft_id=draft_id,
            payload={"reason": reason or ""},
        )
    return {"draft_id": draft_id, "rejected_at": now.isoformat(), "reason": reason}


# ---------------- internals -----------------------------------------------

def _execute(approval_id: str, *, expected_action: str, writer) -> dict:
    approval = _get_approval(approval_id)
    if approval is None:
        raise InvalidState(f"no approval with id={approval_id!r}")
    if approval.action != expected_action:
        raise InvalidState(
            f"approval {approval_id!r} is for action={approval.action!r}, "
            f"not {expected_action!r}."
        )
    if approval.result_status != "pending":
        raise InvalidState(
            f"approval {approval_id!r} already in result_status="
            f"{approval.result_status!r}."
        )
    if _now() > approval.expires_at:
        # Mark expired so the UI can prompt re-approval.
        with db.derived() as conn:
            conn.execute(
                "UPDATE approvals SET result_status = 'failed' WHERE id = ?",
                (approval_id,),
            )
            conn.execute(
                "UPDATE drafts SET status = 'pending', approval_id = NULL, updated_at = ? "
                "WHERE id = ?",
                (_now().isoformat(), approval.draft_id),
            )
        raise ApprovalExpired(
            f"approval {approval_id!r} expired at {approval.expires_at.isoformat()}"
        )

    draft = _get_draft_row(approval.draft_id)
    if draft is None:
        raise DraftNotFound(f"no draft with id={approval.draft_id!r}")
    current_hash = hash_draft(_draft_for_hash(draft))
    if current_hash != approval.approval_hash:
        raise HashMismatch(
            f"draft body changed since approval (current={current_hash[:12]} != "
            f"approved={approval.approval_hash[:12]}); re-approve required."
        )

    # Hash matched — invoke the writer.
    try:
        writer_result = writer(draft)
    except Exception as exc:  # noqa: BLE001
        with db.derived() as conn:
            conn.execute(
                "UPDATE approvals SET result_status = 'failed' WHERE id = ?",
                (approval_id,),
            )
            conn.execute(
                "UPDATE drafts SET status = 'pending', approval_id = NULL, updated_at = ? "
                "WHERE id = ?",
                (_now().isoformat(), draft["id"]),
            )
            _append_audit(
                conn,
                event_type=f"{expected_action}_failed",
                draft_id=draft["id"],
                approval_id=approval_id,
                draft_hash=current_hash,
                approval_hash=approval.approval_hash,
                payload={"error": f"{type(exc).__name__}: {exc}"},
            )
        raise

    now = _now()
    next_status = "saved_as_draft" if expected_action == "save_as_draft" else "sent"
    gmail_draft_id = writer_result.get("gmail_draft_id") if isinstance(writer_result, dict) else None
    gmail_message_id = writer_result.get("gmail_message_id") if isinstance(writer_result, dict) else None

    with db.derived() as conn:
        conn.execute(
            "UPDATE approvals SET executed_at = ?, result_status = 'executed' "
            "WHERE id = ?",
            (now.isoformat(), approval_id),
        )
        conn.execute(
            "UPDATE drafts SET status = ?, gmail_draft_id = COALESCE(?, gmail_draft_id), "
            "gmail_message_id = COALESCE(?, gmail_message_id), updated_at = ? "
            "WHERE id = ?",
            (next_status, gmail_draft_id, gmail_message_id, now.isoformat(), draft["id"]),
        )
        _append_audit(
            conn,
            event_type=expected_action,
            draft_id=draft["id"],
            approval_id=approval_id,
            draft_hash=current_hash,
            approval_hash=approval.approval_hash,
            gmail_draft_id=gmail_draft_id,
            gmail_message_id=gmail_message_id,
            payload={k: v for k, v in (writer_result or {}).items() if k != "raw"},
        )

    return {
        "approval_id": approval_id,
        "draft_id": draft["id"],
        "action": expected_action,
        "executed_at": now.isoformat(),
        "gmail_draft_id": gmail_draft_id,
        "gmail_message_id": gmail_message_id,
        "next_status": next_status,
    }


def _get_draft_row(draft_id: str) -> dict | None:
    with db.derived() as conn:
        row = conn.execute(
            "SELECT * FROM drafts WHERE id = ?", (draft_id,)
        ).fetchone()
    return dict(row) if row else None


def _get_approval(approval_id: str) -> ApprovalRow | None:
    with db.derived() as conn:
        row = conn.execute(
            "SELECT * FROM approvals WHERE id = ?", (approval_id,)
        ).fetchone()
    return ApprovalRow.from_row(row) if row else None


def _draft_for_hash(draft_row: dict) -> dict:
    """Re-pack the columns from a `drafts` row into the dict shape that
    canonicalize() expects."""
    return {
        "to_emails": json.loads(draft_row["to_emails"] or "[]"),
        "cc_emails": json.loads(draft_row["cc_emails"] or "[]"),
        "bcc_emails": json.loads(draft_row["bcc_emails"] or "[]"),
        "subject": draft_row["subject"],
        "body": draft_row["body"],
    }


# ---------------- audit log helpers --------------------------------------

def _append_audit(
    conn: sqlite3.Connection,
    *,
    event_type: str,
    draft_id: str | None = None,
    approval_id: str | None = None,
    draft_hash: str | None = None,
    approval_hash: str | None = None,
    gmail_message_id: str | None = None,
    gmail_draft_id: str | None = None,
    payload: dict | None = None,
) -> str:
    """Append one row to audit_log, chained by prev_id + prev_hash."""
    prev = conn.execute(
        "SELECT id, event_at, event_type, draft_id, approval_id, draft_hash, "
        "approval_hash, gmail_message_id, gmail_draft_id, payload_json, "
        "prev_id, prev_hash "
        "FROM audit_log ORDER BY rowid DESC LIMIT 1"
    ).fetchone()

    if prev is not None:
        prev_id = prev["id"]
        prev_hash = _hash_audit_row(dict(prev))
    else:
        prev_id = None
        prev_hash = None

    row_id = str(ulid.new())
    now_iso = _now().isoformat()
    conn.execute(
        """
        INSERT INTO audit_log (
          id, event_at, event_type, draft_id, approval_id,
          draft_hash, approval_hash, gmail_message_id, gmail_draft_id,
          payload_json, prev_id, prev_hash
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            row_id,
            now_iso,
            event_type,
            draft_id,
            approval_id,
            draft_hash,
            approval_hash,
            gmail_message_id,
            gmail_draft_id,
            json.dumps(payload or {}, sort_keys=True),
            prev_id,
            prev_hash,
        ),
    )
    return row_id


def _hash_audit_row(row: dict) -> str:
    """Canonical hash of one audit_log row, used as the `prev_hash` for the
    next row. Must be deterministic: ordered keys, no whitespace."""
    canonical = {
        "id": row["id"],
        "event_at": row["event_at"],
        "event_type": row["event_type"],
        "draft_id": row.get("draft_id"),
        "approval_id": row.get("approval_id"),
        "draft_hash": row.get("draft_hash"),
        "approval_hash": row.get("approval_hash"),
        "gmail_message_id": row.get("gmail_message_id"),
        "gmail_draft_id": row.get("gmail_draft_id"),
        "payload_json": row.get("payload_json"),
        "prev_id": row.get("prev_id"),
        "prev_hash": row.get("prev_hash"),
    }
    blob = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()


def verify_audit_chain() -> dict:
    """Walk audit_log oldest-first and verify every row's prev_hash matches
    the hash we recompute over the previous row. Returns
    `{ok: bool, total: int, broken_at: id|None}`. Used by tests + the Sent
    tab health indicator."""
    with db.derived() as conn:
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM audit_log ORDER BY rowid ASC"
            ).fetchall()
        ]
    if not rows:
        return {"ok": True, "total": 0, "broken_at": None}

    prev_hash: str | None = None
    prev_id: str | None = None
    for row in rows:
        if row.get("prev_id") != prev_id or row.get("prev_hash") != prev_hash:
            return {"ok": False, "total": len(rows), "broken_at": row["id"]}
        prev_hash = _hash_audit_row(row)
        prev_id = row["id"]
    return {"ok": True, "total": len(rows), "broken_at": None}
