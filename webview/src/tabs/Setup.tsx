import { useCallback, useEffect, useRef, useState } from "react";
import {
  fetchAgentsHealth,
  fetchIngesterHealth,
  type AgentsHealth,
  type IngesterHealth,
} from "../api/health";
import {
  type AuthStatus,
  cancelAuth,
  deleteOAuthConfig,
  disconnectGmail,
  getAuthStatus,
  getPermissions,
  pollAuth,
  saveOAuthConfig,
  setPermissions,
  startAuth,
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
      <GmailConnectionSection />

      <section className="section">
        <h2 className="section__title">Sidecar health</h2>
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

// =============================================================================
// Gmail Connection — drives in-dashboard OAuth setup end-to-end.
// =============================================================================

function GmailConnectionSection() {
  const [status, setStatus] = useState<Loadable<AuthStatus>>({ state: "loading" });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const s = await getAuthStatus();
      setStatus({ state: "ok", data: s });
    } catch (err) {
      setStatus({ state: "err", error: (err as Error).message });
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  return (
    <section className="section">
      <h2 className="section__title">Gmail connection</h2>
      <p className="hint">
        mailmind needs read-only access to your Gmail. The setup is{" "}
        <strong>two one-time steps</strong>: create a Google Cloud OAuth client
        (5 min, browser only — Google won't let any app do this for you), then
        paste the resulting credentials here. After that, connecting / syncing /
        disconnecting are all single clicks.
      </p>

      {error && <div className="error">{error}</div>}
      {status.state === "loading" && <p className="hint">checking connection…</p>}
      {status.state === "err" && (
        <div className="error">
          {status.error}{" "}
          <span className="cell--muted">— is the ingester running on :8766?</span>
        </div>
      )}
      {status.state === "ok" && (
        <ConnectionPanel
          status={status.data}
          busy={busy}
          setBusy={setBusy}
          setError={setError}
          refresh={refresh}
        />
      )}
    </section>
  );
}

function ConnectionPanel({
  status,
  busy,
  setBusy,
  setError,
  refresh,
}: {
  status: AuthStatus;
  busy: boolean;
  setBusy: (b: boolean) => void;
  setError: (e: string | null) => void;
  refresh: () => Promise<void>;
}) {
  if (status.has_tokens && status.account_email) {
    return (
      <ConnectedPanel status={status} busy={busy} setBusy={setBusy} setError={setError} refresh={refresh} />
    );
  }
  if (status.configured) {
    return (
      <NeedsAuthPanel status={status} busy={busy} setBusy={setBusy} setError={setError} refresh={refresh} />
    );
  }
  return <NeedsConfigPanel busy={busy} setBusy={setBusy} setError={setError} refresh={refresh} />;
}

// ---- State A: no OAuth client yet ----

function NeedsConfigPanel({
  busy,
  setBusy,
  setError,
  refresh,
}: {
  busy: boolean;
  setBusy: (b: boolean) => void;
  setError: (e: string | null) => void;
  refresh: () => Promise<void>;
}) {
  const [showGuide, setShowGuide] = useState(true);
  const [clientId, setClientId] = useState("");
  const [clientSecret, setClientSecret] = useState("");

  const onSave = async () => {
    if (!clientId.trim() || !clientSecret.trim()) {
      setError("Both Client ID and Client Secret are required.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await saveOAuthConfig({
        client_id: clientId.trim(),
        client_secret: clientSecret.trim(),
      });
      setClientId("");
      setClientSecret("");
      await refresh();
    } catch (err) {
      const msg = (err as Error).message || String(err);
      const m = msg.match(/→ (\d+):\s*(.*)/);
      if (m) {
        try {
          const body = JSON.parse(m[2]);
          setError(body.message || body.error || msg);
        } catch {
          setError(msg);
        }
      } else {
        setError(msg);
      }
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="auth-panel">
      <div className="auth-panel__step">
        <div className="auth-panel__step-num">1</div>
        <div className="auth-panel__step-body">
          <strong>Create an OAuth client in Google Cloud Console.</strong>
          <button
            className="button button--ghost auth-panel__guide-toggle"
            onClick={() => setShowGuide((v) => !v)}
          >
            {showGuide ? "hide steps" : "show steps"}
          </button>
          {showGuide && (
            <ol className="auth-panel__guide">
              <li>
                Open{" "}
                <a
                  href="https://console.cloud.google.com/apis/credentials"
                  target="_blank"
                  rel="noreferrer"
                >
                  console.cloud.google.com/apis/credentials
                </a>
                . Create a new project (or pick an existing one) — this is just
                a container for the OAuth client; nothing is billed.
              </li>
              <li>
                Configure the OAuth consent screen:{" "}
                <em>External</em> user type, app name <code>mailmind</code>, your
                email as developer contact. Add your Gmail address to{" "}
                <strong>Test users</strong> so you can authorize without
                publishing the app.
              </li>
              <li>
                Add the scope{" "}
                <code>https://www.googleapis.com/auth/gmail.readonly</code>{" "}
                under <em>Scopes</em>. (You can add{" "}
                <code>gmail.compose</code> and <code>gmail.send</code> later
                from <em>Settings → Permissions</em> below if you want the
                draft / send features.)
              </li>
              <li>
                Click <em>Credentials → Create Credentials → OAuth client ID</em>
                . Pick application type <strong>Desktop app</strong> (the
                redirect we use is loopback). Name it <code>mailmind</code>.
              </li>
              <li>
                Copy the <strong>Client ID</strong> and{" "}
                <strong>Client Secret</strong> from the dialog and paste them
                below.
              </li>
            </ol>
          )}
        </div>
      </div>

      <div className="auth-panel__step">
        <div className="auth-panel__step-num">2</div>
        <div className="auth-panel__step-body auth-panel__step-body--form">
          <strong>Paste your credentials.</strong>
          <p className="hint">
            Stored locally at{" "}
            <code>~/Library/Application Support/mailmind/config/oauth_client.json</code>{" "}
            (mode 0600). Never leaves your machine.
          </p>
          <div className="form-row">
            <label>Client ID</label>
            <input
              className="input"
              type="text"
              value={clientId}
              onChange={(e) => setClientId(e.target.value)}
              placeholder="123456789-abc.apps.googleusercontent.com"
              autoComplete="off"
              spellCheck={false}
            />
          </div>
          <div className="form-row">
            <label>Client Secret</label>
            <input
              className="input"
              type="password"
              value={clientSecret}
              onChange={(e) => setClientSecret(e.target.value)}
              placeholder="GOCSPX-…"
              autoComplete="off"
              spellCheck={false}
            />
          </div>
          <div className="actions">
            <button
              className="button button--accent"
              onClick={onSave}
              disabled={busy || !clientId.trim() || !clientSecret.trim()}
            >
              {busy ? "saving…" : "Save & continue"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

// ---- State B: client configured, no tokens ----

function NeedsAuthPanel({
  status,
  busy,
  setBusy,
  setError,
  refresh,
}: {
  status: AuthStatus;
  busy: boolean;
  setBusy: (b: boolean) => void;
  setError: (e: string | null) => void;
  refresh: () => Promise<void>;
}) {
  const [stateToken, setStateToken] = useState<string | null>(null);
  const [authUrl, setAuthUrl] = useState<string | null>(null);
  const [pollMsg, setPollMsg] = useState<string>("");
  const cancelRef = useRef<{ aborted: boolean }>({ aborted: false });

  const onConnect = async () => {
    setBusy(true);
    setError(null);
    setPollMsg("");
    cancelRef.current = { aborted: false };
    try {
      const flow = await startAuth();
      setStateToken(flow.state_token);
      setAuthUrl(flow.auth_url);
      // Try to open the OAuth tab. Most browsers will allow it because
      // it's the result of a click handler; if the popup blocker bites,
      // we render a fallback "Open Google authorization" link.
      const popup = window.open(flow.auth_url, "_blank", "noopener,noreferrer");
      if (!popup) {
        setPollMsg("Popup blocked — click 'Open Google authorization' below.");
      } else {
        setPollMsg("Waiting for you to allow access in the new tab…");
      }
      // Poll until the loopback handler resolves.
      const deadline = Date.now() + flow.expires_in_s * 1000;
      while (Date.now() < deadline && !cancelRef.current.aborted) {
        await new Promise((r) => setTimeout(r, 1500));
        const p = await pollAuth(flow.state_token);
        if (p.status === "authorized") {
          setPollMsg("Authorized.");
          setStateToken(null);
          setAuthUrl(null);
          await refresh();
          return;
        }
        if (p.status === "error") {
          setError(`OAuth failed: ${p.error ?? "unknown"}`);
          setStateToken(null);
          setAuthUrl(null);
          return;
        }
      }
      if (!cancelRef.current.aborted) {
        setError("Authorization timed out. Try again.");
        try { await cancelAuth(flow.state_token); } catch {}
        setStateToken(null);
        setAuthUrl(null);
      }
    } catch (err) {
      const msg = (err as Error).message || String(err);
      const m = msg.match(/→ (\d+):\s*(.*)/);
      if (m) {
        try {
          const body = JSON.parse(m[2]);
          setError(body.message || body.error || msg);
        } catch {
          setError(msg);
        }
      } else {
        setError(msg);
      }
    } finally {
      setBusy(false);
    }
  };

  const onCancelFlow = async () => {
    cancelRef.current = { aborted: true };
    if (stateToken) {
      try { await cancelAuth(stateToken); } catch {}
    }
    setStateToken(null);
    setAuthUrl(null);
    setPollMsg("");
    setBusy(false);
  };

  const onForgetCreds = async () => {
    if (status.config_source === "env") {
      setError(
        "Credentials are sourced from environment variables — edit ingester/.env to change them.",
      );
      return;
    }
    if (!confirm("Forget the saved OAuth client credentials? You'll need to paste them again to reconnect.")) {
      return;
    }
    setBusy(true);
    try {
      await deleteOAuthConfig();
      await refresh();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="auth-panel">
      <div className="auth-panel__connected">
        <span className="status status--ok">credentials saved</span>
        <span className="cell--muted">
          {" "}
          Client ID <code>{status.client_id_preview}</code>
          {status.config_source === "env" && " · sourced from .env"}
          {status.config_saved_at && status.config_source === "file" && (
            <> · saved {shortLocal(status.config_saved_at)}</>
          )}
        </span>
      </div>

      <p className="hint">
        Click <strong>Connect Gmail</strong> to authorize. A new tab will open
        on Google; once you click <em>Allow</em> there, this page will pick up
        the success automatically (no copy-paste of codes required).
      </p>

      <div className="actions">
        {!stateToken && (
          <button
            className="button button--accent"
            onClick={onConnect}
            disabled={busy}
          >
            {busy ? "starting…" : "Connect Gmail"}
          </button>
        )}
        {stateToken && (
          <>
            <span className="auth-panel__poll-msg">{pollMsg}</span>
            {authUrl && (
              <a
                className="button"
                href={authUrl}
                target="_blank"
                rel="noreferrer"
              >
                Open Google authorization
              </a>
            )}
            <button className="button button--ghost" onClick={onCancelFlow}>
              cancel
            </button>
          </>
        )}
        {!stateToken && status.config_source === "file" && (
          <button className="button button--ghost" onClick={onForgetCreds} disabled={busy}>
            forget saved credentials
          </button>
        )}
      </div>
    </div>
  );
}

// ---- State C: connected ----

function ConnectedPanel({
  status,
  busy,
  setBusy,
  setError,
  refresh,
}: {
  status: AuthStatus;
  busy: boolean;
  setBusy: (b: boolean) => void;
  setError: (e: string | null) => void;
  refresh: () => Promise<void>;
}) {
  const onDisconnect = async () => {
    if (
      !confirm(
        `Disconnect ${status.account_email}? mailmind will keep your saved data, but cannot sync new mail until you reconnect.`,
      )
    ) {
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await disconnectGmail();
      await refresh();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="auth-panel auth-panel--connected">
      <div className="auth-panel__connected">
        <span className="status status--ok">connected</span>
        <strong className="auth-panel__email">{status.account_email}</strong>
      </div>
      <p className="hint">
        Sync runs from the Inbox tab's <em>Sync now</em> button. To switch
        accounts: disconnect here first, then reconnect with a different
        Google login.
      </p>
      <div className="actions">
        <button className="button" onClick={onDisconnect} disabled={busy}>
          {busy ? "disconnecting…" : "Disconnect Gmail"}
        </button>
      </div>
    </div>
  );
}

function shortLocal(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString();
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

  async function flipSend(value: boolean) {
    setBusy(true);
    setError(null);
    try {
      const updated = await setPermissions({ gmail_send: value });
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
              {sendOn ? "granted" : "not granted"}
            </span>
            <button
              className="button"
              style={{ marginLeft: 12 }}
              disabled={busy || (!sendOn && !composeOn)}
              onClick={() => {
                if (!sendOn) {
                  const ok = window.confirm(
                    "Enable gmail.send? Drafts you approve from now on can be sent " +
                      "via Gmail after a 30s undo window. You can revoke this at any time."
                  );
                  if (!ok) return;
                }
                flipSend(!sendOn);
              }}
              title={
                sendOn
                  ? "Revoke gmail.send. Existing in-flight approvals will be refused at execute time."
                  : composeOn
                  ? "Enable Gmail send. Approve & Send button activates after grant."
                  : "Grant gmail.compose first."
              }
            >
              {sendOn ? "Revoke" : "Enable Gmail send"}
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
