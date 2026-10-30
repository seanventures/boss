// @vitest-environment happy-dom
import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { describe, it, expect, afterEach } from "vitest";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
import { PipelineView } from "../app/workflows/pipeline-view";

// Minimal spec resembling the merged kg_investigation_pipeline template:
// a for_each lane, a bounded_loop, a human gate step and a deterministic step.
const KG_PIPELINE_SPEC = {
  key: "kg_investigation_pipeline",
  version: 3,
  steps: [
    {
      id: "scan_lanes",
      kind: "for_each",
      lane: "lanes",
      body: "scan_one",
      agent: "scout",
      status: "done",
      reference: "run-report-1",
    },
    {
      id: "scan_one",
      kind: "custom_step",
      agent: "scout",
      status: "done",
      reference: "scan-record-1",
    },
    {
      id: "retry_loop",
      kind: "bounded_loop",
      body: "retry_once",
      max_iterations: 3,
      status: "running",
      reference: "loop-record-1",
    },
    {
      id: "human_gate",
      kind: "gate",
      status: "pending",
      reference: "gate-record-1",
    },
    {
      id: "finalize",
      kind: "custom_step",
      agent: "closer",
      status: "queued",
      reference: "closure-record-1",
    },
  ],
};

let container: HTMLDivElement | null = null;

function mount(spec: unknown) {
  container = document.createElement("div");
  document.body.appendChild(container);
  const root = createRoot(container);
  act(() => {
    root.render(<PipelineView spec={spec as any} />);
  });
  return { root, container };
}

afterEach(() => {
  if (container) {
    document.body.removeChild(container);
    container = null;
  }
});

describe("PipelineView", () => {
  it("P0 renders the kg template stages grouped by lane with the loop and gate visible", () => {
    const { container: el } = mount(KG_PIPELINE_SPEC);
    expect(el!.querySelector('[data-testid="stage-scan_lanes"]')).not.toBeNull();
    expect(el!.querySelector('[data-testid="lane-lanes"]')).not.toBeNull();
    expect(el!.querySelector('[data-testid="stage-retry_loop"]')).not.toBeNull();
    expect(el!.querySelector('[data-testid="loop-retry_loop"]')).not.toBeNull();
    expect(el!.querySelector('[data-testid="gate-human_gate"]')).not.toBeNull();
  });

  it("P1 lists every stage in the table with its agent, status and reference, and renders no raw IDs", () => {
    const { container: el } = mount(KG_PIPELINE_SPEC);
    const row = el!.querySelector('[data-testid="stage-row-finalize"]');
    expect(row).not.toBeNull();
    expect(row!.textContent).toContain("queued");
    expect(row!.textContent).toContain("closer");

    // No element anywhere should surface a raw UUID/opaque record id as visible text.
    const uuidLike = /\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b/i;
    const recordIdLike = /\b(scan|loop|gate|closure)-record-\d+\b/;
    const allText = el!.textContent || "";
    expect(uuidLike.test(allText)).toBe(false);
    expect(recordIdLike.test(allText)).toBe(false);
  });
});
