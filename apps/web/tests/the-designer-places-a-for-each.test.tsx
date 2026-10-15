// @vitest-environment happy-dom
/**
 * The designer places a For each: one step run once per item of a list (#512).
 *
 *   1. THE MODEL. `placeBlock(…, "for_each")` makes the engine's shape — a `for_each` naming its
 *      list (`items`, empty until the operator picks one), its one step (`body`, an agent task
 *      handed the item as `item`), at most 100 items, one at a time — and the head's arrow into
 *      its step is a `loop` edge like a Repeat's: its step returns to it and never follows `next`.
 *      Unlike a Repeat's, that arrow can be pointed at another step (`rewireLink`). Remove takes
 *      the whole block, and its step says to remove the For each instead.
 *   2. THE DRAWER. The list is picked from what the start form and earlier steps make (the
 *      condition builder's sources) or typed as a path; at most and at a time are clamped to the
 *      engine's limits (1–1000, 1–20).
 *   3. THE LANE. A For each is drawn as a stack of cards, and its arrow has a handle.
 *   4. A RUN says how far through its list a For each is: "3 of 10 done".
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
const h = React.createElement;

const { Inspector } = await import("../app/workflows/editor/inspector/inspector");
const { paletteEntries } = await import("../app/workflows/editor/palette");
const { PALETTE, reducer, makeState, setValue, q } = await import("./_helpers/workflow-editor-fakes");
const { placeBlock, removeBlock, removeRefusal, blockRegion } = await import("../lib/workflow-blocks");
const { edgesOf, linksFrom, loopBodies, rewireLink } = await import("../lib/workflow-graph");
const { stepType } = await import("../lib/step-view");
const { nodeShape } = await import("../app/workflows/flow/step-node");
const { forEachProgress } = await import("../lib/workflow-for-each");
const { laneStateOf } = await import("../app/delivery-workflows-ui");

const WITH_CONTROLS: Palette = { ...PALETTE, controls: [
  { kind: "bounded_loop", label: "Repeat until", summary: "Repeat a step until a condition holds." },
  { kind: "for_each", label: "For each", summary: "Run a step once for every item of a list." },
] };

const START: Spec = {
  entry_step: "draft", input_schema: { instructions: "string", urls: "string" },
  steps: [{ id: "draft", kind: "capability", next: [],
            binding: { agent_work: true, produces: ["pages"],
                       inputs: { instructions: { from_input: "instructions" } } } }],
};
const placed = () => placeBlock(START, -1, "for_each");

/* ── 1. The model ────────────────────────────────────────────────────────────────── */

describe("a For each, as the spec says it", () => {
  it("is placed whole: its list, its one step handed the item, at most 100, one at a time", () => {
    const { spec, head } = placed();
    expect(head).toBe("for_each");
    const each = spec.steps.find((s) => s.id === "for_each")!;
    expect(each).toMatchObject({ kind: "for_each", items: "", body: "each_item", max_items: 100,
                                 concurrency: 1, next: [] });
    expect(spec.steps.find((s) => s.id === "draft")!.next).toEqual(["for_each"]);
    const body = spec.steps.find((s) => s.id === "each_item")!;
    expect(body.next).toEqual([]);
    expect((body.binding as { inputs: Record<string, unknown> }).inputs.item).toEqual({ from_item: "" });
    expect(stepType(each)).toBe("foreach");
  });

  it("its arrow into its step is a loop, and the step returns to it", () => {
    const { spec } = placed();
    expect(edgesOf(spec)).toContainEqual({ from: "for_each", to: "each_item", kind: "loop" });
    expect(loopBodies(spec).has("each_item")).toBe(true);
    expect(linksFrom(spec, "each_item")).toEqual([]);
    expect(blockRegion(spec, "for_each")).toEqual({ inside: new Set(["for_each", "each_item"]), exit: null });
  });

  it("its arrow can be pointed at another step; a Repeat's still cannot", () => {
    const { spec } = placed();
    const withOther: Spec = { ...spec, steps: [...spec.steps, { id: "other", kind: "capability", next: [] }] };
    const link = { from: "for_each", to: "each_item", kind: "loop" as const };
    const out = rewireLink(withOther, link, "other");
    expect("spec" in out && out.spec.steps.find((s) => s.id === "for_each")!.body).toBe("other");
    expect(rewireLink(withOther, link, null)).toEqual({ refused: "A For each needs a step to run for each item" });
    expect(rewireLink(withOther, link, "for_each")).toEqual({ refused: "A For each cannot run itself for each item" });
    const loop = placeBlock(START, -1, "bounded_loop").spec;
    expect(rewireLink(loop, { from: "repeat", to: "repeat_step", kind: "loop" }, "draft"))
      .toEqual({ refused: "A Repeat keeps its one step: put a step in it instead" });
  });

  it("Remove on its head takes the whole block; on its step, says to remove the For each", () => {
    const { spec } = placed();
    const out = removeBlock(spec, "for_each");
    expect(out.steps.map((s) => s.id)).toEqual(["draft"]);
    expect(removeRefusal(spec, "each_item")).toBe("A For each needs its one step: remove the For each instead");
  });
});

/* ── 2. The drawer ───────────────────────────────────────────────────────────────── */

