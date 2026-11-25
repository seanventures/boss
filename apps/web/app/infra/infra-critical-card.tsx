"use client";

/**
 * S2 · "INFRA AGENT · CRITICAL WORK IN PROGRESS" on the Command Center (BOSSAPP-1156). The
 * incident as a flow (InfraFlow from the API's flow nodes), the progress to target, then
 * "Cleaning, safest first" with a mark per step, Paused work and Receipts as small cards.
 * The tier 3 question opens in place from its step's Review (S4, one read of the decision);
 * the prose sits behind Details. Live: the same 10 s poll as the strip, restarted by SSE
 * `storage.*` events.
 */
import React, { useState } from "react";
import { Check, CircleNotch, CurrencyDollar, Eye, Prohibit, ShieldCheck, UserCircle, Warning, WifiSlash } from "@phosphor-icons/react";

import { clock, etaWords, gb, progressToTarget, reportPatch, useInfraIncident, type PlanStep, type StorageIncident } from "../../lib/infra-storage";
import { AgentAvatar } from "../agent-avatar";
import { InfraFlow } from "./infra-flow";
import { StripLink } from "./infra-strip-banner";
import { StorageDeleteByDecision } from "./storage-delete-decision";

const RUNS_PATCH = { area: "delivery_workflows", role: null, view: null };

function StepMark({ s }: { s: PlanStep }) {
  if (s.state === "done") return <span className="icc-mark" data-state="done" aria-hidden><Check size={13} weight="bold" /></span>;
  if (s.state === "active") return <span className="icc-mark icc-spin" data-state="active" aria-hidden><CircleNotch size={13} weight="bold" /></span>;
  if (s.state === "ask") return <span className="icc-mark" data-state="ask" aria-hidden><UserCircle size={13} weight="bold" /></span>;
  if (s.state === "failed") return <span className="icc-mark" data-state="failed" aria-hidden><Warning size={13} weight="bold" /></span>;
  if (s.state === "skipped") return <span className="icc-mark" data-state="skipped" aria-hidden><Prohibit size={13} weight="bold" /></span>;
  return <span className="icc-mark" data-state="waiting" aria-hidden />;
}

const STATE_WORD: Record<PlanStep["state"], string> = { done: "done", active: "cleaning now", waiting: "waiting", ask: "asks you first", failed: "failed", skipped: "skipped" };

/** The GB a step shows: what it freed once it ran, the dry run's count before. */
export function stepGb(s: PlanStep): string {
  if (s.state === "done") return gb(s.freedGb || s.gb);
  if (s.state === "skipped") return "—";
  return s.gb === null ? "…" : gb(s.gb);
}

