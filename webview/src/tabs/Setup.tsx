import { useCallback, useEffect, useState } from "react";
import {
  fetchAgentsHealth,
  fetchIngesterHealth,
  type AgentsHealth,
  type IngesterHealth,
} from "../api/health";
import {
  getPermissions,
  setPermissions,
  type PermissionsRow,
} from "../api/mailmind";

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

      <PermissionsSection />
    </>
  );
}

function PermissionsSection() {
  const [perms, setPerms] = useState<Loadable<PermissionsRow>>({ state: "idle" });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setPerms({ state: "loading" });
    try {
      const p = await getPermissions();
      setPerms({ state: "ok", data: p });
    } catch (err) {
      setPerms({ state: "err", error: (err as Error).message });
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  async function flipCompose(value: boolean) {
    setBusy(true);
    setError(null);
    try {
      const updated = await setPermissions({ gmail_compose: value });
      setPerms({ state: "ok", data: updated });
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  const composeOn = perms.state === "ok" && perms.data["gmail.compose"];
  const sendOn = perms.state === "ok" && perms.data["gmail.send"];

  return (
    <section className="section">
      <h2 className="section__title">Permissions</h2>
      <p className="hint">
        OAuth scopes mailmind has been granted. Drafts can only land in Gmail
        once you grant <code>gmail.compose</code>; sending is gated behind a
        separate, opt-in <code>gmail.send</code> grant (P4b — disabled in this
        build).
      </p>
      <div className="card">
        <div className="row">
          <span className="row__label">gmail.compose</span>
          <span className="row__value">
            <span className={composeOn ? "status status--ok" : "status status--unknown"}>
              {composeOn ? "granted" : "not granted"}
            </span>
            <button
              className="button"
              style={{ marginLeft: 12 }}
              disabled={busy}
              onClick={() => flipCompose(!composeOn)}
            >
              {composeOn ? "Revoke" : "Grant gmail.compose"}
            </button>
          </span>
        </div>
        <div className="row">
          <span className="row__label">gmail.send</span>
          <span className="row__value">
            <span className={sendOn ? "status status--ok" : "status status--unknown"}>
              {sendOn ? "granted" : "not granted (P4b)"}
            </span>
            <button
              className="button"
              style={{ marginLeft: 12 }}
              disabled
              title="gmail.send is gated behind P4b. Toggle disabled in this build."
            >
              Enable Gmail send (P4b)
            </button>
          </span>
        </div>
        {error && <div className="error">{error}</div>}
        {perms.state === "err" && <div className="error">{perms.error}</div>}
      </div>
    </section>
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
