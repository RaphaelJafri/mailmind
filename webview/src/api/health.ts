// Typed clients for the two sidecar HTTP APIs.

export interface IngesterHealth {
  status: "ok";
  sidecar: "mailmind-ingester";
  node_version: string;
  data_dir: string;
  raw_db_exists: boolean;
  derived_db_exists: boolean;
  gmail_tokens_present: boolean;
  started_at: string;
}

export interface AgentsHealth {
  status: "ok";
  sidecar: "mailmind-agents";
  python_version: string;
  data_dir: string;
  started_at: string;
  gemini: {
    status: "ok" | "error" | "skipped" | "unexpected_response";
    model: string;
    region: string;
    project: string | null;
    auth_mode: "api_key" | "service_account" | "adc";
    auth_detail: string;
    latency_ms: number | null;
    stubbed: boolean;
    error: string | null;
  };
}

const INGESTER_URL = "http://127.0.0.1:8766";
const AGENTS_URL = "http://127.0.0.1:8765";

async function fetchJson<T>(url: string, signal?: AbortSignal): Promise<T> {
  const res = await fetch(url, { signal });
  if (!res.ok) {
    throw new Error(`${url} returned ${res.status}`);
  }
  return (await res.json()) as T;
}

export function fetchIngesterHealth(signal?: AbortSignal) {
  return fetchJson<IngesterHealth>(`${INGESTER_URL}/health`, signal);
}

export function fetchAgentsHealth(signal?: AbortSignal) {
  return fetchJson<AgentsHealth>(`${AGENTS_URL}/health`, signal);
}
