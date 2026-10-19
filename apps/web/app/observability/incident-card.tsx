"use client";

/**
 * The Incident card in the app drawer (D2): an app's own alert, answered — one surface for what
 * the monitor saw, who reported it, the root cause, the response run, its verification and the
 * report. Read from GET /v1/reliability/incidents/{id}/response (lib/incident-response.ts).
 *
 *   INCIDENT                                                               ● Responding
 *   helpdesk: error rate 12.5% over 5 min                                       opened 12m ago
 *   WHAT THE MONITOR SAW  Errors 12.5% of 220 · Alert above 5% · Serving a1b2c3d · Previous 9f8e7d6
 *                         POST /tickets  GET /tickets/{id}
 *   CUSTOMER REPORTS      3 reports from support-desk  [T-1042] [T-1043]
 *   ROOT CAUSE            <statement>  (basis · confidence)
 *   RESPONSE              Run ↗  Pull request ↗   (the step dots only before anyone is named)
 *   WHO DID WHAT          each part's agent, its role and job, what it did (responders.tsx)
 *                         (or Respond · "Error rate responds automatically" · Alerts ↗)
 *   VERIFICATION          <summary>
 *   REPORT                View · Download .md · .json
 *
 * Each section only when it has data. The status chip is derived from the data
 * (incidentStatus), never sent. Respond starts the catalogue's Incident Response with the
 * view's own `respond_input`; a 409 consent_required opens the consent sheet and re-sends.
 * Responding by itself is PER RULE, never per app (BOSSAPP-598): the card says which of the
 * app's rules respond automatically and links to the Alerts tab (`atab=alerts`), where each is
 * switched. There is no app-wide switch here.
 */
import React, { useState } from "react";

import { GatedButton } from "../../components/gated";
import { usePolled } from "../../lib/factory-floor";
import {
  RESPONSE_POLL_MS, RESPONSE_POLL_RUNNING_MS, STATUS_WORDS, basisWords, evidenceOf, incidentReportUrl,
  incidentResponseUrl, incidentRunHref, incidentStatus, parseIncidentResponse, pullRequestUrl, reportGroups,
  autoRespondLine, responseRunning, responseStrip, sha7, stepTone, toldLine, type IncidentResponse,
} from "../../lib/incident-response";
import { useLocalTime } from "../../lib/local-time";
import { pct } from "../../lib/obs-view";
import { Markdown } from "../markdown";
import { StartRefusal, asReadiness, type PendingRefusal } from "../workflow-readiness-ui";
import { notifyChanged } from "../changed";
import { showToast } from "../client-forms";
import { ago, plainClick, useNow } from "../agents/live-util";
import { post } from "../workflows/delivery-api";
import { Dot, DrawerSection } from "../infra/kit";
import { areaHref } from "../factory/cockpit-util";
import { obsRead, obsWrite } from "./obs-write";
import { leaveTo } from "./obs-util";
import { Responders } from "./responders";

function Fact({ label, children, tip }: { label: string; children: React.ReactNode; tip?: string }) {
  return (
    <span className="obs-fact" title={tip}>
      <span className="obs-fact-k">{label}</span>
      <span className="obs-fact-v inf-num">{children}</span>
    </span>
  );
}

const AUTO_TIP = "A rule with Respond automatically on starts Incident Response by itself when it fires. Switch it per rule on the Alerts tab.";
const VERIFY_TIP = "Judged by BOSS's monitor, not by the fixing agent";

