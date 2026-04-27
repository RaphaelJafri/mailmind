# BUILD.md — operational companion to WORKPLAN-V2.md

> **Audience:** an autonomous Claude Code agent building gmail-ops v2 (`mailmind`) end-to-end.
> **Source of truth for vision:** `WORKPLAN-V2.md` (sibling doc).
> **Source of truth for v1 reference:** `~/Desktop/gmail-ops/` (frozen git tag `v1-frozen` after running `~/Desktop/scripts/freeze-v1.sh`).
> **Maintainer:** Raphael Jafri (`raphael@clyk.co` is the dev account).
> **Mailbox being managed:** `raphaeljafri@gmail.com` (personal Gmail). Treat this as the only target mailbox for v2.

This document fills every gap identified in the WORKPLAN-V2 audit. Read top to bottom on first run; on subsequent runs treat as a reference index.

---

## 0. TL;DR (plain English)

You're building a Gmail manager called **mailmind** as a polished desktop app on Google's stack — Vertex AI / Gemini 2.5, Google's ADK multi-agent framework, exposed over MCP. It's a portfolio piece for a Google FDE role and also a tool Raphael actually wants to use for his inbox.

There's already a working v1 at `~/Desktop/gmail-ops` written in Node.js with single-shot Claude prompts. v2 replaces the LLM layer with multi-agent ADK on Gemini, replaces the HTML dashboard with a Tauri+React desktop app, and adds approval-gated draft + send. Most of the deterministic plumbing (Gmail sync, SQLite schemas, follow-up cadence math, reconcile diff) gets ported from v1 — read v1 as reference, do not assume v1 is being maintained alongside.

When in doubt: keep the **architectural invariants from v1's `CLAUDE.md`** (provenance everywhere, idempotency, append-only corrections, scripts orchestrate / agents reason, dashboards never run pipelines). They survive v2.

---

## 1. Resolved decisions

These were open questions in `WORKPLAN-V2.md`. They are now closed. Do not re-debate.

| Decision | Resolution | Rationale |
|---|---|---|
| **Repo strategy** | New repo `mailmind`. v1 stays as-is. | Two artifacts tells the FDE story better than one branch. |
| **GCP project** | Reuse v1's existing GCP project (the one hosting v1's OAuth client + billing). | Already isolated from CLYK; cleaner to keep gmail-related infra in one place. |
| **Scope** | Full P0–P6 (8-week aggressive vision). | User wants the full portfolio piece. |
| **Bundling** | Polished single-installer Tauri app from P0. | User wants production polish. Use Tauri sidecars for Python+Node services (§4). |
| **v1 data migration** | v2 starts clean — fresh sync from Gmail. v1 keeps running independently as a fallback / reference. | Schema diverges; clean start avoids subtle data-shape bugs. |
| **Send safety posture** | P4 ships in two stages: (a) drafts-only mode with full approval-gate UI but Gmail send disabled at the OAuth-scope level; (b) send capability behind an explicit user-toggle in Settings → Permissions, requires re-auth with `gmail.send` scope. | Avoids any accidental send before the eval phase has validated draft quality. |
| **Eval labeling** | Build a lightweight in-app labeling UI as part of P5. Start with 20 threads + 10 rollups + 5 drafts; grow as Raphael labels. LLM-as-judge supplements but does not replace human labels. | Realistic — the user is a one-person labeling team. |
| **Repo name** | `mailmind` unless trademark-checked and rejected. | Clean, memorable, available on GitHub at time of writing (verify before `gh repo create`). |

---

## 2. v1 freezing protocol (run before P0)

v1 is the reference. It must not move. Before starting v2:

1. Raphael runs `~/Desktop/scripts/freeze-v1.sh` (created alongside this doc). The script does:
   - `cd ~/Desktop/gmail-ops`
   - `git init` (if not already a repo)
   - `git add -A`
   - `git commit -m "v1 frozen as reference for mailmind"`
   - `git tag v1-frozen`
2. v2 references v1 by absolute path: `~/Desktop/gmail-ops/...`
3. v2 must never modify v1 files. Read-only.
4. If v1 needs a real bugfix during v2 development, Raphael does it manually and re-tags `v1-frozen-2`.

**Agent rule:** if you (Claude) need to read a v1 file, use absolute paths under `~/Desktop/gmail-ops/`. If you find yourself wanting to edit one, stop and ask.

---

## 3. GCP / Vertex AI setup

The GCP project is the same one Raphael used for v1's OAuth client. Do not create a new project.

### 3.1 Discover the existing project

```bash
# Reads project ID from v1's OAuth client (the GOOGLE_CLIENT_ID is namespaced by project number)
cat ~/Desktop/gmail-ops/.env | grep GOOGLE_CLIENT_ID
# Then: gcloud projects list — match the project number prefix
```

If `gcloud` isn't installed, install via `brew install --cask google-cloud-sdk`, then `gcloud auth login` with **whichever Google account owns v1's existing GCP project**. That may be `raphael@clyk.co` (the dev/Workspace account) or `raphaeljafri@gmail.com` (the personal account that owns the mailbox). Don't assume — `gcloud projects list` after login confirms access.

### 3.2 Required APIs (enable if not already)

```bash
gcloud config set project <PROJECT_ID>
gcloud services enable \
  aiplatform.googleapis.com \
  gmail.googleapis.com \
  iamcredentials.googleapis.com
```

### 3.3 Authentication for the desktop app

**For local development:** use Application Default Credentials.
```bash
gcloud auth application-default login
```
Pick the **GCP-project-owner account** in the browser consent screen (NOT necessarily the mailbox account). The two are independent: the OAuth client lives in the GCP project, but the OAuth flow against Gmail will prompt for `raphaeljafri@gmail.com` consent at runtime. ADC writes to `~/.config/gcloud/application_default_credentials.json` and the Vertex AI Python SDK picks it up automatically.

**For the packaged Tauri installer (production):** the app must NOT ship with embedded service-account credentials. On first launch, the app prompts the user to either:
- (a) Use ADC (`gcloud` already installed locally — preferred for dev users), or
- (b) Provide an Anthropic-style API key for Gemini via `GOOGLE_GENAI_API_KEY` (Gemini API direct, bypassing Vertex). This is the path for users who don't use `gcloud`.

The agent service reads from env in this priority order:
1. `GOOGLE_GENAI_API_KEY` (direct Gemini API)
2. `GOOGLE_APPLICATION_CREDENTIALS` (service account JSON path)
3. ADC fallback

### 3.4 Region

`us-central1`. Hardcode in `mailmind/agents/lib/vertex_config.py`. Lowest latency from US-east residential, broadest model availability.

### 3.5 $300 credit application

Raphael's task — manual GCP console step (`Billing → Credits → Apply`). Claude cannot do this. Skip if already applied to the v1 project.

