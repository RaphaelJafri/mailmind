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

async function patchJson<T>(url: string, body: unknown, signal?: AbortSignal): Promise<T> {
  const res = await fetch(url, {
    method: "PATCH",
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

// ----- Sync -----

export interface SyncStateRow {
  last_sync_at: string | null;
  last_history_id: string | null;
  oldest_synced_date: string | null;
  tokens_present: boolean;
  raw_db_present: boolean;
}

export interface SyncRunResult {
  ok: true;
  mode: "initial" | "incremental";
  account: string;
  started_at: string;
  finished_at: string;
  duration_ms: number;
  summary: {
    newMessages: number;
    newThreads: number;
    newContacts: number;
    failures: Array<{ id: string; error: string }>;
    classification?: {
      counts: { keep: number; skip: number; newsletter: number; unclassified: number };
      changed: number;
    };
  };
  report_path: string;
  sync_state: {
    last_sync_at: string;
    last_history_id: string | null;
    oldest_synced_date: string | null;
  };
}

export function getSyncState(signal?: AbortSignal) {
  return getJson<SyncStateRow>(`${INGESTER_URL}/sync_state`, signal);
}

export function syncNow(opts: { days?: number; full?: boolean } = {}) {
  return postJson<SyncRunResult>(`${INGESTER_URL}/sync`, opts);
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

export interface ThreadFactsRow {
  thread_id: string;
  content_hash: string;
  extracted_at: string;
  model_version: string;
  confidence: string;
  facts: {
    summary?: string;
    participants?: string[];
    commitments_by_user?: Array<{ description: string; due_date?: string | null; source_message_ids: string[] }>;
    commitments_by_others?: Array<{ description: string; due_date?: string | null; source_message_ids: string[] }>;
    open_questions?: Array<{ question?: string; description?: string; source_message_ids: string[] }>;
    sentiment?: string;
    topic_tags?: string[];
  };
}

export function getThreadFacts(threadId: string, signal?: AbortSignal) {
  return getJson<ThreadFactsRow>(`${AGENTS_URL}/thread_facts/${threadId}`, signal);
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

// ----- Query agent (P3) -----

export interface QueryToolDef {
  name: string;
  description: string;
  inputSchema: Record<string, unknown>;
}

export function listQueryTools(signal?: AbortSignal) {
  return getJson<{ tools: QueryToolDef[]; count: number }>(
    `${AGENTS_URL}/query/tools`,
    signal,
  );
}

export type QueryEvent =
  | { kind: "start"; question: string; model: string; started_at: string }
  | { kind: "thought"; step: number; text: string }
  | { kind: "tool_call"; step: number; tool: string; args: Record<string, unknown> }
  | { kind: "tool_result"; step: number; tool: string; result_count: number | null; result: unknown }
  | { kind: "answer"; answer: string; truncated: boolean; reason?: string }
  | { kind: "done"; tool_calls: number; wall_ms: number; input_tokens: number; output_tokens: number; cost_usd: number; stubbed: boolean; truncated: boolean; reason: string | null }
  | { kind: "error"; error: string };

// ----- Drafts (P4a) -----

export interface DraftRow {
  id: string;
  thread_id: string;
  in_reply_to_message_id: string | null;
  to_emails: string[];
  cc_emails: string[];
  bcc_emails: string[];
  subject: string;
  body: string;
  draft_hash: string;
  rationale: string;
  cited_facts: Array<{ fact_id: string; source_message_ids: string[] }>;
  confidence: "high" | "medium" | "low";
  intent: string | null;
  created_at: string;
  updated_at: string;
  status: "pending" | "approved" | "saved_as_draft" | "sent" | "rejected" | "expired";
  approval_id: string | null;
  gmail_draft_id: string | null;
  gmail_message_id: string | null;
  model_version: string;
  agent_run_id: string;
}

export interface DraftGenerateResponse {
  draft_id: string;
  thread_id: string;
  intent: string;
  draft: {
    thread_id: string;
    in_reply_to_message_id: string | null;
    to_emails: string[];
    cc_emails: string[];
    bcc_emails: string[];
    subject: string;
    body: string;
    rationale: string;
    cited_facts: Array<{ fact_id: string; source_message_ids: string[] }>;
    confidence: "high" | "medium" | "low";
  };
  draft_hash: string;
  stubbed: boolean;
  input_tokens: number;
  output_tokens: number;
  latency_ms: number;
  agent_run_id: string;
}

export function generateDraft(thread_id: string, intent: string) {
  return postJson<DraftGenerateResponse>(`${AGENTS_URL}/draft/generate`, {
    thread_id,
    intent,
  });
}

export function listDrafts(status?: DraftRow["status"], signal?: AbortSignal) {
  const q = status ? `?status=${encodeURIComponent(status)}` : "";
  return getJson<{ drafts: DraftRow[]; count: number }>(
    `${AGENTS_URL}/drafts${q}`,
    signal,
  );
}

export function getDraft(draftId: string, signal?: AbortSignal) {
  return getJson<DraftRow>(`${AGENTS_URL}/drafts/${draftId}`, signal);
}

export function editDraft(
  draftId: string,
  edits: {
    subject?: string;
    body?: string;
    to_emails?: string[];
    cc_emails?: string[];
    bcc_emails?: string[];
  },
) {
  return patchJson<DraftRow>(`${AGENTS_URL}/drafts/${draftId}`, edits);
}

export interface ApprovalResponse {
  approval_id: string;
  draft_id: string;
  action: "save_as_draft" | "send";
  approved_at: string;
  expires_at: string;
  undo_window_seconds: number;
}

export function approveDraft(
  draftId: string,
  opts: { action?: "save_as_draft" | "send"; undo_window_seconds?: number } = {},
) {
  return postJson<ApprovalResponse>(`${AGENTS_URL}/drafts/${draftId}/approve`, {
    action: opts.action ?? "save_as_draft",
    undo_window_seconds: opts.undo_window_seconds,
  });
}

export function cancelApproval(draftId: string, approval_id: string, reason?: string) {
  return postJson<{ approval_id: string; draft_id: string; cancelled_at: string; reason: string | null }>(
    `${AGENTS_URL}/drafts/${draftId}/cancel`,
    { approval_id, reason: reason ?? null },
  );
}

export function executeSaveAsDraft(draftId: string, approval_id: string) {
  return postJson<{
    approval_id: string;
    draft_id: string;
    action: "save_as_draft";
    executed_at: string;
    gmail_draft_id: string | null;
    gmail_message_id: string | null;
    next_status: "saved_as_draft";
  }>(`${AGENTS_URL}/drafts/${draftId}/save_as_gmail_draft`, { approval_id });
}

export function executeSend(draftId: string, approval_id: string) {
  return postJson<{
    approval_id: string;
    draft_id: string;
    action: "send";
    executed_at: string;
    gmail_draft_id: string | null;
    gmail_message_id: string | null;
    next_status: "sent";
  }>(`${AGENTS_URL}/drafts/${draftId}/send`, { approval_id });
}

export function rejectDraft(draftId: string, reason?: string) {
  return postJson<{ draft_id: string; rejected_at: string; reason: string | null }>(
    `${AGENTS_URL}/drafts/${draftId}/reject`,
    { reason: reason ?? null },
  );
}

export interface AuditEvent {
  id: string;
  event_at: string;
  event_type: string;
  draft_id: string | null;
  approval_id: string | null;
  draft_hash: string | null;
  approval_hash: string | null;
  gmail_message_id: string | null;
  gmail_draft_id: string | null;
  payload_json: string;
  prev_id: string | null;
  prev_hash: string | null;
}

export function listAuditLog(opts: { limit?: number; event_type?: string } = {}, signal?: AbortSignal) {
  const qp = new URLSearchParams();
  if (opts.limit) qp.set("limit", String(opts.limit));
  if (opts.event_type) qp.set("event_type", opts.event_type);
  const q = qp.toString();
  return getJson<{
    events: AuditEvent[];
    count: number;
    chain_ok: boolean;
    chain_total: number;
    broken_at: string | null;
  }>(`${AGENTS_URL}/audit_log${q ? "?" + q : ""}`, signal);
}

export interface PermissionsRow {
  "gmail.compose": boolean;
  "gmail.send": boolean;
  configured: boolean;
}

export function getPermissions(signal?: AbortSignal) {
  return getJson<PermissionsRow>(`${AGENTS_URL}/permissions`, signal);
}

export function setPermissions(opts: { gmail_compose?: boolean; gmail_send?: boolean }) {
  return postJson<PermissionsRow>(`${AGENTS_URL}/permissions`, opts);
}

// ----- Observability (P5a) -----

export interface DailySummary {
  day: string;
  total_usd: number;
  cap_usd: number;
  soft_warn_usd: number;
  hard_stop_usd: number;
  pct_of_cap: number;
  bucket: "ok" | "warn" | "stop";
  per_agent: Array<{ agent_name: string; total_usd: number; runs: number }>;
}

export interface ErrorTaxonomy {
  window_days: number;
  total: number;
  by_status: Record<string, number>;
  schema_fail_rate: number;
  retry_rate: number;
}

export interface LatencyByAgent {
  agent_name: string;
  p50_ms: number;
  p95_ms: number;
  runs: number;
}

export interface ObservabilitySummary {
  today: DailySummary;
  errors: ErrorTaxonomy;
  latency_by_agent: LatencyByAgent[];
}

export interface CostTrajectory {
  days: string[];
  agents: string[];
  series: Record<string, number[]>;
  totals: number[];
}

export interface Anomaly {
  id: string;
  agent_name: string;
  started_at: string;
  latency_ms: number | null;
  cost_usd: number | null;
  result_status: string;
  reasons: string[];
}

export interface BudgetConfig {
  pricing_usd_per_million: Record<string, { input: number; output: number }>;
  per_task_usd: Record<string, number>;
  per_day_usd: { total: number; soft_warn_pct: number; hard_stop_pct: number };
  anomalies: { latency_outlier_x_p95: number; cost_outlier_x_p95: number; rolling_window_days: number };
}

export interface LogEvent {
  ts?: string;
  level?: string;
  event?: string;
  agent_name?: string;
  run_id?: string;
  task_id?: string | null;
  result_status?: string;
  cost_usd?: number;
  latency_ms?: number;
  [k: string]: unknown;
}

export function getObservabilitySummary(signal?: AbortSignal) {
  return getJson<ObservabilitySummary>(`${AGENTS_URL}/observability/summary`, signal);
}

export function getCostTrajectory(days = 7, signal?: AbortSignal) {
  return getJson<CostTrajectory>(`${AGENTS_URL}/observability/cost_trajectory?days=${days}`, signal);
}

export function getAnomalies(limit = 50, signal?: AbortSignal) {
  return getJson<{ anomalies: Anomaly[] }>(`${AGENTS_URL}/observability/anomalies?limit=${limit}`, signal);
}

export function getBudgetConfig(signal?: AbortSignal) {
  return getJson<BudgetConfig>(`${AGENTS_URL}/observability/budget`, signal);
}

export function getLogTail(limit = 200, signal?: AbortSignal) {
  return getJson<{ events: LogEvent[]; count_requested: number }>(
    `${AGENTS_URL}/observability/log_tail?limit=${limit}`,
    signal,
  );
}

// ----- Labels + Eval (P5b) -----

export type LabelKind = "thread" | "rollup" | "draft";

export interface LabelRow {
  id: string;
  kind: LabelKind;
  target_id: string;
  expected: Record<string, unknown>;
  labeled_at: string;
  labeled_by: string;
  version: number;
  notes?: string;
  extra?: Record<string, unknown>;
}

export interface LabelCounts {
  thread: number;
  rollup: number;
  draft: number;
  total: number;
}

export function listLabels(kind?: LabelKind, signal?: AbortSignal) {
  const q = kind ? `?kind=${kind}` : "";
  return getJson<{ labels: LabelRow[]; count: number; counts_by_kind: LabelCounts }>(
    `${AGENTS_URL}/labels${q}`,
    signal,
  );
}

export function addLabel(req: {
  kind: LabelKind;
  target_id: string;
  expected: Record<string, unknown>;
  notes?: string;
  extra?: Record<string, unknown>;
}) {
  return postJson<LabelRow>(`${AGENTS_URL}/labels`, req);
}

export function deleteLabel(label_id: string) {
  // Re-using fetch directly since postJson is method-fixed.
  return fetch(`${AGENTS_URL}/labels/${label_id}`, { method: "DELETE" }).then(async (res) => {
    if (!res.ok) {
      const t = await res.text().catch(() => "");
      throw new Error(`/labels/${label_id} → ${res.status}: ${t}`);
    }
    return (await res.json()) as { deleted: boolean; id: string };
  });
}

export interface EvalRunResult {
  run_id: string | null;
  ran_at?: string;
  ok: boolean;
  reason?: string;
  label_counts: LabelCounts;
  metrics: Record<string, Record<string, number | null>>;
  per_label: Array<{
    label_id: string;
    kind: LabelKind;
    target_id: string;
    ok: boolean;
    error?: string;
    stubbed?: boolean;
    scores?: Record<string, number | boolean | null>;
    judge_rationale?: string;
  }>;
  baseline_present: boolean;
  regressions: Array<{ path: string; current: number; baseline: number; delta_pct: number }>;
  judge_prompt_version?: string;
}

export function runEval(dry_run = false) {
  return postJson<EvalRunResult>(`${AGENTS_URL}/eval/run`, { dry_run });
}

export function listEvalResults(limit = 20, signal?: AbortSignal) {
  return getJson<{ results: EvalRunResult[]; count: number }>(
    `${AGENTS_URL}/eval/results?limit=${limit}`,
    signal,
  );
}

export function getEvalBaseline(signal?: AbortSignal) {
  return getJson<{ baseline: EvalRunResult | null; frozen: boolean }>(
    `${AGENTS_URL}/eval/baseline`,
    signal,
  );
}

export function freezeEvalBaseline() {
  return postJson<{ frozen_run_id: string; ran_at: string }>(`${AGENTS_URL}/eval/baseline/freeze`, {});
}

/**
 * Open an SSE-style fetch against /query and call onEvent for each line of
 * `data:` JSON. Returns an AbortController so the caller can cancel.
 *
 * fetch+streams instead of EventSource because EventSource is GET-only and
 * we want POST with a body (the question).
 */
export function streamQuery(
  question: string,
  onEvent: (ev: QueryEvent) => void,
): { abort: () => void; done: Promise<void> } {
  const ctrl = new AbortController();
  const done = (async () => {
    const res = await fetch(`${AGENTS_URL}/query`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question }),
      signal: ctrl.signal,
    });
    if (!res.ok || !res.body) {
      const text = await res.text().catch(() => "");
      throw new Error(`/query → ${res.status}: ${text}`);
    }
    const reader = res.body.getReader();
    const decoder = new TextDecoder("utf-8");
    let buffer = "";
    while (true) {
      const { value, done: rd } = await reader.read();
      if (rd) break;
      buffer += decoder.decode(value, { stream: true });
      // SSE event boundary is a blank line.
      let idx;
      while ((idx = buffer.indexOf("\n\n")) >= 0) {
        const frame = buffer.slice(0, idx);
        buffer = buffer.slice(idx + 2);
        const dataLines = frame
          .split("\n")
          .filter((l) => l.startsWith("data:"))
          .map((l) => l.slice(5).trim());
        if (!dataLines.length) continue;
        try {
          const ev = JSON.parse(dataLines.join("\n")) as QueryEvent;
          onEvent(ev);
        } catch {
          // partial / malformed frame — skip
        }
      }
    }
  })();
  return { abort: () => ctrl.abort(), done };
}
