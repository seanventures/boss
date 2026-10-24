"use client";

/**
 * What is left of the old knowledge console. The explorer (search, why, blast radius) and the
 * knowledge settings moved to the Graph area (app/knowledge/: graph-view.tsx, node-panel.tsx,
 * kg-settings.tsx; KNOWLEDGE_GRAPH_PRODUCT_SPEC.md §5.1, parity in app/knowledge/parity.ts).
 * `BasedOn` stays here: the decision inbox names the facts a decision used with it.
 */
import React from "react";

export function BasedOn({ factIds }: { factIds: string[] }) {
  if (factIds.length === 0) return null;
  return (
    <p className="detail" data-testid="based-on">
      Based on {factIds.join(", ")}
    </p>
  );
}
