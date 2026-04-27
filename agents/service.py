"""FastAPI agent service.

Talks to the Tauri shell over localhost. Default port: 8765 (override via PORT
env or --port flag).

P0 surface: /health only. Real agent endpoints land in P1+ per BUILD.md §10.
"""

from __future__ import annotations

import argparse
import os
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from dotenv import load_dotenv
from fastapi import FastAPI

from lib import gemini_runner, paths, vertex_config

load_dotenv()

STARTED_AT = datetime.now(timezone.utc).isoformat()


@asynccontextmanager
async def lifespan(_app: FastAPI):  # noqa: ANN001
    yield


app = FastAPI(title="mailmind-agents", version="0.1.0", lifespan=lifespan)


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


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8765")))
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()

    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
