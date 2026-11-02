// @vitest-environment happy-dom

import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { readFileSync } from "node:fs";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Spec } from "../lib/workflow-editor-types";
import { layoutFlow } from "../lib/workflow-graph";
import { linear, PALETTE } from "./_helpers/canvas-fixtures";
import { fakeWorkflowApi } from "./_helpers/fake-workflow-api";
import { standingOf } from "./_helpers/run-standing";

/**
 * FULL PAGE (operator, 2026-11-01): "for big workflows like this full view will be helpful… I
 * can't really view the whole thing", and "why we have white boxes?" on the pipeline view.
 *
 *   - one URL per workflow (`wf=<key>&page=full`) and per run (`wfr=<id>&page=full`), read by the
 *     console shell to step its chrome aside;
 *   - the run's full page: its own head with the way back, the lane at the page's width with
 *     Fit and 100%, the step below; the run row offers "Open full page";
 *   - a loop over several steps is drawn as their chain, and steps off the flow (shared services)
 *     go along the bottom, so a 21-step run no longer starts with a tower in its first column;
 *   - every stage card is the console's dark card: design state is an accent, never an inverted
 *     (white) card.
 */

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const { readFullPage, fullPageHref } = await import("../app/workflows/full-page");
const { RunsView } = await import("../app/delivery-workflows-ui");
const { WorkflowEditor } = await import("../app/workflows/editor/workflow-editor");
const { forgetRunDefinitions } = await import("../app/workflows/flow/run-lane");

describe("the full page has one URL per workflow and per run", () => {
  it("reads only this area's URLs that name something to show", () => {
    expect(readFullPage("?area=delivery_workflows&wf=dq_incident_rca&page=full"))
      .toEqual({ workflow: "dq_incident_rca", run: null });
    expect(readFullPage("?area=delivery_workflows&wfr=wfr_1&page=full")).toEqual({ workflow: null, run: "wfr_1" });
    expect(readFullPage("?area=delivery_workflows&wf=dq_incident_rca")).toBeNull();
    expect(readFullPage("?area=delivery_workflows&page=full")).toBeNull();
    expect(readFullPage("?area=budget&wf=x&page=full")).toBeNull();
  });

  it("links keep the org and project and drop the inline view's own params", () => {
    const from = "?org=org_boss&area=delivery_workflows&project_id=np&tab=library&wf=old&view=json&step=s1";
    const wf = new URLSearchParams(fullPageHref(from, { wf: "dq_incident_rca" }));
    expect(Object.fromEntries(wf)).toEqual({ org: "org_boss", area: "delivery_workflows", project_id: "np",
                                             wf: "dq_incident_rca", page: "full" });
    const run = new URLSearchParams(fullPageHref(from, { wfr: "wfr_9" }));
    expect(run.get("wfr")).toBe("wfr_9");
    expect(run.get("wf")).toBeNull();
    expect(run.get("tab")).toBe("runs");
    expect(run.get("page")).toBe("full");
  });

  it("the shell steps its sidebar, top bar and strip aside on a full page", () => {
    const shell = readFileSync("app/ConsoleShell.tsx", "utf8");
    expect(shell).toMatch(/data-page=\{fullPage \? "full" : undefined\}/);
    const css = readFileSync("app/workflows/full-page.css", "utf8");
    for (const hidden of [".bx-sidebar", ".bx-topbar"]) {
      expect(css).toMatch(new RegExp(`\\.bx-shell\\[data-page="full"\\][^{]*\\${hidden}[^{]*\\{[^}]*display: none`));
    }
    expect(css).toMatch(/\.bx-shell\[data-page="full"\] \.bx-content \{[^}]*max-width: none/);
  });
});

