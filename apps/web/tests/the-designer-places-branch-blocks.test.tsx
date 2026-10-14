// @vitest-environment happy-dom
/**
 * The designer places Branch, Do at the same time and Repeat until, and each is set up in the
 * drawer, not in JSON.
 *
 * Mounted against the real store. What is pinned is what an operator does: picks a block from
 * the palette's Flow group; says what a Branch checks from parts (a value, a comparison, what to
 * compare it with), never by typing code; adds and removes a parallel's branches; says whether
 * the flow goes on when every branch or the first is done; repeats a step until (or while)
 * something holds, at most N times. Every control is one dispatch, so undo and the JSON follow.
 */
import React, { act, useReducer } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { EditorAction, EditorState, Palette, Spec } from "../lib/workflow-editor-types";

vi.mock("../lib/workflow-editor-types", async (orig) => {
  const real = await orig<typeof import("../lib/workflow-editor-types")>();
  const f = await import("./_helpers/workflow-editor-fakes");
  return { ...real, FlowLane: f.FakeFlowLane };
});

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const { Inspector } = await import("../app/workflows/editor/inspector/inspector");
const { StepPalette, paletteEntries } = await import("../app/workflows/editor/palette");
const { PALETTE, reducer, makeState, setValue, q } = await import("./_helpers/workflow-editor-fakes");
const { placeBlock } = await import("../lib/workflow-blocks");

const WITH_CONTROLS: Palette = { ...PALETTE, controls: [
  { kind: "condition", label: "Branch", summary: "Take a different path depending on what an earlier step produced." },
  { kind: "parallel", label: "Do at the same time", summary: "Split into branches that run together." },
  { kind: "bounded_loop", label: "Repeat until", summary: "Repeat a step until a condition holds." },
] };

const START: Spec = {
  entry_step: "draft", input_schema: { instructions: "string", team: "string" },
  steps: [{ id: "draft", kind: "capability", next: [],
            binding: { agent_work: true, produces: ["risk"],
                       inputs: { instructions: { from_input: "instructions" } } } }],
};
const placed = (block: "condition" | "parallel" | "bounded_loop") => placeBlock(START, -1, block);

let root: Root | null = null;
let host: HTMLElement | null = null;
const box: { state: EditorState | null; actions: EditorAction[] } = { state: null, actions: [] };

function Harness({ initial }: { initial: EditorState }) {
  const [state, raw] = useReducer(reducer, initial);
  box.state = state;
  const dispatch = (a: EditorAction) => { box.actions.push(a); raw(a); };
  return <Inspector state={state} palette={WITH_CONTROLS} dispatch={dispatch} projectId="p1"
                    refreshPalette={async () => undefined} onClose={() => dispatch({ type: "SELECT", id: null })} />;
}

async function mount(spec: Spec, selection: string) {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => root!.render(<Harness initial={{ ...makeState(spec), selection }} />));
  box.actions = [];
  return host;
}
const click = async (el: Element | null) => {
  expect(el).not.toBeNull();
  await act(async () => { (el as HTMLElement).click(); });
};
const set = async (el: Element | null, v: string) => {
  expect(el).not.toBeNull();
  await act(async () => { setValue(el as HTMLInputElement, v); });
};
const step = (id: string) => box.state!.spec.steps.find((s) => s.id === id)!;

afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  document.body.innerHTML = "";
  root = null; host = null; box.state = null; box.actions = [];
});

describe("the palette", () => {
  it("offers the three blocks under Flow, each a labelled button", async () => {
    const entries = paletteEntries(WITH_CONTROLS).filter((e) => e.key.startsWith("control:"));
    expect(entries.map((e) => [e.label, e.group, e.type])).toEqual([
      ["Branch", "flow", "condition"], ["Do at the same time", "flow", "parallel"],
      ["Repeat until", "flow", "loop"]]);
    host = document.createElement("div");
    document.body.appendChild(host);
    root = createRoot(host);
    const onPick = vi.fn();
    await act(async () => root!.render(
      <StepPalette palette={WITH_CONTROLS} onPick={onPick} onNewStep={vi.fn()} onEditStep={vi.fn()} />));
    const row = q<HTMLButtonElement>(host, "wf-pal-control:parallel")!;
    expect(row.tagName).toBe("BUTTON");
    expect(row.textContent).toContain("Do at the same time");
    await click(row);
    expect(onPick).toHaveBeenCalledWith({ control: "parallel" });
  });
});

