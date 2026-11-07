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
import { TAB_WORDS, projectHref, pxHref, toTab } from "../lib/project-experience";
import { crumbTrail, onCrumbTrail, type CrumbTrail } from "./shell-crumb-here";

/** A template's lab project (template-sheet's naming): insecure by design, never deployed. */
export const LAB_NAME = /lab \(insecure by design\)$/;

export type Crumb = { key: string; label: string; kind: "link" | "project" | "here" | "text" | "href"; href?: string };

/** Observability is org-wide (screen 15): "Observability / Apps", its view in words. */
const OBS_VIEW_WORDS: Record<string, string> = {
  apps: "Apps", system: "BOSS platform", sources: "Alert sources", notify: "Contact points", alerts: "Alerts",
  models: "Models", scorecards: "Scorecards", installs: "Installs",
};

/** The crumbs for an area, as data (the component draws them; tests read them). */
export function crumbsFor({ active, areaLabel, routeMode, hasProject, tab, view = null, trail = null }: {
  active: string; areaLabel: string; routeMode: boolean; hasProject: boolean; tab: string | null; view?: string | null;
  /** An open org-wide thing (a server's drawer in Infra): "Infra / Servers / 203.0.113.20" (14-01). */
  trail?: CrumbTrail;
}): Crumb[] {
  if (trail && trail.area === active && !routeMode && trail.trail.length) {
    const last = trail.trail.length - 1;
    return [{ key: "area", label: areaLabel, kind: "href", href: `?area=${encodeURIComponent(active)}` },
            ...trail.trail.map((t, i): Crumb => (i === last ? { key: `t${i}`, label: t.label, kind: "here" }
              : { key: `t${i}`, label: t.label, kind: t.href ? "href" : "text", href: t.href }))];
  }
  if (active === "observability" && !routeMode) {
    return [{ key: "area", label: areaLabel, kind: "text" },
            { key: "view", label: OBS_VIEW_WORDS[view || tab || "apps"] ?? "Apps", kind: "here" }];
  }
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
function useUrlTab(key = "tab"): string | null {
  const [tab, setTab] = useState<string | null>(null);
  useEffect(() => {
    const read = () => { try { setTab(new URLSearchParams(window.location.search).get(key)); } catch { /* non-browser */ } };
    read();
    window.addEventListener("popstate", read);
    return () => window.removeEventListener("popstate", read);
  }, [key]);
  return tab;
}

export function ShellCrumbs({ active, areaLabel, routeMode, projects, projectId, onProjects }: {
  active: string; areaLabel: string; routeMode: boolean;
  projects?: { items: ProjectMenuItem[]; homeId: string }; projectId?: string;
  onProjects: () => void;
}) {
  const tab = useUrlTab();
  const view = useUrlTab("view");
  const hasProject = Boolean(projects && projectId);
  const [trail, setTrail] = useState<CrumbTrail>(null);
  useEffect(() => { setTrail(crumbTrail()); return onCrumbTrail(() => setTrail(crumbTrail())); }, []);
  const crumbs = crumbsFor({ active, areaLabel, routeMode, hasProject, tab, view, trail });
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
            ) : c.kind === "href" ? (
              <a className="bx-crumb-link" href={pxHref(c.href ?? "")} data-console-load data-testid={`crumb-${c.key}`}>{c.label}</a>
            ) : c.kind === "text" ? (
              <span className="bx-crumb-link" data-testid={`crumb-${c.key}`}>{c.label}</span>
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
