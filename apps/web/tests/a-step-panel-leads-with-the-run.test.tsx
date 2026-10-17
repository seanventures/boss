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

const { StepDoneBy, StepActivity, StepOutputFacts, summariseStepActivity, stepLogHref, useStepActivity,
        stepOutput, factText, stepLogSentence, actorVerb, stepActor } = await import("../app/workflows/step-run");
const { viewOf } = await import("../app/workflows/flow/node-facts");
const { laneAgentOf } = await import("../app/agents/chain-model");
const { usd } = await import("../lib/money");
const { stepQuestion } = await import("../app/delivery-workflows-ui");
const { FlowLane } = await import("../app/workflows/flow/flow-lane");
const { LogExplorer } = await import("../app/activity/log-explorer");
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
  { id: "s1", source: "workflow_step", kind: "step.capability", level: "info", title: "Step Plan Succeeded",
    created_at: "2026-10-04T10:01:00Z", data: { status: "succeeded", attempt: 1, duration_seconds: 20 } },
  { id: "n1", source: "event", kind: "workflow.step_leased", level: "info", title: "Workflow Step Leased",
    created_at: "2026-10-04T10:00:40Z" },
  { id: "a1", source: "app_log", kind: "log.sandbox", level: "info", title: "Log Sandbox",
    created_at: "2026-10-04T10:00:30Z" },
  { id: "p1", source: "event", kind: "code_task.pr_opened", level: "info", title: "Code Task Pr Opened",
    created_at: "2026-10-04T10:00:20Z", data: { number: 2 } },
  { id: "e1", source: "event", kind: "code_task.review_comments_failed", level: "error",
    title: "Code Task Review Comments Failed", created_at: "2026-10-04T10:00:10Z" },
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
const mapped = () => LOG_ROWS.map((r) => ({
  id: r.id, source: r.source, kind: r.kind, status: "ok", level: r.level, title: r.title, actor: "",
  entityType: "", entityId: "", stage: "", runId: "", workflowRunId: "", agent: "", service: "", body: "",
  createdAt: r.created_at, data: (r as { data?: Record<string, unknown> }).data ?? {},
})) as never;
const BASE = { output: {}, projectId: "p1", viewer: "ceo", question: "Why?", cost: 0, calls: 0, model: "",
               onAsk: () => {}, stepId: "plan", facts: [] as string[] };

