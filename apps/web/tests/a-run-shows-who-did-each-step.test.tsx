// @vitest-environment happy-dom
/**
 * A run shows its agents doing the work (spec §3.5-§3.6, S-runs acceptance).
 *
 *  - The Chain's order is the API's order: You → Juno → Atlas → the captain's build (its stages
 *    folded, the repair loop said once) → Vera and Rook in parallel → You → Pilot.
 *  - A step run by a person says `You`; a built-in with no captain says `You` too.
 *  - An Auto step stays Auto until `ran_as` exists, then becomes the agent BOSS picked.
 *  - The drawer is signed: the Act card inline, what the agent said, and `Ask <Name>`, which
 *    opens the dock with that agent, about this step, parented to this work.
 *  - Scope wider than its parent is flagged, never hidden.
 *
 * The run view (RunsView) is mounted for real with only `fetch` stood in, as the API answers.
 */
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { shipItChain, wireAct } from "./_helpers/run-acts-fixtures";
import { standingOf } from "./_helpers/run-standing";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
(globalThis as Record<string, unknown>).React = React;

const { toChain, toAct } = await import("../app/agents/act-wire");
const { chainItems, laneAgentOf } = await import("../app/agents/chain-model");
const { RunChain } = await import("../app/agents/run-chain");
const { StepNode } = await import("../app/workflows/flow/step-node");
const { RunsView } = await import("../app/delivery-workflows-ui");
const { forgetRunDefinitions } = await import("../app/workflows/flow/run-lane");

let root: Root | null = null;
let host: HTMLElement | null = null;
afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  root = null; host = null;
  vi.unstubAllGlobals();
  window.history.replaceState(null, "", "/");
});
function render(node: React.ReactNode): HTMLElement {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  act(() => root!.render(node));
  return host;
}
async function settle() {
  for (let i = 0; i < 25; i += 1) await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
}

const VIEW = (kind: "named" | "auto" | "you" | "person", role_key?: string) => ({
  id: "s", title: "Review it", type: "agent" as const, icon: "agent",
  agent: { kind, role_key, label: kind === "auto" ? "Auto" : kind === "you" ? "You" : role_key ?? "", tip: "" },
  badges: { approval: false, forced: false, inputs: 0, unwired: [], skills: 0, mcp: 0, lacks: [] },
});

