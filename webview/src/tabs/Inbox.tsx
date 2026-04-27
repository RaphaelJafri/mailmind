import { useCallback, useEffect, useState } from "react";
import {
  type SyncRunResult,
  type SyncStateRow,
  type ThreadRow,
  getSyncState,
  listThreads,
  syncNow,
} from "../api/mailmind";

type Loadable<T> =
  | { state: "loading" }
  | { state: "ok"; data: T }
  | { state: "err"; error: string };

export default function Inbox() {
  const [data, setData] = useState<Loadable<{ threads: ThreadRow[]; count: number; raw_db_present: boolean }>>({
    state: "loading",
  });
  const [filter, setFilter] = useState<string>("");
  const [syncState, setSyncState] = useState<SyncStateRow | null>(null);
  const [syncing, setSyncing] = useState(false);
  const [lastResult, setLastResult] = useState<SyncRunResult | null>(null);
  const [syncError, setSyncError] = useState<string | null>(null);

  const refresh = useCallback(
    async (signal?: AbortSignal) => {
      setData({ state: "loading" });
      try {
        const [threads, state] = await Promise.all([
          listThreads({ disposition: filter || undefined, limit: 200 }, signal),
          getSyncState(signal),
        ]);
        setData({ state: "ok", data: threads });
        setSyncState(state);
      } catch (err) {
        if ((err as Error).name === "AbortError") return;
        setData({ state: "err", error: (err as Error).message });
      }
    },
    [filter],
  );

  useEffect(() => {
    const ctrl = new AbortController();
    refresh(ctrl.signal);
    return () => ctrl.abort();
  }, [refresh]);

  const onSync = async () => {
    if (syncing) return;
    setSyncing(true);
    setSyncError(null);
    setLastResult(null);
    try {
      const result = await syncNow();
      setLastResult(result);
      // Re-pull threads + state so the UI reflects the new data.
      await refresh();
    } catch (err) {
      const msg = (err as Error).message || String(err);
      // Try to extract a structured error from the body. The postJson
      // helper formats failures as `URL → STATUS: BODY`.
      const m = msg.match(/→ (\d+):\s*(.*)/);
      if (m) {
        try {
          const body = JSON.parse(m[2]);
          setSyncError(body.message || body.error || msg);
        } catch {
          setSyncError(msg);
        }
      } else {
        setSyncError(msg);
      }
    } finally {
      setSyncing(false);
    }
  };

  const canSync = syncState ? syncState.tokens_present : false;

  return (
    <>
      <section className="section">
        <h2 className="section__title">Inbox</h2>
        <p className="hint">
          Read-only view over <code>raw.sqlite</code>. Click <strong>Sync now</strong>{" "}
          to pull the latest threads from Gmail (incremental — only what changed
          since the last sync). Filter by disposition to see how triage classified
          them.
        </p>

        <div className="inbox__sync-row">
          <button
            className="button button--accent"
            onClick={onSync}
            disabled={syncing || !canSync}
            title={
              !canSync
                ? "OAuth tokens missing — run `cd ingester && npm run auth` first."
                : ""
            }
          >
            {syncing ? "Syncing…" : "Sync now"}
          </button>
          <SyncStatus state={syncState} syncing={syncing} lastResult={lastResult} />
        </div>

        {syncError && <div className="error">Sync failed: {syncError}</div>}
        {lastResult && (
          <div className="inbox__sync-result">
            <span className="cell--muted">just synced:</span>{" "}
            <strong>+{lastResult.summary.newMessages}</strong> messages,{" "}
            <strong>+{lastResult.summary.newThreads}</strong> threads,{" "}
            <strong>+{lastResult.summary.newContacts}</strong> contacts (
            {lastResult.mode}, {Math.round(lastResult.duration_ms / 100) / 10}s)
            {lastResult.summary.failures.length > 0 && (
              <span className="cell--bad">
                {" "}
                · {lastResult.summary.failures.length} failure
                {lastResult.summary.failures.length === 1 ? "" : "s"}
              </span>
            )}
          </div>
        )}

        <div className="actions">
          {[
            { id: "", label: "all" },
            { id: "keep", label: "keep" },
            { id: "newsletter", label: "newsletter" },
            { id: "skip", label: "skip" },
            { id: "unclassified", label: "unclassified" },
          ].map((f) => (
            <button
              key={f.id}
              className={`tab ${filter === f.id ? "tab--active" : ""}`}
              onClick={() => setFilter(f.id)}
            >
              {f.label}
            </button>
          ))}
        </div>
      </section>

      <section className="section">
        {data.state === "loading" && <p className="hint">loading…</p>}
        {data.state === "err" && <div className="error">{data.error}</div>}
        {data.state === "ok" && data.data.threads.length === 0 && (
          <p className="hint">
            No threads.{" "}
            {canSync
              ? "Click Sync now above to pull from Gmail, or load fixtures via "
              : "Run "}
            <code>node fixtures/load_fixtures.mjs</code>
            {canSync ? "." : " or grant OAuth tokens with `cd ingester && npm run auth`."}
          </p>
        )}
        {data.state === "ok" && data.data.threads.length > 0 && (
          <table className="table">
            <thead>
              <tr>
                <th>subject</th>
                <th>sender</th>
                <th>disp</th>
                <th>msgs</th>
                <th>last</th>
              </tr>
            </thead>
            <tbody>
              {data.data.threads.map((t) => (
                <tr key={t.thread_id}>
                  <td className="cell--strong">{t.subject ?? "(no subject)"}</td>
                  <td className="cell--muted">{t.sender ?? ""}</td>
                  <td>
                    <span className={`pill pill--${t.disposition ?? "unknown"}`}>
                      {t.disposition ?? "—"}
                    </span>
                  </td>
                  <td className="cell--num">{t.message_count}</td>
                  <td className="cell--muted">{shortDate(t.last_message_date)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </>
  );
}

function SyncStatus({
  state,
  syncing,
  lastResult,
}: {
  state: SyncStateRow | null;
  syncing: boolean;
  lastResult: SyncRunResult | null;
}) {
  if (syncing) {
    return <span className="inbox__sync-status">pulling from Gmail…</span>;
  }
  if (!state) {
    return null;
  }
  if (!state.tokens_present) {
    return (
      <span className="inbox__sync-status inbox__sync-status--warn">
        no Gmail tokens · run <code>cd ingester &amp;&amp; npm run auth</code>
      </span>
    );
  }
  const ts = lastResult?.sync_state?.last_sync_at ?? state.last_sync_at;
  if (!ts) {
    return (
      <span className="inbox__sync-status">
        never synced — Sync now to populate the inbox
      </span>
    );
  }
  return (
    <span className="inbox__sync-status" title={ts}>
      last synced {humanize(ts)} ago
    </span>
  );
}

function shortDate(s: string | null): string {
  if (!s) return "";
  return s.slice(0, 10);
}

/** "5 minutes ago", "2 hours ago", "yesterday", "Apr 14". */
function humanize(iso: string): string {
  const then = new Date(iso).getTime();
  if (!Number.isFinite(then)) return iso;
  const sec = Math.max(0, Math.round((Date.now() - then) / 1000));
  if (sec < 30) return "just now";
  if (sec < 60) return `${sec}s`;
  const min = Math.round(sec / 60);
  if (min < 60) return `${min} minute${min === 1 ? "" : "s"}`;
  const hr = Math.round(min / 60);
  if (hr < 24) return `${hr} hour${hr === 1 ? "" : "s"}`;
  const day = Math.round(hr / 24);
  if (day === 1) return "1 day";
  if (day < 7) return `${day} days`;
  // Older: just show the date.
  const d = new Date(iso);
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}
