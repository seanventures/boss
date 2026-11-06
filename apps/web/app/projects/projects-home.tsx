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
import React, { useMemo, useState } from "react";
import { Folders, Plus, Lightning, Coins, HandWaving, GitBranch } from "@phosphor-icons/react";

import { GatedButton } from "../../components/gated";
import {
  PX_PATHS, isPxDemo, navigateTo, parseSummary, projectHref, readPxUrl, usePxRead, writePxUrl, type ProjectCard, type ProjectsSummary,
} from "../../lib/project-experience";
import { FX_SUMMARY } from "../../lib/project-experience-fixtures";
import { DemoBanner, Empty, Face, Faces, openAddProject, usePxUrl, HEALTH_TONE, HEALTH_WORD, Ico, Journey, Loading, Ring, Spark, Tag } from "./kit";
import { FirstRun } from "./first-run";
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
  { key: "onboarding", label: "Onboarding" },
] as const;
type Filter = (typeof FILTERS)[number]["key"];

export function ProjectsHome({ projectId, actor, orgId, projects = [], homeId, setupUnfinished = false }: ProjectsHomeProps) {
  const read = usePxRead<ProjectsSummary>(PX_PATHS.summary(), parseSummary, { everyMs: 20_000, demo: () => parseSummary(FX_SUMMARY) });
  const url = usePxUrl();
  const fallback = useMemo<ProjectCard[]>(() => projects.map((p) => ({
    id: p.id, name: p.name, initials: p.name.slice(0, 2).toUpperCase(), repo: "", stage: "ready", journey: [], needs_you: 0, running: 0,
    health: "unknown", live_url: "", captain: null, updated_at: "", href: projectHref(p.id), cost_week: null, series: [],
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
        read.state === "error" ? <ProjectsList cards={fallback} note={read.error ?? "The project summary didn't load."} homeId={homeId} orgId={orgId} />
          : <Loading rows={2} label="Loading projects" />
      ) : (
        <ProjectsList cards={cards} homeId={homeId} orgId={orgId}
                      note={read.state === "missing" ? "Stages, health and cost per project arrive with the next BOSS update." : null} />
      )}
    </div>
  );
}

function ProjectsList({ cards, note }: { cards: ProjectCard[]; note?: string | null; homeId?: string; orgId?: string | null }) {
  const [filter, setFilter] = useState<Filter>("all");
  const shown = cards
    .filter((c) => filter === "all" || (filter === "needs" ? c.needs_you > 0 : filter === "working" ? c.stage === "working" || c.running > 0 : c.stage === "onboarding"))
    // Needs-you first (the operator's rule: what waits on you is always on top), then the newest.
    .sort((x, y) => (y.needs_you > 0 ? 1 : 0) - (x.needs_you > 0 ? 1 : 0) || y.updated_at.localeCompare(x.updated_at));
  const needs = cards.reduce((t, c) => t + c.needs_you, 0);
  const running = cards.reduce((t, c) => t + c.running, 0);
  const cost = cards.reduce((t, c) => t + (c.cost_week ?? 0), 0);
  const counts: Record<Filter, number> = {
    all: cards.length, needs: cards.filter((c) => c.needs_you > 0).length,
    working: cards.filter((c) => c.stage === "working" || c.running > 0).length, onboarding: cards.filter((c) => c.stage === "onboarding").length,
  };
  return (
    <>
      <nav className="px-crumbs" aria-label="Breadcrumb"><span aria-current="page">Projects</span></nav>
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
      {note ? <div className="px-note" role="note" data-testid="px-projects-note">{note}</div> : null}
      <div className="px-grid px-g4" data-testid="px-projects-stats">
        <Stat label="Projects" value={String(cards.length)} icon={Folders} />
        <Stat label="Need you" value={String(needs)} icon={HandWaving} tone={needs ? "amber" : undefined}
              sub={needs ? `in ${counts.needs} project${counts.needs === 1 ? "" : "s"}` : "all clear"}
              onClick={needs ? () => setFilter("needs") : undefined} />
        <Stat label="Running" value={String(running)} icon={Lightning} tone={running ? "teal" : undefined}
              onClick={running ? () => setFilter("working") : undefined} />
        <Stat label="Cost · week" value={cards.some((c) => c.cost_week !== null) ? `$${cost.toFixed(2)}` : "—"} icon={Coins} />
      </div>
      <div className="px-filters" role="group" aria-label="Filter projects">
        {FILTERS.map((f) => (
          <button key={f.key} type="button" className="px-filter" aria-pressed={filter === f.key} data-testid={`px-filter-${f.key}`}
                  onClick={() => setFilter(f.key)}>
            {f.key === "needs" ? <span className="px-dot" data-s="you" aria-hidden /> : null}{f.label} {counts[f.key] ? <span className="px-mono">{counts[f.key]}</span> : null}
          </button>
        ))}
      </div>
      <div className="px-grid px-g3" data-testid="px-project-cards">
        {shown.map((c) => <ProjectCardView key={c.id} card={c} />)}
        {filter === "all" ? (
          <button type="button" className="px-pc-new" data-testid="px-card-add" onClick={() => openAddProject("import")}>
            <Ico icon={Plus} c="teal" size="l" />
            <b>Add project</b>
            <small>New idea · existing repo</small>
          </button>
        ) : null}
      </div>
      {!shown.length && filter !== "all" ? (
        <Empty icon={Folders} title="Nothing here" line="No project matches this filter." testid="px-projects-filter-empty">
          <button type="button" className="px-btn" onClick={() => setFilter("all")}>Show all</button>
        </Empty>
      ) : null}
    </>
  );
}

function Stat({ label, value, icon, sub, tone, onClick }: { label: string; value: string; icon: typeof Folders; sub?: string; tone?: "amber" | "teal"; onClick?: () => void }) {
  const body = (
    <>
      <div className="px-row"><Ico icon={icon} c={tone ?? "blue"} /><span className="px-lbl">{label}</span></div>
      <span className="px-num" style={tone ? { color: `var(--${tone})` } : undefined}>{value}</span>
      {sub ? <small className="px-faint">{sub}</small> : null}
    </>
  );
  return onClick ? (
    <button type="button" className="px-card px-stat" data-tone={tone} data-link onClick={onClick} style={{ textAlign: "left", font: "inherit", color: "inherit" }}>{body}</button>
  ) : <div className="px-card px-stat" data-tone={tone}>{body}</div>;
}

const STAGE_WORD: Record<string, { label: string; tone: "green" | "amber" | "blue" | "violet" | "idle" }> = {
  onboarding: { label: "Onboarding", tone: "blue" }, ready: { label: "Ready", tone: "green" }, backlog: { label: "Backlog", tone: "violet" },
  working: { label: "Working", tone: "green" }, staging: { label: "Live", tone: "green" }, production: { label: "In prod", tone: "green" },
};

export function ProjectCardView({ card }: { card: ProjectCard }) {
  const href = projectHref(card.id);
  const stage = STAGE_WORD[card.stage] ?? STAGE_WORD.ready;
  const open = () => navigateTo(href);
  return (
    <article className="px-card px-pc" data-needs={card.needs_you > 0 || undefined} data-link data-testid={`px-card-${card.id}`}
             onClick={(e) => { if ((e.target as HTMLElement).closest("a,button")) return; open(); }}>
      <div className="px-row">
        <span className="px-mark" data-size="s" aria-hidden>{card.initials}</span>
        <div style={{ minWidth: 0, flex: 1 }}>
          {/* A real link: the card's name is how a keyboard (and a middle-click) opens it. */}
          <a className="px-pc-name px-ell" href={href} data-console-load data-testid={`px-card-open-${card.id}`} style={{ display: "block" }}>{card.name}</a>
          <div className="px-pc-repo px-ell">{card.repo ? <><GitBranch size={10} aria-hidden /> {card.repo}</> : "no repo yet"}</div>
        </div>
        {card.needs_you ? <Tag tone="amber">{card.needs_you} need you</Tag> : <Tag tone={stage.tone}>{stage.label}</Tag>}
      </div>
      {card.journey.length ? <Journey nodes={card.journey} size="s" testid={`px-card-journey-${card.id}`} /> : null}
      <div className="px-row">
        {card.health !== "unknown" ? (
          <Ring pct={card.health === "healthy" ? 100 : card.health === "degraded" ? 60 : 20} tone={HEALTH_TONE[card.health] === "idle" ? "teal" : (HEALTH_TONE[card.health] as "green")}
                size="s" label={`Health: ${HEALTH_WORD[card.health]}`}>{card.health === "healthy" ? "ok" : "!"}</Ring>
        ) : null}
        <div style={{ minWidth: 0, flex: 1, fontSize: ".6rem" }}>
          {card.live_url ? (
            <a href={card.live_url} target="_blank" rel="noreferrer noopener" className="px-ell" style={{ display: "block" }}>
              <span className="px-dot" data-s={card.health} data-round aria-hidden /> {card.live_url.replace(/^https?:\/\//, "")}
            </a>
          ) : <span className="px-faint">{card.stage === "onboarding" ? "Helm is reading it" : "not deployed yet"}</span>}
          {card.running ? <span className="px-faint" style={{ display: "block" }}>{card.running} running</span> : null}
        </div>
        {card.series.length > 1 ? <div style={{ width: 80 }}><Spark series={card.series} label={`${card.name} cost this week`} /></div> : null}
      </div>
      <div className="px-pc-foot">
        {card.captain ? <><Face agent={card.captain} size={20} /><span>{card.captain.name}</span></> : <span className="px-faint">No captain yet</span>}
        <span className="px-spacer" />
        {card.cost_week !== null ? <span className="px-mono">${card.cost_week.toFixed(2)}</span> : null}
      </div>
    </article>
  );
}

export { openAddProject };
