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
import extract_agent
import query_agent
import reconcile
import relationship_agent
import tagger_agent
import triage_agent
from lib import db, gemini_runner, paths, query_tools, vertex_config

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
