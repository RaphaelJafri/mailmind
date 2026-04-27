import { useCallback, useEffect, useState } from "react";
import {
  fetchAgentsHealth,
  fetchIngesterHealth,
  type AgentsHealth,
  type IngesterHealth,
} from "../api/health";

type Loadable<T> =
  | { state: "idle" }
  | { state: "loading" }
  | { state: "ok"; data: T }
  | { state: "err"; error: string };

function statusClass(s: string) {
  if (s === "ok") return "status status--ok";
  if (s === "error") return "status status--err";
  if (s === "skipped" || s === "unknown") return "status status--unknown";
  return "status status--warn";
}

export default function Setup() {
  const [ingester, setIngester] = useState<Loadable<IngesterHealth>>({
    state: "idle",
  });
  const [agents, setAgents] = useState<Loadable<AgentsHealth>>({ state: "idle" });

  const ping = useCallback(() => {
    const ctrl = new AbortController();
    setIngester({ state: "loading" });
    setAgents({ state: "loading" });
    fetchIngesterHealth(ctrl.signal)
      .then((data) => setIngester({ state: "ok", data }))
      .catch((err: Error) =>
        setIngester({ state: "err", error: err.message })
      );
    fetchAgentsHealth(ctrl.signal)
      .then((data) => setAgents({ state: "ok", data }))
      .catch((err: Error) => setAgents({ state: "err", error: err.message }));
    return () => ctrl.abort();
  }, []);

  useEffect(() => {
    return ping();
  }, [ping]);

  return (
    <>
      <section className="section">
        <h2 className="section__title">P0 health check</h2>
        <p className="hint">
          Confirms both sidecars (Node ingester on <code>:8766</code>, Python
          agent service on <code>:8765</code>) are reachable and that the agent
          service can reach Vertex AI.
        </p>
      </section>

      <section className="section">
        <h2 className="section__title">Node ingester</h2>
        <SidecarCard label="ingester" loadable={ingester} render={renderIngester} />
      </section>

      <section className="section">
        <h2 className="section__title">Python agent service</h2>
        <SidecarCard label="agents" loadable={agents} render={renderAgents} />
      </section>

      <div className="actions">
        <button className="button" onClick={ping}>
          Re-check
        </button>
      </div>
    </>
  );
}

function SidecarCard<T>({
  label,
  loadable,
  render,
}: {
  label: string;
  loadable: Loadable<T>;
  render: (data: T) => React.ReactNode;
}) {
  return (
    <div className="card">
      {loadable.state === "loading" && (
        <Row k="status" v={<span className={statusClass("unknown")}>checking…</span>} />
      )}
      {loadable.state === "err" && (
        <>
          <Row
            k="status"
            v={<span className={statusClass("error")}>unreachable</span>}
          />
          <div className="error">
            {loadable.error}
            {"\n"}
            <span style={{ color: "var(--muted)" }}>
              Start it with: <br />
              {label === "agents"
                ? "cd agents && source .venv/bin/activate && python service.py"
                : "cd ingester && npm start"}
            </span>
          </div>
        </>
      )}
      {loadable.state === "ok" && render(loadable.data)}
    </div>
  );
}

function Row({ k, v }: { k: string; v: React.ReactNode }) {
  return (
    <div className="row">
      <span className="row__label">{k}</span>
      <span className="row__value">{v}</span>
    </div>
  );
}

function renderIngester(d: IngesterHealth) {
  return (
    <>
      <Row k="status" v={<span className={statusClass("ok")}>ok</span>} />
      <Row k="node" v={d.node_version} />
      <Row k="data dir" v={d.data_dir} />
      <Row
        k="raw.sqlite"
        v={
          <span className={statusClass(d.raw_db_exists ? "ok" : "skipped")}>
            {d.raw_db_exists ? "present" : "not yet (sync to create)"}
          </span>
        }
      />
      <Row
        k="gmail tokens"
        v={
          <span className={statusClass(d.gmail_tokens_present ? "ok" : "skipped")}>
            {d.gmail_tokens_present ? "authorized" : "not yet"}
          </span>
        }
      />
    </>
  );
}

function renderAgents(d: AgentsHealth) {
  const g = d.gemini;
  return (
    <>
      <Row k="status" v={<span className={statusClass("ok")}>ok</span>} />
      <Row k="python" v={d.python_version} />
      <Row k="data dir" v={d.data_dir} />
      <Row
        k="gemini"
        v={
          <span className={statusClass(g.status)}>
            {g.status}
            {g.stubbed ? " (stubbed)" : ""}
            {g.latency_ms != null ? ` · ${g.latency_ms} ms` : ""}
          </span>
        }
      />
      <Row k="model" v={g.model} />
      <Row k="region" v={g.region} />
      <Row k="project" v={g.project ?? "(none)"} />
      <Row k="auth" v={g.auth_mode} />
      {g.error && <div className="error">{g.error}</div>}
    </>
  );
}
