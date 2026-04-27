"""SQLite handles for v2 tables owned by the Python sidecar.

Architecture (per BUILD.md §8):
- Node ingester owns `raw.sqlite` (Gmail) and the v1 portion of
  `derived.sqlite` (thread_facts, contact_rollups, next_steps, corrections,
  pipeline_runs, review_items, contact_rollup_drafts).
- Python sidecar owns the v2 additions on `derived.sqlite`
  (message_tags, triage_proposals, drafts, approvals, audit_log) and a
  separate `agent_runs.sqlite` for observability.

Both sides are idempotent: CREATE TABLE IF NOT EXISTS, no migrations needed
inside a single phase. WAL mode + a single writer per database keeps
concurrent reads cheap.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from . import paths

V2_DERIVED_SCHEMA = """
CREATE TABLE IF NOT EXISTS message_tags (
  message_id     TEXT NOT NULL,
  tag_kind       TEXT NOT NULL,
  tag_value      TEXT NOT NULL,
  confidence     TEXT NOT NULL,
  rationale      TEXT,
  tagged_at      TIMESTAMP NOT NULL,
  model_version  TEXT NOT NULL,
  agent_run_id   TEXT,
  PRIMARY KEY (message_id, tag_kind, tag_value)
);
CREATE INDEX IF NOT EXISTS idx_message_tags_kind_value
  ON message_tags(tag_kind, tag_value);
CREATE INDEX IF NOT EXISTS idx_message_tags_message
  ON message_tags(message_id);

CREATE TABLE IF NOT EXISTS triage_proposals (
  id                       TEXT PRIMARY KEY,
  sender_email             TEXT NOT NULL,
  proposed_disposition     TEXT NOT NULL,
  confidence               TEXT NOT NULL,
  rationale                TEXT NOT NULL,
  cited_user_context_section TEXT,
  sample_subjects_json     TEXT NOT NULL,
  thread_count             INTEGER NOT NULL,
  proposed_at              TIMESTAMP NOT NULL,
  reviewed_at              TIMESTAMP,
  user_decision            TEXT,
  applied_to_filters_at    TIMESTAMP,
  agent_run_id             TEXT
);
CREATE INDEX IF NOT EXISTS idx_triage_proposals_sender
  ON triage_proposals(sender_email);
CREATE INDEX IF NOT EXISTS idx_triage_proposals_decision
  ON triage_proposals(user_decision);

CREATE TABLE IF NOT EXISTS contact_rollups (
  contact_email         TEXT PRIMARY KEY,
  content_hash          TEXT NOT NULL,
  rolled_up_at          TIMESTAMP NOT NULL,
  model_version         TEXT NOT NULL,
  relationship_summary  TEXT NOT NULL,
  tone                  TEXT NOT NULL,
  cadence               TEXT NOT NULL,
  status                TEXT NOT NULL,
  tags                  TEXT NOT NULL,
  source_thread_ids     TEXT NOT NULL,
  source_correction_ids TEXT NOT NULL,
  confidence            TEXT NOT NULL,
  agent_run_id          TEXT
);

