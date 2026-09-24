"use client";

import { useActionState } from "react";
import { type ChaosRun } from "../lib/chaos";
import {
  describeFinopsSpendHonesty,
  type CommitmentReconciliation,
  type FinopsSummary,
} from "../lib/finops";
import {
  type ActionResult,
  runChaosAction,
  reconcileCommitmentsAction,
  requestInitiativeBudgetAction,
} from "./actions";
import { collectSoc2EvidenceAction } from "./actions-paid";
import { SubmitButton } from "./submit-button";

function usd(micros: number): string {
  return `$${(micros / 1_000_000).toLocaleString("en-US", { maximumFractionDigits: 2 })}`;
}
function verdictTone(v: string): string {
  if (v === "on_track" || v === "recovered") return "#3fb950";
  if (v === "under_utilized" || v === "over_utilized") return "#d29922";
  return "#8b949e";
}

// --- Chaos / load harness ---
export function ChaosCard({ projectId, runs }: { projectId: string; runs: ChaosRun[] }) {
  return (
    <article className="stack-card">
      <h3 style={{ margin: 0 }}>Chaos &amp; resilience</h3>
      <p className="detail">
        Inject controlled faults against live primitives and record recovery: lease reclaim (crashed
        worker), and deadletter storm (anomaly detector raises an incident).
      </p>
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
        {["lease_reclaim", "deadletter_storm"].map((scenario) => (
          <form action={runChaosAction} key={scenario}>
            <input type="hidden" name="project_id" value={projectId} />
            <input type="hidden" name="scenario" value={scenario} />
            <SubmitButton className="button secondary" type="submit">Inject {scenario}</SubmitButton>
          </form>
        ))}
      </div>
      {runs.length > 0 ? (
        <div className="feed-list" style={{ marginTop: 8 }}>
          {runs.slice(0, 6).map((r) => (
            <div className="feed-row" key={r.id}>
              <div>
                <h4 style={{ margin: 0 }}>{r.scenario}</h4>
                <p className="detail">{r.detail}</p>
              </div>
              <span className="pill" style={{ color: r.recovered ? "#3fb950" : "#f85149" }}>
                {r.recovered ? "recovered" : "failed"}
              </span>
            </div>
          ))}
        </div>
      ) : null}
    </article>
  );
}

