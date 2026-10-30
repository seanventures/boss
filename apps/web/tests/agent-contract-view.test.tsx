// @vitest-environment happy-dom
import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { describe, it, expect, afterEach } from "vitest";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
import { AgentContractView } from "../app/workflows/editor/agent-contract-view";

// A step's CONTRACT: what it reads from/writes to the Knowledge Graph and Context Graph,
// plus an MCP call. Mirrors the "Grey = reads, blue = writes, dotted = MCP calls" spec.
const STEP = {
  id: "scan_one",
  agent: "scout",
  contract: {
    reads: [{ id: "kg_node_1", label: "Known incidents" }],
    writes: [{ id: "cg_node_1", label: "Scan findings" }],
    calls: [{ id: "mcp_server_1", label: "Enterprise gateway" }],
  },
};

// What the last real run actually did, from the Context Graph's consulted/produced
// edges with provenance: it never wrote "Scan findings" but it did read an undeclared node.
const REAL_RUN = {
  runId: "run-42",
  reads: [
    { id: "kg_node_1", label: "Known incidents" },
    { id: "kg_node_2", label: "Undeclared lookup" }, // real-only read, not in contract
  ],
  writes: [], // contract-only write never happened
};

let container: HTMLDivElement | null = null;

function mount(props: Record<string, unknown>) {
  container = document.createElement("div");
  document.body.appendChild(container);
  const root = createRoot(container);
  act(() => {
    root.render(<AgentContractView {...(props as any)} />);
  });
  return { root, container };
}

afterEach(() => {
  if (container) {
    document.body.removeChild(container);
    container = null;
  }
});

describe("AgentContractView", () => {
  it("P1 shows read, write and call lines for the selected stage, from its contract", () => {
    const { container: el } = mount({ step: STEP });
    expect(el!.querySelector('[data-testid="kg-band"]')).not.toBeNull();
    expect(el!.querySelector('[data-testid="context-band"]')).not.toBeNull();
    expect(el!.querySelector('[data-testid="mcp-band"]')).not.toBeNull();
    expect(el!.querySelector('[data-testid="line-read-kg_node_1"]')).not.toBeNull();
    expect(el!.querySelector('[data-testid="line-write-cg_node_1"]')).not.toBeNull();
    expect(el!.querySelector('[data-testid="line-call-mcp_server_1"]')).not.toBeNull();
  });

  it("P1 highlights only the lines for the clicked node, leaving the rest unhighlighted", () => {
    const { container: el } = mount({ step: STEP });
    const readLine = el!.querySelector('[data-testid="line-read-kg_node_1"]') as HTMLElement;
    const writeLine = el!.querySelector('[data-testid="line-write-cg_node_1"]') as HTMLElement;
    act(() => {
      readLine.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });
    expect(readLine.getAttribute("data-highlighted")).toBe("true");
    expect(writeLine.getAttribute("data-highlighted")).not.toBe("true");
  });

  it("P1 overlays a real run so a contract write that never happened and an undeclared real read are both visible", () => {
    const { container: el } = mount({ step: STEP, realRun: REAL_RUN });
    const contractOnlyWrite = el!.querySelector('[data-testid="line-write-cg_node_1"]');
    const realOnlyRead = el!.querySelector('[data-testid="line-read-kg_node_2"]');
    expect(contractOnlyWrite).not.toBeNull();
    expect(contractOnlyWrite!.getAttribute("data-overlay")).toBe("contract-only");
    expect(realOnlyRead).not.toBeNull();
    expect(realOnlyRead!.getAttribute("data-overlay")).toBe("real-only");
  });
});
