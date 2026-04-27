"""Extract agent — one thread → one structured ThreadFacts row.

Worker pattern: structured-output Gemini call. Caller (the orchestrator script
or service endpoint) decides which thread(s) to pass in. Concurrency / fan-out
will move to ADK ParallelAgent in a later milestone if it earns its keep.

Idempotency: we key on `raw.threads.content_hash`. If `thread_facts` already
has a row with the same hash, we skip the call (mirroring v1 extract-runner).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from lib import agent_run, db, gemini_runner, paths, prompts, raw_reader, schema, vertex_config

AGENT_NAME = "extract_agent"


def _existing_hash(thread_id: str) -> str | None:
    p = paths.derived_db_path()
    if not p.exists():
        return None
    conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT content_hash FROM thread_facts WHERE thread_id = ?",
            (thread_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        # thread_facts table may not exist yet — Node ingester creates it on
        # first openDerived(). Fall through and re-extract.
        return None
    finally:
        conn.close()
    return row["content_hash"] if row else None


def _current_hash(thread_id: str) -> str | None:
    p = paths.raw_db_path()
    if not p.exists():
        return None
    conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT content_hash FROM threads WHERE thread_id = ?",
            (thread_id,),
        ).fetchone()
    finally:
        conn.close()
    return row["content_hash"] if row else None


def run(thread_id: str, *, force: bool = False) -> dict:
    """Extract facts for one thread. Returns a summary dict."""
    thread = raw_reader.get_thread(thread_id)
    if thread is None:
        raise LookupError(f"thread {thread_id!r} not found in raw.sqlite")

    current = _current_hash(thread_id)
    existing = _existing_hash(thread_id)
    if not force and current and existing == current:
        return {
            "thread_id": thread_id,
            "skipped": True,
            "reason": "content_hash unchanged",
        }

    model = vertex_config.GEMINI_FLASH

    user_prompt = (
        "Extract facts from the following thread. Return one ThreadFacts JSON object.\n\n"
        f"<thread>\n{json.dumps(thread, indent=2)}\n</thread>"
    )
    response_schema = prompts.load_schema("thread-facts.schema.json")
    system_instruction = prompts.compose_system_prompt(
        "extract.md",
        schemas={"THREAD_FACTS_SCHEMA": "thread-facts.schema.json"},
    )

    with agent_run.record(AGENT_NAME, model, task_id=thread_id) as run_row:
        result = gemini_runner.generate_structured(
            prompt=user_prompt,
            system_instruction=system_instruction,
            response_schema=response_schema,
            model=model,
            max_output_tokens=4096,
            agent_name=AGENT_NAME,
        )
        run_row.input_tokens = result.input_tokens
        run_row.output_tokens = result.output_tokens
        run_row.latency_ms = result.latency_ms
        run_row.cost_usd = result.cost_usd
        run_row.stubbed = result.stubbed

        facts = result.parsed
        try:
            schema.validate(facts, "thread-facts.schema.json")
        except schema.SchemaValidationError:
            run_row.result_status = "schema_fail"
            raise

        _check_provenance(facts, thread)

        _persist_facts(thread_id, current, facts, model)
        run_row.result_status = "success"

    return {
        "thread_id": thread_id,
        "skipped": False,
        "stubbed": result.stubbed,
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
        "latency_ms": result.latency_ms,
        "summary": facts.get("summary"),
        "confidence": facts.get("confidence"),
    }


def _check_provenance(facts: dict, thread: dict) -> None:
    """Every cited message_id must exist in the thread payload. Catches
    fabrication early — no source IDs from outer space."""
    valid_ids = {m["message_id"] for m in thread["messages"]}

    def _walk(items: list[dict], context: str) -> None:
        for it in items:
            for mid in it.get("source_message_ids", []):
                if mid not in valid_ids:
                    raise schema.SchemaValidationError(
                        [f"  fabricated message_id {mid!r} in {context}"],
                        schema_name="thread-facts.schema.json",
                    )

    _walk(facts.get("commitments_by_user", []), "commitments_by_user")
    _walk(facts.get("commitments_by_others", []), "commitments_by_others")
    _walk(facts.get("open_questions", []), "open_questions")


def _persist_facts(thread_id: str, content_hash: str | None, facts: dict, model: str) -> None:
    """Write to derived.sqlite/thread_facts. The Node ingester also writes
    here on the v1 schema; both sides agree on shape (verbatim port)."""
    now = datetime.now(timezone.utc).isoformat()
    p = paths.derived_db_path()
    conn = sqlite3.connect(p)
    try:
        conn.execute("PRAGMA journal_mode = WAL;")
        # Ensure the table exists in case the Node ingester hasn't run yet.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS thread_facts (
              thread_id     TEXT PRIMARY KEY,
              content_hash  TEXT NOT NULL,
              extracted_at  TIMESTAMP NOT NULL,
              model_version TEXT NOT NULL,
              facts_json    TEXT NOT NULL,
              confidence    TEXT NOT NULL,
              worker_log_path TEXT
            )
            """
        )
        conn.execute(
            """
            INSERT INTO thread_facts (
              thread_id, content_hash, extracted_at, model_version,
              facts_json, confidence, worker_log_path
            ) VALUES (?, ?, ?, ?, ?, ?, NULL)
            ON CONFLICT(thread_id) DO UPDATE SET
              content_hash = excluded.content_hash,
              extracted_at = excluded.extracted_at,
              model_version = excluded.model_version,
              facts_json = excluded.facts_json,
              confidence = excluded.confidence
            """,
            (
                thread_id,
                content_hash or "",
                now,
                model,
                json.dumps(facts),
                facts.get("confidence", "low"),
            ),
        )
        conn.commit()
    finally:
        conn.close()
