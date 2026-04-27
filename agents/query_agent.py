"""Query agent — ReAct loop over the read-only tool set.

The user asks a free-form question ("who am I ghosting?"); we drive a Gemini
JSON-mode ReAct loop where each turn is a single JSON object:

    {"thought": "...", "action": "tool_call", "tool": "...", "args": {...}}
    {"thought": "...", "action": "answer", "answer": "..."}

Tools are read-only — defined and dispatched in `lib.query_tools`. Budget is
enforced **by this driver**, not the model: if the model tries to keep going
past 8 calls / 30s / $0.10 we synthesize a final answer from the transcript so
far. That's the FDE bullet on guardrails.

Every step is yielded as a `QueryEvent` so the FastAPI handler can stream it
to the dashboard via SSE. The MCP server doesn't use this driver — it exposes
tools directly so external clients (Claude Code / Cursor / ChatGPT desktop) can
do their own reasoning.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterator

from lib import agent_run, cost_guard, gemini_runner, prompts, query_tools, vertex_config


AGENT_NAME = "query_agent"

# Budget (per BUILD §20). Driver enforces all three.
MAX_TOOL_CALLS = 8
MAX_WALL_SECONDS = 30.0
MAX_COST_USD = 0.10


def estimate_cost_usd(input_tokens: int, output_tokens: int, *, model: str | None = None) -> float:
    """Wraps cost_guard.actual_cost_usd so callers don't need to know the
    model — defaults to Flash, which is what the query loop uses today."""
    return cost_guard.actual_cost_usd(model or vertex_config.GEMINI_FLASH, input_tokens, output_tokens)


# ---- streaming event shape ------------------------------------------------

@dataclass
class QueryEvent:
    kind: str  # "start" | "thought" | "tool_call" | "tool_result" | "answer" | "done" | "error"
    payload: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps({"kind": self.kind, **self.payload}, default=str)


# ---- helpers --------------------------------------------------------------

def _build_system_prompt() -> str:
    return prompts.compose_system_prompt("query.md")


def _format_transcript(question: str, history: list[dict]) -> str:
    """Render the running conversation back to the model.

    `history` items are alternating model turns and tool results, each already
    a JSON-shaped dict. The model only ever sees JSON — that keeps the loop
    grammar tight and the parser deterministic.
    """
    lines = [
        "<question>",
        question.strip(),
        "</question>",
        "",
        f"<budget>max_tool_calls={MAX_TOOL_CALLS} max_wall_seconds={MAX_WALL_SECONDS} max_cost_usd={MAX_COST_USD}</budget>",
        "",
        "<transcript>",
    ]
    for entry in history:
        lines.append(json.dumps(entry, default=str))
    lines.append("</transcript>")
    lines.append("")
    lines.append("Your next JSON object:")
    return "\n".join(lines)


def _redact_args_for_log(args: Any) -> Any:
    """Truncate strings >120 chars in the tool-call args for the streaming log."""
    if isinstance(args, dict):
        return {k: _redact_args_for_log(v) for k, v in args.items()}
    if isinstance(args, list):
        return [_redact_args_for_log(x) for x in args]
    if isinstance(args, str) and len(args) > 120:
        return args[:120] + "…"
    return args


def _truncate_tool_result(result: Any, max_items: int = 25) -> Any:
    """Cap the tool-result that goes back into the prompt context. Lists get
    sliced; large dicts are left alone (they're already small in our
    schema)."""
    if isinstance(result, list) and len(result) > max_items:
        return {
            "_truncated": True,
            "shown": max_items,
            "total": len(result),
            "items": result[:max_items],
        }
    return result


# ---- main loop ------------------------------------------------------------

def run(
    question: str,
    *,
    model: str | None = None,
    max_tool_calls: int = MAX_TOOL_CALLS,
    max_wall_seconds: float = MAX_WALL_SECONDS,
    max_cost_usd: float = MAX_COST_USD,
) -> Iterator[QueryEvent]:
    """Yield events as the ReAct loop runs.

    The caller streams events; the final ``answer`` event carries the answer
    text and a summary of tool calls / token usage. ``done`` is always the
    final event (success or budget-truncated).
    """
    if not question or not question.strip():
        yield QueryEvent("error", {"error": "empty question"})
        yield QueryEvent("done", {"reason": "empty"})
        return

    model = model or vertex_config.GEMINI_FLASH
    started = time.monotonic()
    started_iso = datetime.now(timezone.utc).isoformat()

    yield QueryEvent("start", {"question": question, "model": model, "started_at": started_iso})

    history: list[dict] = []
    tool_calls: list[dict] = []
    tokens_in = 0
    tokens_out = 0
    stubbed_any = False

    system_instruction = _build_system_prompt()
    truncation_reason: str | None = None
    final_answer: str | None = None

    with agent_run.record(AGENT_NAME, model) as run_row:
        for step in range(max_tool_calls + 1):  # +1 final answer turn
            elapsed = time.monotonic() - started
            cost_so_far = estimate_cost_usd(tokens_in, tokens_out)

            if elapsed > max_wall_seconds:
                truncation_reason = "wall_clock_exceeded"
                break
            if cost_so_far > max_cost_usd:
                truncation_reason = "cost_cap_exceeded"
                break

            user_prompt = _format_transcript(question, history)
            try:
                result = gemini_runner.generate_structured(
                    prompt=user_prompt,
                    system_instruction=system_instruction,
                    model=model,
                    max_output_tokens=1024,
                    temperature=0.2,
                    agent_name=AGENT_NAME,
                )
            except cost_guard.CostBudgetExceeded as exc:
                yield QueryEvent("error", {"error": f"{exc.code}: {exc}", "code": exc.code})
                truncation_reason = exc.code
                break
            except Exception as exc:  # noqa: BLE001
                yield QueryEvent("error", {"error": f"{type(exc).__name__}: {exc}"})
                truncation_reason = "model_error"
                break

            tokens_in += result.input_tokens or 0
            tokens_out += result.output_tokens or 0
            stubbed_any = stubbed_any or result.stubbed

            turn = result.parsed if isinstance(result.parsed, dict) else None
            if turn is None or "action" not in turn:
                yield QueryEvent(
                    "error",
                    {"error": "model returned non-dict or missing action", "raw": result.raw_text[:500]},
                )
                truncation_reason = "parse_error"
                break

            thought = turn.get("thought") or ""
            if thought:
                yield QueryEvent("thought", {"step": step, "text": thought})

            action = turn.get("action")
            if action == "answer":
                final_answer = (turn.get("answer") or "").strip()
                history.append({"role": "agent", **turn})
                break

            if action != "tool_call":
                yield QueryEvent("error", {"error": f"unknown action {action!r}"})
                truncation_reason = "bad_action"
                break

            # ---- tool call branch ----
            if len(tool_calls) >= max_tool_calls:
                truncation_reason = "tool_call_cap"
                history.append(
                    {
                        "role": "system",
                        "note": "tool_call cap reached; you must answer next turn",
                    }
                )
                # Don't actually call the tool; force the model to answer next iteration.
                continue

            tool_name = turn.get("tool") or ""
            args = turn.get("args") or {}
            yield QueryEvent(
                "tool_call",
                {"step": step, "tool": tool_name, "args": _redact_args_for_log(args)},
            )

            try:
                tool_result = query_tools.call_tool(tool_name, args)
            except KeyError as exc:
                tool_result = {"error": f"unknown_tool: {exc}"}
            except TypeError as exc:
                tool_result = {"error": f"bad_args: {exc}"}
            except Exception as exc:  # noqa: BLE001
                tool_result = {"error": f"{type(exc).__name__}: {exc}"}

            truncated = _truncate_tool_result(tool_result)
            tool_calls.append(
                {"tool": tool_name, "args": args, "result_preview_count": _count_preview(tool_result)}
            )
            history.append({"role": "agent", **turn})
            history.append({"role": "tool", "tool": tool_name, "result": truncated})

            yield QueryEvent(
                "tool_result",
                {
                    "step": step,
                    "tool": tool_name,
                    "result_count": _count_preview(tool_result),
                    "result": truncated,
                },
            )

        # ---- finalize ----
        if final_answer is None:
            # Budget-exhausted or error — synthesize a fallback so the user always
            # sees something. This is the "honest gap" the prompt promises.
            final_answer = _fallback_answer(question, tool_calls, truncation_reason)
            yield QueryEvent(
                "answer",
                {
                    "answer": final_answer,
                    "truncated": True,
                    "reason": truncation_reason or "no_answer_emitted",
                },
            )
        else:
            yield QueryEvent("answer", {"answer": final_answer, "truncated": False})

        run_row.input_tokens = tokens_in
        run_row.output_tokens = tokens_out
        run_row.latency_ms = int((time.monotonic() - started) * 1000)
        run_row.stubbed = stubbed_any
        run_row.cost_usd = round(estimate_cost_usd(tokens_in, tokens_out), 6)
        run_row.tools_called = tool_calls
        run_row.result_status = "success" if not truncation_reason else "truncated"

    yield QueryEvent(
        "done",
        {
            "tool_calls": len(tool_calls),
            "wall_ms": int((time.monotonic() - started) * 1000),
            "input_tokens": tokens_in,
            "output_tokens": tokens_out,
            "cost_usd": round(estimate_cost_usd(tokens_in, tokens_out), 6),
            "stubbed": stubbed_any,
            "truncated": bool(truncation_reason),
            "reason": truncation_reason,
        },
    )


def run_collected(question: str, **kwargs: Any) -> dict:
    """Drive the loop and return one summary dict — used by tests + MCP fallback."""
    events: list[dict] = []
    for ev in run(question, **kwargs):
        events.append(json.loads(ev.to_json()))
    answer_event = next((e for e in events if e["kind"] == "answer"), None)
    done_event = next((e for e in events if e["kind"] == "done"), None)
    tool_calls = [e for e in events if e["kind"] == "tool_call"]
    return {
        "question": question,
        "answer": (answer_event or {}).get("answer"),
        "truncated": (answer_event or {}).get("truncated", False),
        "reason": (answer_event or {}).get("reason"),
        "tool_calls": tool_calls,
        "events": events,
        "stats": done_event or {},
    }


# ---- helpers --------------------------------------------------------------

def _count_preview(result: Any) -> int | None:
    if isinstance(result, list):
        return len(result)
    if isinstance(result, dict):
        for key in ("they_owe_you", "you_owe_them"):
            if key in result and isinstance(result[key], list):
                return len(result[key])
    return None


def _fallback_answer(question: str, tool_calls: list[dict], reason: str | None) -> str:
    if not tool_calls:
        return (
            f"I couldn't answer '{question.strip()}' — the agent hit the {reason or 'budget'} "
            f"limit before producing a result."
        )
    tools_used = ", ".join(t["tool"] for t in tool_calls)
    return (
        f"I called {len(tool_calls)} tool(s) ({tools_used}) but didn't finish reasoning "
        f"before the {reason or 'budget'} limit. Re-run with a narrower question if you "
        f"need a complete answer."
    )
