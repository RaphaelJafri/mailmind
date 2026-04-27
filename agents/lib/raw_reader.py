"""Read-only adapter to the Node-owned `raw.sqlite`.

The Node ingester is the only writer — Python opens read-only handles. We
also do not declare or migrate any raw schema here. If the file doesn't
exist yet (user hasn't run sync), reads return empty lists.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from . import paths


def _open_ro() -> sqlite3.Connection | None:
    p = paths.raw_db_path()
    if not p.exists():
        return None
    uri = f"file:{p}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def list_unclassified_senders(min_thread_count: int = 1, limit: int = 50) -> list[dict]:
    """Senders whose threads are sitting unclassified, ordered by volume.

    Returns: [{sender, thread_count, sample_subjects: [str]}, ...]
    """
    conn = _open_ro()
    if conn is None:
        return []
    try:
        rows = conn.execute(
            """
            SELECT
              td.sender AS sender,
              COUNT(*) AS thread_count,
              GROUP_CONCAT(t.subject, ' ||| ') AS subjects
            FROM thread_dispositions td
            JOIN threads t USING (thread_id)
            WHERE td.disposition = 'unclassified'
              AND td.sender IS NOT NULL
            GROUP BY td.sender
            HAVING thread_count >= ?
            ORDER BY thread_count DESC, sender ASC
            LIMIT ?
            """,
            (min_thread_count, limit),
        ).fetchall()
    finally:
        conn.close()

    out: list[dict] = []
    for r in rows:
        subjects = (r["subjects"] or "").split(" ||| ")
        out.append(
            {
                "sender": r["sender"],
                "thread_count": r["thread_count"],
                "sample_subjects": [s for s in subjects[:10] if s],
            }
        )
    return out


def get_thread(thread_id: str) -> dict | None:
    """Return one thread + its messages in chronological order, ready for the
    extract prompt. None if not found / no raw db."""
    conn = _open_ro()
    if conn is None:
        return None
    try:
        thread = conn.execute(
            "SELECT thread_id, subject FROM threads WHERE thread_id = ?",
            (thread_id,),
        ).fetchone()
        if thread is None:
            return None

        msgs = conn.execute(
            """
            SELECT message_id, from_email, from_name, to_emails, cc_emails,
                   internal_date, body_plain, is_from_user
            FROM messages WHERE thread_id = ?
            ORDER BY internal_date ASC
            """,
            (thread_id,),
        ).fetchall()
    finally:
        conn.close()

    return {
        "thread_id": thread["thread_id"],
        "subject": thread["subject"],
        "messages": [
            {
                "message_id": m["message_id"],
                "from": m["from_email"],
                "from_name": m["from_name"],
                "to": _json_list(m["to_emails"]),
                "cc": _json_list(m["cc_emails"]),
                "date": m["internal_date"],
                "is_from_user": bool(m["is_from_user"]),
                "body": m["body_plain"] or "",
            }
            for m in msgs
        ],
    }


def get_message(message_id: str) -> dict | None:
    conn = _open_ro()
    if conn is None:
        return None
    try:
        m = conn.execute(
            """
            SELECT m.message_id, m.thread_id, m.from_email, m.from_name,
                   m.to_emails, m.cc_emails, m.internal_date, m.body_plain,
                   m.is_from_user, t.subject AS thread_subject,
                   td.disposition AS thread_disposition
            FROM messages m
            JOIN threads t ON t.thread_id = m.thread_id
            LEFT JOIN thread_dispositions td ON td.thread_id = m.thread_id
            WHERE m.message_id = ?
            """,
            (message_id,),
        ).fetchone()
    finally:
        conn.close()
    if m is None:
        return None
    return {
        "message_id": m["message_id"],
        "thread_id": m["thread_id"],
        "thread_subject": m["thread_subject"],
        "from": m["from_email"],
        "from_name": m["from_name"],
        "to": _json_list(m["to_emails"]),
        "cc": _json_list(m["cc_emails"]),
        "date": m["internal_date"],
        "is_from_user": bool(m["is_from_user"]),
        "body": m["body_plain"] or "",
        "thread_disposition": m["thread_disposition"] or "unclassified",
    }


def list_threads(limit: int = 100, disposition: str | None = "keep") -> list[dict]:
    conn = _open_ro()
    if conn is None:
        return []
    try:
        if disposition is None:
            rows = conn.execute(
                "SELECT thread_id, subject, message_count, last_message_date "
                "FROM threads ORDER BY last_message_date DESC LIMIT ?",
                (limit,),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT t.thread_id, t.subject, t.message_count, t.last_message_date,
                       td.disposition, td.sender
                FROM threads t
                JOIN thread_dispositions td USING (thread_id)
                WHERE td.disposition = ?
                ORDER BY t.last_message_date DESC LIMIT ?
                """,
                (disposition, limit),
            ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def _json_list(blob: str | None) -> list[str]:
    if not blob:
        return []
    import json

    try:
        v = json.loads(blob)
    except Exception:
        return []
    return v if isinstance(v, list) else []
