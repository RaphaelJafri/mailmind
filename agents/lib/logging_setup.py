"""Structured logging for the agent service.

Per BUILD §22: every agent invocation lands as one JSON line in
`~/Library/Logs/mailmind/agent-service.log` (or `MAILMIND_LOGS_DIR/`),
rotated daily, 7-day retention. The Observability tab queries
`agent_runs.sqlite` for charts, but the log file is the durable freeform
event stream — it survives DB resets and lets you grep an incident.

`init_logging()` is idempotent and is called from `service.py` lifespan.
Tests don't call it; they read the underlying agent_runs table directly.

The agent_run lifecycle hooks (`emit_run_start`, `emit_run_finish`) are
called from `agent_run.record` so every agent gets file logging for free.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
from pathlib import Path
from typing import Any

from . import paths


_LOGGER_NAME = "mailmind.agents"


class _JsonLineFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base: dict[str, Any] = {
            "ts": self.formatTime(record, datefmt="%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        extra = getattr(record, "extra", None)
        if isinstance(extra, dict):
            base.update(extra)
        for k in ("event", "agent_name", "task_id", "run_id"):
            if hasattr(record, k):
                base[k] = getattr(record, k)
        return json.dumps(base, default=str)


def _log_path() -> Path:
    """Resolve the log directory & filename. `MAILMIND_LOGS_DIR` overrides
    the default macOS location for tests + `npm run demo`."""
    return paths.logs_dir() / "agent-service.log"


def init_logging(*, level: int = logging.INFO) -> logging.Logger:
    """Ensure the mailmind logger has a TimedRotatingFileHandler pointing at
    the current log path. Idempotent — calling repeatedly with the same
    path reuses the existing handler. If `MAILMIND_LOGS_DIR` changed (tests
    use a fresh tmp dir per test) the handler is rebuilt.

    Format is one JSON object per line; no quoting tricks needed downstream.
    """
    logger = logging.getLogger(_LOGGER_NAME)
    logger.setLevel(level)
    logger.propagate = False  # don't double-print into uvicorn's stdout

    log_path = _log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True)

    target = str(log_path)
    for h in list(logger.handlers):
        if isinstance(h, logging.handlers.TimedRotatingFileHandler):
            if getattr(h, "baseFilename", None) == target:
                return logger  # already wired correctly
            # Path changed — close and remove so we can re-attach.
            try:
                h.close()
            except Exception:  # noqa: BLE001
                pass
            logger.removeHandler(h)

    # Daily rotation, keep 7 days. Standard library, no extra deps.
    handler = logging.handlers.TimedRotatingFileHandler(
        log_path,
        when="midnight",
        interval=1,
        backupCount=7,
        encoding="utf-8",
        utc=True,
    )
    handler.setFormatter(_JsonLineFormatter())
    logger.addHandler(handler)
    return logger


def get_logger() -> logging.Logger:
    return init_logging()


def emit_run_event(
    *,
    event: str,
    agent_name: str,
    run_id: str,
    task_id: str | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """Hook called by `agent_run.record` at start + finish of every run.

    `event` is `agent_run_start` or `agent_run_finish`. The `extra` dict is
    forwarded into the JSON line so cost_usd, latency_ms, status, etc. all
    appear without claim-checking.
    """
    if os.environ.get("MAILMIND_DISABLE_AGENT_LOG") == "1":
        return
    try:
        logger = get_logger()
        payload: dict[str, Any] = {"event": event, "agent_name": agent_name, "run_id": run_id}
        if task_id is not None:
            payload["task_id"] = task_id
        if extra:
            payload.update(extra)
        logger.info(event, extra={"extra": payload, **payload})
    except Exception:  # noqa: BLE001
        # Logging failure must never break an agent run. Swallow and move on.
        return


def tail_lines(*, limit: int = 200) -> list[dict]:
    """Read the last `limit` lines of the log file, parsed as JSON.

    Used by `/observability/log_tail` so the dashboard can show recent
    structured events without re-querying the DB.
    """
    p = _log_path()
    if not p.exists():
        return []
    try:
        # Read last ~64KB; cheap and bounded.
        size = p.stat().st_size
        with p.open("rb") as f:
            f.seek(max(0, size - 64 * 1024))
            blob = f.read().decode("utf-8", errors="replace")
        out: list[dict] = []
        for line in blob.splitlines()[-limit:]:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:  # noqa: BLE001
                # Non-JSON lines (rare — only if a stray write got in) are
                # surfaced as freeform so you can see them.
                out.append({"raw": line})
        return out
    except Exception:  # noqa: BLE001
        return []
