"use client";

import { useState } from "react";

import { GatedButton } from "../components/gated";
import { ActionForm } from "./client-forms";
import { consoleHref } from "./console-href";
import { createProjectAction } from "./project-actions";
import { SubmitButton } from "./submit-button";

export type ProjectMenuItem = { id: string; name: string };

/** Where a project opens: the org's own project is the console with no ?project_id=. */
export function projectHref(id: string, homeId: string): string {
  return consoleHref({ project_id: id === homeId ? undefined : id, task: undefined, run: undefined });
}

const NEW_PROJECT_TIP =
  "A new project in this organization: its own agents, skills, budget, settings and work, set up like the first one";

/**
 * THE PROJECT MENU, in the top bar beside the organization: which project is open, the org's
 * other projects, and New project (POST /v1/projects, `project:create`). Short labels, tooltips
 * for the rest. The API decides the permission and its step-up; the button only mirrors it.
 */
export function ProjectMenu({ projects, activeId, homeId }: {
  projects: ProjectMenuItem[];
  activeId: string;
  homeId: string;
}) {
  const [creating, setCreating] = useState(false);
  const active = projects.find((p) => p.id === activeId);
  return (
    <details className="bx-project-menu" data-testid="project-menu">
      <summary title="Project · switch, or create one">{active?.name ?? activeId}</summary>
      <div className="bx-project-menu-panel">
        <ul aria-label="Projects">
          {projects.map((p) => (
            <li key={p.id}>
              <a href={projectHref(p.id, homeId)} aria-current={p.id === activeId ? "page" : undefined}
                 data-testid={`project-menu-${p.id}`}>{p.name}</a>
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
      </div>
    </details>
  );
}
