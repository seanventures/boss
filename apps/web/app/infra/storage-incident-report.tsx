"use client";

/**
 * S5 · THE INCIDENT REPORT (BOSSAPP-1156): what happened start to finish as a flow with times,
 * four numbers (Freed · Work paused · Lost or repeated = 0 · Cost), the cause in plain words,
 * what the agent learned (a rule added, a rule proposed with Review), what was removed and by
 * whose say-so with the receipts link, and the paused work. Read from
 * GET /v1/infra/storage/incidents/{id} (docs/infra/INFRA_AGENT_API.md §2, §6); the incident's
 * `report` fills in once it is resolved. Export hands over the API's Markdown or the JSON.
 */
import React, { useEffect, useState } from "react";
import { ArrowRight, Broom, Check, Clock, CurrencyDollar, Download, Pause, Play, Shield, Sparkle, User } from "@phosphor-icons/react";

import { formatDate } from "../../lib/datetime";
import { clock, gb, incidentMarkdownUrl, storagePatch, useIncidentDetail, type IncidentDetail } from "../../lib/infra-storage";
import { logHref } from "../../lib/log-url";
import { InfraFlow } from "./infra-flow";
import { StripLink } from "./infra-strip-banner";
import { showToast } from "../client-forms";

/** "19 min", "40 s", "1 h 05 min". */
export function minutesWord(seconds: number): string {
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return `${s} s`;
  const m = Math.round(s / 60);
  if (m < 60) return `${m} min`;
  return `${Math.floor(m / 60)} h ${String(m % 60).padStart(2, "0")} min`;
}

const BY_WORD: Record<string, string> = { person: "approved by a person", rule: "by rule", rebuildable: "automatic" };

