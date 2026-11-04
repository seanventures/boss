"use client";

import React, { useEffect, useRef, useState } from "react";

import { GatedButton } from "../components/gated";
import { consoleHref } from "./console-href";
import { useDetailsDismiss } from "../lib/details-dismiss";
import { EMPTY_DRAFT, NewProjectSheet, type NewProjectDraft } from "./new-project-sheet";
import { StartProjectSheet } from "./start-project/start-sheet";
import { NEW_PROJECT_PARAM, clearStepParam, projectPageHref, stepFromParam, type StartStep } from "../lib/start-project";
import { ArchiveDialog, ArchivedNotice, ArchivedProjects, useArchived } from "./project-archive";
import type { ProjectType } from "@boss/api-client";

export type ProjectMenuItem = { id: string; name: string; type?: ProjectType };

/** Where a project opens: the org's own project is the console with no ?project_id=. */
export function projectHref(id: string, homeId: string): string {
  // The AREA STAYS (tester round 2: switching project on the Knowledge Graph landed on the Command
  // Center); what names one project's records (a node, a scan, a build) does not.
  return consoleHref({ project_id: id === homeId ? undefined : id, task: undefined, run: undefined,
                       kg_node: undefined, kg_scan: undefined, kg_tab: undefined, kg_sub: undefined, build: undefined, role: undefined });
}

/** Where a new knowledge-graph project opens: its Knowledge Graph setup (kg-setup.tsx). */
export function knowledgeSetupHref(id: string, homeId: string): string {
  return consoleHref({ project_id: id === homeId ? undefined : id, area: "knowledge", kg_tab: "setup",
                       task: undefined, run: undefined });
}

/** The two kinds of project, said in one line each (POST /v1/projects `project_type`). */
export { PROJECT_TYPES } from "./new-project-sheet";

/** The short name a project's type goes by in a list (none for an app: it is the usual kind). */
export function projectTypeBadge(type: ProjectType | undefined): string | null {
  return type === "knowledge_graph" ? "Knowledge graph" : null;
}

const NEW_PROJECT_TIP =
  "A new project in this organization: its own agents, skills, budget, settings and work, set up like the first one";
const ARCHIVE_TIP = "Hide this project and pause its schedules, monitors and indexing. Nothing is deleted";
const HOME_TIP = "The home project can't be archived: BOSS's own agent and the worker's schedules live there";

/**
 * THE PROJECT MENU, in the top bar beside the organization: which project is open, the org's
 * other projects, and New project (POST /v1/projects, `project:create`). New project asks what
 * the project is FOR first — build an app, or a knowledge graph for an existing codebase — and a
 * knowledge-graph project opens on its guided setup (Knowledge Graph › Setup). A project's type
 * shows beside its name. Short labels, tooltips
 * for the rest. The API decides the permission and its step-up; the button only mirrors it.
 *
 * ARCHIVE (BOSSAPP-768 S5, spec §5.9): "Archive project…" opens a confirm that names what stops
 * for THIS project (project:delete). Archived projects are not in `projects` (GET /v1/projects
 * leaves them out); "Show archived" lists them with Restore. An open project that is archived
 * says so in the menu, with Restore.
 */
