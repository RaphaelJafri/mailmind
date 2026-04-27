"""Tagger agent — one message → one MessageTag row.

Worker pattern. The agent assigns three tag dimensions:
- urgency (5 values)
- category (8 values)
- project (free-form, drawn from user-context active projects)

We persist one row per (message_id, tag_kind, tag_value) tuple in
`message_tags` so tags can be queried/filtered in the UI without re-parsing
JSON.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from lib import agent_run, db, gemini_runner, prompts, raw_reader, schema, vertex_config

AGENT_NAME = "tagger_agent"


def run(message_id: str) -> dict:
    msg = raw_reader.get_message(message_id)
    if msg is None:
        raise LookupError(f"message {message_id!r} not found")

    model = vertex_config.GEMINI_FLASH
    user_prompt = (
        "Tag the following message. Return one MessageTag JSON object.\n\n"
        f"<message>\n{json.dumps(msg, indent=2)}\n</message>"
    )
    response_schema = prompts.load_schema("tag.schema.json")
    system_instruction = prompts.compose_system_prompt(
        "tagger.md",
        schemas={"TAG_SCHEMA": "tag.schema.json"},
    )

    with agent_run.record(AGENT_NAME, model, task_id=message_id) as run_row:
        result = gemini_runner.generate_structured(
            prompt=user_prompt,
            system_instruction=system_instruction,
            response_schema=response_schema,
            model=model,
            max_output_tokens=512,
        )
        run_row.input_tokens = result.input_tokens
        run_row.output_tokens = result.output_tokens
        run_row.latency_ms = result.latency_ms
        run_row.stubbed = result.stubbed

        tag = result.parsed
        # The model is told to echo `message_id`, but be defensive.
        tag["message_id"] = message_id
        try:
            schema.validate(tag, "tag.schema.json")
        except schema.SchemaValidationError:
            run_row.result_status = "schema_fail"
            raise

        rows_written = _persist_tag(tag, model, run_row.id)
        run_row.result_status = "success"

    return {
        "message_id": message_id,
        "rows_written": rows_written,
        "stubbed": result.stubbed,
        "tags": tag["tags"],
        "confidence": tag.get("confidence"),
        "latency_ms": result.latency_ms,
    }


def _persist_tag(tag: dict, model: str, agent_run_id: str) -> int:
    now = datetime.now(timezone.utc).isoformat()
    confidence = tag.get("confidence", "low")
    rationale = tag.get("rationale")
    triples = _flatten_tag(tag)
    if not triples:
        return 0
    with db.derived() as conn:
        # Replace any prior rows for this message — re-tagging is idempotent.
        conn.execute("DELETE FROM message_tags WHERE message_id = ?", (tag["message_id"],))
        for kind, value in triples:
            conn.execute(
                """
                INSERT INTO message_tags (
                  message_id, tag_kind, tag_value, confidence, rationale,
                  tagged_at, model_version, agent_run_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    tag["message_id"],
                    kind,
                    value,
                    confidence,
                    rationale,
                    now,
                    model,
                    agent_run_id,
                ),
            )
    return len(triples)


def _flatten_tag(tag: dict) -> list[tuple[str, str]]:
    tags = tag.get("tags", {})
    out: list[tuple[str, str]] = []
    if v := tags.get("urgency"):
        out.append(("urgency", v))
    if v := tags.get("category"):
        out.append(("category", v))
    for proj in tags.get("project", []) or []:
        if proj:
            out.append(("project", proj))
    return out


def list_tags(kind: str | None = None, value: str | None = None, limit: int = 200) -> list[dict]:
    with db.derived() as conn:
        clauses = []
        params: list = []
        if kind:
            clauses.append("tag_kind = ?")
            params.append(kind)
        if value:
            clauses.append("tag_value = ?")
            params.append(value)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        rows = conn.execute(
            f"SELECT * FROM message_tags {where} ORDER BY tagged_at DESC LIMIT ?",
            params,
        ).fetchall()
    return [dict(r) for r in rows]


def list_tag_summary() -> list[dict]:
    """Counts per (kind, value), for the Tags tab."""
    with db.derived() as conn:
        rows = conn.execute(
            """
            SELECT tag_kind, tag_value, COUNT(*) AS message_count
            FROM message_tags
            GROUP BY tag_kind, tag_value
            ORDER BY tag_kind, message_count DESC
            """
        ).fetchall()
    return [dict(r) for r in rows]
