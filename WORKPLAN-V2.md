# gmail-ops v2 — multi-agent desktop app on Google's stack

> **For autonomous build:** read this file first (vision + phases), then `WORKPLAN-V2-BUILD.md` (operational details, schemas, prompt skeletons, acceptance scripts, "ask the user" triggers). Both are required input.
>
> **Pre-flight:** before starting P0, the user must run `~/Desktop/scripts/freeze-v1.sh` to git-tag v1 as `v1-frozen`. v1 lives at `~/Desktop/gmail-ops` and is read-only reference material.

**Goal:** end-to-end Gmail management: ingest → classify → tag → relationship-rollup → follow-up surface → drafted replies → human-approved sends. Multi-agent on Vertex AI / Gemini / ADK, packaged as a desktop app, exposed over MCP. Built as the FDE portfolio piece.

**Why a v2:** v1 (this repo) is "LLM-as-fact-extractor in a deterministic pipeline" — clean, but single-shot. v2 is a multi-agent system where ingestion, triage, drafting, and Q&A are all separate agents with tools, evals, and human approval gates. Different shape, different stack, different story.

## Stack decision

| Layer | Choice | Why |
|---|---|---|
| Inference | **Gemini 2.5 Flash** for extract/tag/triage; **Gemini 2.5 Pro** for draft generation | Flash is cheap and fast for high-volume; Pro for quality-sensitive draft output |
| Agent framework | **Google ADK** (Python) | JD-named. Native multi-agent, sequential/parallel/loop primitives, Vertex AI integration |
| Orchestration | ADK's `SequentialAgent` + `ParallelAgent`, hierarchical delegation | Maps to "ReAct, hierarchical delegation" verbatim from JD |
| Storage | SQLite (port from v1) + a relationship knowledge graph | Local-first; no cloud DB cost |
| MCP server | Python MCP SDK, exposes read-only DB tools | JD-named |
| Frontend | **Tauri** (Rust shell) + React + TS | Smaller/faster than Electron; Rust shell signals taste |
| Backend | Local Python service (FastAPI) for agents; local Node service for ingestion plumbing | Keep working v1 Node code for sync; agents are Python |
| Auth | OAuth Desktop flow (Gmail read + send + drafts) | Two-stage scope upgrade — read-only first, write later with explicit user consent |
| Hosting (optional) | Cloud Run for the agent service if user wants remote | Free tier covers the demo |
| Eval | Held-out labeled set + automated metrics dashboard | JD-named "evaluation pipelines" |

**Repo decision:** new repo, name TBD (suggestion: `mailmind` or `gmail-agent-gcp`). Keep v1 as-is — link to it from v2's README as "v1 prototype." Two repos tells the FDE story better than one branch: "I built it, learned, rebuilt on production-grade stack."

## Multi-agent architecture

```
                    ┌─────────────────────────┐
                    │   Orchestrator Agent     │  ADK top-level dispatcher
                    │   (intent routing)       │
                    └────────────┬─────────────┘
                                 │
        ┌────────────────────────┼────────────────────────────┐
        │                        │                            │
        ▼                        ▼                            ▼
  ┌──────────┐         ┌────────────────┐           ┌──────────────────┐
  │ Ingestion │ ─────► │ Knowledge       │ ◄──────── │ Query Agent      │
  │ pipeline  │        │ Graph (SQLite)  │           │ (ReAct + tools)  │
  └──────────┘         └────────────────┘           └──────────────────┘
        │                        ▲                            │
        │                        │                            │
        ▼                        │                            ▼
  ┌──────────┐         ┌────────────────┐           ┌──────────────────┐
  │ Triage    │ ─────► │ Extract Agent   │           │ Draft Agent      │
  │ Agent     │        │ (ParallelAgent  │           │ (Gemini Pro)     │
  │ (filters) │        │  over threads)  │           │                  │
  └──────────┘         └────────┬───────┘           └────────┬─────────┘
                                │                            │
                                ▼                            ▼
                       ┌────────────────┐           ┌──────────────────┐
                       │ Relationship   │           │ Approval Gate    │
                       │ Agent          │           │ (human-in-loop)  │
                       │ (per contact)  │           └────────┬─────────┘
                       └────────┬───────┘                    │
                                │                            ▼
                                ▼                    ┌──────────────────┐
                       ┌────────────────┐            │ Send Agent       │
                       │ Cadence Agent  │            │ (Gmail write)    │
                       │ (det. + LLM)   │            └──────────────────┘
                       └────────────────┘                    │
                                                             ▼
                                                    immutable audit log
```

