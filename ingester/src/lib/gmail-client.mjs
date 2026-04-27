// Thin wrapper around googleapis for Gmail.
//
// Responsibilities:
//   - Load + persist OAuth tokens (mode 0600).
//   - Build an authenticated `gmail` client with auto-refresh.
//   - Paginate messages.list and history.list.
//   - Fetch + normalize message payloads (MIME tree, base64url body decoding).
//   - Exponential backoff on 429 / 403 userRateLimitExceeded.

import { google } from 'googleapis';
import {
  chmodSync,
  existsSync,
  readFileSync,
  writeFileSync,
} from 'node:fs';
import { tokenPath } from './paths.mjs';

export const GMAIL_READONLY_SCOPES = [
  'https://www.googleapis.com/auth/gmail.readonly',
];

// ---------- token persistence ----------

export function tokensExist() {
  return existsSync(tokenPath());
}

export function loadTokens() {
  const path = tokenPath();
  if (!existsSync(path)) {
    const err = new Error(`No tokens at ${path}. Run: npm run auth`);
    err.code = 'NO_TOKENS';
    throw err;
  }
  return JSON.parse(readFileSync(path, 'utf8'));
}

export function saveTokens(tokens) {
  const path = tokenPath();
  writeFileSync(path, JSON.stringify(tokens, null, 2));
  chmodSync(path, 0o600);
}

// ---------- OAuth client factory ----------

export function makeOAuthClient({ clientId, clientSecret, redirectUri }) {
  if (!clientId) throw new Error('GOOGLE_CLIENT_ID is not set');
  if (!clientSecret) throw new Error('GOOGLE_CLIENT_SECRET is not set');
  return new google.auth.OAuth2(clientId, clientSecret, redirectUri);
}

export function authorizedOAuthClient({ clientId, clientSecret }) {
  const client = makeOAuthClient({ clientId, clientSecret });
  const tokens = loadTokens();
  client.setCredentials(tokens);
  client.on('tokens', (newTokens) => {
    const merged = { ...tokens, ...newTokens };
    saveTokens(merged);
  });
  return client;
}

// ---------- retry / backoff ----------

const BACKOFF_INITIAL_MS = 500;
const BACKOFF_MAX_MS = 8000;
const MAX_RETRIES = 5;

function isRetryableError(err) {
  const status = err?.code || err?.response?.status;
  if (status === 429) return true;
  if (status === 403) {
    const msg = (err?.errors?.[0]?.reason || err?.message || '').toLowerCase();
    return (
      msg.includes('ratelimit') ||
      msg.includes('rate limit') ||
      msg.includes('userratelimitexceeded')
    );
  }
  if (['ECONNRESET', 'ETIMEDOUT', 'ENOTFOUND'].includes(err?.code)) return true;
  return false;
}

async function withBackoff(fn, { label } = {}) {
  let attempt = 0;
  let delay = BACKOFF_INITIAL_MS;
  // eslint-disable-next-line no-constant-condition
  while (true) {
    try {
      return await fn();
    } catch (err) {
      attempt += 1;
      if (attempt > MAX_RETRIES || !isRetryableError(err)) throw err;
      const jitter = Math.floor(Math.random() * 200);
      const wait = Math.min(delay, BACKOFF_MAX_MS) + jitter;
      if (process.env.MAILMIND_DEBUG) {
        console.error(
          `[gmail-client] retry ${attempt}/${MAX_RETRIES} after ${wait}ms (${label || 'call'}): ${err.message}`
        );
      }
      await new Promise((r) => setTimeout(r, wait));
      delay = Math.min(delay * 2, BACKOFF_MAX_MS);
    }
  }
}

// ---------- MIME body parsing ----------

const MAX_BODY_BYTES = 100_000;

function decodeBase64Url(data) {
  if (!data) return '';
  const b64 = data.replace(/-/g, '+').replace(/_/g, '/');
  return Buffer.from(b64, 'base64').toString('utf8');
}

function truncate(str) {
  if (!str) return str;
  const bytes = Buffer.byteLength(str, 'utf8');
  if (bytes <= MAX_BODY_BYTES) return str;
  const approxChars = MAX_BODY_BYTES;
  return str.slice(0, approxChars) + `\n…[truncated ${bytes - MAX_BODY_BYTES} bytes]`;
}

