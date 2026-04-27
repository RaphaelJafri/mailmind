"""Gemini call wrapper.

Replaces v1's claude-runner.mjs. Uses google-genai SDK against Vertex AI by
default (with ADC); falls back to direct Gemini API if GOOGLE_GENAI_API_KEY is
set.

P0 surface: a single `health_check()` that does a token-bounded ping. Real
structured-output calls land in P1 with the agents.

The stub-responses fixture (BUILD.md §17) is checked here: if
MAILMIND_STUB_RESPONSES is set, no real call is made.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

from google import genai

from . import vertex_config


@dataclass
class GeminiResult:
    text: str
    input_tokens: int
    output_tokens: int
    latency_ms: int
    model: str
    stubbed: bool = False


def _client() -> genai.Client:
    auth = vertex_config.detect_auth_mode()
    if auth.kind == "api_key":
        return genai.Client(api_key=os.environ["GOOGLE_GENAI_API_KEY"])
    return genai.Client(
        vertexai=True,
        project=vertex_config.project_id(),
        location=vertex_config.REGION,
    )


def _stub_path() -> Path | None:
    env = os.environ.get("MAILMIND_STUB_RESPONSES")
    return Path(env).expanduser() if env else None


def _stub_lookup(model: str, prompt: str) -> dict | None:
    p = _stub_path()
    if not p or not p.exists():
        return None
    try:
        data = json.loads(p.read_text())
    except Exception:
        return None
    h = hashlib.sha256(f"{model}::{prompt}".encode()).hexdigest()
    return data.get(h)


def health_check() -> GeminiResult:
    """Tiny round-trip to confirm the model + auth + region all work."""
    model = vertex_config.GEMINI_FLASH
    prompt = "Reply with exactly: PONG"

    if hit := _stub_lookup(model, prompt):
        return GeminiResult(
            text=hit["output"],
            input_tokens=hit.get("input_tokens", 0),
            output_tokens=hit.get("output_tokens", 0),
            latency_ms=hit.get("latency_ms", 0),
            model=model,
            stubbed=True,
        )

    client = _client()
    start = time.monotonic()
    # Gemini 2.5 flash is a reasoning model — internal thinking consumes
    # output tokens before the visible response. 256 leaves room for both.
    # Real agents will tune per-task; this is the health check only.
    response = client.models.generate_content(
        model=model,
        contents=prompt,
        config={"max_output_tokens": 256, "temperature": 0.0},
    )
    latency_ms = int((time.monotonic() - start) * 1000)
    usage = getattr(response, "usage_metadata", None)
    return GeminiResult(
        text=(response.text or "").strip(),
        input_tokens=getattr(usage, "prompt_token_count", 0) if usage else 0,
        output_tokens=getattr(usage, "candidates_token_count", 0) if usage else 0,
        latency_ms=latency_ms,
        model=model,
        stubbed=False,
    )
