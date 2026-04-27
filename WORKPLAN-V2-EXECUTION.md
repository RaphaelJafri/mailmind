# mailmind — execution playbook

> **Read this if you're about to build mailmind, or hand it to Claude to build.**
> Companion docs: [WORKPLAN-V2.md](WORKPLAN-V2.md) (vision + phases), [WORKPLAN-V2-BUILD.md](WORKPLAN-V2-BUILD.md) (operational reference). This file is the chronological narrative that ties them together.

---

## Honest readiness assessment

**Can Claude run this end-to-end without help?** No. No autonomous build of this size is ever truly hands-off. But the plan is now concrete enough that:

- The vision and architecture are locked in (`WORKPLAN-V2.md`).
- The operational details — schemas, prompts, process model, paths, retry policy, acceptance scripts — are specified (`WORKPLAN-V2-BUILD.md`).
- The agent has explicit "pause and ask the user" triggers (BUILD §21).
- Setup is documented end-to-end with a one-shot script (`freeze-v1.sh`) for the manual bits.

**What's still uncertain:**

| Risk | Why | Mitigation |
|---|---|---|
| ADK is young | Google's ADK released late 2024; APIs still evolving. Patterns in BUILD §9 may be stale. | Agent reads `pip show google-adk` version on day 1 and consults current docs if API mismatches the skeleton. |
| PyInstaller + ADK + Vertex SDK bundling | The Python sidecar bundles a lot of native deps. May not be one-command. | If P0's Tauri build smoke fails, agent falls back to pure-localhost-Python (no sidecar bundling) for dev mode and re-attempts bundling at P6. Documented as a known fallback. |
| Tauri + Python sidecar pattern | Uncommon — most Tauri apps ship Rust-only or Rust+Node. | Tauri's `externalBin` config supports any executable. Agent uses the same pattern Tauri's docs use for Go/Rust sidecars. |
| Eval set requires Raphael to label data | Agent can't label for you. P5 will pause until you've labeled the initial 35-item set. | Agent builds the labeling UI first in P5 and then asks you to spend ~2 hours labeling. |

**Translation:** plan to babysit P0 (the bundling decisions), and budget ~2 hours of your time during P5 for labeling. Otherwise it should run.

---

## Assumptions baked into this plan

If any of these are wrong, course-correct before starting.

1. **Mailbox under management:** `raphaeljafri@gmail.com` (personal Gmail). NOT `raphael@clyk.co`.
2. **GCP project:** v1's existing project is reused. Whichever Google account owns it is the one used for ADC.
3. **Hardware:** macOS arm64 (Apple Silicon). Linux/Windows are explicit non-goals for the first version of v2.
4. **Toolchains installed locally:** Node 22+ (already there for v1), Python 3.12+, Rust toolchain (`rustup` + `cargo`), `gh` CLI, `gcloud` CLI, `uv` for Python deps. Agent verifies on day 1; missing toolchains pause the run with install commands.
5. **Single user.** mailmind is for Raphael only. No multi-tenancy, no shared deployment.
6. **Internet access during build.** Agent installs deps from npm/PyPI/crates.io, hits Vertex AI, hits GitHub. No air-gapped build.
7. **$300 GCP credit is "nice to have."** Build proceeds even without it. Worst case: a few dollars of Gemini calls during dev (well under quota at $5/day cap).
8. **Apple code-signing cert is needed only at P6.** P0–P5 use unsigned dev builds. If you don't have a Developer cert, P6 produces an unsigned `.app` and you ship it that way.
9. **GitHub repo is public** at `mailmind`. If the name is taken, you pick an alternative before P0.
10. **v1 stays running.** mailmind doesn't replace v1 until P5+ proves quality. v1 keeps doing your daily Gmail triage during the build.

---

## Pre-flight (you do, ~20 min, one-time)

These are the only things only you can do. Do them in order.

### 1. Freeze v1 as read-only reference
```bash
~/Desktop/scripts/freeze-v1.sh
```
What this does: runs `git init` in `~/Desktop/gmail-ops`, commits everything, tags it `v1-frozen`. Idempotent — safe to re-run. v2 then references v1 by absolute path; if v2 ever wants to write to v1, the agent will pause and ask.

