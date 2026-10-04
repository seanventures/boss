// @vitest-environment happy-dom

import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Spec } from "../lib/workflow-editor-types";
import { PALETTE } from "./_helpers/canvas-fixtures";
import { fakeWorkflowApi } from "./_helpers/fake-workflow-api";
import { standingOf } from "./_helpers/run-standing";

/**
 * A run is drawn from the definition version it ran, the way the designer draws it: the same
 * titles, the same agent on each step, the same columns. A run's own report has no titles and
 * no custom-step bindings, so drawing from it named "Write the PRD" by its capability, lost
 * "Trim the clip" to its id, and drew a parallel workflow as a straight chain.
 *
 * Mounted for real (RunsView) with only `fetch` stood in, next to the designer's lane drawn
 * from the same spec and palette.
 */

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const { RunsView } = await import("../app/delivery-workflows-ui");
const { FlowLane } = await import("../app/workflows/flow/flow-lane");
const { forgetRunDefinitions } = await import("../app/workflows/flow/run-lane");
const { nameOf } = await import("../app/agents/run-face");

/** Write the PRD (titled) → Trim the clip (your step) → parallel(review, test) → join → notify. */
const DEF: Spec = {
  entry_step: "prd",
  steps: [
    { id: "prd", kind: "capability", title: "Write the PRD", captain: "software_engineer",
      binding: { capability: "research" }, next: ["clip"] },
    { id: "clip", kind: "capability", binding: { custom_step: "trim_clip", inputs: {} }, next: ["fan"] },
    { id: "fan", kind: "parallel", branches: ["review", "test"], join: "gather" },
    { id: "review", kind: "capability", binding: { capability: "review" }, next: ["gather"] },
    { id: "test", kind: "capability", binding: { capability: "test" }, next: ["gather"] },
    { id: "gather", kind: "join", wait_for: ["review", "test"], next: ["notify"] },
    { id: "notify", kind: "capability", binding: { capability: "notify" } },
  ],
};

function step(id: string, kind: string, capability: string | null, status = "succeeded") {
  return { step_id: id, kind, status, capability, output: {}, evidence: [], error: null, approval: {},
           due_at: null, timeout_applied: null, attempt: 1, started_at: null, completed_at: null,
           decision_id: null };
}

// What a run reports: its steps in order, no titles, and only a capability for each.
const RUN = {
  id: "wfr_1", status: "running", standing: standingOf("running"), workflow_key: "my_flow",
  workflow_name: "My flow", version: 2, step_count: 7, steps_done: 2, current_step: "review",
  waiting_on: null, blocked_reason: null, error: null, updated_at: null, actions: [],
  input: { title: "Lead triage" },
  steps: [
    step("prd", "capability", "research"), step("clip", "capability", null),
    step("fan", "parallel", null, "running"), step("review", "capability", "review", "running"),
    step("test", "capability", "test", "running"), step("gather", "join", null, "pending"),
    step("notify", "capability", "notify", "pending"),
  ],
};

let root: Root | null = null;
let host: HTMLElement | null = null;
const realClientWidth = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "clientWidth");

beforeEach(() => {
  forgetRunDefinitions();
  Object.defineProperty(HTMLElement.prototype, "clientWidth", {
    configurable: true,
    get() { return (this as HTMLElement).classList?.contains("wf-lane-viewport") ? 4000 : 0; },
  });
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
});

afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  root = null; host = null;
  vi.unstubAllGlobals();
  if (realClientWidth) Object.defineProperty(HTMLElement.prototype, "clientWidth", realClientWidth);
  else delete (HTMLElement.prototype as { clientWidth?: number }).clientWidth;
});

async function settle(rounds = 8) {
  for (let i = 0; i < rounds; i += 1) await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
}

const noop = () => {};
async function mountRun() {
  act(() => root!.render(
    <div>
      <div data-testid="run">
        <RunsView runs={[RUN] as never} openRun={RUN.id} setOpenRun={noop} onAdvance={noop} busy={null}
                  capabilities={PALETTE.capabilities as never}
                  onRetryStep={noop} onSkipStep={noop} onRerun={noop} onStop={noop} onDelete={noop}
                  onStopAndRerun={noop} onBuildOnThis={noop} onRecheck={noop} onSetTarget={noop}
                  onPrepareTarget={noop} onRunPreview={noop} onBound={noop} projectId="prj_1"
                  actor="ceo" roles={[]} targets={[]} hosts={[]} />
      </div>
      <div data-testid="design">
        <FlowLane spec={DEF} palette={PALETTE} mode="design" />
      </div>
    </div>));
  await settle();
}

type Drawn = { title: string; agent: string | null; left: string; top: string };
function drawn(scope: string): Map<string, Drawn> {
  const out = new Map<string, Drawn>();
  for (const n of document.querySelectorAll<HTMLElement>(`[data-testid="${scope}"] .wf-lane .wf-node`)) {
    const slot = n.parentElement!;
    out.set(n.dataset.stepId!, {
      title: n.querySelector(".wf-node-title")!.textContent!,
      agent: n.querySelector(".wf-node-agent-name")?.textContent ?? null,
      left: slot.style.left, top: slot.style.top,
    });
  }
  return out;
}

describe("a run is drawn as its workflow is designed", () => {
  it("names, credits and places every step exactly as the designer does", async () => {
    const api = fakeWorkflowApi(PALETTE, [{ definition_id: "wfd_own", workflow_key: "my_flow", name: "My flow",
                                            is_catalogue: false, version: 3, spec: { steps: [] },
                                            versions: [{ version: 2, spec: DEF }, { version: 3, spec: { steps: [] } }] }]);
    const fetch = vi.fn(api.fetch);
    vi.stubGlobal("fetch", fetch);
    await mountRun();

    // The version the run ran, not the active one.
    expect(fetch.mock.calls.map(([u]) => String(u)).some((u) => u.includes("view=spec") && u.includes("version=2"))).toBe(true);
    const run = drawn("run");
    const design = drawn("design");
    expect([...run.keys()].sort()).toEqual([...design.keys()].sort());
    // The same steps, titles and places. A named agent reads by its callsign on a run — the
    // name the chain, the step panel and the node of a step it has run all use — and by its
    // role in the designer.
    const sameBut = (m: Map<string, Drawn>) => new Map([...m].map(([k, v]) => [k, { ...v, agent: null }]));
    expect(sameBut(run)).toEqual(sameBut(design));
    expect(run.get("prd")!.title).toBe("Write the PRD");
    expect(design.get("prd")!.agent).toBe("Software Engineer");
    expect(run.get("prd")!.agent).toBe(nameOf("software_engineer").name);
    expect(run.get("clip")!.title).toBe("Trim the clip");
    expect(run.get("clip")!.agent).toBe("Auto · QA Lead");
    // Parallel siblings share a column: the run is not drawn as a chain.
    expect(run.get("review")!.left).toBe(run.get("test")!.left);
    expect(run.get("review")!.top).not.toBe(run.get("test")!.top);
    expect(document.querySelector('[data-testid="run"] .wf-lane')!.getAttribute("data-linear")).toBe("false");
  });

  it("draws from the run itself while the definition cannot be read", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ error: "down" }, { status: 503 })));
    await mountRun();
    const run = drawn("run");
    expect(run.size).toBe(7);
    // One step after another, in the order the run reports them.
    const lefts = RUN.steps.map((s) => parseFloat(run.get(s.step_id)!.left));
    expect(lefts).toEqual([...lefts].sort((a, b) => a - b));
    // Still never the raw id.
    expect(run.get("notify")!.title).toBe("Notify");
  });
});
