"use client";

/**
 * OVERVIEW (screen 4, contract §2): the journey, Needs you, four tiles (open work, running, staging
 * health, cost), the captain card, recent work, what's next, try it, this week. Every tile, row
 * and node is a link: in place (a drawer) or to the tab that holds it.
 */
import React, { useEffect, useRef, useState } from "react";
import {
  ArrowRight, CaretRight, ChatCircleDots, ClockCounterClockwise, Coins, Globe, HandWaving, Heartbeat, Key, Kanban, Lightbulb,
  Lightning, MapTrifold, Path, SteeringWheel, Info, Cloud, GearSix, GitMerge, RocketLaunch, ListChecks, Wrench, Question, Lock, Bug, Ticket, Sparkle, Warning,
  type Icon,
} from "@phosphor-icons/react";

import { GatedButton } from "../../components/gated";
import { KIND_WORDS, WORK_ORDER, WORK_WORDS, navigateTo, pxHref, runAccept, startOnboarding, workWhen, writePxUrl, type NextItem, type WorkItem } from "../../lib/project-experience";
import { Empty, Face, Flow, HEALTH_WORD, Ico, Journey, PxLink, Ring, Said, Spark, Stations, Tag, Timeline, Cost, CardFailed } from "./kit";
import { DemoLoginRow } from "./tab-settings";
import { P95_SOURCE, p95Words } from "./tab-deployments";
import { whenLocal } from "../../lib/local-time";
import type { PxCtx } from "./project-page";

/** Each Needs-you card's icon, by what it asks. */
const NEED_ICON: Record<string, Icon> = { merge: GitMerge, deploy: RocketLaunch, plan: ListChecks, setup: Wrench, question: Question };
/** Where a What's-next item came from, in words and an icon. */
const SOURCE_WORDS: Record<string, { word: string; icon: Icon }> = {
  roadmap: { word: "Roadmap", icon: MapTrifold }, ticket: { word: "Open issue", icon: Ticket }, bug: { word: "Bug", icon: Bug },
  ci: { word: "Failing CI", icon: Warning },
};
const NEXT_KIND_WORDS: Record<string, string> = {
  feature: "Feature", test_gap: "Test gap", incident_hotspot: "Incident hotspot", alert_gap: "Alert gap", bug: "Bug", ticket: "Ticket", chore: "Chore",
  security: "Security", dependency: "Dependency", performance: "Performance", docs: "Docs",
};

/** What each crew member does, in one word (screen 04's crew row). */
const CREW_VERB: Record<string, string> = {
  product_manager: "Plans", planner: "Plans", engineer: "Builds", software_engineer: "Builds", builder: "Builds", qa: "Tests", qa_engineer: "Tests",
  sre: "Deploys", devops: "Deploys", chief_of_staff: "Ranks", architect: "Designs", chief_architect: "Designs", security: "Secures",
  security_reviewer: "Secures", adversarial_reviewer: "Reviews", reviewer: "Reviews", designer: "Designs", engineering_captain: "Captains",
};

const WORK_TONE: Record<string, string> = { running: "var(--teal)", stalled: "color-mix(in srgb, var(--amber) 55%, var(--surface-3))", needs_you: "var(--amber)", planned: "var(--quiet)", done: "var(--green)", failed: "var(--red)" };

/** 04-15 the cost delta tag: green when the week costs less than last week, red when more, and
 *  "first week" (idle) when last week cost nothing, so there is no % to show. */
function CostDelta({ pct, first, what }: { pct: number | null; first?: boolean; what: string }) {
  if (pct !== null) return <Tag tone={pct <= 0 ? "green" : "red"} title={what}>{pct > 0 ? "+" : "−"}{Math.abs(pct)}%</Tag>;
  return first ? <Tag tone="idle" title="Nothing was spent last week, so there is no change to show">first week</Tag> : null;
}

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