describe("Branch", () => {
  it("is built from parts: a value, a comparison, what to compare it with", async () => {
    const { spec, head } = placed("condition");
    const el = await mount(spec, head!);
    expect(q(el, "wf-expr-empty")?.textContent).toBe("Nothing to check yet.");
    await click(q(el, "wf-expr-add"));
    expect(step("branch").expression).toEqual({ path: "input.instructions", op: "eq", value: "" });
    await set(q(el, "wf-expr-source-0"), "steps.draft.output.risk");
    await set(q(el, "wf-expr-kind-0"), "number");
    await set(q(el, "wf-expr-op-0"), "gte");
    await set(q(el, "wf-expr-value-0"), "3");
    expect(step("branch").expression).toEqual({ path: "steps.draft.output.risk", op: "gte", value: 3 });
    // A second check offers all / any.
    await click(q(el, "wf-expr-add"));
    await set(q(el, "wf-expr-op-1"), "in");
    await set(q(el, "wf-expr-source-1"), "input.team");
    await set(q(el, "wf-expr-value-1"), "billing, support");
    await click(q(el, "wf-expr-any"));
    expect(step("branch").expression).toEqual({ any: [
      { path: "steps.draft.output.risk", op: "gte", value: 3 },
      { path: "input.team", op: "in", value: ["billing", "support"] }] });
    // Every control is named for a screen reader.
    for (const id of ["wf-expr-source-1", "wf-expr-op-1", "wf-expr-value-1", "wf-expr-remove-1"]) {
      expect(q(el, id)!.getAttribute("aria-label")).toMatch(/^(Check 2: |Remove check 2)/);
    }
    await click(q(el, "wf-expr-remove-0"));
    expect(step("branch").expression).toEqual({ path: "input.team", op: "in", value: ["billing", "support"] });
  });

  it("names each path's first step, and opens it", async () => {
    const { spec, head } = placed("condition");
    const el = await mount(spec, head!);
    expect(q(el, "wf-path-yes")!.textContent).toBe("If yes");
    expect(q(el, "wf-path-no")!.textContent).toBe("If no");
    await click(q(el, "wf-path-no"));
    expect(box.actions).toEqual([{ type: "SELECT", id: "if_no" }]);
  });

  it("a condition written as groups inside groups opens as JSON, as written", async () => {
    const { spec, head } = placed("condition");
    const nested = { all: [{ any: [{ path: "input.a" }, { path: "input.b" }] }] };
    const written: Spec = { ...spec, steps: spec.steps.map((s) => (s.id === head ? { ...s, expression: nested } : s)) };
    const el = await mount(written, head!);
    expect(q(el, "wf-expr")).toBeNull();
    expect(JSON.parse(q<HTMLTextAreaElement>(el, "wf-expr-json-text")!.value)).toEqual(nested);
  });

  it("Remove on its head takes the whole block, and says so", async () => {
    const { spec, head } = placed("condition");
    const el = await mount(spec, head!);
    await click(el.querySelector('button[aria-label="Step actions"]'));
    const remove = document.querySelector<HTMLButtonElement>('[data-testid="wf-step-remove"]')!;
    expect(remove.textContent).toBe("Remove with its paths");
    await click(remove);
    expect(box.state!.spec.steps.map((s) => s.id)).toEqual(["draft"]);
  });
});

describe("Do at the same time", () => {
  it("adds a branch, and removes one while more than two are left", async () => {
    const { spec, head } = placed("parallel");
    const el = await mount(spec, head!);
    expect(q(el, "wf-branch-remove-0")).toBeNull();
    await click(q(el, "wf-branch-add"));
    expect(box.actions).toEqual([{ type: "ADD_BRANCH", id: head }]);
    expect(step("wait_for_branches").wait_for).toEqual(["branch_1", "branch_2", "branch_3"]);
    // The new branch is selected; back on the parallel, any branch can go now.
    const grown = box.state!.spec;
    act(() => root?.unmount());
    host?.remove();
    const again = await mount(grown, head!);
    await click(q(again, "wf-branch-remove-1"));
    expect(box.state!.spec.steps.some((s) => s.id === "branch_2")).toBe(false);
  });

  it("the join goes on when every branch is done, or the first", async () => {
    const { spec } = placed("parallel");
    const el = await mount(spec, "wait_for_branches");
    expect(q(el, "wf-join-all")!.getAttribute("aria-pressed")).toBe("true");
    await click(q(el, "wf-join-any"));
    expect(step("wait_for_branches").success_policy).toBe("any");
  });
});

describe("Repeat until", () => {
  it("repeats until a check passes (the engine's not), or while it holds, at most N times", async () => {
    const { spec, head } = placed("bounded_loop");
    const withMakes: Spec = { ...spec, steps: spec.steps.map((s) => (s.id === "repeat_step"
      ? { ...s, binding: { ...(s.binding ?? {}), produces: ["approved"] } } : s)) };
    const el = await mount(withMakes, head!);
    expect(q(el, "wf-loop-until")!.getAttribute("aria-pressed")).toBe("true");
    expect(q(el, "wf-loop-body")!.textContent).toBe("Repeat this");
    await click(q(el, "wf-expr-add"));
    await set(q(el, "wf-expr-source-0"), "context.approved");
    await set(q(el, "wf-expr-kind-0"), "yesno");
    expect(step("repeat").expression).toEqual({ not: { path: "context.approved", op: "eq", value: true } });
    await click(q(el, "wf-loop-while"));
    expect(step("repeat").expression).toEqual({ path: "context.approved", op: "eq", value: true });
    await set(q(el, "wf-loop-max"), "7");
    expect(step("repeat").max_iterations).toBe(7);
    await set(q(el, "wf-loop-max"), "500");
    expect(step("repeat").max_iterations).toBe(100);
  });
});
