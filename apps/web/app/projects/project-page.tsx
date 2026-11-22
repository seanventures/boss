"use client";

/**
 * THE PROJECT PAGE (BOSSAPP-914 screens 4-13, contract §2-§12): `?project_id=<id>&area=project&tab=…`.
 *
 *   Projects / HelpDesk AI Demo / Work                                       (breadcrumb)
 *   [HD] HelpDesk AI Demo  repo ↗ · staging ↗ · ● Healthy · Working   [Helm · Ask]  [+ New work]
 *   Overview · Work 9 (2) · Roadmap · Repo & knowledge · Deployments · Health · Settings
 *     (the amber (2) is what needs you — ONE count; the list lives in Work's Needs-you group)
 *   <tab>                                                   drawers: &drawer=<kind>:<id>
 *
 * Everything about ONE project is here, at most two clicks away; shared things (servers, the
 * cross-app view) stay in Infra and Observability, linked both ways.
 */
import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ArrowSquareOut, ChatCircleDots, CircleNotch, WarningCircle, Compass, GearSix, GitBranch, Globe, Heartbeat, House, Kanban, MapTrifold,
  Plus, RocketLaunch, TreeStructure, GitCommit, GithubLogo, HardDrives, type Icon,
} from "@phosphor-icons/react";

import { GatedButton, useGate } from "../../components/gated";
import { useAreaKeydown } from "../../lib/area-keys";
import { isLocalRepo } from "../../lib/repo-links";
import { tidyIds } from "../../lib/work-links";
import { notifyChanged } from "../changed";
import { LocalPublishChip } from "./publish-sheet";
import { openDockAbout } from "../../lib/dock-subject";
import {
  PX_PATHS, TAB_WORDS, isMissingStatus, navigateTo, needsYouCards, overviewFromStart, parseOnboarding, parseOverview, parseWork, projectHref, pxHref, readPxUrl, usePxRead,
  workFromStartRow, writePxUrl, type Overview, type Onboarding, type PxUrl, type Tab, type WorkList, a, o,
} from "../../lib/project-experience";
import { FX_ONBOARDING, FX_ONBOARDING_PROJECT, FX_OVERVIEW, FX_WORK } from "../../lib/project-experience-fixtures";
import { DemoBanner, Face, HEALTH_TONE, HEALTH_WORD, Loading, Failed, Empty, STAGE_ICON, closeDrawer, openDrawer, usePxUrl } from "./kit";
export { usePxUrl };
import { OverviewTab } from "./tab-overview";
import { WorkTab } from "./tab-work";
import { RoadmapTab } from "./tab-roadmap";
import { KnowledgeTab } from "./tab-knowledge";
import { DeploymentsTab, p95Words } from "./tab-deployments";
import { HealthTab } from "./tab-health";
import { SettingsTab } from "./tab-settings";
import { DrawerHost } from "./drawers";
import { OnboardingView, clockWords, useElapsed } from "./onboarding";
import { NewWorkModal, type Prefill } from "./new-work";
import { ProjectMoreMenu } from "../project-delete";
import "./px.css";

export const TAB_ICONS: Record<Tab, Icon> = {
  overview: House, work: Kanban, roadmap: MapTrifold, knowledge: TreeStructure, deployments: RocketLaunch, health: Heartbeat, settings: GearSix,
};


export type PxCtx = {
  projectId: string; actor: string; orgId: string | null; overview: Overview; demo: boolean;
  /** The org's home project (it cannot be archived or deleted), when the page was given it. */
  homeId?: string;
  work: WorkList | null; workState: "loading" | "ready" | "missing" | "error"; refreshWork: () => void;
  /** Read the Overview again (a card's "Couldn't load · Retry"). */
  refreshOverview?: () => void;
  /** The onboarding agent's record (§8), for its drawer and the Needs-you bar. */
  onboarding: Onboarding | null;
  /** Switch tab (push: Back returns). */
  tab: (t: Tab, extra?: Record<string, string | null>) => void;
  open: (kind: string, id: string, extra?: Record<string, string | null>) => void;
  ask: (question?: string) => void;
  /** NEW WORK, the modal, on any tab (header, the Work tab's bar, N, an answer's Edit). */
  newWork?: (prefill?: Prefill) => void;
  /** The row a Start just made: it slides in on the Work tab. */
  freshWork?: string;
  /** What the last New work did ("Started — Juno is planning it."), said on the Work tab. */
  workSaid?: { text: string; tone?: "red" | "green" };
};

