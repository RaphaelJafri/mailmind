#!/usr/bin/env node
// HTTP server for the Node ingester sidecar. Exposes deterministic plumbing
// (Gmail sync, classification, reconcile) to the Tauri shell over localhost.
//
// Default port: 8766. Override via PORT env or --port flag.
//
// P0 surface: /health.
// P1 surface: /threads, /contacts, /threads/:id (read-only views over
//             raw.sqlite + v1 derived tables for the webview).

import 'dotenv/config';
import { createHash } from 'node:crypto';
import express from 'express';
import { existsSync } from 'node:fs';

import { dbPath, openRaw, openDerived } from './lib/db.mjs';
import { tokensExist } from './lib/gmail-client.mjs';
import { dataDir } from './lib/paths.mjs';

const DEFAULT_PORT = 8766;
const STARTED_AT = new Date().toISOString();

function parsePort(argv) {
  for (let i = 0; i < argv.length; i++) {
    if (argv[i] === '--port') return Number(argv[++i]);
  }
  return Number(process.env.PORT) || DEFAULT_PORT;
}

const app = express();
app.use(express.json({ limit: '1mb' }));

// CORS for the Vite dev server. Tauri's bundled webview shares the origin in
// production, but `npm run dev` runs Vite on :5173 which would otherwise be
// blocked from calling the sidecar.
app.use((req, res, next) => {
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Access-Control-Allow-Methods', 'GET,POST,OPTIONS');
  res.setHeader('Access-Control-Allow-Headers', 'Content-Type');
  if (req.method === 'OPTIONS') return res.sendStatus(204);
  next();
});

app.get('/health', (_req, res) => {
  res.json({
    status: 'ok',
    sidecar: 'mailmind-ingester',
    node_version: process.version,
    data_dir: dataDir(),
    raw_db_exists: existsSync(dbPath('raw')),
    derived_db_exists: existsSync(dbPath('derived')),
    gmail_tokens_present: tokensExist(),
    started_at: STARTED_AT,
  });
});

// ----- /threads — list view -----------------------------------------------

app.get('/threads', (req, res) => {
  const limit = Math.min(Number(req.query.limit) || 100, 500);
  const disposition = req.query.disposition || null;
  if (!existsSync(dbPath('raw'))) {
    return res.json({ threads: [], count: 0, raw_db_present: false });
  }
  const db = openRaw();
  try {
    let rows;
    if (disposition) {
      rows = db
        .prepare(
          `SELECT t.thread_id, t.subject, t.message_count, t.last_message_date,
                  td.disposition, td.sender
             FROM threads t
             LEFT JOIN thread_dispositions td USING (thread_id)
             WHERE td.disposition = ?
             ORDER BY t.last_message_date DESC
             LIMIT ?`,
        )
        .all(disposition, limit);
    } else {
      rows = db
        .prepare(
          `SELECT t.thread_id, t.subject, t.message_count, t.last_message_date,
                  td.disposition, td.sender
             FROM threads t
             LEFT JOIN thread_dispositions td USING (thread_id)
             ORDER BY t.last_message_date DESC
             LIMIT ?`,
        )
        .all(limit);
    }
    res.json({ threads: rows, count: rows.length, raw_db_present: true });
  } finally {
    db.close();
  }
});

app.get('/threads/:id', (req, res) => {
  if (!existsSync(dbPath('raw'))) {
    return res.status(404).json({ error: 'raw db not present' });
  }
  const db = openRaw();
  try {
    const t = db
      .prepare(
        `SELECT thread_id, subject, message_count, first_message_date,
                last_message_date, participants
           FROM threads WHERE thread_id = ?`,
      )
      .get(req.params.id);
    if (!t) return res.status(404).json({ error: 'thread not found' });
    const messages = db
      .prepare(
        `SELECT message_id, from_email, from_name, to_emails, cc_emails,
                subject, body_plain, internal_date, is_from_user
           FROM messages WHERE thread_id = ? ORDER BY internal_date ASC`,
      )
      .all(req.params.id);

    let facts = null;
    if (existsSync(dbPath('derived'))) {
      const dd = openDerived();
      try {
        const row = dd
          .prepare('SELECT facts_json, confidence FROM thread_facts WHERE thread_id = ?')
          .get(req.params.id);
        facts = row ? { facts: JSON.parse(row.facts_json), confidence: row.confidence } : null;
      } finally {
        dd.close();
      }
    }

    res.json({ thread: t, messages, facts });
  } finally {
    db.close();
  }
});

// ----- /contacts — list view ----------------------------------------------

// ----- /gmail/drafts — P4 write seam (gmail.compose) ----------------------
//
// The Python sidecar POSTs an RFC-822 payload here when the user approves a
// "Save as Gmail Draft" action. P4a ships with the no-network mock baked in;
// the real google-apis client wiring lands once the user grants gmail.compose
// in Settings → Permissions (P4 final). The mock returns a deterministic
// gmail_draft_id so audit-log tests stay stable.

app.post('/gmail/drafts', (req, res) => {
  const { raw_rfc822 } = req.body || {};
  if (typeof raw_rfc822 !== 'string' || raw_rfc822.length === 0) {
    return res.status(400).json({ error: 'raw_rfc822 must be a non-empty string' });
  }
  // No real Gmail call yet. Return a deterministic id so the approval audit
  // row carries something stable. The Python side hashes the body for its
  // own mock id; here we keep it simple and tag the response as `mocked`.
  const hash = createHash('sha256').update(raw_rfc822).digest('hex');
  res.json({
    gmail_draft_id: `ingester-mock-${hash.slice(0, 16)}`,
    rfc822_size: raw_rfc822.length,
    mocked: true,
    note:
      'P4a stub. Wire users.drafts.create here once gmail.compose scope is granted.',
  });
});

app.get('/contacts', (req, res) => {
  const limit = Math.min(Number(req.query.limit) || 200, 1000);
  if (!existsSync(dbPath('raw'))) {
    return res.json({ contacts: [], count: 0 });
  }
  const db = openRaw();
  try {
    const rows = db
      .prepare(
        `SELECT email, display_name, message_count, first_seen, last_seen, domain
           FROM contacts
           ORDER BY message_count DESC, last_seen DESC
           LIMIT ?`,
      )
      .all(limit);
    res.json({ contacts: rows, count: rows.length });
  } finally {
    db.close();
  }
});

const port = parsePort(process.argv.slice(2));

const server = app.listen(port, '127.0.0.1', () => {
  console.log(`[ingester] listening on http://127.0.0.1:${port}`);
});

function shutdown(signal) {
  console.log(`[ingester] ${signal} — shutting down`);
  server.close(() => process.exit(0));
  setTimeout(() => process.exit(1), 5000).unref();
}
process.on('SIGINT', () => shutdown('SIGINT'));
process.on('SIGTERM', () => shutdown('SIGTERM'));
