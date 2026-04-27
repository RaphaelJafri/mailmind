"""Build the sha-keyed stub-response file the gemini_runner expects.

Renders the *exact* prompts each agent will issue against the fixture data,
hashes them, and pairs each hash with a hand-authored canned output from
`fixtures/stub_outputs.json`. The acceptance script runs this immediately
before any agent call, so prompt edits don't need a separate fixture
recompile — the hashes always match.

Usage:
  uv run python fixtures/build_stub_responses.py <output_path>

Output: a JSON file at <output_path> in the format gemini_runner._stub_lookup
expects: { "<sha256(model::full_for_hash)>": {output, input_tokens, ...}, ... }
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "agents"))

from lib import prompts as pmod  # noqa: E402
from lib import vertex_config  # noqa: E402

FIXTURES_DIR = REPO / "fixtures"
SEED_PATH = FIXTURES_DIR / "threads" / "seed.json"
OUTPUTS_PATH = FIXTURES_DIR / "stub_outputs.json"


def _hash(model: str, system_instruction: str, user_prompt: str) -> str:
    full_for_hash = f"{system_instruction or ''}\n---\n{user_prompt}"
    return hashlib.sha256(f"{model}::{full_for_hash}".encode()).hexdigest()


def build_triage_entry(seed: dict, outputs: dict, model: str) -> dict:
    # Triage only sees threads that loaded with disposition='unclassified'.
    # Mirror raw_reader's ORDER BY thread_count DESC, sender ASC.
    unclassified = [t for t in seed["threads"] if t.get("disposition") == "unclassified"]
    sorted_threads = sorted(unclassified, key=lambda t: t["sender"])
    senders = []
    for t in sorted_threads:
        senders.append(
            {
                "sender": t["sender"],
                "thread_count": 1,
                "sample_subjects": [t["subject"]],
            }
        )

    user_prompt = (
        "Classify the following unclassified senders. Return one TriageProposal "
        "per sender, in input order.\n\n"
        f"<senders>\n{json.dumps({'senders': senders}, indent=2)}\n</senders>"
    )
    system = pmod.compose_system_prompt(
        "triage.md",
        schemas={"TRIAGE_PROPOSAL_SCHEMA": "triage-proposal.schema.json"},
    )
    canned = outputs["triage:default"]
    return {
        _hash(model, system, user_prompt): {
            "output": json.dumps(canned),
            "input_tokens": 1200,
            "output_tokens": 300,
            "latency_ms": 50,
        }
    }


def build_extract_entries(seed: dict, outputs: dict, model: str) -> dict:
    out: dict = {}
    system = pmod.compose_system_prompt(
        "extract.md",
        schemas={"THREAD_FACTS_SCHEMA": "thread-facts.schema.json"},
    )
    for t in seed["threads"]:
        thread_payload = {
            "thread_id": t["thread_id"],
            "subject": t["subject"],
            "messages": [
                {
                    "message_id": m["message_id"],
                    "from": m["from_email"],
                    "from_name": m.get("from_name"),
                    "to": m["to_emails"],
                    "cc": m["cc_emails"],
                    "date": m["internal_date"],
                    "is_from_user": m["is_from_user"],
                    "body": m["body_plain"],
                }
                for m in t["messages"]
            ],
        }
        user_prompt = (
            "Extract facts from the following thread. Return one ThreadFacts JSON object.\n\n"
            f"<thread>\n{json.dumps(thread_payload, indent=2)}\n</thread>"
        )
        canned = outputs[f"extract:{t['thread_id']}"]
        out[_hash(model, system, user_prompt)] = {
            "output": json.dumps(canned),
            "input_tokens": 800,
            "output_tokens": 250,
            "latency_ms": 40,
        }
    return out


def build_tagger_entries(seed: dict, outputs: dict, model: str) -> dict:
    out: dict = {}
    system = pmod.compose_system_prompt(
        "tagger.md",
        schemas={"TAG_SCHEMA": "tag.schema.json"},
    )
    for t in seed["threads"]:
        for m in t["messages"]:
            msg_payload = {
                "message_id": m["message_id"],
                "thread_id": t["thread_id"],
                "thread_subject": t["subject"],
                "from": m["from_email"],
                "from_name": m.get("from_name"),
                "to": m["to_emails"],
                "cc": m["cc_emails"],
                "date": m["internal_date"],
                "is_from_user": m["is_from_user"],
                "body": m["body_plain"],
                "thread_disposition": t["disposition"],
            }
            canned_key = f"tagger:{m['message_id']}"
            if canned_key not in outputs:
                # Tagger runs per-message_id on demand; we only need stubs
                # for messages tests/acceptance actually call against.
                continue
            user_prompt = (
                "Tag the following message. Return one MessageTag JSON object.\n\n"
                f"<message>\n{json.dumps(msg_payload, indent=2)}\n</message>"
            )
            canned = outputs[canned_key]
            out[_hash(model, system, user_prompt)] = {
                "output": json.dumps(canned),
                "input_tokens": 400,
                "output_tokens": 80,
                "latency_ms": 30,
            }
    return out


def _build_contacts_index(seed: dict) -> dict[str, dict]:
    """Mirror load_fixtures.mjs contacts upsert across ALL messages.

    The loader locks `display_name` on first insert (ON CONFLICT does not
    touch the column), so we replicate that: first appearance for an email
    decides the name, and we never revise it.
    """
    contacts: dict[str, dict] = {}
    for t in seed["threads"]:
        # JS Set iteration: load_fixtures.mjs builds
        #   `new Set([from, ...to, ...cc])`, then for-of's it. JS Set order
        # is insertion order, so from_email is always seen first within a
        # message.
        for m in t["messages"]:
            ordered: list[str] = []
            seen: set[str] = set()
            for email in (
                [m["from_email"]] + list(m.get("to_emails") or []) + list(m.get("cc_emails") or [])
            ):
                if not email or email in seen:
                    continue
                seen.add(email)
                ordered.append(email)

            for email in ordered:
                if email not in contacts:
                    contacts[email] = {
                        "email": email,
                        "first_seen": m["internal_date"],
                        "last_seen": m["internal_date"],
                        "message_count": 0,
                        "display_name": (
                            m.get("from_name") if email == m["from_email"] else None
                        ),
                    }
                entry = contacts[email]
                entry["first_seen"] = min(entry["first_seen"], m["internal_date"])
                entry["last_seen"] = max(entry["last_seen"], m["internal_date"])
                entry["message_count"] += 1
    return contacts


def build_rollup_entries(seed: dict, outputs: dict, model: str) -> dict:
    """Stub responses for relationship_agent.run(contact_email).

    Mirrors `relationship_agent._build_payload()`:
    - threads filtered to disposition in {keep, newsletter}, sorted ASC by
      `threads.last_message_date` (which load_fixtures.mjs computes as the
      max(internal_date) across the thread's messages);
    - contact metadata pulled from a contacts index that aggregates all
      seed messages, matching the loader's INSERT ... ON CONFLICT semantics.
    """
    out: dict = {}
    system = pmod.compose_system_prompt(
        "rollup.md",
        schemas={"CONTACT_ROLLUP_SCHEMA": "contact-rollup.schema.json"},
    )

    contacts = _build_contacts_index(seed)
    keep_threads = [
        t for t in seed["threads"] if t.get("disposition") in {"keep", "newsletter"}
    ]
    by_contact: dict[str, list] = {}
    for t in keep_threads:
        by_contact.setdefault(t["sender"], []).append(t)

    for contact_email, threads in by_contact.items():
        canned_key = f"rollup:{contact_email}"
        if canned_key not in outputs:
            continue

        # last_message_date column = max(message internal_date) per loader.
        def _thread_last(t):
            return max(m["internal_date"] for m in t["messages"])

        threads_sorted = sorted(threads, key=_thread_last)
        thread_facts = []
        for t in threads_sorted:
            facts = outputs[f"extract:{t['thread_id']}"]
            thread_facts.append(
                {
                    "thread_id": t["thread_id"],
                    "subject": t["subject"] or "",
                    "last_message_date": _thread_last(t),
                    "disposition": t["disposition"],
                    "facts": facts,
                }
            )

        c = contacts.get(contact_email, {})
        payload = {
            "contact_email": contact_email,
            "display_name": c.get("display_name"),
            "first_seen": c.get("first_seen"),
            "last_seen": c.get("last_seen"),
            "message_count": c.get("message_count", 0),
            "thread_facts": thread_facts,
            "corrections": [],
            "previous_rollup": None,
        }
        user_prompt = (
            "Roll up this contact. Return one ContactRollup JSON object.\n\n"
            f"<contact>\n{json.dumps(payload, indent=2)}\n</contact>"
        )
        canned = outputs[canned_key]
        out[_hash(model, system, user_prompt)] = {
            "output": json.dumps(canned),
            "input_tokens": 1500,
            "output_tokens": 400,
            "latency_ms": 60,
        }
    return out


def build_query_entries(outputs: dict, model: str) -> dict:
    """Stub responses for query_agent.run(question).

    The agent's prompt at each turn is `(system, _format_transcript(question,
    history))` — and `history` contains tool results that come from
    `query_tools.call_tool(...)` against the live derived.sqlite. So this
    builder runs the canned ReAct script *forward*, calling the real tools
    between turns to produce the real history bytes the prompt will hash.

    Requires derived.sqlite + raw.sqlite to be populated already (i.e. run
    AFTER extract + relationship + reconcile). At first-pass time (no
    rollups yet) this would silently produce useless stubs; we skip
    altogether if the rollup table is empty.
    """
    out: dict = {}

    # Late import: query_agent imports cadence_runner which imports raw_reader
    # — none of these have side effects but we want to share REPO/agents path
    # with the rest of the build.
    from query_agent import _format_transcript, _truncate_tool_result  # noqa: WPS433
    from lib import db as db_mod  # noqa: WPS433
    from lib import query_tools as qt  # noqa: WPS433

    # Bail fast if rollups haven't been populated — this build pass is meant
    # for the *second* invocation in acceptance/p3.sh.
    derived = db_mod.open_derived()
    try:
        n_rollups = derived.execute(
            "SELECT COUNT(*) AS c FROM contact_rollups"
        ).fetchone()["c"]
    finally:
        derived.close()
    if n_rollups == 0:
        return out

    system = pmod.compose_system_prompt("query.md")

    for key, value in outputs.items():
        if not key.startswith("query:") or not isinstance(value, list):
            continue
        question = key.removeprefix("query:")
        history: list[dict] = []
        for turn in value:
            user_prompt = _format_transcript(question, history)
            out[_hash(model, system, user_prompt)] = {
                "output": json.dumps(turn),
                "input_tokens": 800,
                "output_tokens": 80,
                "latency_ms": 30,
            }
            history.append({"role": "agent", **turn})
            if turn.get("action") != "tool_call":
                break
            tool_name = turn["tool"]
            args = turn.get("args") or {}
            try:
                tool_result = qt.call_tool(tool_name, args)
            except Exception as exc:  # noqa: BLE001
                tool_result = {"error": f"{type(exc).__name__}: {exc}"}
            history.append(
                {
                    "role": "tool",
                    "tool": tool_name,
                    "result": _truncate_tool_result(tool_result),
                }
            )
    return out


def main() -> None:
    if len(sys.argv) < 2:
        print("usage: build_stub_responses.py <output_path> [--include-query]", file=sys.stderr)
        sys.exit(2)
    output_path = Path(sys.argv[1])
    include_query = "--include-query" in sys.argv[2:]

    seed = json.loads(SEED_PATH.read_text())
    outputs = json.loads(OUTPUTS_PATH.read_text())
    model = vertex_config.GEMINI_FLASH

    stub: dict = {}
    stub.update(build_triage_entry(seed, outputs, model))
    stub.update(build_extract_entries(seed, outputs, model))
    stub.update(build_tagger_entries(seed, outputs, model))
    stub.update(build_rollup_entries(seed, outputs, model))
    if include_query:
        stub.update(build_query_entries(outputs, model))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(stub, indent=2))
    print(f"wrote {len(stub)} stub responses to {output_path}")


if __name__ == "__main__":
    main()