### 2. Authenticate gcloud
```bash
brew install --cask google-cloud-sdk    # if not already installed
gcloud auth login                        # pick whichever Google account owns v1's GCP project
gcloud auth application-default login    # same account
gcloud projects list                     # confirms you can see v1's project
```
**If you don't know which account owns v1's GCP project:** check `cat ~/Desktop/gmail-ops/.env` — the `GOOGLE_CLIENT_ID` is namespaced by project number. After login, `gcloud projects list` shows you the matching project ID.

### 3. Verify the GitHub name
```bash
gh repo view mailmind 2>/dev/null && echo "TAKEN" || echo "AVAILABLE"
```
If TAKEN: pick another name (e.g. `mailmind-app`, `inbox-agent`, `gmail-mind`). Update both workplans before handing off.

### 4. (Optional, skip if already done) Apply $300 GCP credit
Manual GCP console step: Billing → Credits → Apply. Skip if already applied to v1's project.

### 5. Hand off to Claude
When the four steps above are done, your hand-off prompt to Claude is:
> Build mailmind end-to-end per `~/Desktop/WORKPLAN-V2.md` and `~/Desktop/WORKPLAN-V2-BUILD.md`. Start with phase P0. Run `acceptance/pN.sh` after each phase and do not advance until it returns 0. Pause and ask me when any §21 trigger fires (BUILD.md). The mailbox under management is `raphaeljafri@gmail.com`.

---

## P0 — Foundation (week 1)

**Plain-English goal:** stand up an empty-but-real Tauri app that can talk to Gemini and ingest a few Gmail threads.

**What gets built:**
1. New GitHub repo `mailmind` created public.
2. Directory tree per BUILD §5 scaffolded — `shell/` (Rust), `webview/` (React), `ingester/` (Node), `agents/` (Python), `mcp/`, `schemas/`, `prompts/`, `config/`, `acceptance/`, `fixtures/`, `.github/workflows/`.
3. **Node sidecar (`ingester/`):** ports `db.mjs`, `auth.mjs`, `sync.mjs`, `gmail-client.mjs` from v1. Tokens move to macOS app data dir (`~/Library/Application Support/mailmind/tokens/`).
4. **Python sidecar (`agents/`):** `uv init`, install `google-cloud-aiplatform`, `google-adk`, `fastapi`, `uvicorn`. Wire `agents/service.py` with a `/health` endpoint that pings Gemini.
5. **Rust shell (`shell/`):** Tauri scaffold with sidecar config in `tauri.conf.json` pointing at the Node + Python binaries.
6. **Webview (`webview/`):** React+TS placeholder with a "Setup" tab and a "ping" button that hits `/health`.
7. **CI:** GitHub Actions workflow runs `npm test`, `pytest`, `cargo test`, plus a Tauri build smoke.
8. **Acceptance script (`acceptance/p0.sh`):** builds everything, spins up the agent service, asserts `/health` returns `{gemini: "ok"}`, builds the Tauri bundle.

**Definition of done:** `acceptance/p0.sh` exits 0. You can `npm run dev` and see the Tauri app launch with a working Gemini ping button.

