// P0 smoke tests for the ingester. Verifies schema creation, thread hash
// determinism, gmail-client header parsers. Does NOT hit Gmail.

import { test } from 'node:test';
import { ok, equal, deepEqual, throws } from 'node:assert/strict';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { resolve } from 'node:path';

// Override data dir so tests don't touch the real ~/Library path.
process.env.MAILMIND_DATA_DIR = mkdtempSync(resolve(tmpdir(), 'mailmind-test-'));

const { openRaw, openDerived } = await import('../src/lib/db.mjs');
const { threadContentHash } = await import('../src/lib/thread-hash.mjs');
const { extractBodies, normalizeMessage } = await import('../src/lib/gmail-client.mjs');
const { classifyThread, isMarketingCandidate } = await import('../src/lib/filters.mjs');
const { readSyncState } = await import('../src/sync.mjs');

test('openRaw creates schema idempotently', () => {
  const db1 = openRaw();
  const db2 = openRaw();
  const tables = db1
    .prepare(`SELECT name FROM sqlite_master WHERE type='table' ORDER BY name`)
    .all()
    .map((r) => r.name);
  ok(tables.includes('messages'));
  ok(tables.includes('threads'));
  ok(tables.includes('contacts'));
  ok(tables.includes('thread_dispositions'));
  ok(tables.includes('sync_state'));
  db1.close();
  db2.close();
});

test('openDerived creates derived schema', () => {
  const db = openDerived();
  const tables = db
    .prepare(`SELECT name FROM sqlite_master WHERE type='table' ORDER BY name`)
    .all()
    .map((r) => r.name);
  ok(tables.includes('thread_facts'));
  ok(tables.includes('contact_rollups'));
  ok(tables.includes('next_steps'));
  ok(tables.includes('corrections'));
  ok(tables.includes('review_items'));
  db.close();
});

test('threadContentHash is order-insensitive', () => {
  const a = threadContentHash({
    messageIds: ['m1', 'm2', 'm3'],
    lastMessageDate: '2026-04-26T12:00:00Z',
  });
  const b = threadContentHash({
    messageIds: ['m3', 'm1', 'm2'],
    lastMessageDate: '2026-04-26T12:00:00Z',
  });
  equal(a, b);
});

test('threadContentHash changes when last date changes', () => {
  const a = threadContentHash({
    messageIds: ['m1', 'm2'],
    lastMessageDate: '2026-04-26T12:00:00Z',
  });
  const b = threadContentHash({
    messageIds: ['m1', 'm2'],
    lastMessageDate: '2026-04-26T12:01:00Z',
  });
  ok(a !== b);
});

test('threadContentHash rejects empty / invalid input', () => {
  throws(() =>
    threadContentHash({ messageIds: [], lastMessageDate: '2026-01-01' })
  );
  throws(() =>
    threadContentHash({ messageIds: ['m1'], lastMessageDate: 'not-a-date' })
  );
});

test('extractBodies handles a simple text/plain payload', () => {
  // base64url of "hello world"
  const b64 = Buffer.from('hello world').toString('base64')
    .replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
  const out = extractBodies({
    mimeType: 'text/plain',
    body: { data: b64 },
  });
  equal(out.body_plain, 'hello world');
  equal(out.body_html, null);
});

test('extractBodies prefers text/plain over text/html', () => {
  const enc = (s) =>
    Buffer.from(s).toString('base64')
      .replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
  const out = extractBodies({
    mimeType: 'multipart/alternative',
    parts: [
      { mimeType: 'text/plain', body: { data: enc('plain version') } },
      { mimeType: 'text/html', body: { data: enc('<p>html version</p>') } },
    ],
  });
  equal(out.body_plain, 'plain version');
  equal(out.body_html, null);
});

