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

// ----- Relationships / Followups -----

export interface ContactRollup {
  contact_email: string;
  relationship_summary: string;
  tone: "warm" | "neutral" | "transactional" | "strained";
  cadence: "daily" | "weekly" | "monthly" | "rare";
  status: "active" | "dormant" | "awaiting_them" | "awaiting_me";
  tags: string[];
  source_thread_ids: string[];
  confidence: "low" | "med" | "high";
  rolled_up_at: string;
  model_version: string;
}

export function listRollups(limit = 100, signal?: AbortSignal) {
  return getJson<{ rollups: ContactRollup[]; count: number }>(
    `${AGENTS_URL}/contact_rollups?limit=${limit}`,
    signal,
  );
}

export function runRelationship(opts: { contact_email?: string; force?: boolean } = {}) {
  return postJson<unknown>(`${AGENTS_URL}/relationship/run`, opts);
}

export interface NextStep {
  id: string;
  contact_email: string;
  description: string;
  priority: "low" | "med" | "high";
  due_date: string | null;
  status: string;
  source_thread_ids: string[];
  source_message_ids: string[];
  created_at: string;
  updated_at: string;
  resolved_at: string | null;
  resolution_reason: string | null;
  confidence: "low" | "med" | "high";
}

export function listNextSteps(status = "pending", signal?: AbortSignal) {
  return getJson<{ next_steps: NextStep[]; count: number }>(
    `${AGENTS_URL}/next_steps?status=${status}`,
    signal,
  );
}

export function dismissStep(stepId: string, user_note?: string) {
  return postJson<{ id: string; correction_id: string; dismissed_at: string }>(
    `${AGENTS_URL}/next_steps/${stepId}/dismiss`,
    { user_note: user_note ?? null },
  );
}

export function runReconcile(opts: { contact_email?: string; dry_run?: boolean } = {}) {
  return postJson<{
    run_id: string;
    contacts_scanned: number;
    totals: {
      matched: number;
      inserted: number;
      resolved: number;
      superseded: number;
      dropped_dismissed: number;
    };
    pending_total: number;
  }>(`${AGENTS_URL}/reconcile/run`, opts);
}

export interface FollowupEntry {
  contact_email: string;
  display_name: string | null;
  thread_id: string;
  subject: string;
  last_message_date: string;
  days_stale: number;
  urgency: "overdue" | "waiting" | "cold";
  expected_latency_days: number;
  category: string;
  rollup_status: string;
}

export interface FollowupReport {
  generated_at: string;
  metadata: {
    they_owe_count: number;
    they_owe_overdue: number;
    you_owe_count: number;
    you_owe_overdue: number;
    stale_pending_steps: number;
    thresholds_used: Record<string, number>;
    latencies_used: Record<string, number>;
  };
  they_owe_you: FollowupEntry[];
  you_owe_them: FollowupEntry[];
  stale_pending_next_steps: Array<{
    id: string;
    contact_email: string;
    description: string;
    priority: string;
    days_since_created: number;
    confidence: string;
  }>;
}

export function getFollowups(bucket?: "overdue" | "cold", signal?: AbortSignal) {
  const q = bucket ? `?bucket=${bucket}` : "";
  return getJson<FollowupReport>(`${AGENTS_URL}/followups${q}`, signal);
}
