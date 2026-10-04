// @vitest-environment happy-dom
/**
 * A run step's panel leads with the step's RUN, and its lane has room (operator, 2026-10-04):
 *
 *   (A) "when we click on it more information about that workflow step's run should be
 *       displayed — what output it created, its Activity logs (take directly to that workflow
 *       step logs) … We should definitely tell which Agent performed that step at what cost,
 *       but limit that info or hide to expand."
 *   (B) "the workflow step is too many and it is so close without space … scroll only for that
 *       track / lane, not the entire page … a tiny bubble at the beginning and end of the line".
 *
 * Rendered for real; the fetches are stubbed at the boundary.
 */
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { linear, PALETTE } from "./_helpers/canvas-fixtures";
import { overBudget } from "./_helpers/copy-budget";
import { shipItChain } from "./_helpers/run-acts-fixtures";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
(globalThis as Record<string, unknown>).React = React;

const { StepDoneBy, StepActivity, summariseStepActivity, stepLogHref, useStepActivity } =
  await import("../app/workflows/step-run");
const { FlowLane } = await import("../app/workflows/flow/flow-lane");
const { ActivityLog } = await import("../app/activity-log-ui");
const { toChain } = await import("../app/agents/act-wire");

const CHAIN = toChain(shipItChain());
const planAct = CHAIN.acts.find((a) => a.actId === "act_plan")!;

let root: Root | null = null;
let host: HTMLElement | null = null;
const asked: string[] = [];
const realClientWidth = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "clientWidth");
let laneWidth = 1100;

const LOG_ROWS = [
  { id: "t1", source: "agent_turn", kind: "turn.out", level: "verbose", title: "thinking", created_at: "2026-10-04T10:03:00Z" },
  { id: "l1", source: "llm_call", kind: "llm.plan", level: "info", title: "Model call", created_at: "2026-10-04T10:02:00Z",
    data: { model: "claude-opus", cost_usd: 0.0125 } },
  { id: "l2", source: "llm_call", kind: "llm.plan", level: "info", title: "Model call", created_at: "2026-10-04T10:01:30Z",
    data: { model: "claude-opus", cost_usd: 0.0075 } },
  { id: "s1", source: "workflow_step", kind: "workflow.step", level: "info", title: "step plan succeeded",
    created_at: "2026-10-04T10:01:00Z" },
];

beforeEach(() => {
  asked.length = 0;
  Object.defineProperty(HTMLElement.prototype, "clientWidth", {
    configurable: true,
    get() { return (this as HTMLElement).classList?.contains("wf-lane-viewport") ? laneWidth : 0; },
  });
  vi.stubGlobal("fetch", vi.fn(async (input: unknown) => {
    const url = new URL(String(input), "http://console.test");
    asked.push(`${url.pathname}?${url.searchParams.toString()}`);
    if (url.pathname === "/api/activity") return Response.json({ items: LOG_ROWS, total: LOG_ROWS.length });
    if (url.pathname === "/api/run-acts") return Response.json({ ...planAct, act_id: "act_plan", turns_excerpt: [] });
    return Response.json({});
  }));
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
});

afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  root = null; host = null;
  laneWidth = 1100;
  vi.unstubAllGlobals();
  if (realClientWidth) Object.defineProperty(HTMLElement.prototype, "clientWidth", realClientWidth);
  window.history.replaceState({}, "", "/");
});

async function render(el: React.ReactElement) {
  await act(async () => root!.render(el));
  for (let i = 0; i < 8; i += 1) await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
  return host!;
}
const q = (sel: string) => host!.querySelector<HTMLElement>(sel);

