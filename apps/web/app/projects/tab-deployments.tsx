"use client";

/**
 * DEPLOYMENTS (screen 8, contract §5): how code ships (the gates from Govern policy), one card per
 * environment — URL, health, last deploy, "runs on <host> ↗" into Infra — production (set up or
 * not), the demo logins, and the deploy history. Servers themselves stay org-wide in Infra.
 */
import React from "react";
import {
  ArrowSquareOut, Buildings, ClockCounterClockwise, Flask, Globe, HardDrives, Key, Lock, RocketLaunch, Terminal, Check, ArrowsClockwise,
} from "@phosphor-icons/react";

import { PX_PATHS, parseDeployments, pxHref, usePxRead, type Deployments, type Environment } from "../../lib/project-experience";
import { FX_DEPLOYMENTS } from "../../lib/project-experience-fixtures";
import { EngineNotReady, Face, Flow, HEALTH_TONE, HEALTH_WORD, Ico, PxLink, ReadGate, Ring, Tag } from "./kit";
import { DemoLoginRow } from "./tab-settings";
import type { PxCtx } from "./project-page";

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
        {(d) => (
          <>
            {d.ship_path.length ? (
              <section className="px-card" aria-labelledby="px-dep-ship">
                <div className="px-card-h"><Ico icon={RocketLaunch} c="teal" /><h2 id="px-dep-ship">How code ships</h2>
                  <a className="px-more" href={pxHref("?area=budget")} data-testid="px-dep-policy">Policy in Govern</a></div>
                <Flow nodes={d.ship_path.map((n) => ({ ...n, icon: n.state === "waiting" ? <Lock size={11} aria-hidden /> : undefined }))} testid="px-dep-ship" />
              </section>
            ) : null}
            <div className="px-grid px-g2">
              {d.environments.map((e) => <EnvCard key={e.key} e={e} ctx={ctx} />)}
              <Production d={d} />
            </div>
            {d.environments.some((e) => e.demo_logins.length) ? (
              <section className="px-card" aria-labelledby="px-dep-logins" data-testid="px-dep-logins">
                <div className="px-card-h"><Ico icon={Key} c="green" /><h2 id="px-dep-logins">Demo logins</h2>
                  <a className="px-more" href="#" onClick={(ev) => { ev.preventDefault(); ctx.tab("settings", { section: "demo_logins" }); }} data-testid="px-dep-logins-settings">Manage in Settings</a></div>
                {d.environments.flatMap((e) => e.demo_logins.map((l) => (
                  <DemoLoginRow key={`${e.key}-${l.user}`} projectId={ctx.projectId} login={{ label: l.label, value: l.user, sub: e.label, reveal: l.reveal, href: "" }} />
                )))}
              </section>
            ) : null}
            <section className="px-card" aria-labelledby="px-dep-hist" data-testid="px-dep-history">
              <div className="px-card-h"><Ico icon={ClockCounterClockwise} c="blue" /><h2 id="px-dep-hist">History</h2>
                <a className="px-more" href={pxHref(infra)}>All in Infra</a></div>
              {d.history.length ? d.history.map((h) => (
                <button key={h.id} type="button" className="px-li" data-testid={`px-dep-h-${h.id}`}
                        style={{ width: "100%", background: "none", border: 0, borderTop: "1px solid var(--border)", color: "inherit", font: "inherit", textAlign: "left", cursor: "pointer" }}
                        onClick={() => ctx.open("deploy", h.id)}>
                  <span className="px-ck-b" aria-hidden style={h.state === "done" ? { background: "var(--green)", borderColor: "var(--green)", color: "#032015" } : { borderColor: "var(--amber)", color: "var(--amber)" }}>
                    {h.state === "done" ? <Check size={11} weight="bold" /> : <ArrowsClockwise size={11} />}</span>
                  <span><b>{h.label}</b><small>{h.sub}</small></span>
                  <Face agent={{ name: "Beacon", role_key: "sre" }} size={20} />
                </button>
              )) : <p className="px-faint">No deploys yet. The first one runs after the first merge.</p>}
            </section>
          </>
        )}
      </ReadGate>
    </div>
  );
}

