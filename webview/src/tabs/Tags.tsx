import { useEffect, useState } from "react";
import { listTags, type TagSummaryRow, type TagRow } from "../api/mailmind";

type Loadable<T> =
  | { state: "loading" }
  | { state: "ok"; data: T }
  | { state: "err"; error: string };

export default function Tags() {
  const [data, setData] = useState<
    Loadable<{ summary: TagSummaryRow[]; tags: TagRow[] }>
  >({ state: "loading" });
  const [filter, setFilter] = useState<{ kind: string; value: string } | null>(null);

  useEffect(() => {
    const ctrl = new AbortController();
    setData({ state: "loading" });
    listTags(
      { kind: filter?.kind, value: filter?.value, limit: 200 },
      ctrl.signal,
    )
      .then((d) => setData({ state: "ok", data: d }))
      .catch((err: Error) => {
        if (err.name !== "AbortError") setData({ state: "err", error: err.message });
      });
    return () => ctrl.abort();
  }, [filter]);

  return (
    <>
      <section className="section">
        <h2 className="section__title">Tags</h2>
        <p className="hint">
          Per-message tags from the Tagger agent. Three dimensions:
          urgency, category, project. Click any cell to filter.
        </p>
      </section>

      {data.state === "loading" && <p className="hint">loading…</p>}
      {data.state === "err" && <div className="error">{data.error}</div>}
      {data.state === "ok" && (
        <>
          <section className="section">
            <h3 className="section__subtitle">Summary</h3>
            {data.data.summary.length === 0 ? (
              <p className="hint">No tags yet. Run the tagger first.</p>
            ) : (
              <div className="tag-grid">
                {(["urgency", "category", "project"] as const).map((kind) => {
                  const rows = data.data.summary.filter((s) => s.tag_kind === kind);
                  if (rows.length === 0) return null;
                  return (
                    <div className="tag-grid__col" key={kind}>
                      <h4 className="tag-grid__title">{kind}</h4>
                      <ul>
                        {rows.map((r) => (
                          <li
                            key={`${kind}:${r.tag_value}`}
                            className={`taglink ${
                              filter?.kind === kind && filter?.value === r.tag_value
                                ? "taglink--active"
                                : ""
                            }`}
                            onClick={() =>
                              setFilter(
                                filter?.kind === kind && filter?.value === r.tag_value
                                  ? null
                                  : { kind, value: r.tag_value },
                              )
                            }
                          >
                            <span className={`pill pill--${r.tag_value}`}>{r.tag_value}</span>
                            <span className="cell--num">{r.message_count}</span>
                          </li>
                        ))}
                      </ul>
                    </div>
                  );
                })}
              </div>
            )}
            {filter && (
              <div className="actions">
                <span className="hint">
                  filter: {filter.kind}={filter.value}
                </span>
                <button className="button" onClick={() => setFilter(null)}>
                  Clear
                </button>
              </div>
            )}
          </section>

          <section className="section">
            <h3 className="section__subtitle">Recent tagged messages</h3>
            {data.data.tags.length === 0 ? (
              <p className="hint">no rows</p>
            ) : (
              <table className="table">
                <thead>
                  <tr>
                    <th>message_id</th>
                    <th>kind</th>
                    <th>value</th>
                    <th>conf</th>
                    <th>at</th>
                  </tr>
                </thead>
                <tbody>
                  {data.data.tags.slice(0, 100).map((t) => (
                    <tr key={`${t.message_id}:${t.tag_kind}:${t.tag_value}`}>
                      <td className="cell--mono">{t.message_id}</td>
                      <td className="cell--muted">{t.tag_kind}</td>
                      <td>
                        <span className={`pill pill--${t.tag_value}`}>{t.tag_value}</span>
                      </td>
                      <td className="cell--muted">{t.confidence}</td>
                      <td className="cell--muted">{t.tagged_at.slice(0, 19).replace("T", " ")}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </section>
        </>
      )}
    </>
  );
}
