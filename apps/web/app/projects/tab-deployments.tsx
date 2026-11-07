"use client";

/**
 * DEPLOYMENTS (screen 8, contract §5): how code ships (the gates from Govern policy, as a diagram
 * with an icon per node), one card per environment — URL, uptime ring, the 24 h strip, p95 and
 * errors, the last deploy ("PR #2 · 2h ago"), Logs, "runs on <server> ↗" into Infra, Redeploy —
 * production (Pick a host), the demo logins (user names, reveal), and the deploy history in words
 * ("Staging · PR #2 UI refresh · Wed 16:40 · 3m 10s · smoke ✓"). Servers stay org-wide in Infra.
 */
import React, { useState } from "react";
import {
  ArrowCounterClockwise, ArrowSquareOut, ArrowsClockwise, Buildings, Check, CheckCircle, ClockCounterClockwise, Cloud, Flask, GearSix, GitMerge, Globe,
  HardDrives, Key, LockSimple, Package, RocketLaunch, TerminalWindow, X,
} from "@phosphor-icons/react";

import { GatedButton } from "../../components/gated";
import { PX_PATHS, isPxDemo, parseDeployments, pxHref, usePxRead, type Deployments, type Environment } from "../../lib/project-experience";
import { FX_DEPLOYMENTS } from "../../lib/project-experience-fixtures";
import { deployTarget } from "../infra/infra-actions";
import { EngineNotReady, Face, Flow, HEALTH_TONE, HEALTH_WORD, Ico, PxLink, ReadGate, Ring, Said, Tag } from "./kit";
import { DemoLoginRow } from "./tab-settings";
import { whenLocal } from "../../lib/local-time";
import type { PxCtx } from "./project-page";

const BEACON = { name: "Beacon", role_key: "sre" };
/** A history row's state in a word, when the engine sent none (X-77: badge + colour + word). */
const HIST_WORD: Record<string, string> = { failed: "Failed", waiting: "Needs you", now: "Deploying", skipped: "Not approved", todo: "Queued" };
export const P95_SOURCE: Record<string, string> = { app: "p95 from the app's own requests (24 h)", probe: "p95 from BOSS's health probe (24 h)" };

