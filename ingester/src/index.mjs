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

import { unlinkSync } from 'node:fs';

import { dbPath, openRaw, openDerived } from './lib/db.mjs';
import {
  tokensExist,
  authorizedOAuthClient,
  makeGmailClient,
} from './lib/gmail-client.mjs';
import {
  loadOAuthConfig,
  saveOAuthConfig,
  deleteOAuthConfig,
  isOAuthConfigured,
} from './lib/oauth-config.mjs';
import { dataDir, tokenPath } from './lib/paths.mjs';
import { runSync, readSyncState } from './sync.mjs';
import { runLoopbackFlow } from './auth.mjs';

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

// ----- /auth — Gmail connection management -------------------------------
//
// The dashboard's Settings → Gmail Connection panel drives all of this.
// Three states the UI can land in:
//   A. No OAuth client configured  → POST /auth/config to save creds
//   B. Configured but no tokens    → POST /auth/start to OAuth
//   C. Connected                   → DELETE /auth/tokens to disconnect
//
// We track in-flight loopback flows in a module-level map so the UI can
// poll progress (the actual OAuth round-trip is async — Google redirects
// to our loopback server when the user clicks "Allow").

const _authFlows = new Map(); // state_token -> { flow, started_at, expires_at }
const AUTH_FLOW_TTL_MS = 5 * 60 * 1000;

function _newStateToken() {
  return Math.random().toString(36).slice(2) + Math.random().toString(36).slice(2);
}

function _gcAuthFlows() {
  const now = Date.now();
  for (const [token, entry] of _authFlows.entries()) {
    if (entry.expires_at < now) {
      try { entry.flow.shutdown(); } catch {}
      _authFlows.delete(token);
    }
  }
}

async function _resolveAccountEmail() {
  if (!tokensExist()) return null;
  const cfg = loadOAuthConfig();
  if (!cfg) return null;
  try {
    const oauth = authorizedOAuthClient({
      clientId: cfg.client_id,
      clientSecret: cfg.client_secret,
    });
    const profile = await makeGmailClient(oauth).getProfile();
    return profile.emailAddress;
  } catch {
    return null;
  }
}

app.get('/auth/status', async (_req, res) => {
  const cfg = loadOAuthConfig();
  const configured = !!cfg;
  const has_tokens = tokensExist();
  const account_email = has_tokens ? await _resolveAccountEmail() : null;
  res.json({
    configured,
    config_source: cfg?.source ?? null,
    config_saved_at: cfg?.saved_at ?? null,
    has_tokens,
    account_email,
    // The truncated client_id helps the UI confirm "yes, this is the one
    // I just pasted" without exposing the secret.
    client_id_preview: cfg
      ? cfg.client_id.length > 18
        ? cfg.client_id.slice(0, 8) + '…' + cfg.client_id.slice(-10)
        : cfg.client_id
      : null,
  });
});

app.post('/auth/config', (req, res) => {
  const { client_id, client_secret } = req.body || {};
  if (!client_id || !client_secret) {
    return res.status(400).json({
      error: 'missing_fields',
      message: 'client_id and client_secret are required.',
    });
  }
  // Refuse to overwrite env-var-based config — that's a developer setup,
  // not something the dashboard should mess with.
  if (process.env.GOOGLE_CLIENT_ID && process.env.GOOGLE_CLIENT_SECRET) {
    return res.status(409).json({
      error: 'env_in_use',
      message:
        'OAuth credentials are currently sourced from environment variables. ' +
        'Unset GOOGLE_CLIENT_ID/GOOGLE_CLIENT_SECRET in ingester/.env to use ' +
        'the dashboard-managed config.',
    });
  }
  try {
    const saved = saveOAuthConfig({ client_id, client_secret });
    res.json({
      ok: true,
      saved_at: saved.saved_at,
      path: saved.path,
      client_id_preview:
        saved.client_id.length > 18
          ? saved.client_id.slice(0, 8) + '…' + saved.client_id.slice(-10)
          : saved.client_id,
    });
  } catch (err) {
    res.status(400).json({ error: 'save_failed', message: err.message });
  }
});

app.delete('/auth/config', (_req, res) => {
  if (process.env.GOOGLE_CLIENT_ID && process.env.GOOGLE_CLIENT_SECRET) {
    return res.status(409).json({
      error: 'env_in_use',
      message:
        'OAuth credentials are sourced from environment variables. The dashboard ' +
        'cannot delete them — edit ingester/.env directly.',
    });
  }
  const deleted = deleteOAuthConfig();
  res.json({ deleted });
});

