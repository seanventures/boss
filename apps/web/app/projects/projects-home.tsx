"use client";

/**
 * PROJECTS (BOSSAPP-914 screens 0a, 0b, 2): `?area=projects`.
 *
 *   first run (no project with a repo, or `&first_run=1`)  → FirstRun (0a empty → 0b guided)
 *   otherwise                                              → one card per project (2)
 *
 * Reads GET /v1/projects/summary (§1). On an engine without it, the cards come from the project
 * list the console already has (names only) and say so; nothing is hidden.
 */
import React, { useEffect, useMemo, useState } from "react";
import { Folders, Plus } from "@phosphor-icons/react";

import { GatedButton } from "../../components/gated";
import {
  PX_PATHS, costWords, navigateTo, parseSummary, projectHref, startOnboarding, usePageSearch, usePxRead, type Node, type ProjectCard, type ProjectsSummary,
} from "../../lib/project-experience";
import { FX_SUMMARY } from "../../lib/project-experience-fixtures";
import { DemoBanner, Empty, Face, openAddProject, usePxUrl, HEALTH_WORD, Ico, Journey, Ring, Said, Spark, Tag, Cost } from "./kit";
import { FirstRun } from "./first-run";
import { StartProjectHost } from "../start-project/start-host";
import "./px.css";

export type ProjectsHomeProps = {
  projectId: string; actor: string; orgId?: string | null;
  /** The org's projects the viewer can open (the shell's project menu list): the fallback cards. */
  projects?: { id: string; name: string }[];
  homeId?: string;
  /** Setup's own word that GitHub, a model or a first PR is missing (the old Get started). */
  setupUnfinished?: boolean;
};


const FILTERS = [
  { key: "all", label: "All" }, { key: "needs", label: "Needs you" }, { key: "working", label: "Working" },
  { key: "onboarding", label: "Onboarding" }, { key: "archived", label: "Archived" },
] as const;
type Filter = (typeof FILTERS)[number]["key"];

export function ProjectsHome({ projectId, actor, orgId, projects = [], homeId, setupUnfinished = false }: ProjectsHomeProps) {
  const read = usePxRead<ProjectsSummary>(PX_PATHS.summaryAll(), parseSummary, { everyMs: 20_000, demo: () => parseSummary(FX_SUMMARY) });
  const url = usePxUrl();
  const fallback = useMemo<ProjectCard[]>(() => projects.map((p) => ({
    id: p.id, name: p.name, initials: p.name.slice(0, 2).toUpperCase(), repo: "", stage: "ready", journey: [], needs_you: 0, running: 0,
    health: "unknown", live_url: "", captain: null, updated_at: "", href: projectHref(p.id), cost_week: null, series: [],
    status_label: "", onboarding_active: false, start_onboarding: null, archived: false, uptime_pct: null, onboarding_progress: null,
    lines: [], line_tone: "", crew: [], live_agents: [], merged: 0, building: 0, week_cap_usd: null,
  })), [projects]);
  const cards = read.data?.items ?? (read.state === "missing" ? fallback : null);
  const withRepo = (cards ?? []).some((c) => c.repo);
  // FIRST RUN: asked for; or no project has a repo yet (the summary says so, or — on an engine
  // without it — setup itself says GitHub, a model or a first PR is missing).
  const firstRun = url.firstRun || (read.state === "ready" && cards !== null && !withRepo)
    || (read.state === "missing" && setupUnfinished);

  return (
    <div className="px" data-testid="px-projects">
      {read.demo ? <DemoBanner /> : null}
      {firstRun ? (
        <FirstRun projectId={projectId} actor={actor} cards={cards ?? []} />
      ) : cards === null ? (
        read.state === "error" ? <ProjectsList cards={fallback} cap={null} note={read.error ?? "The project summary didn't load."} onRetry={read.refresh} />
          : <ProjectsSkeleton />
      ) : (
        <ProjectsList cards={cards} cap={read.data?.week_cap_usd ?? null} onChanged={read.refresh}
                      note={read.state === "missing" ? "BOSS didn't answer with the project summary, so stages, health and cost are missing; names and links are live." : null} />
      )}
      <span hidden>{homeId}{orgId}</span>
      {/* + Add project opens THE sheet here even when the top bar draws no project menu (PX-20). */}
      <StartProjectHost projectId={projectId} homeId={homeId ?? projectId} />
    </div>
  );
}

/** Loading: the shape of the page (4 tiles, 3 cards), never a spinner over nothing. */
function ProjectsSkeleton() {
  return (
    <div className="px-stack" role="status" aria-label="Loading projects" data-testid="px-loading">
      <div className="px-grid px-g4">{[0, 1, 2, 3].map((i) => <div key={i} className="px-skel" style={{ minHeight: 96 }} />)}</div>
      <div className="px-grid px-g3">{[0, 1, 2].map((i) => <div key={i} className="px-skel" style={{ minHeight: 200 }} />)}</div>
    </div>
  );
}

