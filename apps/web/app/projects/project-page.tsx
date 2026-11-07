"use client";

/**
 * THE PROJECT PAGE (BOSSAPP-914 screens 4-13, contract §2-§12): `?project_id=<id>&area=project&tab=…`.
 *
 *   Projects / HelpDesk AI Demo / Work                                       (breadcrumb)
 *   [HD] HelpDesk AI Demo  repo ↗ · staging ↗ · ● Healthy · Working   [Helm · Ask]  [+ New work]
 *   ! 2 need you  [Merge PR #3] [Pick prod host]                             (always visible)
 *   Overview · Work · Roadmap · Repo & knowledge · Deployments · Health · Settings
 *   <tab>                                                   drawers: &drawer=<kind>:<id>
 *
 * Everything about ONE project is here, at most two clicks away; shared things (servers, the
 * cross-app view) stay in Infra and Observability, linked both ways.
 */
import React, { useCallback, useEffect, useMemo, useState } from "react";
import {
  ArrowSquareOut, ChatCircleDots, Check, ClipboardText, Compass, GearSix, GitBranch, Globe, HandWaving, Heartbeat, House, Kanban, MapTrifold,
  Plus, RocketLaunch, TreeStructure, GitMerge, CloudCheck, ListChecks, GithubLogo, type Icon,
} from "@phosphor-icons/react";

import { GatedButton } from "../../components/gated";
import { openDockAbout } from "../../lib/dock-subject";
import {
  PX_PATHS, TAB_WORDS, isMissingStatus, needsYouCards, overviewFromStart, parseOnboarding, parseOverview, parseWork, pxHref, readPxUrl, usePxRead,
  workFromStartRow, writePxUrl, type Overview, type Onboarding, type PxUrl, type Tab, type WorkList, a, o,
} from "../../lib/project-experience";
import { FX_ONBOARDING, FX_ONBOARDING_PROJECT, FX_OVERVIEW, FX_WORK } from "../../lib/project-experience-fixtures";
import { DemoBanner, Face, HEALTH_TONE, HEALTH_WORD, Loading, Failed, Empty, STAGE_ICON, closeDrawer, openDrawer, usePxUrl } from "./kit";
export { usePxUrl };
import { OverviewTab } from "./tab-overview";
import { WorkTab } from "./tab-work";
import { RoadmapTab } from "./tab-roadmap";
import { KnowledgeTab } from "./tab-knowledge";
import { DeploymentsTab } from "./tab-deployments";
import { HealthTab } from "./tab-health";
import { SettingsTab } from "./tab-settings";
import { DrawerHost } from "./drawers";
import { OnboardingView, elapsedWords } from "./onboarding";
import "./px.css";

export const TAB_ICONS: Record<Tab, Icon> = {
  overview: House, work: Kanban, roadmap: MapTrifold, knowledge: TreeStructure, deployments: RocketLaunch, health: Heartbeat, settings: GearSix,
};


export type PxCtx = {
  projectId: string; actor: string; orgId: string | null; overview: Overview; demo: boolean;
  work: WorkList | null; workState: "loading" | "ready" | "missing" | "error"; refreshWork: () => void;
  /** Read the Overview again (a card's "Couldn't load · Retry"). */
  refreshOverview?: () => void;
  /** The onboarding agent's record (§8), for its drawer and the Needs-you bar. */
  onboarding: Onboarding | null;
  /** Switch tab (push: Back returns). */
  tab: (t: Tab, extra?: Record<string, string | null>) => void;
  open: (kind: string, id: string) => void;
  ask: (question?: string) => void;
};

/** The Ask BOSS dock, scoped to this project and talking as its captain (§7): ⌘K's "Ask Helm…"
 *  off the project page. On the project page `ctx.ask` opens the captain drawer (screen 09). */
export function askCaptain(projectId: string, captain: { name: string; role_key: string } | null, question = ""): void {
  openDockAbout({ subject: { kind: "project", id: projectId }, question, role: captain?.name ?? null, roleKey: captain?.role_key || null, history: "push" });
}

