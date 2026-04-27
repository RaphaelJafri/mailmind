"""Gmail write surface — the *only* place mailmind talks back to Gmail.

P4a: implements `save_as_draft` against Gmail's `users.drafts.create` (scope
`gmail.compose`). The send path (`users.messages.send`, scope `gmail.send`)
is stubbed out and refuses unless the scope is granted — that ships in P4b.

The actual HTTP/auth call lives in the Node ingester (which already owns
Gmail OAuth). Python POSTs an RFC-822 payload to a localhost endpoint on
the ingester, which signs it with the user's tokens. That keeps Gmail auth
in one place and the Python sidecar single-purpose.

Test/demo mode: if `MAILMIND_GMAIL_MOCK=1` is set OR no real ingester
endpoint is configured, we return a deterministic fake gmail_draft_id and
no network call is made. The mock path is what runs under pytest and under
fixture-mode demos.
"""

from __future__ import annotations

import hashlib
import json
import os
from email.message import EmailMessage
from typing import Any

import urllib.error
import urllib.request


INGESTER_URL = os.environ.get("MAILMIND_INGESTER_URL", "http://127.0.0.1:8766")
MOCK_ENV = "MAILMIND_GMAIL_MOCK"


# ---------------- public API ---------------------------------------------

def save_as_draft(draft_row: dict) -> dict:
    """Persist a Gmail draft via the ingester's gmail.compose endpoint.

    `draft_row` is the dict produced by `draft_agent._row_to_dict(...)` —
    fields parsed (to_emails is a list, etc.). Returns:

        {"gmail_draft_id": str, "rfc822_size": int, "mocked": bool}

    Never raises on transport — converts everything to a single
    RuntimeError so the approval gate's audit row carries a clean error
    string.
    """
    rfc822 = _build_rfc822(draft_row)
    if _is_mocked():
        return _mock_save_as_draft(draft_row, rfc822)
    try:
        return _post_ingester_save_as_draft(rfc822)
    except RuntimeError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"ingester save_as_draft failed: {type(exc).__name__}: {exc}") from exc


def send(draft_row: dict) -> dict:
    """P4b. Refused outright in P4a — no `gmail.send` scope grant."""
    if _is_mocked():
        return _mock_send(draft_row, _build_rfc822(draft_row))
    raise RuntimeError(
        "gmail.send is not wired in P4a. Enable it in Settings → Permissions "
        "(requires re-OAuth) and rebuild for P4b."
    )


# ---------------- helpers -------------------------------------------------

def _is_mocked() -> bool:
    return os.environ.get(MOCK_ENV, "0") == "1"


def _build_rfc822(draft_row: dict) -> str:
    """Render a minimal RFC-822 message. We deliberately don't set Date or
    Message-ID — Gmail fills those in on `users.drafts.create` and we don't
    want a clock-dependent payload for hash determinism in tests."""
    msg = EmailMessage()
    msg["Subject"] = draft_row["subject"]
    msg["To"] = ", ".join(draft_row["to_emails"])
    if draft_row.get("cc_emails"):
        msg["Cc"] = ", ".join(draft_row["cc_emails"])
    if draft_row.get("bcc_emails"):
        msg["Bcc"] = ", ".join(draft_row["bcc_emails"])
    if draft_row.get("in_reply_to_message_id"):
        msg["In-Reply-To"] = draft_row["in_reply_to_message_id"]
        msg["References"] = draft_row["in_reply_to_message_id"]
    msg.set_content(draft_row["body"])
    return msg.as_string()


def _mock_save_as_draft(draft_row: dict, rfc822: str) -> dict:
    """Deterministic fake — gmail_draft_id is sha256(thread_id + body) prefix.
    Stable across re-runs so tests can assert it."""
    seed = f"{draft_row.get('thread_id') or ''}::{draft_row.get('draft_hash') or ''}"
    fake_id = "draft-" + hashlib.sha256(seed.encode()).hexdigest()[:16]
    return {
        "gmail_draft_id": fake_id,
        "rfc822_size": len(rfc822),
        "mocked": True,
    }


def _mock_send(draft_row: dict, rfc822: str) -> dict:
    seed = f"{draft_row.get('thread_id') or ''}::{draft_row.get('draft_hash') or ''}::send"
    fake_id = "msg-" + hashlib.sha256(seed.encode()).hexdigest()[:16]
    return {
        "gmail_message_id": fake_id,
        "rfc822_size": len(rfc822),
        "mocked": True,
    }


def _post_ingester_save_as_draft(rfc822: str) -> dict:
    """Real path: POST to the Node ingester's gmail.compose endpoint."""
    url = f"{INGESTER_URL.rstrip('/')}/gmail/drafts"
    body = json.dumps({"raw_rfc822": rfc822}).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 — localhost
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"ingester returned {exc.code}: {detail[:300]}") from exc

    gmail_draft_id = payload.get("gmail_draft_id")
    if not gmail_draft_id:
        raise RuntimeError(f"ingester response missing gmail_draft_id: {payload!r}")
    return {
        "gmail_draft_id": gmail_draft_id,
        "rfc822_size": len(rfc822),
        "mocked": False,
    }