/** The card's body from one response view: pure render, so a fixture draws every section. */
export function IncidentCardView({ projectId, r, now, busy, onRespond, onAlerts, onTell, report, onView, answer }: {
  projectId: string; r: IncidentResponse; now: number | null; busy?: "respond" | "tell" | null;
  /** Open the app's Alerts tab in place (the drawer); without it the link navigates. */
  onRespond?: () => void; onAlerts?: () => void; onTell?: () => void;
  /** The report's markdown when View was pressed (null: not open; "": loading). */
  report?: string | null; onView?: () => void;
  /** Answer the agent's question in place (the live card). */
  answer?: { projectId: string; actor: string; onDone?: () => void };
}) {
  const lt = useLocalTime();
  const status = incidentStatus(r);
  const sw = STATUS_WORDS[status];
  const a = r.alert;
  const d = r.deploy;
  const groups = reportGroups(r);
  const rca = evidenceOf(r, "rca_hypothesis");
  const pr = pullRequestUrl(r);
  const v = r.verification;
  const run = r.response;
  const opened = r.incident.firstSeen;
  const runHref = run ? incidentRunHref(run.id) : null;
  const reportOpen = report !== null && report !== undefined;
  const confidence = typeof rca?.payload.confidence === "number" ? rca.payload.confidence : null;
  const alertsHref = r.app?.targetId ? areaHref({ area: "observability", app: r.app.targetId, atab: "alerts" }) : null;
  const autoTip = r.autoRespondRules.length
    ? r.autoRespondRules.map((x) => (x.onBehalfOf ? `${x.label}: on ${x.onBehalfOf}'s standing grant` : x.label)).join("\n")
    : AUTO_TIP;

  return (
    <DrawerSection label="INCIDENT" testId="obs-incident-card"
                   actions={<span className="obs-chip" data-tone={sw.tone === "accent" ? "amber" : sw.tone} data-testid="obs-incident-status"
                                  data-status={status} title={sw.tip}>{sw.word}</span>}>
      <span className="obs-line">
        <span className="obs-inc-title" data-copy="data" data-testid="obs-incident-title">{r.incident.title}</span>
        <span className="ag-spacer" />
        {opened ? (
          <span className="inf-faint inf-num" data-testid="obs-incident-opened" title={lt.full(opened)}>
            {ago(opened, now) ? `opened ${ago(opened, now)} ago` : "opened —"}
          </span>
        ) : null}
      </span>

      {a || d || r.driver ? (
        <div className="obs-inc-part" data-testid="obs-incident-monitor">
          <span className="obs-fact-k">What the monitor saw</span>
          <div className="obs-facts">
            {/* WHAT DROVE IT: the alert firing now (or last). Error facts only for an error-rate
                driver: a recovered latency incident is never labelled "Errors". */}
            {r.driver && r.driver.kind !== "error_rate" ? (
              <Fact label={r.driver.label} tip={`${r.driver.alertname}: the alert driving this incident`}>
                <span data-testid="obs-incident-driver">{r.driver.valueWord ?? r.driver.alertname}</span>
              </Fact>
            ) : a ? (
              <Fact label="Errors" tip={`5xx share over ${a.windowMinutes ?? 5} min when the alert fired`}>
                {pct(a.errorRate, 1)}{a.requests !== null ? ` of ${a.requests.toLocaleString("en-US")}` : ""}
              </Fact>
            ) : null}
            {a && a.errorRatePercent !== null && (!r.driver || r.driver.kind === "error_rate") ? (
              <Fact label="Alert above" tip={`Fires once ${a.minRequests ?? 0}+ requests were seen in the window`}>
                {`${a.errorRatePercent}% / ${a.windowMinutes ?? 5} min`}
              </Fact>
            ) : null}
            {d?.commitSha ? (
              <Fact label="Serving" tip={d.deployedAt ? `Deployed ${lt.full(d.deployedAt)}` : "The release that was serving"}>
                <span className="inf-mono" data-copy="data" data-testid="obs-incident-serving">{sha7(d.commitSha)}</span>
              </Fact>
            ) : null}
            {d?.previousCommitSha ? (
              <Fact label="Previous healthy" tip="The release before it, which was not failing">
                <span className="inf-mono" data-copy="data" data-testid="obs-incident-previous">{sha7(d.previousCommitSha)}</span>
              </Fact>
            ) : null}
          </div>
          {a && a.routes.length && (!r.driver || r.driver.kind === "error_rate") ? (
            <span className="obs-chips" data-testid="obs-incident-routes">
              {[...a.routes].sort((x, y) => y.errors - x.errors).slice(0, 3).map((rt) => (
                <span key={rt.route} className="obs-chip" data-tone="red" data-copy="data"
                      title={`${rt.errors} of ${rt.requests} failed (${pct(rt.errorRate, 1)})`}>{rt.route}</span>
              ))}
            </span>
          ) : null}
        </div>
      ) : null}

      {groups.length ? (
        <div className="obs-inc-part" data-testid="obs-incident-reports">
          <span className="obs-fact-k">Customer reports</span>
          {groups.map((g) => (
            <span key={g.reporter} className="obs-line">
              <span title="Reports the app sent BOSS">
                {g.reports} report{g.reports === 1 ? "" : "s"} from <span data-copy="data">{g.reporter}</span>
              </span>
              {g.refs.length ? (
                <span className="obs-chips">
                  {g.refs.slice(0, 8).map((ref) => <span key={ref} className="obs-chip inf-mono" data-copy="data">{ref}</span>)}
                  {g.refs.length > 8 ? <span className="inf-faint" title={g.refs.slice(8).join(", ")}>+{g.refs.length - 8}</span> : null}
                </span>
              ) : null}
            </span>
          ))}
          {r.told.map((t) => (
            <span key={`${t.appName}:${t.signalId ?? ""}`} className="obs-line" data-testid="obs-incident-told">
              <span className={t.ok ? "inf-faint" : "inf-error"} title={t.word || undefined}>{toldLine(t)}</span>
              {!t.ok && onTell ? (
                <GatedButton permission="reliability:respond" type="button" className="ag-btn" data-testid="obs-incident-tell"
                             disabled={!!busy} aria-busy={busy === "tell" || undefined} onClick={onTell}
                             title={`Send ${t.appName} what BOSS knows about this incident again`}>
                  {busy === "tell" ? "Sending…" : "Send again"}
                </GatedButton>
              ) : null}
            </span>
          ))}
        </div>
      ) : null}

      {rca && typeof rca.payload.root_cause === "string" && rca.payload.root_cause ? (
        <div className="obs-inc-part" data-testid="obs-incident-rca">
          <span className="obs-fact-k">Root cause</span>
          <span className="obs-inc-text" data-copy="data">{rca.payload.root_cause}</span>
          <span className="inf-faint" data-copy="data" data-testid="obs-incident-basis">
            {[basisWords(rca.payload.basis), confidence !== null ? `confidence ${Math.round(confidence * 100)}%` : null].filter(Boolean).join(" · ")}
          </span>
        </div>
      ) : null}

      <div className="obs-inc-part" data-testid="obs-incident-response">
        <span className="obs-fact-k">Response</span>
        {run && runHref ? (
          <span className="obs-line">
            <a className="inf-link" href={runHref} data-testid="obs-incident-run" title="Open the Incident Response run"
               onClick={(e) => { if (plainClick(e)) { e.preventDefault(); leaveTo(runHref); } }}>Run ↗</a>
            {r.responders.length ? null : <ol className="obs-inc-steps" aria-label="Response steps" data-testid="obs-incident-steps">
              {responseStrip(run.steps).map((s) => (
                <li key={s.id} data-step={s.id} data-status={s.status ?? "not_started"} title={`${s.label}: ${s.status ?? "not started"}`}>
                  <Dot tone={stepTone(s.status)} />{s.label}
                </li>
              ))}
            </ol>}
            {pr ? <a className="inf-link" href={pr} target="_blank" rel="noreferrer noopener" data-testid="obs-incident-pr" title={pr}>Pull request ↗</a> : null}
          </span>
        ) : status === "resolved" ? (
          <span className="inf-faint" data-testid="obs-incident-no-response">No response run</span>
        ) : (
          <span className="obs-line">
            <GatedButton permission="workflow_run:start" type="button" className="ag-btn ag-btn-primary" data-testid="obs-incident-respond"
                         disabled={!!busy} aria-busy={busy === "respond" || undefined}
                         title="Start Incident Response: investigate, fix, ship, verify" onClick={onRespond}>
              {busy === "respond" ? "Starting…" : "Respond"}
            </GatedButton>
            <span className="obs-inc-auto" data-testid="obs-incident-auto" data-rules={r.autoRespondRules.map((x) => x.kind).join(" ")}>
              <span className={r.autoRespondRules.length ? undefined : "inf-faint"} title={autoTip}>{autoRespondLine(r.autoRespondRules)}</span>
              {alertsHref ? (
                <a className="inf-link" href={alertsHref} data-testid="obs-incident-alerts" title={AUTO_TIP}
                   onClick={(e) => { if (onAlerts && plainClick(e)) { e.preventDefault(); onAlerts(); } }}>Alerts ↗</a>
              ) : null}
            </span>
          </span>
        )}
      </div>

      <Responders rows={r.responders} onReport={onView} answer={answer} />

      {v && v.summary ? (
        <div className="obs-inc-part" data-testid="obs-incident-verification">
          <span className="obs-fact-k" title={VERIFY_TIP}>Verification</span>
          <span className="obs-inc-text" data-copy="data" data-tone={v.state === "recovered" ? "green" : v.state === "timed_out" ? "red" : undefined}
                title={VERIFY_TIP}>{v.summary}</span>
        </div>
      ) : null}

      {r.reportReady ? (
        <div className="obs-inc-part" data-testid="obs-incident-report">
          <span className="obs-fact-k">Report</span>
          <span className="obs-line">
            <button type="button" className="inf-link" aria-expanded={reportOpen} data-testid="obs-incident-report-view"
                    title="Read the incident report here" onClick={onView}>{reportOpen ? "Hide" : "View"}</button>
            <a className="inf-link" href={incidentReportUrl(projectId, r.incident.id, "md")} data-testid="obs-incident-report-md"
               download={`incident-${r.incident.id}.md`} title="The report as Markdown">Download .md</a>
            <a className="inf-link" href={incidentReportUrl(projectId, r.incident.id, "json")} data-testid="obs-incident-report-json"
               download={`incident-${r.incident.id}.json`} title="The report as JSON, with its timeline and cost">.json</a>
          </span>
          {reportOpen ? (
            <div className="obs-inc-md" data-copy="data" data-testid="obs-incident-report-body">
              {report ? <Markdown text={report} /> : <span className="inf-faint">Loading…</span>}
            </div>
          ) : null}
        </div>
      ) : null}
    </DrawerSection>
  );
}