const isWorking = (c: ProjectCard) => !c.archived && c.running > 0;
const isOnboarding = (c: ProjectCard) => !c.archived && (c.onboarding_active || (c.stage === "onboarding" && c.status_label !== "Not onboarded"));

function ProjectsList({ cards, note, cap, onRetry, onChanged }: {
  cards: ProjectCard[]; note?: string | null; cap: number | null; onRetry?: () => void; onChanged?: () => void;
}) {
  // `&filter=needs` (the bell's "N waiting on you in other projects") opens on that filter.
  const asked = usePageSearch().get("filter");
  const [filter, setFilter] = useState<Filter>(() => (FILTERS.some((f) => f.key === asked) ? asked as Filter : "all"));
  useEffect(() => { if (asked && FILTERS.some((f) => f.key === asked)) setFilter(asked as Filter); }, [asked]);
  const live = cards.filter((c) => !c.archived);
  const shown = (filter === "archived" ? cards.filter((c) => c.archived) : live)
    .filter((c) => filter === "all" || filter === "archived" || (filter === "needs" ? c.needs_you > 0 : filter === "working" ? isWorking(c) : isOnboarding(c)))
    // Needs-you first (the operator's rule: what waits on you is always on top), then the newest.
    .sort((x, y) => (y.needs_you > 0 ? 1 : 0) - (x.needs_you > 0 ? 1 : 0) || y.updated_at.localeCompare(x.updated_at));
  const needs = live.reduce((t, c) => t + c.needs_you, 0);
  const running = live.reduce((t, c) => t + c.running, 0);
  const cost = live.reduce((t, c) => t + (c.cost_week ?? 0), 0);
  const listed = live.reduce((t, c) => t + (c.list_week ?? 0), 0);
  const costSaid = costWords(cost, listed);
  // 02-05 the Running tile's faces are the agents acting right now (live_agents), never the roster.
  const crew = live.flatMap((c) => c.live_agents).filter((g, i, all) => all.findIndex((x) => x.role_key === g.role_key && x.name === g.name) === i).slice(0, 4);
  const counts: Record<Filter, number> = {
    all: live.length, needs: live.filter((c) => c.needs_you > 0).length,
    working: live.filter(isWorking).length, onboarding: live.filter(isOnboarding).length, archived: cards.filter((c) => c.archived).length,
  };
  return (
    <>
      <div className="px-page-h">
        <Ico icon={Folders} c="blue" size="l" />
        <div>
          <h1>Projects</h1>
          <p>Each one has a captain, its work, repo and deploys</p>
        </div>
        <span className="px-spacer" />
        <GatedButton permission="project:create" className="px-btn" data-kind="primary" data-size="l" data-testid="px-add-project"
                     title="A new app from an idea, or an existing repo" onClick={() => openAddProject("import")}>
          <Plus size={14} weight="bold" aria-hidden /> Add project
        </GatedButton>
      </div>
      {note ? (
        <div className="px-note" role={onRetry ? "alert" : "note"} data-testid="px-projects-note">
          {onRetry ? "Couldn't load projects. " : ""}{note}
          {onRetry ? <button type="button" className="px-btn" data-size="s" style={{ marginLeft: 8 }} onClick={onRetry} data-testid="px-projects-retry">Retry</button> : null}
        </div>
      ) : null}
      <div className="px-grid px-g4 px-pl-stats" data-testid="px-projects-stats">
        <Stat label="Projects" value={String(live.length)} testid="px-stat-projects" />
        <Stat label="Need you" value={String(needs)} tone={needs ? "amber" : undefined} testid="px-stat-needs"
              sub={needs ? `in ${counts.needs} project${counts.needs === 1 ? "" : "s"}` : "all clear"}
              onClick={needs ? () => setFilter("needs") : undefined} />
        <Stat label="Running" value={String(running)} tone={running ? "teal" : undefined} testid="px-stat-running"
              onClick={running ? () => setFilter("working") : undefined}>
          {crew.length ? <span className="px-faces" data-testid="px-stat-running-faces">{crew.map((g) => <Face key={`${g.role_key}-${g.name}`} agent={g} size={22} state="working" />)}</span> : null}
        </Stat>
        <Stat label="Cost · week" value={costSaid ? costSaid.text + (costSaid.plan ? " · plan" : "") : live.some((c) => c.cost_week !== null) ? "$0.00" : "—"} testid="px-stat-cost"
              sub={cap ? `of $${cap % 1 ? cap.toFixed(2) : cap} cap` : undefined} />
      </div>
      <div className="px-filters" role="group" aria-label="Filter projects">
        {FILTERS.map((f) => (
          <button key={f.key} type="button" className="px-filter" aria-pressed={filter === f.key} data-testid={`px-filter-${f.key}`}
                  onClick={() => setFilter(f.key)}>
            {f.key === "needs" ? <span className="px-dot" data-s="you" aria-hidden /> : null}{f.label}{f.key === "all" || counts[f.key] ? <> {counts[f.key]}</> : null}
          </button>
        ))}
      </div>
      <div className="px-grid px-g3" data-testid="px-project-cards">
        {shown.map((c) => <ProjectCardView key={c.id} card={c} onChanged={onChanged} />)}
        {filter === "all" ? (
          <button type="button" className="px-pc-new" data-testid="px-card-add" onClick={() => openAddProject("import")}>
            <span className="px-pc-plus" aria-hidden><Plus size={22} /></span>
            <b>Add project</b>
            <small>New idea · existing repo</small>
          </button>
        ) : null}
      </div>
      {!shown.length && filter !== "all" ? (
        <Empty icon={Folders} title={filter === "archived" ? "No archived projects" : "Nothing here"}
               line={filter === "archived" ? "Projects you archive wait here; they keep their history." : "No project matches this filter."} testid="px-projects-filter-empty">
          <button type="button" className="px-btn" onClick={() => setFilter("all")}>Show all</button>
        </Empty>
      ) : null}
    </>
  );
}