describe("a big workflow is laid out left to right, not as a tower", () => {
  // The DQ RCA shape: a loop over three stages, a for-each, and two shared services nothing
  // leads to.
  const DQ: Spec = {
    entry_step: "monitoring",
    steps: [
      { id: "monitoring", kind: "capability", binding: { capability: "research" }, next: ["loop"] },
      { id: "loop", kind: "bounded_loop", body: ["enrich", "history", "rca"], max_iterations: 3, next: ["orchestrate"] },
      { id: "enrich", kind: "capability", binding: { capability: "research" }, next: [] },
      { id: "history", kind: "capability", binding: { capability: "research" }, next: [] },
      { id: "rca", kind: "capability", binding: { capability: "research" }, next: [] },
      { id: "orchestrate", kind: "capability", binding: { capability: "research" }, next: ["close"] },
      { id: "close", kind: "capability", binding: { capability: "research" }, next: [] },
      { id: "curator", kind: "deterministic", binding: {}, next: [] },
      { id: "profile", kind: "deterministic", binding: {}, next: [] },
    ],
  } as unknown as Spec;

  it("a loop over several steps is their chain: in at the first, through them in order", () => {
    const layout = layoutFlow(DQ);
    const edges = layout.edges.map((e) => `${e.kind}:${e.from}>${e.to}`);
    expect(edges).toEqual(expect.arrayContaining(["loop:loop>enrich", "next:enrich>history", "next:history>rca"]));
    const col = new Map(layout.nodes.map((n) => [n.id, n.column]));
    expect(col.get("enrich")).toBeGreaterThan(col.get("loop")!);
    expect(col.get("history")).toBeGreaterThan(col.get("enrich")!);
    expect(col.get("rca")).toBeGreaterThan(col.get("history")!);
    // A loop's steps return to it: none of them is where the flow ends.
    expect(layout.terminal).toEqual(["close"]);
  });

  it("steps off the flow go along the bottom, never stacked under the entry", () => {
    const layout = layoutFlow(DQ);
    const first = layout.nodes.filter((n) => n.column === 0).map((n) => n.id);
    expect(first).toEqual(["monitoring"]);
    for (const id of ["curator", "profile"]) {
      const n = layout.nodes.find((x) => x.id === id)!;
      expect(n.column).toBeGreaterThan(0);
      expect(n.row).toBe(n.rowsInColumn - 1);
    }
    // The lane is wide, not tall: before, the first column alone held six of these nine.
    expect(layout.maxRows).toBeLessThanOrEqual(3);
  });
});

describe("the pipeline's stage cards are the console's dark card", () => {
  const css = readFileSync("app/workflows/pipeline.css", "utf8");
  const STATES = ["locked", "in_review", "not_reviewed", "human_step", "shared_service"];

  it("no state inverts the card (the white box on the dark console)", () => {
    const cardRules = [...css.matchAll(/\.pl-card\[data-design="[a-z_]+"\][^{]*\{([^}]*)\}/g)].map((m) => m[1]);
    expect(cardRules.length).toBeGreaterThan(0);
    for (const body of cardRules) {
      expect(body).not.toMatch(/background:\s*var\(--ink\)/);
      expect(body).not.toMatch(/background:\s*(#fff|white|var\(--surface\))\b/i);
    }
    expect(css).toMatch(/\.pl-card\[data-design\] \{[^}]*background: var\(--surface-3\)[^}]*border-left: 4px solid var\(--pl-state\)/);
  });

  it("each state has its own accent token, shared by the card, the badge and the legend", () => {
    const accent = (state: string) => css.match(new RegExp(
      `:is\\(\\.pl-card, \\.pl-swatch, \\.pl-design\\)\\[data-design="${state}"\\] \\{ --pl-state: (var\\(--[a-z-]+\\))`))?.[1];
    expect(accent("locked")).toBe("var(--green)");
    expect(accent("in_review")).toBe("var(--tier-balanced)");
    expect(accent("human_step")).toBe("var(--amber)");
    expect(accent("not_reviewed")).toBe("var(--faint)");
    expect(new Set(STATES.map(accent)).size).toBeGreaterThanOrEqual(4);
    // The legend draws a swatch for every state, from the same token.
    const view = readFileSync("app/workflows/pipeline-view.tsx", "utf8");
    for (const s of STATES) expect(view).toContain(`<i className="pl-swatch" data-design="${s}" />`);
    expect(css).toMatch(/\.pl-swatch \{[^}]*border-left: 4px solid var\(--pl-state\)/);
  });
});

/* ── Mounted ── */

function step(id: string, kind: string, capability: string | null, status = "succeeded") {
  return { step_id: id, kind, status, capability, output: {}, evidence: [], error: null, approval: {},
           due_at: null, timeout_applied: null, attempt: 1, started_at: null, completed_at: null,
           decision_id: null };
}
const DEF = linear(6);
const RUN = {
  id: "wfr_big", status: "completed", standing: standingOf("completed"), workflow_key: "my_flow",
  workflow_name: "My flow", version: 2, step_count: 6, steps_done: 6, current_step: null,
  waiting_on: null, blocked_reason: null, error: null, updated_at: null, actions: [], finished: true,
  input: { title: "Investigate DQX-0588" }, created_at: "2026-11-01T23:57:00Z",
  steps: DEF.steps.map((s) => step(s.id, "capability", "research")),
};

let root: Root | null = null;
let host: HTMLElement | null = null;
const realClientWidth = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "clientWidth");

