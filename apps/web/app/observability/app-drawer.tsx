"use client";

/**
 * The app drawer (spec §4.9, `app=<target_id>`, 560 px): one deployed app over the window.
 *
 *   api · prod  ● Down  since 1h 4m                                         ⋯  ×
 *   Uptime 24h 91.3%  Uptime 7d 98.7%  p95 240 ms  Last probe 2m  Build a1b2c3d [Serving 9f1e2d3]  Host ↗
 *   <latency chart>
 *   TRAFFIC  Requests 42/min · Errors 1.2% · p50 18 ms · p95 240 ms · Alert Within limit
 *            Metrics: scraped 20s ago   (or the failure in words: "Metrics endpoint returned 404")
 *   INCIDENT   the app's alert, answered (incident-card.tsx), when its metrics carry an incident
 *   INCIDENTS  start · duration (Ongoing) · first error · deploy before it ↗
 *   [Probe now] Open app ↗  Dashboard ↗  Logs ↗  [Ask BOSS]
 *
 * Two tabs, [Overview | Alerts] (`atab=alerts`, which Factory ⋯ › Alerts links to): Alerts is
 * this app's rules, signal sources and alert history (alerts-tab.tsx). `Dashboard ↗` shows only
 * once BOSS has checked that Grafana answers; otherwise it says why, with Check and Deploy stack.
 *
 * `Probe now` probes THIS app only (operator decision §11.5) and kicks both polls; the toast is
 * `Probed · <word>`. The `Serving <v>` chip appears only when the app reports a build other than
 * the one last deployed — the server says so (`serving_mismatch`); nothing is compared here.
 */
import { GatedButton } from "../../components/gated";
import React, { useState } from "react";

import { openDockAbout } from "../../lib/dock-subject";
import { deployHref, hostHref, logsHref } from "../../lib/infra-view";
import { ms, pct, useAppDetail, type AppMetrics, type PulseRow } from "../../lib/obs-view";
import { showToast } from "../client-forms";
import { notifyChanged } from "../changed";
import { ago, plainClick, useNow } from "../agents/live-util";
import type { ObsTabProps } from "./obs-home";
import { IncidentCard } from "./incident-card";
import { AppAlertsTab } from "./alerts-tab";
import { DashboardLink } from "./grafana-link";
import { LatencyChart } from "./latency-chart";
import { obsWrite } from "./obs-write";
import { dur, leaveTo, obsAsks } from "./obs-util";
import { Drawer, DrawerSection, Skeleton, StateWord } from "../infra/kit";
import { useLocalTime } from "../../lib/local-time";

function Fact({ label, children, tip }: { label: string; children: React.ReactNode; tip?: string }) {
  return (
    <span className="obs-fact" title={tip}>
      <span className="obs-fact-k">{label}</span>
      <span className="obs-fact-v inf-num">{children}</span>
    </span>
  );
}

