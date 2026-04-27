import { useCallback, useEffect, useState } from "react";
import {
  type ContactRollup,
  type EvalRunResult,
  type LabelCounts,
  type LabelRow,
  type ThreadFactsRow,
  type ThreadRow,
  addLabel,
  deleteLabel,
  freezeEvalBaseline,
  generateDraft,
  getEvalBaseline,
  getThreadFacts,
  listEvalResults,
  listLabels,
  listRollups,
  listThreads,
  runEval,
} from "../api/mailmind";

type Loadable<T> =
  | { state: "idle" }
  | { state: "loading" }
  | { state: "ok"; data: T }
  | { state: "err"; error: string };

type Section = "threads" | "rollups" | "drafts";

export default function Eval() {
  const [section, setSection] = useState<Section>("threads");
  const [labels, setLabels] = useState<Loadable<{ labels: LabelRow[]; counts: LabelCounts }>>({
    state: "loading",
  });
  const [baseline, setBaseline] = useState<Loadable<{ frozen: boolean; baseline: EvalRunResult | null }>>({
    state: "loading",
  });
  const [results, setResults] = useState<Loadable<{ results: EvalRunResult[] }>>({ state: "loading" });
  const [running, setRunning] = useState(false);
  const [latest, setLatest] = useState<EvalRunResult | null>(null);

  const refresh = useCallback(async () => {
    setLabels({ state: "loading" });
    setBaseline({ state: "loading" });
    setResults({ state: "loading" });
    try {
      const [l, b, r] = await Promise.all([listLabels(), getEvalBaseline(), listEvalResults(20)]);
      setLabels({ state: "ok", data: { labels: l.labels, counts: l.counts_by_kind } });
      setBaseline({ state: "ok", data: b });
      setResults({ state: "ok", data: { results: r.results } });
    } catch (err) {
      const msg = (err as Error).message;
      setLabels({ state: "err", error: msg });
      setBaseline({ state: "err", error: msg });
      setResults({ state: "err", error: msg });
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const onRunEval = async () => {
    setRunning(true);
    try {
      const r = await runEval();
      setLatest(r);
      refresh();
    } catch (err) {
      alert(`Eval run failed: ${(err as Error).message}`);
    } finally {
      setRunning(false);
    }
  };

  const onFreezeBaseline = async () => {
    if (!confirm("Freeze the most recent eval run as the regression baseline?\n\nFuture runs will be compared to this point. The previous baseline (if any) is overwritten.")) {
      return;
    }
    try {
      await freezeEvalBaseline();
      refresh();
    } catch (err) {
      alert(`Freeze failed: ${(err as Error).message}`);
    }
  };

  const counts = labels.state === "ok" ? labels.data.counts : { thread: 0, rollup: 0, draft: 0, total: 0 };

  return (
    <>
      <section className="section">
        <h2 className="section__title">Eval · Held-out labeled set</h2>
        <p className="hint">
          Hand-label a small set of threads, rollups, and drafts. The eval
          agent re-runs each agent against your labels and reports
          precision/recall, F1, draft style-match (LLM-as-judge), per-agent
          latency, and $/task. Per BUILD §23 the v2 minimum is{" "}
          <strong>20 threads + 10 rollups + 5 drafts</strong>; you can grow
          the set incrementally.
        </p>

        <div className="eval__counts">
          <span className="eval__count">
            <strong>{counts.thread}</strong> threads
          </span>
          <span className="eval__count">
            <strong>{counts.rollup}</strong> rollups
          </span>
          <span className="eval__count">
            <strong>{counts.draft}</strong> drafts
          </span>
        </div>

        <div className="actions">
          <button className="button button--accent" onClick={onRunEval} disabled={running || counts.total === 0}>
            {running ? "Running…" : "Run eval"}
          </button>
          <button className="button" onClick={onFreezeBaseline} disabled={!latest && (results.state !== "ok" || results.data.results.length === 0)}>
            Freeze baseline
          </button>
          <button className="button" onClick={refresh}>
            Refresh
          </button>
        </div>
      </section>

      {latest && <EvalResultPanel result={latest} />}
      {!latest && results.state === "ok" && results.data.results[0] && (
        <EvalResultPanel result={results.data.results[0]} title="Most recent run" />
      )}
      {baseline.state === "ok" && baseline.data.frozen && baseline.data.baseline && (
        <BaselineBadge baseline={baseline.data.baseline} />
      )}

      <section className="section">
        <nav className="tabs tabs--inner">
          {(["threads", "rollups", "drafts"] as const).map((s) => (
            <button
              key={s}
              className={`tab ${s === section ? "tab--active" : ""}`}
              onClick={() => setSection(s)}
            >
              {s} ({s === "threads" ? counts.thread : s === "rollups" ? counts.rollup : counts.draft})
            </button>
          ))}
        </nav>
      </section>

      {section === "threads" && (
        <ThreadLabeler
          existing={labels.state === "ok" ? labels.data.labels.filter((l) => l.kind === "thread") : []}
          onChange={refresh}
        />
      )}
      {section === "rollups" && (
        <RollupLabeler
          existing={labels.state === "ok" ? labels.data.labels.filter((l) => l.kind === "rollup") : []}
          onChange={refresh}
        />
      )}
      {section === "drafts" && (
        <DraftLabeler
          existing={labels.state === "ok" ? labels.data.labels.filter((l) => l.kind === "draft") : []}
          onChange={refresh}
        />
      )}

      <section className="section">
        <h3 className="section__title">Recent eval runs</h3>
        {results.state === "loading" && <p className="hint">loading…</p>}
        {results.state === "err" && <div className="error">{results.error}</div>}
        {results.state === "ok" && results.data.results.length === 0 && (
          <p className="hint">No eval runs yet — label a few targets above and click "Run eval".</p>
        )}
        {results.state === "ok" && results.data.results.length > 0 && (
          <table className="table">
            <thead>
              <tr>
                <th>when</th>
                <th>labels</th>
                <th>extract F1</th>
                <th>rollup tone</th>
                <th>draft style</th>
                <th>regressions</th>
              </tr>
            </thead>
            <tbody>
              {results.data.results.map((r) => (
                <tr key={r.run_id ?? r.ran_at}>
                  <td className="cell--muted">{(r.ran_at ?? "").replace("T", " ").slice(0, 19)}</td>
                  <td>{r.label_counts.total}</td>
                  <td>{fmtMetric(r.metrics?.extract_agent?.f1_commitments_user)}</td>
                  <td>{fmtMetric(r.metrics?.relationship_agent?.tone_accuracy)}</td>
                  <td>{fmtMetric(r.metrics?.draft_agent?.overall_style_match)}</td>
                  <td>
                    {r.regressions.length === 0 ? (
                      <span className="pill pill--success">none</span>
                    ) : (
                      <span className="pill pill--rejected">{r.regressions.length} ↓</span>
                    )}
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

// ============================================================================
// Per-result + baseline panels
// ============================================================================

function EvalResultPanel({ result, title = "Latest run" }: { result: EvalRunResult; title?: string }) {
  const okRows = result.per_label.filter((p) => p.ok !== false);
  const errRows = result.per_label.filter((p) => p.ok === false);

  return (
    <section className="section">
      <h3 className="section__title">{title}</h3>
      {!result.ok && result.reason === "no_labels" && (
        <div className="hint">No labels yet — add a few below.</div>
      )}
      {result.ok === false && result.reason !== "no_labels" && (
        <div className="error">Eval reported issues — see regressions below.</div>
      )}

      <div className="eval__metrics">
        {Object.entries(result.metrics).map(([agent, block]) => (
          <div key={agent} className="eval__metric-card">
            <div className="eval__metric-card-title">
              <code>{agent}</code> <span className="cell--muted">(n={block?.n ?? 0})</span>
            </div>
            <table className="table table--mini">
              <tbody>
                {Object.entries(block ?? {})
                  .filter(([k]) => k !== "n")
                  .map(([k, v]) => (
                    <tr key={k}>
                      <td>
                        <code>{k}</code>
                      </td>
                      <td>{fmtScore(k, v)}</td>
                    </tr>
                  ))}
              </tbody>
            </table>
          </div>
        ))}
      </div>

      {result.regressions.length > 0 && (
        <div className="eval__regressions">
          <h4>Regressions vs baseline (BUILD §23, &gt;5% drop)</h4>
          <table className="table">
            <thead>
              <tr>
                <th>metric</th>
                <th>current</th>
                <th>baseline</th>
                <th>Δ%</th>
              </tr>
            </thead>
            <tbody>
              {result.regressions.map((r) => (
                <tr key={r.path}>
                  <td>
                    <code>{r.path}</code>
                  </td>
                  <td>{r.current.toFixed(3)}</td>
                  <td>{r.baseline.toFixed(3)}</td>
                  <td className="cell--bad">{r.delta_pct.toFixed(1)}%</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <details className="eval__details">
        <summary>
          {okRows.length} scored / {errRows.length} errors — show per-label
        </summary>
        <table className="table">
          <thead>
            <tr>
              <th>label</th>
              <th>kind</th>
              <th>target</th>
              <th>scores</th>
            </tr>
          </thead>
          <tbody>
            {result.per_label.map((p) => (
              <tr key={p.label_id}>
                <td className="cell--mono">{p.label_id.slice(0, 12)}…</td>
                <td>
                  <code>{p.kind}</code>
                </td>
                <td className="cell--mono">{p.target_id}</td>
                <td>
                  {p.ok === false ? (
                    <span className="cell--bad">{p.error}</span>
                  ) : (
                    <code className="cell--muted">
                      {Object.entries(p.scores ?? {})
                        .map(([k, v]) => `${k}=${typeof v === "number" ? v.toFixed(2) : String(v)}`)
                        .join(" · ")}
                    </code>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </details>
    </section>
  );
}

function BaselineBadge({ baseline }: { baseline: EvalRunResult }) {
  return (
    <section className="section">
      <h3 className="section__title">Frozen baseline</h3>
      <p className="hint">
        Frozen at {(baseline.ran_at ?? "").replace("T", " ").slice(0, 19)} ·{" "}
        {baseline.label_counts.total} labels (
        {baseline.label_counts.thread} threads + {baseline.label_counts.rollup} rollups +{" "}
        {baseline.label_counts.draft} drafts)
      </p>
    </section>
  );
}

// ============================================================================
// Thread labeler
// ============================================================================

function ThreadLabeler({ existing, onChange }: { existing: LabelRow[]; onChange: () => void }) {
  const [threads, setThreads] = useState<Loadable<ThreadRow[]>>({ state: "loading" });
  const [picked, setPicked] = useState<string>("");
  const [facts, setFacts] = useState<Loadable<ThreadFactsRow>>({ state: "idle" });
  const [summary, setSummary] = useState("");
  const [user, setUser] = useState<Array<{ description: string; source_message_ids: string }>>([]);
  const [others, setOthers] = useState<Array<{ description: string; source_message_ids: string }>>([]);
  const [priority, setPriority] = useState<"" | "low" | "med" | "high">("");
  const [notes, setNotes] = useState("");
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    listThreads({ limit: 50 })
      .then((r) => setThreads({ state: "ok", data: r.threads }))
      .catch((e: Error) => setThreads({ state: "err", error: e.message }));
  }, []);

  const loadFacts = async (threadId: string) => {
    setPicked(threadId);
    setFacts({ state: "loading" });
    try {
      const f = await getThreadFacts(threadId);
      setFacts({ state: "ok", data: f });
      setSummary(f.facts.summary ?? "");
      setUser(
        (f.facts.commitments_by_user ?? []).map((c) => ({
          description: c.description,
          source_message_ids: c.source_message_ids.join(", "),
        })),
      );
      setOthers(
        (f.facts.commitments_by_others ?? []).map((c) => ({
          description: c.description,
          source_message_ids: c.source_message_ids.join(", "),
        })),
      );
      setPriority("");
      setNotes("");
    } catch (e) {
      setFacts({ state: "err", error: (e as Error).message });
    }
  };

  const onSave = async () => {
    if (!picked) return;
    setSaving(true);
    try {
      await addLabel({
        kind: "thread",
        target_id: picked,
        expected: {
          summary,
          commitments_by_user: user
            .filter((c) => c.description.trim())
            .map((c) => ({
              description: c.description.trim(),
              source_message_ids: c.source_message_ids
                .split(",")
                .map((s) => s.trim())
                .filter(Boolean),
            })),
          commitments_by_others: others
            .filter((c) => c.description.trim())
            .map((c) => ({
              description: c.description.trim(),
              source_message_ids: c.source_message_ids
                .split(",")
                .map((s) => s.trim())
                .filter(Boolean),
            })),
          ...(priority ? { next_step_priority: priority } : {}),
        },
        notes: notes || undefined,
      });
      setPicked("");
      setSummary("");
      setUser([]);
      setOthers([]);
      setPriority("");
      setNotes("");
      setFacts({ state: "idle" });
      onChange();
    } catch (e) {
      alert(`Save failed: ${(e as Error).message}`);
    } finally {
      setSaving(false);
    }
  };

  return (
    <>
      <section className="section">
        <h3 className="section__title">Label a thread</h3>
        <p className="hint">
          Pick a thread, review the agent's extracted facts, and edit/accept
          them as ground truth. The eval will re-extract this thread on each
          run and score against your label.
        </p>

        {threads.state === "loading" && <p className="hint">loading threads…</p>}
        {threads.state === "err" && <div className="error">{threads.error}</div>}
        {threads.state === "ok" && (
          <div className="form-row">
            <label>thread</label>
            <select
              value={picked}
              onChange={(e) => loadFacts(e.target.value)}
              className="input"
            >
              <option value="">— pick a thread —</option>
              {threads.data.map((t) => (
                <option key={t.thread_id} value={t.thread_id}>
                  {t.thread_id} — {t.subject ?? "(no subject)"} ({t.disposition ?? "?"})
                </option>
              ))}
            </select>
          </div>
        )}

        {picked && facts.state === "loading" && <p className="hint">loading facts…</p>}
        {picked && facts.state === "err" && (
          <div className="error">
            {facts.error} — run extract on this thread first via the Inbox tab or
            <code> POST /extract/run</code>.
          </div>
        )}
        {picked && facts.state === "ok" && (
          <div className="eval__form">
            <div className="form-row">
              <label>summary</label>
              <textarea className="input" rows={2} value={summary} onChange={(e) => setSummary(e.target.value)} />
            </div>

            <div className="form-row">
              <label>commitments by user</label>
              <CommitmentEditor items={user} onChange={setUser} />
            </div>

            <div className="form-row">
              <label>commitments by others</label>
              <CommitmentEditor items={others} onChange={setOthers} />
            </div>

            <div className="form-row">
              <label>next-step priority</label>
              <select value={priority} onChange={(e) => setPriority(e.target.value as typeof priority)} className="input">
                <option value="">(skip)</option>
                <option value="high">high</option>
                <option value="med">med</option>
                <option value="low">low</option>
              </select>
            </div>

            <div className="form-row">
              <label>notes (optional)</label>
              <input className="input" value={notes} onChange={(e) => setNotes(e.target.value)} />
            </div>

            <div className="actions">
              <button className="button button--accent" onClick={onSave} disabled={saving}>
                {saving ? "saving…" : "Save thread label"}
              </button>
            </div>
          </div>
        )}
      </section>

      <ExistingLabels rows={existing} onChange={onChange} kindLabel="thread" />
    </>
  );
}

function CommitmentEditor({
  items,
  onChange,
}: {
  items: Array<{ description: string; source_message_ids: string }>;
  onChange: (next: Array<{ description: string; source_message_ids: string }>) => void;
}) {
  const update = (i: number, patch: Partial<{ description: string; source_message_ids: string }>) => {
    const next = items.map((x, idx) => (idx === i ? { ...x, ...patch } : x));
    onChange(next);
  };
  const remove = (i: number) => onChange(items.filter((_, idx) => idx !== i));
  const add = () => onChange([...items, { description: "", source_message_ids: "" }]);
  if (items.length === 0) {
    return (
      <div className="commit-editor">
        <p className="hint">none</p>
        <button className="button button--ghost" onClick={add}>
          + add commitment
        </button>
      </div>
    );
  }
  return (
    <div className="commit-editor">
      {items.map((c, i) => (
        <div className="commit-editor__row" key={i}>
          <input
            className="input"
            placeholder="description"
            value={c.description}
            onChange={(e) => update(i, { description: e.target.value })}
          />
          <input
            className="input commit-editor__ids"
            placeholder="msg-id, msg-id"
            value={c.source_message_ids}
            onChange={(e) => update(i, { source_message_ids: e.target.value })}
          />
          <button className="button button--ghost" onClick={() => remove(i)}>
            remove
          </button>
        </div>
      ))}
      <button className="button button--ghost" onClick={add}>
        + add commitment
      </button>
    </div>
  );
}

// ============================================================================
// Rollup labeler
// ============================================================================

function RollupLabeler({ existing, onChange }: { existing: LabelRow[]; onChange: () => void }) {
  const [rollups, setRollups] = useState<Loadable<ContactRollup[]>>({ state: "loading" });
  const [picked, setPicked] = useState<string>("");
  const [tone, setTone] = useState<ContactRollup["tone"]>("neutral");
  const [cadence, setCadence] = useState<ContactRollup["cadence"]>("weekly");
  const [status, setStatus] = useState<ContactRollup["status"]>("active");
  const [tags, setTags] = useState("");
  const [notes, setNotes] = useState("");
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    listRollups(50)
      .then((r) => setRollups({ state: "ok", data: r.rollups }))
      .catch((e: Error) => setRollups({ state: "err", error: e.message }));
  }, []);

  const loadRollup = (email: string) => {
    setPicked(email);
    if (rollups.state !== "ok") return;
    const found = rollups.data.find((r) => r.contact_email === email);
    if (found) {
      setTone(found.tone);
      setCadence(found.cadence);
      setStatus(found.status);
      setTags(found.tags.join(", "));
      setNotes("");
    }
  };

  const onSave = async () => {
    if (!picked) return;
    setSaving(true);
    try {
      await addLabel({
        kind: "rollup",
        target_id: picked,
        expected: {
          tone,
          cadence,
          status,
          tags: tags
            .split(",")
            .map((s) => s.trim())
            .filter(Boolean),
        },
        notes: notes || undefined,
      });
      setPicked("");
      setNotes("");
      onChange();
    } catch (e) {
      alert(`Save failed: ${(e as Error).message}`);
    } finally {
      setSaving(false);
    }
  };

  return (
    <>
      <section className="section">
        <h3 className="section__title">Label a rollup</h3>
        <p className="hint">
          Pick a contact who's been rolled up, then accept or override
          tone/cadence/status/tags. Eval re-runs the relationship agent and
          compares.
        </p>

        {rollups.state === "loading" && <p className="hint">loading rollups…</p>}
        {rollups.state === "err" && <div className="error">{rollups.error}</div>}
        {rollups.state === "ok" && rollups.data.length === 0 && (
          <p className="hint">No rollups yet — run the relationship agent first.</p>
        )}
        {rollups.state === "ok" && rollups.data.length > 0 && (
          <div className="form-row">
            <label>contact</label>
            <select value={picked} onChange={(e) => loadRollup(e.target.value)} className="input">
              <option value="">— pick a contact —</option>
              {rollups.data.map((r) => (
                <option key={r.contact_email} value={r.contact_email}>
                  {r.contact_email} — {r.tone}/{r.cadence}/{r.status}
                </option>
              ))}
            </select>
          </div>
        )}

        {picked && (
          <div className="eval__form">
            <div className="form-row">
              <label>tone</label>
              <select className="input" value={tone} onChange={(e) => setTone(e.target.value as typeof tone)}>
                <option value="warm">warm</option>
                <option value="neutral">neutral</option>
                <option value="transactional">transactional</option>
                <option value="strained">strained</option>
              </select>
            </div>

            <div className="form-row">
              <label>cadence</label>
              <select className="input" value={cadence} onChange={(e) => setCadence(e.target.value as typeof cadence)}>
                <option value="daily">daily</option>
                <option value="weekly">weekly</option>
                <option value="monthly">monthly</option>
                <option value="rare">rare</option>
              </select>
            </div>

            <div className="form-row">
              <label>status</label>
              <select className="input" value={status} onChange={(e) => setStatus(e.target.value as typeof status)}>
                <option value="active">active</option>
                <option value="dormant">dormant</option>
                <option value="awaiting_them">awaiting_them</option>
                <option value="awaiting_me">awaiting_me</option>
              </select>
            </div>

            <div className="form-row">
              <label>tags (comma-separated)</label>
              <input className="input" value={tags} onChange={(e) => setTags(e.target.value)} />
            </div>

            <div className="form-row">
              <label>notes (optional)</label>
              <input className="input" value={notes} onChange={(e) => setNotes(e.target.value)} />
            </div>

            <div className="actions">
              <button className="button button--accent" onClick={onSave} disabled={saving}>
                {saving ? "saving…" : "Save rollup label"}
              </button>
            </div>
          </div>
        )}
      </section>

      <ExistingLabels rows={existing} onChange={onChange} kindLabel="rollup" />
    </>
  );
}

// ============================================================================
// Draft labeler
// ============================================================================

function DraftLabeler({ existing, onChange }: { existing: LabelRow[]; onChange: () => void }) {
  const [threadId, setThreadId] = useState("");
  const [intent, setIntent] = useState("");
  const [generated, setGenerated] = useState<Loadable<{ subject: string; body: string }>>({ state: "idle" });
  const [tone, setTone] = useState("warm-confident");
  const [mustMention, setMustMention] = useState("");
  const [mustNot, setMustNot] = useState("");
  const [factuallyCorrect, setFactuallyCorrect] = useState(true);
  const [wouldSend, setWouldSend] = useState(true);
  const [body, setBody] = useState("");
  const [notes, setNotes] = useState("");
  const [saving, setSaving] = useState(false);

  const onGenerate = async () => {
    if (!threadId.trim() || !intent.trim()) return;
    setGenerated({ state: "loading" });
    try {
      const r = await generateDraft(threadId.trim(), intent.trim());
      setGenerated({ state: "ok", data: { subject: r.draft.subject, body: r.draft.body } });
    } catch (e) {
      setGenerated({ state: "err", error: (e as Error).message });
    }
  };

  const onSave = async () => {
    if (!threadId.trim() || !intent.trim()) {
      alert("Fill thread_id + intent first.");
      return;
    }
    setSaving(true);
    try {
      await addLabel({
        kind: "draft",
        target_id: `${threadId.trim()}::${intent.trim()}`,
        expected: {
          tone,
          must_mention: mustMention
            .split(",")
            .map((s) => s.trim())
            .filter(Boolean),
          must_not_mention: mustNot
            .split(",")
            .map((s) => s.trim())
            .filter(Boolean),
          factually_correct: factuallyCorrect,
          would_send: wouldSend,
          ...(body ? { body } : {}),
        },
        notes: notes || undefined,
      });
      setThreadId("");
      setIntent("");
      setMustMention("");
      setMustNot("");
      setBody("");
      setNotes("");
      setGenerated({ state: "idle" });
      onChange();
    } catch (e) {
      alert(`Save failed: ${(e as Error).message}`);
    } finally {
      setSaving(false);
    }
  };

  return (
    <>
      <section className="section">
        <h3 className="section__title">Label a draft</h3>
        <p className="hint">
          Provide a (thread_id, intent) pair, generate the agent's current
          draft, then mark expected tone, must-mention strings, factual
          correctness, and would-send. The LLM-as-judge in
          <code> evals/judge_prompts/v1.md</code> compares each future
          generation against this.
        </p>

        <div className="form-row">
          <label>thread_id</label>
          <input className="input" value={threadId} onChange={(e) => setThreadId(e.target.value)} placeholder="fix-thread-102" />
        </div>
        <div className="form-row">
          <label>intent</label>
          <input
            className="input"
            value={intent}
            onChange={(e) => setIntent(e.target.value)}
            placeholder="Reply to Adam Moore confirming the Thursday 2pm call"
          />
        </div>
        <div className="actions">
          <button className="button" onClick={onGenerate} disabled={!threadId.trim() || !intent.trim()}>
            Preview agent draft
          </button>
        </div>

        {generated.state === "loading" && <p className="hint">generating…</p>}
        {generated.state === "err" && <div className="error">{generated.error}</div>}
        {generated.state === "ok" && (
          <div className="eval__draft-preview">
            <div className="eval__draft-subject">
              <strong>{generated.data.subject}</strong>
            </div>
            <pre className="eval__draft-body">{generated.data.body}</pre>
          </div>
        )}

        <div className="eval__form">
          <div className="form-row">
            <label>expected tone</label>
            <input className="input" value={tone} onChange={(e) => setTone(e.target.value)} />
          </div>

          <div className="form-row">
            <label>must mention (comma-separated)</label>
            <input className="input" value={mustMention} onChange={(e) => setMustMention(e.target.value)} placeholder="Thursday, 2pm, Adam" />
          </div>

          <div className="form-row">
            <label>must NOT mention</label>
            <input className="input" value={mustNot} onChange={(e) => setMustNot(e.target.value)} />
          </div>

          <div className="form-row">
            <label>factually correct</label>
            <select className="input" value={factuallyCorrect ? "1" : "0"} onChange={(e) => setFactuallyCorrect(e.target.value === "1")}>
              <option value="1">yes</option>
              <option value="0">no</option>
            </select>
          </div>

          <div className="form-row">
            <label>would send as-is</label>
            <select className="input" value={wouldSend ? "1" : "0"} onChange={(e) => setWouldSend(e.target.value === "1")}>
              <option value="1">yes</option>
              <option value="0">no</option>
            </select>
          </div>

          <div className="form-row">
            <label>gold body (optional)</label>
            <textarea className="input" rows={4} value={body} onChange={(e) => setBody(e.target.value)} placeholder="Leave blank if you don't want to write a model answer." />
          </div>

          <div className="form-row">
            <label>notes (optional)</label>
            <input className="input" value={notes} onChange={(e) => setNotes(e.target.value)} />
          </div>

          <div className="actions">
            <button className="button button--accent" onClick={onSave} disabled={saving}>
              {saving ? "saving…" : "Save draft label"}
            </button>
          </div>
        </div>
      </section>

      <ExistingLabels rows={existing} onChange={onChange} kindLabel="draft" />
    </>
  );
}

// ============================================================================
// Existing labels list (shared)
// ============================================================================

function ExistingLabels({
  rows,
  onChange,
  kindLabel,
}: {
  rows: LabelRow[];
  onChange: () => void;
  kindLabel: "thread" | "rollup" | "draft";
}) {
  const onDelete = async (id: string) => {
    if (!confirm("Delete this label? (soft delete — append-only on disk)")) return;
    try {
      await deleteLabel(id);
      onChange();
    } catch (e) {
      alert(`Delete failed: ${(e as Error).message}`);
    }
  };
  return (
    <section className="section">
      <h3 className="section__title">Existing {kindLabel} labels</h3>
      {rows.length === 0 ? (
        <p className="hint">none yet</p>
      ) : (
        <table className="table">
          <thead>
            <tr>
              <th>target</th>
              <th>labeled at</th>
              <th>preview</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.id}>
                <td className="cell--mono">{r.target_id}</td>
                <td className="cell--muted">{r.labeled_at.replace("T", " ").slice(0, 19)}</td>
                <td className="cell--muted">{summarize(r)}</td>
                <td>
                  <button className="button button--ghost" onClick={() => onDelete(r.id)}>
                    delete
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}

// ============================================================================
// Helpers
// ============================================================================

function summarize(r: LabelRow): string {
  const e = r.expected as Record<string, unknown>;
  if (r.kind === "thread") {
    const s = (e.summary as string | undefined) ?? "";
    return s.length > 80 ? s.slice(0, 80) + "…" : s;
  }
  if (r.kind === "rollup") {
    return `${e.tone}/${e.cadence}/${e.status} · tags=${
      Array.isArray(e.tags) ? (e.tags as string[]).join(",") : ""
    }`;
  }
  if (r.kind === "draft") {
    const must = Array.isArray(e.must_mention) ? (e.must_mention as string[]).join(",") : "";
    return `tone=${e.tone}, would_send=${e.would_send}, must_mention=[${must}]`;
  }
  return "";
}

function fmtScore(k: string, v: unknown): string {
  if (typeof v === "number") {
    // ms latency keys are ints; everything else is a [0,1] float.
    if (k.endsWith("_ms")) return `${v} ms`;
    if (k === "mean_cost_usd") return `$${v.toFixed(4)}`;
    return v.toFixed(3);
  }
  if (v == null) return "-";
  return String(v);
}

function fmtMetric(v: unknown): string {
  if (typeof v === "number") return v.toFixed(3);
  return "-";
}