/** ASK THE CAPTAIN, from any door (09-01: header Ask, Overview Ask Helm, ⌘K "Ask Helm…"): the
 *  captain drawer (screen 09) on this project's page, the question riding as `cap_ask` (sent
 *  once when the drawer opens). Already on the page: the drawer opens in place; elsewhere the
 *  page opens with it. The drawer IS the Ask BOSS dock's conversation scoped to the project
 *  (09-02), so there is one agent path. Only with no project at all does the dock open. */
export function askCaptainHref(projectId: string, question = ""): string {
  const q = question.trim();
  return projectHref(projectId, q ? { drawer: "captain", cap_ask: q } : { drawer: "captain" });
}

export function askCaptain(projectId: string, captain: { name: string; role_key: string } | null, question = ""): void {
  if (!projectId) {
    openDockAbout({ subject: { kind: "project", id: projectId }, question, role: captain?.name ?? null, roleKey: captain?.role_key || null, history: "push" });
    return;
  }
  let here = false;
  try {
    const now = new URLSearchParams(window.location.search);
    here = now.get("area") === "project" && now.get("project_id") === projectId;
  } catch { /* non-browser */ }
  if (here) writePxUrl({ drawer: "captain", cap_ask: question.trim() ? question.trim() : null });
  else navigateTo(askCaptainHref(projectId, question));
}

export function useStickyHead(): (el: HTMLDivElement | null) => void {
  const observer = useRef<ResizeObserver | null>(null);
  return useCallback((el: HTMLDivElement | null) => {
    observer.current?.disconnect();
    observer.current = null;
    if (!el) return;
    const measure = () => {
      const bar = document.querySelector<HTMLElement>(".bx-topbar");
      const top = bar ? Math.round(bar.getBoundingClientRect().height) : 0;
      el.style.setProperty("--px-top", `${top}px`);
      const tabs = el.querySelector<HTMLElement>(".px-tabs");
      // Everything above the tab row (screen 10 has no tabs: then nothing stays on a phone but the block's foot).
      el.style.setProperty("--px-stick-off", `${Math.max(0, Math.round(el.offsetHeight - (tabs?.offsetHeight ?? 0)))}px`);
      // EVERYTHING THAT STAYS ON TOP, for the page: a section a link scrolls to (Settings › Code
      // from "Add a repo") lands BELOW the top bar and this sticky block, never under it (stranger
      // test 2026-11-17: "Add repository" landed with the Code card hidden under the tabs).
      document.documentElement.style.setProperty("--px-head-h", `${top + Math.round(el.offsetHeight)}px`);
    };
    measure();
    if (typeof ResizeObserver !== "undefined") {
      observer.current = new ResizeObserver(measure);
      observer.current.observe(el);
      const bar = document.querySelector<HTMLElement>(".bx-topbar");
      if (bar) observer.current.observe(bar);
    }
  }, []);
}