**Risks:** Tauri+sidecar bundling (see Risk #2 above). PyInstaller may need flags or fallback. ADC may need re-login if the wrong account got picked.

**You'll be asked about:** nothing if pre-flight was done correctly. Possibly which Apple Developer cert to use if signing is attempted (skip signing in P0).

---

## P1 — Ingestion + classification agents (week 2)

**Plain-English goal:** the system actually classifies and tags your inbox. You can see the Triage Queue tab populate with proposed filter rules.

**What gets built:**
1. **Gmail OAuth flow:** the `Setup` tab walks you through granting `gmail.readonly` (only). First successful sync of 30 days of mail. Tokens stored at `~/Library/Application Support/mailmind/tokens/gmail.json` with `0600` perms.
2. **Triage agent (`agents/triage_agent.py`):** reads unclassified senders from SQLite, proposes `keep / skip / newsletter / unclear` per sender. Writes to `triage_proposals` table.
3. **Extract agent (`agents/extract_agent.py`):** ADK `ParallelAgent` over threads. Per-thread structured output matching `thread-facts.schema.json`. Concurrency cap = `min(quota_rpm / 2, 8)`.
4. **Tagger agent (`agents/tagger_agent.py`):** message-level tags — urgency / category / project. Writes to `message_tags` table.
5. **Webview tabs added:** Inbox (raw mail view), Triage Queue (approve/reject sender proposals), Tags (tag stats), Contacts (contact list, no rollups yet).
6. **The "approve a sender" loop works end-to-end:** click approve → writes to `config/filters.yml` → triggers reclassify → unclassified count drops in real time.
7. **Acceptance script (`acceptance/p1.sh`):** runs all three agents over `fixtures/threads/`, asserts table counts match expected, asserts every proposal validates against schema, asserts no fabricated message IDs.

**Definition of done:** `acceptance/p1.sh` exits 0. You can sync your real inbox, watch agents classify it, and approve sender proposals through the UI.

**Risks:** Gemini RPM quota tighter than expected — ParallelAgent backs off. Schema-validation retry budget (BUILD §13) burns through faster than expected; would surface as `schema_fail` rate in observability.

**You'll be asked about:** how aggressive the Triage agent should be (suggest its initial confidence threshold for auto-proposing — high, medium, or low).

---

## P2 — Relationship rollup + follow-up (week 3)

**Plain-English goal:** the system knows who you owe replies to, who's ghosting whom, and what's pending.

**What gets built:**
1. **Relationship agent (`agents/relationship_agent.py`):** per-contact rollup writing to `contact_rollups` table. Reads prior corrections + verdicts as few-shot. Outputs draft `next_steps` with provenance.
2. **Cadence module (`ingester/src/followup-cadence.mjs`):** verbatim port from v1. Classifies threads as `overdue / waiting / cold` per the per-category latency rules in `user-context.md`.
3. **Reconcile module (`agents/reconcile.py` or kept in Node):** the v1 reconcile + the dismiss-bug fix from BUILD §19.
4. **Webview tabs added:** Follow-ups (you owe / they owe / stale), Contacts (now with rollups), Review (sample low-confidence inferences for verdicting).
5. **Self-improvement loop online:** correction goes into `corrections.jsonl`, next rollup picks it up as few-shot.
6. **Bootstrap UI:** the first-run wizard walks you through filling in `user-context.md` (sections per BUILD §15). Saves to `~/Library/Application Support/mailmind/config/user-context.md`.
7. **Acceptance script (`acceptance/p2.sh`):** rolls up fixture contacts, asserts cadence classifies a known-overdue thread, asserts the dismiss-bug regression test passes.

**Definition of done:** you can open the Follow-ups tab and see real "who am I ghosting?" data on your real inbox.

**Risks:** rollup quality depends heavily on `user-context.md` being filled in. If you skip the bootstrap, rollups will be generic.

**You'll be asked about:** sections of `user-context.md` you may want to leave blank vs. fill in.

---

## P3 — Query agent + MCP (week 4)

**Plain-English goal:** you can ask the dashboard questions in natural language and get answers with visible reasoning. External tools (Claude Code, Cursor) can query mailmind too.

**What gets built:**
1. **Query agent (`agents/query_agent.py`):** ReAct LlmAgent on Gemini Pro. Tools: `search_contacts`, `get_rollup`, `list_overdue`, `get_thread`, `get_pending_drafts`, `list_threads_by_tag`. Tool budget: 8 calls / 30s / $0.10.
2. **Streaming reasoning UI:** the Ask tab renders the agent's reasoning + tool calls live (SSE from `/query` endpoint).
3. **MCP server (`mcp/server.py`):** exposes the same tool set over stdio. Read-only — no write tools. `mcp/README.md` documents how to register it with Claude Code, Cursor, or ChatGPT desktop.
4. **Demo prompts pre-loaded:** "who am I ghosting?", "what's pending with my recruiter?", "summarize this week's job-search activity."
5. **Webview tabs added:** Ask.
6. **Acceptance script (`acceptance/p3.sh`):** query agent answers two canned questions within budget; MCP server starts, lists exactly the documented tool set, all read-only.

**Definition of done:** you can ask "who am I ghosting?" in the Ask tab and see a sourced answer in <30s. You can register the MCP server in Claude Code and ask the same question from there.

**Risks:** Gemini Pro RPM is tight (~10 RPM default). If you stress-test with rapid queries, you'll get rate-limited. Backoff is in place but UX is "please wait."

**You'll be asked about:** which MCP clients you actually want to register with (Claude Code is mandatory; Cursor / ChatGPT desktop are optional).

---

## P4 — Drafts + sends with approval gate (weeks 5–6)

**Plain-English goal:** mailmind can draft replies. You approve, it sends. Zero accidental sends, ever.

**Two-stage rollout (per resolved decision):**

### P4a — drafts only (no `gmail.send` scope yet)
1. **Draft agent (`agents/draft_agent.py`):** generates draft replies. Writes to `drafts` table with `confidence`, `rationale`, `cited_facts`, `draft_hash`. Never directly to Gmail.
2. **Approval gate UI:** Drafts tab shows pending drafts with rendered preview, diff vs. user style, cited facts, edit-in-place. **Two actions enabled: "Save as Gmail Draft" and "Reject."** Send is disabled at the OAuth-scope level.
3. **Save-to-Gmail-Drafts** uses `gmail.compose` scope (which doesn't allow sending — drafts only).
4. **Approval flow:** approve → 10s undo window → save as Gmail draft → status `saved_as_draft` → audit_log entry.
5. **Acceptance script `acceptance/p4a.sh`:** generates a draft, exercises full approval flow, asserts schema validation, asserts audit log chain.

### P4b — send capability (opt-in)
1. **Settings → Permissions** tab gets a "Enable Gmail send" toggle. Toggling triggers a fresh OAuth consent for `gmail.send`. Token stored in a separate file. Either token can be revoked.
2. **Send agent (`agents/send_agent.py`):** single `send_email` tool — refuses unless approval state is `approved` AND draft hash matches approval-time hash.
3. **Approve & Send button** appears in Drafts tab once send is enabled. 30s undo window with "Sending in 30s — Cancel" UX.
4. **Audit log chain (BUILD §8) is enforced** — append-only via SQLite triggers.
5. **Acceptance script `acceptance/p4b.sh`:** mocks `gmail.send` at the sidecar boundary, exercises the full approval+send+undo+cancel paths, verifies audit_log chain integrity.

**Definition of done:** you can say "Reply to Adam Moore confirming the call" → draft appears → you approve → 30s undo → mailmind sends. Audit log shows the chain.

**Risks:** the very first real send is a milestone — you should hand-pick the first thread to send to (a fresh test thread to your own address). The agent will pause and ask before any send happens during dev (BUILD §21 trigger #3).

**You'll be asked about:** when to flip the "Enable Gmail send" toggle (recommend: after P5 eval shows ≥85% LLM-as-judge style match).

---

## P5 — Eval + observability + self-improvement (week 7)

**Plain-English goal:** mailmind has numbers on a screen for everything — accuracy, cost, latency, schema-fail rate. You can see drift before it bites you.

**What gets built:**
1. **In-app labeling UI** (the part that requires you): the Review tab gets an "Eval set" mode where you label 20 threads + 10 rollups + 5 drafts as ground truth. Saves to `~/Library/Application Support/mailmind/evals/labeled.jsonl`. **Budget ~2 hours of your time.**
2. **Eval agent (`agents/eval_agent.py`):** runs the labeled set. Reports per agent: precision/recall, F1 on next-step priority, draft style-match (LLM-as-judge), P50/P95 latency, mean $/task, schema-fail rate.
3. **Baseline freeze:** first eval run writes `evals/baseline.jsonl`. All subsequent runs compare.
4. **Observability tab:** 7-day cost trajectory, token histograms, schema-validation failure rate, retry rate, P50/P95 latency, anomaly detection.
5. **Cost guardrails enforced:** per-task caps and per-day caps from `config/pipeline.yml` (BUILD §20). Refuse + escalate at limit.
6. **Self-improvement loop online:** `sample-for-review` → user verdict → `build-few-shot` regenerates `prompts/few-shot-examples.md` → next agent run uses them.
7. **Acceptance script (`acceptance/p5.sh`):** eval suite runs, results.jsonl populated, no metric regressed >5% vs baseline.

**Definition of done:** Observability tab shows real numbers ("extract: F1=0.91, P50=1.8s, $0.003/thread, 2.1% schema-fail rate"). You've labeled the eval set. Baseline is frozen.

**Risks:** if your eval scores are bad (F1 < 0.8 on extract, or style-match < 0.7 on drafts), there's a question about whether to advance to P4b (send capability). The agent will surface this and ask.

**You'll be asked about:** ~2 hours of labeling time, and a go/no-go on enabling Gmail send based on eval scores.

---

## P6 — Desktop polish + portfolio (week 8)

**Plain-English goal:** anyone can clone, install, see the full system in 60 seconds. Interview-grade demo.

**What gets built:**
1. **Demo dataset:** 100 anonymized fixture threads + canned `stub-gemini-responses.json`. `npm run demo` runs the full pipeline cold against fixtures with no GCP credentials needed.
2. **Tauri polish:** menu bar app, system tray, native notifications for high-priority follow-ups.
3. **Code-signed `.dmg`** (requires your Apple Developer cert).
4. **README narrative reframe:** lead with the FDE story (multi-agent, Gemini, ADK, MCP, eval, write-scope safety), architecture diagram, 60-second walkthrough GIF, eval scoreboard.
5. **Interview demo script (`docs/demo-script.md`):** 5-minute walkthrough hitting every JD bullet, 15-minute deep dive into Draft+Approval+Send, 30-second free-form Q&A demo.
6. **Architecture doc (`ARCHITECTURE.md` v2):** invariants, non-goals, cost model, failure modes.
7. **Acceptance script (`acceptance/p6.sh`):** cold-installs the .dmg in a sandboxed user, runs `npm run demo`, asserts dashboard renders with non-zero data.

**Definition of done:** public GitHub repo, working app, eval numbers, demo GIF, demo script. Shippable as a portfolio piece.

**Risks:** Apple notarization can fail in subtle ways. Without a paid Developer account, you ship an unsigned `.app` — fine for portfolio, less polished for users.

**You'll be asked about:** Apple Developer cert availability for code-signing.

---

## When the agent will pause and ask you something

Per BUILD §21, the agent must stop and confirm before:

1. Creating or modifying any GCP resource (project, billing, OAuth client).
2. Granting or requesting a new OAuth scope.
3. Sending any actual Gmail message during dev.
4. Overwriting a file in `~/Desktop/gmail-ops/` (v1 is frozen).
5. Cost guard reports projected >$10 spend for a single run.
6. A schema migration would drop a column or change a primary key.
7. About to commit anything that might be a secret.
8. Eval score regresses >5% from baseline.
9. `user-context.md` is missing required sections and bootstrap would auto-fill.
10. Tauri installer is about to be code-signed — needs your Apple cert.

In practice, expect ~5–10 confirmations across the 8 weeks: P0 toolchain decisions, P1 OAuth grant, P4 first real send, P5 send-capability go/no-go, P6 code-signing.

---

## What ships at the end

- `mailmind` GitHub repo (public).
- `mailmind.dmg` desktop app (signed if cert available, else unsigned).
- `mcp/` server registrable from Claude Code, Cursor, ChatGPT.
- Eval scoreboard with real numbers on real (your) data.
- 60-second demo GIF in the README.
- `docs/demo-script.md` for the interview.
- v1 (`gmail-ops`) still running, untouched, as fallback.

---

## TL;DR for "is it ready to run?"

Yes, with three caveats:
- You do the 4-step pre-flight (~20 min).
- You're available for ~5–10 mid-build confirmations.
- You budget ~2 hours during P5 for eval labeling.

The hand-off prompt to Claude is in the Pre-flight §5 above.
