import { useCallback, useEffect, useState } from "react";
import {
  decideTriage,
  listTriageProposals,
  runTriage,
  type TriageProposal,
} from "../api/mailmind";

type Loadable<T> =
  | { state: "loading" }
  | { state: "ok"; data: T }
  | { state: "err"; error: string };

export default function Triage() {
  const [data, setData] = useState<Loadable<TriageProposal[]>>({ state: "loading" });
  const [running, setRunning] = useState(false);
  const [lastRun, setLastRun] = useState<string | null>(null);

  const refresh = useCallback(() => {
    const ctrl = new AbortController();
    setData({ state: "loading" });
    listTriageProposals("pending", ctrl.signal)
      .then((d) => setData({ state: "ok", data: d.proposals }))
      .catch((err: Error) => {
        if (err.name !== "AbortError") setData({ state: "err", error: err.message });
      });
    return () => ctrl.abort();
  }, []);

  useEffect(() => {
    return refresh();
  }, [refresh]);

  const onRun = async () => {
    setRunning(true);
    try {
      const r = await runTriage({ min_thread_count: 1, limit: 30 });
      setLastRun(
        `run ${r.run_id.slice(0, 8)} · senders=${r.senders_considered} · written=${r.proposals_written}` +
          (r.stubbed ? " · stubbed" : ""),
      );
      refresh();
    } catch (err) {
      setLastRun(`error: ${(err as Error).message}`);
    } finally {
      setRunning(false);
    }
  };

  const onDecide = async (id: string, decision: "approve" | "reject") => {
    try {
      await decideTriage(id, decision);
      refresh();
    } catch (err) {
      alert(`failed: ${(err as Error).message}`);
    }
  };

  return (
    <>
      <section className="section">
        <h2 className="section__title">Triage queue</h2>
        <p className="hint">
          The Triage agent proposes <code>keep</code>/<code>skip</code>/<code>newsletter</code> for unclassified
          senders. Approve or reject each one. v1's interactive CLI, with reasoning.
        </p>
        <div className="actions">
          <button className="button" onClick={onRun} disabled={running}>
            {running ? "running…" : "Run triage"}
          </button>
          <button className="button" onClick={refresh}>
            Refresh
          </button>
          {lastRun && <span className="hint">{lastRun}</span>}
        </div>
      </section>

      <section className="section">
        {data.state === "loading" && <p className="hint">loading…</p>}
        {data.state === "err" && <div className="error">{data.error}</div>}
        {data.state === "ok" && data.data.length === 0 && (
          <p className="hint">No pending proposals. Run triage to generate some.</p>
        )}
        {data.state === "ok" && data.data.length > 0 && (
          <div className="proposals">
            {data.data.map((p) => (
              <div className="proposal" key={p.id}>
                <div className="proposal__head">
                  <span className="cell--strong">{p.sender_email}</span>
                  <span className={`pill pill--${p.proposed_disposition}`}>
                    {p.proposed_disposition}
                  </span>
                  <span className="pill">conf: {p.confidence}</span>
                  <span className="pill">{p.thread_count} thread{p.thread_count > 1 ? "s" : ""}</span>
                </div>
                <p className="proposal__rationale">{p.rationale}</p>
                {p.cited_user_context_section && (
                  <p className="hint">
                    cited: <em>{p.cited_user_context_section}</em>
                  </p>
                )}
                {p.sample_subjects.length > 0 && (
                  <ul className="proposal__samples">
                    {p.sample_subjects.slice(0, 3).map((s, i) => (
                      <li key={i}>{s}</li>
                    ))}
                  </ul>
                )}
                <div className="actions">
                  <button className="button button--primary" onClick={() => onDecide(p.id, "approve")}>
                    Approve
                  </button>
                  <button className="button" onClick={() => onDecide(p.id, "reject")}>
                    Reject
                  </button>
                </div>
              </div>
            ))}
          </div>
        )}
      </section>
    </>
  );
}