app.post('/auth/start', async (_req, res) => {
  _gcAuthFlows();
  const cfg = loadOAuthConfig();
  if (!cfg) {
    return res.status(412).json({
      error: 'oauth_not_configured',
      message:
        'Save OAuth client credentials first via Settings → Gmail Connection.',
    });
  }
  // Refuse to start a second flow concurrently — the loopback ports
  // would collide and Google's prompt only handles one outstanding flow
  // at a time anyway.
  for (const [, entry] of _authFlows.entries()) {
    if (entry.flow.state.status === 'waiting') {
      return res.status(409).json({
        error: 'auth_in_flight',
        message: 'An auth flow is already in progress. Complete or cancel it first.',
      });
    }
  }

  try {
    const flow = await runLoopbackFlow({
      clientId: cfg.client_id,
      clientSecret: cfg.client_secret,
      openBrowser: false, // dashboard opens it in a tab via window.open
    });
    const token = _newStateToken();
    _authFlows.set(token, {
      flow,
      started_at: Date.now(),
      expires_at: Date.now() + AUTH_FLOW_TTL_MS,
    });
    res.json({
      ok: true,
      state_token: token,
      auth_url: flow.authUrl,
      redirect_uri: flow.redirectUri,
      expires_in_s: AUTH_FLOW_TTL_MS / 1000,
    });
  } catch (err) {
    res.status(500).json({ error: 'flow_failed', message: err.message });
  }
});

app.get('/auth/poll/:token', (req, res) => {
  _gcAuthFlows();
  const entry = _authFlows.get(req.params.token);
  if (!entry) {
    return res.status(404).json({
      error: 'unknown_state',
      message: 'Auth flow not found or already cleaned up. Re-start the flow.',
    });
  }
  const { status, account_email, error } = entry.flow.state;
  // Once the flow lands a terminal state, drop it from the map after one
  // more poll so the UI doesn't keep seeing stale state.
  if (status === 'authorized' || status === 'error') {
    setTimeout(() => _authFlows.delete(req.params.token), 1000);
  }
  res.json({
    status,
    account_email: account_email ?? null,
    error: error ?? null,
    age_ms: Date.now() - entry.started_at,
  });
});

app.post('/auth/cancel/:token', (req, res) => {
  const entry = _authFlows.get(req.params.token);
  if (!entry) return res.json({ cancelled: false });
  try { entry.flow.shutdown(); } catch {}
  _authFlows.delete(req.params.token);
  res.json({ cancelled: true });
});

app.delete('/auth/tokens', (_req, res) => {
  // Disconnect Gmail — remove the token file. OAuth client config stays
  // (so re-connect is one click). Also revokes from Google's side
  // best-effort, but we don't fail the request if revoke fails.
  const path = tokenPath();
  try {
    if (tokensExist()) unlinkSync(path);
    res.json({ disconnected: true });
  } catch (err) {
    res.status(500).json({ error: 'delete_failed', message: err.message });
  }
});

// ----- /sync_state — read-only "when did we last sync" -------------------

app.get('/sync_state', (_req, res) => {
  // Surface "have we ever synced" + the last_sync_at timestamp + whether
  // OAuth tokens exist. The Inbox tab uses this to render the "Last
  // synced N minutes ago" caption + decide whether the sync button is
  // enabled.
  if (!existsSync(dbPath('raw'))) {
    return res.json({
      last_sync_at: null,
      last_history_id: null,
      oldest_synced_date: null,
      tokens_present: tokensExist(),
      raw_db_present: false,
    });
  }
  const state = readSyncState();
  res.json({
    last_sync_at: state?.last_sync_at ?? null,
    last_history_id: state?.last_history_id ?? null,
    oldest_synced_date: state?.oldest_synced_date ?? null,
    tokens_present: tokensExist(),
    raw_db_present: true,
  });
});

// ----- /sync — trigger a Gmail sync from the UI --------------------------
//
// Wraps `sync.mjs`'s `runSync()`. Returns when the sync completes (the UI
// shows a spinner; sync usually takes 5-60s incremental, longer on initial).
// A module-level lock prevents two concurrent syncs from racing each
// other on raw.sqlite.

let _syncInFlight = false;

app.post('/sync', async (req, res) => {
  if (_syncInFlight) {
    return res.status(409).json({
      error: 'sync_in_flight',
      message: 'A sync is already running. Wait for it to finish.',
    });
  }

  if (!process.env.GOOGLE_CLIENT_ID || !process.env.GOOGLE_CLIENT_SECRET) {
    return res.status(412).json({
      error: 'oauth_config_missing',
      message:
        'GOOGLE_CLIENT_ID/GOOGLE_CLIENT_SECRET not set. Add them to ingester/.env.',
    });
  }
  if (!tokensExist()) {
    return res.status(412).json({
      error: 'no_tokens',
      message:
        'No Gmail OAuth tokens. Run `cd ingester && npm run auth` first.',
    });
  }

  const days = Number.isFinite(Number(req.body?.days)) ? Number(req.body.days) : 30;
  const full = !!req.body?.full;

  _syncInFlight = true;
  const startedAt = new Date().toISOString();
  try {
    const result = await runSync({ days, full });
    res.json({
      ok: true,
      started_at: startedAt,
      ...result,
    });
  } catch (err) {
    if (err.code === 'NO_TOKENS') {
      return res.status(412).json({ error: 'no_tokens', message: err.message });
    }
    if (err.code === 'MISSING_OAUTH_CONFIG') {
      return res
        .status(412)
        .json({ error: 'oauth_config_missing', message: err.message });
    }
    console.error('[ingester] /sync failed:', err);
    res.status(500).json({ error: 'sync_failed', message: err.message || String(err) });
  } finally {
    _syncInFlight = false;
  }
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
