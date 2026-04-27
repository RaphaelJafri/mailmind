#!/usr/bin/env node
// Pull Gmail messages into raw.sqlite.
//
// Two modes:
//   initial    — no sync_state or --full flag: fetch messages newer than
//                (now - --days N) via messages.list, then upsert.
//   incremental — sync_state.last_history_id exists: use history.list to
//                 collect only what's changed since last run.
//
// Idempotent: re-runs on unchanged inboxes pull 0 messages.

import 'dotenv/config';
import { writeFileSync } from 'node:fs';
import { resolve } from 'node:path';

import { openRaw } from './lib/db.mjs';
import {
  authorizedOAuthClient,
  makeGmailClient,
} from './lib/gmail-client.mjs';
import { threadContentHash } from './lib/thread-hash.mjs';
import { loadFilters, reclassifyAll } from './lib/filters.mjs';
import { reportsDir } from './lib/paths.mjs';

function parseArgs(argv) {
  const args = { days: 30, full: false };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === '--days') args.days = Number(argv[++i]);
    else if (a === '--full') args.full = true;
  }
  if (!Number.isFinite(args.days) || args.days <= 0) {
    throw new Error(`--days must be a positive number, got ${args.days}`);
  }
  return args;
}

function upsertMessage(db, m) {
  db.prepare(`
    INSERT INTO messages (
      message_id, thread_id, from_email, from_name,
      to_emails, cc_emails, subject, body_plain, body_html,
      internal_date, labels, is_from_user, raw_size_bytes, synced_at
    ) VALUES (
      @message_id, @thread_id, @from_email, @from_name,
      @to_emails, @cc_emails, @subject, @body_plain, @body_html,
      @internal_date, @labels, @is_from_user, @raw_size_bytes, CURRENT_TIMESTAMP
    )
    ON CONFLICT(message_id) DO UPDATE SET
      thread_id = excluded.thread_id,
      from_email = excluded.from_email,
      from_name = excluded.from_name,
      to_emails = excluded.to_emails,
      cc_emails = excluded.cc_emails,
      subject = excluded.subject,
      body_plain = excluded.body_plain,
      body_html = excluded.body_html,
      internal_date = excluded.internal_date,
      labels = excluded.labels,
      is_from_user = excluded.is_from_user,
      raw_size_bytes = excluded.raw_size_bytes,
      synced_at = CURRENT_TIMESTAMP
  `).run({
    message_id: m.message_id,
    thread_id: m.thread_id,
    from_email: m.from_email,
    from_name: m.from_name ?? null,
    to_emails: JSON.stringify(m.to_emails || []),
    cc_emails: JSON.stringify(m.cc_emails || []),
    subject: m.subject ?? null,
    body_plain: m.body_plain ?? null,
    body_html: m.body_html ?? null,
    internal_date: m.internal_date,
    labels: JSON.stringify(m.labels || []),
    is_from_user: m.is_from_user ? 1 : 0,
    raw_size_bytes: m.raw_size_bytes ?? null,
  });
}

function rebuildThread(db, threadId) {
  const rows = db
    .prepare(
      `SELECT message_id, from_email, to_emails, cc_emails, subject, internal_date
       FROM messages WHERE thread_id = ? ORDER BY internal_date ASC`
    )
    .all(threadId);
  if (rows.length === 0) {
    db.prepare(`DELETE FROM threads WHERE thread_id = ?`).run(threadId);
    return;
  }
  const participants = new Set();
  for (const r of rows) {
    if (r.from_email) participants.add(r.from_email);
    for (const e of JSON.parse(r.to_emails || '[]')) participants.add(e);
    for (const e of JSON.parse(r.cc_emails || '[]')) participants.add(e);
  }
  const subject = rows[0].subject;
  const first_message_date = rows[0].internal_date;
  const last_message_date = rows[rows.length - 1].internal_date;
  const ids = rows.map((r) => r.message_id);
  const content_hash = threadContentHash({
    messageIds: ids,
    lastMessageDate: last_message_date,
  });
  db.prepare(`
    INSERT INTO threads (
      thread_id, subject, message_count, first_message_date,
      last_message_date, participants, content_hash, last_synced_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
    ON CONFLICT(thread_id) DO UPDATE SET
      subject = excluded.subject,
      message_count = excluded.message_count,
      first_message_date = excluded.first_message_date,
      last_message_date = excluded.last_message_date,
      participants = excluded.participants,
      content_hash = excluded.content_hash,
      last_synced_at = CURRENT_TIMESTAMP
  `).run(
    threadId,
    subject,
    rows.length,
    first_message_date,
    last_message_date,
    JSON.stringify([...participants].sort()),
    content_hash
  );
}

