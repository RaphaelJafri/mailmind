// Marketing-mail filters.
//
// Deterministic classifier. Given a thread's participants, Gmail labels, and
// the user's filter config, returns a bucket:
//
//   'skip'         — user has listed this sender as skip. Extract drops.
//   'keep'         — explicitly kept, OR thread is not marketing.
//   'newsletter'   — extract, but rollup treats as low-relational-weight.
//   'unclassified' — promotional thread, sender on no list. Blocks extraction
//                    until the user triages via the Triage Queue tab.
//
// A thread is a "marketing candidate" if ≥50% of its messages have Gmail's
// CATEGORY_PROMOTIONS label.

import { existsSync, readFileSync, writeFileSync } from 'node:fs';
import { parse as parseYaml, stringify as stringifyYaml } from 'yaml';
import { filtersPath } from './paths.mjs';

const PROMO_LABEL = 'CATEGORY_PROMOTIONS';
const PROMO_THRESHOLD = 0.5;
const BUCKETS = ['skip', 'keep', 'newsletter'];

function emptyConfig() {
  return { marketing: { skip: [], keep: [], newsletter: [] } };
}

function normalizeEmail(s) {
  return String(s || '').trim().toLowerCase();
}

function toSet(list) {
  return new Set((list || []).map(normalizeEmail).filter(Boolean));
}

export function loadFilters(path = filtersPath()) {
  if (!existsSync(path)) return { path, ...emptyConfig() };
  const raw = readFileSync(path, 'utf8');
  const parsed = raw.trim() ? (parseYaml(raw) || {}) : {};
  const m = parsed.marketing || {};
  return {
    path,
    marketing: {
      skip: Array.isArray(m.skip) ? m.skip : [],
      keep: Array.isArray(m.keep) ? m.keep : [],
      newsletter: Array.isArray(m.newsletter) ? m.newsletter : [],
    },
  };
}

export function saveFilters(filters, path = filtersPath()) {
  const clean = {
    marketing: {
      skip: dedupSorted(filters.marketing?.skip),
      keep: dedupSorted(filters.marketing?.keep),
      newsletter: dedupSorted(filters.marketing?.newsletter),
    },
  };
  writeFileSync(path, stringifyYaml(clean));
}

function dedupSorted(list) {
  const s = new Set((list || []).map(normalizeEmail).filter(Boolean));
  return Array.from(s).sort();
}

export function isMarketingCandidate(messages) {
  if (!Array.isArray(messages) || messages.length === 0) return false;
  let promo = 0;
  for (const m of messages) {
    const labels = Array.isArray(m.labels) ? m.labels : [];
    if (labels.includes(PROMO_LABEL)) promo++;
  }
  return promo / messages.length >= PROMO_THRESHOLD;
}

export function bucketForSender(senderEmail, filters) {
  const e = normalizeEmail(senderEmail);
  if (!e) return null;
  const m = filters.marketing || {};
  for (const b of BUCKETS) {
    if (toSet(m[b]).has(e)) return b;
  }
  return null;
}

export function primarySender(messages) {
  const counts = new Map();
  for (const m of messages || []) {
    if (m.is_from_user) continue;
    const e = normalizeEmail(m.from_email);
    if (!e) continue;
    counts.set(e, (counts.get(e) || 0) + 1);
  }
  if (counts.size === 0) return null;
  let best = null;
  let bestN = -1;
  for (const [e, n] of counts) {
    if (n > bestN) {
      bestN = n;
      best = e;
    }
  }
  return best;
}

export function classifyThread(messages, filters) {
  const sender = primarySender(messages);
  const isMarketing = isMarketingCandidate(messages);
  const explicit = sender ? bucketForSender(sender, filters) : null;

  if (explicit === 'skip') {
    return { disposition: 'skip', reason: 'sender on skip list', sender };
  }
  if (explicit === 'newsletter') {
    return { disposition: 'newsletter', reason: 'sender on newsletter list', sender };
  }
  if (explicit === 'keep') {
    return { disposition: 'keep', reason: 'sender on keep list', sender };
  }
  if (!isMarketing) {
    return { disposition: 'keep', reason: 'not marketing', sender };
  }
  return {
    disposition: 'unclassified',
    reason: 'promotional thread, sender not yet triaged',
    sender,
  };
}

export function reclassifyAll(rawDb, filters) {
  const threads = rawDb.prepare(`SELECT thread_id FROM threads`).all();
  const getMessages = rawDb.prepare(
    `SELECT from_email, is_from_user, labels
       FROM messages
      WHERE thread_id = ?`
  );
  const existing = new Map(
    rawDb
      .prepare(`SELECT thread_id, disposition, sender, reason FROM thread_dispositions`)
      .all()
      .map((r) => [r.thread_id, r])
  );
  const upsert = rawDb.prepare(
    `INSERT INTO thread_dispositions
       (thread_id, disposition, sender, reason, classified_at)
     VALUES (?, ?, ?, ?, ?)
     ON CONFLICT(thread_id) DO UPDATE SET
       disposition = excluded.disposition,
       sender = excluded.sender,
       reason = excluded.reason,
       classified_at = excluded.classified_at`
  );

  const counts = { keep: 0, skip: 0, newsletter: 0, unclassified: 0 };
  let changed = 0;
  let unchanged = 0;
  const now = new Date().toISOString();

  const run = rawDb.transaction(() => {
    for (const t of threads) {
      const rows = getMessages.all(t.thread_id);
      const messages = rows.map((r) => ({
        from_email: r.from_email,
        is_from_user: !!r.is_from_user,
        labels: JSON.parse(r.labels || '[]'),
      }));
      const cls = classifyThread(messages, filters);
      counts[cls.disposition]++;
      const prev = existing.get(t.thread_id);
      const same =
        prev &&
        prev.disposition === cls.disposition &&
        (prev.sender || null) === (cls.sender || null) &&
        (prev.reason || null) === (cls.reason || null);
      if (same) {
        unchanged++;
        continue;
      }
      upsert.run(t.thread_id, cls.disposition, cls.sender, cls.reason, now);
      changed++;
    }
  });
  run();
  return { counts, changed, unchanged, total: threads.length };
}
