"use client";

/**
 * HEALTH (screen 12, contract §12): is THIS project's app OK — one card per environment (uptime ring,
 * error rate and p95 sparklines, SLO burn), incidents as mini flows into Agents at work, alerts,
 * Beacon's monitoring pack, and the way out to Grafana, logs and the org-wide Observability.
 */
import React from "react";
import {
  ArrowRight, ArrowSquareOut, Bell, BellRinging, Buildings, CaretRight, Certificate, ChartLine, Check, Cylinder, FileCode, FileText, GitBranch, Globe,
  HardDrive, Heartbeat, LinkSimple, MagnifyingGlass, Pulse, Robot, ShieldCheck, Siren, Terminal, Timer, Warning, WaveSine, Wrench, type Icon,
} from "@phosphor-icons/react";

import { PX_PATHS, isPxDemo, navigateTo, parseHealth, pxHref, usePxRead, type HealthView } from "../../lib/project-experience";
import { FX_HEALTH } from "../../lib/project-experience-fixtures";
import { Empty, EngineNotReady, Face, Flow, HEALTH_TONE, HEALTH_WORD, Ico, PxLink, ReadGate, Ring, Spark, Tag } from "./kit";
import { AppAlertsTab } from "../observability/alerts-tab";
import { usePulse } from "../../lib/obs-view";
import "../infra/infra.css";
import "../observability/obs.css";
import { P95_SOURCE } from "./tab-deployments";
import type { PxCtx } from "./project-page";

type Env = HealthView["environments"][number];
type Incident = HealthView["incidents"][number];
type AlertLine = HealthView["alerts"][number];

/** "Today 09:12", "Wed 09:12", "Nov 2 09:12": when something happened, in the viewer's zone. */
export function dayTime(iso: string, now: Date = new Date()): string {
  if (!/^\d{4}-\d{2}-\d{2}T/.test(iso)) return iso;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const hm = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
  const same = (x: Date, y: Date) => x.toDateString() === y.toDateString();
  if (same(d, now)) return `Today ${hm}`;
  const days = (now.getTime() - d.getTime()) / 86_400_000;
  // Always with its clock time (12-09 "Today 09:12 · 14 min"): an incident on Wednesday is
  // "Wed 09:12", never a bare "Wed".
  if (days < 6) return `${d.toLocaleDateString([], { weekday: "short" })} ${hm}`;
  return `${d.toLocaleDateString([], { month: "short", day: "numeric" })} ${hm}`;
}
/** "09:52" when it is today, else as dayTime ("Wed", "Nov 2"): THE ONE CLOCK of this tab, the same
 *  24-hour local text as the incidents ("Today 09:38") and the parity component's history
 *  ("Resolved 09:52"), never "9:52 AM" beside "09:52" (12-09). */
export function clockTime(iso: string, now: Date = new Date()): string {
  const d = new Date(iso);
  if (!/^\d{4}-\d{2}-\d{2}T/.test(iso) || Number.isNaN(d.getTime())) return iso;
  return d.toDateString() === now.toDateString()
    ? d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false }) : dayTime(iso, now);
}
/** 840 → "14 min", 7200 → "2 h". */
export const spanWords = (sec: number | null) => (sec === null ? "" : sec < 3600 ? `${Math.max(1, Math.round(sec / 60))} min` : `${Math.round(sec / 360) / 10} h`);

