// @vitest-environment happy-dom
/**
 * A run's chain says each step ONCE, by who really did it (operator report, wfr_47b4e27e9a3c).
 *
 * The run card drew seven identical "A person" rows in a parallel column for one build step
 * retried six times, and every built-in step — the panel, the PRD gate, the build — as "A
 * person", because each is signed `run_actor` with no role. The server now resolves the real
 * actor (agent_acts.resolve_actors) and the web draws it:
 *
 *  - a retried step is one hop, by its latest attempt, with `· 7 attempts` that opens each one;
 *  - a build step is its builder, a panel its chair, a gate its approver by display name, and
 *    BOSS's own machinery is BOSS; "A person" only when nothing names the human;
 *  - the step's cost is every attempt's spend, with what a subscription covered said beside it.
 */
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";

import { wireAct } from "./_helpers/run-acts-fixtures";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
(globalThis as Record<string, unknown>).React = React;

const { toChain } = await import("../app/agents/act-wire");
const { chainItems, whoDid, laneAgentOf, agentKeys, spendLabel, creditWho, personName } =
  await import("../app/agents/chain-model");
const { nameOf } = await import("../app/agents/run-face");
const { RunChain } = await import("../app/agents/run-chain");
const { StepDoneBy } = await import("../app/workflows/step-run");

let root: Root | null = null;
let host: HTMLElement | null = null;
afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  root = null; host = null;
});
function render(node: React.ReactNode): HTMLElement {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  act(() => root!.render(node));
  return host;
}

const PERSON = "usr_operator";
const agent = (role_key: string) => ({ kind: "agent", role_key, person_id: null, name: null });
const BOSS = { kind: "boss", role_key: null, person_id: null, name: "BOSS" };

/** The operator's run, as the API now sends it. */
function operatorRun(): Record<string, unknown> {
  const acts: Record<string, unknown>[] = [];
  const at = (m: number) => `2026-10-11T${String(4 + Math.floor(m / 60)).padStart(2, "0")}:${String(m % 60).padStart(2, "0")}:00Z`;
  acts.push(wireAct({ act_id: "act_root", kind: "run", how: "started", handed_by_kind: "person", handed_by_id: PERSON,
    accountable: PERSON, status: "active", started_at: at(0), label: "HelpDesk",
    actor: { kind: "person", role_key: null, person_id: PERSON, name: "Param G" } }));
  acts.push(wireAct({ act_id: "act_meeting", kind: "step", how: "run_actor", parent_act_id: "act_root", step_id: "meeting",
    meeting_run_id: "mrun_1", accountable: PERSON, handed_by_kind: "person", handed_by_id: PERSON, started_at: at(1),
    label: "Meeting · HelpDesk", actor: { ...agent("pm"), panel: ["pm", "software_engineer", "design_critic"] } }));
  for (const [i, role] of ["pm", "software_engineer", "design_critic"].entries()) {
    acts.push(wireAct({ act_id: `act_seat_${i}`, kind: "seat", role_key: role, how: "seated", parent_act_id: "act_meeting",
      meeting_run_id: "mrun_1", started_at: at(1), label: "Panel · HelpDesk" }));
  }
  acts.push(wireAct({ act_id: "act_prd", kind: "step", how: "run_actor", parent_act_id: "act_meeting", step_id: "spec_review",
    decision_id: "dec_1", accountable: PERSON, started_at: at(5), label: "Spec review · HelpDesk",
    actor: { kind: "person", role_key: null, person_id: PERSON, name: "Param G" } }));
  // Seven attempts at one build: BOSS's auto-retries and the operator's. The second never
  // reached an agent (BOSS); the others were built by the software engineer.
  for (let n = 0; n < 7; n += 1) {
    const id = `act_build_${n}`;
    acts.push(wireAct({ act_id: id, kind: "step", how: "run_actor", parent_act_id: "act_prd", step_id: "build",
      code_task_run_id: `crun_${n}`, status: "error", accountable: PERSON, started_at: at(30 + n * 60),
      label: "Build · HelpDesk", cost_usd: 0, actor: n === 1 ? BOSS : agent("software_engineer") }));
    if (n === 1) continue;
    acts.push(wireAct({ act_id: `${id}_plan`, kind: "stage", role_key: "chief_architect", how: "next_stage",
      parent_act_id: id, step_id: "plan", started_at: at(31 + n * 60), label: "Plan · HelpDesk", cost_usd: 0,
      included_usd: 4 }));
    acts.push(wireAct({ act_id: `${id}_impl`, kind: "stage", role_key: "software_engineer", how: "next_stage",
      parent_act_id: id, step_id: "implement", started_at: at(32 + n * 60), label: "Implement · HelpDesk", cost_usd: 0,
      included_usd: 21 }));
  }
  return {
    acts, accountable: PERSON,
    totals: { steps: 9, minutes: 600, cost: 0, included: 150 },
    steps: { build: { cost_usd: 0, included_usd: 150, attempts: 7, act_ids: [], recorded: [0, 0] },
             meeting: { cost_usd: 4.68, included_usd: 0, attempts: 1, act_ids: ["act_meeting"], recorded: [4.68, 0] } },
    people: { [PERSON]: "Param G" },
    credits: [{ role_key: null, line: "Started the run · Approved" }, { role_key: "pm", line: "Panel" }],
  };
}

