"use client";

/**
 * "Moved to Project › Settings" (contract §11, screen 13's old-home bar, 13-30): the one line an
 * old home of a project setting shows — a gear, **Moved to Project › Settings**, and a primary
 * `Open →` that lands on the right section of the project's Settings tab. The old control stays
 * beside it and keeps working (README parity table, 13-31).
 */
import React from "react";
import { ArrowRight, GearSix } from "@phosphor-icons/react";

import { consoleHref } from "../console-href";

export function MovedToProjectSettings({ section, testid = "moved-to-project-settings", projectId }: {
  section: string; testid?: string; projectId?: string | null;
}) {
  const href = consoleHref({ area: "project", tab: "settings", section, app: undefined, build: undefined,
                             ...(projectId ? { project_id: projectId } : {}) });
  return (
    <a className="px-moved" href={href} data-testid={testid} title="This setting lives on the project's page now"
       style={{ display: "flex", alignItems: "center", gap: 8, padding: "8px 12px", margin: "6px 0", borderRadius: 10,
                background: "var(--accent-soft)", border: "1px solid var(--accent-line)", color: "var(--ink)",
                textDecoration: "none", fontSize: "0.8rem" }}>
      <GearSix size={14} aria-hidden />
      <b style={{ fontWeight: 650 }}>Moved to Project › Settings</b>
      <span style={{ flex: 1 }} />
      <span style={{ display: "inline-flex", alignItems: "center", gap: 5, padding: "4px 10px", borderRadius: 8,
                     background: "var(--accent)", color: "#fff", fontWeight: 600, fontSize: "0.75rem" }}>
        Open <ArrowRight size={11} aria-hidden /></span>
    </a>
  );
}

/**
 * WORKFLOWS › NEW BUILD HAS MOVED (Add a project v2, c-after-create.html note 3): a new app is
 * Add a project (one screen), adding a feature is Project › New work (it builds onto the app's
 * branch). Shown above the old composer for one release; the composer keeps working beneath it.
 */
export function MovedNewBuild({ projectId, testid = "moved-new-build" }: { projectId?: string | null; testid?: string }) {
  const newApp = consoleHref({ new_project: "idea" });
  const newWork = consoleHref({ area: "project", tab: "work", compose: "1", app: undefined, build: undefined, wfr: undefined,
                                ...(projectId ? { project_id: projectId } : {}) });
  const link = { display: "inline-flex", alignItems: "center", gap: 5, padding: "4px 10px", borderRadius: 8,
                 fontWeight: 600, fontSize: "0.75rem", textDecoration: "none" } as const;
  return (
    <div className="px-moved" role="status" data-testid={testid}
         style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap", padding: "10px 14px", margin: "6px 0 12px", borderRadius: 12,
                  background: "var(--accent-soft)", border: "1px solid var(--accent-line)", color: "var(--ink)", fontSize: "0.8rem" }}>
      <ArrowRight size={14} aria-hidden />
      <b style={{ fontWeight: 650 }}>New build has moved</b>
      <span style={{ color: "var(--muted)" }}>A new app is one screen now; a feature is New work on its project. This page goes next release.</span>
      <span style={{ flex: 1 }} />
      <a href={newApp} data-testid={`${testid}-new-app`} style={{ ...link, background: "var(--accent)", color: "#fff" }}>New app → Add a project</a>
      <a href={newWork} data-testid={`${testid}-new-work`} style={{ ...link, border: "1px solid var(--accent-line)", color: "var(--accent-text)" }}>Add a feature → New work</a>
    </div>
  );
}