test('normalizeMessage parses From / To / Subject', () => {
  const enc = (s) =>
    Buffer.from(s).toString('base64')
      .replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
  const out = normalizeMessage({
    id: 'msg1',
    threadId: 'thr1',
    internalDate: '1714150800000',
    labelIds: ['INBOX'],
    sizeEstimate: 1024,
    payload: {
      headers: [
        { name: 'From', value: '"Adam Moore" <adam@example.com>' },
        { name: 'To', value: 'raphaeljafri@gmail.com' },
        { name: 'Subject', value: 'Hello' },
      ],
      mimeType: 'text/plain',
      body: { data: enc('hi') },
    },
  });
  equal(out.message_id, 'msg1');
  equal(out.thread_id, 'thr1');
  equal(out.from_email, 'adam@example.com');
  equal(out.from_name, 'Adam Moore');
  deepEqual(out.to_emails, ['raphaeljafri@gmail.com']);
  equal(out.subject, 'Hello');
  equal(out.body_plain, 'hi');
  equal(out.labels[0], 'INBOX');
});

test('classifyThread: not marketing → keep', () => {
  const filters = { marketing: { skip: [], keep: [], newsletter: [] } };
  const messages = [
    { from_email: 'adam@example.com', is_from_user: false, labels: ['INBOX'] },
    { from_email: 'raphaeljafri@gmail.com', is_from_user: true, labels: ['INBOX', 'SENT'] },
  ];
  const out = classifyThread(messages, filters);
  equal(out.disposition, 'keep');
});

test('classifyThread: promotional + no list → unclassified', () => {
  const filters = { marketing: { skip: [], keep: [], newsletter: [] } };
  const messages = [
    { from_email: 'spam@example.com', is_from_user: false, labels: ['INBOX', 'CATEGORY_PROMOTIONS'] },
    { from_email: 'spam@example.com', is_from_user: false, labels: ['INBOX', 'CATEGORY_PROMOTIONS'] },
  ];
  const out = classifyThread(messages, filters);
  equal(out.disposition, 'unclassified');
  equal(out.sender, 'spam@example.com');
});

test('classifyThread: explicit skip wins', () => {
  const filters = {
    marketing: { skip: ['promo@example.com'], keep: [], newsletter: [] },
  };
  const messages = [
    { from_email: 'promo@example.com', is_from_user: false, labels: ['CATEGORY_PROMOTIONS'] },
  ];
  const out = classifyThread(messages, filters);
  equal(out.disposition, 'skip');
});

test('isMarketingCandidate: 50% threshold', () => {
  const m = (labels) => ({ labels });
  ok(!isMarketingCandidate([m([]), m([])]));
  ok(isMarketingCandidate([m(['CATEGORY_PROMOTIONS']), m(['CATEGORY_PROMOTIONS'])]));
  ok(isMarketingCandidate([m(['CATEGORY_PROMOTIONS']), m([])]));
  ok(!isMarketingCandidate([m([]), m([]), m(['CATEGORY_PROMOTIONS'])]));
});

test('readSyncState returns null on a fresh raw.sqlite (no sync run)', () => {
  // openRaw creates the table; readSyncState reads it. Empty table → null.
  const before = openRaw();
  before.close();
  const state = readSyncState();
  equal(state, null);
});

test('readSyncState reflects a written sync_state row', () => {
  const db = openRaw();
  db.prepare(
    `INSERT INTO sync_state (id, last_history_id, last_sync_at, oldest_synced_date)
     VALUES (1, '12345', '2026-04-27T15:00:00Z', '2026-03-28T00:00:00Z')
     ON CONFLICT(id) DO UPDATE SET
       last_history_id = excluded.last_history_id,
       last_sync_at = excluded.last_sync_at,
       oldest_synced_date = excluded.oldest_synced_date`
  ).run();
  db.close();
  const state = readSyncState();
  ok(state);
  equal(state.last_history_id, '12345');
  equal(state.last_sync_at, '2026-04-27T15:00:00Z');
  equal(state.oldest_synced_date, '2026-03-28T00:00:00Z');
});
