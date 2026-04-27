import { useCallback, useEffect, useState } from "react";
import {
  type Anomaly,
  type BudgetConfig,
  type CostTrajectory,
  type LogEvent,
  type ObservabilitySummary,
  getAnomalies,
  getBudgetConfig,
  getCostTrajectory,
  getLogTail,
  getObservabilitySummary,
} from "../api/mailmind";

type Loadable<T> =
  | { state: "loading" }
  | { state: "ok"; data: T }
  | { state: "err"; error: string };

export default function Observability() {
  const [summary, setSummary] = useState<Loadable<ObservabilitySummary>>({ state: "loading" });
  const [trajectory, setTrajectory] = useState<Loadable<CostTrajectory>>({ state: "loading" });
  const [anomalies, setAnomalies] = useState<Loadable<{ anomalies: Anomaly[] }>>({ state: "loading" });
  const [budget, setBudget] = useState<Loadable<BudgetConfig>>({ state: "loading" });
  const [logs, setLogs] = useState<Loadable<{ events: LogEvent[] }>>({ state: "loading" });

  const refresh = useCallback(async () => {
    setSummary({ state: "loading" });
    setTrajectory({ state: "loading" });
    setAnomalies({ state: "loading" });
    setBudget({ state: "loading" });
    setLogs({ state: "loading" });

    const wrap = <T,>(p: Promise<T>, set: (v: Loadable<T>) => void) =>
      p
        .then((data) => set({ state: "ok", data }))
        .catch((err: unknown) => set({ state: "err", error: (err as Error).message }));

    await Promise.all([
      wrap(getObservabilitySummary(), setSummary),
      wrap(getCostTrajectory(7), setTrajectory),
      wrap(getAnomalies(50), setAnomalies),
      wrap(getBudgetConfig(), setBudget),
      wrap(getLogTail(150), setLogs),
    ]);
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  return (
    <>
      <section className="section">
        <h2 className="section__title">Observability</h2>
        <p className="hint">
          Per-agent cost, latency, schema-fail rate, and anomalies. All numbers
          come from <code>agent_runs.sqlite</code>. Caps live in{" "}
          <code>config/pipeline.yml</code>; tweak there to retune.
        </p>
        <div className="actions">
          <button className="button" onClick={refresh}>
            Refresh
          </button>
        </div>
      </section>

      {summary.state === "loading" && <p className="hint">loading summary…</p>}
      {summary.state === "err" && <div className="error">{summary.error}</div>}
      {summary.state === "ok" && (
        <SummaryCards summary={summary.data} />
      )}

      <section className="section">
        <h3 className="section__title">7-day cost trajectory</h3>
        {trajectory.state === "loading" && <p className="hint">loading…</p>}
        {trajectory.state === "err" && <div className="error">{trajectory.error}</div>}
        {trajectory.state === "ok" && <CostChart data={trajectory.data} />}
      </section>

      <section className="section">
        <h3 className="section__title">Per-agent latency (rolling 7 days)</h3>
        {summary.state === "ok" && <LatencyTable rows={summary.data.latency_by_agent} />}
      </section>

      <section className="section">
        <h3 className="section__title">Anomalies</h3>
        <p className="hint">
          A run earns an anomaly badge when its latency or cost is markedly
          higher than that agent's rolling P95. Multipliers come from{" "}
          <code>config/pipeline.yml → anomalies</code>.
        </p>
        {anomalies.state === "loading" && <p className="hint">loading…</p>}
        {anomalies.state === "err" && <div className="error">{anomalies.error}</div>}
        {anomalies.state === "ok" && <AnomalyTable rows={anomalies.data.anomalies} />}
      </section>

      <section className="section">
        <h3 className="section__title">Budget</h3>
        {budget.state === "loading" && <p className="hint">loading…</p>}
        {budget.state === "err" && <div className="error">{budget.error}</div>}
        {budget.state === "ok" && <BudgetPanel cfg={budget.data} />}
      </section>

      <section className="section">
        <h3 className="section__title">Recent events</h3>
        <p className="hint">
          Tail of <code>~/Library/Logs/mailmind/agent-service.log</code>. Useful
          when chasing a spike — every <code>agent_run_finish</code> event is
          here whether or not the row ever hit the DB.
        </p>
        {logs.state === "loading" && <p className="hint">loading…</p>}
        {logs.state === "err" && <div className="error">{logs.error}</div>}
        {logs.state === "ok" && <LogTable rows={logs.data.events} />}
      </section>
    </>
  );
}

// -------- Summary cards --------

function SummaryCards({ summary }: { summary: ObservabilitySummary }) {
  const { today, errors } = summary;
  return (
    <section className="section observability__cards">
      <div className={`obs-card obs-card--${today.bucket}`}>
        <div className="obs-card__label">today</div>
        <div className="obs-card__value">${today.total_usd.toFixed(4)}</div>
        <div className="obs-card__sub">
          of ${today.cap_usd.toFixed(2)} ({today.pct_of_cap.toFixed(1)}%)
        </div>
        <div className="obs-card__bar">
          <div
            className={`obs-card__bar-fill obs-card__bar-fill--${today.bucket}`}
            style={{ width: `${Math.min(100, today.pct_of_cap)}%` }}
          />
        </div>
      </div>

      <div className="obs-card">
        <div className="obs-card__label">schema-fail rate (7d)</div>
        <div className="obs-card__value">{(errors.schema_fail_rate * 100).toFixed(2)}%</div>
        <div className="obs-card__sub">
          {errors.by_status["schema_fail"] ?? 0} fail / {errors.total} runs
        </div>
      </div>

      <div className="obs-card">
        <div className="obs-card__label">retry rate (7d)</div>
        <div className="obs-card__value">{(errors.retry_rate * 100).toFixed(2)}%</div>
        <div className="obs-card__sub">{errors.total} runs in window</div>
      </div>

      <div className="obs-card">
        <div className="obs-card__label">runs today</div>
        <div className="obs-card__value">
          {today.per_agent.reduce((acc, a) => acc + a.runs, 0)}
        </div>
        <div className="obs-card__sub">{today.per_agent.length} agents active</div>
      </div>
    </section>
  );
}

// -------- Cost chart (no chart lib — small inline SVG) --------

function CostChart({ data }: { data: CostTrajectory }) {
  const maxTotal = Math.max(0.0001, ...data.totals);
  const W = 480;
  const H = 100;
  const padX = 28;
  const padY = 14;
  const innerW = W - padX * 2;
  const barW = innerW / Math.max(1, data.days.length);
  return (
    <div>
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label="7-day cost trajectory" className="obs-chart">
        <line x1={padX} y1={H - padY} x2={W - padX} y2={H - padY} className="obs-chart__axis" />
        {data.days.map((d, i) => {
          const total = data.totals[i] || 0;
          const h = ((H - padY * 2) * total) / maxTotal;
          const x = padX + i * barW + 2;
          const y = H - padY - h;
          return (
            <g key={d}>
              <rect x={x} y={y} width={barW - 4} height={h} className="obs-chart__bar" />
              <text x={x + barW / 2 - 2} y={H - 2} className="obs-chart__tick">
                {d.slice(5)}
              </text>
            </g>
          );
        })}
      </svg>
      {data.totals.every((v) => v === 0) ? (
        <p className="hint">No cost recorded in window — run an agent to populate this chart.</p>
      ) : (
        <div className="obs-chart__legend">
          {data.agents.map((agent) => {
            const totalForAgent = (data.series[agent] ?? []).reduce((a, b) => a + b, 0);
            return (
              <span key={agent} className="obs-chart__legend-item">
                <code>{agent}</code> ${totalForAgent.toFixed(4)}
              </span>
            );
          })}
        </div>
      )}
    </div>
  );
}

// -------- Latency table --------

function LatencyTable({ rows }: { rows: ObservabilitySummary["latency_by_agent"] }) {
  if (!rows.length) {
    return <p className="hint">No latency samples yet — run an agent.</p>;
  }
  return (
    <table className="table">
      <thead>
        <tr>
          <th>agent</th>
          <th>P50</th>
          <th>P95</th>
          <th>runs</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((r) => (
          <tr key={r.agent_name}>
            <td>
              <code>{r.agent_name}</code>
            </td>
            <td>{r.p50_ms} ms</td>
            <td>{r.p95_ms} ms</td>
            <td className="cell--muted">{r.runs}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

// -------- Anomaly table --------

function AnomalyTable({ rows }: { rows: Anomaly[] }) {
  if (!rows.length) {
    return <p className="hint">No anomalies in the rolling window — that's the happy path.</p>;
  }
  return (
    <table className="table">
      <thead>
        <tr>
          <th>when</th>
          <th>agent</th>
          <th>latency</th>
          <th>cost</th>
          <th>status</th>
          <th>reasons</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((r) => (
          <tr key={r.id}>
            <td className="cell--muted">{r.started_at.replace("T", " ").slice(0, 19)}</td>
            <td>
              <code>{r.agent_name}</code>
            </td>
            <td>{r.latency_ms ?? "-"}</td>
            <td>{r.cost_usd != null ? `$${r.cost_usd.toFixed(4)}` : "-"}</td>
            <td>
              <span className={`pill pill--${r.result_status}`}>{r.result_status}</span>
            </td>
            <td className="cell--mono">{r.reasons.join(", ")}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

// -------- Budget panel --------

function BudgetPanel({ cfg }: { cfg: BudgetConfig }) {
  return (
    <div className="obs-budget">
      <div className="obs-budget__row">
        <span className="obs-budget__label">Daily cap</span>
        <span className="obs-budget__value">
          ${cfg.per_day_usd.total.toFixed(2)} (warn at {cfg.per_day_usd.soft_warn_pct}%, stop at{" "}
          {cfg.per_day_usd.hard_stop_pct}%)
        </span>
      </div>
      <div className="obs-budget__row">
        <span className="obs-budget__label">Anomaly thresholds</span>
        <span className="obs-budget__value">
          latency &gt; {cfg.anomalies.latency_outlier_x_p95}× P95, cost &gt;{" "}
          {cfg.anomalies.cost_outlier_x_p95}× P95, window {cfg.anomalies.rolling_window_days} days
        </span>
      </div>
      <table className="table">
        <thead>
          <tr>
            <th>agent</th>
            <th>per-task cap</th>
          </tr>
        </thead>
        <tbody>
          {Object.entries(cfg.per_task_usd)
            .sort(([a], [b]) => a.localeCompare(b))
            .map(([k, v]) => (
              <tr key={k}>
                <td>
                  <code>{k}</code>
                </td>
                <td>${v.toFixed(4)}</td>
              </tr>
            ))}
        </tbody>
      </table>
    </div>
  );
}

// -------- Log tail --------

function LogTable({ rows }: { rows: LogEvent[] }) {
  if (!rows.length) {
    return <p className="hint">No events tailed yet — start the agent service to populate this view.</p>;
  }
  // Newest at the top.
  const ordered = [...rows].reverse();
  return (
    <table className="table">
      <thead>
        <tr>
          <th>ts</th>
          <th>event</th>
          <th>agent</th>
          <th>status</th>
          <th>cost</th>
          <th>latency</th>
        </tr>
      </thead>
      <tbody>
        {ordered.map((r, i) => (
          <tr key={`${r.ts ?? ""}-${i}`}>
            <td className="cell--muted">{(r.ts ?? "").slice(0, 19).replace("T", " ")}</td>
            <td>
              <code>{r.event ?? r.level ?? ""}</code>
            </td>
            <td>{r.agent_name ?? ""}</td>
            <td>
              {r.result_status ? (
                <span className={`pill pill--${r.result_status}`}>{r.result_status}</span>
              ) : (
                ""
              )}
            </td>
            <td>{r.cost_usd != null ? `$${(r.cost_usd as number).toFixed(4)}` : ""}</td>
            <td>{r.latency_ms != null ? `${r.latency_ms} ms` : ""}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