export function extractBodies(payload) {
  let plain = '';
  let html = '';
  function walk(part) {
    if (!part) return;
    const mime = (part.mimeType || '').toLowerCase();
    if (mime === 'text/plain' && !plain) {
      plain = decodeBase64Url(part.body?.data);
    } else if (mime === 'text/html' && !html) {
      html = decodeBase64Url(part.body?.data);
    }
    if (Array.isArray(part.parts)) {
      for (const p of part.parts) walk(p);
    }
  }
  walk(payload);
  return {
    body_plain: truncate(plain) || null,
    body_html: plain ? null : truncate(html) || null,
  };
}

// ---------- header helpers ----------

function headersToMap(headers) {
  const map = new Map();
  for (const h of headers || []) {
    map.set(h.name.toLowerCase(), h.value || '');
  }
  return map;
}

function parseAddresses(raw) {
  if (!raw) return [];
  const out = [];
  for (const chunk of raw.split(',')) {
    const m = chunk.match(/<([^>]+)>/);
    const email = (m ? m[1] : chunk).trim().toLowerCase();
    if (email) out.push(email);
  }
  return out;
}

function parseFirstAddress(raw) {
  const list = parseAddresses(raw);
  return list[0] || null;
}

function parseFromName(raw) {
  if (!raw) return null;
  const m = raw.match(/^\s*"?([^"<]+?)"?\s*</);
  if (m) return m[1].trim();
  return null;
}

export function normalizeMessage(msg) {
  const headers = headersToMap(msg.payload?.headers);
  const fromRaw = headers.get('from') || '';
  const from_email = parseFirstAddress(fromRaw) || '(unknown)';
  const from_name = parseFromName(fromRaw);
  const to_emails = parseAddresses(headers.get('to'));
  const cc_emails = parseAddresses(headers.get('cc'));
  const subject = headers.get('subject') || null;
  const internal_date = new Date(Number(msg.internalDate || 0)).toISOString();
  const { body_plain, body_html } = extractBodies(msg.payload);

  return {
    message_id: msg.id,
    thread_id: msg.threadId,
    from_email,
    from_name,
    to_emails,
    cc_emails,
    subject,
    body_plain,
    body_html,
    internal_date,
    labels: msg.labelIds || [],
    raw_size_bytes: msg.sizeEstimate ?? null,
  };
}

// ---------- Gmail client surface ----------

export function makeGmailClient(oauth) {
  const gmail = google.gmail({ version: 'v1', auth: oauth });

  async function getProfile() {
    const res = await withBackoff(
      () => gmail.users.getProfile({ userId: 'me' }),
      { label: 'getProfile' }
    );
    return {
      emailAddress: res.data.emailAddress,
      historyId: res.data.historyId,
      messagesTotal: res.data.messagesTotal,
    };
  }

  async function* listMessageIdsAfter(sinceIsoDate) {
    const d = new Date(sinceIsoDate);
    const q = `after:${d.getUTCFullYear()}/${d.getUTCMonth() + 1}/${d.getUTCDate()}`;
    let pageToken = undefined;
    do {
      const res = await withBackoff(
        () =>
          gmail.users.messages.list({
            userId: 'me',
            q,
            includeSpamTrash: false,
            maxResults: 500,
            pageToken,
          }),
        { label: 'messages.list' }
      );
      const messages = res.data.messages || [];
      for (const m of messages) yield m.id;
      pageToken = res.data.nextPageToken;
    } while (pageToken);
  }

  async function* listHistoryEvents(startHistoryId) {
    let pageToken = undefined;
    try {
      do {
        const res = await withBackoff(
          () =>
            gmail.users.history.list({
              userId: 'me',
              startHistoryId: String(startHistoryId),
              historyTypes: ['messageAdded', 'labelAdded', 'labelRemoved'],
              maxResults: 500,
              pageToken,
            }),
          { label: 'history.list' }
        );
        const events = res.data.history || [];
        for (const ev of events) yield ev;
        pageToken = res.data.nextPageToken;
      } while (pageToken);
    } catch (err) {
      const status = err?.code || err?.response?.status;
      if (status === 404 || status === 410) {
        yield { needsFullRefresh: true };
        return;
      }
      throw err;
    }
  }

  async function getMessage(id) {
    const res = await withBackoff(
      () =>
        gmail.users.messages.get({
          userId: 'me',
          id,
          format: 'full',
        }),
      { label: `messages.get(${id})` }
    );
    return normalizeMessage(res.data);
  }

  return {
    gmail,
    getProfile,
    listMessageIdsAfter,
    listHistoryEvents,
    getMessage,
  };
}
