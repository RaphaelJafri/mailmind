// Pure, deterministic thread content hash.
//
// Used by sync to compute threads.content_hash, and later by the extract agent
// to decide whether a thread's facts need re-extraction.
//
// Hash inputs: sorted message IDs + last message date (epoch ms). Any change
// to a thread's message set OR the latest message's date invalidates the hash.

import { createHash } from 'node:crypto';

export function threadContentHash({ messageIds, lastMessageDate }) {
  if (!Array.isArray(messageIds) || messageIds.length === 0) {
    throw new Error('threadContentHash: messageIds must be a non-empty array');
  }
  const sorted = [...messageIds].sort();
  const epoch = new Date(lastMessageDate).getTime();
  if (Number.isNaN(epoch)) {
    throw new Error(`threadContentHash: invalid lastMessageDate: ${lastMessageDate}`);
  }
  const payload = `${sorted.join('|')}::${epoch}`;
  return createHash('sha256').update(payload).digest('hex');
}