CREATE TABLE IF NOT EXISTS contact_rollup_drafts (
  contact_email TEXT PRIMARY KEY,
  rolled_up_at  TIMESTAMP NOT NULL,
  model_version TEXT NOT NULL,
  drafts_json   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS next_steps (
  id                  TEXT PRIMARY KEY,
  contact_email       TEXT NOT NULL,
  description         TEXT NOT NULL,
  priority            TEXT NOT NULL,
  due_date            TEXT,
  status              TEXT NOT NULL DEFAULT 'pending',
  source_thread_ids   TEXT NOT NULL,
  source_message_ids  TEXT NOT NULL,
  created_at          TIMESTAMP NOT NULL,
  updated_at          TIMESTAMP NOT NULL,
  resolved_at         TIMESTAMP,
  resolution_reason   TEXT,
  confidence          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_next_steps_contact ON next_steps(contact_email);
CREATE INDEX IF NOT EXISTS idx_next_steps_status  ON next_steps(status);

CREATE TABLE IF NOT EXISTS corrections (
  id              TEXT PRIMARY KEY,
  timestamp       TIMESTAMP NOT NULL,
  contact_email   TEXT,
  entity_type     TEXT NOT NULL,
  entity_id       TEXT,
  field           TEXT NOT NULL,
  old_value       TEXT,
  new_value       TEXT NOT NULL,
  user_note       TEXT,
  applied_to_runs TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_corrections_contact     ON corrections(contact_email);
CREATE INDEX IF NOT EXISTS idx_corrections_entity      ON corrections(entity_type, entity_id);

CREATE TABLE IF NOT EXISTS pipeline_runs (
  run_id              TEXT PRIMARY KEY,
  stage               TEXT NOT NULL,
  started_at          TIMESTAMP NOT NULL,
  finished_at         TIMESTAMP,
  status              TEXT NOT NULL,
  contacts_processed  INTEGER NOT NULL DEFAULT 0,
  threads_processed   INTEGER NOT NULL DEFAULT 0,
  workers_spawned     INTEGER NOT NULL DEFAULT 0,
  workers_failed      INTEGER NOT NULL DEFAULT 0,
  estimated_cost_usd  REAL,
  dropped_count       INTEGER NOT NULL DEFAULT 0,
  notes               TEXT
);

-- ---- P4: drafts / approvals / audit_log -------------------------------
-- Per BUILD §8 + §12. The full safety guarantee is:
--   1. Drafts always go through `drafts` first (no direct send path).
--   2. Send/save requires an `approvals` row whose hash matches the
--      current draft body — so post-approval edits invalidate.
--   3. `audit_log` is append-only via SQLite triggers, with a sha256
--      chain pointer (prev_hash) so external tampering is detectable.
-- The same invariants are also enforced in `lib.approval`, but having
-- them at the schema level is the belt-and-suspenders we want for
-- write actions.

CREATE TABLE IF NOT EXISTS drafts (
  id                       TEXT PRIMARY KEY,
  thread_id                TEXT NOT NULL,
  in_reply_to_message_id   TEXT,
  to_emails                TEXT NOT NULL,        -- JSON array
  cc_emails                TEXT NOT NULL DEFAULT '[]',
  bcc_emails               TEXT NOT NULL DEFAULT '[]',
  subject                  TEXT NOT NULL,
  body                     TEXT NOT NULL,
  draft_hash               TEXT NOT NULL,        -- sha256 of canonical {to,cc,bcc,subject,body}
  rationale                TEXT NOT NULL,
  cited_facts_json         TEXT NOT NULL,        -- array of {fact_id, source_message_ids[]}
  confidence               TEXT NOT NULL,
  intent                   TEXT,                 -- the human-language ask that produced this draft
  created_at               TIMESTAMP NOT NULL,
  updated_at               TIMESTAMP NOT NULL,
  status                   TEXT NOT NULL,        -- pending | approved | sent | saved_as_draft | rejected | expired
  approval_id              TEXT,                 -- FK -> approvals.id (nullable)
  gmail_draft_id           TEXT,                 -- assigned by Gmail on save_as_draft
  gmail_message_id         TEXT,                 -- assigned by Gmail on send (P4b)
  model_version            TEXT NOT NULL,
  agent_run_id             TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_drafts_status ON drafts(status);
CREATE INDEX IF NOT EXISTS idx_drafts_thread ON drafts(thread_id);

CREATE TABLE IF NOT EXISTS approvals (
  id                       TEXT PRIMARY KEY,
  draft_id                 TEXT NOT NULL,
  approval_hash            TEXT NOT NULL,        -- sha256 of canonical body at approval time
  approved_by              TEXT NOT NULL,        -- mailbox owner (raphaeljafri@gmail.com)
  approved_at              TIMESTAMP NOT NULL,
  expires_at               TIMESTAMP NOT NULL,   -- 5 min after approved_at
  action                   TEXT NOT NULL,        -- 'send' | 'save_as_draft'
  undo_window_seconds      INTEGER NOT NULL DEFAULT 30,
  cancelled_at             TIMESTAMP,
  executed_at              TIMESTAMP,
  result_status            TEXT NOT NULL DEFAULT 'pending'  -- pending | cancelled | executed | failed
);
CREATE INDEX IF NOT EXISTS idx_approvals_draft ON approvals(draft_id);
CREATE INDEX IF NOT EXISTS idx_approvals_status ON approvals(result_status);

CREATE TABLE IF NOT EXISTS audit_log (
  id                       TEXT PRIMARY KEY,
  event_at                 TIMESTAMP NOT NULL,
  event_type               TEXT NOT NULL,        -- send | save_as_draft | reject | cancel | approve | auth_grant | auth_revoke | config_change
  draft_id                 TEXT,
  approval_id              TEXT,
  draft_hash               TEXT,
  approval_hash            TEXT,
  gmail_message_id         TEXT,
  gmail_draft_id           TEXT,
  payload_json             TEXT NOT NULL,        -- non-sensitive metadata only
  prev_id                  TEXT,                 -- pointer to prior row (chain head = NULL)
  prev_hash                TEXT                  -- sha256 of prior row's canonical bytes
);
CREATE INDEX IF NOT EXISTS idx_audit_log_event_at ON audit_log(event_at);
CREATE INDEX IF NOT EXISTS idx_audit_log_event_type ON audit_log(event_type);
CREATE INDEX IF NOT EXISTS idx_audit_log_draft ON audit_log(draft_id);

-- Append-only triggers — schema-level enforcement. The Python helpers in
-- lib.approval also refuse to issue UPDATE/DELETE, but these triggers
-- catch buggy code and any direct sqlite shell mistakes.
CREATE TRIGGER IF NOT EXISTS audit_log_no_update
  BEFORE UPDATE ON audit_log
  BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;

CREATE TRIGGER IF NOT EXISTS audit_log_no_delete
  BEFORE DELETE ON audit_log
  BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
"""

AGENT_RUNS_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_runs (
  id                TEXT PRIMARY KEY,
  agent_name        TEXT NOT NULL,
  task_id           TEXT,
  parent_run_id     TEXT,
  model             TEXT NOT NULL,
  started_at        TIMESTAMP NOT NULL,
  finished_at       TIMESTAMP,
  input_tokens      INTEGER,
  output_tokens     INTEGER,
  cost_usd          REAL,
  latency_ms        INTEGER,
  tools_called_json TEXT,
  result_status     TEXT NOT NULL,
  error_message     TEXT,
  retry_count       INTEGER NOT NULL DEFAULT 0,
  stubbed           INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_agent_runs_started ON agent_runs(started_at);
CREATE INDEX IF NOT EXISTS idx_agent_runs_agent   ON agent_runs(agent_name);
CREATE INDEX IF NOT EXISTS idx_agent_runs_task    ON agent_runs(task_id);
"""


def _init(conn: sqlite3.Connection, schema: str) -> None:
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.executescript(schema)


def derived_path() -> Path:
    return paths.db_dir() / "derived.sqlite"


def agent_runs_path() -> Path:
    return paths.db_dir() / "agent_runs.sqlite"


def open_derived() -> sqlite3.Connection:
    """Open `derived.sqlite` and ensure the v2 tables exist.

    Safe to call even before the Node ingester has created the file — SQLite
    creates the file on first connect, and v1 tables get added later when
    the ingester runs. Our v2 tables are independent.
    """
    paths.db_dir().mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(derived_path())
    _init(conn, V2_DERIVED_SCHEMA)
    return conn


def open_agent_runs() -> sqlite3.Connection:
    paths.db_dir().mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(agent_runs_path())
    _init(conn, AGENT_RUNS_SCHEMA)
    return conn


@contextmanager
def derived() -> Iterator[sqlite3.Connection]:
    conn = open_derived()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


@contextmanager
def agent_runs() -> Iterator[sqlite3.Connection]:
    conn = open_agent_runs()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()