export function ProjectMenu({ projects, activeId, homeId, orgId }: {
  projects: ProjectMenuItem[];
  activeId: string;
  homeId: string;
  orgId?: string;
}) {
  // NEW PROJECT (BOSSAPP-810): which screen of the flow is open — the three doors and the presets,
  // the sample, Import a repo, the idea (one sentence), or the name-and-description form (both types).
  // `/?new_project=sample|import|idea|1` opens it at that choice (the Welcome screen's links).
  const [step, setStep] = useState<StartStep | null>(null);
  useEffect(() => {
    // On load, and on an in-console link (pushState + popstate) to `?new_project=…`.
    const read = () => {
      try {
        const asked = stepFromParam(new URL(window.location.href).searchParams.get(NEW_PROJECT_PARAM));
        if (asked) setStep(asked);
      } catch { /* non-browser */ }
    };
    read();
    window.addEventListener("popstate", read);
    return () => window.removeEventListener("popstate", read);
  }, []);
  const closeStart = () => { setStep(null); clearStepParam(); };
  // The draft is the menu's, not the sheet's form: a passkey prompt or a refused step-up keeps it.
  const [draft, setDraft] = useState<NewProjectDraft>(EMPTY_DRAFT);
  const [archiving, setArchiving] = useState(false);
  const [showArchived, setShowArchived] = useState(false);
  const active = projects.find((p) => p.id === activeId);
  // Not in the list: it may be archived. Asked once per project; never for a listed one.
  const archived = useArchived(active ? undefined : activeId);
  const isHome = activeId === homeId;
  const goHome = () => window.location.assign(projectHref(homeId, homeId));
  const openProject = (id: string) => window.location.assign(projectHref(id, homeId));
  // A menu closes when it has done its job or the person looks away (KG tester round 2: it stayed
  // open over the page after a pick): on a pick, on Esc, and on a press anywhere outside it.
  const menu = useRef<HTMLDetailsElement>(null);
  const { open: isOpen, onToggle, close } = useDetailsDismiss(menu);
  return (
    <>
    <details ref={menu} className="bx-project-menu" data-testid="project-menu"
             onToggle={onToggle}>
      <summary title={`Project${projectTypeBadge(active?.type) ? ` · ${projectTypeBadge(active?.type)}` : ""} · switch, create, or archive one`}>
        {active?.name ?? archived?.name ?? activeId}
        {projectTypeBadge(active?.type) ? <span className="bx-project-menu-type" data-testid="project-menu-type">{projectTypeBadge(active?.type)}</span> : null}
        {archived ? <span className="bx-project-menu-archived" data-testid="project-menu-archived-badge">Archived</span> : null}
      </summary>
      <div className="bx-project-menu-panel">
        {archived ? <ArchivedNotice state={archived} onRestored={openProject} /> : null}
        <ul aria-label="Projects">
          {projects.map((p) => (
            <li key={p.id}>
              {/* A real load (data-console-load): the home project's link names no project, so the
                  shell could not tell it from an in-console reveal of the project on screen. */}
              <a href={projectHref(p.id, homeId)} aria-current={p.id === activeId ? "page" : undefined}
                 data-testid={`project-menu-${p.id}`} data-console-load=""
                 onClick={(e) => {
                   // Read the URL at the press, not at render: a server-rendered href has no query
                   // (no window), and hydration does not rewrite it, so the area was dropped.
                   if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button !== 0) return;
                   e.preventDefault(); close(); window.location.assign(projectHref(p.id, homeId));
                 }}>
                {p.name}
                {projectTypeBadge(p.type) ? <span className="bx-project-menu-type" data-testid={`project-menu-type-${p.id}`}>{projectTypeBadge(p.type)}</span> : null}
              </a>
            </li>
          ))}
        </ul>
        <GatedButton permission="project:create" className="bx-project-menu-new" title={NEW_PROJECT_TIP}
                     data-testid="new-project" onClick={() => { close(); setStep("choose"); }}>
          + New project
        </GatedButton>
        {active ? (isHome ? (
          <button type="button" className="bx-project-menu-new bx-project-menu-archive" aria-disabled="true"
                  title={HOME_TIP} data-testid="archive-project" onClick={(event) => event.preventDefault()}>
            Archive project…<span className="sr-only">{HOME_TIP}</span>
          </button>
        ) : (
          <GatedButton permission="project:delete" className="bx-project-menu-new bx-project-menu-archive"
                       title={ARCHIVE_TIP} data-testid="archive-project" onClick={() => { close(); setArchiving(true); }}>
            Archive project…
          </GatedButton>
        )) : null}
        <button type="button" className="bx-project-menu-new bx-project-menu-toggle" aria-expanded={showArchived}
                aria-controls="boss-archived-projects" data-testid="show-archived"
                title="Projects that were archived: hidden from this list, restorable"
                onClick={() => setShowArchived((open) => !open)}>
          {showArchived ? "Hide archived" : "Show archived"}
        </button>
        {showArchived ? (
          <div id="boss-archived-projects" data-testid="archived-projects">
            <ArchivedProjects orgId={orgId} onRestored={openProject} />
          </div>
        ) : null}
      </div>
    </details>
    {step === "form" ? (
      <NewProjectSheet draft={draft} onDraft={setDraft} onClose={closeStart} onBack={() => setStep("idea")}
                       onCreated={(id, type) => {
                         setDraft(EMPTY_DRAFT);
                         clearStepParam();
                         // A knowledge-graph project opens on its setup: import, team, first scan.
                         // An app opens on its page: "What should we build?", the description ready.
                         window.location.assign(type === "knowledge_graph" ? knowledgeSetupHref(id, homeId) : projectPageHref(id, homeId));
                       }} />
    ) : step ? (
      <StartProjectSheet step={step} onStep={setStep} onClose={closeStart} projectId={activeId} homeId={homeId} />
    ) : null}
    {archiving && active ? (
      <ArchiveDialog projectId={active.id} name={active.name} onClose={() => setArchiving(false)}
                     onArchived={goHome} />
    ) : null}
    </>
  );
}