export function ProjectPage({ projectId, actor, orgId }: { projectId: string; actor: string; orgId?: string | null }) {
  const url = usePxUrl();
  const demoOnboarding = projectId === FX_ONBOARDING_PROJECT;
  const ov = usePxRead<Overview>(PX_PATHS.overview(projectId), parseOverview, {
    everyMs: 15_000,
    demo: () => {
      const base = parseOverview(FX_OVERVIEW);
      return demoOnboarding ? { ...base, project: { ...base.project, id: projectId, name: "NorthPeak Retail", initials: "NP", stage: "onboarding", status_label: "Onboarding", live_url: "", health: "unknown" }, needs_you: [] } : base;
    },
  });
  // AN ENGINE WITHOUT §2: the overview is built from the project page's existing start view.
  const start = usePxRead<unknown>(ov.state === "missing" ? PX_PATHS.start(projectId) : null, (raw) => raw, { everyMs: 20_000 });
  const overview: Overview | null = ov.data ?? (ov.state === "missing" && start.data ? overviewFromStart(start.data, projectId) : null);
  const onboarding = usePxRead<Onboarding>(PX_PATHS.onboarding(projectId), parseOnboarding, {
    everyMs: overview?.project.stage === "onboarding" ? 5_000 : 60_000,
    demo: demoOnboarding ? () => parseOnboarding(FX_ONBOARDING) : () => null,
  });
  const work = usePxRead<WorkList>(PX_PATHS.work(projectId), parseWork, { everyMs: 15_000, demo: () => parseWork(FX_WORK) });
  const workFallback = useMemo<WorkList | null>(() => (work.state === "missing" && start.data
    ? { items: a(o(start.data).work).map((r) => workFromStartRow(r, projectId)) } : null), [work.state, start.data, projectId]);

  const tab = useCallback((t: Tab, extra: Record<string, string | null> = {}) => writePxUrl({ tab: t === "overview" ? null : t, drawer: null, ...extra }), []);
  const ctxBase = { projectId, actor, orgId: orgId ?? null };

  if (!overview) {
    const failed = ov.state === "error" || (ov.state === "missing" && start.state === "error");
    const gone = ov.state === "missing" && start.state === "missing";
    return (
      <div className="px" data-testid="px-project">
        {failed ? <Failed what="This project" error={ov.error ?? start.error} onRetry={() => { ov.refresh(); start.refresh(); }} />
          : gone ? <Empty title="Project not found" line="It may have been archived, or you may not have access to it." testid="px-project-gone">
              <a className="px-btn" href={pxHref("?area=projects")}>All projects</a></Empty>
          : <Loading rows={3} label="Loading the project" />}
      </div>
    );
  }
  const captain = overview.captain;
  const ctx: PxCtx = {
    ...ctxBase, overview, demo: ov.demo,
    work: work.data ?? workFallback, workState: work.data ? "ready" : workFallback ? "ready" : work.state === "missing" ? (start.state === "missing" ? "missing" : start.state) : work.state,
    refreshWork: () => { work.refresh(); start.refresh(); },
    refreshOverview: () => ov.refresh(),
    onboarding: onboarding.data,
    // ASK THE CAPTAIN opens its drawer (screen 09) on this page; a question rides as `cap_ask`
    // and is sent once when the drawer opens (the dock's own conversation, one agent path).
    tab, open: openDrawer, ask: (q) => writePxUrl({ drawer: "captain", cap_ask: q?.trim() ? q.trim() : null }),
  };
  const ob = onboarding.data;
  const onboardingNow = overview.project.stage === "onboarding" && ob !== null;
  // The tab's badge is the Work list's "All" count (wireframe: Work 9 = All 9), never a second number.
  const openCount = ctx.work ? ctx.work.items.length
    : Object.values(overview.tiles.open_work?.by_state ?? {}).reduce((x, y) => x + y, 0) || undefined;

  return (
    <div className="px" data-testid="px-project" data-tab={url.tab}>
      {ov.demo ? <DemoBanner /> : null}
      {ov.state === "missing" ? <div className="px-note" role="note" data-testid="px-project-note">BOSS didn&apos;t answer with this project&apos;s overview, so journey, health and cost are missing here; work and repo are live.</div> : null}
      <div className="px-sticky">
        {/* The top bar carries the breadcrumb (Projects / <project> / <tab>); the page draws none (04). */}
        <Header ctx={ctx} onboarding={onboardingNow} />
        <NeedsBar ctx={ctx} onboarding={onboardingNow ? ob : null} />
        <div className="px-tabs" role="tablist" aria-label="Project" data-testid="px-tabs"
             onKeyDown={(e) => {
               if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
               const order = Object.keys(TAB_WORDS) as Tab[];
               const next = order[(order.indexOf(url.tab) + (e.key === "ArrowRight" ? 1 : order.length - 1)) % order.length];
               tab(next, {});
               requestAnimationFrame(() => document.querySelector<HTMLElement>(`[data-testid="px-tab-${next}"]`)?.focus());
             }}>
          {(Object.keys(TAB_WORDS) as Tab[]).map((t) => {
            const I = TAB_ICONS[t];
            const count = t === "work" ? openCount : t === "roadmap" ? (ctx.overview.roadmap_count ?? undefined) : undefined;
            return (
              <button key={t} type="button" role="tab" className="px-tab" aria-selected={url.tab === t} tabIndex={url.tab === t ? 0 : -1}
                      ref={url.tab === t ? (el) => { el?.scrollIntoView?.({ block: "nearest", inline: "nearest" }); } : undefined}
                      aria-controls="px-tabpanel" data-testid={`px-tab-${t}`} onClick={() => tab(t)}>
                <I size={14} weight={url.tab === t ? "fill" : "regular"} aria-hidden />{TAB_WORDS[t]}
                {count ? <span className="px-nbadge">{count}</span> : null}
              </button>
            );
          })}
        </div>
      </div>
      <div id="px-tabpanel" role="tabpanel" aria-label={TAB_WORDS[url.tab]} data-testid={`px-panel-${url.tab}`}>
        {url.tab === "overview" ? (onboardingNow && ob ? <OnboardingView projectId={projectId} ob={ob} /> : <OverviewTab ctx={ctx} />)
          : url.tab === "work" ? <WorkTab ctx={ctx} />
          : url.tab === "roadmap" ? <RoadmapTab ctx={ctx} />
          : url.tab === "knowledge" ? <KnowledgeTab ctx={ctx} />
          : url.tab === "deployments" ? <DeploymentsTab ctx={ctx} />
          : url.tab === "health" ? <HealthTab ctx={ctx} />
          : <SettingsTab ctx={ctx} />}
      </div>
      {url.drawer ? <DrawerHost ctx={ctx} drawer={url.drawer} onClose={closeDrawer} /> : null}
    </div>
  );
}

