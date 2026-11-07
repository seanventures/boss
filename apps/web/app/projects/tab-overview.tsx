"use client";

/**
 * OVERVIEW (screen 4, contract §2): the journey, Needs you, four tiles (open work, running, staging
 * health, cost), the captain card, recent work, what's next, try it, this week. Every tile, row
 * and node is a link: in place (a drawer) or to the tab that holds it.
 */
import React, { useState } from "react";
import {
  ArrowRight, CaretRight, ChatCircleDots, ClockCounterClockwise, Coins, Globe, HandWaving, Heartbeat, Key, Kanban, Lightbulb,
  Lightning, MapTrifold, Path, SteeringWheel, Info,
} from "@phosphor-icons/react";

import { GatedButton } from "../../components/gated";
import { WORK_ORDER, WORK_WORDS, navigateTo, pxHref, runAccept, type NextItem, type WorkItem } from "../../lib/project-experience";
import { Empty, Face, Flow, HEALTH_WORD, Ico, Journey, PxLink, Ring, Said, Spark, Stations, Tag } from "./kit";
import { DemoLoginRow } from "./tab-settings";
import { whenLocal } from "../../lib/local-time";
import type { PxCtx } from "./project-page";

const WORK_TONE: Record<string, string> = { running: "var(--teal)", stalled: "color-mix(in srgb, var(--amber) 55%, var(--surface-3))", needs_you: "var(--amber)", planned: "var(--quiet)", done: "var(--green)", failed: "var(--red)" };