describe("the chain", () => {
  it("is in the API's order, with parallel work stacked, inner work folded and loops said once", () => {
    const chain = toChain(shipItChain());
    const items = chainItems(chain, "ceo");
    const said = items.map((it) => it.kind === "loop" ? "loop"
      : it.acts.map((a) => a.roleKey ?? (a.handedBy.id === "ceo" || a.accountable === "ceo" ? "You" : "?")).join("+"));
    expect(said).toEqual(["You", "pm", "chief_architect", "engineering_captain", "qa+adversarial_reviewer",
      "You", "You", "release_manager"]);
    const build = items[3];
    expect(build.kind === "hop" && build.insideCount).toBe(6);
    expect(build.kind === "hop" && build.inside.map((i) => i.kind)).toEqual(["loop"]);
    const loop = build.kind === "hop" ? build.inside[0] : null;
    expect(loop?.kind === "loop" && [loop.a.acts[0].roleKey, loop.b.acts[0].roleKey, loop.rounds])
      .toEqual(["backend_engineer", "adversarial_reviewer", 3]);
  });

  it("keeps distinct steps that alternate between two faces: a loop is the same work coming back", () => {
    // Built-ins run as You, so research → PRD → readiness → approve → deploy alternates You and
    // the PM while doing five different things. None of it may fold into `You ↔ PM ×3`.
    const root = wireAct({ act_id: "act_r", kind: "run", how: "started", handed_by_kind: "person", handed_by_id: "ceo",
                           label: "Run · Ship it #182" });
    const steps = [["research", null], ["prd", "pm"], ["readiness", null], ["approve", "pm"], ["deploy", null]]
      .map(([id, role], i, all) => wireAct({ act_id: `act_${id}`, kind: "step", role_key: role, step_id: id,
        parent_act_id: i === 0 ? "act_r" : `act_${all[i - 1][0]}`, how: role ? "named" : "run_actor",
        label: `${id} · Ship it #182` }));
    const items = chainItems(toChain({ acts: [root, ...steps], accountable: "ceo", totals: {}, credits: [] }), "ceo");
    expect(items.map((it) => it.kind)).toEqual(["hop", "hop", "hop", "hop", "hop", "hop"]);
    expect(items.slice(1).map((it) => it.acts[0].links.step_id)).toEqual(["research", "prd", "readiness", "approve", "deploy"]);
  });

  it("draws that order, and opens the build's inner work on request", () => {
    const el = render(<RunChain chain={toChain(shipItChain())} viewer="ceo" />);
    const hops = [...el.querySelectorAll('[data-testid="ag-chain-strip"] > [data-testid="ag-chain-hop"]')]
      .map((h) => h.getAttribute("data-roles"));
    expect(hops).toEqual(["", "pm", "chief_architect", "engineering_captain", "qa,adversarial_reviewer", "", "", "release_manager"]);
    const fold = el.querySelector('[data-testid="ag-chain-fold"]') as HTMLButtonElement;
    expect(fold.textContent).toContain("+6 inside Build");
    act(() => fold.click());
    const loop = el.querySelector('[data-testid="ag-chain-sub"] [data-testid="ag-chain-loop"]');
    expect(loop?.getAttribute("data-roles")).toBe("backend_engineer,adversarial_reviewer");
    expect(loop?.textContent).toContain("×3");
  });

  it("rings the hop that is working now, and only that one", () => {
    const el = render(<RunChain chain={toChain(shipItChain())} viewer="ceo" />);
    const working = [...el.querySelectorAll('[data-testid="ag-chain-strip"] .ag-face[data-state="working"]')]
      .map((f) => f.getAttribute("data-role-key"));
    expect(working).toEqual(["release_manager"]);
  });

  it("opens a hop's Act card, which says how the work reached it", () => {
    const el = render(<RunChain chain={toChain(shipItChain())} viewer="ceo" />);
    const atlas = el.querySelector('[data-act-id="act_plan"][data-testid="ag-act-face"]') as HTMLButtonElement;
    act(() => atlas.click());
    const card = document.querySelector('[role="dialog"] [data-testid="ag-act-card"]')!;
    expect(card.querySelector('[data-testid="ag-act-how-label"]')?.textContent).toBe("Picked by BOSS over 3");
    act(() => document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" })));
    expect(document.querySelector('[role="dialog"] [data-testid="ag-act-card"]')).toBeNull();
  });

  it("draws nothing for a run with no signed work (an older install)", () => {
    const el = render(<RunChain chain={toChain({ acts: [], totals: {}, credits: [] })} viewer="ceo" />);
    expect(el.querySelector('[data-testid="ag-chain"]')).toBeNull();
  });
});

describe("a run step's face", () => {
  it("stays Auto until ran_as exists, then becomes the agent BOSS picked", () => {
    const before = laneAgentOf({}, null, "ceo");
    expect(before).toBeUndefined();
    let el = render(<StepNode mode="run" view={VIEW("auto")} run={{ phase: "moving", meta: "", agent: before }} />);
    expect(el.querySelector('[data-testid="wf-node-agent"]')?.getAttribute("data-kind")).toBe("auto");
    expect(el.querySelector('[data-morph="true"]')).toBeNull();
    act(() => root?.unmount());

    const after = laneAgentOf({ ran_as: { agent: "adversarial_reviewer", how: "model" } }, null, "ceo");
    el = render(<StepNode mode="run" view={VIEW("auto")} run={{ phase: "moving", meta: "", agent: after }} />);
    const node = el.querySelector('[data-testid="wf-node-agent"]')!;
    expect(node.getAttribute("data-morph")).toBe("true");
    expect(node.getAttribute("data-ran-as")).toBe("adversarial_reviewer");
    // The agent's callsign, the name every surface gives it (the role is in the tooltip).
    expect(node.querySelector(".wf-node-agent-name")?.textContent).toBe("Rook");
    expect(el.querySelector('[data-testid="ag-how"]')?.getAttribute("data-how")).toBe("model_pick");
  });

  it("says You for a step a person ran, and for a built-in with no captain", () => {
    const ranAsYou = laneAgentOf({ ran_as: { agent: "ceo", how: "run_actor" } }, null, "ceo");
    let el = render(<StepNode mode="run" view={VIEW("named", "qa")} run={{ phase: "done", meta: "", agent: ranAsYou }} />);
    expect(el.querySelector(".wf-node-agent-name")?.textContent).toBe("You");
    expect(el.querySelector('.ag-face')?.getAttribute("data-variant")).toBe("you");
    act(() => root?.unmount());

    el = render(<StepNode mode="run" view={VIEW("you")} run={{ phase: "done", meta: "" }} />);
    expect(el.querySelector(".wf-node-agent-name")?.textContent).toBe("You");
  });

  it("never gives the workflow, a gate's person or another person an agent's face", () => {
    const runner = laneAgentOf({ ran_as: { agent: "workflow_runner", how: "workflow_runner" } }, null, "ceo");
    expect(runner && [runner.roleKey, runner.variant]).toEqual([null, "boss"]);
    const legacy = laneAgentOf({ ran_as: { agent: "workflow_runner", how: "legacy_workflow_runner" } }, null, "ceo");
    expect(legacy && [legacy.roleKey, legacy.variant]).toEqual([null, "boss"]);
    const gate = laneAgentOf({ ran_as: { agent: "human", how: "named" } }, null, "ceo");
    expect(gate && [gate.roleKey, gate.variant]).toEqual([null, "person"]);
    const theirs = laneAgentOf({ ran_as: { agent: "cto", how: "run_actor" } }, null, "ceo");
    expect(theirs && [theirs.roleKey, theirs.variant]).toEqual([null, "person"]);

    let el = render(<StepNode mode="run" view={VIEW("auto")} run={{ phase: "done", meta: "", agent: runner }} />);
    expect(el.querySelector(".wf-node-agent-name")?.textContent).toBe("BOSS");
    expect(el.querySelector('[data-morph="true"]')).toBeNull();
    act(() => root?.unmount());
    el = render(<StepNode mode="run" view={VIEW("you")} run={{ phase: "done", meta: "", agent: gate }} />);
    expect(el.querySelector(".wf-node-agent-name")?.textContent).toBe("A person");
    expect(el.querySelector(".ag-face")?.getAttribute("data-variant")).toBe("person");
    act(() => root?.unmount());
    el = render(<StepNode mode="run" view={VIEW("you")} run={{ phase: "done", meta: "", agent: theirs }} />);
    expect(el.querySelector(".wf-node-agent-name")?.textContent).toBe("A person");
  });

  it("rings a named agent with its work's real state", () => {
    const a = toAct(wireAct({ kind: "step", role_key: "qa", status: "awaiting_input", step_id: "s" }));
    const el = render(<StepNode mode="run" view={VIEW("named", "qa")}
                                run={{ phase: "needs_you", meta: "", agent: laneAgentOf({}, a, "ceo") }} />);
    expect(el.querySelector('.ag-face')?.getAttribute("data-state")).toBe("needs_you");
  });
});

/* ── The run view, mounted ── */

function step(id: string, capability: string, status = "succeeded", output: Record<string, unknown> = {}) {
  return { step_id: id, kind: "capability", status, capability, output, evidence: [], error: status === "failed" ? "pytest exited 1" : null,
           approval: {}, due_at: null, timeout_applied: null, attempt: 1, started_at: null, completed_at: null, decision_id: null };
}
const RUN = {
  id: "wfr_1", status: "running", standing: standingOf("running"), workflow_key: "ship_it", workflow_name: "Ship it",
  version: 1, step_count: 3, steps_done: 1, current_step: "plan", waiting_on: null, blocked_reason: null, error: null,
  updated_at: null, actions: [], input: { title: "Ship it" }, finished: false,
  steps: [
    step("prd", "write_prd", "succeeded", { ran_as: { agent: "pm", how: "named" } }),
    step("plan", "plan", "running", { ran_as: { agent: "chief_architect", how: "model" } }),
    step("deploy", "deploy", "pending"),
  ],
};
const TURNS = { act_id: "act_plan", turns_excerpt: [{ content: "Two options. I rejected the queue: it adds a broker we don't need." }] };

describe("the run view, signed", () => {
  let asked: string[] = [];
  beforeEach(() => {
    forgetRunDefinitions();
    asked = [];
    const chain = shipItChain();
    vi.stubGlobal("fetch", vi.fn(async (input: unknown) => {
      const url = new URL(String(input), "http://console.test");
      asked.push(`${url.pathname}?${url.searchParams.toString()}`);
      if (url.pathname === "/api/run-acts" && url.searchParams.get("view") === "chain") return Response.json(chain);
      if (url.pathname === "/api/run-acts" && url.searchParams.get("view") === "act") {
        const id = url.searchParams.get("act_id");
        const a = (chain.acts as Record<string, unknown>[]).find((x) => x.act_id === id);
        return Response.json({ ...a, ...(id === "act_plan" ? TURNS : {}) });
      }
      if (url.pathname.includes("host-advice")) {
        return Response.json({ show: true, severity: "warning", headline: "Disk nearly full", cause: "disk" });
      }
      return Response.json({});
    }));
  });

  async function mount() {
    host = document.createElement("div");
    document.body.appendChild(host);
    root = createRoot(host);
    const noop = () => {};
    await act(async () => root!.render(
      <RunsView runs={[RUN as never]} openRun="wfr_1" setOpenRun={noop} onAdvance={noop} busy={null}
                capabilities={[]} onRetryStep={noop} onSkipStep={noop} onRerun={noop} onStop={noop} onDelete={noop}
                onStopAndRerun={noop} onBuildOnThis={noop} onRecheck={noop} onSetTarget={noop} onPrepareTarget={noop}
                onRunPreview={noop} onBound={noop} projectId="p1" actor="ceo" roles={[]} targets={[]} hosts={[]} />));
    await settle();
    return host;
  }

  it("says the host's trouble once above the runs, not on every live card", async () => {
    host = document.createElement("div");
    document.body.appendChild(host);
    root = createRoot(host);
    const noop = () => {};
    const second = { ...RUN, id: "wfr_2" };
    await act(async () => root!.render(
      <RunsView runs={[RUN as never, second as never]} openRun={null} setOpenRun={noop} onAdvance={noop} busy={null}
                capabilities={[]} onRetryStep={noop} onSkipStep={noop} onRerun={noop} onStop={noop} onDelete={noop}
                onStopAndRerun={noop} onBuildOnThis={noop} onRecheck={noop} onSetTarget={noop} onPrepareTarget={noop}
                onRunPreview={noop} onBound={noop} projectId="p1" actor="ceo" roles={[]} targets={[]} hosts={[]} />));
    await settle();
    expect(host.querySelectorAll('[data-testid="host-advice"]')).toHaveLength(1);
    expect(host.querySelector('[data-testid="dw-run"] [data-testid="host-advice"]')).toBeNull();
  });

  it("draws the chain above the lane, and the lane's faces from the same work", async () => {
    const el = await mount();
    expect(asked.some((u) => u.startsWith("/api/run-acts?view=chain") && u.includes("run_id=wfr_1"))).toBe(true);
    const chain = el.querySelector('[data-testid="ag-chain"]');
    const lane = el.querySelector('[data-testid="dw-run-lane"]');
    expect(chain).not.toBeNull();
    expect(chain!.compareDocumentPosition(lane!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    const plan = el.querySelector('[data-step-id="plan"] [data-testid="wf-node-agent"]');
    expect(plan?.getAttribute("data-ran-as")).toBe("chief_architect");
    expect(el.querySelector('[data-testid="ag-credits-open"]')).not.toBeNull();
  });

  it("signs the open step's drawer, with what it said and Ask <Name>", async () => {
    const el = await mount();
    // ONE LINE FIRST: the agent's whole card is behind "Agent", not the panel's lead.
    expect(el.querySelector('[data-testid="ag-signed-by"]')).toBeNull();
    expect(el.querySelector('[data-testid="dw-step-doneby-name"]')?.textContent).toBe("Atlas");
    await act(async () => (el.querySelector('[data-testid="dw-step-agent-toggle"]') as HTMLButtonElement).click());
    await settle();
    const signed = el.querySelector('[data-testid="ag-signed-by"]')!;
    expect(signed).not.toBeNull();
    expect(signed.querySelector('[data-testid="ag-act-how-label"]')?.textContent).toBe("Picked by BOSS over 3");
    expect(signed.querySelector('[data-testid="ag-said"]')?.textContent).toContain("I rejected the queue");
    // The old unsigned ask is replaced by the agent's own.
    expect(el.querySelector('[data-testid="dw-ask-boss"]')).toBeNull();
    // ONE Ask, on the Done-by line: the card's own would be a second door to the same talk.
    expect(signed.querySelector('[data-testid="ag-signed-ask"]')).toBeNull();
    const ask = el.querySelector('[data-testid="dw-step-ask"]') as HTMLButtonElement;
    expect(ask.getAttribute("data-tip")).toBe("Ask Atlas about this step");
    // The 1:1 talk with that agent, about this step: its first reply is signed under this act.
    const opened: { kind: string; roleKey?: string; fresh?: boolean; context?: Record<string, string> }[] = [];
    const onOpen = (e: Event) => opened.push((e as CustomEvent<(typeof opened)[number]>).detail);
    window.addEventListener("boss:converse", onOpen);
    act(() => ask.click());
    window.removeEventListener("boss:converse", onOpen);
    expect(opened).toEqual([expect.objectContaining({
      kind: "talk", roleKey: "chief_architect", fresh: true,
      context: { from: "run_step", actId: "act_plan", workflowRunId: "wfr_1", stepId: "plan" },
    })]);
  });

  it("flags scope wider than the step that handed it over", () => {
    const chain = toChain(shipItChain());
    const build = chain.acts.find((a) => a.actId === "act_build")!;
    const el = render(<RunChain chain={{ ...chain, acts: [chain.acts[0], build] }} viewer="ceo" />);
    act(() => (el.querySelector('[data-act-id="act_build"][data-testid="ag-act-face"]') as HTMLButtonElement).click());
    const wide = document.querySelector('[data-testid="ag-act-wide"]');
    expect(wide?.textContent).toBe("Wider than its parent");
  });
});