### 3.6 Quota verification (acceptance check for P0)

```bash
gcloud quotas list --service=aiplatform.googleapis.com \
  --filter='quota_id:aiplatform.googleapis.com/generate_content_requests_per_minute_per_project_per_base_model'
```
Acceptance: per-base-model RPM ≥ 60 for both Gemini 2.5 Flash and Pro. If lower, P0 acceptance script (`acceptance/p0.sh`) prints a warning but does not fail.

---

## 4. Process model & bundling

v2 has four runtimes. Below is the canonical layout.

```
┌────────────────────── Tauri shell (Rust) ──────────────────────┐
│                                                                  │
│  ┌─────────────────── webview (React + TS) ────────────────────┐ │
│  │  All UI tabs: Inbox / Contacts / Triage / Tags / Follow-ups │ │
│  │  / Drafts / Sent / Ask / Review / Observability / Settings  │ │
│  └─────────────────────────────────────────────────────────────┘ │
│                                                                  │
│  Tauri commands (IPC) ──► sidecar processes:                     │
│    • node-ingester (Node sidecar)   — Gmail sync, deterministic  │
│    • agent-service (Python sidecar) — ADK agents, FastAPI on     │
│                                       127.0.0.1:8765             │
│    • mcp-server    (Python sidecar) — MCP over stdio, on demand  │
│                                                                  │
│  Tauri also owns: SQLite path, log files, OAuth token storage,   │
│                   notifications, app menu, system tray.          │
└──────────────────────────────────────────────────────────────────┘
```

### 4.1 Bundling strategy

- **Tauri sidecars** for Node and Python. Tauri's `externalBin` config bundles arbitrary executables.
- **Python:** ship as a `PyInstaller --onefile` binary per platform (macOS arm64 only for v1 of v2; expand later). PyInstaller bundles ADK, google-cloud-aiplatform, FastAPI, and all deps into one ~80 MB executable. Build artifact: `dist/agent-service-macos-arm64`.
- **Node:** ship as a `pkg`-built single binary (~50 MB). Build artifact: `dist/node-ingester-macos-arm64`.
- **Tauri config:** `tauri.conf.json` declares both binaries under `tauri.bundle.externalBin`. Tauri at runtime spawns them with stdio piped.

### 4.2 Internal RPC

Tauri shell ↔ sidecars: localhost HTTP, NOT stdio (stdio is reserved for log streaming).