let root: Root | null = null;
let host: HTMLElement | null = null;
const box: { state: EditorState | null } = { state: null };

function Harness({ initial }: { initial: EditorState }) {
  const [state, raw] = useReducer(reducer, initial);
  box.state = state;
  const dispatch = (a: EditorAction) => raw(a);
  return <Inspector state={state} palette={WITH_CONTROLS} dispatch={dispatch} projectId="p1"
                    refreshPalette={async () => undefined} onClose={() => dispatch({ type: "SELECT", id: null })} />;
}

async function mount(spec: Spec, selection: string) {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => root!.render(<Harness initial={{ ...makeState(spec), selection }} />));
  return host;
}
const set = async (el: Element | null, v: string) => {
  expect(el).not.toBeNull();
  await act(async () => { setValue(el as HTMLInputElement, v); });
};
const each = () => box.state!.spec.steps.find((s) => s.id === "for_each")!;

afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  document.body.innerHTML = "";
  root = null; host = null; box.state = null;
});

describe("the palette", () => {
  it("offers For each under Flow", () => {
    const entries = paletteEntries(WITH_CONTROLS).filter((e) => e.key.startsWith("control:"));
    expect(entries.map((e) => [e.label, e.group, e.type])).toContainEqual(["For each", "flow", "foreach"]);
  });
});

describe("For each settings", () => {
  it("picks its list from what runs before it, or a path; clamps at most and at a time", async () => {
    const el = await mount(placed().spec, "for_each");
    expect(q(el, "wf-foreach-body")!.textContent).toBe("For each item");
    const options = [...q<HTMLSelectElement>(el, "wf-foreach-items")!.querySelectorAll("option")].map((o) => o.value);
    expect(options).toEqual(expect.arrayContaining(["input.urls", "steps.draft.output.pages", "__other__"]));
    await set(q(el, "wf-foreach-items"), "steps.draft.output.pages");
    expect(each().items).toBe("steps.draft.output.pages");
    await set(q(el, "wf-foreach-items"), "__other__");
    await set(q(el, "wf-foreach-items-path"), "input.urls");
    expect(each().items).toBe("input.urls");
    await set(q(el, "wf-foreach-max"), "250");
    expect(each().max_items).toBe(250);
    await set(q(el, "wf-foreach-max"), "5000");
    expect(each().max_items).toBe(1000);
    await set(q(el, "wf-foreach-at-once"), "3");
    expect(each().concurrency).toBe(3);
    await set(q(el, "wf-foreach-at-once"), "50");
    expect(each().concurrency).toBe(20);
  });

  it("a fixed list in the spec is shown, not lost", async () => {
    const { spec } = placed();
    const literal: Spec = { ...spec, steps: spec.steps.map((s) => (s.id === "for_each" ? { ...s, items: ["a", "b"] } : s)) };
    const el = await mount(literal, "for_each");
    expect(q(el, "wf-foreach-literal")!.textContent).toContain("2");
    expect(q(el, "wf-foreach-items")).toBeNull();
  });
});

/* ── 3. The lane, and 4. a run ───────────────────────────────────────────────────── */

describe("drawn and run", () => {
  it("is drawn as a stack of cards, and says so", () => {
    expect(nodeShape({ type: "foreach", icon: "foreach" })).toEqual({ shape: "stack", words: "For each" });
  });

  it("its arrow into its step has a handle on the lane; a Repeat's has none", async () => {
    const { FlowLane } = await vi.importActual<typeof import("../app/workflows/flow/flow-lane")>(
      "../app/workflows/flow/flow-lane");
    const { spec } = placed();
    host = document.createElement("div");
    document.body.appendChild(host);
    root = createRoot(host);
    act(() => root!.render(h(FlowLane, { spec, palette: WITH_CONTROLS, mode: "design", onRewire: vi.fn() })));
    const kinds = [...host.querySelectorAll<HTMLElement>(".wf-edge-end")].map((b) => b.dataset.testid);
    expect(kinds).toContain("wf-edge-end-loop:for_each>each_item");
    const loop = placeBlock(START, -1, "bounded_loop").spec;
    act(() => root!.render(h(FlowLane, { spec: loop, palette: WITH_CONTROLS, mode: "design", onRewire: vi.fn() })));
    expect([...host.querySelectorAll<HTMLElement>(".wf-edge-end")].map((b) => b.dataset.kind)).not.toContain("loop");
  });

  it("a run says how far through its list it is", () => {
    const going = { kind: "for_each", status: "waiting", output: { count: 10, done: 3 } };
    expect(forEachProgress(going)).toBe("3 of 10 done");
    expect(forEachProgress({ kind: "bounded_loop", status: "waiting", output: { count: 10, done: 3 } })).toBeNull();
    expect(forEachProgress({ kind: "for_each", status: "ready", output: {} })).toBeNull();
    const step = { step_id: "each", kind: "for_each", status: "waiting", capability: null,
                   output: { count: 10, done: 3 }, evidence: [], error: null, approval: {},
                   due_at: null, timeout_applied: null, attempt: 0, started_at: null, completed_at: null };
    expect(laneStateOf(step as unknown as Parameters<typeof laneStateOf>[0]))
      .toMatchObject({ phase: "moving", meta: "3 of 10 done" });
  });
});
