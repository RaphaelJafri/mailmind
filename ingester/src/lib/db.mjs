// SQLite connection + schema helper. Idempotent — safe to call repeatedly.
//
// Two databases:
//   <dbDir>/raw.sqlite     — populated by sync, never by LLM
//   <dbDir>/derived.sqlite — populated by LLM inference + reconciliation
//
// Schema: ported verbatim from gmail-ops v1 (raw + derived). v2-only tables
// (drafts, approvals, audit_log, agent_runs, message_tags, triage_proposals)
// are managed by agents/lib/db.py — Python sidecar owns those.

import Database from 'better-sqlite3';
import { resolve } from 'node:path';
import { dbDir } from './paths.mjs';

const RAW_SCHEMA = `
CREATE TABLE IF NOT EXISTS sync_state (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  last_history_id TEXT,
  last_sync_at TIMESTAMP,
  oldest_synced_date TIMESTAMP
);

CREATE TABLE IF NOT EXISTS messages (
  message_id TEXT PRIMARY KEY,
  thread_id TEXT NOT NULL,
  from_email TEXT NOT NULL,
  from_name TEXT,
  to_emails TEXT NOT NULL,
  cc_emails TEXT,
  subject TEXT,
  body_plain TEXT,
  body_html TEXT,
  internal_date TIMESTAMP NOT NULL,
  labels TEXT,
  is_from_user BOOLEAN NOT NULL,
  raw_size_bytes INTEGER,
  synced_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_messages_thread ON messages(thread_id);
CREATE INDEX IF NOT EXISTS idx_messages_from ON messages(from_email);
CREATE INDEX IF NOT EXISTS idx_messages_date ON messages(internal_date);

CREATE TABLE IF NOT EXISTS threads (
  thread_id TEXT PRIMARY KEY,
  subject TEXT,
  message_count INTEGER NOT NULL,
  first_message_date TIMESTAMP,
  last_message_date TIMESTAMP,
  participants TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  last_synced_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_threads_last_message ON threads(last_message_date);

CREATE TABLE IF NOT EXISTS contacts (
  email TEXT PRIMARY KEY,
  display_name TEXT,
  first_seen TIMESTAMP,
  last_seen TIMESTAMP,
  message_count INTEGER DEFAULT 0,
  domain TEXT
);

-- Deterministic per-thread disposition from (Gmail labels × config/filters.yml).
-- Populated at ingest time by sync (and on demand by reclassify after the user
-- edits filters). Never written by LLM code.
--
-- disposition: 'keep' | 'skip' | 'newsletter' | 'unclassified'
CREATE TABLE IF NOT EXISTS thread_dispositions (
  thread_id TEXT PRIMARY KEY,
  disposition TEXT NOT NULL,
  sender TEXT,
  reason TEXT,
  classified_at TIMESTAMP NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_dispositions_disposition ON thread_dispositions(disposition);
CREATE INDEX IF NOT EXISTS idx_dispositions_sender ON thread_dispositions(sender);
`;

const DERIVED_SCHEMA = `
CREATE TABLE IF NOT EXISTS thread_facts (
  thread_id TEXT PRIMARY KEY,
  content_hash TEXT NOT NULL,
  extracted_at TIMESTAMP NOT NULL,
  model_version TEXT NOT NULL,
  facts_json TEXT NOT NULL,
  confidence TEXT NOT NULL,
  worker_log_path TEXT
);

CREATE TABLE IF NOT EXISTS contact_rollups (
  contact_email TEXT PRIMARY KEY,
  content_hash TEXT NOT NULL,
  rolled_up_at TIMESTAMP NOT NULL,
  model_version TEXT NOT NULL,
  relationship_summary TEXT NOT NULL,
  tone TEXT,
  cadence TEXT,
  status TEXT,
  tags TEXT,
  source_thread_ids TEXT NOT NULL,
  source_correction_ids TEXT,
  confidence TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS contact_rollup_drafts (
  contact_email TEXT PRIMARY KEY,
  rolled_up_at TIMESTAMP NOT NULL,
  model_version TEXT NOT NULL,
  drafts_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS next_steps (
  id TEXT PRIMARY KEY,
  contact_email TEXT NOT NULL,
  description TEXT NOT NULL,
  priority TEXT NOT NULL,
  due_date TIMESTAMP,
  status TEXT NOT NULL,
  source_thread_ids TEXT NOT NULL,
  source_message_ids TEXT NOT NULL,
  created_at TIMESTAMP NOT NULL,
  updated_at TIMESTAMP NOT NULL,
  resolved_at TIMESTAMP,
  confidence TEXT NOT NULL,
  resolution_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_next_steps_contact ON next_steps(contact_email);
CREATE INDEX IF NOT EXISTS idx_next_steps_status ON next_steps(status);

CREATE TABLE IF NOT EXISTS corrections (
  id TEXT PRIMARY KEY,
  timestamp TIMESTAMP NOT NULL,
  contact_email TEXT,
  entity_type TEXT NOT NULL,
  entity_id TEXT,
  field TEXT,
  old_value TEXT,
  new_value TEXT,
  user_note TEXT,
  applied_to_runs TEXT
);

CREATE TABLE IF NOT EXISTS pipeline_runs (
  run_id TEXT PRIMARY KEY,
  started_at TIMESTAMP NOT NULL,
  finished_at TIMESTAMP,
  stage TEXT NOT NULL,
  threads_processed INTEGER,
  contacts_processed INTEGER,
  workers_spawned INTEGER,
  workers_failed INTEGER,
  estimated_cost_usd REAL,
  status TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS review_items (
  id TEXT PRIMARY KEY,
  entity_type TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  contact_email TEXT,
  snapshot_json TEXT NOT NULL,
  sampled_at TIMESTAMP NOT NULL,
  reviewed_at TIMESTAMP,
  verdict TEXT,
  note TEXT,
  run_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_review_items_entity ON review_items(entity_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_review_items_pending ON review_items(reviewed_at);
`;

function openAndInit(filename, schema) {
  const db = new Database(resolve(dbDir(), filename));
  db.pragma('journal_mode = WAL');
  db.pragma('foreign_keys = ON');
  db.exec(schema);
  return db;
}

export function openRaw() {
  return openAndInit('raw.sqlite', RAW_SCHEMA);
}

export function openDerived() {
  return openAndInit('derived.sqlite', DERIVED_SCHEMA);
}

export function dbPath(kind) {
  const f = kind === 'raw' ? 'raw.sqlite' : 'derived.sqlite';
  return resolve(dbDir(), f);
}
