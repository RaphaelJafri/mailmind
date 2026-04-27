"""Agent-run bookkeeping: one row per agent invocation.

Wraps the lifecycle of an `agent_runs` row so every agent's call site looks
the same. P1 records start/finish, model, tokens, latency, status. P5
extends with cost, retries, and the Observability tab reads it back out.
"""

from __future__ import annotations

import json
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterator

import ulid

from . import db


@dataclass
class AgentRun:
    id: str
    agent_name: str
    model: str
    started_at: str
    task_id: str | None = None
    parent_run_id: str | None = None
    finished_at: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    latency_ms: int | None = None
    tools_called: list[dict[str, Any]] = field(default_factory=list)
    result_status: str = "running"
    error_message: str | None = None
    retry_count: int = 0
    stubbed: bool = False

    @classmethod
    def begin(
        cls,
        agent_name: str,
        model: str,
        *,
        task_id: str | None = None,
        parent_run_id: str | None = None,
    ) -> AgentRun:
        return cls(
            id=str(ulid.new()),
            agent_name=agent_name,
            model=model,
            task_id=task_id,
            parent_run_id=parent_run_id,
            started_at=datetime.now(timezone.utc).isoformat(),
        )


def _persist(run: AgentRun) -> None:
    with db.agent_runs() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO agent_runs (
              id, agent_name, task_id, parent_run_id, model,
              started_at, finished_at,
              input_tokens, output_tokens, cost_usd, latency_ms,
              tools_called_json, result_status, error_message,
              retry_count, stubbed
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run.id,
                run.agent_name,
                run.task_id,
                run.parent_run_id,
                run.model,
                run.started_at,
                run.finished_at,
                run.input_tokens,
                run.output_tokens,
                run.cost_usd,
                run.latency_ms,
                json.dumps(run.tools_called) if run.tools_called else None,
                run.result_status,
                run.error_message,
                run.retry_count,
                1 if run.stubbed else 0,
            ),
        )


@contextmanager
def record(
    agent_name: str,
    model: str,
    *,
    task_id: str | None = None,
    parent_run_id: str | None = None,
) -> Iterator[AgentRun]:
    """Context manager that opens an agent_runs row and persists it on exit.

    Usage:
        with record("triage_agent", model) as run:
            result = gemini_runner.generate_structured(...)
            run.input_tokens = result.input_tokens
            run.output_tokens = result.output_tokens
            run.latency_ms = result.latency_ms
            run.stubbed = result.stubbed
            run.result_status = "success"
    """
    run = AgentRun.begin(
        agent_name, model, task_id=task_id, parent_run_id=parent_run_id
    )
    _persist(run)  # row exists immediately so concurrent readers see "running"
    started = time.monotonic()
    try:
        yield run
        if run.result_status == "running":
            run.result_status = "success"
    except Exception as exc:
        run.result_status = "error"
        run.error_message = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        run.finished_at = datetime.now(timezone.utc).isoformat()
        if run.latency_ms is None:
            run.latency_ms = int((time.monotonic() - started) * 1000)
        _persist(run)