function rebuildContact(db, email) {
  const agg = db
    .prepare(
      `SELECT
         MIN(internal_date) AS first_seen,
         MAX(internal_date) AS last_seen,
         COUNT(*) AS cnt,
         MAX(from_name) AS display_name
       FROM messages WHERE from_email = ?`
    )
    .get(email);
  if (!agg || agg.cnt === 0) {
    db.prepare(`DELETE FROM contacts WHERE email = ?`).run(email);
    return;
  }
  const domain = email.includes('@') ? email.split('@')[1] : null;
  db.prepare(`
    INSERT INTO contacts (email, display_name, first_seen, last_seen, message_count, domain)
    VALUES (?, ?, ?, ?, ?, ?)
    ON CONFLICT(email) DO UPDATE SET
      display_name = excluded.display_name,
      first_seen = excluded.first_seen,
      last_seen = excluded.last_seen,
      message_count = excluded.message_count,
      domain = excluded.domain
  `).run(email, agg.display_name, agg.first_seen, agg.last_seen, agg.cnt, domain);
}

function getSyncState(db) {
  return db.prepare(`SELECT * FROM sync_state WHERE id = 1`).get();
}

function writeSyncState(db, { historyId, windowStartIso }) {
  db.prepare(`
    INSERT INTO sync_state (id, last_history_id, last_sync_at, oldest_synced_date)
    VALUES (1, ?, CURRENT_TIMESTAMP, ?)
    ON CONFLICT(id) DO UPDATE SET
      last_history_id = excluded.last_history_id,
      last_sync_at = CURRENT_TIMESTAMP,
      oldest_synced_date = COALESCE(sync_state.oldest_synced_date, excluded.oldest_synced_date)
  `).run(String(historyId), windowStartIso);
}

async function fetchMessagesInBatches(client, ids, onProgress) {
  const results = [];
  const failures = [];
  let i = 0;
  for (const id of ids) {
    i++;
    try {
      const msg = await client.getMessage(id);
      results.push(msg);
    } catch (err) {
      failures.push({ id, error: err.message || String(err) });
    }
    if (onProgress && i % 25 === 0) onProgress(i, ids.length);
  }
  if (onProgress) onProgress(i, ids.length);
  return { results, failures };
}

function persistBatch(db, messages, userEmail) {
  const threadIdsTouched = new Set();
  const contactEmailsTouched = new Set();
  db.transaction(() => {
    for (const m of messages) {
      m.is_from_user =
        !!m.from_email && m.from_email.toLowerCase() === userEmail.toLowerCase();
      upsertMessage(db, m);
      threadIdsTouched.add(m.thread_id);
      if (m.from_email) contactEmailsTouched.add(m.from_email);
    }
    for (const tid of threadIdsTouched) rebuildThread(db, tid);
    for (const e of contactEmailsTouched) rebuildContact(db, e);
  })();
  return {
    threads: threadIdsTouched.size,
    contacts: contactEmailsTouched.size,
  };
}

async function initialSync({ db, client, profile, days }) {
  const windowStart = new Date(Date.now() - days * 24 * 60 * 60 * 1000);
  const windowStartIso = windowStart.toISOString();
  console.log(`Initial sync: fetching messages after ${windowStartIso.slice(0, 10)}...`);

  const ids = [];
  for await (const id of client.listMessageIdsAfter(windowStartIso)) {
    ids.push(id);
  }
  console.log(`Found ${ids.length} message IDs. Fetching bodies...`);
  if (ids.length === 0) {
    writeSyncState(db, { historyId: profile.historyId, windowStartIso });
    return { newMessages: 0, newThreads: 0, newContacts: 0, failures: [] };
  }

  const { results, failures } = await fetchMessagesInBatches(
    client,
    ids,
    (done, total) => process.stdout.write(`  ${done}/${total}\r`)
  );
  process.stdout.write('\n');

  const { threads, contacts } = persistBatch(db, results, profile.emailAddress);
  writeSyncState(db, { historyId: profile.historyId, windowStartIso });

  return {
    newMessages: results.length,
    newThreads: threads,
    newContacts: contacts,
    failures,
  };
}