/** Each node of an incident's story drawn by what it is (screen 12). */
const NODE_LOOK: Record<string, { t: string; icon?: Icon }> = {
  signal: { t: "signal", icon: Bell }, cause: { t: "decision", icon: MagnifyingGlass }, fix: { t: "done", icon: Wrench },
  decision: { t: "waiting" },
};
/** Beacon's pack chips: what each kind of rule watches. */
const PACK_ICON: Record<string, Icon> = { Uptime: Pulse, "5xx": Warning, Latency: Timer, Disk: HardDrive, "DB conns": Cylinder, TLS: Certificate, Traffic: WaveSine, Deploys: GitBranch };
const PACK_ORDER = ["Uptime", "5xx", "Latency", "Disk", "DB conns", "TLS"];
const packOrder = (k: string) => { const i = PACK_ORDER.indexOf(k); return i < 0 ? 99 : i; };

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
            {h.environments.length ? h.environments.map((e) => <EnvCard key={e.key} e={e} ctx={ctx} />) : (
              <div className="px-card"><Empty icon={Heartbeat} tone="green" title="No environment to watch yet" line="Health starts once the app is deployed: Beacon installs a monitoring pack on the first deploy." testid="px-health-empty" /></div>
            )}
            {!h.environments.some((e) => /^prod(uction)?$/i.test(e.key)) ? (
              <section className="px-card px-locked px-h-prod" aria-label="Production" data-testid="px-h-prod">
                <div className="px-row px-wrap"><Ico icon={Buildings} c="amber" /><b>Production</b><Tag tone="amber">Not set up</Tag><small className="px-faint">Health appears once prod has a host</small>
                  <span className="px-spacer" /><button type="button" className="px-chip" onClick={() => ctx.tab("deployments")} data-testid="px-health-to-deploy">Deployments <ArrowRight size={10} /></button></div>
              </section>
            ) : null}
            <div className="px-split">
              <div className="px-stack">
                <section className="px-card" aria-labelledby="px-h-inc" data-testid="px-h-incidents">
                  <div className="px-card-h"><Ico icon={Siren} c="red" /><h2 id="px-h-inc">Incidents</h2>
                    {h.incidents.length ? <span className="px-nbadge" data-tone={h.incidents.some((i) => i.state !== "resolved") ? "amber" : undefined}>{h.incidents.length}</span> : null}
                    <a className="px-more" href={pxHref(`?area=agent_ops&project_id=${encodeURIComponent(ctx.projectId)}`)} data-testid="px-h-inc-all">In Agents at work <ArrowRight size={11} /></a></div>
                  {h.incidents.length ? h.incidents.map((i) => <IncidentRow key={i.id} i={i} />) : <NoIncidents />}
                </section>
                <section className="px-card" aria-labelledby="px-h-al" data-testid="px-h-alerts">
                  <div className="px-card-h"><Ico icon={BellRinging} c="amber" /><h2 id="px-h-al">Alerts</h2>
                    <Tag tone={h.alerts.some((x) => x.state === "firing") ? "red" : "green"}>{h.alerts.filter((x) => x.state === "firing").length} firing</Tag></div>
                  {h.alerts.length ? h.alerts.map((x) => <AlertLineRow key={x.id} x={x} />)
                    : <Empty icon={Bell} tone="green" title="Nothing has fired" line="Rules arrive with the first deploy; what fires shows here." testid="px-h-al-empty" />}
                </section>
              </div>
              <div className="px-stack">
                <Monitoring h={h} />
                <section className="px-card" aria-labelledby="px-h-deeper" data-testid="px-h-deeper">
                  <div className="px-card-h"><Ico icon={LinkSimple} c="blue" /><h2 id="px-h-deeper">Go deeper</h2></div>
                  <div className="px-h-links">
                    {h.links.grafana ? <a className="px-h-link" href={h.links.grafana} target="_blank" rel="noreferrer noopener" data-testid="px-h-grafana"><ChartLine size={14} aria-hidden /><span>Grafana dashboard</span><ArrowSquareOut size={12} aria-label="opens in a new tab" /></a>
                      : <span className="px-h-link" data-off data-testid="px-h-grafana-why" title={h.links.grafana_reason || "Grafana isn't answering for this install"}><ChartLine size={14} aria-hidden /><span>Grafana dashboard<small>{h.links.grafana_reason || "not connected"}</small></span><ArrowSquareOut size={12} aria-hidden /></span>}
                    {h.links.logs ? <a className="px-h-link" href={pxHref(h.links.logs)} data-testid="px-h-logs"><Terminal size={14} aria-hidden /><span>Logs · {(h.environments[0]?.label ?? "deploys").toLowerCase()}</span><CaretRight size={12} aria-hidden /></a> : null}
                    <a className="px-h-link" href={pxHref(h.links.observability || obs)} data-testid="px-h-observability" title="Org-wide view of every app and BOSS itself"><Pulse size={14} aria-hidden /><span>Observability › this app</span><ArrowRight size={12} aria-hidden /></a>
                  </div>
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

