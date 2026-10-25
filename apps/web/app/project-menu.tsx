"use client";

import React, { useEffect, useRef, useState } from "react";

import { GatedButton } from "../components/gated";
import { ActionForm } from "./client-forms";
import { consoleHref } from "./console-href";
import { createProjectAction } from "./project-actions";
import { ArchiveDialog, ArchivedNotice, ArchivedProjects, useArchived } from "./project-archive";
import type { ProjectType } from "@boss/api-client";
import { SubmitButton } from "./submit-button";

export type ProjectMenuItem = { id: string; name: string; type?: ProjectType };

/** Where a project opens: the org's own project is the console with no ?project_id=. */
export function projectHref(id: string, homeId: string): string {
  return consoleHref({ project_id: id === homeId ? undefined : id, task: undefined, run: undefined });
}

/** Where a new knowledge-graph project opens: its Knowledge Graph setup (kg-setup.tsx). */
export function knowledgeSetupHref(id: string, homeId: string): string {
  return consoleHref({ project_id: id === homeId ? undefined : id, area: "knowledge", kg_tab: "setup",
                       task: undefined, run: undefined });
}

/** The two kinds of project, said in one line each (POST /v1/projects `project_type`). */
export const PROJECT_TYPES: { key: ProjectType; label: string; line: string }[] = [
  { key: "app", label: "Build an app",
    line: "Agents plan, build, test and ship an app through gated pull requests." },
  { key: "knowledge_graph", label: "Knowledge Graph for an existing codebase",
    line: "Import a repository, map its code, and have agents find its business rules and learn as they work." },
];

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
  const [creating, setCreating] = useState(false);
  const [newType, setNewType] = useState<ProjectType>("app");
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
  const [isOpen, setIsOpen] = useState(false);
  const close = () => { if (menu.current) menu.current.open = false; setIsOpen(false); };
  useEffect(() => {
    if (!isOpen) return undefined;
    const outside = (e: PointerEvent) => { if (menu.current && !menu.current.contains(e.target as Node)) close(); };
    const escape = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      close();
      menu.current?.querySelector<HTMLElement>("summary")?.focus();
    };
    document.addEventListener("pointerdown", outside);
    document.addEventListener("keydown", escape);
    return () => { document.removeEventListener("pointerdown", outside); document.removeEventListener("keydown", escape); };
  }, [isOpen]);
  return (
    <>
    <details ref={menu} className="bx-project-menu" data-testid="project-menu"
             onToggle={(e) => setIsOpen((e.currentTarget as HTMLDetailsElement).open)}>
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
                 data-testid={`project-menu-${p.id}`} data-console-load="" onClick={close}>
                {p.name}
                {projectTypeBadge(p.type) ? <span className="bx-project-menu-type" data-testid={`project-menu-type-${p.id}`}>{projectTypeBadge(p.type)}</span> : null}
              </a>
            </li>
          ))}
        </ul>
        {creating ? (
          <ActionForm action={createProjectAction} permission="project:create"
                      data-testid="new-project-form"
                      onResult={(r) => {
                        const id = r.ok ? r.data?.projectId : null;
                        if (typeof id !== "string") return;
                        // A knowledge-graph project opens on its setup: import, team, first scan.
                        window.location.assign(r.data?.projectType === "knowledge_graph"
                          ? knowledgeSetupHref(id, homeId) : projectHref(id, homeId));
                      }}>
            <fieldset className="bx-project-type" data-testid="new-project-type">
              <legend>What is it for?</legend>
              {PROJECT_TYPES.map((t) => (
                <label key={t.key} className="bx-project-type-option" data-checked={newType === t.key || undefined}
                       data-testid={`new-project-type-${t.key}`}>
                  <input type="radio" name="project_type" value={t.key} checked={newType === t.key}
                         onChange={() => setNewType(t.key)} />
                  <span className="bx-project-type-text">
                    <b>{t.label}</b>
                    <small>{t.line}</small>
                  </span>
                </label>
              ))}
            </fieldset>
            <label>Name<input name="name" required maxLength={120} autoFocus
                              placeholder={newType === "knowledge_graph" ? "e.g. Fineract knowledge graph" : undefined} /></label>
            <label>Description<input name="description" maxLength={2000} placeholder="Optional" /></label>
            <div className="bx-project-menu-actions">
              <SubmitButton type="submit" pendingLabel="Creating…"
                            title={newType === "knowledge_graph" ? "Create the project and open its setup: import the code, assign the team, a first scan" : "Create the project and open it"}>
                {newType === "knowledge_graph" ? "Create and set up" : "Create"}
              </SubmitButton>
              <button type="button" onClick={() => { setCreating(false); setNewType("app"); }}>Cancel</button>
            </div>
          </ActionForm>
        ) : (
          <GatedButton permission="project:create" className="bx-project-menu-new" title={NEW_PROJECT_TIP}
                       data-testid="new-project" onClick={() => setCreating(true)}>
            + New project
          </GatedButton>
        )}
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
    {archiving && active ? (
      <ArchiveDialog projectId={active.id} name={active.name} onClose={() => setArchiving(false)}
                     onArchived={goHome} />
    ) : null}
    </>
  );
}
