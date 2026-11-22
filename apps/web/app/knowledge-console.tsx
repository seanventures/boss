"use client";

/**
 * What is left of the old knowledge console. The explorer (search, why, blast radius) and the
 * knowledge settings moved to the Graph area (app/knowledge/: graph-view.tsx, node-panel.tsx,
 * kg-settings.tsx; KNOWLEDGE_GRAPH_PRODUCT_SPEC.md §5.1, parity in app/knowledge/parity.ts).
 * `BasedOn` stays here: the decision inbox names the facts a decision used with it.
 */
import React from "react";

/** The facts a decision used, as ONE count chip ("Based on 14 facts"): the ids are graph
 *  plumbing (`kgn_…`), so they sit in the tooltip, never inline in the card's type
 *  (validation-review SPEC §0 #4, §1.5). */
export function BasedOn({ factIds }: { factIds: string[] }) {
  if (factIds.length === 0) return null;
  const n = factIds.length;
  return (
    <span className="dq-ev" data-testid="based-on" title={factIds.join("\n")}>
      Based on {n} fact{n === 1 ? "" : "s"}
    </span>
  );
}