function goHref(ctx: PxCtx, href: string, fallback: () => void) {
  // A contract href that names this page's own tab switches in place; anything else navigates.
  try {
    if (href && /^https?:\/\//.test(href)) { window.open(href, "_blank", "noopener,noreferrer"); return; }
    if (href) {
      const p = new URLSearchParams(href.slice(href.indexOf("?") + 1));
      if (p.get("area") === "project" && (!p.get("project_id") || p.get("project_id") === ctx.projectId)) {
        const drawer = p.get("drawer");
        ctx.tab((p.get("tab") as never) ?? "overview", drawer ? { drawer } : {});
        return;
      }
      navigateTo(pxHref(href));
      return;
    }
  } catch { /* fall through */ }
  fallback();
}

export function OverviewTab({ ctx }: { ctx: PxCtx }) {
  const ov = ctx.overview;
  const t = ov.tiles;
  return (
    <div className="px-stack" data-testid="px-overview">
      {ov.journey.length ? (
        <section className="px-card" aria-labelledby="px-ov-j">
          <div className="px-card-h"><Ico icon={Path} c="teal" /><h2 id="px-ov-j">Journey</h2>
            <a className="px-more" href="#" title="Each stage opens what happened there" onClick={(e) => { e.preventDefault(); ctx.open("stage", "how"); }}>How it works <Info size={11} /></a></div>
          <Journey nodes={ov.journey} onOpen={(n) => goHref(ctx, n.href, () => ctx.open("stage", n.key))} />
        </section>
      ) : null}

      <div className="px-grid px-g4" data-testid="px-tiles">
        <Tile label="Open work" icon={Kanban} c="blue" testid="px-tile-work" onClick={() => goHref(ctx, t.open_work?.href ?? "", () => ctx.tab("work"))}>
          <span className="px-num">{t.open_work?.count ?? ctx.work?.items.filter((w) => w.state !== "done").length ?? 0}</span>
          {t.open_work ? <WorkBar by={t.open_work.by_state} /> : null}
        </Tile>
        <Tile label="Running" icon={Lightning} c="teal" testid="px-tile-running" onClick={() => goHref(ctx, t.running?.href ?? "", () => ctx.tab("work"))}>
          {t.running?.current ? (
            <div className="px-row">
              <Ring pct={t.running.current.progress[1] ? (100 * t.running.current.progress[0]) / t.running.current.progress[1] : 30} tone="teal"
                    label={`${t.running.current.progress[0]} of ${t.running.current.progress[1]} steps`}>
                {t.running.current.progress[1] ? `${t.running.current.progress[0]}/${t.running.current.progress[1]}` : "…"}</Ring>
              <div style={{ minWidth: 0 }}>
                <div className="px-ell" style={{ color: "var(--teal)", fontWeight: 600 }}>{t.running.current.label}</div>
                {t.running.current.agent ? <div className="px-row px-faint" style={{ fontSize: ".58rem" }}><Face agent={t.running.current.agent} size={18} state="working" /> {t.running.current.agent.name}</div> : null}
              </div>
            </div>
          ) : <span className="px-faint">Nothing running</span>}
        </Tile>
        <Tile label="Staging" icon={Heartbeat} c="green" testid="px-tile-staging" onClick={() => goHref(ctx, t.staging?.href ?? "", () => ctx.tab("health"))}>
          {t.staging ? (
            <>
              <div className="px-row">
                <Ring pct={t.staging.uptime_pct ?? 0} tone={t.staging.health === "healthy" ? "green" : t.staging.health === "down" ? "red" : "amber"} label={`Uptime ${t.staging.uptime_pct ?? "unknown"}%`}>
                  {t.staging.uptime_pct ?? "—"}</Ring>
                <div><div style={{ color: "var(--green)", fontWeight: 600 }}>{HEALTH_WORD[t.staging.health]}</div>
                  {t.staging.p95_ms !== null ? <span className="px-mono px-faint" style={{ fontSize: ".58rem" }}>p95 {t.staging.p95_ms}ms</span> : null}</div>
              </div>
              <Spark series={t.staging.series} tone="green" label="Staging latency, last 24 hours" />
            </>
          ) : <span className="px-faint">Not deployed yet</span>}
        </Tile>
        <Tile label="Cost · week" icon={Coins} c="violet" testid="px-tile-cost" onClick={() => goHref(ctx, t.cost_week?.href ?? "?area=budget", () => ctx.tab("settings", { section: "autonomy" }))}>
          {t.cost_week ? (
            <>
              <div className="px-row"><span className="px-num" style={{ color: "var(--teal)" }}>${t.cost_week.usd.toFixed(2)}</span>
                {t.cost_week.delta_pct !== null ? <Tag tone={t.cost_week.delta_pct <= 0 ? "green" : "amber"}>{t.cost_week.delta_pct > 0 ? "+" : ""}{t.cost_week.delta_pct}%</Tag> : null}</div>
              <Spark series={t.cost_week.series} fill label="Cost per day this week" />
              {t.cost_week.cap_usd ? <small className="px-faint">{Math.round((100 * t.cost_week.usd) / t.cost_week.cap_usd)}% of ${t.cost_week.cap_usd} cap</small> : null}
            </>
          ) : <span className="px-faint">No spend yet</span>}
        </Tile>
      </div>

      <div className="px-split">
        <div className="px-stack">
          <section className="px-card" aria-labelledby="px-ov-ny" data-testid="px-ov-needs">
            <div className="px-card-h"><Ico icon={HandWaving} c="amber" /><h2 id="px-ov-ny">Needs you</h2><span className="px-nbadge" data-tone={ov.needs_you.length ? "amber" : undefined}>{ov.needs_you.length}</span></div>
            {ov.needs_you.length ? ov.needs_you.map((it) => (
              <div key={it.id} className="px-ny" data-testid={`px-ov-ny-${it.id}`}>
                <span className="px-ny-ico" aria-hidden>!</span>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <button type="button" className="px-wk-t" onClick={() => ctx.open("needs", it.id)}>{it.label}</button>
                  {it.mini.length ? <Flow nodes={it.mini} /> : <small className="px-faint" style={{ display: "block" }}>{it.detail}</small>}
                </div>
                {it.action ? <ActionButton ctx={ctx} label={it.action.label} href={it.action.href} amber testid={`px-ov-ny-act-${it.id}`} /> : null}
              </div>
            )) : <Empty icon={HandWaving} tone="green" title="All clear" line="Nothing is waiting on you. Helm asks here when it needs a person." />}
          </section>

          <section className="px-card" aria-labelledby="px-ov-rw">
            <div className="px-card-h"><Ico icon={Kanban} c="blue" /><h2 id="px-ov-rw">Recent work</h2>
              <a className="px-more" href="#" onClick={(e) => { e.preventDefault(); ctx.tab("work"); }} data-testid="px-ov-all-work">All work <ArrowRight size={11} /></a></div>
            {ov.recent_work.length ? <div>{ov.recent_work.map((w) => <MiniRow key={w.id} w={w} ctx={ctx} />)}</div>
              : <Empty icon={Kanban} title="No work yet" line="Describe a feature or a bug and the crew takes it from there.">
                  <button type="button" className="px-btn" data-kind="primary" onClick={() => ctx.tab("work", { compose: "1" })}>New work</button></Empty>}
          </section>

          {ov.week.length ? (
            <section className="px-card" aria-labelledby="px-ov-wk">
              <div className="px-card-h"><Ico icon={ClockCounterClockwise} c="blue" /><h2 id="px-ov-wk">This week</h2></div>
              <div className="px-jr" role="list" aria-label="This week" data-testid="px-ov-week">
                {ov.week.map((w, i) => (
                  <React.Fragment key={`${w.label}-${i}`}>
                    {i ? <span className="px-jr-wire" data-s="done" aria-hidden style={{ marginTop: 20 }} /> : null}
                    <div className="px-jr-node" role="listitem" style={{ cursor: w.href ? "pointer" : "default", width: 110 }} data-testid={`px-ov-week-${i}`}
                         onClick={w.href ? () => goHref(ctx, w.href, () => undefined) : undefined}>
                      <span className="px-jr-meta">{whenLocal(w.at)}</span>
                      <span className="px-dot" data-round style={{ width: 12, height: 12, background: w.kind === "idea" ? "#8b7cf6" : w.kind === "pr" ? "var(--amber)" : "var(--green)" }} aria-hidden />
                      {w.href ? <a className="px-jr-cap" style={{ fontWeight: 500 }} href={pxHref(w.href)} {...(/^https?:/.test(w.href) ? { target: "_blank", rel: "noreferrer noopener" } : {})}
                                   onClick={(e) => { e.stopPropagation(); if (/^https?:/.test(w.href) || e.metaKey || e.ctrlKey) return; e.preventDefault(); goHref(ctx, w.href, () => undefined); }}>{w.label}</a>
                        : <span className="px-jr-cap" style={{ fontWeight: 500 }}>{w.label}</span>}
                    </div>
                  </React.Fragment>
                ))}
              </div>
            </section>
          ) : null}
        </div>

        <div className="px-stack">
          <CaptainCard ctx={ctx} />
          <NextCard ctx={ctx} />
          {ov.try_it && (ov.try_it.url || ov.try_it.demo_logins.length) ? (
            <section className="px-card" aria-labelledby="px-ov-try" data-testid="px-ov-try">
              <div className="px-card-h"><Ico icon={Key} c="green" /><h2 id="px-ov-try">Try it</h2></div>
              {ov.try_it.url ? (
                <div className="px-row" style={{ fontSize: ".64rem" }}><Globe size={13} aria-hidden /><span className="px-faint" style={{ width: 54 }}>URL</span>
                  <a className="px-mono" href={ov.try_it.url} target="_blank" rel="noreferrer noopener" data-testid="px-ov-try-url">{ov.try_it.url.replace(/^https?:\/\//, "")} ↗</a></div>
              ) : null}
              {ov.try_it.demo_logins.map((d) => <DemoLoginRow key={d.user} projectId={ctx.projectId} login={{ label: d.label, value: d.user, sub: "", reveal: d.reveal, href: "" }} compact />)}
            </section>
          ) : null}
        </div>
      </div>
    </div>
  );
}

function Tile({ label, icon, c, children, onClick, testid }: { label: string; icon: typeof Kanban; c: "blue" | "teal" | "green" | "violet"; children: React.ReactNode; onClick: () => void; testid: string }) {
  return (
    <button type="button" className="px-card" data-link data-testid={testid} onClick={onClick}
            style={{ textAlign: "left", font: "inherit", color: "inherit", display: "flex", flexDirection: "column", gap: 8 }}>
      <span className="px-row"><Ico icon={icon} c={c} /><span className="px-lbl">{label}</span><span className="px-spacer" /><CaretRight size={12} className="px-faint" aria-hidden /></span>
      {children}
    </button>
  );
}

function WorkBar({ by }: { by: Record<string, number> }) {
  const total = Object.values(by).reduce((x, y) => x + y, 0) || 1;
  return (
    <>
      <span className="px-bar" role="img" aria-label={WORK_ORDER.filter((k) => by[k]).map((k) => `${by[k]} ${WORK_WORDS[k]}`).join(", ")}>
        {WORK_ORDER.filter((k) => by[k]).map((k) => <span key={k} style={{ width: `${(100 * by[k]) / total}%`, background: WORK_TONE[k] }} />)}
      </span>
      <span className="px-row px-wrap px-mono" style={{ fontSize: ".56rem", gap: 8 }} aria-hidden>
        {WORK_ORDER.filter((k) => by[k]).map((k) => <span key={k} className="px-row" style={{ gap: 3 }}><span className="px-dot" style={{ background: WORK_TONE[k] }} />{by[k]}</span>)}
      </span>
    </>
  );
}

function MiniRow({ w, ctx }: { w: WorkItem; ctx: PxCtx }) {
  return (
    <div className="px-wk" data-s={w.state} style={{ gridTemplateColumns: "14px minmax(0,1fr) auto auto 14px" }} data-testid={`px-ov-work-${w.id}`}
         onClick={() => ctx.open("work", w.id)}>
      <span className="px-dot" data-s={w.state === "running" ? "now" : w.state === "needs_you" || w.state === "stalled" ? "you" : w.state === "failed" ? "bad" : w.state === "done" ? "done" : undefined} aria-hidden />
      <span style={{ minWidth: 0 }}>
        <button type="button" className="px-wk-t px-ell" style={{ display: "block", maxWidth: "100%" }} onClick={(e) => { e.stopPropagation(); ctx.open("work", w.id); }}>{w.label}</button>
        <span className="px-wk-sub"><span>{w.state === "stalled" ? "Stalled" : w.links.pr ? `PR #${w.links.pr.number}` : w.kind[0].toUpperCase() + w.kind.slice(1)}</span>{w.age ? <span className="px-mono">{w.age}</span> : null}
          {w.cost_usd !== null ? <span className="px-mono">${w.cost_usd.toFixed(2)}</span> : null}</span>
      </span>
      <span className="px-hide-phone"><Stations stages={w.stages} /></span>
      <span className="px-hide-phone"><FacesInline w={w} /></span>
      <CaretRight size={12} className="px-faint" aria-hidden />
    </div>
  );
}
function FacesInline({ w }: { w: WorkItem }) {
  return <span className="px-faces">{w.agents.slice(0, 3).map((a, i) => <Face key={`${a.name}-${i}`} agent={a} size={20} state={w.state === "running" && i === w.agents.length - 1 ? "working" : "idle"} />)}</span>;
}

/** An action from the contract: in-page hrefs switch tab, others navigate, external ones open a tab. */
export function ActionButton({ ctx, label, href, amber, testid }: { ctx: PxCtx; label: string; href: string; amber?: boolean; testid?: string }) {
  if (/^https?:\/\//.test(href)) {
    return <a className="px-btn" data-kind={amber ? "amber" : undefined} data-size="s" href={href} target="_blank" rel="noreferrer noopener" data-testid={testid}>{label} ↗</a>;
  }
  return <button type="button" className="px-btn" data-kind={amber ? "amber" : undefined} data-size="s" data-testid={testid}
                 onClick={() => goHref(ctx, href, () => undefined)} disabled={!href}>{label}</button>;
}

function CaptainCard({ ctx }: { ctx: PxCtx }) {
  const c = ctx.overview.captain;
  if (!c) {
    return (
      <section className="px-card" aria-labelledby="px-ov-cap">
        <div className="px-card-h"><Ico icon={SteeringWheel} c="blue" /><h2 id="px-ov-cap">Captain</h2></div>
        <Empty icon={SteeringWheel} title="No captain yet" line="A captain owns this project: it onboards it, plans the work and answers for it.">
          <button type="button" className="px-btn" onClick={() => ctx.tab("settings", { section: "captain" })}>Choose a captain</button>
          <button type="button" className="px-btn" onClick={() => ctx.ask()}><ChatCircleDots size={13} /> Ask BOSS</button>
        </Empty>
      </section>
    );
  }
  return (
    <section className="px-card" aria-labelledby="px-ov-cap" data-testid="px-ov-captain">
      <div className="px-card-h"><Ico icon={SteeringWheel} c="blue" /><h2 id="px-ov-cap">Captain</h2>
        <a className="px-more" href="#" data-testid="px-ov-captain-change" onClick={(e) => { e.preventDefault(); ctx.tab("settings", { section: "captain" }); }}>Change</a></div>
      <div className="px-cap">
        <Face agent={c} size={48} state={c.status ? "working" : "idle"} standalone />
        <div style={{ minWidth: 0 }}>
          <div className="px-row"><span className="px-cap-name">{c.name}</span><Tag tone="blue">{c.role_key === "engineering_captain" ? "Engineering Captain" : "Captain"}</Tag></div>
          {c.status ? <div className="px-cap-now"><span className="px-dot" data-s="now" aria-hidden />{c.status}</div> : null}
        </div>
      </div>
      <div className="px-row px-wrap" style={{ marginTop: 12 }}>
        <button type="button" className="px-btn" data-kind="primary" data-testid="px-ov-ask" onClick={() => ctx.ask()}><ChatCircleDots size={13} /> Ask {c.name}</button>
        <button type="button" className="px-btn" data-testid="px-ov-plan-week" onClick={() => ctx.ask("Plan next week for this project.")}><MapTrifold size={13} /> Plan next week</button>
      </div>
      {c.squad.length ? (
        <div className="px-crew" data-testid="px-ov-crew">
          {c.squad.map((m) => (
            <span key={m.name} className="px-crew-m"><Face agent={m} size={22} /><span>{m.name}<small>{m.role}</small></span></span>
          ))}
        </div>
      ) : null}
    </section>
  );
}

/** Accepting a roadmap item plans it (work_item:manage); accepting a proposed one starts it
 *  through THE start door (workflow_run:start) — each item says which (§2 `accept`). */
const acceptPermission = (n: NextItem) => (n.accept && n.accept.href.startsWith("/v1/ambient/") ? "work_item:manage" : "workflow_run:start");

function NextCard({ ctx }: { ctx: PxCtx }) {
  const [said, setSaid] = useState("");
  const accept = async (n: NextItem) => {
    if (!n.accept) return;
    const r = await runAccept(ctx.projectId, n.accept);
    const planned = n.accept.href.startsWith("/v1/ambient/");
    setSaid(r.ok ? (r.demo ? "Sample mode — nothing was sent." : planned ? `${n.label} is planned work now.` : `${n.label} started — it's on Work.`)
      : (r.error ?? "Not accepted."));
    if (r.ok && !r.demo) ctx.refreshWork();
  };
  const next = ctx.overview.next;
  return (
    <section className="px-card" aria-labelledby="px-ov-next" data-testid="px-ov-next">
      <div className="px-card-h"><Ico icon={Lightbulb} c="violet" /><h2 id="px-ov-next">What&apos;s next</h2>
        <a className="px-more" href="#" onClick={(e) => { e.preventDefault(); ctx.tab("roadmap"); }} data-testid="px-ov-roadmap">Roadmap <ArrowRight size={11} /></a></div>
      {next.length ? next.slice(0, 3).map((n, i) => (
        <div key={n.id || i} className="px-li">
          <span className="px-rank">{i + 1}</span>
          <span style={{ minWidth: 0 }}>
            {n.href ? <PxLink href={n.href} className="px-wk-t" onOpen={() => goHref(ctx, n.href, () => undefined)}><b>{n.label}</b></PxLink> : <b>{n.label}</b>}
            <small>{n.source}</small>
          </span>
          {n.accept ? (
            <GatedButton permission={acceptPermission(n)} projectId={ctx.projectId} className="px-btn" data-size="s" data-testid={`px-ov-accept-${n.id}`}
                         title={n.accept.href.startsWith("/v1/ambient/") ? "Plan it: it joins Work as planned" : "Start it now through the project's normal path"}
                         onClick={() => void accept(n)}>{n.accept.href.startsWith("/v1/ambient/") ? "Accept" : "Start"}</GatedButton>
          ) : <Tag tone="green">Next</Tag>}
        </div>
      )) : <p className="px-faint" style={{ margin: 0 }}>Helm proposes what&apos;s next once the backlog is ranked.</p>}
      <Said text={said} />
    </section>
  );
}
