"use client";

/**
 * WHERE THE KNOWLEDGE SCREENS WENT (KNOWLEDGE_GRAPH_PRODUCT_SPEC.md §5.1). Reliability's
 * knowledge explorer and settings moved to Graph, and Approvals' fact queue to Graph › Rules
 * review › Facts; each old place keeps one card that says so and goes there, in the same org
 * AND the same project (tester round 1 P1-2: the link dropped ?project_id= and opened the home
 * project's graph).
 */
import React, { useEffect, useState } from "react";
import { Graph } from "@phosphor-icons/react";

import { consoleHref } from "../console-href";
import "./kg.css";

function target(to: "explorer" | "facts"): Record<string, string | undefined> {
  return to === "facts" ? { area: "knowledge", kg_tab: "rules", kg_sub: "facts" } : { area: "knowledge" };
}

export function KnowledgeMovedCard({ to, orgId, projectId }: { to: "explorer" | "facts"; orgId?: string | null; projectId?: string | null }) {
  // First render (the server's): the org and project it was given. In the browser: the current
  // URL's own scope, whatever else it carries, through the one in-console link builder.
  const initial = (() => {
    const p = new URLSearchParams();
    if (orgId) p.set("org", orgId);
    if (projectId) p.set("project_id", projectId);
    for (const [k, v] of Object.entries(target(to))) if (v) p.set(k, v);
    return `/?${p.toString()}`;
  })();
  const [href, setHref] = useState(initial);
  useEffect(() => {
    const here = new URLSearchParams(window.location.search);
    const keep = new URLSearchParams();
    for (const k of ["org", "org_id", "project_id"]) { const v = here.get(k); if (v) keep.set(k, v); }
    if (orgId && !keep.get("org")) keep.set("org", orgId);
    if (projectId && !keep.get("project_id")) keep.set("project_id", projectId);
    setHref(`/${consoleHref(target(to), `?${keep.toString()}`)}`);
  }, [to, orgId, projectId]);
  const facts = to === "facts";
  return (
    <aside className="kg-moved" data-testid={`knowledge-moved-${to}`} role="note">
      <Graph size={18} weight="duotone" className="kg-moved-icon" aria-hidden />
      <span className="kg-moved-text">
        <strong>{facts ? "Knowledge facts moved to Graph" : "Knowledge search moved to Graph"}</strong>
        <span>{facts
          ? "Facts agents inferred are reviewed in Rules review › Facts, beside the rules they propose."
          : "Search, why, blast radius, indexing and the knowledge settings live with the project's code, rules and what agents learned."}</span>
      </span>
      <a className="ag-btn" href={href}>{facts ? "Open Facts" : "Open Graph"}</a>
    </aside>
  );
}