describe("(A) who did it is one line, with the agent's card one press away", () => {
  it("says who, what it cost and on which model, and hides the card until asked", async () => {
    await render(<StepDoneBy act={planAct} output={{}} phase="done" projectId="p1" viewer="ceo"
                             question="Why?" cost={0} model="claude-opus" onAsk={() => {}} stepId="plan" />);
    const line = q('[data-testid="dw-step-doneby"]')!;
    expect(line.textContent).toContain("Done by");
    expect(q('[data-testid="dw-step-doneby-name"]')!.textContent).toBe("Atlas");
    expect(q('[data-testid="dw-step-model"]')!.textContent).toBe("claude-opus");
    expect(q('[data-testid="ag-signed-by"]')).toBeNull();
    await act(async () => q('[data-testid="dw-step-agent-toggle"]')!.click());
    expect(q('[data-testid="ag-signed-by"]')).not.toBeNull();
    expect(q('[data-testid="dw-step-agent-toggle"]')!.getAttribute("aria-expanded")).toBe("true");
  });

  it("falls back to the cost of the step's own model calls when the work record has none", async () => {
    await render(<StepDoneBy act={{ ...planAct, costUsd: 0 }} output={{}} phase="done" projectId="p1" viewer="ceo"
                             question="Why?" cost={0.02} model="" onAsk={() => {}} stepId="plan" />);
    expect(q('[data-testid="dw-step-cost"]')!.textContent).toBe("$0.02");
  });

  it("before the step runs it names who it is assigned to, and a skipped step with nobody says nothing", async () => {
    await render(<StepDoneBy act={null} output={{}} phase="ahead" projectId="p1" viewer="ceo" question="?"
                             cost={0} model="" assigned="qa" onAsk={() => {}} stepId="qa" />);
    expect(q('[data-testid="dw-step-doneby"]')!.textContent).toContain("Assigned to");
    await render(<StepDoneBy act={null} output={{}} phase="skipped" projectId="p1" viewer="ceo" question="?"
                             cost={0} model="" assigned="qa" onAsk={() => {}} stepId="qa" />);
    expect(q('[data-testid="dw-step-doneby"]')).toBeNull();
  });
});

describe("(A) the step's activity, and the way straight into its log", () => {
  it("reads the log scoped to this step and summarises it: no verbose turns, model, summed cost", () => {
    const s = summariseStepActivity(LOG_ROWS.map((r) => ({
      id: r.id, source: r.source, kind: r.kind, status: "ok", level: r.level, title: r.title, actor: "",
      entityType: "", entityId: "", stage: "", runId: "", workflowRunId: "", agent: "", service: "", body: "",
      createdAt: r.created_at, data: (r as { data?: Record<string, unknown> }).data ?? {},
    })) as never);
    expect(s.rows.map((r) => r.id)).toEqual(["l1", "l2", "s1"]);
    expect(s.model).toBe("claude-opus");
    expect(s.cost).toBeCloseTo(0.02, 6);
  });

  it("asks the API for this step of this run, and links to the step's own log", async () => {
    window.history.replaceState({}, "", "/?org=org_boss&project=p1&area=delivery_workflows&wfr=wfr_1");
    function Probe() {
      const state = useStepActivity("p1", "wfr_1", "plan", false, "succeeded:1");
      return <StepActivity state={state} live={false}
                           href={stepLogHref(window.location.search, "wfr_1", "plan")} />;
    }
    await render(<Probe />);
    const call = asked.find((u) => u.startsWith("/api/activity?"))!;
    expect(new URLSearchParams(call.split("?")[1]).get("step_id")).toBe("plan");
    expect(new URLSearchParams(call.split("?")[1]).get("workflow_run_id")).toBe("wfr_1");
    const rows = host!.querySelectorAll('[data-testid="dw-step-log-row"]');
    expect(rows).toHaveLength(3);
    const href = new URL(q('[data-testid="dw-step-log"]')!.getAttribute("href")!, "http://console.test");
    expect(href.searchParams.get("area")).toBe("activity");
    expect(href.searchParams.get("wfr")).toBe("wfr_1");
    expect(href.searchParams.get("step")).toBe("plan");
    expect(href.searchParams.get("org")).toBe("org_boss");
  });

  it("the activity log opened on that link is scoped to the step, and says so", async () => {
    window.history.replaceState({}, "", "/?area=activity&wfr=wfr_1&step=plan");
    await render(<ActivityLog projectId="p1" actor="ceo" />);
    const lists = asked.filter((u) => u.startsWith("/api/activity?"));
    expect(lists.length).toBeGreaterThan(0);
    // What the log settles on is the step's scope, for the rows and for the chips' counts.
    expect(lists.at(-1)).toContain("step_id=plan");
    expect(lists.at(-1)).toContain("workflow_run_id=wfr_1");
    expect(asked.filter((u) => u.startsWith("/api/activity/facets?")).at(-1)).toContain("step_id=plan");
    expect(q('[data-testid="activity-scope-step"]')!.textContent).toBe("plan");
    await act(async () => q('[data-testid="activity-scope-step-clear"]')!.click());
    for (let i = 0; i < 4; i += 1) await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
    expect(new URLSearchParams(window.location.search).get("step")).toBeNull();
    expect(q('[data-testid="activity-scope-step"]')).toBeNull();
  });

  it("prints no sentence: six words at most outside a tooltip, no paragraphs", async () => {
    function Panel() {
      const state = useStepActivity("p1", "wfr_1", "plan", false, "x");
      return (<div>
        <StepDoneBy act={planAct} output={{}} phase="done" projectId="p1" viewer="ceo" question="Why?"
                    cost={0} model="claude-opus" onAsk={() => {}} stepId="plan" />
        <StepActivity state={state} live={false} href="/x" />
      </div>);
    }
    const el = await render(<Panel />);
    expect(overBudget(el)).toEqual([]);
  });
});