function Header({ ctx, onboarding }: { ctx: PxCtx; onboarding: boolean }) {
  const p = ctx.overview.project;
  const repo = p.repos[0];
  const staging = ctx.overview.tiles.staging;
  const health = staging?.health ?? p.health;
  const captain = ctx.overview.captain;
  return (
    <div className="px-head" data-testid="px-head">
      <span className="px-mark" aria-hidden title={p.name}>{p.initials}</span>
      <div className="px-title">
        <h1>{p.name}</h1>
        <div className="px-row">
          {repo ? (
            <a className="px-hchip" href={pxHref(`?project_id=${encodeURIComponent(ctx.projectId)}&area=project&tab=knowledge`)} data-testid="px-head-repo"
               title="Repository — opens Repo & knowledge"
               onClick={(e) => { if (e.metaKey || e.ctrlKey || e.shiftKey) return; e.preventDefault(); ctx.tab("knowledge"); }}>
              <GithubLogo size={11} aria-hidden />{repo.full_name}</a>
          ) : (
            <a className="px-hchip" href="#" data-testid="px-head-norepo" onClick={(e) => { e.preventDefault(); ctx.tab("settings", { section: "repos" }); }}>
              <Plus size={11} aria-hidden /> Add a repo</a>
          )}
          {p.live_url ? (
            <a className="px-hchip" href={p.live_url} target="_blank" rel="noreferrer noopener" data-testid="px-head-live"
               title={`${staging?.label ?? "Staging"} — opens the live app`}>
              <Globe size={11} aria-hidden />{(staging?.label ?? "staging").toLowerCase()} · {p.live_url.replace(/^https?:\/\//, "")}<ArrowSquareOut size={10} aria-label="opens in a new tab" /></a>
          ) : null}
          {p.live_url || health !== "unknown" ? (
            <button type="button" className="px-hchip" style={{ border: 0, cursor: "pointer", color: health === "healthy" ? "var(--green)" : health === "unknown" ? "var(--faint)" : `var(--${HEALTH_TONE[health]})` }}
                    data-testid="px-head-health" onClick={() => ctx.tab("health")}
                    title={[HEALTH_WORD[health], staging?.uptime_pct != null ? `${staging.uptime_pct}%` : "", staging?.p95_ms != null ? `p95 ${staging.p95_ms}ms` : ""].filter(Boolean).join(" · ") + " — opens Health"}>
              <span className="px-dot" data-s={health} data-round aria-hidden />{HEALTH_WORD[health]}</button>
          ) : null}
          <span className="px-tag" data-tone={onboarding ? "blue" : "green"} data-testid="px-head-status" title="Project stage on the journey">{onboarding ? <><Compass size={11} aria-hidden /> Onboarding</>
            : <>{(() => { const I = STAGE_ICON[p.stage] ?? Compass; return <I size={11} aria-hidden />; })()} {p.status_label || "Ready"}</>}</span>
          {onboarding && ctx.onboarding?.elapsed_s ? <span className="px-mono px-faint" style={{ fontSize: ".6rem" }} data-testid="px-head-elapsed">{elapsedWords(ctx.onboarding.elapsed_s)}</span> : null}
        </div>
      </div>
      <span className="px-spacer" />
      {captain ? (
        <span className="px-captain" data-testid="px-head-captain" role="button" tabIndex={-1} style={{ cursor: "pointer" }}
              title={`${captain.name} captains this project. Click to talk to ${captain.name}.`}
              onClick={(e) => { if ((e.target as HTMLElement).closest("button")) return; ctx.ask(); }}>
          <Face agent={captain} size={30} state={onboarding ? "working" : "idle"} />
          <span><b>{captain.name}</b><small>{onboarding ? "Onboarding" : "Captain"}</small></span>
          <button type="button" className="px-btn" data-size="s" data-testid="px-head-ask" onClick={() => ctx.ask()} title={`Ask ${captain.name} about this project`}>
            <ChatCircleDots size={13} aria-hidden /> Ask</button>
        </span>
      ) : (
        <button type="button" className="px-btn" data-testid="px-head-ask" onClick={() => ctx.ask()}><ChatCircleDots size={13} aria-hidden /> Ask BOSS</button>
      )}
      {!onboarding ? (
        <GatedButton permission="workflow_run:start" projectId={ctx.projectId} className="px-btn" data-kind="primary" data-size="l" data-testid="px-head-new-work"
                     title="Describe a feature, a bug or paste a ticket" onClick={() => ctx.tab("work", { compose: "1" })}>
          <Plus size={14} weight="bold" aria-hidden /> New work
        </GatedButton>
      ) : null}
    </div>
  );
}

/** NEEDS YOU, on every tab: what waits on a person, each one press from its action. */
function NeedsBar({ ctx, onboarding }: { ctx: PxCtx; onboarding: Onboarding | null }) {
  // The overview already carries the onboarding agent's "only you" items (§14: `onb:<key>`);
  // the live onboarding record replaces them while the project onboards, never doubles them.
  // ONE SOURCE (lib needsYouCards): the Work tab's Needs-you count is this same list's length.
  const all = needsYouCards(ctx.overview.needs_you, onboarding, ctx.work?.items ?? []);
  const yours = onboarding?.only_you ?? [];
  // The live onboarding record's items keep their own links below; every other card is a button.
  const items = yours.length ? all.filter((it) => !it.id.startsWith("onb:")) : all;
  const n = all.length;
  if (!n) {
    return (
      <div className="px-needs-bar" data-calm data-testid="px-needs-bar" role="status">
        <Check size={14} weight="bold" style={{ color: "var(--green)" }} aria-hidden /> <span>Nothing needs you</span>
      </div>
    );
  }
  return (
    <div className="px-needs-bar" data-testid="px-needs-bar" role="region" aria-label={`${n} need you`}>
      <span className="px-ny-ico" aria-hidden>!</span>
      <b>{n} need{n === 1 ? "s" : ""} you</b>
      {items.map((it) => (
        <button key={it.id} type="button" className="px-needs-item" data-testid={`px-needs-${it.id}`} onClick={() => ctx.open("needs", it.id)}>
          {it.kind === "merge" ? <GitMerge size={12} aria-hidden /> : it.kind === "deploy" ? <RocketLaunch size={12} aria-hidden />
            : it.icon === "cloud" ? <CloudCheck size={12} aria-hidden /> : it.kind === "plan" ? <ListChecks size={12} aria-hidden /> : <HandWaving size={12} aria-hidden />}{it.label}
        </button>
      ))}
      {yours.map((it) => (
        <a key={it.key} className="px-needs-item" href={it.action ? pxHref(it.action.href) : "#"} data-testid={`px-needs-ob-${it.key}`}
           onClick={it.action ? undefined : (e) => { e.preventDefault(); ctx.tab("overview"); }}>
          <ClipboardText size={12} aria-hidden />{it.label}</a>
      ))}
      <span className="px-spacer" />
      {/* About a minute of the person's time per item (X-39 "≈ 2 min"); hidden on a phone. */}
      <span className="px-faint px-hide-phone" title="About how long these take you" data-testid="px-needs-time">≈ {n} min</span>
    </div>
  );
}

export { isMissingStatus };