async function incrementalSync({ db, client, profile, syncState }) {
  console.log(
    `Incremental sync: history events since historyId=${syncState.last_history_id}...`
  );

  const newIds = new Set();
  let needsFullRefresh = false;
  for await (const ev of client.listHistoryEvents(syncState.last_history_id)) {
    if (ev.needsFullRefresh) {
      needsFullRefresh = true;
      break;
    }
    for (const added of ev.messagesAdded || []) {
      if (added.message?.id) newIds.add(added.message.id);
    }
    for (const lbl of ev.labelsAdded || []) {
      if (lbl.message?.id) newIds.add(lbl.message.id);
    }
    for (const lbl of ev.labelsRemoved || []) {
      if (lbl.message?.id) newIds.add(lbl.message.id);
    }
  }

  if (needsFullRefresh) {
    console.log(`historyId too old. Falling back to full refresh.`);
    const daysSince = Math.max(
      1,
      Math.ceil(
        (Date.now() - new Date(syncState.oldest_synced_date).getTime()) /
          (24 * 60 * 60 * 1000)
      )
    );
    return initialSync({ db, client, profile, days: daysSince });
  }

  console.log(`Found ${newIds.size} changed message IDs.`);
  if (newIds.size === 0) {
    writeSyncState(db, {
      historyId: profile.historyId,
      windowStartIso: syncState.oldest_synced_date,
    });
    return { newMessages: 0, newThreads: 0, newContacts: 0, failures: [] };
  }

  const { results, failures } = await fetchMessagesInBatches(
    client,
    [...newIds],
    (done, total) => process.stdout.write(`  ${done}/${total}\r`)
  );
  process.stdout.write('\n');

  const { threads, contacts } = persistBatch(db, results, profile.emailAddress);
  writeSyncState(db, {
    historyId: profile.historyId,
    windowStartIso: syncState.oldest_synced_date,
  });

  return {
    newMessages: results.length,
    newThreads: threads,
    newContacts: contacts,
    failures,
  };
}

function writeReport({ startedAt, mode, summary, profile }) {
  const stamp = startedAt
    .toISOString()
    .replace(/[-:]/g, '')
    .replace(/\..+/, '');
  const path = resolve(reportsDir(), `sync-${stamp}.md`);
  const lines = [
    `# Sync report — ${startedAt.toISOString()}`,
    '',
    `- Account: ${profile.emailAddress}`,
    `- Mode: ${mode}`,
    `- Messages synced: ${summary.newMessages}`,
    `- Threads touched: ${summary.newThreads}`,
    `- Contacts touched: ${summary.newContacts}`,
    `- Failures: ${summary.failures.length}`,
    '',
  ];
  if (summary.classification) {
    const c = summary.classification.counts;
    lines.push(
      '## Classification',
      '',
      `- keep: ${c.keep}`,
      `- skip: ${c.skip}`,
      `- newsletter: ${c.newsletter}`,
      `- unclassified: ${c.unclassified}`,
      `- changed this run: ${summary.classification.changed}`,
      ''
    );
  }
  if (summary.failures.length) {
    lines.push('## Failures', '');
    for (const f of summary.failures.slice(0, 50)) {
      lines.push(`- ${f.id}: ${f.error}`);
    }
    if (summary.failures.length > 50) {
      lines.push(`- …and ${summary.failures.length - 50} more`);
    }
  }
  writeFileSync(path, lines.join('\n') + '\n');
  return path;
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const { GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET } = process.env;
  if (!GOOGLE_CLIENT_ID || !GOOGLE_CLIENT_SECRET) {
    console.error('GOOGLE_CLIENT_ID/GOOGLE_CLIENT_SECRET missing.');
    process.exit(3);
  }

  let oauth;
  try {
    oauth = authorizedOAuthClient({
      clientId: GOOGLE_CLIENT_ID,
      clientSecret: GOOGLE_CLIENT_SECRET,
    });
  } catch (err) {
    if (err.code === 'NO_TOKENS') {
      console.error(err.message);
      process.exit(2);
    }
    throw err;
  }

  const client = makeGmailClient(oauth);
  const profile = await client.getProfile();
  console.log(`Signed in as ${profile.emailAddress} (inbox: ${profile.messagesTotal} msgs)`);

  const db = openRaw();
  const startedAt = new Date();

  try {
    const state = args.full ? null : getSyncState(db);
    const mode = !state || args.full ? 'initial' : 'incremental';

    let summary;
    if (mode === 'initial') {
      summary = await initialSync({ db, client, profile, days: args.days });
    } else {
      summary = await incrementalSync({ db, client, profile, syncState: state });
    }

    const filters = loadFilters();
    const cls = reclassifyAll(db, filters);
    console.log(
      `  Classified: keep=${cls.counts.keep} skip=${cls.counts.skip} ` +
        `newsletter=${cls.counts.newsletter} unclassified=${cls.counts.unclassified} ` +
        `(${cls.changed} changed)`
    );
    summary.classification = cls;

    const reportPath = writeReport({ startedAt, mode, summary, profile });

    console.log(
      `\nDone. +${summary.newMessages} msg, +${summary.newThreads} threads, +${summary.newContacts} contacts.`
    );
    console.log(`  Report: ${reportPath}`);
  } finally {
    db.close();
  }
}

main().catch((err) => {
  console.error('sync failed:', err.message || err);
  if (err.stack && process.env.MAILMIND_DEBUG) console.error(err.stack);
  process.exit(1);
});