/** One environment: the four numbers that answer "is it OK?". The ring and the sparks open the
 *  environment's drawer with its full charts. */
function EnvCard({ e, ctx }: { e: Env; ctx: PxCtx }) {
  const open = () => ctx.open("env", e.target_id || e.key);
  const url = e.url || ctx.overview.project.live_url;
  const burnTone = e.slo.burn === "ok" ? "green" : e.slo.burn === "warning" ? "amber" : "red";
  const used = e.slo.budget_used_pct ?? (e.slo.burn === "ok" ? null : e.slo.burn === "warning" ? 65 : 100);
  return (
    <section className="px-card" aria-labelledby={`px-h-${e.key}`} data-testid={`px-h-env-${e.key}`}>
      <div className="px-card-h"><Ico icon={Heartbeat} c="green" /><h2 id={`px-h-${e.key}`}>{e.label}</h2>
        <Tag tone={HEALTH_TONE[e.health]} title={e.health_reason || undefined}><span className="px-dot" data-s={e.health} data-round aria-hidden />{HEALTH_WORD[e.health]}</Tag>
        {e.health_reason && e.health !== "healthy" ? <small className="px-h-why" data-testid={`px-h-env-why-${e.key}`} data-s={e.health}>{e.health_reason}</small> : null}
        {url ? <a className="px-chip" href={url} target="_blank" rel="noreferrer noopener" title="Opens the live app" data-testid={`px-h-env-url-${e.key}`}><Globe size={11} aria-hidden />{url.replace(/^https?:\/\//, "")} ↗</a> : null}
        <span className="px-spacer" /><small className="px-faint" title={P95_SOURCE.probe}>last 24h</small></div>
      <div className="px-grid px-g4 px-h-nums">
        <button type="button" className="px-h-num" onClick={open} data-testid={`px-h-env-uptime-${e.key}`} title="Full charts">
          <span className="px-lbl">Uptime</span>
          <span className="px-row" style={{ marginTop: 6 }}>
            <Ring pct={e.uptime_pct ?? 0} tone={e.health === "healthy" ? "green" : e.health === "down" ? "red" : "amber"} label={`Uptime ${e.uptime_pct ?? "unknown"}%`}>{e.uptime_pct ?? "—"}</Ring>
            {e.slo.target !== null ? <small className="px-faint">SLO {e.slo.target}%</small> : null}
          </span>
        </button>
        <button type="button" className="px-h-num" onClick={open} data-testid={`px-h-env-errors-${e.key}`} title="Full charts">
          <span className="px-lbl">Error rate</span>
          <span className="px-num px-mono">{e.error_rate ?? "—"}{e.error_rate !== null ? "%" : ""}</span>
          <Spark series={e.series.errors} tone="amber" label="Error rate, last 24 hours" />
        </button>
        <button type="button" className="px-h-num" onClick={open} data-testid={`px-h-env-p95-${e.key}`} title="Full charts">
          <span className="px-lbl" title={P95_SOURCE[e.p95_source || "probe"]}>p95 latency</span>
          <span className="px-num px-mono">{e.p95_ms ?? "—"}<small className="px-faint"> ms</small></span>
          <Spark series={e.series.latency} tone="teal" label="p95 latency, last 24 hours" />
        </button>
        <div className="px-h-num" data-testid={`px-h-env-slo-${e.key}`}>
          <span className="px-lbl">SLO burn</span>
          <span className="px-bar px-h-burn" role="img" aria-label={`Error budget ${used ?? 0}% used`}>
            <span style={{ width: `${Math.max(3, used ?? 0)}%`, background: `var(--${burnTone})` }} /></span>
          <span className="px-row" style={{ gap: 6 }}><Tag tone={burnTone}>{e.slo.burn === "ok" ? "OK" : e.slo.burn === "warning" ? "Burning" : "Breached"}</Tag>
            {used !== null ? <small className="px-faint" data-testid={`px-h-env-budget-${e.key}`}>{used}% of budget used</small> : null}</span>
        </div>
      </div>
    </section>
  );
}

/** One incident as its story: the signal → SRE squad → root cause → fix → recovered. */
function IncidentRow({ i }: { i: Incident }) {
  const resolved = i.state === "resolved";
  return (
    <div className="px-h-inc" data-testid={`px-h-inc-${i.id}`} data-s={i.state}>
      <span className="px-h-inc-ico" data-s={i.state} aria-hidden>{resolved ? <Check size={12} weight="bold" /> : "!"}</span>
      <div className="px-h-inc-b">
        <b>{i.label}</b>
        <small className="px-faint">{[i.since ? dayTime(i.since) : "", spanWords(i.duration_s), i.outcome].filter(Boolean).join(" · ")}</small>
        <Flow testid={`px-h-inc-flow-${i.id}`} nodes={i.mini.map((m) => {
          const look = NODE_LOOK[m.type] ?? NODE_LOOK[m.state];
          const I = look?.icon;
          return {
            // THE REAL TIME of each step (fired, mitigation live, recovery verified) on hover.
            label: m.label, state: m.state, title: m.at ? `${m.label} · ${dayTime(m.at)}` : undefined,
            type: m.type === "agent" ? "agent" : look?.t ?? (m.state === "done" ? "done" : m.state),
            icon: m.type === "agent" ? <Face agent={{ name: "Beacon", role_key: "sre" }} size={16} />
              : I ? <I size={11} aria-hidden /> : m.state === "done" ? <Check size={11} weight="bold" aria-hidden /> : undefined,
          };
        })} />
      </div>
      {i.graph_href ? <PxLink href={i.graph_href} className="px-chip px-h-graph" testid={`px-h-inc-graph-${i.id}`} title="The whole diagram, in Agents at work"><Robot size={11} aria-hidden /> Graph ↗</PxLink> : null}
    </div>
  );
}

/** No incidents: the path an incident WOULD take, drawn faint, so the empty card still says how
 *  it works (an alert fires → the SRE squad → the cause → the fix → recovered). */
function NoIncidents() {
  return (
    <div className="px-h-noinc" data-testid="px-h-inc-empty">
      <span className="px-h-inc-ico" data-s="resolved" aria-hidden><Check size={12} weight="bold" /></span>
      <div className="px-h-inc-b">
        <b>No incidents</b>
        <small className="px-faint">When an alert fires, the SRE squad works it here:</small>
        <Flow testid="px-h-inc-empty-flow" nodes={[
          { label: "Alert", state: "todo", icon: <Bell size={11} aria-hidden /> },
          { label: "SRE squad", state: "todo", icon: <Face agent={{ name: "Beacon", role_key: "sre" }} size={16} /> },
          { label: "Root cause", state: "todo", icon: <MagnifyingGlass size={11} aria-hidden /> },
          { label: "Fix", state: "todo", icon: <Wrench size={11} aria-hidden /> },
          { label: "Recovered", state: "todo", icon: <Check size={11} aria-hidden /> },
        ]} />
      </div>
    </div>
  );
}

/** One alert rule: its name, severity and where it stands (firing / resolved at … / ok · quiet). */
function AlertLineRow({ x }: { x: AlertLine }) {
  const when = x.state === "firing" ? `firing since ${x.since ? clockTime(x.since) : "now"}` : x.state === "resolved" ? `resolved ${x.since ? clockTime(x.since) : ""}`.trim() : x.quiet || "quiet";
  return (
    <div className="px-li px-h-al" data-testid={`px-h-al-${x.id}`} data-s={x.state}>
      <span className="px-dot" data-round data-s={x.state === "firing" ? "bad" : x.state === "resolved" ? "done" : undefined} aria-hidden />
      <span style={{ minWidth: 0 }}><b className="px-ell" title={x.condition || undefined}>{x.href ? <PxLink href={x.href} className="">{x.label}</PxLink> : x.label}</b>
        <small>{x.severity} · {when}</small></span>
      <Tag tone={x.state === "firing" ? "red" : x.state === "resolved" ? "green" : "idle"}>{x.state}</Tag>
    </div>
  );
}

/** Beacon's monitoring pack: who keeps it, how many rules, where they live, what they watch. */
function Monitoring({ h }: { h: HealthView }) {
  const m = h.monitoring_pack;
  return (
    <section className="px-card" aria-labelledby="px-h-mon" data-testid="px-h-monitoring">
      <div className="px-card-h"><Ico icon={ShieldCheck} c="teal" /><h2 id="px-h-mon">Monitoring</h2></div>
      {m && m.rules > 0 ? (
        <>
          <div className="px-cap">
            <Face agent={{ name: "Beacon", role_key: "sre" }} size={44} standalone />
            <div><div className="px-cap-name">Beacon&apos;s pack</div>
              <div className="px-cap-now"><span className="px-dot" data-s="done" aria-hidden />{m.rules} rule{m.rules === 1 ? "" : "s"}{m.off ? ` · ${m.off} off` : ""} · {m.managed_in === "repo" ? "in repo" : "set in BOSS"} · on deploy</div></div>
          </div>
          {m.kinds.length ? (
            <div className="px-row px-wrap px-h-kinds" data-testid="px-h-pack-kinds">
              {[...m.kinds].sort((x, y) => packOrder(x) - packOrder(y)).map((k) => { const I = PACK_ICON[k] ?? Bell; return <span key={k} className="px-tag"><I size={11} aria-hidden /> {k}</span>; })}
            </div>
          ) : null}
          {m.file?.external_url ? <a className="px-h-file" href={m.file.external_url} target="_blank" rel="noreferrer noopener" data-testid="px-h-pack-file"><FileCode size={12} aria-hidden /> {m.file.path} ↗</a>
            : m.href ? <PxLink href={m.href} testid="px-h-pack"><FileText size={11} aria-hidden /> The rules</PxLink> : null}
        </>
      ) : (
        <Empty icon={ShieldCheck} tone="teal" title="No rules yet · Beacon's pack installs on deploy"
               line={h.environments.length ? "Beacon writes the pack on the next deploy; until then BOSS only checks that the app answers."
                 : "Beacon installs a monitoring pack (uptime, 5xx, latency, disk, TLS) on the first deploy."}>
          {m?.href ? <PxLink href={m.href} testid="px-h-pack">Alerts in Observability</PxLink> : null}
        </Empty>
      )}
    </section>
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
        <Empty icon={Bell} tone="amber" title="No rules yet · Beacon's pack installs on deploy" line="Rules arrive with the first deploy: Beacon writes them to the app's boss.deploy.yaml." />
      </section>
    );
  }
  return (
    <section aria-labelledby="px-h-rules" className="px-stack" style={{ gap: 10 }} data-testid="px-h-rules">
      <div className="px-row"><Ico icon={Bell} c="amber" /><h2 id="px-h-rules" style={{ margin: 0, fontFamily: "var(--display)", fontSize: ".86rem" }}>Alert rules</h2>
        <small className="px-faint">the same rules and actions as Observability</small></div>
      {targets.map((t, i) => (
        <details key={t.targetId} className="px-card px-h-rules-card" open={i === 0} data-testid={`px-h-rules-${t.targetId}`}>
          <summary style={{ cursor: "pointer", fontWeight: 600 }}>{t.label}
            <a className="px-chip" style={{ marginLeft: 8 }} href={pxHref(`?area=observability&app=${encodeURIComponent(t.targetId)}&atab=alerts`)}
               data-testid={`px-h-rules-obs-${t.targetId}`}>In Observability</a></summary>
          <div className="inf" style={{ marginTop: 10 }}>
            <AppAlertsTab projectId={ctx.projectId} actor={ctx.actor} targetId={t.targetId} plainNames
                          onIncident={(id) => navigateTo(pxHref(`?area=observability&app=${encodeURIComponent(t.targetId)}${id ? `&inc=${encodeURIComponent(id)}` : ""}`))} />
          </div>
        </details>
      ))}
    </section>
  );
}