describe("a retried step", () => {
  it("is one hop, by its latest attempt, with every attempt folded under it", () => {
    const chain = toChain(operatorRun());
    const items = chainItems(chain, "someone_else");
    const hops = items.filter((h) => h.kind === "hop");
    // run → panel → PRD gate → build: four hops, nothing stacked as parallel.
    expect(hops).toHaveLength(4);
    expect(hops.every((h) => h.kind === "hop" && h.acts.length === 1)).toBe(true);
    const build = hops[3];
    if (build.kind !== "hop") throw new Error("not a hop");
    expect(build.acts[0].actId).toBe("act_build_6");
    expect(build.attempts?.map((h) => h.acts[0].actId)).toEqual(
      ["act_build_0", "act_build_1", "act_build_2", "act_build_3", "act_build_4", "act_build_5", "act_build_6"]);
    // Each attempt keeps its own inner work.
    expect(build.attempts?.[0].insideCount).toBe(2);
    expect(build.attempts?.[1].insideCount).toBe(0);
  });

  it("draws one face with the count, and opens into each attempt", () => {
    const html = render(<RunChain chain={toChain(operatorRun())} viewer="someone_else" />);
    expect(html.querySelectorAll('[data-stack="true"]')).toHaveLength(0);
    const tries = html.querySelector<HTMLButtonElement>('[data-testid="ag-chain-attempts"]')!;
    expect(tries.textContent).toContain("7 attempts");
    expect(html.querySelector('[data-testid="ag-chain-attempt-list"]')).toBeNull();
    act(() => tries.click());
    const list = html.querySelector('[data-testid="ag-chain-attempt-list"]')!;
    expect(list.textContent).toContain("Attempt 1");
    expect(list.textContent).toContain("Attempt 7");
    expect(list.textContent).toContain("BOSS");
  });
});

describe("who did each step", () => {
  const chain = toChain(operatorRun());
  const by = (id: string) => chain.acts.find((a) => a.actId === id)!;
  const label = (id: string, viewer = "someone_else") => {
    const w = whoDid(by(id), viewer);
    return nameOf(w.roleKey, w.variant, w.personId, w.name);
  };

  it("names the builder, the panel's chair, the approver and BOSS — never a blank person", () => {
    expect(whoDid(by("act_build_6"), "x")).toMatchObject({ roleKey: "software_engineer", variant: "agent" });
    expect(whoDid(by("act_meeting"), "x")).toMatchObject({ roleKey: "pm", variant: "agent" });
    expect(label("act_prd").name).toBe("Param G");
    expect(label("act_root").name).toBe("Param G");
    expect(label("act_build_1").name).toBe("BOSS");
    for (const id of ["act_root", "act_meeting", "act_prd", "act_build_6", "act_build_1"]) {
      expect(label(id).name, id).not.toBe("A person");
    }
  });

  it("says You to the person themselves", () => {
    expect(label("act_prd", PERSON).name).toBe("You");
  });

  it("keeps \"A person\" for a human nothing names", () => {
    const anon = toChain({ acts: [wireAct({ act_id: "a", kind: "approval", actor: { kind: "person", role_key: null } })] });
    const w = whoDid(anon.acts[0], "x");
    expect(nameOf(w.roleKey, w.variant, w.personId, w.name).name).toBe("A person");
  });

  it("puts the agent on the lane and in Done by", () => {
    const lane = laneAgentOf({}, by("act_build_6"), "x");
    expect(lane).toMatchObject({ roleKey: "software_engineer", variant: "agent" });
    const prd = laneAgentOf({}, by("act_prd"), "x");
    expect(prd).toMatchObject({ variant: "person", name: "Param G" });
  });

  it("counts the agents behind the work, builders included", () => {
    expect(agentKeys(chain).sort()).toEqual(["chief_architect", "design_critic", "pm", "software_engineer"]);
  });

  it("names people in Credits", () => {
    expect(personName(creditWho(chain.credits[0], chain.accountable, "", chain.people))).toBe("Param G");
  });
});

describe("what a step cost", () => {
  it("is every attempt's spend, and says what a subscription covered", () => {
    expect(spendLabel(0, 0)).toBe("$0.00");
    expect(spendLabel(0, 150)).toBe("$150 included");
    expect(spendLabel(4.68, 0)).toBe("$4.68");
    expect(spendLabel(2, 30)).toBe("$2.00 + $30 included");
  });

  it("shows the chain's per-step sum in Done by, not the latest attempt's $0.00", () => {
    const chain = toChain(operatorRun());
    const act6 = chain.acts.find((a) => a.actId === "act_build_6")!;
    const html = render(
      <StepDoneBy act={act6} output={{}} phase="failed" status="failed" viewer="x" projectId="p1"
                  question="" cost={0} calls={0} model="claude-sonnet-5" onAsk={() => {}} stepId="build"
                  facts={[]} spend={chain.steps!.build} />,
    );
    expect(html.querySelector('[data-testid="dw-step-cost"]')!.textContent).toBe("$150 included");
    expect(html.querySelector('[data-testid="dw-step-doneby-name"]')!.textContent).not.toBe("A person");
  });
});
