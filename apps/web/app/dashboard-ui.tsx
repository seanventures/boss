"use client";

import { type DashboardSnapshot } from "../lib/dashboard";

function usd(micros: number): string {
  return `$${(micros / 1_000_000).toLocaleString(undefined, { maximumFractionDigits: 2 })}`;
}

function tone(status?: string): string {
  const s = (status ?? "").toLowerCase();
  if (s === "pass" || s === "green" || s === "healthy") return "#3fb950";
  if (s === "fail" || s === "red") return "#f85149";
  if (s === "orange" || s === "yellow" || s === "amber") return "#d29922";
  return "#8b949e";
}

function Tile({ label, value, hint, color }: { label: string; value: string | number; hint?: string; color?: string }) {
  return (
    <article className="stack-card" style={{ minWidth: 0 }}>
      <div className="detail" style={{ textTransform: "uppercase", letterSpacing: 0.4, fontSize: 11 }}>{label}</div>
      <div style={{ fontSize: 26, fontWeight: 700, color: color ?? "var(--ink)", lineHeight: 1.1, marginTop: 4 }}>{value}</div>
      {hint ? <div className="detail" style={{ marginTop: 4 }}>{hint}</div> : null}
    </article>
  );
}

export function DashboardPanel({ snapshot }: { snapshot: DashboardSnapshot }) {
  if (!snapshot) {
    return (
      <article className="stack-card">
        <h3 style={{ margin: 0 }}>Live dashboard</h3>
        <p className="detail">Dashboard snapshot unavailable — is the control plane reachable?</p>
      </article>
    );
  }
  const s = snapshot;
  const capPct = s.spend.capUsdMicros
    ? Math.round(((s.spend.reservedUsdMicros + s.spend.actualUsdMicros) / s.spend.capUsdMicros) * 100)
    : 0;
  return (
    <div style={{ display: "grid", gap: 14 }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", gap: 10, flexWrap: "wrap" }}>
        <p className="detail" style={{ margin: 0 }}>
          One consolidated, cross-phase snapshot — budget, queue, work, incidents, provider health,
          proof suites, spend, and the no-silent-drops backlog. Auto-refreshes live.
        </p>
        <span className="detail" style={{ fontFamily: "var(--mono, monospace)", fontSize: 11 }}>cursor {s.cursor}</span>
      </div>

      <div className="grid" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(150px, 1fr))", gap: 12 }}>
        <Tile label="Budget" value={s.budgetState.toUpperCase()} color={tone(s.budgetState)} hint={`${capPct}% of cap committed`} />
        <Tile label="Spend (actual)" value={usd(s.spend.actualUsdMicros)} hint={`run-rate ${usd(s.spend.runRate1hUsdMicros)}/h`} />
        <Tile label="Active jobs" value={s.queue.leased} hint={`${s.queue.queued} queued · ${s.queue.retryable} retryable`} />
        <Tile label="Open tasks" value={s.workload.openTasks} hint={`${s.workload.completedTasks} completed`} />
        <Tile
          label="Pending decisions"
          value={s.workload.pendingDecisions}
          color={s.workload.highRiskPending > 0 ? tone("amber") : undefined}
          hint={`${s.workload.highRiskPending} high-risk`}
        />
        <Tile
          label="Incidents"
          value={s.incidents.deadletters + s.incidents.blockedOutbox}
          color={s.incidents.deadletters + s.incidents.blockedOutbox > 0 ? tone("red") : tone("green")}
          hint={`${s.incidents.deadletters} deadletters · ${s.incidents.blockedOutbox} blocked`}
        />
        <Tile
          label="Provider health"
          value={`${s.providerHealth.healthy}/${s.providerHealth.total}`}
          color={s.providerHealth.total && s.providerHealth.healthy < s.providerHealth.total ? tone("amber") : tone("green")}
          hint="model lanes healthy"
        />
        <Tile
          label="Ack backlog"
          value={s.ackBacklog}
          color={s.ackBacklog > 0 ? tone("amber") : tone("green")}
          hint="inbound not yet terminal"
        />
        <Tile
          label="Canary"
          value={s.canary ? s.canary.status.toUpperCase() : "—"}
          color={s.canary ? tone(s.canary.status) : undefined}
          hint={s.canary ? `${s.canary.passed} pass · ${s.canary.failed} fail` : "no runs yet"}
        />
        <Tile
          label="Eval harness"
          value={s.evals ? s.evals.status.toUpperCase() : "—"}
          color={s.evals ? tone(s.evals.status) : undefined}
          hint={s.evals ? `${s.evals.passed} pass · ${s.evals.failed} fail` : "no runs yet"}
        />
      </div>
    </div>
  );
}
