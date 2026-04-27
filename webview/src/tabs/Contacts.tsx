import { useEffect, useState } from "react";
import { listContacts, type ContactRow } from "../api/mailmind";

type Loadable<T> =
  | { state: "loading" }
  | { state: "ok"; data: T }
  | { state: "err"; error: string };

export default function Contacts() {
  const [data, setData] = useState<Loadable<{ contacts: ContactRow[]; count: number }>>({
    state: "loading",
  });

  useEffect(() => {
    const ctrl = new AbortController();
    setData({ state: "loading" });
    listContacts(200, ctrl.signal)
      .then((d) => setData({ state: "ok", data: d }))
      .catch((err: Error) => {
        if (err.name !== "AbortError") setData({ state: "err", error: err.message });
      });
    return () => ctrl.abort();
  }, []);

  return (
    <>
      <section className="section">
        <h2 className="section__title">Contacts</h2>
        <p className="hint">
          Distinct senders/recipients. Populated by the ingester at sync time. The
          Relationship agent (P2) will overlay rollups, tone, and cadence.
        </p>
      </section>

      <section className="section">
        {data.state === "loading" && <p className="hint">loading…</p>}
        {data.state === "err" && <div className="error">{data.error}</div>}
        {data.state === "ok" && data.data.contacts.length === 0 && (
          <p className="hint">No contacts yet — sync first.</p>
        )}
        {data.state === "ok" && data.data.contacts.length > 0 && (
          <table className="table">
            <thead>
              <tr>
                <th>email</th>
                <th>name</th>
                <th>domain</th>
                <th>msgs</th>
                <th>last seen</th>
              </tr>
            </thead>
            <tbody>
              {data.data.contacts.map((c) => (
                <tr key={c.email}>
                  <td className="cell--mono">{c.email}</td>
                  <td className="cell--muted">{c.display_name ?? ""}</td>
                  <td className="cell--muted">{c.domain ?? ""}</td>
                  <td className="cell--num">{c.message_count}</td>
                  <td className="cell--muted">{c.last_seen?.slice(0, 10) ?? ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </>
  );
}