export function ProjectPage({ projectId, actor, orgId, homeId }: { projectId: string; actor: string; orgId?: string | null; homeId?: string }) {
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
  // LIVE while the agent works: every 3 s on its view or while it runs; a minute otherwise.
  const [obLive, setObLive] = useState(false);
  const onboarding = usePxRead<Onboarding>(PX_PATHS.onboarding(projectId), parseOnboarding, {
    everyMs: obLive || overview?.project.stage === "onboarding" ? 3_000 : url.view === "onboarding" ? 10_000 : 60_000,
    demo: demoOnboarding ? () => parseOnboarding(FX_ONBOARDING) : () => null,
  });
  useEffect(() => { setObLive(onboarding.data?.state === "running"); }, [onboarding.data?.state]);
  const work = usePxRead<WorkList>(PX_PATHS.work(projectId), parseWork, { everyMs: 15_000, demo: () => parseWork(FX_WORK) });
  const workFallback = useMemo<WorkList | null>(() => (work.state === "missing" && start.data
    ? { items: a(o(start.data).work).map((r) => workFromStartRow(r, projectId)) } : null), [work.state, start.data, projectId]);

  const tab = useCallback((t: Tab, extra: Record<string, string | null> = {}) => writePxUrl({ tab: t === "overview" ? null : t, drawer: null, ...extra }), []);
  const ctxBase = { projectId, actor, orgId: orgId ?? null, homeId };
  const stickyRef = useStickyHead();
  // NEW WORK: one modal for the whole page. `compose=1` (the header's old door, ⌘K, an Ask-Helm
  // answer's "New work · Edit" with `compose_text`/`compose_kind`) opens it prefilled — never sent —
  // and the flag is dropped so a reload does not reopen it.
  const [nw, setNw] = useState<{ open: boolean; prefill: Prefill }>({ open: false, prefill: null });
  const [fresh, setFresh] = useState("");
  const [workSaid, setWorkSaid] = useState<{ text: string; tone?: "red" | "green" }>({ text: "" });
  const startGate = useGate("workflow_run:start", projectId);
  const canStart = useRef(startGate.allowed);
  canStart.current = startGate.allowed;
  const newWork = useCallback((prefill?: Prefill) => { if (canStart.current) setNw({ open: true, prefill: prefill ?? null }); }, []);
  useEffect(() => {
    const p = new URLSearchParams(window.location.search);
    // Only once the person may start work (the gate reads the identity after mount).
    if (p.get("compose") !== "1" || !startGate.allowed) return;
    const k = p.get("compose_kind");
    const words = p.get("compose_text") ?? "";
    // AN APP'S DOOR (AR-20): `app` names the app New work builds onto — its branch, its change.
    const app = p.get("app")?.trim() ?? "";
    // AN ANSWER CARD'S EDIT WITH LONG WORDS (AR-21): they are not in the link (a URL that long
    // is refused); the card kept them as New work's draft, which the modal brings back.
    const cardChars = p.get("compose_from") === "draft" ? Number(p.get("compose_chars")) || 0 : 0;
    newWork(cardChars ? { fromCard: cardChars, ...(app ? { app } : {}) }
      : words || k || app ? { text: words || undefined, kind: k === "feature" || k === "bug" || k === "ticket" ? k : undefined, ...(app ? { app } : {}) } : null);
    writePxUrl({ compose: null, compose_text: null, compose_kind: null, compose_from: null, compose_chars: null, app: null }, "replace");
  }, [url, newWork, startGate.allowed]);
  // N opens it, anywhere on the page but in a box or behind another dialog.
  // Only while this project is the area on screen, so a hidden project does not steal N.
  useAreaKeydown((e) => {
    if (e.key !== "n" && e.key !== "N") return;
    if (e.metaKey || e.ctrlKey || e.altKey || e.defaultPrevented) return;
    const el = e.target as HTMLElement | null;
    if (el && (el.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName))) return;
    if (document.querySelector("[role='dialog'][aria-modal='true']")) return;
    e.preventDefault();
    newWork();
  });

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
    newWork, freshWork: fresh, workSaid,
  };
  const ob = onboarding.data;
  // PX-15: "Onboarding" (with the working face and the timer) only once a run has started —
  // a project whose onboarding never started says so on its page, not "Onboarding" up here.
  const onboardingStage = overview.project.stage === "onboarding" && ob !== null;
  const onboardingNow = onboardingStage && ob.state !== "not_started";
  // SCREEN 10 is its own page (`tab=onboarding`), and the Overview while the project onboards
  // (or has not started: its Start onboarding button).
  const obView = url.view === "onboarding" || (url.tab === "overview" && onboardingStage);
  // The tab's badge is the Work list's "All" count (wireframe: Work 9 = All 9), never a second number.
  // What waits on a person: ONE count (lib needsYouCards, the Work tab's own Needs-you list).
  const needsCount = needsYouCards(overview.needs_you, onboardingNow ? ob : null, ctx.work?.items ?? []).length;
  const openCount = ctx.work ? ctx.work.items.length
    : Object.values(overview.tiles.open_work?.by_state ?? {}).reduce((x, y) => x + y, 0) || undefined;

  return (
    <div className="px" data-testid="px-project" data-tab={url.tab}>
      {ov.demo ? <DemoBanner /> : null}
      {ov.state === "missing" ? <div className="px-note" role="note" data-testid="px-project-note">BOSS didn&apos;t answer with this project&apos;s overview, so journey, health and cost are missing here; work and repo are live.</div> : null}
      <div className="px-sticky" data-sticky="" data-testid="px-sticky" ref={stickyRef}>
        {/* The top bar carries the breadcrumb (Projects / <project> / <tab>); the page draws none (04). */}
        <Header ctx={ctx} onboarding={onboardingNow || (obView && ob?.state === "running")} />
        {/* NEEDS YOU ONCE PER SCREEN (operator 2026-11-21: "Needs you in lots of places … all
            showing yellow"): no bar above the tabs — the Work tab carries the one amber count
            and its Needs-you group holds the list. Screen 10 draws no tabs. */}
        {!obView ? <div className="px-tabs" role="tablist" aria-label="Project" data-testid="px-tabs"
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
            const needs = t === "work" ? needsCount : 0;
            return (
              <button key={t} type="button" role="tab" className="px-tab" aria-selected={url.tab === t} tabIndex={url.tab === t ? 0 : -1}
                      ref={url.tab === t ? (el) => { el?.scrollIntoView?.({ block: "nearest", inline: "nearest" }); } : undefined}
                      aria-controls="px-tabpanel" data-testid={`px-tab-${t}`} onClick={() => tab(t)}>
                <I size={14} weight={url.tab === t ? "fill" : "regular"} aria-hidden />{TAB_WORDS[t]}
                {count ? <span className="px-nbadge">{count}</span> : null}
                {needs ? <span className="px-nbadge" data-tone="amber" data-testid="px-tab-work-needs" title={`${needs} need${needs === 1 ? "s" : ""} you — the Needs-you group in Work`}
                               aria-label={`${needs} need${needs === 1 ? "s" : ""} you`}>{needs}</span> : null}
              </button>
            );
          })}
        </div> : null}
      </div>
      <div id="px-tabpanel" role="tabpanel" aria-label={obView ? "Onboarding" : TAB_WORDS[url.tab]} data-testid={`px-panel-${obView ? "onboarding" : url.tab}`}>
        {obView ? (ob ? <OnboardingView projectId={projectId} ob={ob} onRefresh={() => { setObLive(true); onboarding.refresh(); }} />
                     : onboarding.state === "error" ? <Failed what="Onboarding" error={onboarding.error} onRetry={onboarding.refresh} />
                     : <Loading rows={3} label="Loading onboarding" />)
          : url.tab === "overview" ? <OverviewTab ctx={ctx} />
          : url.tab === "work" ? <WorkTab ctx={ctx} />
          : url.tab === "roadmap" ? <RoadmapTab ctx={ctx} />
          : url.tab === "knowledge" ? <KnowledgeTab ctx={ctx} />
          : url.tab === "deployments" ? <DeploymentsTab ctx={ctx} />
          : url.tab === "health" ? <HealthTab ctx={ctx} />
          : <SettingsTab ctx={ctx} />}
      </div>
      {url.drawer ? <DrawerHost ctx={ctx} drawer={url.drawer} onClose={closeDrawer} /> : null}
      {nw.open ? (
        <NewWorkModal ctx={ctx} prefill={nw.prefill}
                      onClose={() => setNw({ open: false, prefill: null })}
                      onDone={(r) => {
                        setNw({ open: false, prefill: null });
                        setWorkSaid({ text: r.said, tone: "green" });
                        try { window.dispatchEvent(new CustomEvent("boss:toast", { detail: { type: "success", message: r.said } })); } catch { /* non-browser */ }
                        if (r.runId) setFresh(r.runId);
                        if (r.tab && url.tab !== r.tab) tab(r.tab);
                        work.refresh(); start.refresh();
                      }} />
      ) : null}
    </div>
  );
}

