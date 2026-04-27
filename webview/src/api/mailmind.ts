// P1 API client. Two sidecars:
//   - ingester (:8766) — read views over raw.sqlite + v1 derived tables
//   - agents   (:8765) — runs and reads the v2 agent tables

const INGESTER_URL = "http://127.0.0.1:8766";
const AGENTS_URL = "http://127.0.0.1:8765";

async function getJson<T>(url: string, signal?: AbortSignal): Promise<T> {
  const res = await fetch(url, { signal });
  if (!res.ok) throw new Error(`${url} → ${res.status}`);
  return (await res.json()) as T;
}

async function postJson<T>(url: string, body: unknown, signal?: AbortSignal): Promise<T> {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body ?? {}),
    signal,
  });
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(`${url} → ${res.status}: ${text}`);
  }
  return (await res.json()) as T;
}

// ----- Inbox / Threads -----

export interface ThreadRow {
  thread_id: string;
  subject: string | null;
  message_count: number;
  last_message_date: string;
  disposition: string | null;
  sender: string | null;
}

export interface ContactRow {
  email: string;
  display_name: string | null;
  message_count: number;
  first_seen: string | null;
  last_seen: string | null;
  domain: string | null;
}

export function listThreads(
  opts: { disposition?: string; limit?: number } = {},
  signal?: AbortSignal,
) {
  const qp = new URLSearchParams();
  if (opts.disposition) qp.set("disposition", opts.disposition);
  if (opts.limit) qp.set("limit", String(opts.limit));
  const q = qp.toString();
  return getJson<{ threads: ThreadRow[]; count: number; raw_db_present: boolean }>(
    `${INGESTER_URL}/threads${q ? "?" + q : ""}`,
    signal,
  );
}

export function listContacts(limit = 200, signal?: AbortSignal) {
  return getJson<{ contacts: ContactRow[]; count: number }>(
    `${INGESTER_URL}/contacts?limit=${limit}`,
    signal,
  );
}

// ----- Triage -----

export interface TriageProposal {
  id: string;
  sender_email: string;
  proposed_disposition: "keep" | "skip" | "newsletter" | "unclear";
  confidence: "high" | "medium" | "low";
  rationale: string;
  cited_user_context_section: string | null;
  sample_subjects: string[];
  thread_count: number;
  proposed_at: string;
  reviewed_at: string | null;
  user_decision: "approve" | "reject" | null;
  applied_to_filters_at: string | null;
}

export function listTriageProposals(status: "pending" | "all" = "pending", signal?: AbortSignal) {
  return getJson<{ proposals: TriageProposal[]; count: number }>(
    `${AGENTS_URL}/triage/proposals?status=${status}`,
    signal,
  );
}

export function runTriage(opts: { min_thread_count?: number; limit?: number } = {}) {
  return postJson<{
    run_id: string;
    senders_considered: number;
    proposals_returned?: number;
    proposals_written: number;
    schema_invalid?: number;
    stubbed: boolean;
  }>(`${AGENTS_URL}/triage/run`, opts);
}

export function decideTriage(proposalId: string, decision: "approve" | "reject") {
  return postJson<TriageProposal>(`${AGENTS_URL}/triage/proposals/${proposalId}/decide`, {
    decision,
  });
}

// ----- Tags -----

export interface TagRow {
  message_id: string;
  tag_kind: "urgency" | "category" | "project";
  tag_value: string;
  confidence: string;
  rationale: string | null;
  tagged_at: string;
  model_version: string;
  agent_run_id: string | null;
}

export interface TagSummaryRow {
  tag_kind: string;
  tag_value: string;
  message_count: number;
}

export function listTags(opts: { kind?: string; value?: string; limit?: number } = {}, signal?: AbortSignal) {
  const qp = new URLSearchParams();
  if (opts.kind) qp.set("kind", opts.kind);
  if (opts.value) qp.set("value", opts.value);
  if (opts.limit) qp.set("limit", String(opts.limit));
  const q = qp.toString();
  return getJson<{ summary: TagSummaryRow[]; tags: TagRow[] }>(
    `${AGENTS_URL}/tags${q ? "?" + q : ""}`,
    signal,
  );
}