/** "20s ago" / "4m ago": a scrape runs every minute or two, so minutes are too coarse. */
function agoShort(iso: string | null, now: number | null): string {
  if (!iso || now === null) return "";
  const t = Date.parse(iso);
  if (!Number.isFinite(t)) return "";
  const s = Math.max(0, Math.floor((now - t) / 1000));
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

/** The app's own request metrics (app_metrics): what users saw, not what the probe saw. */
export function TrafficSection({ m, now }: { m: AppMetrics; now: number | null }) {
  if (!m.declared) {
    return (
      <DrawerSection label="TRAFFIC" testId="obs-app-traffic">
        <span className="inf-empty" data-testid="obs-metrics-word" title={m.tip ?? undefined}>Metrics not declared</span>
      </DrawerSection>
    );
  }
  const word = m.state === "ok" ? `Metrics: scraped ${agoShort(m.lastScrapeAt, now) || "just now"}` : m.word;
  const tone = m.state === "failing" ? "red" : m.state === "ok" ? undefined : "amber";
  const win = m.windowMinutes ?? 5;
  const a = m.alert;
  const ruleTip = a ? (a.rule.enabled
    // ≤ 14 words (copy budget): the rule is set by metrics.alert in boss.deploy.yaml.
    ? `Alerts when more than ${a.rule.errorRatePercent}% of requests fail (5xx) over ${a.rule.windowMinutes} min (${a.rule.minRequests}+ requests)`
    : "Alert switched off in boss.deploy.yaml") : undefined;
  return (
    <DrawerSection label="TRAFFIC" testId="obs-app-traffic">
      <div className="obs-facts" data-testid="obs-app-metrics">
        <Fact label="Requests" tip={`Requests per minute over the last ${win} min, from the app's ${m.path ?? "/metrics"}`}>
          {m.ratePerMin === null ? "—" : `${m.ratePerMin.toLocaleString("en-US")}/min`}
        </Fact>
        <Fact label="Errors" tip={m.errorRate === null ? "No error rate: no requests yet, or no status label on the counter" : `5xx share of ${m.requests ?? 0} requests over ${win} min`}>
          {pct(m.errorRate, 1)}
        </Fact>
        <Fact label="p50" tip={`Median request latency over ${win} min, from the app's histogram`}>{ms(m.p50Ms)}</Fact>
        <Fact label="p95" tip={`95th percentile request latency over ${win} min`}>{ms(m.p95Ms)}</Fact>
        {a ? (
          <span className="obs-chip" data-tone={a.state === "firing" ? "red" : undefined} data-testid="obs-metrics-alert" title={ruleTip}>
            {a.word}
          </span>
        ) : null}
      </div>
      <span className="inf-faint" data-testid="obs-metrics-word" data-tone={tone}
            title={[m.url, m.route ? `via ${m.route}` : null, m.naming ? `names: ${m.naming}` : null].filter(Boolean).join(" · ") || undefined}>
        {word}
      </span>
    </DrawerSection>
  );
}

export function AppDrawer({ projectId, actor, url, go, pulse, targetId, row }: ObsTabProps & { targetId: string; row: PulseRow | null }) {
  const lt = useLocalTime();
  const detail = useAppDetail(projectId, targetId, url.win);
  const d = detail.data;
  const now = useNow(30_000);
  const [busy, setBusy] = useState(false);
  const [shown, setShown] = useState<Set<number>>(new Set());
  const close = () => go({ app: null, atab: null }, "push");
  const tab = url.atab;
  const setTab = (t: "overview" | "alerts") => go({ atab: t === "alerts" ? "alerts" : null }, "replace");
  const name = d ? `${d.app} · ${d.env}` : row ? `${row.app} · ${row.env}` : targetId;
  const word = d ? { word: d.word, tone: d.tone } : row ? { word: row.word, tone: row.tone } : null;
  const since = d?.since ?? row?.since ?? null;

  const probe = async () => {
    if (busy) return;
    setBusy(true);
    const r = await obsWrite<{ word?: string }>(`/v1/observability/apps/${encodeURIComponent(targetId)}/probe`,
                                                { project_id: projectId, actor }, { scope: `obs:probe:${targetId}`, label: "Probe now" });
    setBusy(false);
    if (r.ok) {
      showToast("success", `Probed · ${r.body?.word ?? "done"}`);
      notifyChanged("observability", { target_id: targetId });
      detail.refresh();
      pulse.refresh();
    } else {
      showToast("error", r.error ?? "Probe failed");
    }
  };
  const copyLink = async () => {
    try { await navigator.clipboard.writeText(window.location.href); showToast("success", "Copied"); } catch { /* no clipboard */ }
  };
  const ask = () => {
    const starters = obsAsks(pulse.data);
    const mine = starters.find((a) => a.subject?.id === targetId);
    openDockAbout({ subject: { kind: "deployment", id: targetId },
                    question: mine?.question ?? `How is ${name} doing, and what should we watch?`, role: null });
  };

  const head = (
    <span className="obs-dhead">
      <span className="obs-dhead-name" data-copy="data" data-user-text>{name}</span>
      {word ? <StateWord w={word} testId="obs-app-state" /> : null}
      <span className="inf-faint inf-num" data-testid="obs-app-since" title={since ? lt.full(since) : "Never probed"}>
        {since && now !== null ? `since ${dur((now - Date.parse(since)) / 1000)}` : since ? "since —" : "No signal"}
      </span>
      <button type="button" className="ag-icon-btn" aria-label="Copy link" title="Copy link" data-testid="obs-app-copy" onClick={copyLink}>⋯</button>
    </span>
  );

  const tabs = (
    <span className="inf-seg obs-dtabs" role="tablist" aria-label={`${name} views`} data-testid="obs-app-tabs">
      {([["overview", "Overview", "Health, traffic and incidents"], ["alerts", "Alerts", "Rules, signal sources and alert history"]] as const).map(([k, label, tip]) => (
        <button key={k} type="button" role="tab" aria-selected={tab === k} className="inf-seg-btn" title={tip}
                data-testid={`obs-app-tab-${k}`} onClick={() => setTab(k)}>{label}</button>
      ))}
    </span>
  );

  return (
    <Drawer label={`${name} health`} testId="obs-app-drawer" width={880} expandable onClose={close} head={head}>
      {tabs}
      {tab === "alerts" ? (
        <AppAlertsTab projectId={projectId} actor={actor} targetId={targetId} onIncident={() => setTab("overview")} />
      ) : !d ? (detail.error ? <span className="inf-error" title={detail.error}>Can&apos;t load this app</span> : <Skeleton testId="obs-app-loading" />) : (
        <>
          <div className="obs-facts" data-testid="obs-app-facts">
            <Fact label="Uptime 24h">{pct(d.facts.uptime24h, 2)}</Fact>
            <Fact label="Uptime 7d">{pct(d.facts.uptime7d, 2)}</Fact>
            <Fact label="p95" tip={d.facts.p95Ms === null ? "No latency recorded" : `avg ${ms(d.facts.avgMs)}`}>{ms(d.facts.p95Ms)}</Fact>
            <Fact label="Last probe" tip={d.facts.lastProbeAt ? lt.full(d.facts.lastProbeAt) : "Never probed"}>
              {d.facts.lastProbeAt ? ago(d.facts.lastProbeAt, now) || "—" : "—"}
            </Fact>
            <Fact label="Probes" tip={`${d.facts.failed} failed of ${d.facts.probes}`}>{d.facts.probes.toLocaleString("en-US")}</Fact>
            <Fact label="Build" tip={d.facts.deployedAt ? `Deployed ${lt.full(d.facts.deployedAt)}` : undefined}>
              <span className="inf-mono" data-copy="data">{d.facts.buildSha7 ?? "—"}</span>
            </Fact>
            {d.facts.servingMismatch && d.facts.observedVersion ? (
              <span className="obs-chip" data-tone="amber" data-testid="obs-app-serving" title="The app reports a different build than the last deploy">
                Serving <span className="inf-mono" data-copy="data">{d.facts.observedVersion.slice(0, 7)}</span>
              </span>
            ) : null}
            {d.facts.host ? (
              <a className="inf-link" href={hostHref(d.facts.host.id)} title={`${d.facts.host.name} in Infra`} data-testid="obs-app-host"
                 onClick={(e) => { if (plainClick(e)) { e.preventDefault(); leaveTo(hostHref(d.facts.host!.id)); } }}>Host ↗</a>
            ) : null}
          </div>

          {d.metrics.alert?.incidentId ? (
            <IncidentCard projectId={projectId} actor={actor} incidentId={d.metrics.alert.incidentId} />
          ) : null}

          <LatencyChart series={d.series} pins={d.pins} incidents={d.incidents} fromIso={d.window.from} toIso={d.window.to} label={name} />

          <TrafficSection m={d.metrics} now={now} />

          <DrawerSection label="INCIDENTS" testId="obs-app-incidents">
            {d.incidents.length === 0 ? <span className="inf-empty">No incidents</span> : (
              <ul className="obs-list">
                {d.incidents.map((inc, i) => {
                  const start = Date.parse(inc.start);
                  return (
                    <li key={inc.start} className="obs-incident" data-ongoing={inc.end ? undefined : "1"}>
                      <span className="obs-line">
                        <span className="inf-num" title={lt.full(start)}>{lt.hhmm(Date.parse(String(start)))}</span>
                        {inc.end ? <span className="inf-muted inf-num">{inc.durationS !== null ? dur(inc.durationS) : "—"}</span>
                                 : <span className="obs-ongoing" data-testid="obs-incident-ongoing">Ongoing</span>}
                        {inc.firstError ? <span className="inf-mono inf-trunc obs-err" title={inc.firstError} data-copy="data">{inc.firstError}</span> : null}
                        <span className="ag-spacer" />
                        {inc.firstError ? (
                          <button type="button" className="inf-link" aria-expanded={shown.has(i)} data-testid="obs-incident-show-error"
                                  onClick={() => setShown((s) => { const n = new Set(s); if (n.has(i)) n.delete(i); else n.add(i); return n; })}>
                            {shown.has(i) ? "Hide error" : "Show error"}
                          </button>
                        ) : null}
                        {inc.deployBefore ? (
                          <a className="inf-link" href={deployHref(inc.deployBefore.ref)} data-testid="obs-incident-deploy"
                             title={`Deploy ${inc.deployBefore.sha7 ?? ""} finished shortly before`}
                             onClick={(e) => { if (plainClick(e)) { e.preventDefault(); leaveTo(deployHref(inc.deployBefore!.ref)); } }}>deploy before it ↗</a>
                        ) : null}
                      </span>
                      {shown.has(i) && inc.firstError ? <pre className="obs-errfull" data-copy="data">{inc.firstError}</pre> : null}
                    </li>
                  );
                })}
              </ul>
            )}
          </DrawerSection>

          <div className="inf-actions obs-dactions">
            <GatedButton permission="observability:manage" type="button" className="ag-btn ag-btn-primary" data-testid="obs-probe-now" disabled={busy} aria-busy={busy || undefined}
                    title="Check this app's health now instead of waiting" onClick={probe}>{busy ? "Probing…" : "Probe now"}</GatedButton>
            {d.facts.openUrl ? (
              <a className="inf-link" href={d.facts.openUrl} target="_blank" rel="noreferrer noopener" data-testid="obs-app-open" title={d.facts.openUrl}>Open app ↗</a>
            ) : <span className="inf-faint" data-testid="obs-app-open" title="Not serving yet">Not deployed</span>}
            <DashboardLink g={d.facts.grafana} url={d.facts.dashboardUrl} reason={d.facts.dashboardReason} projectId={projectId} actor={actor}
                           onChecked={() => { detail.refresh(); pulse.refresh(); }}
                           onDeploy={() => go({ view: "sources", app: null, atab: null }, "push")} />
            <a className="inf-link" href={logsHref("deployment", targetId)} data-testid="obs-app-logs"
               onClick={(e) => { if (plainClick(e)) { e.preventDefault(); leaveTo(logsHref("deployment", targetId)); } }}>Logs ↗</a>
            <span className="ag-spacer" />
            <button type="button" className="ag-btn" data-testid="obs-app-ask" onClick={ask}>Ask BOSS</button>
          </div>
        </>
      )}
    </Drawer>
  );
}
