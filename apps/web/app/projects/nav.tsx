"use client";

/**
 * The rail's PROJECTS section (BOSSAPP-914 screen 1): Projects, then a SHORT list of projects (a dot
 * coloured by its state — amber needs you, teal working, green ok, grey not started) for one-click
 * switching, then "All projects (N) ›" and "+ Add project".
 *
 * AT MOST FIVE ROWS (walk 2026-11-06, GAPS 1): twelve projects with two-line names pushed every area
 * after Code Tasks off the rail. The five are the ones that need you first, then the most recently
 * active; the open project, the one you opened last and the newest one are always among them. Each is one line with an ellipsis (the full name
 * is the tooltip), so the rail's height no longer depends on how many projects there are.
 */
import React, { useEffect, useMemo, useState } from "react";
import { CaretRight, Folders, Plus } from "@phosphor-icons/react";

import { PX_PATHS, parseSummary, projectHref, usePxRead, type ProjectCard, type ProjectsSummary } from "../../lib/project-experience";
import { FX_SUMMARY } from "../../lib/project-experience-fixtures";
import { openAddProject } from "./kit";

/** How many projects the rail lists before "All projects (N) ›". */
export const RAIL_PROJECTS_MAX = 5;

export type RailProject = { id: string; name: string; dot: "you" | "now" | "idle" | "done" | ""; needs: number; at: string;
  /** When it was made: the newest project always has a row (2026-11-19: Park bench map, just
   *  created, was hidden under five projects that needed the person more). */
  created?: string };

const PROJECTS_TIP = "Projects — Every project: its work, crew, repo and deploys";
const ADD_TIP = "Add a project — new from an idea, or an existing repo";

const DOT_WORDS: Record<RailProject["dot"], string> = {
  you: "needs you", now: "working", idle: "not started", done: "up to date", "": "",
};

/** The rows the rail shows: needs-you first (most first), then most recent, at most `max`. Three
 *  projects always have a row, whatever the ranking: the open one, the one the person opened
 *  last (`recentId`) and the newest one made. They take the places of the lowest-ranked rows,
 *  and every row keeps its ranked order, so the cap holds and nothing jumps about. */
export function railProjects(list: RailProject[], activeId: string | null, max = RAIL_PROJECTS_MAX,
                             recentId: string | null = null): RailProject[] {
  const ranked = [...list].sort((a, b) =>
    (b.needs > 0 ? 1 : 0) - (a.needs > 0 ? 1 : 0) || b.needs - a.needs
    || (b.at || "").localeCompare(a.at || "") || a.name.localeCompare(b.name));
  const newest = list.reduce<RailProject | undefined>(
    (best, p) => (p.created && (!best || p.created > (best.created ?? "")) ? p : best), undefined);
  const kept = new Set<string>();
  for (const id of [activeId, recentId, newest?.id]) {
    if (id && kept.size < max && ranked.some((p) => p.id === id)) kept.add(id);
  }
  for (const p of ranked) {
    if (kept.size >= max) break;
    kept.add(p.id);
  }
  return ranked.filter((p) => kept.has(p.id));
}

/** The project this person opened last, remembered in this browser only (a convenience: the
 *  rail still works when storage is blocked). */
const LAST_OPENED_KEY = "boss.rail.last-project";
export function readLastOpened(): string | null {
  try { return window.localStorage.getItem(LAST_OPENED_KEY) || null; } catch { return null; }
}
export function rememberLastOpened(id: string): void {
  try { window.localStorage.setItem(LAST_OPENED_KEY, id); } catch { /* storage blocked */ }
}

/** THE RAIL COUNTS WHAT "All projects" COUNTS (stranger test 2026-11-17: the rail said "All
 *  projects (1)" over a page that said "Add your first project"). Once the summary has answered,
 *  its projects are the list, even when there are none: the install's Default project, which the
 *  summary leaves out, is never listed (or counted) from the shell's own list instead. The shell's
 *  list stands in only when the summary cannot be read at all. */
export function railList(state: string, cards: ProjectCard[] | undefined | null,
                         items: { id: string; name: string }[]): RailProject[] {
  if (cards && (state === "ready" || cards.length)) {
    return cards.filter((c) => !c.archived).map((c): RailProject => ({
      id: c.id, name: c.name, needs: c.needs_you, at: c.updated_at, created: c.created_at || undefined,
      // The same meaning as the Projects list: amber only when you are needed, teal only while
      // something is actually running, grey for a project nobody has started on.
      dot: c.needs_you ? "you" : c.running ? "now" : c.stage === "onboarding" ? "idle" : "done",
    }));
  }
  if (state === "loading") return [];
  return items.map((p): RailProject => ({ id: p.id, name: p.name, dot: "", needs: 0, at: "" }));
}