function Stat({ label, value, sub, tone, onClick, children, testid }: {
  label: string; value: string; sub?: string; tone?: "amber" | "teal"; onClick?: () => void; children?: React.ReactNode; testid?: string;
}) {
  const body = (
    <>
      <span className="px-lbl">{label}</span>
      <span className="px-num" style={tone ? { color: `var(--${tone})` } : undefined}>{value}</span>
      {children}
      {sub ? <small className="px-faint">{sub}</small> : null}
    </>
  );
  return onClick ? (
    <button type="button" className="px-card px-stat" data-tone={tone} data-link onClick={onClick} data-testid={testid} style={{ textAlign: "left", font: "inherit", color: "inherit" }}>{body}</button>
  ) : <div className="px-card px-stat" data-tone={tone} data-testid={testid}>{body}</div>;
}

/** Screen 02's card strip: four stages (Onboard · Ready · Working · Deployed) from the six. */
export function cardJourney(card: ProjectCard): Node[] {
  const by = Object.fromEntries(card.journey.map((j) => [j.key, j]));
  const pick = (...keys: string[]): Node["state"] => {
    const states = keys.map((k) => by[k]?.state).filter(Boolean) as Node["state"][];
    if (states.includes("waiting")) return "waiting";
    if (states.includes("now")) return "now";
    if (states.includes("failed")) return "failed";
    return states.some((x) => x === "done") ? "done" : "todo";
  };
  const deployed = by.staging?.state === "done" || by.production?.state === "done" ? "done" : pick("staging", "production");
  const ready = pick("ready", "backlog");
  return [
    { key: "onboarding", label: "Onboard", state: card.onboarding_active ? "now" : pick("onboarding"), sub: "", href: "" },
    { key: "ready", label: "Ready", state: ready === "done" || by.working?.state === "done" || by.working?.state === "now" || by.working?.state === "waiting" ? "done" : ready, sub: "", href: "" },
    // Working pulses only while the card's work runs; a quiet project's work is done.
    { key: "working", label: "Working", state: card.running > 0 || by.working?.state === "waiting" ? (pick("working") === "todo" ? "todo" : "now")
        : pick("working") === "now" ? "done" : pick("working"), sub: "", href: "" },
    { key: "staging", label: "Deployed", state: deployed, sub: "", href: "" },
  ];
}

