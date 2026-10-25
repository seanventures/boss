"use client";

import React, { useEffect, useRef, useState } from "react";

import { GatedButton } from "../components/gated";
import { ActionForm } from "./client-forms";
import { consoleHref } from "./console-href";
import { createProjectAction } from "./project-actions";
import { ArchiveDialog, ArchivedNotice, ArchivedProjects, useArchived } from "./project-archive";
import { SubmitButton } from "./submit-button";

export type ProjectMenuItem = { id: string; name: string };

/** Where a project opens: the org's own project is the console with no ?project_id=. */
export function projectHref(id: string, homeId: string): string {
  return consoleHref({ project_id: id === homeId ? undefined : id, task: undefined, run: undefined });
}

const NEW_PROJECT_TIP =
  "A new project in this organization: its own agents, skills, budget, settings and work, set up like the first one";
const ARCHIVE_TIP = "Hide this project and pause its schedules, monitors and indexing. Nothing is deleted";
const HOME_TIP = "The home project can't be archived: BOSS's own agent and the worker's schedules live there";

/**
 * THE PROJECT MENU, in the top bar beside the organization: which project is open, the org's
 * other projects, and New project (POST /v1/projects, `project:create`). Short labels, tooltips
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
      <summary title="Project · switch, create, or archive one">
        {active?.name ?? archived?.name ?? activeId}
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
                 data-testid={`project-menu-${p.id}`} data-console-load="" onClick={close}>{p.name}</a>
            </li>
          ))}
        </ul>
        {creating ? (
          <ActionForm action={createProjectAction} permission="project:create"
                      data-testid="new-project-form"
                      onResult={(r) => {
                        const id = r.ok ? r.data?.projectId : null;
                        if (typeof id === "string") window.location.assign(projectHref(id, homeId));
                      }}>
            <label>Name<input name="name" required maxLength={120} autoFocus /></label>
            <label>Description<input name="description" maxLength={2000} placeholder="Optional" /></label>
            <div className="bx-project-menu-actions">
              <SubmitButton type="submit" pendingLabel="Creating…" title="Create the project and open it">Create</SubmitButton>
              <button type="button" onClick={() => setCreating(false)}>Cancel</button>
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
