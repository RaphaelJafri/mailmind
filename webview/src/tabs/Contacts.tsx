import { useEffect, useState } from "react";
import {
  listContacts,
  listRollups,
  type ContactRollup,
  type ContactRow,
} from "../api/mailmind";

type Loadable<T> =
  | { state: "loading" }
  | { state: "ok"; data: T }
  | { state: "err"; error: string };

export default function Contacts() {
  const [contacts, setContacts] = useState<Loadable<ContactRow[]>>({ state: "loading" });
  const [rollups, setRollups] = useState<Loadable<ContactRollup[]>>({ state: "loading" });

  useEffect(() => {
    const ctrl = new AbortController();
    listContacts(200, ctrl.signal)
      .then((d) => setContacts({ state: "ok", data: d.contacts }))
      .catch((err: Error) => {
        if (err.name !== "AbortError") setContacts({ state: "err", error: err.message });
      });
    listRollups(100, ctrl.signal)
      .then((d) => setRollups({ state: "ok", data: d.rollups }))
      .catch((err: Error) => {
        if (err.name !== "AbortError") setRollups({ state: "err", error: err.message });
      });
    return () => ctrl.abort();
  }, []);

  const rollupsByEmail =
    rollups.state === "ok" ? new Map(rollups.data.map((r) => [r.contact_email, r])) : new Map();

  return (
    <>
      <section className="section">
        <h2 className="section__title">Contacts</h2>
        <p className="hint">
          Distinct senders/recipients. Rollups (tone, cadence, status) come from the
          Relationship agent. Run from the Follow-ups tab.
        </p>
      </section>

      {rollups.state === "ok" && rollups.data.length > 0 && (
        <section className="section">
          <h3 className="section__subtitle">Relationship rollups</h3>
          <div className="rollup-grid">
            {rollups.data.map((r) => (
              <article className="rollup-card" key={r.contact_email}>
                <header className="rollup-card__head">
                  <span className="cell--strong cell--mono">{r.contact_email}</span>
                  <span className={`pill pill--${r.status === "awaiting_them" ? "followup" : r.status === "awaiting_me" ? "urgent" : r.status === "active" ? "high" : "medium"}`}>
                    {r.status.replace("_", " ")}
                  </span>
                </header>
                <p className="rollup-card__summary">{r.relationship_summary}</p>
                <div className="rollup-card__meta">
                  <span className="pill">{r.tone}</span>
                  <span className="pill">{r.cadence}</span>
                  <span className={`pill pill--${r.confidence}`}>conf: {r.confidence}</span>
                </div>
                {r.tags.length > 0 && (
                  <div className="rollup-card__tags">
                    {r.tags.map((t) => (
                      <span className="pill" key={t}>
                        #{t}
                      </span>
                    ))}
                  </div>
                )}
              </article>
            ))}
          </div>
        </section>
      )}

      <section className="section">
        <h3 className="section__subtitle">All contacts</h3>
        {contacts.state === "loading" && <p className="hint">loading…</p>}
        {contacts.state === "err" && <div className="error">{contacts.error}</div>}
        {contacts.state === "ok" && contacts.data.length === 0 && (
          <p className="hint">No contacts yet — sync first.</p>
        )}
        {contacts.state === "ok" && contacts.data.length > 0 && (
          <table className="table">
            <thead>
              <tr>
                <th>email</th>
                <th>name</th>
                <th>rollup</th>
                <th>msgs</th>
                <th>last seen</th>
              </tr>
            </thead>
            <tbody>
              {contacts.data.map((c) => {
                const r = rollupsByEmail.get(c.email);
                return (
                  <tr key={c.email}>
                    <td className="cell--mono">{c.email}</td>
                    <td className="cell--muted">{c.display_name ?? ""}</td>
                    <td>
                      {r ? (
                        <span className="cell--muted">
                          {r.tone} · {r.cadence} · {r.status.replace("_", " ")}
                        </span>
                      ) : (
                        <span className="cell--muted">—</span>
                      )}
                    </td>
                    <td className="cell--num">{c.message_count}</td>
                    <td className="cell--muted">{c.last_seen?.slice(0, 10) ?? ""}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </section>
    </>
  );
}
