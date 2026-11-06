"use client";

/**
 * HEALTH (screen 12, contract §12): is THIS project's app OK — one card per environment (uptime ring,
 * error rate and p95 sparklines, SLO burn), incidents as mini flows into Agents at work, alerts,
 * Beacon's monitoring pack, and the way out to Grafana, logs and the org-wide Observability.
 */
import React from "react";
import {
  ArrowRight, ArrowSquareOut, Bell, Buildings, ChartLine, FileText, Heartbeat, LinkSimple, Pulse, ShieldCheck, Siren, Terminal,
} from "@phosphor-icons/react";

import { PX_PATHS, isPxDemo, navigateTo, parseHealth, pxHref, usePxRead, type HealthView } from "../../lib/project-experience";
import { FX_HEALTH } from "../../lib/project-experience-fixtures";
import { Empty, EngineNotReady, Face, Flow, HEALTH_TONE, HEALTH_WORD, Ico, PxLink, ReadGate, Ring, Spark, Tag } from "./kit";
import { whenLocal } from "../../lib/local-time";
import { AppAlertsTab } from "../observability/alerts-tab";
import { usePulse } from "../../lib/obs-view";
import "../infra/infra.css";
import "../observability/obs.css";
import type { PxCtx } from "./project-page";

export function HealthTab({ ctx }: { ctx: PxCtx }) {
  const read = usePxRead<HealthView>(PX_PATHS.health(ctx.projectId), parseHealth, { everyMs: 20_000, demo: () => parseHealth(FX_HEALTH) });
  const obs = `?area=observability&project_id=${encodeURIComponent(ctx.projectId)}`;
  return (
    <div className="px-stack" data-testid="px-health">
      <ReadGate read={read} what="Health" missing={
        <>
          <EngineNotReady what="This project's health summary"><a className="px-btn" href={pxHref(obs)} data-testid="px-health-obs">Open Observability</a></EngineNotReady>
          <AlertRules ctx={ctx} envs={[]} />
        </>
      }>
        {(h) => (
          <>
            {h.environments.length ? h.environments.map((e) => (
              <section key={e.key} className="px-card" aria-labelledby={`px-h-${e.key}`} data-testid={`px-h-env-${e.key}`}>
                <div className="px-card-h"><Ico icon={Heartbeat} c="green" /><h2 id={`px-h-${e.key}`}>{e.label}</h2>
                  <Tag tone={HEALTH_TONE[e.health]}><span className="px-dot" data-s={e.health} data-round aria-hidden />{HEALTH_WORD[e.health]}</Tag>
                  {ctx.overview.project.live_url ? <a className="px-chip" href={ctx.overview.project.live_url} target="_blank" rel="noreferrer noopener">{ctx.overview.project.live_url.replace(/^https?:\/\//, "")} ↗</a> : null}
                  <span className="px-spacer" /><small className="px-faint">last 24h</small></div>
                <div className="px-grid px-g4">
                  <div><span className="px-lbl">Uptime</span>
                    <div className="px-row" style={{ marginTop: 6 }}>
                      <Ring pct={e.uptime_pct ?? 0} tone={e.health === "healthy" ? "green" : "amber"} label={`Uptime ${e.uptime_pct ?? "unknown"}%`}>{e.uptime_pct ?? "—"}</Ring>
                      {e.slo.target !== null ? <small className="px-faint">SLO {e.slo.target}%</small> : null}
                    </div></div>
                  <div><span className="px-lbl">Error rate</span><div className="px-num" style={{ marginTop: 6 }}>{e.error_rate ?? "—"}{e.error_rate !== null ? "%" : ""}</div>
                    <Spark series={e.series.errors} tone="amber" label="Error rate, last 24 hours" /></div>
                  <div><span className="px-lbl">p95 latency</span><div className="px-num" style={{ marginTop: 6 }}>{e.p95_ms ?? "—"}<small className="px-faint" style={{ fontSize: ".55rem" }}> ms</small></div>
                    <Spark series={e.series.latency} tone="teal" label="p95 latency, last 24 hours" /></div>
                  <div><span className="px-lbl">SLO burn</span>
                    <span className="px-bar" style={{ marginTop: 14 }} role="img" aria-label={`SLO burn ${e.slo.burn}`}>
                      <span style={{ width: e.slo.burn === "ok" ? "22%" : e.slo.burn === "warning" ? "65%" : "100%", background: e.slo.burn === "ok" ? "var(--green)" : e.slo.burn === "warning" ? "var(--amber)" : "var(--red)" }} /></span>
                    <div style={{ marginTop: 6 }}><Tag tone={e.slo.burn === "ok" ? "green" : e.slo.burn === "warning" ? "amber" : "red"}>{e.slo.burn === "ok" ? "OK" : e.slo.burn === "warning" ? "Burning" : "Breached"}</Tag></div></div>
                </div>
                {e.href ? <div style={{ marginTop: 8 }}><PxLink href={e.href} testid={`px-h-env-more-${e.key}`}>Full charts <ArrowRight size={10} /></PxLink></div> : null}
              </section>
            )) : (
              <div className="px-card"><Empty icon={Heartbeat} tone="green" title="No environment to watch yet" line="Health starts once the app is deployed: Beacon installs a monitoring pack on the first deploy." testid="px-health-empty" /></div>
            )}
            {!ctx.overview.project.live_url || h.environments.length < 2 ? (
              <section className="px-card px-locked" aria-label="Production" style={{ padding: "10px 14px" }}>
                <div className="px-row px-wrap"><Ico icon={Buildings} c="amber" /><b>Production</b><Tag tone="amber">Not set up</Tag><small className="px-faint">Health appears once prod has a host</small>
                  <span className="px-spacer" /><button type="button" className="px-chip" onClick={() => ctx.tab("deployments")} data-testid="px-health-to-deploy">Deployments <ArrowRight size={10} /></button></div>
              </section>
            ) : null}
            <div className="px-split">
              <div className="px-stack">
                <section className="px-card" aria-labelledby="px-h-inc" data-testid="px-h-incidents">
                  <div className="px-card-h"><Ico icon={Siren} c="red" /><h2 id="px-h-inc">Incidents</h2>
                    <a className="px-more" href={pxHref("?area=agent_ops")}>In Agents at work <ArrowRight size={11} /></a></div>
                  {h.incidents.length ? h.incidents.map((i) => (
                    <div key={i.id} className="px-row" style={{ alignItems: "flex-start", padding: "8px 0", borderTop: "1px solid var(--border)" }}>
                      <span className="px-ck-b" aria-hidden style={i.state === "resolved" ? { background: "var(--green)", borderColor: "var(--green)", color: "#032015" } : { background: "var(--amber)", borderColor: "var(--amber)", color: "#1b1300", borderRadius: "50%" }}>{i.state === "resolved" ? "✓" : "!"}</span>
                      <div style={{ flex: 1, minWidth: 0 }}>
                        <b style={{ fontSize: ".66rem" }}>{i.label}</b>
                        <Flow nodes={i.mini.map((m, k) => ({ ...m, type: /squad|sre|beacon/i.test(m.label) ? "agent" : undefined,
                          icon: /squad|sre|beacon/i.test(m.label) ? <Face agent={{ name: "Beacon", role_key: "sre" }} size={16} /> : k === 0 ? <Bell size={11} aria-hidden /> : undefined }))} />
                      </div>
                      {i.graph_href ? <PxLink href={i.graph_href} testid={`px-h-inc-graph-${i.id}`}>Graph</PxLink> : null}
                    </div>
                  )) : <p className="px-faint">No incidents. When an alert fires, the SRE squad's work shows here as a diagram.</p>}
                </section>
                <section className="px-card" aria-labelledby="px-h-al" data-testid="px-h-alerts">
                  <div className="px-card-h"><Ico icon={Bell} c="amber" /><h2 id="px-h-al">Alerts</h2>
                    <Tag tone={h.alerts.some((a) => a.state === "firing") ? "red" : "green"}>{h.alerts.filter((a) => a.state === "firing").length} firing</Tag></div>
                  {h.alerts.length ? h.alerts.map((a) => (
                    <div key={a.id} className="px-li">
                      <span className="px-dot" data-s={a.state === "firing" ? "bad" : "done"} aria-hidden />
                      <span><b>{a.href ? <PxLink href={a.href} className="">{a.label}</PxLink> : a.label}</b><small>{a.severity} · {a.state === "firing" ? `firing since ${whenLocal(a.since)}` : `resolved ${whenLocal(a.since)}`}</small></span>
                      <Tag tone={a.state === "firing" ? "red" : "green"}>{a.state}</Tag>
                    </div>
                  )) : <p className="px-faint">Nothing has fired.</p>}
                </section>
              </div>
              <div className="px-stack">
                <section className="px-card" aria-labelledby="px-h-mon" data-testid="px-h-monitoring">
                  <div className="px-card-h"><Ico icon={ShieldCheck} c="teal" /><h2 id="px-h-mon">Monitoring</h2></div>
                  {h.monitoring_pack ? (
                    <div className="px-cap">
                      <Face agent={{ name: "Beacon", role_key: "sre" }} size={40} standalone />
                      <div><div className="px-cap-name">Beacon&apos;s pack</div>
                        <div className="px-cap-now"><span className="px-dot" data-s="done" aria-hidden />{h.monitoring_pack.rules} rules · in {h.monitoring_pack.managed_in} · on deploy</div>
                        {h.monitoring_pack.href ? <PxLink href={h.monitoring_pack.href} testid="px-h-pack"><FileText size={11} aria-hidden /> The rules</PxLink> : null}</div>
                    </div>
                  ) : <p className="px-faint">Beacon installs a monitoring pack (uptime, 5xx, latency, disk, TLS) on the first deploy.</p>}
                </section>
                <section className="px-card" aria-labelledby="px-h-deeper" data-testid="px-h-deeper">
                  <div className="px-card-h"><Ico icon={LinkSimple} c="blue" /><h2 id="px-h-deeper">Go deeper</h2></div>
                  {h.links.grafana ? <a className="px-li" href={h.links.grafana} target="_blank" rel="noreferrer noopener" data-testid="px-h-grafana"><ChartLine size={14} aria-hidden /><b>Grafana dashboard</b><ArrowSquareOut size={12} aria-label="opens in a new tab" /></a> : null}
                  {h.links.logs ? <a className="px-li" href={pxHref(h.links.logs)} data-testid="px-h-logs"><Terminal size={14} aria-hidden /><b>Logs</b><ArrowRight size={12} aria-hidden /></a> : null}
                  <a className="px-li" href={pxHref(h.links.observability || obs)} data-testid="px-h-observability"><Pulse size={14} aria-hidden /><b>Observability › this app</b><ArrowRight size={12} aria-hidden /></a>
                </section>
              </div>
            </div>
            <AlertRules ctx={ctx} envs={h.environments.map((e) => ({ label: e.label, targetId: e.target_id }))} />
          </>
        )}
      </ReadGate>
    </div>
  );
}

/**
 * THE SAME ALERT RULES AS OBSERVABILITY (operator, 2026-11-06: "we need to have same experience -
 * dont kill these screens"). Each of this project's environments gets Observability's own app
 * Alerts component — the rules with their PromQL and facts, state, Tune, Silence, Respond
 * automatically, + New alert rule, signal sources and history — not a summary. Which environments:
 * §12's `target_id`s, else this project's apps as Observability lists them.
 */
function AlertRules({ ctx, envs }: { ctx: PxCtx; envs: { label: string; targetId: string }[] }) {
  const demo = isPxDemo();
  const pulse = usePulse(demo ? "" : ctx.projectId, "24h");
  const known = envs.filter((e) => e.targetId);
  const targets = known.length ? known
    : (pulse.data?.rows ?? []).filter((r) => r.state !== "not_deployed").map((r) => ({ label: `${r.app} · ${r.env}`, targetId: r.targetId }));
  if (demo) {
    return <p className="px-faint" data-testid="px-h-rules-demo">Sample mode: the alert rules (Tune, Silence, Respond automatically, New alert rule) load from the engine.</p>;
  }
  if (!targets.length) {
    return pulse.loading ? null : (
      <section className="px-card" data-testid="px-h-rules-none">
        <Empty icon={Bell} tone="amber" title="No alert rules yet" line="Rules arrive with the first deploy: Beacon installs the monitoring pack." />
      </section>
    );
  }
  return (
    <section aria-labelledby="px-h-rules" className="px-stack" style={{ gap: 10 }} data-testid="px-h-rules">
      <div className="px-row"><Ico icon={Bell} c="amber" /><h2 id="px-h-rules" style={{ margin: 0, fontFamily: "var(--display)", fontSize: ".86rem" }}>Alert rules</h2>
        <small className="px-faint">the same rules and actions as Observability</small></div>
      {targets.map((t, i) => (
        <details key={t.targetId} className="px-card" open={i === 0} data-testid={`px-h-rules-${t.targetId}`}>
          <summary style={{ cursor: "pointer", fontWeight: 600 }}>{t.label}
            <a className="px-chip" style={{ marginLeft: 8 }} href={pxHref(`?area=observability&app=${encodeURIComponent(t.targetId)}&atab=alerts`)}
               data-testid={`px-h-rules-obs-${t.targetId}`}>In Observability</a></summary>
          <div className="inf" style={{ marginTop: 10 }}>
            <AppAlertsTab projectId={ctx.projectId} actor={ctx.actor} targetId={t.targetId}
                          onIncident={(id) => navigateTo(pxHref(`?area=observability&app=${encodeURIComponent(t.targetId)}${id ? `&inc=${encodeURIComponent(id)}` : ""}`))} />
          </div>
        </details>
      ))}
    </section>
  );
}
