#!/usr/bin/env node
// Seed `raw.sqlite` with fixture threads. Idempotent: deletes any prior
// fixture rows (id prefix 'fix-') before inserting.
//
// Usage:
//   MAILMIND_DATA_DIR=$DATA node fixtures/load_fixtures.mjs
//
// Reads from fixtures/threads/seed.json. Mirrors the schema in
// ingester/src/lib/db.mjs but does not require the ingester to be running.

import { readFileSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { openRaw } from '../ingester/src/lib/db.mjs';

const __dirname = dirname(fileURLToPath(import.meta.url));

function loadSeed() {
  const p = resolve(__dirname, 'threads', 'seed.json');
  return JSON.parse(readFileSync(p, 'utf8'));
}

function main() {
  const seed = loadSeed();
  const db = openRaw();

  db.exec("DELETE FROM messages WHERE message_id LIKE 'fix-%';");
  db.exec("DELETE FROM threads WHERE thread_id LIKE 'fix-%';");
  db.exec("DELETE FROM thread_dispositions WHERE thread_id LIKE 'fix-%';");
  db.exec(
    "DELETE FROM contacts WHERE email IN (" +
      "'morgan@northstar-talent.com','newsletter@stratechery.com'," +
      "'deals@example-retailer.com','raphaeljafri@gmail.com');",
  );

  const insertThread = db.prepare(`
    INSERT INTO threads (
      thread_id, subject, message_count, first_message_date,
      last_message_date, participants, content_hash
    ) VALUES (?, ?, ?, ?, ?, ?, ?)
  `);
  const insertMessage = db.prepare(`
    INSERT INTO messages (
      message_id, thread_id, from_email, from_name,
      to_emails, cc_emails, subject, body_plain,
      internal_date, is_from_user, raw_size_bytes
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
  `);
  const insertDisposition = db.prepare(`
    INSERT INTO thread_dispositions (
      thread_id, disposition, sender, reason, classified_at
    ) VALUES (?, ?, ?, 'fixture seed', ?)
  `);
  const upsertContact = db.prepare(`
    INSERT INTO contacts (email, display_name, first_seen, last_seen, message_count, domain)
      VALUES (?, ?, ?, ?, ?, ?)
    ON CONFLICT(email) DO UPDATE SET
      message_count = contacts.message_count + excluded.message_count,
      last_seen = MAX(contacts.last_seen, excluded.last_seen),
      first_seen = MIN(contacts.first_seen, excluded.first_seen)
  `);

  const now = new Date().toISOString();
  const tx = db.transaction((threads) => {
    for (const t of threads) {
      const participants = Array.from(
        new Set(t.messages.flatMap((m) => [m.from_email, ...m.to_emails, ...m.cc_emails]))
      );
      const dates = t.messages.map((m) => m.internal_date).sort();
      insertThread.run(
        t.thread_id,
        t.subject,
        t.messages.length,
        dates[0],
        dates[dates.length - 1],
        JSON.stringify(participants),
        `fixture-hash-${t.thread_id}`
      );
      for (const m of t.messages) {
        insertMessage.run(
          m.message_id,
          t.thread_id,
          m.from_email,
          m.from_name ?? null,
          JSON.stringify(m.to_emails),
          JSON.stringify(m.cc_emails),
          m.subject ?? t.subject,
          m.body_plain,
          m.internal_date,
          m.is_from_user ? 1 : 0,
          (m.body_plain || '').length
        );
      }
      insertDisposition.run(t.thread_id, t.disposition, t.sender, now);

      for (const m of t.messages) {
        const allEmails = new Set([m.from_email, ...m.to_emails, ...m.cc_emails]);
        for (const email of allEmails) {
          if (!email) continue;
          const domain = email.split('@')[1] ?? null;
          const displayName = email === m.from_email ? m.from_name ?? null : null;
          upsertContact.run(email, displayName, m.internal_date, m.internal_date, 1, domain);
        }
      }
    }
  });
  tx(seed.threads);

  console.log(`fixtures: loaded ${seed.threads.length} threads`);
  db.close();
}

main();
