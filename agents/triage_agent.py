"""Triage agent — proposes keep/skip/newsletter for unclassified senders.

Replaces v1's interactive `npm run filters` CLI. The agent reads
`raw.thread_dispositions` (Node-owned), proposes a disposition for each
unclassified sender with rationale, and writes them to `triage_proposals` for
the user to one-click approve in the Triage Queue tab.

Pattern: structured-output Gemini call (workers reason, scripts orchestrate —
v1 design principle carried forward). When P3 lands, the Query agent will be
the first true ADK ReAct agent. Triage stays as a worker because there's no
multi-turn reasoning needed — one bulk classification call is enough.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import ulid

from lib import agent_run, db, gemini_runner, prompts, raw_reader, schema, vertex_config

AGENT_NAME = "triage_agent"
DEFAULT_MIN_THREAD_COUNT = 1
DEFAULT_SENDER_LIMIT = 30


def run(*, min_thread_count: int = DEFAULT_MIN_THREAD_COUNT, limit: int = DEFAULT_SENDER_LIMIT) -> dict:
    """Run one triage pass. Returns a summary dict suitable for the API response.

    Idempotent: re-running over the same senders creates *new* proposals (one
    row per run), but the UI dedupes by sender + status=pending so the user
    only sees the latest pending row per sender.
    """
    senders = raw_reader.list_unclassified_senders(
        min_thread_count=min_thread_count, limit=limit
    )

    model = vertex_config.GEMINI_FLASH

    if not senders:
        with agent_run.record(AGENT_NAME, model) as run_row:
            run_row.result_status = "success"
            run_row.input_tokens = 0
            run_row.output_tokens = 0
        return {
            "run_id": run_row.id,
            "senders_considered": 0,
            "proposals_written": 0,
            "stubbed": False,
        }

    payload = {"senders": senders}
    user_prompt = (
        "Classify the following unclassified senders. Return one TriageProposal "
        "per sender, in input order.\n\n"
        f"<senders>\n{json.dumps(payload, indent=2)}\n</senders>"
    )

    response_schema = {
        "type": "object",
        "properties": {
            "proposals": {
                "type": "array",
                "items": prompts.load_schema("triage-proposal.schema.json"),
            }
        },
        "required": ["proposals"],
    }

    system_instruction = prompts.compose_system_prompt(
        "triage.md",
        schemas={"TRIAGE_PROPOSAL_SCHEMA": "triage-proposal.schema.json"},
    )

    with agent_run.record(AGENT_NAME, model) as run_row:
        result = gemini_runner.generate_structured(
            prompt=user_prompt,
            system_instruction=system_instruction,
            response_schema=response_schema,
            model=model,
            max_output_tokens=4096,
            temperature=0.1,
        )
        run_row.input_tokens = result.input_tokens
        run_row.output_tokens = result.output_tokens
        run_row.latency_ms = result.latency_ms
        run_row.stubbed = result.stubbed

        proposals = result.parsed.get("proposals", [])

        # Per-element schema validation. Drop invalid elements rather than
        # failing the whole run — the user still benefits from the good ones.
        valid: list[dict] = []
        invalid: list[dict] = []
        for p in proposals:
            try:
                schema.validate(p, "triage-proposal.schema.json")
                valid.append(p)
            except schema.SchemaValidationError as exc:
                invalid.append({"proposal": p, "errors": exc.errors})

        run_row.result_status = "success" if not invalid else "schema_partial_fail"

        written = _persist_proposals(valid, agent_run_id=run_row.id)

    return {
        "run_id": run_row.id,
        "senders_considered": len(senders),
        "proposals_returned": len(proposals),
        "proposals_written": written,
        "schema_invalid": len(invalid),
        "stubbed": result.stubbed,
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
        "latency_ms": result.latency_ms,
    }


def _persist_proposals(proposals: list[dict], *, agent_run_id: str) -> int:
    if not proposals:
        return 0
    now = datetime.now(timezone.utc).isoformat()
    with db.derived() as conn:
        for p in proposals:
            conn.execute(
                """
                INSERT INTO triage_proposals (
                  id, sender_email, proposed_disposition, confidence,
                  rationale, cited_user_context_section,
                  sample_subjects_json, thread_count,
                  proposed_at, agent_run_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(ulid.new()),
                    p["sender_email"],
                    p["proposed_disposition"],
                    p["confidence"],
                    p["rationale"],
                    p.get("cited_user_context_section"),
                    json.dumps(p.get("sample_subjects", [])),
                    int(p["thread_count"]),
                    now,
                    agent_run_id,
                ),
            )
    return len(proposals)


def list_proposals(status: str | None = "pending", limit: int = 100) -> list[dict]:
    """Read proposals back out for the UI. status='pending' means user hasn't
    approved/rejected yet."""
    with db.derived() as conn:
        if status == "pending":
            rows = conn.execute(
                """
                SELECT * FROM triage_proposals
                WHERE user_decision IS NULL
                ORDER BY proposed_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM triage_proposals ORDER BY proposed_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["sample_subjects"] = json.loads(d.pop("sample_subjects_json") or "[]")
        out.append(d)
    return out


def decide(proposal_id: str, decision: str) -> dict:
    """Record a user decision (approve/reject). Writing back to filters.yml is
    deferred to the ingester (Node owns config). For P1 we only mark the row;
    a follow-up CLI/endpoint can flush approved decisions to filters.yml."""
    if decision not in {"approve", "reject"}:
        raise ValueError(f"decision must be approve|reject, got {decision!r}")
    now = datetime.now(timezone.utc).isoformat()
    with db.derived() as conn:
        cur = conn.execute(
            """
            UPDATE triage_proposals
            SET user_decision = ?, reviewed_at = ?
            WHERE id = ? AND user_decision IS NULL
            """,
            (decision, now, proposal_id),
        )
        if cur.rowcount == 0:
            raise LookupError(f"proposal {proposal_id} not found or already decided")
        row = conn.execute(
            "SELECT * FROM triage_proposals WHERE id = ?", (proposal_id,)
        ).fetchone()
    return dict(row)
