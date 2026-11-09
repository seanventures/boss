// @vitest-environment happy-dom
/**
 * Applied learning (design §12.4, §11.0 "a diagram, not text"): when the incident squad reuses a
 * lesson from a past incident, the flow chart draws it as its own node ("INC-1 lesson") with an
 * arrow into the decision that applied it; the whole sentence ("Applied learning: … from INC-1")
 * is the node's tooltip, never body text.
 */
import React from "react";
import { afterEach, describe, expect, it } from "vitest";

import { parseEdge, parseNode } from "../../lib/ambient";
import { $$, mountAs, settle, unmount } from "../_helpers/multirepo-fixtures";

const { FlowCanvas, TYPE_WORDS } = await import("../../app/agent-ops/flow-canvas");

afterEach(() => unmount());

const wire = {
  nodes: [
    { id: "signal:s1", type: "signal", label: "5xx on POST /orders", stage: 0, badges: ["high"] },
    { id: "party:agent:sre", type: "agent", label: "SRE lead", stage: 1, badges: [] },
    { id: "learning:kgn_1", type: "learning", label: "INC-1 lesson", stage: 2, badges: ["applied"],
      ref: { kind: "kg_node", id: "kgn_1" },
      detail: "Applied learning: order-api: bulk discount broke totals from INC-1" },
    { id: "decision:a1", type: "decision", label: "Reuse INC-1 fix", stage: 3, badges: ["A2"] },
    { id: "outcome", type: "outcome", label: "Recovered 4m", stage: 5, badges: ["resolved"] },
  ],
  edges: [
    { from: "signal:s1", to: "party:agent:sre", kind: "raised", label: "raised" },
    { from: "party:agent:sre", to: "decision:a1", kind: "decided", label: "decided" },
    { from: "learning:kgn_1", to: "decision:a1", kind: "applied", label: "applied" },
    { from: "decision:a1", to: "outcome", kind: "resulted", label: "resulted" },
  ],
};

describe("An applied lesson in the flow chart", () => {
  it("parses the learning node and keeps its sentence", () => {
    const n = parseNode(wire.nodes[2]);
    expect(n.type).toBe("learning");
    expect(n.detail).toContain("from INC-1");
    expect(TYPE_WORDS.learning).toBe("Applied learning");
  });

  it("draws it as a node with an arrow into the decision, the sentence as its tooltip", async () => {
    const nodes = wire.nodes.map(parseNode);
    const edges = wire.edges.map(parseEdge);
    await mountAs(<FlowCanvas nodes={nodes} edges={edges} lifecycle="resolved" onSelect={() => undefined} />);
    await settle();
    const node = $$('[data-testid="aa-node"]').find((b) => b.dataset.type === "learning")!;
    expect(node).toBeTruthy();
    expect(node.textContent).toContain("INC-1 lesson");
    expect(node.getAttribute("title")).toContain("Applied learning:");
    expect(node.getAttribute("aria-label")).toBe("Applied learning: INC-1 lesson");
  });
});