function EnvCard({ e, ctx }: { e: Environment; ctx: PxCtx }) {
  const bars = e.series.length ? e.series : [];
  return (
    <section className="px-card" aria-labelledby={`px-env-${e.key}`} data-testid={`px-env-${e.key}`}>
      <div className="px-card-h"><Ico icon={Flask} c="green" /><h2 id={`px-env-${e.key}`}>{e.label}</h2>
        <Tag tone={HEALTH_TONE[e.health]}><span className="px-dot" data-s={e.health} data-round aria-hidden />{HEALTH_WORD[e.health]}</Tag>
        <span className="px-spacer" />
        <button type="button" className="px-btn" data-size="s" data-testid={`px-env-open-${e.key}`} onClick={() => ctx.open("env", e.key)}>Details</button>
      </div>
      {e.url ? <a className="px-env-url" href={e.url} target="_blank" rel="noreferrer noopener" data-testid={`px-env-url-${e.key}`}><Globe size={12} aria-hidden />{e.url}<ArrowSquareOut size={10} aria-label="opens in a new tab" /></a> : null}
      <div className="px-row" style={{ margin: "12px 0", gap: 14 }}>
        {e.uptime_pct !== null ? <Ring pct={e.uptime_pct} tone={e.health === "healthy" ? "green" : "amber"} size="l" label={`Uptime ${e.uptime_pct}%`}>{e.uptime_pct}%</Ring> : null}
        {bars.length ? (
          <div style={{ flex: 1, minWidth: 0 }}>
            <span className="px-lbl">24h</span>
            <div className="px-health" role="img" aria-label="Health over the last 24 hours">
              {bars.map((b, i) => <span key={i} style={{ height: `${Math.max(20, b * 100)}%` }} data-s={b < 0.5 ? "bad" : b < 0.95 ? "warn" : undefined} />)}
            </div>
          </div>
        ) : null}
        <dl className="px-kv">
          {e.p95_ms !== null ? <><dt>p95</dt><dd className="px-mono">{e.p95_ms}ms</dd></> : null}
          {e.error_rate !== null ? <><dt>Errors</dt><dd className="px-mono">{e.error_rate}%</dd></> : null}
        </dl>
      </div>
      <div className="px-row px-wrap" style={{ borderTop: "1px solid var(--border)", paddingTop: 10, fontSize: ".6rem" }}>
        <Face agent={{ name: "Beacon", role_key: "sre" }} size={20} />
        {e.last_deploy ? <span>Last deploy <b>{e.last_deploy.label}</b> · {e.last_deploy.at} <span className="px-mono px-faint">{e.last_deploy.commit}</span></span> : <span className="px-faint">Not deployed yet</span>}
        <span className="px-spacer" />
        {e.last_deploy?.href ? <PxLink href={e.last_deploy.href} onOpen={() => ctx.open("deploy", e.key)}><Terminal size={11} aria-hidden /> Logs</PxLink> : null}
        {e.host ? (
          <PxLink href={e.host_href || `?area=infra&view=fleet&q=${encodeURIComponent(e.host)}`} testid={`px-env-host-${e.key}`} title="The server, in Infra (shared across projects)">
            <HardDrives size={11} aria-hidden /> runs on {e.host}</PxLink>
        ) : null}
      </div>
    </section>
  );
}

function Production({ d }: { d: Deployments }) {
  if (d.production.state === "ready") return null;
  return (
    <section className="px-card px-locked" aria-labelledby="px-dep-prod" data-testid="px-dep-prod">
      <div className="px-card-h"><Ico icon={Buildings} c="amber" /><h2 id="px-dep-prod">Production</h2><Tag tone="amber">Not set up</Tag></div>
      <div className="px-empty" style={{ padding: "12px 10px" }}>
        <svg width="110" height="50" viewBox="0 0 110 50" aria-hidden>
          <rect x="4" y="12" width="40" height="30" rx="5" fill="none" stroke="var(--border-strong)" strokeDasharray="4 3" />
          <line x1="44" y1="27" x2="62" y2="27" stroke="var(--amber)" strokeDasharray="3 3" />
          <rect x="62" y="12" width="44" height="30" rx="5" fill="color-mix(in srgb, var(--amber) 18%, transparent)" stroke="var(--amber)" strokeWidth="2" />
          <circle cx="98" cy="10" r="7" fill="var(--amber)" /><text x="98" y="14" textAnchor="middle" fontSize="10" fontWeight="800" fill="#1b1300">!</text>
        </svg>
        <h3>Pick a host for production</h3>
        <p>Beacon sets it up; you approve each prod deploy.</p>
        {d.production.action ? <PxLink href={d.production.action.href} className="px-btn" testid="px-dep-prod-act">{d.production.action.label}</PxLink> : null}
      </div>
    </section>
  );
}
