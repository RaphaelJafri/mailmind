"""Gemini call wrapper.

Replaces v1's claude-runner.mjs. Uses google-genai SDK against Vertex AI by
default (with ADC); falls back to direct Gemini API if GOOGLE_GENAI_API_KEY is
set.

Two surfaces:
- `health_check()` — small text round-trip to confirm auth + region (P0).
- `generate_structured()` — JSON-mode call with response_schema (P1+). Used by
  every worker agent (triage, extract, tagger, …). Schema enforcement happens
  here; semantic validation happens in `lib/schema.py`.

Stub mode: if `MAILMIND_STUB_RESPONSES` points at a JSON file, lookups are
keyed by `sha256(model::prompt)` and no real call is made. Used by tests and
by `npm run demo` against fixture threads.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from google import genai

from . import cost_guard, vertex_config


@dataclass
class GeminiResult:
    text: str
    input_tokens: int
    output_tokens: int
    latency_ms: int
    model: str
    stubbed: bool = False
    cost_usd: float = 0.0


@dataclass
class StructuredResult:
    parsed: Any
    raw_text: str
    input_tokens: int
    output_tokens: int
    latency_ms: int
    model: str
    stubbed: bool = False
    cost_usd: float = 0.0


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


def _stub_key(model: str, prompt: str) -> str:
    return hashlib.sha256(f"{model}::{prompt}".encode()).hexdigest()


def _stub_lookup(model: str, prompt: str) -> dict | None:
    p = _stub_path()
    if not p or not p.exists():
        return None
    try:
        data = json.loads(p.read_text())
    except Exception:
        return None
    return data.get(_stub_key(model, prompt))


def health_check() -> GeminiResult:
    """Tiny round-trip to confirm the model + auth + region all work."""
    model = vertex_config.GEMINI_FLASH
    prompt = "Reply with exactly: PONG"

    if hit := _stub_lookup(model, prompt):
        in_t = hit.get("input_tokens", 0)
        out_t = hit.get("output_tokens", 0)
        return GeminiResult(
            text=hit["output"],
            input_tokens=in_t,
            output_tokens=out_t,
            latency_ms=hit.get("latency_ms", 0),
            model=model,
            stubbed=True,
            cost_usd=cost_guard.actual_cost_usd(model, in_t, out_t),
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
    in_t = getattr(usage, "prompt_token_count", 0) if usage else 0
    out_t = getattr(usage, "candidates_token_count", 0) if usage else 0
    return GeminiResult(
        text=(response.text or "").strip(),
        input_tokens=in_t,
        output_tokens=out_t,
        latency_ms=latency_ms,
        model=model,
        stubbed=False,
        cost_usd=cost_guard.actual_cost_usd(model, in_t, out_t),
    )


def generate_structured(
    *,
    prompt: str,
    response_schema: dict | None = None,
    model: str | None = None,
    max_output_tokens: int = 4096,
    temperature: float = 0.1,
    system_instruction: str | None = None,
    agent_name: str | None = None,
) -> StructuredResult:
    """Generate JSON-mode output.

    The prompt is the **complete user-message** content (the system instruction
    + few-shot + payload concatenation happens in `lib/prompts.py`). The model
    is asked for `application/json` and the SDK parses + returns it.

    Stub-mode lookup hashes only on `(model, prompt)` — system_instruction is
    folded into the prompt before hashing so callers can either include it
    inline or pass it separately.

    `agent_name` opts the call into P5a's cost guards. When provided we
    (a) refuse if today's cumulative spend has hit the per-day cap, and
    (b) refuse if the pre-flight estimate exceeds the per-task cap. Both
    raise `cost_guard.CostBudgetExceeded`. Stub-mode is enforced too, but
    stub responses have zero tokens so the cap is effectively a no-op.
    """
    model = model or vertex_config.GEMINI_FLASH

    if agent_name:
        # Per-day first — the cheapest check.
        cost_guard.check_per_day(agent_name)
        # Pre-flight per-task estimate. The estimate is intentionally
        # rough — chars/4 + max_output_tokens at full output rate.
        estimate = cost_guard.estimate_request_cost_usd(
            model=model,
            prompt_chars=len(prompt or ""),
            system_chars=len(system_instruction or ""),
            max_output_tokens=max_output_tokens,
        )
        cost_guard.check_per_task(agent_name, estimate)

    # Stub mode: hash the full prompt (system + user) so test fixtures stay
    # deterministic regardless of how the call is structured.
    full_for_hash = f"{system_instruction or ''}\n---\n{prompt}"

    if hit := _stub_lookup(model, full_for_hash):
        raw = hit["output"]
        parsed = json.loads(raw) if isinstance(raw, str) else raw
        in_t = hit.get("input_tokens", 0)
        out_t = hit.get("output_tokens", 0)
        return StructuredResult(
            parsed=parsed,
            raw_text=raw if isinstance(raw, str) else json.dumps(raw),
            input_tokens=in_t,
            output_tokens=out_t,
            latency_ms=hit.get("latency_ms", 0),
            model=model,
            stubbed=True,
            cost_usd=cost_guard.actual_cost_usd(model, in_t, out_t),
        )

    client = _client()
    config: dict[str, Any] = {
        "max_output_tokens": max_output_tokens,
        "temperature": temperature,
        "response_mime_type": "application/json",
    }
    # Note: Vertex AI's `response_schema` accepts only Google's stricter
    # subset (no $schema, no format:"email"). We carry the canonical
    # JSON Schema in `schemas/*.schema.json`, validate post-hoc in
    # `lib/schema.py`, and rely on the prompt + JSON mode to keep Gemini
    # honest. Caller can ignore `response_schema` here — it's accepted but
    # not forwarded to Vertex.
    if system_instruction:
        config["system_instruction"] = system_instruction

    start = time.monotonic()
    response = client.models.generate_content(
        model=model,
        contents=prompt,
        config=config,
    )
    latency_ms = int((time.monotonic() - start) * 1000)

    raw_text = (response.text or "").strip()
    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise GeminiOutputError(
            f"Gemini returned non-JSON despite response_mime_type=application/json: {exc}",
            raw_text=raw_text,
        ) from exc

    usage = getattr(response, "usage_metadata", None)
    in_t = getattr(usage, "prompt_token_count", 0) if usage else 0
    out_t = getattr(usage, "candidates_token_count", 0) if usage else 0
    return StructuredResult(
        parsed=parsed,
        raw_text=raw_text,
        input_tokens=in_t,
        output_tokens=out_t,
        latency_ms=latency_ms,
        model=model,
        stubbed=False,
        cost_usd=cost_guard.actual_cost_usd(model, in_t, out_t),
    )


class GeminiOutputError(Exception):
    """Raised when Gemini's output can't be parsed/validated. Caller decides retry."""

    def __init__(self, message: str, *, raw_text: str = ""):
        super().__init__(message)
        self.raw_text = raw_text
