import { useCallback, useEffect, useState } from "react";
import { listAuditLog, type AuditEvent } from "../api/mailmind";

type Loadable<T> =
  | { state: "loading" }
  | { state: "ok"; data: T }
  | { state: "err"; error: string };

interface AuditPayload {
  events: AuditEvent[];
  count: number;
  chain_ok: boolean;
  chain_total: number;
  broken_at: string | null;
}

export default function Sent() {
  const [data, setData] = useState<Loadable<AuditPayload>>({ state: "loading" });

  const refresh = useCallback(async () => {
    setData({ state: "loading" });
    try {
      const res = await listAuditLog({ limit: 200 });
      setData({ state: "ok", data: res });
    } catch (err) {
      setData({ state: "err", error: (err as Error).message });
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  return (
    <>
      <section className="section">
        <h2 className="section__title">Sent · Audit log</h2>
        <p className="hint">
          Append-only chain of every write the agent service performed. Each
          row is hashed and points at the previous row's id + sha256, so any
          tamper breaks the chain on the next read. P4a only emits{" "}
          <code>save_as_draft</code>, <code>cancel</code>, <code>reject</code>,
          <code>approve</code>, and <code>auth_grant</code> events; sends land
          in P4b.
        </p>
        <div className="actions">
          <button className="button" onClick={refresh}>
            Refresh
          </button>
        </div>
      </section>

      {data.state === "ok" && (
        <section className="section">
          <ChainBadge ok={data.data.chain_ok} brokenAt={data.data.broken_at} total={data.data.chain_total} />
        </section>
      )}

      <section className="section">
        {data.state === "loading" && <p className="hint">loading…</p>}
        {data.state === "err" && <div className="error">{data.error}</div>}
        {data.state === "ok" && data.data.events.length === 0 && (
          <p className="hint">No audit events yet — approve a draft to populate this view.</p>
        )}
        {data.state === "ok" && data.data.events.length > 0 && (
          <table className="table">
            <thead>
              <tr>
                <th>when</th>
                <th>event</th>
                <th>draft</th>
                <th>approval</th>
                <th>gmail id</th>
                <th>chain</th>
              </tr>
            </thead>
            <tbody>
              {data.data.events.map((ev) => (
                <tr key={ev.id}>
                  <td className="cell--muted">{shortIso(ev.event_at)}</td>
                  <td>
                    <span className={`pill pill--${ev.event_type}`}>{ev.event_type}</span>
                  </td>
                  <td className="cell--mono">{shortId(ev.draft_id)}</td>
                  <td className="cell--mono">{shortId(ev.approval_id)}</td>
                  <td className="cell--mono">
                    {ev.gmail_message_id ?? ev.gmail_draft_id ?? ""}
                  </td>
                  <td className="cell--muted" title={ev.prev_hash ?? ""}>
                    {ev.prev_id ? "→ " + shortId(ev.prev_id) : "head"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </>
  );
}

function ChainBadge({ ok, brokenAt, total }: { ok: boolean; brokenAt: string | null; total: number }) {
  if (total === 0) {
    return <div className="hint">Chain is empty — no audit rows yet.</div>;
  }
  if (ok) {
    return (
      <div className="status status--ok">
        ✓ chain ok · {total} row{total === 1 ? "" : "s"}
      </div>
    );
  }
  return (
    <div className="status status--err">
      ✗ chain broken at {brokenAt} — external tamper or schema regression.
    </div>
  );
}

function shortIso(s: string): string {
  return s.replace("T", " ").slice(0, 19);
}

function shortId(s: string | null): string {
  if (!s) return "";
  return s.length > 10 ? s.slice(0, 10) + "…" : s;
}