- `agent-service` → `127.0.0.1:8765` (FastAPI). Endpoints documented in §10.
- `node-ingester` → `127.0.0.1:8766` (Express, ported from v1's `dashboard/server.mjs`).
- `mcp-server` → stdio only (per MCP transport spec). Spawned on demand by external clients (Claude Code, Cursor, ChatGPT desktop).

Ports are randomized at launch and written to `~/Library/Application Support/mailmind/runtime.json` so the React webview can read them via Tauri command `get_runtime_endpoints()`.

### 4.3 Single-command start

**Dev mode:** `npm run dev` at the mailmind repo root spawns Tauri dev, which spawns sidecars in dev-mode (uvicorn for Python, `node` for Node, hot-reload enabled).

**Production:** double-click the `.dmg` → app launches, sidecars spawn automatically. No terminal involvement.

### 4.4 Where data lives (macOS paths)

| Data | Path |
|---|---|
| SQLite databases (`raw.sqlite`, `derived.sqlite`, `agent_runs.sqlite`) | `~/Library/Application Support/mailmind/db/` |
| OAuth tokens | `~/Library/Application Support/mailmind/tokens/gmail.json` (chmod 0600) |
| Logs (rotated daily, 7-day retention) | `~/Library/Logs/mailmind/{agent-service,node-ingester,tauri}.log` |
| User config (filters.yml, pipeline.yml, user-context.md) | `~/Library/Application Support/mailmind/config/` |
| Corrections JSONL | `~/Library/Application Support/mailmind/config/corrections.jsonl` |
| Reports | `~/Library/Application Support/mailmind/reports/` |
| Eval labeled set | `~/Library/Application Support/mailmind/evals/` |
| Runtime metadata | `~/Library/Application Support/mailmind/runtime.json` |

The Tauri shell exposes these via the standard Tauri `path` plugin. Linux/Windows paths are deferred to a later version.

### 4.5 Secrets management

- **OAuth tokens:** stored at the path above with `0600` perms, never logged, never serialized to JSON outside that file.
- **GCP credentials:** ADC managed by `gcloud` CLI, lives outside the app. The app reads `GOOGLE_APPLICATION_CREDENTIALS` env var only.
- **Gemini API key (optional):** if user provides one in Settings, stored in macOS Keychain via `tauri-plugin-keychain`. Never on disk in plaintext.
- **Audit logs:** explicitly never log token values, API keys, or full message bodies — only message IDs.

---

## 5. v2 directory tree

```
mailmind/
├── README.md                       Vision + 60-second walkthrough GIF
├── WORKPLAN-V2.md                  Copied from ~/Desktop, updated as canonical
├── BUILD.md                        This file (canonical)
├── ARCHITECTURE.md                 Invariants, non-goals, cost model — port + extend v1
├── CLAUDE.md                       Top-level agent instructions for v2
├── package.json                    Tauri dev scripts + Node sidecar deps
├── pyproject.toml                  Python sidecar deps (uv-managed)
├── Cargo.toml                      Rust shell deps
├── tauri.conf.json                 Tauri config (sidecars, permissions, paths)
│
├── shell/                          Rust Tauri shell
│   └── src/
│       ├── main.rs                 Window + sidecar lifecycle + IPC commands
│       └── commands.rs             Tauri commands (paths, runtime endpoints)
│
├── webview/                        React + TS frontend
│   ├── src/
│   │   ├── App.tsx
│   │   ├── tabs/
│   │   │   ├── Inbox.tsx
│   │   │   ├── Contacts.tsx
│   │   │   ├── TriageQueue.tsx
│   │   │   ├── Tags.tsx
│   │   │   ├── FollowUps.tsx
│   │   │   ├── Drafts.tsx
│   │   │   ├── Sent.tsx
│   │   │   ├── Ask.tsx
│   │   │   ├── Review.tsx
│   │   │   ├── Observability.tsx
│   │   │   └── Settings.tsx
│   │   ├── api/                    Typed clients for agent-service + node-ingester
│   │   └── components/
│   ├── vite.config.ts
│   └── package.json
│
├── ingester/                       Node sidecar — Gmail sync, deterministic
│   ├── src/
│   │   ├── index.mjs               HTTP server on 8766
│   │   ├── auth.mjs                Port from ~/Desktop/gmail-ops/scripts/auth.mjs
│   │   ├── sync.mjs                Port from ~/Desktop/gmail-ops/scripts/sync.mjs
│   │   ├── reclassify.mjs          Port from v1
│   │   ├── filters.mjs             Port from v1 scripts/lib/filters.mjs
│   │   ├── followup-cadence.mjs    Port from v1 scripts/followup-cadence.mjs
│   │   ├── reconcile.mjs           Port from v1 scripts/reconcile-next-steps.mjs
│   │   ├── cost-estimator.mjs      Port from v1 scripts/lib/cost-estimator.mjs
│   │   └── lib/
│   │       ├── db.mjs              Port from v1
│   │       └── gmail-client.mjs    Port from v1
│   └── package.json
│
├── agents/                         Python sidecar — ADK multi-agent
│   ├── service.py                  FastAPI app on 8765, ADK orchestrator entry
│   ├── orchestrator.py             Top-level dispatcher SequentialAgent
│   ├── triage_agent.py             P1
│   ├── extract_agent.py            P1 — ParallelAgent over threads
│   ├── tagger_agent.py             P1
│   ├── relationship_agent.py       P2
│   ├── cadence.py                  Deterministic, calls into ingester via HTTP
│   ├── query_agent.py              P3 — ReAct
│   ├── draft_agent.py              P4
│   ├── send_agent.py               P4
│   ├── eval_agent.py               P5
│   ├── review_agent.py             P5 — sample-for-review + build-few-shot
│   ├── lib/
│   │   ├── vertex_config.py        Region, model IDs, env-var auth detection
│   │   ├── gemini_runner.py        Replaces v1 claude-runner.mjs — structured output
│   │   ├── db.py                   SQLite helpers, schema migrations
│   │   ├── cost_guard.py           Token budgets, daily caps (port v1 cost-estimator)
│   │   ├── retry.py                Schema-validation retry policy (§13)
│   │   ├── audit.py                Append-only audit log helpers
│   │   ├── approval.py             Approval-gate state machine (§12)
│   │   └── prompts.py              Loads + renders prompt templates
│   └── tests/                      Pytest suite
│
├── mcp/                            MCP server (Python, stdio transport)
│   ├── server.py
│   ├── tools.py                    Tool implementations (read-only)
│   └── README.md                   Install instructions for Claude Code/Cursor
│
├── schemas/                        JSON Schemas for structured outputs
│   ├── thread-facts.schema.json    Port from v1
│   ├── contact-rollup.schema.json  Port from v1
│   ├── correction.schema.json      Port from v1
│   ├── review.schema.json          Port from v1
│   ├── draft.schema.json           NEW (§11)
│   ├── tag.schema.json             NEW (§11)
│   └── triage-proposal.schema.json NEW (§11)
│
├── prompts/                        Markdown prompt templates
│   ├── _shared.md                  Common preamble (provenance, schema rules)
│   ├── triage.md                   NEW (§14)
│   ├── extract.md                  Port v1 modes/extract-thread.md
│   ├── tagger.md                   NEW (§14)
│   ├── rollup.md                   Port v1 modes/rollup-contact.md
│   ├── query.md                    NEW (§14) — ReAct system prompt
│   ├── draft.md                    NEW (§14)
│   ├── eval-judge.md               NEW (§14) — LLM-as-judge prompt
│   ├── provenance-rules.md         Port from v1
│   ├── confidence-rubric.md        Port from v1
│   └── few-shot-examples.md        Generated by build_few_shot — gitignored
│
├── modes/                          ADK agent system-prompt scaffolds (one per agent)
│   └── (deprecated — folded into prompts/)
│
├── config/                         Default user config — copied to app data on first run
│   ├── pipeline.yml                Port + extend v1
│   ├── filters.yml                 Empty initially; populated by Triage agent + user
│   └── user-context.template.md    Template (§15) — copied to user-context.md on bootstrap
│
├── evals/
│   ├── labeled.jsonl               Hand-labeled ground truth — gitignored when private
│   ├── results.jsonl               Append-only eval results
│   ├── judge_prompts/              Versioned LLM-as-judge prompts (file name = v1, v2, ...)
│   └── README.md                   How to label, format spec
│
├── acceptance/                     Per-phase verification scripts (§16)
│   ├── p0.sh
│   ├── p1.sh
│   ├── p2.sh
│   ├── p3.sh
│   ├── p4.sh
│   ├── p5.sh
│   └── p6.sh
│
├── fixtures/                       For demo + offline tests
│   ├── threads/                    100 anonymized fixture threads
│   ├── stub-gemini-responses.json  Canned responses keyed by prompt hash (§17)
│   └── README.md                   How fixtures were anonymized
│
├── scripts/                        Repo-level dev scripts (NOT shipped)
│   ├── dev.sh                      Spawns Tauri + sidecars in hot-reload mode
│   ├── build-installer.sh          Produces signed .dmg
│   └── seed-fixtures.sh            Anonymizes a Gmail account for fixtures
│
└── .github/workflows/
    ├── ci.yml                      pytest + npm test + cargo test + tauri build smoke
    └── release.yml                 On tag, builds + signs + uploads .dmg
```

---

## 6. What to port from v1 — corrected & complete list

The original WORKPLAN-V2 list is missing items. The full list:

| v1 path | v2 path | Port type |
|---|---|---|
| `scripts/lib/db.mjs` | `ingester/src/lib/db.mjs` (Node) + `agents/lib/db.py` (Python — same schema, sqlite3) | Verbatim schema; Python is a parallel reader |
| `scripts/lib/gmail-client.mjs` | `ingester/src/lib/gmail-client.mjs` | Verbatim |
| `scripts/auth.mjs` | `ingester/src/auth.mjs` | Verbatim — but token path moves to app data dir |
| `scripts/sync.mjs` | `ingester/src/sync.mjs` | Verbatim |
| `scripts/reclassify.mjs` | `ingester/src/reclassify.mjs` | Verbatim |
| `scripts/lib/filters.mjs` | `ingester/src/filters.mjs` | Verbatim |
| `scripts/followup-cadence.mjs` | `ingester/src/followup-cadence.mjs` | Verbatim |
| `scripts/reconcile-next-steps.mjs` | `ingester/src/reconcile.mjs` | **Port + fix dashboard-dismiss bug (§19)** |
| `scripts/lib/cost-estimator.mjs` | `agents/lib/cost_guard.py` | Translate to Python; logic intact |
| `scripts/lib/plan-budget.mjs` | DROPPED | v2 uses Gemini token billing, not Claude session/weekly windows |
| `scripts/sample-for-review.mjs` | `agents/review_agent.py` | Logic preserved as a tool inside review_agent |
| `scripts/build-few-shot.mjs` | `agents/review_agent.py` | Same |
| `scripts/export-dashboard-data.mjs` | DROPPED | Tauri reads SQLite directly via agent-service endpoints |
| `dashboard/server.mjs` + `dashboard/public/*` | DROPPED | Replaced by Tauri+React |
| `schemas/thread-facts.schema.json` | `schemas/thread-facts.schema.json` | Verbatim |
| `schemas/contact-rollup.schema.json` | `schemas/contact-rollup.schema.json` | Verbatim |
| `schemas/correction.schema.json` | `schemas/correction.schema.json` | Verbatim |
| `schemas/review.schema.json` | `schemas/review.schema.json` | Verbatim |
| `prompts/provenance-rules.md` | `prompts/provenance-rules.md` | Verbatim |
| `prompts/confidence-rubric.md` | `prompts/confidence-rubric.md` | Verbatim |
| `modes/extract-thread.md` | `prompts/extract.md` | Adapted into ADK LlmAgent system prompt |
| `modes/rollup-contact.md` | `prompts/rollup.md` | Same |
| `modes/_shared.md` | `prompts/_shared.md` | Adapted (drops Claude-specific framing) |
| `modes/troubleshoot.md` | DROPPED | Surfaced in-app via Settings → Diagnostics |
| `modes/setup.md` | DROPPED | Tauri first-run wizard replaces it |
| `modes/bootstrap.md` | Migrated into in-app onboarding flow | UI + small helper agent for context interview |
| `modes/review.md` | Folded into Review tab UX | |
| `modes/nuke.md` | Settings → Reset all data | UI flow |
| `config/pipeline.yml` | `config/pipeline.yml` | Port + drop `claude_plan` block; add `gemini_models` block |
| `config/filters.yml` | `config/filters.yml` | Verbatim format |
| `corrections.jsonl` | `corrections.jsonl` | Verbatim format; lives in app data dir |
| `user-context.md` | `user-context.md` | Verbatim format; template at §15 |
| `ARCHITECTURE.md` | `ARCHITECTURE.md` | Port + add v2-specific invariants (§7) |
| `CLAUDE.md` | `CLAUDE.md` | Port + adapt for v2 stack |
| `tests/*` (186 tests, node:test) | `ingester/tests/*` (node:test, ported verbatim) + `agents/tests/*` (pytest, rewritten) | Node tests stay Node; Python tests are new equivalents |

### NOT to port

- `scripts/lib/claude-runner.mjs` — replaced by `agents/lib/gemini_runner.py`
- Interactive filters CLI (`scripts/filters.mjs` interactive mode) — replaced by Triage agent + Triage Queue tab
- Plain HTML dashboard — replaced by React+TS in Tauri
- Single-shot worker model — replaced by ADK multi-agent
- `scripts/lib/plan-budget.mjs` — Claude-plan-specific, not relevant on Gemini

---

## 7. Architectural invariants for v2 (extends v1's)

Port v1's invariants verbatim and add:

1. **Every write to Gmail goes through the Approval Gate.** No exceptions. Single `send_email` tool in `send_agent.py`. Refuses unless approval state is `approved` AND draft hash matches approval-time hash.
2. **OAuth scopes are layered.** `gmail.readonly` is granted on first run. `gmail.send` is opt-in via Settings → Permissions and triggers a fresh consent flow with a separate token file. Either token can be revoked independently.
3. **Tool budgets per agent.** Every LlmAgent declares max iterations, max wall time, max cost per task. Refuses + escalates to human at limit.
4. **Schema validation on every LLM output.** Output must pass JSON Schema validation before any state change. On failure, retry per §13.
5. **Audit log is append-only, trigger-enforced.** SQLite trigger raises on UPDATE/DELETE.
6. **Logs never contain token values, API keys, or full message bodies.** Only IDs and counts.
7. **The orchestrator is a script, never an agent.** Agents are tools the orchestrator invokes. (Carried from v1.)
8. **Idempotency by content hash everywhere.** Re-running an agent with unchanged input produces no new writes. (Carried from v1.)
9. **Provenance: every claim cites source_message_ids.** (Carried from v1.)
10. **Append-only corrections.** (Carried from v1.)
11. **`raw.sqlite` is never written by an LLM.** Only `derived.sqlite`. (Carried from v1.)

---

## 8. New SQLite tables (v2 additions)

v1's schemas (raw + derived) port verbatim. v2 adds these:

```sql
-- in derived.sqlite, alongside existing tables

CREATE TABLE IF NOT EXISTS message_tags (
  message_id TEXT NOT NULL,
  tag_kind TEXT NOT NULL,           -- 'urgency' | 'category' | 'project'
  tag_value TEXT NOT NULL,          -- 'urgent' | 'recruiting' | 'clyk' | ...
  confidence TEXT NOT NULL,
  tagged_at TIMESTAMP NOT NULL,
  model_version TEXT NOT NULL,
  PRIMARY KEY (message_id, tag_kind, tag_value)
);
CREATE INDEX IF NOT EXISTS idx_message_tags_kind_value ON message_tags(tag_kind, tag_value);

CREATE TABLE IF NOT EXISTS triage_proposals (
  id TEXT PRIMARY KEY,
  sender_email TEXT NOT NULL,
  proposed_disposition TEXT NOT NULL,  -- 'keep' | 'skip' | 'newsletter' | 'unclear'
  confidence TEXT NOT NULL,
  rationale TEXT NOT NULL,
  sample_subjects_json TEXT NOT NULL,
  thread_count INTEGER NOT NULL,
  proposed_at TIMESTAMP NOT NULL,
  reviewed_at TIMESTAMP,
  user_decision TEXT,                  -- NULL until approved/rejected
  applied_to_filters_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS drafts (
  id TEXT PRIMARY KEY,
  thread_id TEXT NOT NULL,
  in_reply_to_message_id TEXT,         -- NULL for new threads (rare)
  to_emails TEXT NOT NULL,             -- JSON array
  cc_emails TEXT,                      -- JSON array
  bcc_emails TEXT,                     -- JSON array
  subject TEXT NOT NULL,
  body TEXT NOT NULL,
  draft_hash TEXT NOT NULL,            -- sha256 of canonicalized {to,cc,bcc,subject,body}
  rationale TEXT NOT NULL,
  cited_facts_json TEXT NOT NULL,      -- array of {fact_id, source_message_ids[]}
  confidence TEXT NOT NULL,
  created_at TIMESTAMP NOT NULL,
  status TEXT NOT NULL,                -- 'pending' | 'approved' | 'sent' | 'rejected' | 'saved_as_draft' | 'expired'
  approval_id TEXT,                    -- FK → approvals.id
  model_version TEXT NOT NULL,
  agent_run_id TEXT NOT NULL           -- FK → agent_runs.id
);
CREATE INDEX IF NOT EXISTS idx_drafts_status ON drafts(status);
CREATE INDEX IF NOT EXISTS idx_drafts_thread ON drafts(thread_id);

CREATE TABLE IF NOT EXISTS approvals (
  id TEXT PRIMARY KEY,
  draft_id TEXT NOT NULL,              -- FK → drafts.id
  approval_hash TEXT NOT NULL,         -- sha256 of approved draft body at approval time
  approved_by TEXT NOT NULL,           -- the authorized mailbox owner (raphaeljafri@gmail.com)
  approved_at TIMESTAMP NOT NULL,
  expires_at TIMESTAMP NOT NULL,       -- approval valid 5 min; after that requires re-approval
  action TEXT NOT NULL,                -- 'send' | 'save_as_draft'
  undo_window_seconds INTEGER NOT NULL DEFAULT 30,
  cancelled_at TIMESTAMP,              -- if user clicks Cancel during undo window
  executed_at TIMESTAMP                -- when send/save actually happened
);

CREATE TABLE IF NOT EXISTS audit_log (
  id TEXT PRIMARY KEY,
  event_at TIMESTAMP NOT NULL,
  event_type TEXT NOT NULL,            -- 'send' | 'save_as_draft' | 'auth_grant' | 'auth_revoke' | 'config_change'
  draft_id TEXT,
  approval_id TEXT,
  draft_hash TEXT,
  approval_hash TEXT,
  gmail_message_id TEXT,               -- assigned by Gmail on send
  payload_json TEXT NOT NULL,          -- non-sensitive metadata only
  prev_id TEXT,                        -- chain pointer
  prev_hash TEXT                       -- sha256 of prev row, for tamper detection
);
-- Trigger: forbid UPDATE and DELETE on audit_log.
CREATE TRIGGER IF NOT EXISTS audit_log_no_update BEFORE UPDATE ON audit_log
  BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_log_no_delete BEFORE DELETE ON audit_log
  BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
```

```sql
-- in a new database: agent_runs.sqlite

CREATE TABLE IF NOT EXISTS agent_runs (
  id TEXT PRIMARY KEY,                 -- ULID
  agent_name TEXT NOT NULL,
  task_id TEXT,                        -- thread_id for extract, contact_email for rollup, etc.
  parent_run_id TEXT,                  -- for nested ADK calls
  model TEXT NOT NULL,                 -- 'gemini-2.5-flash' | 'gemini-2.5-pro'
  started_at TIMESTAMP NOT NULL,
  finished_at TIMESTAMP,
  input_tokens INTEGER,
  output_tokens INTEGER,
  cost_usd REAL,
  latency_ms INTEGER,
  tools_called_json TEXT,              -- array of tool-call objects
  result_status TEXT NOT NULL,         -- 'success' | 'schema_fail' | 'tool_budget_exceeded' | 'cost_cap_exceeded' | 'error'
  error_message TEXT,
  retry_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_agent_runs_started ON agent_runs(started_at);
CREATE INDEX IF NOT EXISTS idx_agent_runs_agent ON agent_runs(agent_name);
CREATE INDEX IF NOT EXISTS idx_agent_runs_task ON agent_runs(task_id);
```

---

## 9. ADK agent skeleton

Reference shape for every agent in `agents/`. `agents/triage_agent.py` is the canonical example:

```python
# agents/triage_agent.py
from google.adk.agents import LlmAgent
from google.adk.tools import FunctionTool
from .lib.vertex_config import flash_model
from .lib.cost_guard import enforce_budget
from .lib.db import open_derived
from .lib.prompts import load_prompt

def list_unclassified_senders(min_thread_count: int = 3) -> list[dict]:
    """Returns senders with disposition='unclassified' grouped by sender,
    each with count + sample subjects. Read-only."""
    db = open_derived()
    rows = db.execute("""
        SELECT sender, COUNT(*) AS thread_count,
               GROUP_CONCAT(subject, ' | ') AS sample_subjects
        FROM raw.thread_dispositions td
        JOIN raw.threads t USING (thread_id)
        WHERE disposition = 'unclassified'
        GROUP BY sender
        HAVING thread_count >= :min
        ORDER BY thread_count DESC
        LIMIT 50
    """, {"min": min_thread_count}).fetchall()
    return [dict(r) for r in rows]

def write_triage_proposal(proposal: dict) -> dict:
    """Persists a single proposal to triage_proposals table.
    Schema-validated against schemas/triage-proposal.schema.json before write."""
    # ... validation + insert ...

triage_agent = LlmAgent(
    name="triage_agent",
    model=flash_model(),
    description="Proposes keep/skip/newsletter classification for unclassified senders.",
    instruction=load_prompt("triage.md"),
    tools=[
        FunctionTool(list_unclassified_senders),
        FunctionTool(write_triage_proposal),
    ],
    # Budget enforcement is a callback wrapper:
    before_tool_callback=enforce_budget(max_tool_calls=20, max_cost_usd=0.50),
    output_key="triage_run_summary",
)
```

Key conventions:
- Every tool function has a typed signature (ADK reflects on it for schema).
- Tool docstrings are surfaced to the LLM — write them as if for the agent, not human readers.
- `enforce_budget` is the universal cost-cap callback (defined in `agents/lib/cost_guard.py`).
- Agent state passed via `output_key` and ADK's `Session` object (not globals).
- For agents with side effects (writes to DB), validate via JSON Schema in the tool function before insert.

---

## 10. agent-service HTTP API (FastAPI on 8765)

The Tauri shell talks to the Python agent service over HTTP. Endpoints:

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | Liveness + Vertex AI connectivity check |
| `POST` | `/triage/run` | Kick off Triage agent run (async — returns run_id) |
| `GET` | `/triage/proposals?status=pending` | List pending proposals for UI |
| `POST` | `/triage/proposals/{id}/approve` | Approve a proposal → writes to filters.yml + reclassify |
| `POST` | `/triage/proposals/{id}/reject` | Reject |
| `POST` | `/extract/run` | Kick off Extract agent over thread list (async) |
| `POST` | `/tag/run` | Kick off Tagger over message list |
| `POST` | `/relationship/run` | Kick off Relationship rollup |
| `POST` | `/cadence/run` | Trigger cadence recompute (deterministic, calls into ingester) |
| `GET` | `/followups?bucket=overdue` | List follow-ups |
| `POST` | `/query` | ReAct query agent — streams tool calls + final answer (SSE) |
| `POST` | `/draft/generate` | Generate a draft for a thread (returns draft_id) |
| `GET` | `/drafts?status=pending` | List pending drafts |
| `POST` | `/drafts/{id}/approve` | Approve → enters undo window |
| `POST` | `/drafts/{id}/cancel` | Cancel during undo window |
| `POST` | `/drafts/{id}/save_as_gmail_draft` | Save to Gmail Drafts (no send) |
| `POST` | `/drafts/{id}/reject` | Reject |
| `GET` | `/drafts/{id}/diff` | Returns user-style diff payload |
| `GET` | `/agent_runs?since=...` | Stream of agent runs for Observability tab |
| `GET` | `/eval/results?suite=extract` | Latest eval scores |
| `POST` | `/eval/run?suite=all` | Kick off full eval |
| `GET` | `/runs/{id}/logs` | Live log stream (SSE) |

All endpoints return `application/json` unless SSE is noted. All async operations return `{run_id}` immediately and surface progress via `/runs/{id}/logs`.

---

## 11. New JSON Schemas

### `schemas/triage-proposal.schema.json`
```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "TriageProposal",
  "type": "object",
  "required": ["sender_email", "proposed_disposition", "confidence", "rationale", "sample_subjects", "thread_count"],
  "properties": {
    "sender_email": {"type": "string", "format": "email"},
    "proposed_disposition": {"enum": ["keep", "skip", "newsletter", "unclear"]},
    "confidence": {"enum": ["high", "medium", "low"]},
    "rationale": {"type": "string", "maxLength": 500},
    "sample_subjects": {"type": "array", "items": {"type": "string"}, "maxItems": 10},
    "thread_count": {"type": "integer", "minimum": 1},
    "cited_user_context_section": {"type": "string"}
  }
}
```

### `schemas/tag.schema.json`
```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "MessageTag",
  "type": "object",
  "required": ["message_id", "tags"],
  "properties": {
    "message_id": {"type": "string"},
    "tags": {
      "type": "object",
      "properties": {
        "urgency": {"enum": ["urgent", "followup", "informational", "automated", "ignore"]},
        "category": {"enum": ["recruiting", "legal", "personal", "finance", "vendor", "newsletter", "transactional", "other"]},
        "project": {"type": "array", "items": {"type": "string"}}
      },
      "additionalProperties": false
    },
    "confidence": {"enum": ["high", "medium", "low"]}
  }
}
```

### `schemas/draft.schema.json`
```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "DraftReply",
  "type": "object",
  "required": ["thread_id", "to_emails", "subject", "body", "rationale", "cited_facts", "confidence"],
  "properties": {
    "thread_id": {"type": "string"},
    "in_reply_to_message_id": {"type": ["string", "null"]},
    "to_emails": {"type": "array", "items": {"type": "string", "format": "email"}, "minItems": 1},
    "cc_emails": {"type": "array", "items": {"type": "string", "format": "email"}, "default": []},
    "bcc_emails": {"type": "array", "items": {"type": "string", "format": "email"}, "default": []},
    "subject": {"type": "string", "minLength": 1, "maxLength": 200},
    "body": {"type": "string", "minLength": 1, "maxLength": 8000},
    "rationale": {"type": "string", "maxLength": 1000},
    "cited_facts": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["fact_id", "source_message_ids"],
        "properties": {
          "fact_id": {"type": "string"},
          "source_message_ids": {"type": "array", "items": {"type": "string"}, "minItems": 1}
        }
      },
      "minItems": 1
    },
    "confidence": {"enum": ["high", "medium", "low"]}
  }
}
```

---

## 12. Approval gate state machine

```
draft created (status=pending)
   │
   ├──► user clicks Reject ──► status=rejected (terminal)
   │
   ├──► user clicks Save as Gmail Draft
   │     ├─ approval row created (action=save_as_draft, undo=10s)
   │     ├─ undo window starts
   │     │     ├─ user cancels ──► approval.cancelled_at set, draft stays pending
   │     │     └─ window expires ──► save_as_gmail_draft executed
   │     └─ on success: status=saved_as_draft, audit_log entry
   │
   └──► user clicks Approve & Send (only if gmail.send scope granted)
         ├─ approval row created (action=send, undo=30s)
         ├─ approval_hash = sha256(canonicalized draft body)
         ├─ undo window starts (UI shows "Sending in 30s — Cancel")
         │     ├─ user cancels ──► approval.cancelled_at set, draft stays pending
         │     └─ window expires
         │           ├─ Send agent invoked
         │           ├─ verifies approval.expires_at > now
         │           ├─ verifies sha256(current draft body) == approval_hash
         │           ├─ calls gmail.send
         │           └─ on success: status=sent, audit_log entry with gmail_message_id
         │
         └─ if any verification fails: status stays pending, user notified, no send
```

**Edge cases:**
- **Approved-then-edited:** if user edits the draft after approving, the draft_hash changes → next send attempt fails the hash check → user must re-approve.
- **Approval expires (5 min):** Send agent refuses, prompts re-approval.
- **Partial-send failure:** Gmail returns non-2xx. Status stays `approved`, error logged, user notified, retry button shown.
- **Concurrent approvals:** UI disables approve button while one is in flight per draft.

---

## 13. Schema-validation retry policy

When a Gemini structured-output call returns invalid JSON or fails schema validation:

1. **First failure:** retry once with the same prompt + the validation error message appended as a system note.
2. **Second failure:** retry with a "repair" prompt — feed the malformed output back and ask Gemini to fix only the validation issues.
3. **Third failure:** mark the agent_run as `result_status=schema_fail`, write the malformed output to a `schema_failures/` dir for inspection, surface to user via Observability tab.

Retry counter on `agent_runs.retry_count`. Cost guard counts retries against the per-task budget — three retries cost 3× one call, hitting the cost cap fast.

For Gemini API errors (5xx, rate limit, timeout): exponential backoff with jitter. Base 1s, cap 32s, max 5 retries. Distinct from schema retries — these don't count against the 3-retry semantic budget.

---

## 14. Agent prompt skeletons

Adapted from v1 `modes/`. Full text lives in `prompts/*.md` files. Skeletons here:

**`prompts/_shared.md`** (loaded by every agent):
```
You are an agent in mailmind, a multi-agent Gmail manager.

INVARIANTS YOU MUST FOLLOW:
- Every claim cites source_message_ids. No exceptions.
- Output strict JSON matching the schema in this prompt's "OUTPUT SCHEMA" section.
- If you cannot answer with high confidence, return confidence="low" with empty results
  rather than fabricating.
- You do not have memory across runs unless given explicit context. Treat each call as fresh.
- The user is the system of record. You propose; the user disposes.

User context follows. Treat it as ground truth about Raphael's preferences and relationships:
{{USER_CONTEXT}}

Few-shot examples follow. These are PAST inferences the user verdicted. Mirror their shape:
{{FEW_SHOT_EXAMPLES}}
```

**`prompts/triage.md`** — proposes filter rules for unclassified senders. Inputs: sender list with sample subjects + thread counts. Outputs: array of TriageProposal. Cite which user-context section informed each call (work / personal / etc.).

**`prompts/extract.md`** — port from `~/Desktop/gmail-ops/modes/extract-thread.md`. Replace any "Claude" references with "the extraction agent". Schema reference unchanged (`thread-facts.schema.json`).

**`prompts/tagger.md`** — message-level tagging. Input: a message body + thread context. Output: MessageTag JSON. Confidence calibrated against known categories from user-context.

**`prompts/rollup.md`** — port from `~/Desktop/gmail-ops/modes/rollup-contact.md`. Same swap.

**`prompts/query.md`** — ReAct system prompt. Tells the agent: tool budget (8 calls), wall time (30s), cost cap ($0.10). Forbid recursion. Stream reasoning + tool calls.

**`prompts/draft.md`** — generates a reply. Inputs: thread context, user style (read from a `user-style.md` file if present, else inferred), relationship rollup, intent ("Reply to Adam Moore confirming the call time"). Outputs: DraftReply JSON with cited_facts.

**`prompts/eval-judge.md`** — LLM-as-judge prompt for draft quality. Versioned: filename includes `_v1`, `_v2`, etc. Each version eval'd against held-out human-verdicted drafts before being adopted as the active judge.

---

## 15. user-context.md template

Lives at `config/user-context.template.md`, copied to user-context.md on first bootstrap. Structure:

```markdown
# User context — Raphael Jafri

## Identity
- Name: Raphael Jafri
- Primary email (mailbox managed by mailmind): raphaeljafri@gmail.com
- Other emails: raphael@clyk.co (CLYK / dev work — not managed by mailmind)
- Role: <e.g. founder of CLYK, building iOS app>
- Location & timezone: <e.g. NYC / America/New_York>

## Active projects
- **CLYK** — <one-line description>
- **mailmind** (this tool) — <one-line>
- <others>

## Important contacts
<one bullet per important contact>
- <Name> (<email>) — <relationship one-liner> [reply-latency: 2d]

## Reply-latency expectations
- recruiting: 1d
- legal: 3d
- vendor: 5d
- personal: 1d
- newsletter: never (informational only)
- transactional: never (no reply expected)
- automated: never

## Communication style
- Tone: <e.g. warm but direct>
- Preferred sign-off: <e.g. "—Raphael">
- Sentence length: <e.g. short, conversational>
- Formality: <e.g. low for known contacts, medium-high for cold outreach>

## Boundaries
- Topics I will not discuss over email: <e.g. equity terms before a call>
- Senders I never want surfaced as follow-ups: <list of emails / domains>
- Auto-archive rules I want enforced: <list>

## How I want mailmind to act
- <free-form notes — read by every agent as context>
```

The bootstrap UI walks the user through filling each section. Sections are appended, not overwritten, so a re-bootstrap adds missing sections without clobbering existing prose.

---

## 16. Acceptance scripts (per phase)

Each `acceptance/pN.sh` is a single shell script that exits 0 if the phase is "done" and non-zero otherwise. Claude runs them after every phase to self-verify. Sample:

**`acceptance/p0.sh`**
```bash
#!/usr/bin/env bash
set -euo pipefail

# Build everything
cargo build --manifest-path shell/Cargo.toml
(cd webview && npm install && npm run build)
(cd ingester && npm install && npm test)
uv run --directory agents pytest -q
cargo build --release --manifest-path shell/Cargo.toml

# Smoke: spawn agent service, hit /health
(cd agents && uv run uvicorn service:app --port 8765 &) ; SVC_PID=$!
sleep 3
HEALTH=$(curl -s http://127.0.0.1:8765/health)
echo "$HEALTH" | jq -e '.gemini == "ok"' >/dev/null
kill $SVC_PID

# Tauri bundle smoke
npm run tauri build -- --target aarch64-apple-darwin

echo "P0 OK"
```

**`acceptance/p1.sh`** — runs Triage, Extract, Tagger over `fixtures/threads/`, asserts table row counts, asserts all proposals validate against schema, asserts no fabricated message IDs.

**`acceptance/p2.sh`** — relationship rollup over fixtures, asserts cadence classifies a known overdue thread, asserts reconcile bug fix (§19) resolves a "dismiss" correction without re-creating the next_step.

**`acceptance/p3.sh`** — query agent answers two canned questions ("who am I ghosting?", "what's pending with my recruiter?") within budget. MCP server starts, lists exactly the documented tool set, all read-only.

**`acceptance/p4.sh`** — generates a draft for a fixture thread, asserts schema validation, exercises full approval flow with mock send (Gmail API mocked at the sidecar boundary), verifies audit_log row chain.

**`acceptance/p5.sh`** — runs eval suite, asserts results.jsonl has rows, asserts no eval score regressed >5% vs `evals/baseline.jsonl` (committed once at P5 start).

**`acceptance/p6.sh`** — cold-installs the .dmg in a sandboxed user, runs `npm run demo`, asserts dashboard renders with non-zero data after fixture run.

---

## 17. Stub Gemini runner (for tests + demo mode)

`agents/lib/gemini_runner.py` accepts an env var `MAILMIND_STUB_RESPONSES` pointing at a JSON file:

```json
{
  "<sha256 of (model + system_prompt + user_prompt)>": {
    "output": "<canned JSON string matching the requested schema>",
    "input_tokens": 1234,
    "output_tokens": 567,
    "latency_ms": 50
  }
}
```

When set, every Gemini call hashes its inputs, looks up the response, and returns it without calling Vertex. Used by:
- All Pytest tests (`fixtures/stub-gemini-responses.json`).
- `npm run demo` end-to-end pipeline against `fixtures/threads/`.
- CI (no Gemini credentials needed for PR checks).

If a hash misses, the runner errors with the missing hash + the inputs that produced it, so a developer can record a new fixture by running once against real Gemini and saving the response.

---

## 18. Rate limits & quotas

- **Gmail API:** 250 quota units/user/sec, ~1B/day. Sync uses ~5 units/message. Backoff: exponential, base 1s, cap 60s, retry 7 times.
- **Vertex AI Gemini Flash:** ~60 RPM default in `us-central1`. Acceptance script in §3.6 verifies. ParallelAgent concurrency cap = `min(quota_rpm / 2, 8)`.
- **Vertex AI Gemini Pro:** ~10 RPM default. Draft + Query agents are serialized at the orchestrator level — no concurrent Pro calls.
- **Local SQLite:** WAL mode (carried from v1). One writer at a time enforced by ingester sidecar holding the write lock.

Rate-limit hits log to `agent_runs.result_status='rate_limited'` and surface in Observability.

---

## 19. The reconcile dismiss bug (concrete fix spec)

**v1 bug:** when the user clicks "Dismiss" on a `next_step` in the dashboard, the row's `status` is updated to `dismissed`, but the next rollup run regenerates a near-identical step (the `corrections` table is read but not consulted on the dismiss path). Net effect: dismissals don't stick.

**v2 fix:** `agents/reconcile.py` (or the Node port) on every run:
1. Reads `corrections` rows where `entity_type='next_step'` and `field='status'` and `new_value='dismissed'`.
2. Builds a set of (contact_email, normalized_description) tuples that should never be recreated.
3. When matching draft proposals against existing steps, also matches against this dismiss set. If a proposal Jaccard-overlaps a dismissed step ≥ 0.7, it's silently dropped (with a row in `pipeline_runs.dropped_count` for observability).

Tests: `agents/tests/test_reconcile.py::test_dismiss_persists_across_rollups` — seed a dismiss correction, run rollup, assert no new step matching it.

---

## 20. Cost guardrails — concrete numbers

`config/pipeline.yml` (v2 version) adds:

```yaml
gemini_models:
  flash: gemini-2.5-flash
  pro: gemini-2.5-pro

cost_caps:
  per_task_usd:
    triage: 0.05
    extract: 0.02
    tagger: 0.005
    relationship: 0.05
    cadence: 0.00          # deterministic, no LLM
    query: 0.10
    draft: 0.20
    eval_judge: 0.02
  per_day_usd:
    total: 5.00
    soft_warn_pct: 70
    hard_stop_pct: 100
```

`agents/lib/cost_guard.py` enforces per-task caps via the ADK `before_tool_callback`. Per-day caps are enforced at the agent_runs level — before invoking any agent, check `SUM(cost_usd) WHERE started_at > date('now', 'start of day')` against `per_day_usd.total`.

Production target (after P5 stabilization): **< $5/month for personal use** (~1000 threads/week, mostly cached).

---

## 21. "Ask the user" trigger list

Claude (the building agent) MUST pause and ask Raphael when:

1. About to create or modify any GCP resource (project, billing, OAuth client).
2. About to grant or request a new OAuth scope.
3. About to send any actual Gmail message during development (vs. fixture/mock).
4. About to overwrite a file in `~/Desktop/gmail-ops/` (v1 is frozen).
5. Cost guard reports projected >$10 spend for a single run.
6. A schema migration would drop a column or change a primary key.
7. About to commit a file that might contain secrets (.env, tokens, JSON keys).
8. An eval score regresses more than 5% from baseline.
9. The user-context.md is missing required sections and bootstrap would auto-fill them.
10. The Tauri installer is about to be code-signed — Raphael must provide the Apple Developer cert manually.

Otherwise, proceed without asking.

---

## 22. Observability — log paths & schemas

- Per-agent logs: `~/Library/Logs/mailmind/agent-service.log` (one line = one structured JSON object, `agent_runs` row + freeform message).
- Daily rotation, keep 7 days, compress older. Use Python `logging.handlers.TimedRotatingFileHandler`.
- Observability tab queries `agent_runs.sqlite` directly for charts; tails the log file for live event stream.
- Anomaly detection (P5): flag agent runs where latency > 3× rolling P95 or cost > 2× rolling P95. Surface as "Anomalies" panel in Observability tab.

---

## 23. Eval rigor

- **Held-out set:** 20 threads + 10 rollups + 5 drafts at P5 start. Locked — never used for few-shot training. Stored in `evals/labeled.jsonl` (gitignored if it contains real user data).
- **Baseline:** first eval run at P5 start writes `evals/baseline.jsonl`. All subsequent runs compare to it.
- **Regression budget:** any metric dropping >5% vs baseline fails CI (`acceptance/p5.sh` exits non-zero).
- **LLM-as-judge:** prompts versioned in `evals/judge_prompts/v1.md`, `v2.md`, etc. Each new judge version is itself eval'd against the same human-verdicted set; only adopted if it agrees with humans ≥85%.
- **Eval set growth:** the in-app labeling UI lets Raphael add labels incrementally. New labels appended to `labeled.jsonl`, baseline re-frozen on user request only.

---

## 24. Single-command demo (P6 deliverable)

`npm run demo` from a freshly cloned mailmind repo:
1. Installs all deps (Rust toolchain, Node, Python via uv).
2. Sets `MAILMIND_STUB_RESPONSES=fixtures/stub-gemini-responses.json`.
3. Seeds SQLite with `fixtures/threads/`.
4. Runs full pipeline: triage → extract → tag → rollup → cadence → reconcile.
5. Spawns agent-service against the fixture DB.
6. Builds + launches Tauri app pointing at the demo data.
7. Opens to the Inbox tab with a non-empty dashboard.

No GCP credentials, no Gmail account, no real network calls. ~60s on a recent Mac.

---

## 25. CLAUDE.md for v2 (skeleton)

Top of `mailmind/CLAUDE.md`. Drop in v2 with these section headers, mostly ported from v1:

1. **Read first:** ARCHITECTURE.md is the self-awareness anchor.
2. **First-run setup detection** (port v1's pattern, adapt for new paths).
3. **What this is** — multi-agent Gemini-backed Gmail manager.
4. **Design principles** — port v1's seven principles verbatim, add the v2-specific invariants from §7 of this doc.
5. **Repository map** — point at the directory tree in §5.
6. **Modes / routing** — v2 doesn't use modes; route by agent name + UI surface.
7. **Data contract — user layer vs system layer** — port v1's, update paths.
8. **Sensitive files** — port v1's, add Gemini API key.
9. **Anti-patterns** — port v1's, add v2-specific (no auto-send, no batch approve, no LLM diffing of drafts).

---

## 26. Open items still requiring human action

These are not gaps in the spec — they're tasks only Raphael can do. List them at the top of the agent's work plan:

- [ ] Apply the $300 GCP credit (manual console step).
- [ ] Confirm `mailmind` GitHub name available, or pick alternative.
- [ ] Provide Apple Developer cert for code-signing the .dmg (P6).
- [ ] Label the initial eval set (20 threads + 10 rollups + 5 drafts) at P5 start.
- [ ] Decide if v1 keeps running in parallel for the duration, or gets archived after v2 reaches feature parity.
- [ ] If publishing the repo: scrub `fixtures/` for any non-anonymized data.

---

## 27. Pre-flight checklist (run before handing this off to Claude)

- [ ] `~/Desktop/scripts/freeze-v1.sh` has been run. v1 is git-tagged `v1-frozen`.
- [ ] `gcloud auth application-default login` completed as `raphael@clyk.co`.
- [ ] `gcloud services list --enabled` shows `aiplatform.googleapis.com`.
- [ ] `gh auth status` shows GitHub authentication is good.
- [ ] `mailmind` is available on GitHub (or alternative name chosen).
- [ ] This file (`WORKPLAN-V2-BUILD.md`) is committed in the same place as `WORKPLAN-V2.md`.
- [ ] An empty `mailmind/` repo is initialized, with `WORKPLAN-V2.md`, `WORKPLAN-V2-BUILD.md`, and a one-line `README.md` pointing at the workplan.

When all boxes are checked, hand `WORKPLAN-V2.md` + `WORKPLAN-V2-BUILD.md` to Claude with a single prompt:

> Build mailmind end-to-end per WORKPLAN-V2.md and WORKPLAN-V2-BUILD.md. Start with phase P0. Run `acceptance/pN.sh` after each phase and do not advance until it returns 0. Pause and ask me when any §21 trigger fires.