/** A Needs-you item that arrived after the page first drew glows once (aa-glow), then stays still. */
function useFresh(ids: string[]): (id: string) => boolean {
  const seen = useRef<Set<string> | null>(null);
  const [fresh, setFresh] = useState<Set<string>>(new Set());
  useEffect(() => {
    if (seen.current === null) { seen.current = new Set(ids); return; }
    const arrived = ids.filter((i) => !seen.current!.has(i));
    arrived.forEach((i) => seen.current!.add(i));
    if (arrived.length) {
      setFresh(new Set(arrived));
      const t = window.setTimeout(() => setFresh(new Set()), 2400);
      return () => window.clearTimeout(t);
    }
    return undefined;
  }, [ids.join("|")]); // eslint-disable-line react-hooks/exhaustive-deps
  return (id) => fresh.has(id);
}

export function OverviewTab({ ctx }: { ctx: PxCtx }) {
  const ov = ctx.overview;
  const t = ov.tiles;
  const fresh = useFresh(ov.needs_you.map((n) => n.id));
  // EACH CARD LOADS OR FAILS ON ITS OWN (04 states): a card BOSS couldn't read this time says
  // "Couldn't load · Retry" inside itself; every other card still shows.
  const failed = (card: string) => (ov.errors?.[card]
    ? <CardFailed card={card} why={ov.errors[card]} onRetry={ctx.refreshOverview} /> : null);
  return (
    <div className="px-stack" data-testid="px-overview">
      {ov.journey.length || ov.errors?.journey ? (
        <section className="px-card" aria-labelledby="px-ov-j">
          <div className="px-card-h"><Ico icon={Path} c="teal" /><h2 id="px-ov-j">Journey</h2>
            <a className="px-more" href="#" title="Each stage opens what happened there" onClick={(e) => { e.preventDefault(); ctx.open("stage", "how"); }}>How it works <Info size={11} /></a></div>
          {failed("journey") ?? <Journey nodes={ov.journey} onOpen={(n) => goHref(ctx, n.href, () => ctx.open("stage", n.key))} />}
        </section>
      ) : null}

      <div className="px-grid px-g4" data-testid="px-tiles">
        <Tile label="Open work" icon={Kanban} c="blue" testid="px-tile-work" onClick={() => goHref(ctx, t.open_work?.href ?? "", () => ctx.tab("work"))}>
          {failed("tile_work") ?? <>
          <span className="px-num">{t.open_work?.count ?? ctx.work?.items.filter((w) => w.state !== "done").length ?? 0}</span>
          {t.open_work ? <WorkBar by={t.open_work.by_state} /> : null}</>}
        </Tile>
        <Tile label="Running" icon={Lightning} c="teal" testid="px-tile-running"
              onClick={() => (t.running?.current?.id ? ctx.open("work", t.running.current.id) : goHref(ctx, t.running?.href ?? "", () => ctx.tab("work")))}>
          {failed("tile_running") ?? (t.running?.current ? (
            <div className="px-row">
              <Ring pct={t.running.current.progress[1] ? (100 * t.running.current.progress[0]) / t.running.current.progress[1] : 30} tone="teal"
                    label={`${t.running.current.progress[0]} of ${t.running.current.progress[1]} steps`}>
                {t.running.current.progress[1] ? `${t.running.current.progress[0]}/${t.running.current.progress[1]}` : "…"}</Ring>
              <div style={{ minWidth: 0 }}>
                <div className="px-ell" style={{ color: "var(--teal)", fontWeight: 600 }}>{t.running.current.label}</div>
                <div className="px-row px-faint" style={{ fontSize: ".58rem" }}>
                  {t.running.current.agent ? <Face agent={t.running.current.agent} size={18} state="working" /> : null}
                  <span>{t.running.current.sub || t.running.current.agent?.name}</span></div>
              </div>
            </div>
          ) : <span className="px-faint">Nothing running</span>)}
        </Tile>
        <Tile label="Staging" icon={Heartbeat} c="green" testid="px-tile-staging" onClick={() => goHref(ctx, t.staging?.href ?? "", () => ctx.tab("health"))}>
          {failed("tile_staging") ?? (t.staging ? (
            <>
              <div className="px-row">
                <Ring pct={t.staging.uptime_pct ?? 0} tone={t.staging.health === "healthy" ? "green" : t.staging.health === "down" ? "red" : "amber"} label={`Uptime ${t.staging.uptime_pct ?? "unknown"}%`}>
                  {t.staging.uptime_pct ?? "—"}</Ring>
                <div><div style={{ color: `var(--${t.staging.health === "healthy" ? "green" : t.staging.health === "down" ? "red" : t.staging.health === "degraded" ? "amber" : "muted"})`, fontWeight: 600 }}
                           title={t.staging.health_reason || undefined} data-testid="px-tile-staging-health">{HEALTH_WORD[t.staging.health]}</div>
                  {t.staging.p95_ms !== null ? <span className="px-mono px-faint" style={{ fontSize: ".58rem" }} title={P95_SOURCE[t.staging.p95_source ?? ""] ?? undefined} data-testid="px-tile-staging-p95">{p95Words(t.staging.p95_ms, t.staging.window)}</span> : null}</div>
              </div>
              <Spark series={t.staging.series} tone="green" label="Staging latency, last 24 hours" />
            </>
          ) : <span className="px-faint">Not deployed</span>)}
        </Tile>
        <Tile label="Cost · week" icon={Coins} c="violet" testid="px-tile-cost" onClick={() => goHref(ctx, t.cost_week?.href ?? "?area=budget", () => ctx.tab("settings", { section: "autonomy" }))}>
          {failed("tile_cost") ?? (t.cost_week ? (
            <>
              {t.cost_week.on_subscription ? (
                <>
                  <div className="px-row"><span className="px-num" style={{ color: "var(--teal)" }}>$0</span><Tag tone="green" title="Runs on your model subscription: no money spent this week">On subscription</Tag>
                    <CostDelta pct={t.cost_week.list_delta_pct ?? null} first={t.cost_week.first_week} what="List price against last week" /></div>
                  <Spark series={t.cost_week.list_series ?? []} fill label="List price per day this week" />
                  <small className="px-faint">≈ ${(t.cost_week.list_usd ?? 0).toFixed(2)} at list price{t.cost_week.cap_usd ? ` · 0% of $${t.cost_week.cap_usd} cap` : ""}</small>
                </>
              ) : (
                <>
              <div className="px-row"><span className="px-num" style={{ color: "var(--teal)" }}>${t.cost_week.usd.toFixed(2)}</span>
                <CostDelta pct={t.cost_week.delta_pct} first={t.cost_week.first_week} what="Against last week" /></div>
              <Spark series={t.cost_week.series} fill label="Cost per day this week" />
              {t.cost_week.cap_usd ? <small className="px-faint" data-testid="px-tile-cost-cap">{t.cost_week.cap_pct ?? Math.round((100 * t.cost_week.usd) / t.cost_week.cap_usd)}% of ${t.cost_week.cap_usd} cap</small>
                : <small className="px-faint" data-testid="px-tile-cost-cap">No weekly cap set</small>}
                </>
              )}
            </>
          ) : <span className="px-faint">No spend yet</span>)}
        </Tile>
      </div>

      <div className="px-split">
        <div className="px-stack">
          <section className="px-card" aria-labelledby="px-ov-ny" data-testid="px-ov-needs">
            <div className="px-card-h"><Ico icon={HandWaving} c="amber" /><h2 id="px-ov-ny">Needs you</h2><span className="px-nbadge" data-tone={ov.needs_you.length ? "amber" : undefined}>{ov.needs_you.length}</span></div>
            {failed("needs_you") ?? (ov.needs_you.length ? ov.needs_you.map((it) => {
              const KindIcon = (it.icon === "cloud" ? Cloud : NEED_ICON[it.kind]) ?? Question;
              return (
                <div key={it.id} className="px-ny" data-testid={`px-ov-ny-${it.id}`} data-new={fresh(it.id) ? "1" : undefined}
                     role="button" tabIndex={-1} onClick={(e) => { if ((e.target as HTMLElement).closest("button,a")) return; ctx.open("needs", it.id); }}>
                  <span className="px-ny-ico" aria-hidden><KindIcon size={12} weight="bold" /></span>
                  <div className="px-ny-b">
                    <button type="button" className="px-wk-t" onClick={() => ctx.open("needs", it.id)}>{it.label}</button>
                    {it.mini.length ? <Flow nodes={it.mini.map((m) => ({ ...m, icon: m.state === "waiting" ? <Lock size={10} weight="fill" aria-hidden /> : undefined }))} testid={`px-ov-ny-flow-${it.id}`} />
                      : it.detail ? <small className="px-faint" style={{ display: "block" }}>{it.detail}</small> : null}
                    {it.mini.length && it.kind === "setup" && it.detail ? <small className="px-faint">{it.detail}</small> : null}
                  </div>
                  {it.action ? <ActionButton ctx={ctx} label={it.action.label} href={it.action.href} amber testid={`px-ov-ny-act-${it.id}`} /> : null}
                </div>
              );
                        }) : <Empty icon={HandWaving} tone="green" title="All clear" line="Nothing is waiting on you. Helm asks here when it needs a person." />)}
          </section>

          <section className="px-card" aria-labelledby="px-ov-rw">
            <div className="px-card-h"><Ico icon={Kanban} c="blue" /><h2 id="px-ov-rw">Recent work</h2>
              <a className="px-more" href="#" onClick={(e) => { e.preventDefault(); ctx.tab("work"); }} data-testid="px-ov-all-work">All work <ArrowRight size={11} /></a></div>
            {failed("recent_work") ?? (ov.recent_work.length ? <div>{ov.recent_work.map((w) => <MiniRow key={w.id} w={w} ctx={ctx} />)}</div>
              : <Empty icon={Kanban} title="No work yet" line="Describe a feature or a bug and the crew takes it from there.">
                  <button type="button" className="px-btn" data-kind="primary" onClick={() => ctx.tab("work", { compose: "1" })}>New work</button></Empty>)}
          </section>

          {ov.week.length || ov.errors?.week ? (
            <section className="px-card px-hide-phone" aria-labelledby="px-ov-wk">
              <div className="px-card-h"><Ico icon={ClockCounterClockwise} c="blue" /><h2 id="px-ov-wk">This week</h2></div>
              {failed("week") ?? <Timeline points={ov.week} testid="px-ov-week" onOpen={(i) => goHref(ctx, ov.week[i].href, () => undefined)} />}
            </section>
          ) : null}
        </div>

        <div className="px-stack">
          <CaptainCard ctx={ctx} />
          <NextCard ctx={ctx} />
          {ov.try_it && (ov.try_it.url || ov.try_it.demo_logins.length || ov.errors?.try_it) ? (
            <section className="px-card" aria-labelledby="px-ov-try" data-testid="px-ov-try">
              <div className="px-card-h"><Ico icon={Key} c="green" /><h2 id="px-ov-try">Try it</h2>
                <a className="px-more" href="#" data-testid="px-ov-try-settings" onClick={(e) => { e.preventDefault(); ctx.tab("settings", { section: "demo_logins" }); }}><GearSix size={11} aria-hidden /> Settings</a></div>
              {failed("try_it")}
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
  // The onboarding agent's run is a piece of work too (05-27): its own drawer, its own mini flow.
  const setup = w.kind === "setup" && w.source === "onboarding";
  const open = () => (setup ? writePxUrl({ drawer: "onboarding" }) : ctx.open("work", w.id));
  return (
    <div className="px-wk px-ov-wk" data-s={w.state} data-testid={`px-ov-work-${w.id}`}
         onClick={open}>
      <span className="px-dot" data-s={w.state === "running" ? "now" : w.state === "needs_you" || w.state === "stalled" ? "you" : w.state === "failed" ? "bad" : w.state === "done" ? "done" : undefined} aria-hidden />
      <span style={{ minWidth: 0 }}>
        <button type="button" className="px-wk-t px-ell" style={{ display: "block", maxWidth: "100%" }} title={w.detail || w.label} onClick={(e) => { e.stopPropagation(); open(); }}>{w.label}</button>
        <span className="px-wk-sub"><span>{w.state === "stalled" ? "Stalled" : w.links.pr ? `PR #${w.links.pr.number}` : KIND_WORDS[w.kind] ?? w.kind}</span>{workWhen(w) ? <span className="px-mono">{workWhen(w)}</span> : null}
          <Cost usd={w.cost_usd} list={w.list_usd} /></span>
      </span>
      {/* 04 at 390: the station strip stays (wireframe .wk-row > .st); faces and the chevron go. */}
      <span className="px-ov-wk-st">{w.flow.length ? <Flow nodes={w.flow.map((f) => ({ label: f.label, state: f.state, type: f.state }))} /> : <Stations stages={w.stages} />}</span>
      <span className="px-hide-phone"><FacesInline w={w} /></span>
      <CaretRight size={12} className="px-faint px-hide-phone" aria-hidden />
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
  if (ctx.overview.errors?.captain) {
    return (
      <section className="px-card" aria-labelledby="px-ov-cap" data-testid="px-ov-captain">
        <div className="px-card-h"><Ico icon={SteeringWheel} c="blue" /><h2 id="px-ov-cap">Captain</h2></div>
        <CardFailed card="captain" why={ctx.overview.errors.captain} onRetry={ctx.refreshOverview} />
      </section>
    );
  }
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
        <a className="px-more" href="#" data-testid="px-ov-captain-change" title="Hand the project to another captain" onClick={(e) => { e.preventDefault(); ctx.tab("settings", { section: "captain" }); }}>Change</a></div>
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
      {c.squad.length || c.ranker ? (
        <div className="px-crew" data-testid="px-ov-crew">
          {/* 04-34 the label and its tooltip read the ONE verb the engine gives (`role`). */}
          {[...c.squad, ...(c.ranker ? [c.ranker] : [])].map((m) => (
            <a key={m.role_key || m.name} className="px-crew-m" href={pxHref(`?area=workforce&role=${encodeURIComponent(m.role_key || m.name)}`)}
               title={m.role_key === "chief_of_staff" ? "Chief of Staff ranks this project's backlog against the others" : `${m.name} ${(m.role ?? "works here").toLowerCase()} — opens Agent Studio`}
               data-testid={`px-ov-crew-${m.role_key || m.name}`}>
              <Face agent={m} size={22} /><span>{m.name}<small>{m.role || CREW_VERB[m.role_key || ""] || ""}</small></span></a>
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
      {ctx.overview.errors?.next ? <CardFailed card="next" why={ctx.overview.errors.next} onRetry={ctx.refreshOverview} />
        : next.length ? next.slice(0, 3).map((n, i) => {
        const src = SOURCE_WORDS[n.source] ?? SOURCE_WORDS.roadmap;
        const Src = src.icon;
        const kind = n.kind ? (NEXT_KIND_WORDS[n.kind] ?? n.kind.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase())) : "";
        return (
          <div key={n.id || i} className="px-li" data-testid={`px-ov-next-${i}`}>
            <span className="px-rank">{i + 1}</span>
            <span style={{ minWidth: 0 }}>
              {n.href ? <PxLink href={n.href} className="px-wk-t" onOpen={() => goHref(ctx, n.href, () => undefined)}><b>{n.label}</b></PxLink> : <b>{n.label}</b>}
              <small className="px-row" style={{ gap: 4 }}><Src size={10} aria-hidden />{[kind, n.accepted ? "accepted" : src.word].filter(Boolean).join(" · ")}</small>
            </span>
            {n.accepted || !n.accept ? <Tag tone="green">Next</Tag> : (
              <GatedButton permission={acceptPermission(n)} projectId={ctx.projectId} className="px-btn" data-size="s" data-testid={`px-ov-accept-${n.id}`}
                           title={n.accept.href.startsWith("/v1/ambient/") ? "Accept it: it joins Work as planned" : "Accept it: it starts through the project's normal path"}
                           onClick={() => void accept(n!)}>Accept</GatedButton>
            )}
          </div>
        );
      }) : (
        <div className="px-empty" style={{ padding: "10px 6px" }} data-testid="px-ov-next-empty">
          <Sparkle size={22} weight="duotone" style={{ color: "var(--px-violet, #8b7cf6)" }} aria-hidden />
          <h3 style={{ margin: "6px 0 2px" }}>Helm is drafting ideas</h3>
          <p style={{ margin: "0 0 10px" }}>It ranks issues, failing checks and ideas into a backlog.</p>
          <GatedButton permission="workflow_run:start" projectId={ctx.projectId} className="px-btn" data-size="s" data-testid="px-ov-next-rank"
                       onClick={() => void startOnboarding(ctx.projectId).then((r) => { setSaid(r.ok ? (r.demo ? "Sample mode — nothing was sent." : "Helm is ranking the backlog.") : (r.error ?? "Not started.")); if (r.ok && !r.demo) ctx.refreshWork(); })}>
            Rank the backlog</GatedButton>
        </div>
      )}
      <Said text={said} />
    </section>
  );
}