export function ProjectsNav({ items, active, activeProjectId, onGo, open, onToggle }: {
  items: { id: string; name: string }[];
  /** The shell's active area. */
  active: string;
  activeProjectId: string | null;
  onGo: () => void;
  open: boolean;
  onToggle: () => void;
}) {
  const summary = usePxRead<ProjectsSummary>(PX_PATHS.summary(), parseSummary, { everyMs: 30_000, demo: () => parseSummary(FX_SUMMARY) });
  const cards = summary.data?.items;
  const list: RailProject[] = useMemo(() => railList(summary.state, cards, items), [summary.state, cards, items]);
  const onProjects = active === "projects";
  const onProject = active === "project";
  // The project page you are on is the one you opened last; read after that is written.
  const [recentId, setRecentId] = useState<string | null>(null);
  useEffect(() => {
    if (onProject && activeProjectId) rememberLastOpened(activeProjectId);
    setRecentId(readLastOpened());
  }, [onProject, activeProjectId]);
  const shown = open ? railProjects(list, activeProjectId, RAIL_PROJECTS_MAX, recentId) : list.filter((p) => p.id === activeProjectId);
  // The list's own states (wireframe 01): a skeleton while it loads, a quiet retry when it fails.
  const loading = !list.length && summary.state === "loading";
  const failed = !list.length && summary.state === "error";
  return (
    <div className="bx-nav-section bx-nav-projects" data-open={open || undefined} data-testid="nav-projects">
      <div className="bx-nav-projects-head">
        <button type="button" className={`bx-nav-item${onProjects ? " active" : ""}`} aria-current={onProjects ? "page" : undefined}
                data-area-nav="projects" data-label={PROJECTS_TIP} title={PROJECTS_TIP} onClick={onGo}>
          <Folders size={17} weight={onProjects || onProject ? "fill" : "regular"} aria-hidden />
          <span>Projects</span>
          {/* NEW while Projects is not the page you are on (wireframe 01). */}
          {onProjects ? null : <span className="bx-nav-new" data-testid="nav-projects-new">NEW</span>}
        </button>
        {list.length ? (
          <button type="button" className="bx-nav-caret-btn" aria-expanded={open} aria-label={open ? "Hide the project list" : "Show the project list"}
                  onClick={onToggle} data-testid="nav-projects-toggle">
            <CaretRight size={12} weight="bold" aria-hidden />
          </button>
        ) : null}
      </div>
      {open || (onProject && shown.length) ? (
        <div className="bx-nav-children" data-testid="nav-projects-list">
          {shown.map((p) => {
            const here = onProject && p.id === activeProjectId;
            return (
              <a key={p.id} className={`bx-nav-item bx-nav-child bx-nav-project${here ? " active" : ""}`} aria-current={here ? "page" : undefined}
                 href={projectHref(p.id)} data-console-load data-testid={`nav-project-${p.id}`}
                 title={DOT_WORDS[p.dot] ? `${p.name} · ${DOT_WORDS[p.dot]}` : p.name}>
                <i className="bx-nav-dot" data-s={p.dot || undefined} aria-hidden />
                <span className="bx-nav-ell">{p.name}</span>
              </a>
            );
          })}
          {open && loading ? (
            <span className="bx-nav-skeleton" data-testid="nav-projects-loading" aria-label="Loading projects">
              <i /><i /><i />
            </span>
          ) : null}
          {open && failed ? (
            <span className="bx-nav-child bx-nav-failed" data-testid="nav-projects-error">
              Couldn&rsquo;t load projects ·{" "}
              <button type="button" className="linklike" onClick={summary.refresh}>Retry</button>
            </span>
          ) : null}
          {open ? (
            <>
              {list.length ? (
                <button type="button" className="bx-nav-item bx-nav-child bx-nav-all" data-testid="nav-all-projects"
                        title="Every project, with its state" onClick={onGo}>
                  <span className="bx-nav-ell">All projects ({list.length})</span>
                  <CaretRight size={11} weight="bold" aria-hidden />
                </button>
              ) : null}
              <button type="button" className="bx-nav-item bx-nav-child" data-testid="nav-add-project"
                      title={ADD_TIP} onClick={() => openAddProject("1")}>
                <Plus size={13} aria-hidden /><span className="bx-nav-ell">Add project</span>
              </button>
            </>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