export function ProjectCardView({ card, onChanged }: { card: ProjectCard; onChanged?: () => void }) {
  const href = projectHref(card.id);
  const open = () => navigateTo(href);
  const [said, setSaid] = useState("");
  const deployed = card.journey.some((j) => (j.key === "staging" || j.key === "production") && j.state === "done");
  // THE CHIP SAYS THE REAL STATE: Needs you · Onboarding · Working (only while something
  // runs, teal and live) · Live · Idle — never "Working" over a card that says idle.
  const pill = card.needs_you ? <Tag tone="amber">{card.needs_you} need you</Tag>
    : card.onboarding_active ? <Tag tone="blue">Onboarding</Tag>
    : card.archived ? <Tag tone="idle">Archived</Tag>
    : card.running > 0 ? <Tag tone="teal"><span className="px-dot" data-s="now" data-round aria-hidden />Working</Tag>
    : deployed ? <Tag tone="green">Live</Tag>
    : card.status_label === "Not onboarded" ? <Tag tone="idle">Not onboarded</Tag>
    : card.status_label === "Idle" || card.stage === "working" ? <Tag tone="idle">Idle</Tag>
    : <Tag tone="blue">{card.status_label || "Ready"}</Tag>;
  const startOnb = async () => {
    const r = await startOnboarding(card.id);
    setSaid(r.ok ? (r.demo ? "Sample mode — nothing was sent." : "Helm is reading it now.") : (r.error ?? "It didn't start."));
    if (r.ok && !r.demo) onChanged?.();
  };
  const progress = card.onboarding_progress;
  const lines = card.lines.length ? card.lines : [card.live_url ? "live" : "not deployed yet"];
  return (
    <article className="px-card px-pc" data-needs={card.needs_you > 0 || undefined} data-link data-testid={`px-card-${card.id}`}
             title={`Open ${card.name}`}
             onClick={(e) => { if ((e.target as HTMLElement).closest("a,button")) return; open(); }}>
      <div className="px-row" style={{ alignItems: "flex-start" }}>
        <span className="px-mark" data-size="s" aria-hidden>{card.initials}</span>
        <div style={{ minWidth: 0, flex: 1 }}>
          {/* A real link: the card's name is how a keyboard (and a middle-click) opens it. */}
          <a className="px-pc-name px-ell" href={href} data-console-load data-testid={`px-card-open-${card.id}`} style={{ display: "block" }}>{card.name}</a>
          <div className="px-pc-repo px-ell">{card.repo || "no repo yet"}</div>
        </div>
        {pill}
      </div>
      {card.journey.length ? <Journey nodes={cardJourney(card)} size="s" testid={`px-card-journey-${card.id}`} /> : null}
      <div className="px-row">
        {card.onboarding_active && progress ? (
          <Ring pct={progress[1] ? (100 * progress[0]) / progress[1] : 0} tone="teal" size="s" label={`Onboarding ${progress[0]} of ${progress[1]}`}>{progress[0]}/{progress[1]}</Ring>
        ) : card.uptime_pct !== null ? (
          <Ring pct={card.uptime_pct} tone={card.health === "down" ? "red" : card.health === "degraded" ? "amber" : "green"} size="s"
                label={`Uptime ${card.uptime_pct}% in the last 24 h, ${HEALTH_WORD[card.health]}`}>{card.uptime_pct >= 99.95 ? "100" : card.uptime_pct.toFixed(1).replace(/\.0$/, "")}</Ring>
        ) : <Ring pct={0} tone="teal" size="s" label="No health signal yet">—</Ring>}
        <div style={{ minWidth: 0, flex: 1, fontSize: ".6rem" }} data-testid={`px-card-lines-${card.id}`}>
          <span className="px-pc-line1" data-tone={card.line_tone || undefined}>
            <span className="px-dot" data-s={card.line_tone === "done" ? "done" : card.line_tone === "now" ? "now" : card.line_tone === "bad" ? "bad" : card.line_tone === "you" ? "you" : undefined} aria-hidden />
            {lines[0]}</span>
          {card.status_label === "Not onboarded" && !card.archived ? (
            <GatedButton permission="workflow_run:start" projectId={card.id} className="px-linkbtn" data-testid={`px-card-onboard-${card.id}`}
                         onClick={() => void startOnb()}>Not onboarded · Start onboarding</GatedButton>
          ) : lines[1] ? <span className="px-faint px-ell" style={{ display: "block" }}>{lines[1]}</span> : null}
          {said ? <Said text={said} tone={said.startsWith("Helm") || said.startsWith("Sample") ? "green" : "red"} /> : null}
        </div>
        {card.series.length > 1 ? <div className="px-pc-spark"><Spark series={card.series} label={`${card.name} cost this week`} /></div> : null}
      </div>
      <div className="px-pc-foot">
        {card.captain ? <><Face agent={card.captain} size={20} state={card.onboarding_active ? "working" : "idle"} /><span>{card.captain.name}</span></> : <span className="px-faint">No captain yet</span>}
        {/* 02-16 the crew's faces, always; the working face on the ones acting right now. */}
        {card.crew.length ? <span className="px-faces" title={card.crew.map((g) => `${g.name}${g.state === "working" ? " (working now)" : ""}`).join(", ")} data-testid={`px-card-crew-${card.id}`}>
          {card.crew.map((g) => <Face key={g.role_key} agent={g} size={20} state={g.state === "working" ? "working" : "idle"} />)}</span> : null}
        <span className="px-spacer" />
        <Cost usd={card.cost_week} list={card.list_week} testid={`px-card-cost-${card.id}`} />
      </div>
    </article>
  );
}

export { openAddProject };