describe("(B) a run's lane has room, and scrolls only inside itself", () => {
  it("draws a run roomy, Comfortable at full size, however many steps it has", async () => {
    const el = await render(<FlowLane spec={linear(11)} palette={PALETTE} mode="run" runState={{}} />);
    const lane = el.querySelector<HTMLElement>('[data-testid="wf-lane"]')!;
    expect(lane.dataset.density).toBe("comfortable");
    expect(Number(el.querySelector<HTMLElement>(".wf-lane-canvas")!.dataset.scale)).toBe(1);
    expect(lane.dataset.scrolls).toBe("true");
    // Real air between steps: the connector is wider than the node padding, not a hairline.
    const slots = [...el.querySelectorAll<HTMLElement>('.wf-slot[data-slot]:not(.wf-slot-end)')];
    const a = slots[0], b = slots[1];
    const gap = parseFloat(b.style.left) - (parseFloat(a.style.left) + parseFloat(a.style.width));
    expect(gap).toBeGreaterThanOrEqual(48);
  });

  it("shows which way there is more, and the arrow scrolls the lane, not the page", async () => {
    const el = await render(<FlowLane spec={linear(11)} palette={PALETTE} mode="run" runState={{}} />);
    const left = el.querySelector<HTMLButtonElement>('[data-testid="wf-lane-left"]')!;
    const right = el.querySelector<HTMLButtonElement>('[data-testid="wf-lane-right"]')!;
    expect(left.hidden).toBe(true);
    expect(right.hidden).toBe(false);
    expect(el.querySelector('.wf-lane-fade[data-side="right"]')!.hasAttribute("data-on")).toBe(true);
    const viewport = el.querySelector<HTMLElement>(".wf-lane-viewport")!;
    (viewport as unknown as { scrollBy: unknown }).scrollBy = undefined;
    await act(async () => right.click());
    await act(async () => { viewport.dispatchEvent(new Event("scroll")); });
    expect(viewport.scrollLeft).toBeGreaterThan(0);
    expect(left.hidden).toBe(false);
  });

  it("fits the editor's lane by default, and draws no arrows when nothing is hidden", async () => {
    laneWidth = 4000;
    const el = await render(<FlowLane spec={linear(5)} palette={PALETTE} mode="run" runState={{}} />);
    expect(el.querySelector('[data-testid="wf-lane-right"]')).toBeNull();
  });

  it("puts a bubble at each end of the line, filled once the run has reached it", async () => {
    const spec = linear(3);
    const ids = spec.steps.map((s) => s.id);
    const done = Object.fromEntries(ids.map((id) => [id, { phase: "done", meta: "" }]));
    let el = await render(<FlowLane spec={spec} palette={PALETTE} mode="run"
                                    runState={{ [ids[0]]: { phase: "moving", meta: "" } }} />);
    expect(el.querySelector('[data-testid="wf-start"] .wf-bubble')).not.toBeNull();
    expect(el.querySelector('[data-testid="wf-start"]')!.getAttribute("data-flow")).toBe("past");
    expect(el.querySelector('[data-testid="wf-end"]')!.getAttribute("data-flow")).toBe("future");
    el = await render(<FlowLane spec={spec} palette={PALETTE} mode="run" runState={done} />);
    expect(el.querySelector('[data-testid="wf-end"] .wf-bubble')).not.toBeNull();
    expect(el.querySelector('[data-testid="wf-end"]')!.getAttribute("data-flow")).toBe("past");
  });
});