describe("(A) who did it is one line, with the agent's card one press away", () => {
  it("says who, what it cost and on which model, and hides the card until asked", async () => {
    await render(<StepDoneBy {...BASE} act={planAct} phase="done" status="succeeded" model="claude-opus"
                             facts={["Started 10:01Z"]} />);
    const line = q('[data-testid="dw-step-doneby"]')!;
    expect(line.textContent).toContain("Done by");
    expect(q('[data-testid="dw-step-doneby-name"]')!.textContent).toBe("Atlas");
    expect(q('[data-testid="dw-step-model"]')!.textContent).toBe("claude-opus");
    expect(q('[data-testid="ag-signed-by"]')).toBeNull();
    await act(async () => q('[data-testid="dw-step-agent-toggle"]')!.click());
    expect(q('[data-testid="ag-signed-by"]')).not.toBeNull();
    expect(q('[data-testid="dw-step-agent-toggle"]')!.getAttribute("aria-expanded")).toBe("true");
    // ONE Ask (the line's), and the card opens BELOW the step's facts, not between them.
    expect(q('[data-testid="ag-signed-ask"]')).toBeNull();
    expect(q('[data-testid="ag-act-ask"]')).toBeNull();
    expect(host!.querySelectorAll('[data-testid="dw-step-ask"]')).toHaveLength(1);
    const facts = q('[data-testid="dw-step-facts"]')!;
    expect(facts.compareDocumentPosition(q('[data-testid="dw-step-agent-card"]')!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("falls back to the cost of the step's own model calls when the work record has none", async () => {
    await render(<StepDoneBy {...BASE} act={{ ...planAct, costUsd: 0 }} phase="done" status="succeeded"
                             cost={0.02} calls={2} />);
    expect(q('[data-testid="dw-step-cost"]')!.textContent).toBe("$0.02");
    // A step whose model calls cost nothing still says so, in the one format.
    await render(<StepDoneBy {...BASE} act={null} output={{ ran_as: { agent: "qa", how: "named" } }}
                             phase="done" status="succeeded" cost={0} calls={3} />);
    expect(q('[data-testid="dw-step-cost"]')!.textContent).toBe("$0.00");
  });

  it("before the step runs it names who the lane names; a skipped step says Skipped and why, with no agent", async () => {
    await render(<StepDoneBy {...BASE} act={null} phase="ahead" status="pending"
                             planned={{ kind: "you", label: "You", tip: "" }} />);
    expect(q('[data-testid="dw-step-doneby"]')!.textContent).toContain("Assigned to");
    expect(q('[data-testid="dw-step-doneby-name"]')!.textContent).toBe("You");
    await render(<StepDoneBy {...BASE} act={planAct} phase="skipped" status="succeeded"
                             planned={{ kind: "named", role_key: "qa", label: "QA Lead", tip: "" }}
                             skipped="Skipped: research cannot run here. The panel works from your request instead." />);
    expect(q('[data-testid="dw-step-skipped"]')!.textContent).toBe("Skipped");
    expect(q('[data-testid="dw-step-doneby"]')!.textContent).toContain("research cannot run here");
    expect(q('[data-testid="dw-step-doneby-name"]')).toBeNull();
    expect(q('[data-testid="dw-step-agent-toggle"]')).toBeNull();
  });

  it("a ready step nobody has picked up is queued, not being done", () => {
    expect(actorVerb("now", "ready")).toBe("Queued for");
    expect(actorVerb("now", "running")).toBe("Doing");
    expect(actorVerb("done", "succeeded")).toBe("Done by");
    // A failed step was not done (rc10 tester: "Done by BOSS" over a failed smoke test).
    expect(actorVerb("done", "failed")).toBe("Tried by");
    expect(actorVerb("done", "deadlettered")).toBe("Tried by");
  });

  it("names the same actor as the lane node: who ran it, else the step's planned view", async () => {
    const spec = { id: "deploy", kind: "capability", binding: { capability: "notify" } } as never;
    const planned = viewOf(spec, PALETTE).agent;
    expect(planned.kind).toBe("you");
    await render(<StepDoneBy {...BASE} act={null} phase="ahead" status="pending" planned={planned} />);
    expect(q('[data-testid="dw-step-doneby-name"]')!.textContent).toBe("You");
    // A gate BOSS approved by itself is BOSS's on the lane and in the panel alike.
    const auto = { outcome: "approved", approved_by: "auto_approval" };
    expect(laneAgentOf(auto, null, "ceo")?.variant).toBe("boss");
    expect(stepActor(laneAgentOf(auto, null, "ceo"), { kind: "person", label: "A person", tip: "" })?.variant).toBe("boss");
  });

  it("asks about a step in English", () => {
    expect(stepQuestion({ step_id: "build", status: "succeeded" }, "Build")).toBe("What did the Build step do?");
    expect(stepQuestion({ step_id: "build", status: "skipped" }, "Build")).toBe("Why was the Build step skipped?");
  });

  it("writes money one way", () => {
    expect([usd(0), usd(0.002), usd(0.0125), usd(3.456), usd(12.4)]).toEqual(["$0.00", "<$0.01", "$0.01", "$3.46", "$12"]);
  });
});

describe("(A) the step's activity, and the way straight into its log", () => {
  it("reads the log as sentences, errors first, bookkeeping out, repeats folded", () => {
    const s = summariseStepActivity(mapped());
    expect(s.rows.map((r) => r.text)).toEqual([
      "Review comments failed", "Plan: model call", "Step succeeded in 20s", "Opened pull request #2"]);
    expect(s.errors).toBe(1);
    expect(s.model).toBe("claude-opus");
    expect(s.cost).toBeCloseTo(0.02, 6);
    expect(s.calls).toBe(2);
    expect(stepLogSentence({ source: "event", kind: "workflow.step_ready", level: "info", title: "x", data: {} })).toBeNull();
    // Said once, by the step's own row with its duration — not also as "Step finished".
    expect(stepLogSentence({ source: "event", kind: "workflow.step_succeeded", level: "info", title: "x", data: {} })).toBeNull();
  });

  it("asks the API for this step of this run, and links to the step's own log", async () => {
    window.history.replaceState({}, "", "/?org=org_boss&project=p1&area=delivery_workflows&wfr=wfr_1");
    function Probe() {
      const state = useStepActivity("p1", "wfr_1", "plan", false, "succeeded:1");
      return <StepActivity state={state} live={false}
                           href={stepLogHref(window.location.search, "wfr_1", "plan", { step: "Plan it", run: "Ship it" })} />;
    }
    await render(<Probe />);
    const call = asked.find((u) => u.startsWith("/api/activity?"))!;
    expect(new URLSearchParams(call.split("?")[1]).get("step_id")).toBe("plan");
    expect(new URLSearchParams(call.split("?")[1]).get("workflow_run_id")).toBe("wfr_1");
    // ONE bounded read: the server's summary (rows, error/warn counts, model calls), not a
    // 100-row page re-scanned every five seconds.
    expect(new URLSearchParams(call.split("?")[1]).get("summary")).toBe("1");
    expect(asked.filter((u) => u.startsWith("/api/activity"))).toHaveLength(1);
    const rows = host!.querySelectorAll('[data-testid="dw-step-log-row"]');
    expect(rows).toHaveLength(4);
    // An error the step log holds is never below the fold of the panel: pinned first, counted.
    expect(rows[0].getAttribute("data-level")).toBe("error");
    expect(q('[data-testid="dw-step-log-loud"]')!.textContent).toBe("1 error");
    const href = new URL(q('[data-testid="dw-step-log"]')!.getAttribute("href")!, "http://console.test");
    expect(href.searchParams.get("area")).toBe("activity");
    expect(href.searchParams.get("wfr")).toBe("wfr_1");
    expect(href.searchParams.get("step")).toBe("plan");
    expect(href.searchParams.get("org")).toBe("org_boss");
    expect(href.searchParams.get("step_name")).toBe("Plan it");
  });

  it("warnings are counted only once a step has failed; a running step counts its errors", async () => {
    const state = { rows: [], loaded: true, errors: 2, warnings: 29, model: "", cost: 0, calls: 0 };
    const loud = () => q('[data-testid="dw-step-log-loud"]')!.textContent?.trim();
    await render(<StepActivity state={state} live href="#" />);
    expect(loud()).toBe("2 errors");
    await render(<StepActivity state={state} live={false} failed href="#" />);
    expect(loud()).toBe("2 errors · 29 warnings");
    await render(<StepActivity state={state} live={false} settled href="#" />);
    expect(loud()).toBe("2 recovered");
    await render(<StepActivity state={{ ...state, errors: 0 }} live href="#" />);
    expect(host!.querySelector('[data-testid="dw-step-log-loud"]')).toBeNull();
  });

  it("a step that succeeded counts what it hit along the way as recovered, not as errors", async () => {
    window.history.replaceState({}, "", "/?org=org_boss&project=p1&area=delivery_workflows&wfr=wfr_1");
    function Probe() {
      const state = useStepActivity("p1", "wfr_1", "plan", false, "succeeded:1");
      return <StepActivity state={state} live={false} settled
                           href={stepLogHref(window.location.search, "wfr_1", "plan", { step: "Plan it", run: "Ship it" })} />;
    }
    await render(<Probe />);
    const loud = q('[data-testid="dw-step-log-loud"]')!;
    expect(loud.textContent?.trim()).toBe("1 recovered");
    expect(loud.getAttribute("data-level")).toBe("recovered");
  });

  it("the activity log opened on that link is scoped to the step, and says so", async () => {
    window.history.replaceState({}, "", "/?area=activity&wfr=wfr_1&step=plan&step_name=Plan+it&run_name=Lead+Triage+Inbox");
    await render(<LogExplorer projectId="p1" actor="ceo" />);
    const lists = asked.filter((u) => u.startsWith("/api/activity?"));
    expect(lists.length).toBeGreaterThan(0);
    // What the log settles on is the step's scope, for the rows and for the rail's counts.
    expect(lists.at(-1)).toContain("step_id=plan");
    expect(lists.at(-1)).toContain("workflow_run_id=wfr_1");
    expect(asked.filter((u) => u.startsWith("/api/activity/facets?")).at(-1)).toContain("step_id=plan");
    // Names, not ids: the ids are the tokens' tooltips.
    expect(q('[data-testid="lx-token-step"]')!.textContent).toBe("step: Plan it");
    expect(q('[data-testid="lx-token-wfr"]')!.textContent).toBe("wfr: Lead Triage Inbox");
    await act(async () => q('[data-testid="lx-whole-run"]')!.click());
    for (let i = 0; i < 4; i += 1) await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
    expect(new URLSearchParams(window.location.search).get("step")).toBeNull();
    expect(q('[data-testid="lx-token-step"]')).toBeNull();
  });

  it("prints no sentence: six words at most outside a tooltip, no paragraphs", async () => {
    function Panel() {
      const state = useStepActivity("p1", "wfr_1", "plan", false, "x");
      return (<div>
        <StepDoneBy {...BASE} act={planAct} phase="done" status="succeeded" model="claude-opus" />
        <StepOutputFacts {...stepOutput(BUILD_OUTPUT, { noteShownAbove: false })} />
        <StepActivity state={state} live={false} href="/x" />
      </div>);
    }
    const el = await render(<Panel />);
    expect(overBudget(el)).toEqual([]);
  });
});

const BUILD_OUTPUT = {
  tool: "create_code_task", await: { id: "crun_768a", kind: "code_task" }, act_id: "act_01", run_id: "crun_768a",
  status: "succeeded", verdict: "awaiting", binding_id: null, capability: "build", project_id: "boss-dogfood",
  repository: "e2e-org/e2e-app", base_branch: "main", awaited_status: "completed", code_task_run_id: "crun_768a",
  meeting: { job_id: null, run_id: "mrun_1", task_id: "tsk_1" },
};

describe("(A) Output is what the step made, for a person", () => {
  it("shows human outputs and files every machine field under Details, each once, never null", async () => {
    const { facts, details } = stepOutput(BUILD_OUTPUT, { noteShownAbove: false });
    expect(facts).toEqual([{ label: "Repository", value: "e2e-org/e2e-app" }, { label: "Base branch", value: "main" }]);
    const texts = details.map((d) => d.value);
    // A record id is a named link, once — never the raw id as text.
    expect(details.filter((d) => d.href === "?area=code_tasks&build=crun_768a")).toHaveLength(1);
    expect(texts).toContain("Open build");
    expect(texts).toContain("awaiting");                    // still in flight: no status given
    expect(texts.join(" ")).not.toMatch(/null|act_01|boss-dogfood|crun_768a|mrun_1/);
    await render(<StepOutputFacts facts={facts} details={details} />);
    expect(q('[data-testid="dw-step-out"]')!.textContent).not.toContain("awaiting");
    expect((q('[data-testid="dw-step-details"]') as HTMLDetailsElement).open).toBe(false);
  });

  it("a finished step shows no in-flight state and no raw ids, only named links", () => {
    const deploy = { await: { id: "deprun_8bdeb296", kind: "deployment" }, verdict: "awaiting",
      next_action: "approve_decision", target_id: "deptgt_be289c6af97a", decision_id: "dec_fb1f425c2979",
      changeset_id: "chg_13206e6b5f80", app_name: "HelpDesk", environment: "staging" };
    const { facts, details } = stepOutput(deploy, { noteShownAbove: false, status: "succeeded" });
    const all = [...facts, ...details].map((f) => `${f.label} ${f.value} ${f.href ?? ""}`).join(" | ");
    expect(all).not.toMatch(/awaiting|approve_decision|deptgt_/);
    expect(details.map((d) => d.value).join(" ")).not.toMatch(/(deprun|dec|chg)_/);
    expect(details.map((d) => d.href)).toEqual(expect.arrayContaining([
      "?area=decisions&decision=dec_fb1f425c2979",
      "?area=infra&view=cloud&cloud=changes&changeset=chg_13206e6b5f80"]));
  });

  it("a succeeded step's note is an output; a step that did not succeed shows it above instead", () => {
    const out = { note: "Skipped: research cannot run here. The panel works from your request.", verdict: "succeeded",
                  reason: "research_source_unavailable", status: "not_applicable" };
    expect(stepOutput(out, { noteShownAbove: false }).facts[0]).toMatchObject(
      { label: "Note", value: "Skipped: research cannot run here." });
    expect(stepOutput(out, { noteShownAbove: true }).facts).toEqual([]);
  });

  it("an object's text is its first id that has a value", () => {
    expect(factText({ job_id: null, run_id: "mrun_1" })).toBe("mrun_1");
    expect(factText({ job_id: null })).toBe("");
    expect(factText(null)).toBe("");
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
    expect(left.disabled).toBe(true);
    expect(right.disabled).toBe(false);
    // In gutters beside the steps, never over them, and not a tab stop.
    const viewport0 = el.querySelector<HTMLElement>(".wf-lane-viewport")!;
    expect(viewport0.contains(right)).toBe(false);
    expect(right.parentElement).toBe(viewport0.parentElement);
    expect(right.tabIndex).toBe(-1);
    expect(el.querySelector('.wf-lane-fade[data-side="right"]')!.hasAttribute("data-on")).toBe(true);
    const viewport = el.querySelector<HTMLElement>(".wf-lane-viewport")!;
    (viewport as unknown as { scrollBy: unknown }).scrollBy = undefined;
    await act(async () => right.click());
    await act(async () => { viewport.dispatchEvent(new Event("scroll")); });
    expect(viewport.scrollLeft).toBeGreaterThan(0);
    expect(left.disabled).toBe(false);
  });

  it("draws no arrows when nothing is hidden, and closes the gaps rather than scroll for a few pixels", async () => {
    laneWidth = 4000;
    let el = await render(<FlowLane spec={linear(5)} palette={PALETTE} mode="run" runState={{}} />);
    expect(el.querySelector('[data-testid="wf-lane-right"]')).toBeNull();
    // Four steps, 33px wider than the lane at full spacing: they fit, with tighter connectors.
    const geometry = await import("../app/workflows/flow/geometry");
    const { layoutFlow } = await import("../lib/workflow-graph");
    const four = linear(4);
    const types = new Map(four.steps.map((x) => [x.id, "builtin" as const]));
    laneWidth = geometry.laneGeometry(layoutFlow(four), types, "comfortable").width - 33;
    el = await render(<FlowLane spec={four} palette={PALETTE} mode="run" runState={{}} />);
    expect(el.querySelector<HTMLElement>('[data-testid="wf-lane"]')!.dataset.scrolls).toBeUndefined();
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