### Agent inventory

| Agent | Type | Tools / I/O | Model |
|---|---|---|---|
| **Ingestion** | deterministic Node, not an agent | Gmail API → SQLite | n/a |
| **Triage** | LlmAgent | `list_unclassified_senders`, `propose_filter_rule`, `request_human_approval` | Gemini Flash |
| **Extract** | ParallelAgent over LlmAgent | per-thread structured output (JSON Schema) | Gemini Flash |
| **Tagger** | LlmAgent | message-level: urgency / category / project. Schema-bounded | Gemini Flash |
| **Relationship** | LlmAgent | per-contact rollup (port v1 schema). Reads corrections + verdicts as few-shot | Gemini Flash |
| **Cadence** | hybrid (det. + LlmAgent for edge cases) | classifies overdue / waiting / cold | Gemini Flash on edges only |
| **Query** | LlmAgent (ReAct) | `search_contacts`, `get_rollup`, `list_overdue`, `get_thread`, `get_pending_drafts` | Gemini Pro |
| **Draft** | LlmAgent | `read_thread_context`, `read_user_style`, `generate_draft` | Gemini Pro |
| **Approval Gate** | deterministic UI flow, not an agent | preview + diff + per-action approve | n/a |
| **Send** | LlmAgent with single `send_email` tool — checks approval state first | `gmail.send` | Gemini Flash |
| **Eval** | LlmAgent | runs held-out set, computes P/R/F1, latency, cost | Gemini Flash |
| **Orchestrator** | SequentialAgent + router | dispatches based on user intent | Gemini Flash |

---

## Phases

### P0 — Foundation port (week 1)

**Goal:** stand up a new repo with Gemini-backed inference, ported v1 plumbing, and a working dashboard skeleton.

- [ ] New repo `mailmind` (or chosen name). MIT license, public.
- [ ] Port `scripts/lib/db.mjs` and the SQLite schema verbatim. Keep WAL.
- [ ] Port `scripts/auth.mjs` + `scripts/sync.mjs` (Gmail read scope only for now).
- [ ] **Replace `scripts/lib/claude-runner.mjs` with `agents/lib/gemini_runner.py`** — Vertex AI / Gemini SDK with structured output via `response_schema`.
- [ ] Port `ARCHITECTURE.md` discipline. New invariants for v2:
  - Agents have explicit tool budgets (max iterations, max tool calls)
  - Every write action requires human approval, no batching
  - Token usage logged per agent per task
- [ ] Tauri shell wrapping a placeholder React+TS dashboard. Single tab: "Setup."
- [ ] CI from day 1 — GitHub Actions runs `pytest` + `npm test` + Tauri build smoke test.

**Demo at end of P0:** `npm run sync` ingests, dashboard renders inbox stats, Gemini health-check inference works.

### P1 — Ingestion + classification agents (week 2)

**Goal:** triage + extract + tag operating as agents, replacing v1's interactive filters CLI with an agentic suggester.

- [ ] **Triage agent** (`agents/triage_agent.py`):
  - Reads unclassified senders from SQLite
  - For each: proposes `keep / skip / newsletter / unclear` with confidence + reasoning citing user-context
  - Writes proposals to `proposals/triage-{run_id}.md`
  - Dashboard surfaces them in an approval queue; user approves → writes to `config/filters.yml`
- [ ] **Extract agent** (`agents/extract_agent.py`):
  - ADK `ParallelAgent` fanning out across threads with concurrency cap
  - Per-thread: structured output matching `schemas/thread-facts.json`
  - Provenance check (every claim cites `source_message_ids`) before write