beforeEach(() => {
  forgetRunDefinitions();
  Object.defineProperty(HTMLElement.prototype, "clientWidth", {
    configurable: true,
    get() { return (this as HTMLElement).classList?.contains("wf-lane-viewport") ? 1800 : 0; },
  });
  const api = fakeWorkflowApi(PALETTE, [{ definition_id: "wfd_own", workflow_key: "my_flow", name: "My flow",
                                          is_catalogue: false, version: 2, spec: DEF,
                                          versions: [{ version: 2, spec: DEF }] }]);
  vi.stubGlobal("fetch", vi.fn(api.fetch));
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
const $ = (sel: string) => host!.querySelector<HTMLElement>(sel);
const noop = () => {};

async function mountRuns(extra: Record<string, unknown>) {
  act(() => root!.render(
    <RunsView runs={[RUN] as never} openRun={RUN.id} setOpenRun={noop} onAdvance={noop} busy={null}
              capabilities={PALETTE.capabilities as never}
              onRetryStep={noop} onSkipStep={noop} onRerun={noop} onStop={noop} onDelete={noop}
              onStopAndRerun={noop} onBuildOnThis={noop} onRecheck={noop} onSetTarget={noop}
              onPrepareTarget={noop} onRunPreview={noop} onBound={noop} projectId="prj_1"
              actor="ceo" roles={[]} targets={[]} hosts={[]} {...extra} />));
  await settle();
}

describe("a run's full page", () => {
  it("the run row offers Open full page, for that run", async () => {
    const onFullPage = vi.fn();
    await mountRuns({ onFullPage });
    expect($('[data-testid="run-full-page"]')).toBeNull();
    const open = $('[data-testid="open-full-page"]');
    expect(open?.textContent).toContain("Open full page");
    act(() => open!.click());
    expect(onFullPage).toHaveBeenCalledWith("wfr_big");
    // In the row the lane stays roomy and offers no zoom: that is the full page's.
    expect($('[data-testid="wf-zoom"]')).toBeNull();
  });

  it("draws the run alone: its head and way back, the whole lane with Fit and 100%, the step", async () => {
    const onFullPage = vi.fn();
    await mountRuns({ onFullPage, fullPageRun: RUN.id });
    const page = $('[data-testid="run-full-page"]')!;
    expect(page.dataset.runId).toBe("wfr_big");
    expect($('[data-testid="run-full-title"]')?.textContent).toContain("Investigate DQX-0588");
    expect(page.querySelectorAll('[data-testid="dw-run"]').length).toBe(0);
    expect(page.querySelector('[data-testid="dw-path"]')?.getAttribute("data-full-page")).toBe("true");
    // Every step of the run is on the lane.
    expect(page.querySelectorAll('[data-testid="dw-run-lane"] .wf-node').length).toBe(6);
    // Fit first; 100% one press away.
    expect($('[data-testid="wf-zoom-fit"]')?.getAttribute("aria-pressed")).toBe("true");
    act(() => $('[data-testid="wf-zoom-full"]')!.click());
    expect($('[data-testid="wf-zoom-full"]')?.getAttribute("aria-pressed")).toBe("true");
    // The run's own actions are still here, and no second "Open full page".
    expect(page.querySelector('[data-testid="open-full-page"]')).toBeNull();
    act(() => $('[data-testid="run-full-back"]')!.click());
    act(() => $('[data-testid="exit-full-page"]')!.click());
    expect(onFullPage.mock.calls).toEqual([[null], [null]]);
  });

  it("a run that is not in the list says so, with the way back", async () => {
    await mountRuns({ onFullPage: noop, fullPageRun: "wfr_gone" });
    expect($('[data-testid="run-full-missing"]')?.textContent).toContain("wfr_gone");
    expect($('[data-testid="run-full-back"]')).not.toBeNull();
  });
});

describe("the workflow's full page", () => {
  it("the editor's bar switches it, and says Back to library on it", async () => {
    window.history.replaceState(null, "", "/?area=delivery_workflows&wf=my_flow");
    const onFullPage = vi.fn();
    const draw = (fullPage: boolean) => act(() => root!.render(
      <WorkflowEditor projectId="prj_1" workflowKey="my_flow" onBack={noop} onStart={noop}
                      fullPage={fullPage} onFullPage={onFullPage} />));
    draw(false);
    await settle();
    expect($('[data-testid="wf-back"]')?.textContent).toBe("Library");
    act(() => $('[data-testid="open-full-page"]')!.click());
    expect(onFullPage).toHaveBeenCalledTimes(1);
    draw(true);
    await settle();
    expect($('[data-testid="wf-editor"]')?.dataset.fullPage).toBe("true");
    expect($('[data-testid="wf-back"]')?.textContent).toBe("Back to library");
    expect($('[data-testid="exit-full-page"]')).not.toBeNull();
    // Every view is still there: Visual, JSON, Pipeline.
    for (const v of ["visual", "json", "pipeline"]) expect($(`[data-testid="wf-view-${v}"]`)).not.toBeNull();
  });
});

describe("on a full page every step is named in full", () => {
  it("nodes are sized for ~20-character titles on three lines at the root font in use, waits too", async () => {
    const { laneGeometry, wrapNodeWidth, wrapNodeHeight, nodeHeight } = await import("../app/workflows/flow/geometry");
    const layout = layoutFlow(linear(3));
    const types = new Map([["s0", "wait"], ["s1", "agent"], ["s2", "builtin"]] as const);
    for (const rem of [16, 19, 22.8]) {
      const g = laneGeometry(layout, types as never, "comfortable", undefined, rem, true);
      // The longest word of a ~20-character title ("Orchestrator", 12 characters of a 0.875rem
      // title at ≈ 0.6em each) fits on a line beside the icon, so it wraps between words to
      // three lines at most and is never cut.
      expect(g.node.w).toBeGreaterThanOrEqual(Math.ceil(12 * 0.875 * rem * 0.6) + 2 * 12 + 32);
      expect(g.node.w).toBe(wrapNodeWidth(rem));
      expect(g.node.h).toBe(wrapNodeHeight(rem));
      expect(g.node.h).toBeGreaterThan(nodeHeight("comfortable", rem));
      for (const box of g.boxes.values()) expect(box.w).toBe(g.node.w);
    }
  });

  it("Fit never goes below 0.8: wider than that, the lane scrolls", async () => {
    const { wrapFitScale, WRAP_MIN_SCALE } = await import("../app/workflows/flow/geometry");
    expect(WRAP_MIN_SCALE).toBe(0.8);
    expect(wrapFitScale(1000, 1200)).toBe(1);
    expect(wrapFitScale(1000, 900)).toBeCloseTo(0.9);
    expect(wrapFitScale(5000, 1800)).toBe(0.8);
  });

  it("the wrapped lane's titles, names and statuses are never clamped or cut", () => {
    const css = readFileSync("app/workflows/flow.css", "utf8").replace(/\/\*[\s\S]*?\*\//g, "");
    const rule = (sel: string) => css.match(new RegExp(`${sel.replace(/[.[\]"=]/g, "\\$&")}[^{]*\\{([^}]*)\\}`))?.[1] ?? "";
    const title = rule('.wf-lane[data-wrap="true"] .wf-node-title');
    expect(title).toMatch(/-webkit-line-clamp: unset/);
    expect(title).toMatch(/overflow: visible/);
    expect(title).toMatch(/text-overflow: clip/);
    expect(css).toMatch(/\.wf-lane\[data-wrap="true"\] \.wf-node-agent-name,\s*\.wf-lane\[data-wrap="true"\] \.wf-node-status \{[^}]*white-space: normal/);
  });

  it("the run's full page draws the wrapped lane; the row does not", async () => {
    await mountRuns({ onFullPage: noop, fullPageRun: RUN.id });
    expect($('.wf-lane')?.dataset.wrap).toBe("true");
    act(() => root!.render(<div />));
    await mountRuns({ onFullPage: noop });
    expect($('.wf-lane')?.dataset.wrap).toBeUndefined();
  });

  it("a For each says what it runs inside its stack, never an empty box", async () => {
    const { FlowLane } = await import("../app/workflows/flow/flow-lane");
    const spec = {
      entry_step: "plan",
      steps: [
        { id: "plan", kind: "capability", binding: { capability: "research" }, next: ["lanes"] },
        { id: "lanes", kind: "for_each", title: "Lanes", items: "steps.plan.output.items", body: "check",
          item_label: "root-cause family", next: [] },
        { id: "check", kind: "capability", title: "Cause Check", binding: { capability: "research" } },
      ],
    } as unknown as Spec;
    act(() => root!.render(<FlowLane spec={spec} palette={PALETTE} mode="design" wrap />));
    await settle();
    expect($('.wf-node[data-step-id="lanes"] [data-testid="wf-node-repeats"]')?.textContent)
      .toBe("Cause Check × each root-cause family");
  });
});