/** Health word, then " · " and the p95 words, only when that p95 string is non-empty. */
export function healthChipText(healthWord: string, p95: string): string {
  return p95 ? `${healthWord} · ${p95}` : healthWord;
}

/** The project's health reason. A trimmed reason, or nothing when blank. */
export function healthReasonSentence(reason: string | null | undefined): string | null {
  const trimmed = reason?.trim() ?? "";
  return trimmed ? trimmed : null;
}

/** WHY, ONE PRESS AWAY (operator 2026-11-21: the deploy reason filled the header as a huge
 *  paragraph of engine prose with raw build ids). Beside a status chip: a small "Why?" toggle;
 *  the reason itself opens below the chips as a small block (HeaderWhyBlock), never at display
 *  size and never inline in the status line. */
export function HealthWhy({ sentence, open = false, onToggle }: { sentence: string | null; open?: boolean; onToggle?: () => void }) {
  return <WhyToggle sentence={sentence} open={open} onToggle={onToggle} testid="px-head-health-why" />;
}

/** The deploy chip's reason. A trimmed reason, or nothing when blank. */
export function deployChipReasonSentence(reason: string | null | undefined): string | null {
  const trimmed = reason?.trim() ?? "";
  return trimmed ? trimmed : null;
}

/** The deploy chip's Why? toggle (the reason opens in HeaderWhyBlock). */
export function DeployChipWhy({ sentence, open = false, onToggle }: { sentence: string | null; open?: boolean; onToggle?: () => void }) {
  return <WhyToggle sentence={sentence} open={open} onToggle={onToggle} testid="px-head-deploy-why" />;
}