/** The report as Markdown (the fallback when the API's report.md is not there). */
export function reportMarkdown(d: IncidentDetail): string {
  const i = d.incident;
  const r = i.report;
  const flow = r?.timeline.length ? r.timeline : i.flow;
  const lines = [
    `# Storage incident · ${i.hostLabel}`,
    `${formatDate(i.openedAt, "")} · ${clock(i.openedAt)}${i.resolvedAt ? ` to ${clock(i.resolvedAt)}` : ""} · ${i.status}`,
    "",
    ...(r?.summary ? [r.summary, ""] : []),
    "## What happened",
    ...flow.map((n) => `- ${n.label}${n.detail ? `: ${n.detail}` : ""}${n.at ? ` (${clock(n.at)})` : ""}`),
    "",
    `- Freed: ${gb(r?.freedGb ?? i.freedGb)} (${i.freePctStart ?? "?"} % → ${i.freePctNow ?? "?"} % free)`,
    `- Work paused: ${r ? `${r.pausedRuns} runs · longest wait ${minutesWord(r.waitedSecondsMax)}` : `${i.pausedRuns} runs`}`,
    `- Lost or repeated: ${r?.lostOrRepeated ?? 0}`,
    `- Cost: $${(r?.costUsd ?? i.spendUsd).toFixed(2)} · ${d.toolCalls} tool calls`,
    "",
    "## Cause", r?.cause.text || i.causeText, ...(r?.cause.chips.length ? ["", r.cause.chips.map((c) => `\`${c}\``).join(" ")] : []),
    "",
    "## Learned",
    ...(r?.learned.ruleAdded ? [`- Rule added: ${r.learned.ruleAdded.label} · ${r.learned.ruleAdded.words}`] : []),
    ...(r?.learned.ruleProposed ? [`- Proposed: ${r.learned.ruleProposed.kind} · ${r.learned.ruleProposed.words}`] : []),
    ...(!r?.learned.ruleAdded && !r?.learned.ruleProposed ? ["- Nothing new this time."] : []),
    "",
    "## What was removed", ...(r?.removed ?? []).map((x) => `- ${x.type}${x.detail ? ` · ${x.detail}` : ""} · ${BY_WORD[x.by]} · ${gb(x.gb)}`),
    "",
    "## Paused work", ...d.pausedWork.map((p) => `- ${p.title}${p.runId ? ` (#${p.runId})` : ""} · waited ${minutesWord(p.waitedSeconds)} · ${p.resumedAt ? "resumed" : "waiting"}`),
  ];
  return lines.join("\n");
}

function handOver(name: string, text: string, type: string) {
  try {
    const url = URL.createObjectURL(new Blob([text], { type }));
    const a = document.createElement("a");
    a.href = url; a.download = name; a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  } catch { /* none */ }
}

export function StorageIncidentReportView({ d, orgId, sample }: { d: IncidentDetail; orgId?: string | null; sample?: boolean }) {
  const i = d.incident;
  const r = i.report;
  const resolved = i.status === "resolved";
  const date = formatDate(i.openedAt, "");
  const flow = r?.timeline.length ? r.timeline : i.flow;
  const removed = r?.removed ?? [];
  // Links that keep who you are (org=) are built from the URL, so only after mount.
  const [receiptsHref, setReceiptsHref] = useState<string>("");
  useEffect(() => { setReceiptsHref(logHref({ kind: "storage", subject: i.id, run_name: `Storage incident · ${i.hostLabel}` })); }, [i.id, i.hostLabel]);
  const exportMd = async () => {
    if (!sample) {
      try {
        const res = await fetch(incidentMarkdownUrl(i.id), { cache: "no-store" });
        if (res.ok) { handOver(`storage-incident-${i.id}.md`, await res.text(), "text/markdown"); return; }
      } catch { /* the console's own words below */ }
    }
    handOver(`storage-incident-${i.id}.md`, reportMarkdown(d), "text/markdown");
    if (!sample) showToast("success", "Exported from the console's view; the API's report.md was not available");
  };
  return (
    <article className="sir" data-testid="storage-incident-report" data-status={i.status}>
      <header className="sir-head">
        <span className="sc-crumb">
          <StripLink patch={storagePatch(i.hostId)} orgId={orgId} className="sc-link" testId="sir-back">Govern › Infrastructure › Storage</StripLink>
          <span aria-hidden> › </span>Incidents
        </span>
        <span className="sc-title-row">
          <span>
            <h2 className="sc-title">Storage incident · {i.hostLabel}</h2>
            <div className="sc-sub">{date}{date ? " · " : ""}{clock(i.openedAt)}{resolved ? ` to ${clock(i.resolvedAt)} · resolved by the Infra Agent` : " · still open"}</div>
          </span>
          <span className="sc-spacer" />
          <span className="ia-chip" data-tone={resolved ? "green" : "red"} data-testid="sir-status"><Check size={11} weight="bold" aria-hidden />{resolved ? "Resolved" : i.status}</span>
          <details className="sir-export">
            <summary className="sc-btn" data-testid="sir-export"><Download size={14} aria-hidden /> Export</summary>
            <span className="sir-export-menu">
              <button type="button" className="linklike" data-testid="sir-export-md" onClick={() => void exportMd()}>Markdown</button>
              <button type="button" className="linklike" data-testid="sir-export-json" onClick={() => handOver(`storage-incident-${i.id}.json`, JSON.stringify(d, null, 2), "application/json")}>JSON</button>
            </span>
          </details>
          {sample ? <span className="ia-sample">Sample data</span> : null}
        </span>
      </header>

      <section className="sc-card" aria-label="What happened, start to finish">
        <span className="sc-label">What happened, start to finish</span>
        <InfraFlow flow={flow} size="report" testId="sir-flow" />
        {r?.summary ? <div className="sir-cause" data-testid="sir-summary">{r.summary}</div> : null}
      </section>

      <div className="sc-tiles" data-testid="sir-tiles">
        <div className="sc-tile" data-tone="green"><span className="sc-tile-label"><Broom size={13} aria-hidden /> Freed</span><b className="sc-tile-n">{gb(r?.freedGb ?? i.freedGb)}</b><span className="sc-tile-sub">{i.freePctStart ?? "?"} % → {i.freePctNow ?? "?"} % free</span></div>
        <div className="sc-tile" data-tone="amber"><span className="sc-tile-label"><Pause size={13} aria-hidden /> Work paused</span><b className="sc-tile-n">{r ? minutesWord(r.durationSeconds) : minutesWord(i.hold.since && i.hold.releasedAt ? (Date.parse(i.hold.releasedAt) - Date.parse(i.hold.since)) / 1000 : 0)}</b><span className="sc-tile-sub">{r?.pausedRuns ?? i.pausedRuns} runs{r ? ` · longest wait ${minutesWord(r.waitedSecondsMax)}` : ""}</span></div>
        <div className="sc-tile" data-tone={(r?.lostOrRepeated ?? 0) === 0 ? "green" : "red"} data-testid="sir-lost"><span className="sc-tile-label"><Shield size={13} aria-hidden /> Lost or repeated</span><b className="sc-tile-n">{r?.lostOrRepeated ?? 0}</b><span className="sc-tile-sub">every run resumed from its checkpoint</span></div>
        <div className="sc-tile"><span className="sc-tile-label"><CurrencyDollar size={13} aria-hidden /> Cost</span><b className="sc-tile-n">${(r?.costUsd ?? i.spendUsd).toFixed(2)}</b><span className="sc-tile-sub">Infra Agent · {d.toolCalls} tool calls</span></div>
      </div>

      <section className="sc-card" aria-labelledby="sir-cause-h">
        <h3 id="sir-cause-h" className="sir-h">Cause</h3>
        <div className="sir-cause" data-testid="sir-cause">{r?.cause.text || i.causeText || "Not recorded."}</div>
        {r?.cause.chips.length ? <span className="sdd-chips">{r.cause.chips.map((c) => <span key={c} className="ia-chip" data-tone="red">{c}</span>)}</span> : null}
      </section>

      {r && (r.learned.ruleAdded || r.learned.ruleProposed) ? (
        <section className="sc-card" aria-labelledby="sir-learned-h">
          <h3 id="sir-learned-h" className="sir-h"><Sparkle size={14} aria-hidden /> Learned</h3>
          <ul className="sir-rows" data-testid="sir-learned">
            {r.learned.ruleAdded ? (
              <li className="sir-row" data-state="added">
                <span className="ia-chip" data-tone="green">Rule added</span>
                <span className="sir-row-main">{r.learned.ruleAdded.label} · {r.learned.ruleAdded.words}</span>
                {r.learned.ruleAdded.source === "learned" ? <span className="sir-by"><User size={12} aria-hidden /> approved by a person</span> : null}
              </li>
            ) : null}
            {r.learned.ruleProposed ? (
              <li className="sir-row" data-state="proposed">
                <span className="ia-chip" data-tone="violet">Proposed</span>
                <span className="sir-row-main">{r.learned.ruleProposed.kind.replace(/_/g, " ")} · {r.learned.ruleProposed.words}</span>
                <StripLink patch={{ area: "decisions", decision: r.learned.ruleProposed.decisionId, decision_filter: "needs_action", role: null }} orgId={orgId} className="icc-btn" testId="sir-review-rule">Review</StripLink>
              </li>
            ) : null}
          </ul>
          <div className="sc-faint">A cause seen twice becomes a rule proposal. You approve once; it is automatic after that.</div>
        </section>
      ) : null}

      <section className="sc-card" aria-labelledby="sir-removed-h">
        <header className="sc-card-head">
          <h3 id="sir-removed-h" className="sir-h">What was removed</h3>
          <span className="sc-spacer" />
          <a className="sc-link" href={receiptsHref} data-testid="sir-receipts" title="Every tool call of this incident, in the Log">
            {d.receiptsCount} receipt{d.receiptsCount === 1 ? "" : "s"} <ArrowRight size={11} aria-hidden /></a>
        </header>
        <ul className="sir-rows" data-testid="sir-removed">
          {removed.map((x) => (
            <li key={`${x.type}-${x.receiptId ?? ""}`} className="sir-row">
              <span className="sir-row-main">{x.type}</span>
              <span className="sc-faint">{x.detail}</span>
              <span className="ia-chip" data-tone={x.by === "person" ? "amber" : "green"}>{x.by === "person" ? <><User size={11} aria-hidden /> {BY_WORD.person}</> : <><Check size={11} aria-hidden /> {BY_WORD[x.by]}</>}</span>
              <code className="sir-gb">{gb(x.gb)}</code>
            </li>
          ))}
          {!removed.length ? (
            i.plan.filter((s) => s.state === "done").map((s) => (
              <li key={s.key} className="sir-row">
                <span className="sir-row-main">{s.label}</span>
                <span className="sc-faint">{s.detail}</span>
                <span className="ia-chip" data-tone={s.tier === 3 ? "amber" : "green"}>{s.tier === 3 ? <><User size={11} aria-hidden /> approved by a person</> : <><Check size={11} aria-hidden /> automatic</>}</span>
                <code className="sir-gb">{gb(s.freedGb || s.gb)}</code>
              </li>
            ))
          ) : null}
          {!removed.length && !i.plan.some((s) => s.state === "done") ? <li className="sc-faint">Nothing removed yet.</li> : null}
        </ul>
        <div className="sc-note"><Shield size={13} aria-hidden /> Everything removed carried a BOSS label. Nothing of yours was touched.</div>
      </section>

      {d.pausedWork.length ? (
        <section className="sc-card" aria-labelledby="sir-paused-h">
          <h3 id="sir-paused-h" className="sir-h">Paused work</h3>
          <ul className="sir-rows" data-testid="sir-paused">
            {d.pausedWork.map((p) => (
              <li key={p.jobId || p.title} className="sir-row">
                <span className="sir-row-main">{p.title}</span>
                {p.runId ? <span className="sc-faint">#{p.runId}</span> : null}
                <span className="ia-chip" data-tone="grey"><Clock size={11} aria-hidden /> waited {minutesWord(p.waitedSeconds)}</span>
                <span className="ia-chip" data-tone={p.resumedAt ? "green" : "amber"}>{p.resumedAt ? <><Play size={11} aria-hidden /> resumed</> : <><Pause size={11} aria-hidden /> waiting</>}</span>
              </li>
            ))}
          </ul>
          {(r?.pausedRuns ?? i.pausedRuns) > d.pausedWork.length ? <StripLink patch={{ area: "delivery_workflows", role: null, view: null }} orgId={orgId} className="sc-link" testId="sir-all-runs">All {r?.pausedRuns ?? i.pausedRuns} runs <ArrowRight size={11} aria-hidden /></StripLink> : null}
        </section>
      ) : null}
    </article>
  );
}

export function StorageIncidentReport({ projectId, incidentId, orgId }: { projectId: string; incidentId: string; orgId?: string | null }) {
  const { detail, error, loading, sample } = useIncidentDetail(projectId, incidentId);
  if (!detail) {
    return (
      <div className="sir" data-testid="storage-incident-report" data-empty>
        <span className="sc-crumb"><StripLink patch={storagePatch(null)} orgId={orgId} className="sc-link" testId="sir-back">Govern › Infrastructure › Storage</StripLink><span aria-hidden> › </span>Incidents</span>
        {loading ? <div className="gv-skel" aria-busy="true" /> : <div className="sc-alert" data-tone="grey" role="status" data-testid="sir-unavailable">Can&apos;t load this incident{error ? `: ${error}` : ""}.</div>}
      </div>
    );
  }
  return <StorageIncidentReportView d={detail} orgId={orgId} sample={sample} />;
}
