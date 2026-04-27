import { useEffect, useState } from "react";
import { listThreads, type ThreadRow } from "../api/mailmind";

type Loadable<T> =
  | { state: "loading" }
  | { state: "ok"; data: T }
  | { state: "err"; error: string };

export default function Inbox() {
  const [data, setData] = useState<Loadable<{ threads: ThreadRow[]; count: number; raw_db_present: boolean }>>({
    state: "loading",
  });
  const [filter, setFilter] = useState<string>("");

  useEffect(() => {
    const ctrl = new AbortController();
    setData({ state: "loading" });
    listThreads({ disposition: filter || undefined, limit: 200 }, ctrl.signal)
      .then((d) => setData({ state: "ok", data: d }))
      .catch((err: Error) => {
        if (err.name !== "AbortError") setData({ state: "err", error: err.message });
      });
    return () => ctrl.abort();
  }, [filter]);

  return (
    <>
      <section className="section">
        <h2 className="section__title">Inbox</h2>
        <p className="hint">
          Read-only view over <code>raw.sqlite</code>. Threads sync from the ingester
          (<code>:8766/threads</code>). Filter by disposition to see how triage classified them.
        </p>
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
          <p className="hint">No threads. Run sync first, or load fixtures via <code>node fixtures/load_fixtures.mjs</code>.</p>
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

function shortDate(s: string | null): string {
  if (!s) return "";
  return s.slice(0, 10);
}