- [ ] **Tagger agent** (`agents/tagger_agent.py`):
  - Message-level tags: `urgent / followup / informational / automated`, `category: recruiting/legal/personal/...`, `project: clyk/jobsearch/...`
  - Tags stored in `message_tags` table
- [ ] Dashboard tabs: **Inbox**, **Contacts**, **Triage Queue**, **Tags**.

**Demo at end of P1:** point at a fresh inbox, agents classify and extract end-to-end, user approves triage suggestions through the UI.

### P2 — Relationship + follow-up (week 3)

**Goal:** per-contact rollup as an agent, deterministic cadence tracker, follow-up dashboard tab.

- [ ] **Relationship agent** (`agents/relationship_agent.py`):
  - Per-contact rollup: `relationship_summary`, `tone`, `cadence`, `status`, `tags`
  - Reads prior corrections + verdicts as few-shot (port v1's logic)
  - Outputs draft next-steps with provenance
- [ ] **Cadence module** (`scripts/cadence.py`, deterministic):
  - Port `followup-core.mjs` to Python
  - `overdue / waiting / cold` classification per relationship
  - Per-contact reply-latency overrides from `user-context.md`
- [ ] **Reconcile** (`scripts/reconcile.py`, deterministic):
  - Diff new drafts against pending next_steps (jaccard + ID overlap, port from v1)
  - **NEW:** also consume `corrections` table for direct status updates (fixes the v1 bug where dashboard "dismiss" never propagated)
- [ ] Dashboard tabs: **Follow-ups** (you owe / they owe / stale), **Contacts** (rollup view).

**Demo at end of P2:** dashboard shows "what's overdue, who am I ghosting, who's waiting on me" — direct value prop.

### P3 — Query agent + MCP (week 4)

**Goal:** demonstrate ReAct + tool use end-to-end. Expose the knowledge graph over MCP so any agent can consume it.

- [ ] **Query agent** (`agents/query_agent.py`, ReAct):
  - Tools: `search_contacts(query)`, `get_rollup(email)`, `list_overdue(window_days)`, `get_thread(thread_id)`, `get_pending_drafts()`, `list_threads_by_tag(tag)`
  - Bounded: max 8 tool calls, max 30s wall time, $0.10 cost cap per query
  - Streams reasoning + tool calls to dashboard for transparency
- [ ] **MCP server** (`mcp/server.py`):
  - Exposes the same tool set as the query agent
  - Read-only — no write tools
  - Documented in `mcp/README.md` so Claude Code, Cursor, ChatGPT desktop can consume
- [ ] Dashboard tab: **Ask** — conversational Q&A surface against query agent.
- [ ] Demo prompts pre-loaded: "who am I ghosting?", "what's pending with my recruiter?", "summarize this week's job-search activity."

**Demo at end of P3:** ask the dashboard a free-form question, see the agent's tool calls + final answer streamed live. Show same MCP tools working from Claude Code.

### P4 — Drafts + sends with approval gate (weeks 5–6)

**Goal:** the agent writes, the human approves, Gmail sends. Zero accidental sends, ever.

- [ ] **Gmail write-scope OAuth** — separate consent flow, isolated token, never granted by default. User must opt in via dashboard.
- [ ] **Draft agent** (`agents/draft_agent.py`):
  - Tools: `read_thread_context(thread_id)`, `read_user_style()`, `read_relationship_rollup(email)`, `generate_draft(thread_id, intent)`
  - Output: structured draft with `body`, `subject`, `to`, `cc`, `confidence`, `rationale`, `cited_facts[]`
  - Always writes to a `drafts` table — never directly to Gmail
- [ ] **Approval gate** (deterministic UI flow):
  - Dashboard "Drafts" tab shows agent-generated drafts as pending
  - Each draft: rendered preview, diff vs. user style, cited facts, edit-in-place
  - Three actions: **Approve & Send**, **Save as Gmail Draft**, **Reject**
  - **No batch-approve. One draft, one click.**
- [ ] **Send agent** (`agents/send_agent.py`):
  - Single `send_email` tool — refuses if approval state isn't `approved`
  - 30-second undo window: dashboard shows "Sending in 30s — Cancel"
  - Writes to immutable `audit_log` table: who, what, when, draft_hash, approval_id
- [ ] **Safety invariants** (added to ARCHITECTURE.md):
  - Send tool is the only place `gmail.send` is called
  - Draft hash must match approval-time hash (no post-approval edits)
  - Audit log is append-only, sqlite trigger-enforced
- [ ] Dashboard: **Drafts** tab, **Sent** tab (with audit log), **Settings → Permissions** for write-scope grant/revoke.

**Demo at end of P4:** "Reply to Adam Moore about the recruiter intro" → draft appears in Drafts tab → review/edit → approve → 30s undo window → sends. Audit log shows the chain.

### P5 — Eval + observability + self-improvement (week 7)

**Goal:** every JD bullet on "evaluation, accuracy, latency, cost" answered with a number on a screen.

- [ ] **Held-out labeled set** (`evals/labeled.jsonl`):
  - 50–100 hand-labeled threads (extraction ground truth)
  - 30–50 hand-labeled rollups (relationship ground truth)
  - 20–30 hand-labeled drafts (style + correctness)
- [ ] **Eval agent** (`agents/eval_agent.py`):
  - Runs the labeled set on every release
  - Reports per agent: precision/recall on commitments, F1 on next-step priority, draft style-match score (LLM-as-judge), P50/P95 latency, mean $/task
- [ ] **Observability tab** in dashboard:
  - 7-day cost trajectory per agent (Gemini Flash vs Pro split)
  - Token usage histograms
  - Schema-validation failure rate
  - Retry rate + error taxonomy
  - Per-agent latency P50/P95
- [ ] **Self-improvement loop** (port from v1):
  - Review queue tab: weighted sample of low-confidence inferences
  - Verdicts → regenerate few-shot examples → next run uses them
  - Corrections → propagate deterministically (fixed v1 bug)
- [ ] **Tools/cost guardrails** baked into every agent: per-task token cap, per-day cost cap, refuse + escalate to human if exceeded.

**Demo at end of P5:** open Observability tab, show "extract: F1=0.91 on commitments, P50=1.8s, $0.003/thread, 2.1% schema-fail rate (auto-retry succeeded)." That's the FDE bullet answered.

### P6 — Desktop polish + portfolio (week 8)

**Goal:** anyone can clone, install, see the full system in 60 seconds. Interview-grade demo.

- [ ] **Demo dataset:** 100 anonymized fixture threads + a stub Gemini runner that returns canned responses. `npm run demo` runs the full pipeline cold against the fixture.
- [ ] **Tauri app polish:** menu bar, system tray, native notifications for high-priority follow-ups.
- [ ] **README narrative reframe:**
  - Lead with the FDE story (multi-agent, Gemini, ADK, MCP, eval, write-scope safety)
  - Architecture diagram (the one above, but rendered)
  - 60-second walkthrough GIF
  - Eval scoreboard (current numbers)
- [ ] **Interview demo script** (`docs/demo-script.md`):
  - 5-minute walkthrough hitting every JD bullet
  - 15-minute deep dive into one agent (Draft + Approval + Send)
  - 30-second "ask the system anything" Q&A demo
- [ ] **Architecture doc** (`ARCHITECTURE.md` v2):
  - Invariants (read-only by default, every write requires approval, audit log is append-only)
  - Non-goals (no multi-tenant, no shared cloud, no batch sends, no auto-reply without human)
  - Cost model (token economy per agent)
  - Failure modes + recovery

**Demo at end of P6:** the portfolio piece is shippable. Public repo, working app, eval numbers, demo GIF, write-up.

---

## Cross-cutting concerns

### Safety (build in from P0, not retrofitted)

- All write actions go through Approval Gate. No exceptions. ARCHITECTURE.md invariant.
- Tool budgets per agent: max iterations, max wall time, max cost. Refuse + escalate at limit.
- Schema validation on every LLM output, before any state change.
- Immutable audit log on send, with draft hash chained to approval hash.
- Undo window (30s) on send. Cancel halts the actual API call.
- OAuth scopes layered: read-only by default. Write scope requires explicit dashboard consent + can be revoked.

### Token economy / cost discipline

- Pre-flight cost estimate per agent run, blocks runs > config cap (port v1's `max_cost_per_run_usd`).
- Per-agent daily cost cap. Soft warn at 70%, hard stop at 100%.
- Gemini Flash for high-volume, Pro only for quality-sensitive (drafts, query-agent reasoning).
- Cache per-thread extractions by content hash (port v1's idempotency).
- $300 GCP credit covers full development. Production cost target: <$5/month for personal use (~1000 threads/week).

### Eval rigor

- Held-out set is locked — never used in few-shot training.
- Eval runs on every commit via CI.
- Per-agent metrics in `evals/results.jsonl`, append-only.
- Regression: eval scores must not drop > 5% between commits without explicit acknowledgement.
- LLM-as-judge prompts are versioned and themselves eval'd against human verdicts.

### Observability

- Every agent invocation logged with: `agent_name`, `task_id`, `model`, `input_tokens`, `output_tokens`, `cost_usd`, `latency_ms`, `tools_called[]`, `result_status`.
- Logs → SQLite `agent_runs` table → Observability dashboard tab.
- 7-day rolling stats. Anomaly detection on latency/cost spikes.

---

## Non-goals (deliberate)

- **Not multi-tenant.** One Gmail account per install.
- **Not a hosted SaaS.** Local-first. Cloud Run is optional and demo-only.
- **Not auto-reply.** No agent sends without human approval. Ever.
- **Not a Gmail UI replacement.** The dashboard is for orchestration + review, not for reading individual messages.
- **Not multi-language.** English-only for v2. Localized models are deferred.
- **Not a calendar or task manager integration.** Stays focused on Gmail surface.

---

## Resolved decisions (was: open questions)

> Full decision record in `WORKPLAN-V2-BUILD.md` §1. Summary:

1. **Repo strategy:** ✅ new repo `mailmind`. v1 stays as-is, frozen at git tag `v1-frozen`.
2. **Agent language:** ✅ Python (ADK is Python-native). Node remains for ingestion sidecar.
3. **GCP project:** ✅ reuse v1's existing GCP project (already isolated from CLYK).
4. **Scope:** ✅ full P0–P6 (8-week aggressive vision).
5. **Bundling:** ✅ polished single-installer Tauri app from P0, using Tauri sidecars for Node + Python (PyInstaller-built binaries).
6. **v1 → v2 data:** ✅ v2 starts clean from a fresh Gmail sync. v1 keeps running independently as fallback / reference.
7. **Send safety:** ✅ P4 ships in two stages — drafts-only first (`gmail.send` scope NOT requested), then explicit user-toggle in Settings → Permissions to enable send.
8. **Eval labeling:** ✅ in-app labeling UI built as part of P5. Initial set: 20 threads + 10 rollups + 5 drafts. Grow incrementally.
9. **Cloud deployment:** ❎ deferred. Local-only for v2. Cloud Run is a hypothetical P7.
10. **Mobile:** ❎ deferred. If asked in interview: "would design as an MCP-consuming React Native app, agent core stays the same."

---

## Time estimate

- **Aggressive (full-time):** 8 weeks calendar.
- **Realistic (part-time alongside CLYK):** 12–16 weeks calendar.
- **Interview-minimum (P0-P3):** 4 weeks. Demo: ingestion + multi-agent classification + relationship rollup + query agent + MCP. Skip drafts/sends. Still hits "multi-agent + tool use + MCP + eval scaffolded" but not the full story.

If interviewing within 4 weeks: ship P0-P3 + a stubbed Drafts tab so the architecture is visible, with "P4 in progress" callout. Honest is better than complete.

---

## What to port from v1

> **Canonical port table** lives in `WORKPLAN-V2-BUILD.md` §6. The summary below is for reading; the table is the source of truth.

Port (verbatim or near-verbatim):
- **Core schemas:** `scripts/lib/db.mjs` (RAW + DERIVED) — Node side stays Node, Python sidecar reads via `sqlite3`. New v2 tables in BUILD §8.
- **Ingestion plumbing:** `scripts/auth.mjs`, `scripts/sync.mjs`, `scripts/reclassify.mjs`, `scripts/lib/gmail-client.mjs`, `scripts/lib/filters.mjs`.
- **Cadence + reconcile:** `scripts/followup-cadence.mjs` (verbatim), `scripts/reconcile-next-steps.mjs` (port + fix v1 dismiss bug — see BUILD §19).
- **Self-improvement loop:** `scripts/sample-for-review.mjs` + `scripts/build-few-shot.mjs` → folded into `agents/review_agent.py`.
- **Cost discipline:** `scripts/lib/cost-estimator.mjs` → `agents/lib/cost_guard.py` (Python translation, same logic).
- **Schemas:** all 4 `schemas/*.json` reused verbatim. Plus 3 new schemas (BUILD §11).
- **Prompt fragments:** `prompts/provenance-rules.md`, `prompts/confidence-rubric.md` verbatim.
- **Worker prompts:** `modes/extract-thread.md`, `modes/rollup-contact.md` → adapted as ADK system prompts (drop Claude framing).
- **Configs:** `config/pipeline.yml` (drop `claude_plan`, add `gemini_models` + `cost_caps`), `config/filters.yml` (verbatim format).
- **Architecture discipline:** `ARCHITECTURE.md` invariants ported and extended (BUILD §7). v1's `CLAUDE.md` folded into v2's CLAUDE.md (BUILD §25).
- **Append-only correction & user-context formats:** `corrections.jsonl`, `user-context.md` (template in BUILD §15).
- **Tests:** 186 node:test tests → Node sidecar keeps them as-is; new pytest suite for the Python agents.

What to **not** port:
- `claude-runner.mjs` → replaced by `agents/lib/gemini_runner.py` (Vertex AI structured output).
- `scripts/lib/plan-budget.mjs` → Claude-plan-specific, not relevant on Gemini.
- Interactive filters CLI → replaced by Triage agent + Triage Queue tab.
- Single-shot worker model → replaced by ADK multi-agent (`SequentialAgent`, `ParallelAgent`).
- Plain HTML dashboard (`dashboard/server.mjs`, `dashboard/public/*`) → replaced by Tauri+React.
- `scripts/export-dashboard-data.mjs` → Tauri reads SQLite directly via agent-service endpoints.
- `modes/troubleshoot.md`, `modes/setup.md`, `modes/bootstrap.md`, `modes/nuke.md` → folded into in-app UX (Settings, first-run wizard, Diagnostics).

---

## Immediate next step (autonomous-agent kick-off)

Prerequisites the user (Raphael) handles manually before the agent starts:

1. Run `~/Desktop/scripts/freeze-v1.sh` — git-tags v1 as `v1-frozen`.
2. Run `gcloud auth application-default login` and pick the Google account that owns v1's GCP project (could be the personal or the workspace account; `gcloud projects list` confirms after login). The mailbox being managed is `raphaeljafri@gmail.com` — that's a separate consent step at first OAuth.
3. Verify `mailmind` is available on GitHub or pick alternative.
4. (Optional, can defer) Apply $300 GCP credit to the existing project.

Then the agent (Claude) starts P0:

1. `gh repo create mailmind --public --description "Multi-agent Gmail manager on Gemini + ADK"`
2. Initialize Tauri scaffold per directory tree in `WORKPLAN-V2-BUILD.md` §5.
3. Port `db.mjs` schemas + `sync.mjs` ingestion as the Node sidecar (`ingester/`).
4. Set up Python sidecar: `uv init`, install `google-cloud-aiplatform`, `google-adk`, `fastapi`.
5. Wire `/health` endpoint that pings Gemini and reports OK/fail.
6. Run `acceptance/p0.sh`. When green, advance to P1.

End of week 1: P0 acceptance script returns 0. A fresh dev can `git clone && npm run demo` and see the Tauri app launch with stub data.

The agent must pause and ask the user when any §21 trigger in BUILD.md fires (e.g. about to grant a new OAuth scope, or send a real Gmail message).