/** The card, live: polls the response view (faster while a response runs) and does the writes. */
export function IncidentCard({ projectId, actor, incidentId, onAlerts }: {
  projectId: string; actor: string; incidentId: string; onAlerts?: () => void;
}) {
  const polled = usePolled(projectId && incidentId ? incidentResponseUrl(projectId, incidentId) : null, parseIncidentResponse,
                           (d) => (d && responseRunning(d) ? RESPONSE_POLL_RUNNING_MS : RESPONSE_POLL_MS));
  const now = useNow(30_000);
  const [busy, setBusy] = useState<"respond" | "tell" | null>(null);
  const [report, setReport] = useState<string | null>(null);
  // A start refused for what the project lacks: the checklist and "Respond anyway" (one drawing, StartRefusal).
  const [refusal, setRefusal] = useState<PendingRefusal | null>(null);
  const r = polled.data;
  if (!r) {
    return polled.error ? (
      <DrawerSection label="INCIDENT" testId="obs-incident-card">
        <span className="inf-error" title={polled.error}>Can&apos;t load the incident</span>
      </DrawerSection>
    ) : null;
  }

  const respond = async (startAnyway: string[] = []) => {
    if (busy) return;
    setBusy("respond");
    setRefusal(null);
    const res = await post("start", { project_id: projectId, workflow_key: "incident_response", actor, input: r.respondInput,
                                      ...(startAnyway.length ? { start_anyway: startAnyway } : {}) });
    setBusy(null);
    const body = res.ok ? (res.data as { run_id?: unknown; id?: unknown; run?: { id?: unknown } } | null) : null;
    const started = body?.run_id ?? body?.id ?? body?.run?.id;
    if (res.ok && started) {
      showToast("success", "Responding");
      notifyChanged("observability", { incident_id: incidentId });
      polled.refresh();
      return;
    }
    const readiness = res.ok ? null : asReadiness(res.readiness);
    if (readiness) {
      setRefusal({ label: "Respond", message: String(res.error ?? ""), readiness, retry: (keys) => respond(keys) });
      return;
    }
    showToast("error", !res.ok && typeof res.error === "string" && res.error ? res.error : "Could not start the response");
  };
  const tellAgain = async () => {
    if (busy) return;
    setBusy("tell");
    const res = await obsWrite<{ told?: { ok?: unknown; word?: unknown }[] }>(
      `/v1/reliability/incidents/${encodeURIComponent(incidentId)}/tell-reporters`, { project_id: projectId },
      { scope: "obs:tell-reporters", label: "Send again" });
    setBusy(null);
    const told: { ok?: unknown; word?: unknown }[] = res.ok ? res.body?.told ?? [] : [];
    if (res.ok && told.length && told.every((t) => t.ok === true)) showToast("success", "Sent");
    else showToast("error", res.ok ? String(told.find((t) => t.ok !== true)?.word ?? "Not taken") : res.error ?? "Could not send it");
    polled.refresh();
  };
  const view = async () => {
    if (report !== null) { setReport(null); return; }
    setReport("");
    const got = await obsRead<{ markdown?: unknown }>(incidentReportUrl(projectId, incidentId, "json"));
    const md = typeof got?.markdown === "string" ? got.markdown : "";
    if (!md) { setReport(null); showToast("error", "Could not read the report"); return; }
    setReport(md);
  };

  return (
    <>
      <IncidentCardView projectId={projectId} r={r} now={now} busy={busy} onRespond={() => void respond()} onAlerts={onAlerts}
                        onTell={() => void tellAgain()}
                        report={report} onView={view}
                        answer={{ projectId, actor, onDone: () => polled.refresh() }} />
      {refusal ? <StartRefusal refusal={refusal} projectId={projectId} busy={busy === "respond"} onDismiss={() => setRefusal(null)} /> : null}
    </>
  );
}