/** "Wed 16:40" in the viewer's zone (a week's history reads by weekday), else "Nov 2". */
export function dayTime(at: string): string {
  const d = new Date(at);
  if (Number.isNaN(d.getTime())) return "";
  const week = Date.now() - d.getTime() < 6 * 86_400_000;
  return week ? d.toLocaleString(undefined, { weekday: "short", hour: "2-digit", minute: "2-digit", hour12: false })
    : d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

/** "2h ago", "3d ago": how long since, in one short token. */
export function agoWords(at: string): string {
  const t = new Date(at).getTime();
  if (!Number.isFinite(t)) return "";
  const s = Math.max(0, Math.round((Date.now() - t) / 1000));
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

/** The ship path's icon per node: merged, built, the environment's agent, the smoke test, prod's lock. */
function shipIcon(label: string, state: string): React.ReactNode {
  if (/merged/i.test(label)) return <GitMerge size={12} aria-hidden />;
  if (/built/i.test(label)) return <Package size={12} aria-hidden />;
  if (/smoke/i.test(label)) return <CheckCircle size={12} aria-hidden />;
  if (/prod/i.test(label) || state === "waiting") return <LockSimple size={11} weight="fill" aria-hidden />;
  return <Face agent={BEACON} size={16} />;
}

export function DeploymentsTab({ ctx }: { ctx: PxCtx }) {
  const read = usePxRead<Deployments>(PX_PATHS.deployments(ctx.projectId), parseDeployments, { everyMs: 20_000, demo: () => parseDeployments(FX_DEPLOYMENTS) });
  const infra = `?area=infra&view=deploys&project_id=${encodeURIComponent(ctx.projectId)}`;
  return (
    <div className="px-stack" data-testid="px-deployments">
      <ReadGate read={read} what="Deployments" missing={
        <EngineNotReady what="This project's deployments">
          {ctx.overview.project.live_url ? <a className="px-btn" href={ctx.overview.project.live_url} target="_blank" rel="noreferrer noopener">Open the app ↗</a> : null}
          <a className="px-btn" href={pxHref(infra)} data-testid="px-dep-infra">Deploys in Infra</a>
        </EngineNotReady>
      }>
        {(d) => {
          const logins = d.environments.flatMap((e) => e.demo_logins.map((l) => ({ e, l })));
          const envNames = [...new Set(logins.map((x) => x.e.label))];
          return (
            <>
              {d.ship_path.length ? (
                <section className="px-card" aria-labelledby="px-dep-ship">
                  <div className="px-card-h"><Ico icon={RocketLaunch} c="teal" /><h2 id="px-dep-ship">How code ships</h2>
                    <a className="px-more" href={pxHref("?area=budget&tab=autonomy")} data-testid="px-dep-policy">Policy in Govern</a></div>
                  <Flow nodes={d.ship_path.map((n) => ({
                    ...n, icon: shipIcon(n.label, n.state),
                    type: /prod/i.test(n.label) && n.state !== "done" ? "waiting" : /auto/i.test(n.label) ? "agent" : n.state,
                  }))} testid="px-dep-ship" />
                </section>
              ) : null}
              <div className="px-grid px-g2">
                {d.environments.map((e) => <EnvCard key={e.target_id || e.key} e={e} ctx={ctx} />)}
                <Production d={d} />
              </div>
              <section className="px-card" aria-labelledby="px-dep-logins" data-testid="px-dep-logins">
                <div className="px-card-h"><Ico icon={Key} c="green" /><h2 id="px-dep-logins">Demo logins</h2>
                  {envNames.map((n) => <Tag key={n}>{n}</Tag>)}
                  <a className="px-more" href="#" onClick={(ev) => { ev.preventDefault(); ctx.tab("settings", { section: "demo_logins" }); }} data-testid="px-dep-logins-settings"><GearSix size={11} aria-hidden /> Manage in Settings</a></div>
                {logins.length ? logins.map(({ e, l }) => (
                  <DemoLoginRow key={`${e.key}-${l.user || l.label}`} projectId={ctx.projectId} compact
                                login={{ label: l.label, value: l.user, sub: envNames.length > 1 ? e.label : "", reveal: l.reveal, href: "" }} />
                )) : <p className="px-faint" style={{ margin: 0 }} data-testid="px-dep-logins-none">No demo users yet: they show once the app is deployed and names them (its seed or boss.deploy.yaml).</p>}
              </section>
              <section className="px-card" aria-labelledby="px-dep-hist" data-testid="px-dep-history">
                <div className="px-card-h"><Ico icon={ClockCounterClockwise} c="blue" /><h2 id="px-dep-hist">History</h2>
                  <a className="px-more" href={pxHref(infra)}>All in Infra</a></div>
                {d.history.length ? d.history.map((h) => (
                  <a key={h.id} className="px-li px-hist" data-s={h.recovered ? "recovered" : h.state} data-testid={`px-dep-h-${h.id}`} href={pxHref(h.href)}
                     title={`${h.word} — opens this deploy in Infra`}>
                    <span className="px-ck-b" aria-hidden>
                      {h.recovered ? <ArrowCounterClockwise size={11} weight="bold" /> : h.state === "done" ? <Check size={11} weight="bold" /> : h.state === "failed" ? <X size={11} weight="bold" />
                        : h.state === "waiting" ? <LockSimple size={10} weight="fill" /> : h.state === "skipped" ? <X size={10} /> : <ArrowsClockwise size={11} />}</span>
                    <span style={{ minWidth: 0 }}><b className="px-ell" style={{ display: "block" }}>{h.label}
                      {h.state !== "done" ? <> <Tag tone={h.recovered || h.state === "waiting" || h.state === "now" ? "amber" : h.state === "failed" ? "red" : undefined}>
                        {h.recovered ? "Recovered" : h.word || HIST_WORD[h.state] || h.state}</Tag></> : null}</b>
                      <small>{[h.at ? dayTime(h.at) : "", h.sub, h.state === "waiting" ? "needs your approval" : ""].filter(Boolean).join(" · ")}</small></span>
                    {h.by && !h.by_agent ? <span title={`by ${h.by}`}><Face agent={{ name: h.by }} size={20} /></span> : <Face agent={BEACON} size={20} />}
                  </a>
                )) : <p className="px-faint">No deploys yet. The first one runs after the first merge.</p>}
              </section>
            </>
          );
        }}
      </ReadGate>
    </div>
  );
}

function EnvCard({ e, ctx }: { e: Environment; ctx: PxCtx }) {
  const [said, setSaid] = useState("");
  const [busy, setBusy] = useState(false);
  const redeploy = async () => {
    if (isPxDemo()) { setSaid("Sample mode — nothing was sent."); return; }
    setBusy(true);
    const r = await deployTarget(ctx.projectId, e.target_id, "", e.last_deploy?.commit || null);
    setBusy(false);
    setSaid(r.ok ? "Redeploy planned · approve it in Needs you." : (r.error ?? "Not planned."));
    if (r.ok) ctx.refreshWork();
  };
  const host = e.host_name || e.host;
  const url = e.url ? (/^https?:/.test(e.url) ? e.url : `http://${e.url}`) : "";
  return (
    <section className="px-card px-env" aria-labelledby={`px-env-${e.key}`} data-testid={`px-env-${e.key}`}>
      <div className="px-card-h"><Ico icon={Flask} c="green" />
        <h2 id={`px-env-${e.key}`}><button type="button" className="px-linkish" onClick={() => ctx.open("env", e.target_id || e.key)} data-testid={`px-env-open-${e.key}`}>{e.label}</button></h2>
        <Tag tone={HEALTH_TONE[e.health]}><span className="px-dot" data-s={e.health} data-round aria-hidden />{HEALTH_WORD[e.health]}</Tag>
        <span className="px-spacer" />
        {e.target_id && e.last_deploy ? (
          <GatedButton permission="deployment:create" projectId={ctx.projectId} className="px-btn" data-size="s" data-testid={`px-env-redeploy-${e.key}`}
                       title="Plan this commit again on this environment (asks for approval like any deploy)" disabled={busy} onClick={() => void redeploy()}>
            <ArrowCounterClockwise size={12} aria-hidden /> {busy ? "Planning…" : "Redeploy"}</GatedButton>
        ) : null}
      </div>
      {url ? <a className="px-env-url" href={url} target="_blank" rel="noreferrer noopener" data-testid={`px-env-url-${e.key}`}>
        <Globe size={12} aria-hidden />{url}<ArrowSquareOut size={10} aria-label="opens in a new tab" /></a> : null}
      <div className="px-env-m">
        {e.uptime_pct !== null ? <Ring pct={e.uptime_pct} tone={e.health === "healthy" ? "green" : "amber"} size="l" label={`Uptime ${e.uptime_pct}%`}>{Math.round(e.uptime_pct * 10) / 10}%</Ring> : null}
        <div style={{ flex: 1, minWidth: 0 }}>
          <span className="px-lbl">24h</span>
          {e.series.length ? (
            <div className="px-health" role="img" aria-label="Health over the last 24 hours">
              {e.series.map((b, i) => <span key={i} style={{ height: `${Math.max(20, b * 100)}%` }} data-s={b < 0.5 ? "bad" : b < 0.95 ? "warn" : undefined} />)}
            </div>
          ) : <div className="px-faint" style={{ fontSize: ".56rem" }}>No checks yet</div>}
        </div>
        <dl className="px-kv">
          <dt title={P95_SOURCE[e.p95_source ?? ""] ?? undefined}>p95</dt><dd className="px-mono">{e.p95_ms !== null ? `${e.p95_ms}ms` : "—"}</dd>
          <dt>Errors</dt><dd className="px-mono">{e.error_rate !== null ? `${e.error_rate}%` : "—"}</dd>
        </dl>
      </div>
      <div className="px-row px-wrap px-env-f">
        <Face agent={BEACON} size={20} />
        {e.last_deploy ? <span>Last deploy {e.last_deploy.pr_url
                  ? <a href={e.last_deploy.pr_url} target="_blank" rel="noreferrer noopener" title="Opens the pull request"><b>{e.last_deploy.label}</b></a>
                  : <b>{e.last_deploy.label}</b>} · {agoWords(e.last_deploy.at) || whenLocal(e.last_deploy.at)}</span> : <span className="px-faint">Not deployed yet</span>}
        {e.last_deploy?.commit ? <span className="px-mono px-faint">{e.last_deploy.commit}</span> : null}
        <span className="px-spacer" />
        {e.last_deploy?.href ? <PxLink href={e.last_deploy.href} testid={`px-env-logs-${e.key}`} title="This deploy and its log, in Infra"><TerminalWindow size={11} aria-hidden /> Logs</PxLink> : null}
        {host ? (
          <PxLink href={e.host_href || `?area=infra&view=fleet&q=${encodeURIComponent(e.host)}`} testid={`px-env-host-${e.key}`}
                  title={`${e.host} — opens this server in Infra${e.host_projects && e.host_projects > 1 ? ` (shared by ${e.host_projects} projects)` : ""}`}>
            <HardDrives size={11} aria-hidden /> runs on {host} ↗</PxLink>
        ) : null}
      </div>
      <Said text={said} tone={/^not |refus|fail/i.test(said) ? "red" : "green"} />
    </section>
  );
}

function Production({ d }: { d: Deployments }) {
  if (d.production.state === "ready") return null;
  return (
    <section className="px-card px-locked" aria-labelledby="px-dep-prod" data-testid="px-dep-prod">
      <div className="px-card-h"><Ico icon={Buildings} c="amber" /><h2 id="px-dep-prod">Production</h2><Tag tone="amber">Not set up</Tag></div>
      <div className="px-empty" style={{ padding: "14px 10px" }}>
        <svg width="120" height="64" viewBox="0 0 120 64" aria-hidden>
          <rect x="8" y="22" width="44" height="34" rx="6" fill="none" stroke="var(--border-strong)" strokeWidth="2" strokeDasharray="4 3" />
          <rect x="68" y="22" width="44" height="34" rx="6" fill="color-mix(in srgb, var(--amber) 14%, transparent)" stroke="var(--amber)" strokeWidth="2" />
          <path d="M52 39 H68" stroke="var(--amber)" strokeWidth="2" strokeDasharray="3 3" />
          <circle cx="104" cy="18" r="8" fill="var(--amber)" /><text x="104" y="22" textAnchor="middle" fontSize="11" fontWeight="800" fill="#1b1300">!</text>
        </svg>
        <h3>Pick a host for production</h3>
        <p>Beacon sets it up; you approve each prod deploy.</p>
        {d.production.action ? (
          <a className="px-btn" data-kind="amber" href={pxHref(d.production.action.href)} data-testid="px-dep-prod-act"><Cloud size={13} aria-hidden /> Pick host</a>
        ) : null}
      </div>
    </section>
  );
}
