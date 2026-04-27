import { useCallback, useEffect, useState } from "react";
import {
  dismissStep,
  getFollowups,
  listNextSteps,
  runReconcile,
  runRelationship,
  type FollowupEntry,
  type FollowupReport,
  type NextStep,
} from "../api/mailmind";

type Loadable<T> =
  | { state: "loading" }
  | { state: "ok"; data: T }
  | { state: "err"; error: string };

export default function Followups() {
  const [report, setReport] = useState<Loadable<FollowupReport>>({ state: "loading" });
  const [steps, setSteps] = useState<Loadable<NextStep[]>>({ state: "loading" });
  const [busy, setBusy] = useState(false);
  const [lastEvent, setLastEvent] = useState<string | null>(null);

  const refresh = useCallback(() => {
    const ctrl = new AbortController();
    setReport({ state: "loading" });
    setSteps({ state: "loading" });
    getFollowups(undefined, ctrl.signal)
      .then((d) => setReport({ state: "ok", data: d }))
      .catch((err: Error) => {
        if (err.name !== "AbortError") setReport({ state: "err", error: err.message });
      });
    listNextSteps("pending", ctrl.signal)
      .then((d) => setSteps({ state: "ok", data: d.next_steps }))
      .catch((err: Error) => {
        if (err.name !== "AbortError") setSteps({ state: "err", error: err.message });
      });
    return () => ctrl.abort();
  }, []);

  useEffect(() => {
    return refresh();
  }, [refresh]);

  const onRefreshAll = async () => {
    setBusy(true);
    try {
      const rel = await runRelationship({});
      const rec = await runReconcile({});
      setLastEvent(
        `rolled up ${(rel as { count?: number }).count ?? "?"} contacts · ` +
          `reconcile inserted=${rec.totals.inserted} matched=${rec.totals.matched} dropped=${rec.totals.dropped_dismissed}`,
      );
      refresh();
    } catch (err) {
      setLastEvent(`error: ${(err as Error).message}`);
    } finally {
      setBusy(false);
    }
  };

  const onDismiss = async (stepId: string) => {
    try {
      await dismissStep(stepId);
      refresh();
    } catch (err) {
      alert(`failed: ${(err as Error).message}`);
    }
  };

  return (
    <>
      <section className="section">
        <h2 className="section__title">Follow-ups</h2>
        <p className="hint">
          Deterministic cadence tracker. <em>They owe you</em> = you sent last and they
          haven't replied. <em>You owe them</em> = they sent last and you haven't
          replied. Overdue thresholds come from <code>user-context.md</code>.
        </p>
        <div className="actions">
          <button className="button" onClick={onRefreshAll} disabled={busy}>
            {busy ? "running…" : "Refresh rollups + reconcile"}
          </button>
          <button className="button button--secondary" onClick={refresh}>
            Reload
          </button>
          {lastEvent && <span className="hint">{lastEvent}</span>}
        </div>
      </section>

      {report.state === "ok" && (
        <section className="section">
          <div className="followup-summary">
            <div className="followup-summary__cell">
              <span className="cell--muted">they owe you</span>
              <strong>
                {report.data.metadata.they_owe_count}{" "}
                <span className="cell--muted">
                  ({report.data.metadata.they_owe_overdue} overdue)
                </span>
              </strong>
            </div>
            <div className="followup-summary__cell">
              <span className="cell--muted">you owe them</span>
              <strong>
                {report.data.metadata.you_owe_count}{" "}
                <span className="cell--muted">
                  ({report.data.metadata.you_owe_overdue} overdue)
                </span>
              </strong>
            </div>
            <div className="followup-summary__cell">
              <span className="cell--muted">stale steps</span>
              <strong>{report.data.metadata.stale_pending_steps}</strong>
            </div>
          </div>
        </section>
      )}

      {report.state === "err" && <div className="error">{report.error}</div>}

      {report.state === "ok" && (
        <>
          <FollowupSection title="You owe them (reply overdue)" entries={report.data.you_owe_them} />
          <FollowupSection title="They owe you (nudge candidates)" entries={report.data.they_owe_you} />
        </>
      )}

      <section className="section">
        <h3 className="section__subtitle">Pending next steps</h3>
        {steps.state === "loading" && <p className="hint">loading…</p>}
        {steps.state === "err" && <div className="error">{steps.error}</div>}
        {steps.state === "ok" && steps.data.length === 0 && (
          <p className="hint">No pending next steps. Run rollups + reconcile to seed.</p>
        )}
        {steps.state === "ok" && steps.data.length > 0 && (
          <table className="table">
            <thead>
              <tr>
                <th>contact</th>
                <th>step</th>
                <th>priority</th>
                <th>conf</th>
                <th>created</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {steps.data.map((s) => (
                <tr key={s.id}>
                  <td className="cell--mono">{s.contact_email}</td>
                  <td>{s.description}</td>
                  <td>
                    <span className={`pill pill--${s.priority === "high" ? "urgent" : s.priority === "med" ? "medium" : "low"}`}>
                      {s.priority}
                    </span>
                  </td>
                  <td>
                    <span className={`pill pill--${s.confidence}`}>{s.confidence}</span>
                  </td>
                  <td className="cell--muted">{s.created_at.slice(0, 10)}</td>
                  <td>
                    <button className="button button--secondary" onClick={() => onDismiss(s.id)}>
                      Dismiss
                    </button>
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

function FollowupSection({ title, entries }: { title: string; entries: FollowupEntry[] }) {
  return (
    <section className="section">
      <h3 className="section__subtitle">{title}</h3>
      {entries.length === 0 ? (
        <p className="hint">none</p>
      ) : (
        <table className="table">
          <thead>
            <tr>
              <th>urgency</th>
              <th>days</th>
              <th>contact</th>
              <th>subject</th>
              <th>category</th>
              <th>last reply</th>
            </tr>
          </thead>
          <tbody>
            {entries.map((e) => (
              <tr key={`${e.thread_id}::${e.contact_email}`}>
                <td>
                  <span className={`pill pill--${e.urgency}`}>{e.urgency}</span>
                </td>
                <td className="cell--num">{e.days_stale}d</td>
                <td className="cell--mono">{e.display_name ?? e.contact_email}</td>
                <td>{e.subject || <span className="cell--muted">(no subject)</span>}</td>
                <td className="cell--muted">{e.category}</td>
                <td className="cell--muted">{e.last_message_date.slice(0, 10)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
