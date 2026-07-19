"use client";

import { type DashboardSnapshot } from "../lib/dashboard";

function usd(micros: number): string {
  return `$${(micros / 1_000_000).toLocaleString(undefined, { maximumFractionDigits: 2 })}`;
}

// State → signal color (design tokens). Status renders as colored mono text, never a pill.
function tone(status?: string): string {
  const s = (status ?? "").toLowerCase();
  if (s === "pass" || s === "green" || s === "healthy" || s === "verified") return "var(--green)";
  if (s === "fail" || s === "red" || s === "blocked") return "var(--red)";
  if (s === "orange" || s === "yellow" || s === "amber" || s === "awaiting") return "var(--amber)";
  if (s === "running") return "var(--teal)";
  return "var(--muted)";
}

function Kpi({ label, value, color, hint }: { label: string; value: string | number; color?: string; hint?: string }) {
  return (
    <div className="kpi-cell">
      <div className="kpi-label">{label}</div>
      <div className="kpi-value" style={color ? { color } : undefined}>{value}</div>
      {hint ? <div className="kpi-hint">{hint}</div> : null}
    </div>
  );
}

function StatusText({ label, color, pulse }: { label: string; color: string; pulse?: boolean }) {
  return <span className={`status-text${pulse ? " pulse" : ""}`} style={{ color }}>{label}</span>;
}

export function DashboardPanel({ snapshot }: { snapshot: DashboardSnapshot }) {
  if (!snapshot) {
    return (
      <article className="stack-card">
        <h3 style={{ margin: 0 }}>Mission Dashboard</h3>
        <p className="detail">Dashboard snapshot unavailable — is the control plane reachable?</p>
      </article>
    );
  }
  const s = snapshot;
  const avail = Math.max(s.spend.capUsdMicros - s.spend.reservedUsdMicros - s.spend.actualUsdMicros, 0);
  const incidents = s.incidents.deadletters + s.incidents.blockedOutbox + s.incidents.deadletteredOutbox;
  const running = s.queue.leased;

  return (
    <div style={{ display: "grid", gap: 16 }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", gap: 10, flexWrap: "wrap" }}>
        <p className="detail" style={{ margin: 0 }}>
          One consolidated, cross-phase snapshot — budget, queue, work, proof, and health. Auto-refreshes live.
        </p>
        <span className="detail" style={{ fontFamily: "var(--mono)", fontSize: 11 }}>cursor {s.cursor}</span>
      </div>

      {/* KPI strip — one bordered row, hairline-divided cells (not separate cards) */}
      <div className="kpi-strip">
        <Kpi label="Budget" value={s.budgetState.toUpperCase()} color={tone(s.budgetState)} hint={`${usd(avail)} available`} />
        <Kpi label="Awaiting You" value={s.workload.pendingDecisions} color={s.workload.pendingDecisions > 0 ? "var(--amber)" : undefined} hint={`${s.workload.highRiskPending} high-risk`} />
        <Kpi label="Open Tasks" value={s.workload.openTasks} hint={`${s.workload.completedTasks} done`} />
        <Kpi label="Running" value={running} color={running > 0 ? "var(--teal)" : undefined} hint={`${s.queue.queued} queued · ${s.queue.retryable} retry`} />
        <Kpi label="Run-rate 1h" value={usd(s.spend.runRate1hUsdMicros)} hint={`of ${usd(s.spend.capUsdMicros)} cap`} />
        <Kpi label="Canary" value={s.canary ? s.canary.status.toUpperCase() : "—"} color={s.canary ? tone(s.canary.status) : undefined} hint={s.canary ? `${s.canary.passed}/${s.canary.failed}/${s.canary.skipped}` : "not run"} />
      </div>

      {/* Health & signals — hairline-divided rows, status as colored mono text */}
      <article className="stack-card" style={{ padding: 0, overflow: "hidden" }}>
        <div style={{ padding: "13px 16px", borderBottom: "1px solid var(--border)" }}>
          <span className="eyebrow">Health &amp; Signals</span>
        </div>
        <div className="hairline-list">
          <div className="hairline-row">
            <span className="detail" style={{ color: "var(--ink-soft)" }}>Provider health</span>
            <StatusText label={`${s.providerHealth.healthy}/${s.providerHealth.total} HEALTHY`}
              color={s.providerHealth.total && s.providerHealth.healthy < s.providerHealth.total ? "var(--amber)" : "var(--green)"} />
          </div>
          <div className="hairline-row">
            <span className="detail" style={{ color: "var(--ink-soft)" }}>Incidents / deadletters</span>
            <StatusText label={incidents > 0 ? `${incidents} OPEN` : "NONE"} color={incidents > 0 ? "var(--red)" : "var(--green)"} />
          </div>
          <div className="hairline-row">
            <span className="detail" style={{ color: "var(--ink-soft)" }}>Ack backlog (no silent drops)</span>
            <StatusText label={s.ackBacklog > 0 ? `${s.ackBacklog} PENDING` : "CLEAR"} color={s.ackBacklog > 0 ? "var(--amber)" : "var(--green)"} />
          </div>
          <div className="hairline-row">
            <span className="detail" style={{ color: "var(--ink-soft)" }}>Eval harness</span>
            <StatusText label={s.evals ? `${s.evals.status.toUpperCase()} · ${s.evals.passed}/${s.evals.failed}` : "NOT RUN"}
              color={s.evals ? tone(s.evals.status) : "var(--muted)"} />
          </div>
        </div>
      </article>
    </div>
  );
}
