import { useCallback, useEffect, useRef, useState } from "react";
import {
  listQueryTools,
  streamQuery,
  type QueryEvent,
  type QueryToolDef,
} from "../api/mailmind";

interface ChatTurn {
  id: number;
  question: string;
  events: QueryEvent[];
  status: "streaming" | "done" | "error";
}

const DEMO_PROMPTS = [
  "who am I ghosting?",
  "what's pending with my recruiter?",
  "summarize this week's job-search activity",
] as const;

export default function Ask() {
  const [tools, setTools] = useState<QueryToolDef[]>([]);
  const [toolsErr, setToolsErr] = useState<string | null>(null);
  const [turns, setTurns] = useState<ChatTurn[]>([]);
  const [pending, setPending] = useState("");
  const [busy, setBusy] = useState(false);
  const turnIdRef = useRef(0);
  const cancelRef = useRef<{ abort: () => void } | null>(null);

  useEffect(() => {
    const ctrl = new AbortController();
    listQueryTools(ctrl.signal)
      .then((d) => setTools(d.tools))
      .catch((err: Error) => {
        if (err.name !== "AbortError") setToolsErr(err.message);
      });
    return () => ctrl.abort();
  }, []);

  const submit = useCallback((question: string) => {
    const trimmed = question.trim();
    if (!trimmed || busy) return;
    const id = ++turnIdRef.current;
    setTurns((prev) => [
      ...prev,
      { id, question: trimmed, events: [], status: "streaming" },
    ]);
    setPending("");
    setBusy(true);

    const stream = streamQuery(trimmed, (ev) => {
      setTurns((prev) =>
        prev.map((t) => (t.id === id ? { ...t, events: [...t.events, ev] } : t)),
      );
    });
    cancelRef.current = stream;

    stream.done
      .then(() => {
        setTurns((prev) =>
          prev.map((t) => (t.id === id ? { ...t, status: "done" } : t)),
        );
      })
      .catch((err: Error) => {
        if (err.name === "AbortError") return;
        setTurns((prev) =>
          prev.map((t) =>
            t.id === id
              ? {
                  ...t,
                  status: "error",
                  events: [...t.events, { kind: "error", error: err.message } as QueryEvent],
                }
              : t,
          ),
        );
      })
      .finally(() => {
        setBusy(false);
        cancelRef.current = null;
      });
  }, [busy]);

  const onCancel = () => {
    cancelRef.current?.abort();
    cancelRef.current = null;
    setBusy(false);
  };

  return (
    <section className="ask">
      <header className="ask__header">
        <h2>Ask</h2>
        <p className="ask__hint">
          Free-form Q&amp;A against the read-only knowledge graph. The query agent
          plans, calls tools, and answers — every step streams below so you can
          see the reasoning. Budget: ≤8 tool calls, ≤30s, ≤$0.10.
        </p>
      </header>

      <ul className="ask__demo-list" aria-label="Demo prompts">
        {DEMO_PROMPTS.map((p) => (
          <li key={p}>
            <button
              type="button"
              className="ask__chip"
              onClick={() => submit(p)}
              disabled={busy}
            >
              {p}
            </button>
          </li>
        ))}
      </ul>

      <form
        className="ask__form"
        onSubmit={(e) => {
          e.preventDefault();
          submit(pending);
        }}
      >
        <input
          className="ask__input"
          placeholder="Ask a question…"
          value={pending}
          onChange={(e) => setPending(e.target.value)}
          disabled={busy}
        />
        <button type="submit" className="ask__submit" disabled={busy || !pending.trim()}>
          {busy ? "Thinking…" : "Ask"}
        </button>
        {busy ? (
          <button type="button" className="ask__cancel" onClick={onCancel}>
            Cancel
          </button>
        ) : null}
      </form>

      <section className="ask__transcript" aria-live="polite">
        {turns.length === 0 ? (
          <p className="ask__empty">No questions yet — try a demo prompt above.</p>
        ) : (
          turns.map((t) => <Turn key={t.id} turn={t} />)
        )}
      </section>

      <details className="ask__tools">
        <summary>{tools.length} read-only tools available</summary>
        {toolsErr ? (
          <p className="ask__err">tools list failed: {toolsErr}</p>
        ) : (
          <ul>
            {tools.map((t) => (
              <li key={t.name}>
                <code>{t.name}</code>
                <span className="ask__tool-desc"> — {t.description}</span>
              </li>
            ))}
          </ul>
        )}
      </details>
    </section>
  );
}

function Turn({ turn }: { turn: ChatTurn }) {
  const answer = turn.events.find((e) => e.kind === "answer");
  const done = turn.events.find((e) => e.kind === "done");

  return (
    <article className={`ask__turn ask__turn--${turn.status}`}>
      <header className="ask__q">
        <span className="ask__q-prefix">Q</span> {turn.question}
      </header>
      <ol className="ask__steps">
        {turn.events.map((ev, i) => (
          <Step key={i} ev={ev} />
        ))}
      </ol>
      {answer && answer.kind === "answer" ? (
        <div className="ask__answer">
          <span className="ask__a-prefix">A</span>
          {answer.answer}
          {answer.truncated ? (
            <small className="ask__truncated">
              {" "}(truncated: {answer.reason})
            </small>
          ) : null}
        </div>
      ) : null}
      {done && done.kind === "done" ? (
        <footer className="ask__stats">
          {done.tool_calls} tool calls · {done.wall_ms}ms · {done.input_tokens}+
          {done.output_tokens} tok · ${done.cost_usd.toFixed(4)}
          {done.stubbed ? " · stubbed" : ""}
          {done.truncated ? ` · truncated:${done.reason}` : ""}
        </footer>
      ) : null}
    </article>
  );
}

function Step({ ev }: { ev: QueryEvent }) {
  if (ev.kind === "thought") {
    return (
      <li className="ask__step ask__step--thought">
        <span className="ask__step-tag">thought</span> {ev.text}
      </li>
    );
  }
  if (ev.kind === "tool_call") {
    return (
      <li className="ask__step ask__step--tool">
        <span className="ask__step-tag">→ {ev.tool}</span>
        <code>{JSON.stringify(ev.args)}</code>
      </li>
    );
  }
  if (ev.kind === "tool_result") {
    return (
      <li className="ask__step ask__step--result">
        <span className="ask__step-tag">← {ev.tool}</span>
        {ev.result_count !== null ? (
          <span className="ask__step-meta">{ev.result_count} item(s)</span>
        ) : (
          <span className="ask__step-meta">ok</span>
        )}
      </li>
    );
  }
  if (ev.kind === "error") {
    return (
      <li className="ask__step ask__step--error">
        <span className="ask__step-tag">error</span> {ev.error}
      </li>
    );
  }
  // start / answer / done are surfaced in the Turn header/footer.
  return null;
}
