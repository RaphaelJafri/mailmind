"""Send agent — the *only* path to `users.messages.send`.

Per BUILD §7 invariant #1, every Gmail send goes through this agent. There
is no other code path that should be allowed to call gmail.send. The
"agent" here is intentionally thin — most of the safety lives in
`lib.approval`, which:

  - verifies the requester is the mailbox owner;
  - verifies the approval row is non-cancelled, non-expired;
  - verifies the draft hash hasn't drifted since approval;
  - calls the writer (this module's `_call_gmail_send` indirection);
  - writes the audit_log row;
  - flips draft.status to `sent` and stores the gmail_message_id.

The actual network call lives in `lib.gmail_writer.send`, which today
returns a deterministic mock id (P4a/P4b dev mode). The real Google API
client lands once the user grants `gmail.send` and re-runs OAuth.

Why expose this as a thin module instead of inlining into the service
layer: ARCHITECTURE.md says "send_agent is the only place gmail.send is
called." Having the file makes that grep-able and gives us a place to add
P5's per-task budget caps without polluting service.py.
"""

from __future__ import annotations

from typing import Any

from lib import agent_run, approval, gmail_writer, vertex_config


AGENT_NAME = "send_agent"


def run(approval_id: str) -> dict:
    """Execute the send for `approval_id`. Caller (the FastAPI handler) has
    already waited the 30s undo window client-side.

    Refuses immediately if `gmail.send` scope isn't granted — this is a
    belt-and-suspenders check; `approval.execute_send` does the same gate.
    """
    # The send agent doesn't call Gemini — there's nothing to reason about.
    # We still record one agent_runs row so the Observability tab has a
    # complete picture of who did what when.
    with agent_run.record(AGENT_NAME, model=vertex_config.GEMINI_FLASH) as run_row:
        result = approval.execute_send(approval_id, gmail_writer=gmail_writer.send)
        run_row.result_status = "success"
        run_row.cost_usd = 0.0  # no LLM call; the seam itself is free.
        run_row.tools_called = [
            {"tool": "gmail.send", "approval_id": approval_id, "draft_id": result["draft_id"]}
        ]
    return result


def is_send_permitted() -> bool:
    """Helper for the service layer / UI — read-only check on the OAuth
    grant file. Mirrors the check in `approval.execute_send`."""
    return "gmail.send" in approval._granted_scopes()  # noqa: SLF001 — internal helper, intentional


__all__ = ["run", "is_send_permitted", "AGENT_NAME"]
