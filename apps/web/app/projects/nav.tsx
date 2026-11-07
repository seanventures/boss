"use client";

/**
 * The rail's PROJECTS section (BOSSAPP-914 screen 1): Projects, then a SHORT list of projects (a dot
 * coloured by its state — amber needs you, teal working, green ok, grey not started) for one-click
 * switching, then "All projects (N) ›" and "+ Add project".
 *
 * AT MOST FIVE ROWS (walk 2026-11-06, GAPS 1): twelve projects with two-line names pushed every area
 * after Code Tasks off the rail. The five are the ones that need you first, then the most recently
 * active; the open project is always one of them. Each is one line with an ellipsis (the full name
 * is the tooltip), so the rail's height no longer depends on how many projects there are.
 */
import React, { useMemo } from "react";
import { CaretRight, Folders, Plus } from "@phosphor-icons/react";

import { PX_PATHS, parseSummary, projectHref, usePxRead, type ProjectsSummary } from "../../lib/project-experience";
import { FX_SUMMARY } from "../../lib/project-experience-fixtures";
import { openAddProject } from "./kit";

/** How many projects the rail lists before "All projects (N) ›". */
export const RAIL_PROJECTS_MAX = 5;

export type RailProject = { id: string; name: string; dot: "you" | "now" | "idle" | "done" | ""; needs: number; at: string };

const PROJECTS_TIP = "Projects — Every project: its work, crew, repo and deploys";
const ADD_TIP = "Add a project — new from an idea, or an existing repo";

const DOT_WORDS: Record<RailProject["dot"], string> = {
  you: "needs you", now: "working", idle: "not started", done: "up to date", "": "",
};

/** The rows the rail shows: needs-you first (most first), then most recent, at most `max`, and the
 *  open project always among them (it takes the last place when it would not have made the cut). */
export function railProjects(list: RailProject[], activeId: string | null, max = RAIL_PROJECTS_MAX): RailProject[] {
  const ranked = [...list].sort((a, b) =>
    (b.needs > 0 ? 1 : 0) - (a.needs > 0 ? 1 : 0) || b.needs - a.needs
    || (b.at || "").localeCompare(a.at || "") || a.name.localeCompare(b.name));
  const top = ranked.slice(0, max);
  const current = activeId ? ranked.find((p) => p.id === activeId) : undefined;
  if (current && !top.includes(current)) top[top.length - 1] = current;
  return top;
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
  const list: RailProject[] = useMemo(() => (cards?.length
    ? cards.map((c): RailProject => ({
      id: c.id, name: c.name, needs: c.needs_you, at: c.updated_at,
      // The same meaning as the Projects list: amber only when you are needed, teal only while
      // something is actually running, grey for a project nobody has started on.
      dot: c.needs_you ? "you" : c.running ? "now" : c.stage === "onboarding" ? "idle" : "done",
    }))
    : items.map((p): RailProject => ({ id: p.id, name: p.name, dot: "", needs: 0, at: "" }))), [cards, items]);
  const onProjects = active === "projects";
  const onProject = active === "project";
  const shown = open ? railProjects(list, activeProjectId) : list.filter((p) => p.id === activeProjectId);
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
                      title={ADD_TIP} onClick={() => openAddProject("import")}>
                <Plus size={13} aria-hidden /><span className="bx-nav-ell">Add project</span>
              </button>
            </>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