export function InfraCriticalCardView({ incident, projectId, actor, orgId, sample }: {
  incident: StorageIncident; projectId: string; actor: string; orgId?: string | null; sample?: boolean;
}) {
  const [reviewing, setReviewing] = useState(false);
  const progress = progressToTarget(incident);
  const eta = etaWords(incident.etaSeconds);
  const resolved = incident.status === "resolved";
  const asking = !resolved && incident.askDecisionId;
  return (
    <section className="icc cc-card" aria-labelledby="icc-h" data-testid="infra-critical-card" data-status={incident.status}>
      <header className="icc-head">
        <span className="icc-face" aria-hidden><AgentAvatar agent="infra_steward" size={40} aiBadge={false} state={resolved ? "idle" : "working"} /></span>
        <span className="icc-title">
          <span className="icc-title-row">
            <h2 id="icc-h">Infra Agent · {resolved ? "storage incident resolved" : "critical work in progress"}</h2>
            <span className="ia-chip" data-tone={resolved ? "green" : incident.severity === "warn" ? "amber" : "red"} data-testid="icc-sev">
              <Warning size={11} weight="bold" aria-hidden />{resolved ? "Resolved" : incident.severity === "warn" ? "Early" : "Critical"}</span>
            {!incident.helper.reachable ? <span className="ia-chip" data-tone="amber" data-testid="icc-helper" title="The host helper has not reported for 10 minutes; nothing is deleted until it is back"><WifiSlash size={11} aria-hidden />helper unavailable</span> : null}
            {sample ? <span className="ia-sample">Sample data</span> : null}
          </span>
          <div className="icc-cause" data-testid="icc-cause">
            Storage on <code>{incident.hostLabel}</code> fell to <b>{incident.freePctStart !== null ? `${Math.round(incident.freePctStart)} % free` : "critical"}</b>.
            {incident.causeText ? <> Cause: <b>{incident.causeText.replace(/\.+$/, "")}</b>.</> : null}
          </div>
        </span>
        <span className="icc-big" data-testid="icc-freed">
          <b>{gb(incident.freedGb)}</b>
          <span>freed{resolved ? "" : " so far"}{eta ? ` · ${eta}` : ""}</span>
        </span>
      </header>

      <InfraFlow flow={incident.flow} size="card" testId="icc-flow" />

      <div className="icc-progress" role="img" aria-label={`${incident.freePctNow !== null ? Math.round(incident.freePctNow) : "?"} % free, target ${incident.targetPct} %`} data-testid="icc-progress">
        <span className="icc-progress-l">{incident.freePctStart !== null ? `${Math.round(incident.freePctStart)} %` : "—"} → {incident.targetPct} % target</span>
        <svg className="icc-bar" viewBox="0 0 100 1" preserveAspectRatio="none" aria-hidden><rect className="icc-bar-fill" data-done={progress >= 1 || undefined} x="0" y="0" width={Math.round(progress * 100)} height="1" /></svg>
        <span className="icc-progress-r">{incident.freePctNow !== null ? `${Math.round(incident.freePctNow)} % free` : ""}</span>
      </div>

      <div className="icc-grid">
        <div className="icc-box">
          <span className="icc-label">Cleaning, safest first</span>
          <ol className="icc-steps" data-testid="icc-steps">
            {incident.plan.map((s) => (
              <li key={s.key} className="icc-step" data-state={s.state} data-tier={s.tier} data-testid="icc-step" title={`${s.label}: ${s.detail || STATE_WORD[s.state]} · ${STATE_WORD[s.state]}`}>
                <StepMark s={s} />
                <span className="icc-step-text">
                  <b>{s.label}</b>
                  <span>{s.detail}</span>
                </span>
                <span className="icc-step-gb">{stepGb(s)}</span>
                {s.state === "ask" && s.decisionId ? (
                  <button type="button" className="icc-btn" data-testid="icc-review" aria-expanded={reviewing}
                          onClick={() => setReviewing((v) => !v)}>{reviewing ? "Hide" : "Review"}</button>
                ) : null}
              </li>
            ))}
            {!incident.plan.length ? <li className="icc-faint">The agent is still planning: the dry runs are counting.</li> : null}
          </ol>
        </div>
        <div className="icc-side">
          <div className="icc-box">
            <span className="icc-label">Paused work</span>
            <b data-testid="icc-paused">{incident.pausedRuns} run{incident.pausedRuns === 1 ? "" : "s"}{incident.hold.active ? " · new work not claimed" : ""}</b>
            <span className="icc-faint">{resolved || !incident.hold.active ? "Every run resumed from its checkpoint." : "Stopped at a checkpoint. Resume is automatic, oldest first."}</span>
            <StripLink patch={RUNS_PATCH} orgId={orgId} className="icc-link" testId="icc-see-runs">See the runs →</StripLink>
          </div>
          <div className="icc-box">
            <span className="icc-label">Receipts</span>
            <span className="icc-faint">Every action is a receipted tool call. <b>{incident.receipts} so far</b></span>
            <span className="icc-chips">
              <span className="ia-chip" data-tone="green"><Eye size={11} aria-hidden />dry-run shown first</span>
              <span className="ia-chip" data-tone="green"><ShieldCheck size={11} aria-hidden />labelled items only</span>
              <span className="ia-chip" data-tone="grey"><CurrencyDollar size={11} aria-hidden />${incident.spendUsd.toFixed(2)} spent</span>
            </span>
          </div>
        </div>
      </div>

      {asking && reviewing && incident.askDecisionId ? (
        <div className="icc-review" data-testid="icc-review-card">
          <StorageDeleteByDecision decisionId={incident.askDecisionId} projectId={projectId} actor={actor} why={incident.causeText}
                                   density="card" onAnswered={() => setReviewing(false)} />
        </div>
      ) : null}

      <details className="icc-details" data-testid="icc-details">
        <summary>Details</summary>
        <dl className="icc-kv">
          <dt>Opened</dt><dd>{clock(incident.openedAt) || "—"}</dd>
          {incident.acknowledgedBy ? <><dt>Acknowledged</dt><dd>{incident.acknowledgedBy}{incident.acknowledgedAt ? ` · ${clock(incident.acknowledgedAt)}` : ""}</dd></> : null}
          <dt>Target</dt><dd>{incident.targetPct} % free; the hold lifts and paused work resumes oldest first</dd>
          <dt>Tiers</dt><dd>1 rebuildable, automatic · 2 labelled and past an approved rule, automatic · 3 irreversible or unlabelled, one card</dd>
          {incident.causeKey ? <><dt>Cause key</dt><dd><code>{incident.causeKey}</code></dd></> : null}
          <dt>Report</dt><dd><StripLink patch={reportPatch(incident.id, incident.hostId)} orgId={orgId} className="icc-link" testId="icc-report">Incident report →</StripLink></dd>
        </dl>
      </details>
    </section>
  );
}

/** The card, live. Nothing while the host is fine. */
export function InfraCriticalCard({ projectId, actor, orgId }: { projectId: string; actor: string; orgId?: string | null }) {
  const { incident, sample } = useInfraIncident(projectId);
  if (!incident || incident.status === "resolved") return null;
  return <InfraCriticalCardView incident={incident} projectId={projectId} actor={actor} orgId={orgId} sample={sample} />;
}
