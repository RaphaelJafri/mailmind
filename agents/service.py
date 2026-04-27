"""FastAPI agent service.

Talks to the Tauri shell over localhost. Default port: 8765 (override via PORT
env or --port flag).

P0 surface: /health.
P1 surface: /triage/run, /triage/proposals, /triage/proposals/{id}/{decide},
            /extract/run, /tag/run, /tags, /agent_runs.
P2 surface: /relationship/run, /contact_rollups, /reconcile/run,
            /next_steps, /next_steps/{id}/dismiss, /cadence/run, /followups,
            /corrections.
P3 surface: /query (SSE), /query/tools.
P4a surface: /draft/generate, /drafts (list/get/edit), /drafts/{id}/approve,
             /drafts/{id}/cancel, /drafts/{id}/save_as_gmail_draft,
             /drafts/{id}/reject, /audit_log, /permissions (read/grant).
P4b surface: gmail.send unlocked behind the Settings toggle; /drafts/{id}/send
             dispatches send_agent (which is the ONLY caller of
             gmail.send per ARCHITECTURE.md).

Real agent endpoints land here. Other phases (drafts, query) extend in later
milestones.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

import cadence_runner
import draft_agent
import extract_agent
import query_agent
import reconcile
import relationship_agent
import send_agent
import tagger_agent
import triage_agent
from lib import (
    approval as approval_lib,
    db,
    gemini_runner,
    gmail_writer,
    paths,
    query_tools,
    vertex_config,
)

# Pin dotenv to mailmind/agents/.env only. Without an explicit path, dotenv
# walks up the directory tree and picks up unrelated .env files (e.g. a
# desktop-level one from another project), which silently overrides our
# GCP project resolution.
_AGENTS_ENV = Path(__file__).resolve().parent / ".env"
load_dotenv(dotenv_path=_AGENTS_ENV, override=False)

STARTED_AT = datetime.now(timezone.utc).isoformat()


@asynccontextmanager
async def lifespan(_app: FastAPI):  # noqa: ANN001
    yield


app = FastAPI(title="mailmind-agents", version="0.2.0", lifespan=lifespan)

# In production the Tauri webview shares an origin with the bundled assets,
# but `npm run dev` serves the React app from Vite on :5173 which would
# otherwise be CORS-blocked. The sidecars only listen on 127.0.0.1 so the
# wildcard origin is safe here.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ----- /health -------------------------------------------------------------

@app.get("/health")
def health() -> dict:
    auth = vertex_config.detect_auth_mode()
    project = vertex_config.project_id()

    gemini_status = "skipped"
    gemini_error: str | None = None
    gemini_latency_ms: int | None = None
    gemini_stubbed = False

    if os.environ.get("MAILMIND_HEALTH_SKIP_GEMINI") == "1":
        gemini_status = "skipped"
    else:
        try:
            result = gemini_runner.health_check()
            gemini_status = "ok" if "PONG" in result.text.upper() else "unexpected_response"
            gemini_latency_ms = result.latency_ms
            gemini_stubbed = result.stubbed
        except Exception as exc:  # noqa: BLE001
            gemini_status = "error"
            gemini_error = f"{type(exc).__name__}: {exc}"

    return {
        "status": "ok",
        "sidecar": "mailmind-agents",
        "python_version": sys.version.split()[0],
        "data_dir": str(paths.data_dir()),
        "started_at": STARTED_AT,
        "gemini": {
            "status": gemini_status,
            "model": vertex_config.GEMINI_FLASH,
            "region": vertex_config.REGION,
            "project": project,
            "auth_mode": auth.kind,
            "auth_detail": auth.detail,
            "latency_ms": gemini_latency_ms,
            "stubbed": gemini_stubbed,
            "error": gemini_error,
        },
    }


# ----- /triage -------------------------------------------------------------

class TriageRunRequest(BaseModel):
    min_thread_count: int = 1
    limit: int = 30


@app.post("/triage/run")
def triage_run(req: TriageRunRequest | None = None) -> dict:
    req = req or TriageRunRequest()
    return triage_agent.run(min_thread_count=req.min_thread_count, limit=req.limit)


@app.get("/triage/proposals")
def triage_proposals(status: str = "pending", limit: int = 100) -> dict:
    proposals = triage_agent.list_proposals(status=status, limit=limit)
    return {"proposals": proposals, "count": len(proposals)}


class DecisionRequest(BaseModel):
    decision: str  # "approve" | "reject"


@app.post("/triage/proposals/{proposal_id}/decide")
def triage_decide(proposal_id: str, req: DecisionRequest) -> dict:
    try:
        return triage_agent.decide(proposal_id, req.decision)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ----- /extract ------------------------------------------------------------

class ExtractRunRequest(BaseModel):
    thread_ids: list[str]
    force: bool = False


@app.post("/extract/run")
def extract_run(req: ExtractRunRequest) -> dict:
    """Sequential per-thread extract. ParallelAgent fan-out is a follow-up."""
    if not req.thread_ids:
        raise HTTPException(status_code=400, detail="thread_ids must be non-empty")
    results: list[dict] = []
    failures: list[dict] = []
    for tid in req.thread_ids:
        try:
            results.append(extract_agent.run(tid, force=req.force))
        except Exception as exc:  # noqa: BLE001
            failures.append({"thread_id": tid, "error": f"{type(exc).__name__}: {exc}"})
    return {
        "extracted": results,
        "failures": failures,
        "count": len(results),
        "failed": len(failures),
    }


# ----- /tag ----------------------------------------------------------------

class TagRunRequest(BaseModel):
    message_ids: list[str]


@app.post("/tag/run")
def tag_run(req: TagRunRequest) -> dict:
    if not req.message_ids:
        raise HTTPException(status_code=400, detail="message_ids must be non-empty")
    results: list[dict] = []
    failures: list[dict] = []
    for mid in req.message_ids:
        try:
            results.append(tagger_agent.run(mid))
        except Exception as exc:  # noqa: BLE001
            failures.append({"message_id": mid, "error": f"{type(exc).__name__}: {exc}"})
    return {"tagged": results, "failures": failures, "count": len(results), "failed": len(failures)}


@app.get("/tags")
def tags(kind: str | None = None, value: str | None = None, limit: int = 200) -> dict:
    return {
        "summary": tagger_agent.list_tag_summary(),
        "tags": tagger_agent.list_tags(kind=kind, value=value, limit=limit),
    }


# ----- /relationship -------------------------------------------------------

class RelationshipRunRequest(BaseModel):
    contact_email: str | None = None
    force: bool = False


@app.post("/relationship/run")
def relationship_run(req: RelationshipRunRequest | None = None) -> dict:
    req = req or RelationshipRunRequest()
    if req.contact_email:
        try:
            return relationship_agent.run(req.contact_email, force=req.force)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
    return relationship_agent.run_all(force=req.force)


@app.get("/contact_rollups")
def contact_rollups(limit: int = 100) -> dict:
    rollups = relationship_agent.list_rollups(limit=limit)
    return {"rollups": rollups, "count": len(rollups)}


@app.get("/contact_rollups/{contact_email}")
def contact_rollup(contact_email: str) -> dict:
    r = relationship_agent.get_rollup(contact_email)
    if r is None:
        raise HTTPException(status_code=404, detail=f"no rollup for {contact_email!r}")
    return r


# ----- /reconcile + /next_steps -------------------------------------------

class ReconcileRunRequest(BaseModel):
    contact_email: str | None = None
    dry_run: bool = False


@app.post("/reconcile/run")
def reconcile_run(req: ReconcileRunRequest | None = None) -> dict:
    req = req or ReconcileRunRequest()
    return reconcile.run(contact=req.contact_email, dry_run=req.dry_run)


@app.get("/next_steps")
def next_steps(status: str = "pending", limit: int = 200) -> dict:
    rows = reconcile.list_next_steps(status=status, limit=limit)
    return {"next_steps": rows, "count": len(rows)}


class DismissRequest(BaseModel):
    user_note: str | None = None


@app.post("/next_steps/{step_id}/dismiss")
def next_step_dismiss(step_id: str, req: DismissRequest | None = None) -> dict:
    req = req or DismissRequest()
    try:
        return reconcile.dismiss_step(step_id, user_note=req.user_note)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/corrections")
def corrections(limit: int = 100) -> dict:
    rows = reconcile.list_corrections(limit=limit)
    return {"corrections": rows, "count": len(rows)}


# ----- /cadence + /followups ----------------------------------------------

@app.post("/cadence/run")
def cadence_run() -> dict:
    return cadence_runner.run()


@app.get("/followups")
def followups(bucket: str | None = None) -> dict:
    """Return the latest follow-up report.

    `bucket=overdue` filters to overdue entries on both sides; `bucket=cold`
    filters to cold; otherwise returns the full report.
    """
    report = cadence_runner.compute_followups()
    if bucket in {"overdue", "cold"}:
        report = {
            **report,
            "they_owe_you": [e for e in report["they_owe_you"] if e["urgency"] == bucket],
            "you_owe_them": [e for e in report["you_owe_them"] if e["urgency"] == bucket],
        }
    return report


# ----- /query (P3 — ReAct over read-only tools) ---------------------------

class QueryRunRequest(BaseModel):
    question: str
    max_tool_calls: int | None = None
    max_wall_seconds: float | None = None


@app.post("/query")
def query_post(req: QueryRunRequest) -> StreamingResponse:
    """Stream the ReAct loop as Server-Sent Events.

    Each event is a single line of JSON, framed by SSE's `data:` prefix and a
    blank line. The webview reads them with EventSource and renders thoughts +
    tool calls + tool results live. The final two events are always `answer`
    then `done` (or `error` then `done`).
    """
    if not req.question.strip():
        raise HTTPException(status_code=400, detail="question is required")

    kwargs: dict[str, Any] = {}
    if req.max_tool_calls is not None:
        kwargs["max_tool_calls"] = req.max_tool_calls
    if req.max_wall_seconds is not None:
        kwargs["max_wall_seconds"] = req.max_wall_seconds

    def _stream() -> Any:
        try:
            for ev in query_agent.run(req.question, **kwargs):
                yield f"data: {ev.to_json()}\n\n"
        except Exception as exc:  # noqa: BLE001
            err = json.dumps({"kind": "error", "error": f"{type(exc).__name__}: {exc}"})
            yield f"data: {err}\n\n"
            done = json.dumps({"kind": "done", "reason": "exception"})
            yield f"data: {done}\n\n"

    return StreamingResponse(_stream(), media_type="text/event-stream")


@app.get("/query/tools")
def query_tools_list() -> dict:
    """Manifest of read-only tools the query agent (and MCP server) expose."""
    return {"tools": query_tools.manifest(), "count": len(query_tools.TOOLS)}


# ----- /draft + /drafts (P4 — drafts only, no send yet) -------------------
#
# The lifecycle, flowing through these endpoints, is:
#
#   POST /draft/generate {thread_id, intent}
#     → creates a `drafts` row, status=pending, returns {draft_id, draft, ...}
#   GET  /drafts?status=pending     → UI list
#   PATCH /drafts/{id}              → user edits subject/body/recipients;
#                                     recomputes draft_hash; refused if not pending.
#   POST /drafts/{id}/approve {action='save_as_draft'}
#     → creates `approvals` row, draft.status=approved, audit_log row.
#   POST /drafts/{id}/save_as_gmail_draft
#     → caller has waited the undo window; we verify hash, invoke gmail_writer,
#       audit_log row, draft.status=saved_as_draft.
#   POST /drafts/{id}/cancel  (during undo window)
#     → approval.cancelled, draft.status reverts to pending.
#   POST /drafts/{id}/reject  (any time before sent)
#     → draft.status=rejected, audit_log row.

class DraftGenerateRequest(BaseModel):
    thread_id: str
    intent: str


@app.post("/draft/generate")
def draft_generate(req: DraftGenerateRequest) -> dict:
    if not req.thread_id.strip():
        raise HTTPException(status_code=400, detail="thread_id is required")
    if not req.intent.strip():
        raise HTTPException(status_code=400, detail="intent is required")
    try:
        return draft_agent.run(req.thread_id, req.intent)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/drafts")
def drafts_list(status: str | None = None, limit: int = 100) -> dict:
    rows = draft_agent.list_drafts(status=status, limit=limit)
    return {"drafts": rows, "count": len(rows)}


@app.get("/drafts/{draft_id}")
def drafts_get(draft_id: str) -> dict:
    row = draft_agent.get_draft(draft_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"no draft {draft_id!r}")
    return row


class DraftEditRequest(BaseModel):
    subject: str | None = None
    body: str | None = None
    to_emails: list[str] | None = None
    cc_emails: list[str] | None = None
    bcc_emails: list[str] | None = None


@app.patch("/drafts/{draft_id}")
def drafts_edit(draft_id: str, req: DraftEditRequest) -> dict:
    try:
        return draft_agent.update_draft_body(
            draft_id,
            subject=req.subject,
            body=req.body,
            to_emails=req.to_emails,
            cc_emails=req.cc_emails,
            bcc_emails=req.bcc_emails,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except draft_agent.DraftEditError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


class DraftApproveRequest(BaseModel):
    action: str = "save_as_draft"  # P4a: only save_as_draft is reachable
    approved_by: str | None = None
    undo_window_seconds: int | None = None


@app.post("/drafts/{draft_id}/approve")
def drafts_approve(draft_id: str, req: DraftApproveRequest | None = None) -> dict:
    req = req or DraftApproveRequest()
    approver = req.approved_by or os.environ.get("MAILMIND_APPROVER_EMAIL", "owner@mailmind.local")
    try:
        return approval_lib.approve(
            draft_id,
            action=req.action,
            approved_by=approver,
            undo_window_seconds=req.undo_window_seconds,
        )
    except approval_lib.DraftNotFound as exc:
        raise HTTPException(status_code=404, detail={"code": exc.code, "message": str(exc)}) from exc
    except approval_lib.SendNotPermitted as exc:
        raise HTTPException(status_code=403, detail={"code": exc.code, "message": str(exc)}) from exc
    except approval_lib.ApprovalError as exc:
        raise HTTPException(status_code=400, detail={"code": exc.code, "message": str(exc)}) from exc


class CancelRequest(BaseModel):
    approval_id: str
    reason: str | None = None


@app.post("/drafts/{draft_id}/cancel")
def drafts_cancel(draft_id: str, req: CancelRequest) -> dict:
    try:
        result = approval_lib.cancel(req.approval_id, reason=req.reason)
    except approval_lib.ApprovalError as exc:
        raise HTTPException(status_code=400, detail={"code": exc.code, "message": str(exc)}) from exc
    if result["draft_id"] != draft_id:
        raise HTTPException(
            status_code=400,
            detail={"code": "draft_mismatch", "message": "approval is not for this draft"},
        )
    return result


class ExecuteRequest(BaseModel):
    approval_id: str


@app.post("/drafts/{draft_id}/save_as_gmail_draft")
def drafts_save(draft_id: str, req: ExecuteRequest) -> dict:
    """Save the draft to Gmail Drafts. Caller is responsible for waiting the
    undo window — this endpoint executes immediately. The Tauri shell does
    that wait client-side so the API stays stateless and synchronous."""
    try:
        result = approval_lib.execute_save_as_draft(
            req.approval_id, gmail_writer=gmail_writer.save_as_draft
        )
    except approval_lib.ApprovalError as exc:
        raise HTTPException(status_code=400, detail={"code": exc.code, "message": str(exc)}) from exc
    if result["draft_id"] != draft_id:
        raise HTTPException(
            status_code=400,
            detail={"code": "draft_mismatch", "message": "approval is not for this draft"},
        )
    return result


@app.post("/drafts/{draft_id}/send")
def drafts_send(draft_id: str, req: ExecuteRequest) -> dict:
    """P4b. Execute a send-action approval. Same shape as save_as_gmail_draft
    but routes through `send_agent` — the only place in the codebase that
    invokes gmail.send (per ARCHITECTURE.md invariant #1).

    Refuses with 403 if `gmail.send` scope isn't granted. Refuses with 400
    `hash_mismatch` if the draft body changed between approval and send.
    Refuses with 400 `approval_expired` past the 5-min TTL.
    """
    try:
        result = send_agent.run(req.approval_id)
    except approval_lib.SendNotPermitted as exc:
        raise HTTPException(status_code=403, detail={"code": exc.code, "message": str(exc)}) from exc
    except approval_lib.ApprovalError as exc:
        raise HTTPException(status_code=400, detail={"code": exc.code, "message": str(exc)}) from exc
    if result["draft_id"] != draft_id:
        raise HTTPException(
            status_code=400,
            detail={"code": "draft_mismatch", "message": "approval is not for this draft"},
        )
    return result


class RejectRequest(BaseModel):
    reason: str | None = None


@app.post("/drafts/{draft_id}/reject")
def drafts_reject(draft_id: str, req: RejectRequest | None = None) -> dict:
    req = req or RejectRequest()
    try:
        return approval_lib.reject(draft_id, reason=req.reason)
    except approval_lib.DraftNotFound as exc:
        raise HTTPException(status_code=404, detail={"code": exc.code, "message": str(exc)}) from exc
    except approval_lib.ApprovalError as exc:
        raise HTTPException(status_code=400, detail={"code": exc.code, "message": str(exc)}) from exc


# ----- /audit_log + /permissions ------------------------------------------

@app.get("/audit_log")
def audit_log_list(limit: int = 100, event_type: str | None = None) -> dict:
    """Append-only audit feed. Used by the Sent tab to show the chain.

    Verifies the chain integrity (sha256 of prev row matches `prev_hash` on
    every row) and surfaces a `chain_ok` flag the UI can render as a
    health indicator.
    """
    with db.derived() as conn:
        if event_type:
            rows = conn.execute(
                "SELECT * FROM audit_log WHERE event_type = ? ORDER BY event_at DESC LIMIT ?",
                (event_type, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM audit_log ORDER BY event_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
    chain = approval_lib.verify_audit_chain()
    return {
        "events": [dict(r) for r in rows],
        "count": len(rows),
        "chain_ok": chain["ok"],
        "chain_total": chain["total"],
        "broken_at": chain["broken_at"],
    }


@app.get("/permissions")
def permissions_get() -> dict:
    """Return the current OAuth-scope grants. P4a default: nothing granted →
    no Save / Send buttons until the user opts in."""
    p = paths.config_dir() / "oauth_scopes.json"
    if not p.exists():
        return {"gmail.compose": False, "gmail.send": False, "configured": False}
    try:
        data = json.loads(p.read_text())
    except Exception:  # noqa: BLE001
        data = {}
    return {
        "gmail.compose": bool(data.get("gmail.compose")),
        "gmail.send": bool(data.get("gmail.send")),
        "configured": True,
    }


class PermissionsRequest(BaseModel):
    gmail_compose: bool | None = None
    gmail_send: bool | None = None


@app.post("/permissions")
def permissions_set(req: PermissionsRequest) -> dict:
    """Grant / revoke local-side OAuth-scope flags. P4a only flips
    gmail.compose; gmail.send stays false until P4b unblocks it."""
    p = paths.config_dir() / "oauth_scopes.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    current = {"gmail.compose": False, "gmail.send": False}
    if p.exists():
        try:
            current.update(json.loads(p.read_text()))
        except Exception:
            pass
    if req.gmail_compose is not None:
        current["gmail.compose"] = bool(req.gmail_compose)
    if req.gmail_send is not None:
        # P4b: granting gmail.send unlocks the Approve & Send button. The
        # actual send still requires (a) an approval row whose hash matches
        # the current draft body, and (b) the 30s undo window expiring on
        # the client side. We belt-and-suspenders refuse if the user tries
        # to grant send without first having compose — the UX is meant to
        # be additive: drafts work, then sends.
        if req.gmail_send and not current.get("gmail.compose"):
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "compose_required",
                    "message": "Grant gmail.compose before enabling gmail.send.",
                },
            )
        current["gmail.send"] = bool(req.gmail_send)
    p.write_text(json.dumps(current, indent=2))
    # Audit-log the grant/revoke for visibility. Distinguish revoke (any
    # explicit toggle to false) from grant.
    granting = any([req.gmail_compose, req.gmail_send])
    with db.derived() as conn:
        approval_lib._append_audit(  # noqa: SLF001 — internal helper, intentional
            conn,
            event_type="auth_grant" if granting else "auth_revoke",
            payload=current,
        )
    return current


# ----- /agent_runs ---------------------------------------------------------

@app.get("/agent_runs")
def agent_runs(limit: int = 50, agent_name: str | None = None) -> dict:
    with db.agent_runs() as conn:
        if agent_name:
            rows = conn.execute(
                "SELECT * FROM agent_runs WHERE agent_name = ? "
                "ORDER BY started_at DESC LIMIT ?",
                (agent_name, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM agent_runs ORDER BY started_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
    return {"runs": [_row_to_dict(r) for r in rows]}


def _row_to_dict(r: Any) -> dict:
    d = dict(r)
    if isinstance(d.get("stubbed"), int):
        d["stubbed"] = bool(d["stubbed"])
    return d


# ----- main ----------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8765")))
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()

    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