function WhyToggle({ sentence, open, onToggle, testid }: { sentence: string | null; open: boolean; onToggle?: () => void; testid: string }) {
  if (!sentence) return null;
  return (
    <button type="button" className="px-why-t" aria-expanded={open} aria-controls="px-head-why-block" data-testid={testid}
            onClick={(e) => { e.stopPropagation(); onToggle?.(); }}>Why?</button>
  );
}

/** The opened reason: small, muted, ids shortened ("build ad50ce9f", never `crun_…` in full). */
export function HeaderWhyBlock({ sentence, next }: { sentence: string | null; next?: React.ReactNode }) {
  if (!sentence && !next) return null;
  return (
    <div className="px-why-block" id="px-head-why-block" role="note" data-testid="px-head-why-block">
      {sentence ? <span data-user-text>{tidyIds(sentence)}</span> : null}
      {next ? <span className="px-why-next">{sentence ? " " : null}Next: {next}</span> : null}
    </div>
  );
}

/** The header's failure chip: the engine's word, except "Needs you" — that is the Work tab's
 *  one count, never a second chip up here (operator 2026-11-21). */
export function failedWord(word: string | null | undefined): string {
  const w = String(word ?? "").trim();
  if (!w || /^needs you$/i.test(w)) return "Build failed";
  return /^needs your ok$/i.test(w) ? "Waiting for your OK" : w;
}

/** A failure's short next step (its fix's own label), at most six words, ids shortened. */
export function statusHint(label: string | null | undefined): string {
  const words = tidyIds(String(label ?? "").trim()).split(/\s+/).filter(Boolean);
  if (!words.length) return "";
  const cut = words.slice(0, 6).join(" ");
  return words.length > 6 ? `${cut}…` : cut;
}