// --- InfraCommitment reconciler + tier-2 initiative budget approval ---
export function FinopsCard({
  projectId,
  reconciliations,
  summary,
}: {
  projectId: string;
  reconciliations: CommitmentReconciliation[];
  summary?: FinopsSummary | null;
}) {
  const [initState, initAction, initPending] = useActionState(requestInitiativeBudgetAction, null);
  const honesty = describeFinopsSpendHonesty(summary);
  const maxProviderSpend = Math.max(
    1,
    ...(summary?.spendByProvider ?? []).map((row) => row.costUsdMicros),
  );
  return (
    <article className="stack-card">
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 10 }}>
        <h3 style={{ margin: 0 }}>FinOps — commitments &amp; initiatives</h3>
        <form action={reconcileCommitmentsAction}>
          <input type="hidden" name="project_id" value={projectId} />
          <SubmitButton className="button secondary" type="submit">Reconcile commitments</SubmitButton>
        </form>
      </div>
      <p className="detail">
        Score each commitment’s utilization vs actual usage (on-track / under / over → review
        decision), and gate tier-2+ initiative budgets above the approval threshold.
      </p>
      <div
        data-testid="finops-spend-honesty"
        style={{
          display: "grid",
          gap: 8,
          marginBottom: 10,
          padding: "8px 0",
          borderBottom: "1px solid var(--hair, #131d34)",
        }}
      >
        <div style={{ display: "flex", justifyContent: "space-between", gap: 12, fontSize: 12.5 }}>
          <span className="detail" style={{ margin: 0 }}>
            {honesty.label}
          </span>
          <b data-testid="finops-spend-amount">
            {honesty.amountUsdMicros == null ? "—" : usd(honesty.amountUsdMicros)}
          </b>
        </div>
        <p className="detail" style={{ margin: 0 }}>
          {honesty.helper}
        </p>
        {summary?.apiReachable ? (
          <div style={{ display: "flex", justifyContent: "space-between", gap: 12, fontSize: 12.5 }}>
            <span className="detail" style={{ margin: 0 }}>
              Open cost anomalies
            </span>
            <b
              style={{
                color:
                  summary.anomaliesOpenCount > 0
                    ? "var(--amber, #d29922)"
                    : "var(--muted, #8b949e)",
              }}
            >
              {summary.anomaliesOpenCount}
            </b>
          </div>
        ) : null}
        {honesty.kind === "recorded"
          ? (summary?.spendByProvider ?? []).slice(0, 4).map((row) => (
              <div key={row.provider} style={{ display: "grid", gap: 3 }}>
                <div style={{ display: "flex", justifyContent: "space-between", fontSize: 12 }}>
                  <span style={{ fontFamily: "var(--mono, monospace)" }}>{row.provider}</span>
                  <span>
                    <b>{usd(row.costUsdMicros)}</b>
                    <span style={{ color: "var(--muted, #8b949e)" }}> · {row.eventCount} evt</span>
                  </span>
                </div>
                <div
                  aria-hidden
                  style={{
                    height: 3,
                    background: "var(--hair, #131d34)",
                    position: "relative",
                  }}
                >
                  <div
                    style={{
                      position: "absolute",
                      inset: 0,
                      width: `${Math.round((row.costUsdMicros / maxProviderSpend) * 100)}%`,
                      background: "var(--teal, #2ec5c5)",
                    }}
                  />
                </div>
              </div>
            ))
          : null}
      </div>
      {reconciliations.length > 0 ? (
        <div className="feed-list">
          {reconciliations.slice(0, 6).map((r) => (
            <div className="feed-row" key={r.id}>
              <div>
                <h4 style={{ margin: 0 }}>{r.provider} · {r.commitmentId}</h4>
                <p className="detail">
                  {usd(r.actualUsdMicros)} / {usd(r.committedUsdMicros)} ({Math.round(r.utilization * 100)}% used)
                  {r.decisionId ? ` · decision ${r.decisionId}` : ""}
                </p>
              </div>
              <span className="pill" style={{ color: verdictTone(r.verdict) }}>{r.verdict}</span>
            </div>
          ))}
        </div>
      ) : (
        <p className="detail">No reconciliations yet — click “Reconcile commitments”.</p>
      )}

      <form action={initAction} className="composer compact" style={{ marginTop: 10 }}>
        <input type="hidden" name="project_id" value={projectId} />
        <div style={{ display: "flex", gap: 8 }}>
          <label className="field" style={{ flex: 2 }}>
            <span>Initiative title</span>
            <input name="title" className="input" placeholder="New GPU cluster" required />
          </label>
          <label className="field" style={{ flex: 1 }}>
            <span>Tier</span>
            <input name="tier" className="input" type="number" min={0} max={3} defaultValue={2} />
          </label>
          <label className="field" style={{ flex: 1 }}>
            <span>Amount (USD)</span>
            <input name="amount_usd" className="input" type="number" min={0} defaultValue={100} />
          </label>
        </div>
        <SubmitButton className="button secondary" type="submit" disabled={initPending}>
          {initPending ? "Requesting…" : "Request initiative budget"}
        </SubmitButton>
      </form>
      {initState && !initState.ok ? (
        <p className="detail" style={{ color: "#f85149" }}>{initState.error}</p>
      ) : null}
      {initState?.ok ? (
        <p className="detail" style={{ marginTop: 6 }}>
          <b style={{ color: initState.data?.approval_required ? "#d29922" : "#3fb950" }}>
            {String(initState.data?.status)}
          </b>
          {initState.data?.decision_id ? ` · decision ${String(initState.data.decision_id)}` : ""}
        </p>
      ) : null}
    </article>
  );
}

// --- SOC2 evidence bundle ---
export function Soc2Card({ projectId }: { projectId: string }) {
  const [state, action, pending] = useActionState(collectSoc2EvidenceAction, null);
  const summary = state?.ok ? (state.data?.summary as Record<string, unknown> | undefined) : undefined;
  const controls = state?.ok ? (state.data?.controls as Array<Record<string, unknown>> | undefined) : undefined;
  return (
    <article className="stack-card">
      <h3 style={{ margin: 0 }}>SOC2 evidence bundle</h3>
      <p className="detail">
        Generate a Trust Services Common-Criteria evidence bundle from live audit, policy-eval,
        break-glass, change-management, and canary/eval data.
      </p>
      <form action={action}>
        <input type="hidden" name="project_id" value={projectId} />
        <SubmitButton className="button secondary" type="submit" disabled={pending}>
          {pending ? "Collecting…" : "Collect SOC2 evidence"}
        </SubmitButton>
      </form>
      {state && !state.ok ? <p className="detail" style={{ color: "#f85149" }}>{state.error}</p> : null}
      {summary ? (
        <p className="detail" style={{ marginTop: 6 }}>
          <b style={{ color: "#3fb950" }}>{String(summary.controls_satisfied)}</b>/{String(summary.controls_total)} controls satisfied
          {" "}· artifact {String(state?.data?.artifact_id ?? "")}
        </p>
      ) : null}
      {controls ? (
        <div style={{ display: "grid", gap: 4, marginTop: 6 }}>
          {controls.map((c) => (
            <div key={String(c.id)} style={{ fontSize: 12.5, color: "var(--ink-soft)" }}>
              <span style={{ color: c.status === "satisfied" ? "#3fb950" : "#d29922" }}>●</span>{" "}
              <b style={{ color: "var(--ink)" }}>{String(c.id)}</b>{" "}
              <span style={{ color: "var(--muted)" }}>{String(c.name)}</span>
            </div>
          ))}
        </div>
      ) : null}
    </article>
  );
}
