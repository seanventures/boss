"use client";

/**
 * WHERE THE KNOWLEDGE SCREENS WENT (KNOWLEDGE_GRAPH_PRODUCT_SPEC.md §5.1). Reliability's
 * knowledge explorer and settings moved to Graph, and Approvals' fact queue to Graph › Rules
 * review › Facts; each old place keeps one line that says so and goes there.
 */
import React from "react";

export function KnowledgeMovedCard({ to, orgId }: { to: "explorer" | "facts"; orgId?: string | null }) {
  const p = new URLSearchParams();
  if (orgId) p.set("org", orgId);
  p.set("area", "knowledge");
  if (to === "facts") { p.set("kg_tab", "rules"); p.set("kg_sub", "facts"); }
  const href = `/?${p.toString()}`;
  return (
    <p className="detail kg-moved" data-testid={`knowledge-moved-${to}`} role="note">
      {to === "facts"
        ? <>Knowledge facts waiting for a person are in <a href={href}>Graph › Rules review › Facts</a>, beside the rules agents propose.</>
        : <>Knowledge search, why, blast radius, indexing and knowledge settings are in <a href={href}>Graph</a>, on the project&apos;s code, rules and what agents learned.</>}
    </p>
  );
}