export function Header({ ctx, onboarding }: { ctx: PxCtx; onboarding: boolean }) {
  const p = ctx.overview.project;
  const repo = p.repos[0];
  const staging = ctx.overview.tiles.staging;
  // 10-08 one clock: the header's elapsed is the live card's (same source, same words).
  const elapsed = useElapsed(ctx.onboarding ?? null);
  // THE PROJECT'S HEALTH (health.state_of, worst environment): the same word as the tiles, the
  // cards and the Health tab — never "Healthy" beside a breached SLO or a firing alert (12-05).
  const health = p.health !== "unknown" ? p.health : staging?.health ?? p.health;
  const healthWhy = p.health_reason || staging?.health_reason || "";
  const captain = ctx.overview.captain;
  const url = usePxUrl();
  // Which status's reason is open below the chips (one at a time).
  const [why, setWhy] = useState<"deploy" | "health" | "failed" | null>(null);
  const flip = (k: "deploy" | "health" | "failed") => () => setWhy((cur) => (cur === k ? null : k));
  const deployWhy = p.deploy && ["deploying", "waiting", "fixing", "failed"].includes(p.deploy.state) ? deployChipReasonSentence(p.deploy.reason) : null;
  const failedWhy = !onboarding && p.build_failed ? healthReasonSentence(p.build_failed.reason) : null;
  const hint = !onboarding && p.build_failed ? statusHint(p.build_failed.fix?.label) : "";
  const openWhy = why === "deploy" ? deployWhy : why === "health" ? healthReasonSentence(healthWhy) : why === "failed" ? failedWhy : null;
  // The failure's next step (its fix's own label) is said inside its Why, never as more words on
  // the status row (operator 2026-11-21: "Build failed · Open the action item" crowded it).
  const hintEl = hint ? (p.build_failed?.fix?.href
    ? <a className="px-head-hint" href={pxHref(p.build_failed.fix.href)} data-testid="px-head-hint">{hint}</a>
    : <span className="px-head-hint" data-testid="px-head-hint">{hint}</span>) : null;
  return (
    <div className="px-head" data-testid="px-head">
      <span className="px-mark" aria-hidden title={p.name}>{p.initials}</span>
      <div className="px-title">
        <h1>{p.name}</h1>
        <div className="px-row" data-testid="px-head-statusline">
          {repo ? (
            <a className="px-hchip" href={pxHref(`?project_id=${encodeURIComponent(ctx.projectId)}&area=project&tab=knowledge`)} data-testid="px-head-repo"
               title="Repository — opens Repo & knowledge"
               onClick={(e) => { if (e.metaKey || e.ctrlKey || e.shiftKey) return; e.preventDefault(); ctx.tab("knowledge"); }}>
              {isLocalRepo(repo.full_name) ? <HardDrives size={11} aria-label="Local repo" /> : <GithubLogo size={11} aria-hidden />}{repo.full_name}</a>
          ) : (
            <a className="px-hchip" href="#" data-testid="px-head-norepo" onClick={(e) => { e.preventDefault(); ctx.tab("settings", { section: "repos" }); }}>
              <Plus size={11} aria-hidden /> Add a repo</a>
          )}
          {/* GIT HISTORY, ONE PRESS AWAY (BOSSAPP-993): the default branch's log lives on
              Repo & knowledge; this chip names it so nobody reaches for the CLI to see it. */}
          {repo ? (
            <a className="px-hchip" href={pxHref(`?project_id=${encodeURIComponent(ctx.projectId)}&area=project&tab=knowledge`)} data-testid="px-head-history"
               title="Commit history — opens Repo & knowledge"
               onClick={(e) => { if (e.metaKey || e.ctrlKey || e.shiftKey) return; e.preventDefault(); ctx.tab("knowledge"); }}>
              <GitCommit size={11} aria-hidden />History</a>
          ) : null}
          {/* LOCAL FIRST (BOSSAPP-921): a project whose code is on this machine says so, quietly,
              with Publish to GitHub one press away. */}
          {!ctx.demo && repo && isLocalRepo(repo.full_name) ? <LocalPublishChip projectId={ctx.projectId} onPublished={() => notifyChanged("project.published")} /> : null}
          {p.live_url ? (
            <a className="px-hchip" href={p.live_url} target="_blank" rel="noreferrer noopener" data-testid="px-head-live"
               title={`${staging?.label ?? "Staging"} — opens the live app`}>
              <Globe size={11} aria-hidden />{(staging?.label ?? "staging").toLowerCase()} · {p.live_url.replace(/^https?:\/\//, "")}<ArrowSquareOut size={10} aria-label="opens in a new tab" /></a>
          ) : null}
          {/* THE DEPLOY CHIP (BOSSAPP-932): a deploy moving now ("Deploying · 2/7"), one waiting
              for its approval, or the last one that failed — a link to the Deployments card
              that shows its steps and log live. */}
          {p.deploy && ["deploying", "waiting", "fixing", "failed"].includes(p.deploy.state) ? (
            <span className="px-hgroup">
              <a className="px-hchip px-hchip-deploy" data-s={p.deploy.state} data-testid="px-head-deploy"
                 href={pxHref(`?project_id=${encodeURIComponent(ctx.projectId)}&area=project&tab=deployments`)}
                 onClick={(e) => { if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return; e.preventDefault(); ctx.tab("deployments"); }}
                 title={[`${p.deploy.app || "The app"} · ${p.deploy.environment || "staging"}`, p.deploy.reason].filter(Boolean).join(": ") + " — opens Deployments"}>
                {p.deploy.state === "failed" ? <WarningCircle size={11} aria-hidden /> : <CircleNotch size={11} className="px-spin" aria-hidden />}
                {p.deploy.state === "deploying" ? `Deploying${p.deploy.total ? ` · ${p.deploy.index}/${p.deploy.total}` : ""}`
                  // BOSS acting on a failed deploy, or the fix waiting on approval: the engine's words.
                  : p.deploy.state === "fixing" ? (p.deploy.word || "BOSS is fixing the deploy")
                  : p.deploy.state === "waiting" ? (p.deploy.word.startsWith("Fixed") ? p.deploy.word : "Deploy waiting for approval") : "Last deploy failed"}</a>
              <DeployChipWhy sentence={deployWhy} open={why === "deploy"} onToggle={flip("deploy")} />
            </span>
          ) : null}
          {/* THE HEALTH CHIP, ON EVERY TAB (12-24): a link to this project's Health tab — the
              environments' word, or "No health yet" before anything is deployed (Health's own
              empty state says what comes next). */}
          <span className="px-hgroup">
          <a className="px-hchip px-hchip-health" data-s={health}
             href={pxHref(`?project_id=${encodeURIComponent(ctx.projectId)}&area=project&tab=health`)}
             data-testid="px-head-health" aria-current={url.tab === "health" ? "page" : undefined}
             onClick={(e) => { if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return; e.preventDefault(); ctx.tab("health"); }}
             title={[p.live_url || health !== "unknown" ? HEALTH_WORD[health] : "Nothing deployed yet", healthWhy,
                     staging?.uptime_pct != null ? `${staging.label || "Staging"} ${staging.uptime_pct}% up` : "",
                     p95Words(staging?.p95_ms, staging?.window)].filter(Boolean).join(" · ") + " — opens Health"}>
            <span className="px-dot" data-s={health} data-round aria-hidden />{healthChipText(p.live_url || health !== "unknown" ? HEALTH_WORD[health] : "No health yet", p95Words(staging?.p95_ms, staging?.window))}</a>
          <HealthWhy sentence={healthReasonSentence(healthWhy)} open={why === "health"} onToggle={flip("health")} />
          </span>
          {/* A FAILED LATEST BUILD (engine build_failure): red "Build failed" / "Deploy failed", its
              reason on hover, a link to the build's row — never "Working" over a build that
              failed. ON A LIVE PROJECT it sits BESIDE the stage chip ("On staging" stays). */}
          {!onboarding && p.build_failed ? (
            <span className="px-hgroup">
            <a className="px-tag" data-tone="red" data-testid={p.build_failed.beside ? "px-head-failed" : "px-head-status"} data-failed
               href={pxHref(p.build_failed.work_href || `?project_id=${encodeURIComponent(ctx.projectId)}&area=project&tab=work`)}
               title={`${p.build_failed.label}: ${p.build_failed.reason} — opens the build`}
               onClick={(e) => { if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return; e.preventDefault(); ctx.open("work", p.build_failed!.id); }}>
              <WarningCircle size={11} aria-hidden /> {failedWord(p.build_failed.word)}</a>
            <WhyToggle sentence={failedWhy || (hint ? hint : null)} open={why === "failed"} onToggle={flip("failed")} testid="px-head-failed-why" />
            </span>
          ) : null}
          {!onboarding && p.build_failed && !p.build_failed.beside ? null : (
          <span className="px-tag" data-tone={onboarding ? "blue" : "green"} data-testid="px-head-status" title="Project stage on the journey">{onboarding ? <><Compass size={11} aria-hidden /> Onboarding</>
            : <>{(() => { const I = STAGE_ICON[p.stage] ?? Compass; return <I size={11} aria-hidden />; })()} {p.status_label || "Ready"}</>}</span>
          )}
          {onboarding && elapsed ? <span className="px-mono px-faint" style={{ fontSize: "var(--fs-meta)" }} data-testid="px-head-elapsed">{clockWords(elapsed)}</span> : null}
        </div>
        <HeaderWhyBlock sentence={openWhy} next={why === "failed" ? hintEl : null} />
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
                     title="Describe a feature, a bug or paste a ticket (N)" onClick={() => ctx.newWork?.()}>
          <Plus size={14} weight="bold" aria-hidden /> New work
        </GatedButton>
      ) : null}
      {/* THE PROJECT'S ⋯ MENU: Settings, Archive project…, Delete project… (project-delete.tsx). */}
      {!ctx.demo ? (
        <ProjectMoreMenu projectId={ctx.projectId} name={p.name} home={!!ctx.homeId && ctx.homeId === ctx.projectId}
                         onSettings={() => ctx.tab("settings")} />
      ) : null}
    </div>
  );
}

export { isMissingStatus };
