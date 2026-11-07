"use client";

/**
 * THE TOP BAR'S BREADCRUMB (BOSSAPP-914 screen 1, wireframes 01/02/04): where you are, said once.
 *
 *   Projects                                 on the Projects list
 *   Projects / HelpDesk AI Demo ▾ / Work     on a project's page (no tab crumb on Overview)
 *   HelpDesk AI Demo ▾ / Code Tasks          on any other area: the project it is scoped to
 *
 * Every crumb but the last is a muted link: Projects opens the list, the project crumb opens that
 * project's page. Switching projects is the rail's list (wireframe 01-19: no second project menu in
 * the bar); New project, archive and show archived live in the avatar menu. So the old "Project: X
 * ← Back to Y" banner over every area has nothing left to say. A template's lab project carries a
 * Lab chip that says it is insecure by design and never deployed.
 */
import React, { useEffect, useState } from "react";

import type { ProjectMenuItem } from "./project-menu";
import { TAB_WORDS, projectHref, toTab } from "../lib/project-experience";

/** A template's lab project (template-sheet's naming): insecure by design, never deployed. */
export const LAB_NAME = /lab \(insecure by design\)$/;

export type Crumb = { key: string; label: string; kind: "link" | "project" | "here" };

/** The crumbs for an area, as data (the component draws them; tests read them). */
export function crumbsFor({ active, areaLabel, routeMode, hasProject, tab }: {
  active: string; areaLabel: string; routeMode: boolean; hasProject: boolean; tab: string | null;
}): Crumb[] {
  if (routeMode || !hasProject) return [{ key: "area", label: areaLabel, kind: "here" }];
  if (active === "projects") return [{ key: "area", label: "Projects", kind: "here" }];
  if (active === "project") {
    // Screen 10's own page: Projects / HelpDesk AI Demo / Onboarding.
    if (tab === "onboarding") {
      return [{ key: "projects", label: "Projects", kind: "link" }, { key: "project", label: "", kind: "project" },
              { key: "tab", label: "Onboarding", kind: "here" }];
    }
    const t = toTab(tab);
    return [
      { key: "projects", label: "Projects", kind: "link" },
      { key: "project", label: "", kind: t === "overview" ? "here" : "project" },
      ...(t === "overview" ? [] : [{ key: "tab", label: TAB_WORDS[t], kind: "here" as const }]),
    ];
  }
  return [{ key: "project", label: "", kind: "project" }, { key: "area", label: areaLabel, kind: "here" }];
}

/** The project page's tab, from the URL, kept current as the page changes it (pushState + popstate). */
function useUrlTab(): string | null {
  const [tab, setTab] = useState<string | null>(null);
  useEffect(() => {
    const read = () => { try { setTab(new URLSearchParams(window.location.search).get("tab")); } catch { /* non-browser */ } };
    read();
    window.addEventListener("popstate", read);
    return () => window.removeEventListener("popstate", read);
  }, []);
  return tab;
}

export function ShellCrumbs({ active, areaLabel, routeMode, projects, projectId, onProjects }: {
  active: string; areaLabel: string; routeMode: boolean;
  projects?: { items: ProjectMenuItem[]; homeId: string }; projectId?: string;
  onProjects: () => void;
}) {
  const tab = useUrlTab();
  const hasProject = Boolean(projects && projectId);
  const crumbs = crumbsFor({ active, areaLabel, routeMode, hasProject, tab });
  const name = projects?.items.find((p) => p.id === projectId)?.name ?? projectId ?? "";
  const lab = LAB_NAME.test(name);
  return (
    <nav className="bx-crumbs" aria-label="Breadcrumb" data-testid="topbar-crumbs">
      <ol>
        {crumbs.map((c, i) => (
          <li key={c.key} className="bx-crumb" data-crumb={c.key} data-kind={c.kind}>
            {i ? <span className="bx-crumb-sep" aria-hidden>/</span> : null}
            {c.key === "project" && projects && projectId ? (
              <span className="bx-crumb-project" data-here={c.kind === "here" || undefined}>
                {c.kind === "here"
                  ? <span className="bx-crumb-here" aria-current="page" data-testid="crumb-project" title={name}>{name}</span>
                  : <a className="bx-crumb-link" href={projectHref(projectId)} data-console-load
                       data-testid="crumb-project" title={`${name} — open the project`}>{name}</a>}
                {lab ? (
                  <span className="bx-crumb-lab" data-testid="crumb-lab"
                        title="Lab project · insecure by design, never deployed to a host">Lab</span>
                ) : null}
              </span>
            ) : c.kind === "link" ? (
              <button type="button" className="bx-crumb-link" data-testid={`crumb-${c.key}`} onClick={onProjects}>{c.label}</button>
            ) : (
              <span className="bx-crumb-here" aria-current="page" data-testid={`crumb-${c.key}`}>{c.label}</span>
            )}
          </li>
        ))}
      </ol>
    </nav>
  );
}
