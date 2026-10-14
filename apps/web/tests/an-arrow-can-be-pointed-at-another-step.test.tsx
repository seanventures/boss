// @vitest-environment happy-dom
/**
 * The workflow designer's arrows can be pointed at another step (#551).
 *
 *   1. THE MODEL (`rewireLink`). Each kind of arrow is one field of the spec: a step's `next`
 *      (its arrow to End too), a condition's `on_true`/`on_false`, a parallel's `branches`.
 *      Pointing it elsewhere rewrites that field and nothing else. What the spec cannot say the
 *      way the canvas draws it is refused with words: Start, End for a path, Yes and No to one
 *      step, a branch twice, into a join, a Repeat's own step, a path's arrow into its join.
 *   2. THE VALIDATOR DECIDES THE REST (`checkRewire`). A cycle outside a Repeat or a step nothing
 *      reaches any more is refused in the validator's words, only when the link would ADD the
 *      problem; a check that cannot be asked links nothing and says so.
 *   3. THE STORE. A link is one edit: one undo puts the arrow back; a refused one changes nothing.
 *   4. THE LANE. An arrow's end has a handle; dragged onto a step, End or Start it asks for that
 *      link; let go on nothing it asks for nothing. The handle is a button: from the keyboard it
 *      opens a list of where the arrow can go.
 *   5. THE EDITOR, end to end against a fake API whose check is the engine's graph rules: a
 *      drag links, Undo unlinks, a cycle and a path to End are refused out loud.
 */
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { CheckResult, FlowLink, Problem, Spec, SpecStep } from "../lib/workflow-editor-types";
import { fakeWorkflowApi, type FakeApi } from "./_helpers/fake-workflow-api";
import { BRANCHED, PALETTE } from "./_helpers/canvas-fixtures";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const h = React.createElement;
const { edgesOf, linksFrom, rewireLink, START_TARGET } = await import("../lib/workflow-graph");
const { addedProblems, checkRewire, CHECK_FAILED } = await import("../lib/workflow-rewire");
const { editorReducer, emptyEditorState, newMeta } = await import("../lib/workflow-editor-store");
const { FlowLane } = await import("../app/workflows/flow/flow-lane");
const { WorkflowEditor } = await import("../app/workflows/editor/workflow-editor");

const cap = (id: string, capability: string, next?: string[]): SpecStep =>
  ({ id, kind: "capability", binding: { capability }, ...(next ? { next } : {}) });

/** triage → ok? yes: hot → deploy · no: cold (which ends). */
const PATHS: Spec = {
  entry_step: "triage",
  steps: [
    cap("triage", "research", ["ok"]),
    { id: "ok", kind: "condition", expression: { path: "input.urgent", op: "truthy" }, on_true: "hot", on_false: "cold" },
    cap("hot", "build", ["deploy"]),
    cap("cold", "review"),
    cap("deploy", "deploy"),
  ],
};

/** The engine's graph rules (workflows.py `normalise_workflow_spec`, `_targets`): a missing
 *  target, a cycle from the entry, a step the entry cannot reach. Stands in for POST check. */
function engineCheck(spec: Spec): Problem[] {
  const byId = new Map(spec.steps.map((s) => [s.id, s]));
  const targets = (s: SpecStep): string[] => {
    const out = typeof s.next === "string" ? [s.next] : Array.isArray(s.next) ? s.next.map(String) : [];
    if (s.kind === "condition") out.push(String(s.on_true ?? ""), String(s.on_false ?? ""));
    if (s.kind === "parallel") { out.push(...((s.branches as string[]) ?? [])); if (s.join) out.push(String(s.join)); }
    if (s.kind === "bounded_loop" && s.body) out.push(String(s.body));
    return out.filter(Boolean);
  };
  const problems: Problem[] = [];
  for (const s of spec.steps) {
    for (const t of targets(s)) {
      if (!byId.has(t)) problems.push({ step_id: s.id, field: "next", message: `step '${s.id}' references missing step '${t}'.` });
    }
  }
  if (problems.length) return problems;
  const visiting = new Set<string>();
  const visited = new Set<string>();
  let cycle: string | null = null;
  const visit = (id: string) => {
    if (cycle) return;
    if (visiting.has(id)) { cycle = id; return; }
    if (visited.has(id)) return;
    visiting.add(id);
    for (const t of targets(byId.get(id)!)) visit(t);
    visiting.delete(id);
    visited.add(id);
  };
  visit(spec.entry_step ?? spec.steps[0].id);
  if (cycle) return [{ step_id: cycle, field: "next", message: `unbounded cycle detected at step '${cycle}'; use bounded_loop.` }];
  return spec.steps.filter((s) => !visited.has(s.id)).map((s) => ({ step_id: s.id, field: null,
    message: `step '${s.id}' cannot be reached from the entry step: no step leads to it.` }));
}
const result = (spec: Spec): CheckResult => ({ ok: !engineCheck(spec).length, problems: engineCheck(spec),
  summary: { steps: spec.steps.length, agents: [], approvals: [], typical_minutes: null, needs: [] } });

const step = (spec: Spec, id: string) => spec.steps.find((s) => s.id === id)!;
const relinked = (spec: Spec, link: FlowLink, target: string | null): Spec => {
  const out = rewireLink(spec, link, target);
  if (!("spec" in out)) throw new Error(`refused: ${out.refused}`);
  return out.spec;
};
const refusal = (spec: Spec, link: FlowLink, target: string | null) => {
  const out = rewireLink(spec, link, target);
  return "refused" in out ? out.refused : null;
};

/* ── 1. The model ──────────────────────────────────────────────────────────────── */

describe("each kind of arrow is one field of the spec", () => {
  it("lists a step's arrows: its edges, or its arrow to End", () => {
    expect(linksFrom(PATHS, "ok")).toEqual([
      { from: "ok", to: "hot", kind: "yes" }, { from: "ok", to: "cold", kind: "no" }]);
    expect(linksFrom(PATHS, "cold")).toEqual([{ from: "cold", to: null, kind: "end" }]);
  });

  it("a path's end pointed at a step joins it there: the paths of a Branch meet again (#550)", () => {
    const out = relinked(PATHS, { from: "cold", to: null, kind: "end" }, "deploy");
    expect(step(out, "cold").next).toEqual(["deploy"]);
    expect(edgesOf(out)).toContainEqual({ from: "cold", to: "deploy", kind: "next" });
    // Nothing else moved.
    expect(out.steps.filter((s) => s.id !== "cold")).toEqual(PATHS.steps.filter((s) => s.id !== "cold"));
  });

  it("a next is rewritten in the shape it was written in, and to End it is dropped", () => {
    const written: Spec = { ...PATHS, steps: PATHS.steps.map((s) => (s.id === "hot" ? { ...s, next: "deploy" } : s)) };
    expect(step(relinked(written, { from: "hot", to: "deploy", kind: "next" }, "cold"), "hot").next).toBe("cold");
    expect(step(relinked(PATHS, { from: "hot", to: "deploy", kind: "next" }, null), "hot").next).toEqual([]);
    const fan: Spec = { steps: [cap("a", "research", ["b", "c"]), cap("b", "review"), cap("c", "test"), cap("d", "notify")] };
    expect(step(relinked(fan, { from: "a", to: "c", kind: "next" }, "d"), "a").next).toEqual(["b", "d"]);
  });

  it("Yes and No are the condition's on_true and on_false", () => {
    expect(step(relinked(PATHS, { from: "ok", to: "hot", kind: "yes" }, "deploy"), "ok"))
      .toMatchObject({ on_true: "deploy", on_false: "cold" });
    expect(step(relinked(PATHS, { from: "ok", to: "cold", kind: "no" }, "deploy"), "ok"))
      .toMatchObject({ on_true: "hot", on_false: "deploy" });
  });

  it("a branch is its place in the parallel's branches", () => {
    const spec: Spec = { ...BRANCHED, steps: [...BRANCHED.steps, cap("lint", "review", ["gather"])] };
    expect(step(relinked(spec, { from: "fan", to: "test", kind: "branch" }, "lint"), "fan").branches)
      .toEqual(["review", "lint"]);
  });

  it("where it already goes is no change at all: the same spec", () => {
    expect(rewireLink(PATHS, { from: "ok", to: "hot", kind: "yes" }, "hot")).toEqual({ spec: PATHS });
    expect((rewireLink(PATHS, { from: "ok", to: "hot", kind: "yes" }, "hot") as { spec: Spec }).spec).toBe(PATHS);
  });

  it("refuses, with words, what the spec cannot say the way the canvas draws it", () => {
    const yes: FlowLink = { from: "ok", to: "hot", kind: "yes" };
    expect(refusal(PATHS, yes, START_TARGET)).toMatch(/Start/);
    expect(refusal(PATHS, yes, null)).toBe("Each path of a Branch needs a step to go to");
    expect(refusal(PATHS, yes, "cold")).toMatch(/Yes and No would both go to the same step/);
    expect(refusal(PATHS, yes, "nowhere")).toBe("Link it to a step, or to End");
    expect(refusal(PATHS, { from: "ok", to: "deploy", kind: "yes" }, "cold")).toBe("That arrow is not there any more");
    expect(refusal(PATHS, { from: "triage", to: "ok", kind: "next" }, "ok")).toBeNull();
    const fan: Spec = { steps: [cap("a", "research", ["b", "c"]), cap("b", "review"), cap("c", "test")] };
    expect(refusal(fan, { from: "a", to: "b", kind: "next" }, "c")).toBe("It already leads there");

    const branch: FlowLink = { from: "fan", to: "review", kind: "branch" };
    expect(refusal(BRANCHED, branch, null)).toBe("Each branch needs a step of its own");
    expect(refusal(BRANCHED, branch, "test")).toBe("That step is already one of its branches");
    expect(refusal(BRANCHED, branch, "gather")).toMatch(/lead into a join/);
    expect(refusal(BRANCHED, { from: "research", to: "fan", kind: "next" }, "gather")).toMatch(/lead into a join/);
    expect(refusal(BRANCHED, { from: "review", to: "gather", kind: "join" }, "deploy")).toMatch(/Do at the same time waits/);

    const loop: Spec = { entry_step: "again", steps: [
      { id: "again", kind: "bounded_loop", body: "build", max_iterations: 3, expression: {}, next: ["ship"] },
      cap("build", "build"), cap("ship", "deploy")] };
    expect(refusal(loop, { from: "again", to: "build", kind: "loop" }, "ship")).toMatch(/Repeat keeps its one step/);
    // A Repeat's way on is a plain next, and can go elsewhere; its step returns to it, and has no arrow of its own.
    expect(refusal(loop, { from: "again", to: "ship", kind: "next" }, null)).toBeNull();
    expect(linksFrom(loop, "build")).toEqual([]);
  });

  it("leaves cycles and unreachable steps to the validator: the model does not judge them", () => {
    expect(refusal(PATHS, { from: "hot", to: "deploy", kind: "next" }, "triage")).toBeNull();
    expect(refusal(PATHS, { from: "ok", to: "hot", kind: "yes" }, "deploy")).toBeNull();
  });
});

/* ── 2. The validator decides the rest ──────────────────────────────────────────── */

describe("the validator is asked before a link is made", () => {
  let check = vi.fn(async (spec: Spec) => result(spec));
  beforeEach(() => { check = vi.fn(async (spec: Spec) => result(spec)); });

  it("a link the validator is happy with is made", async () => {
    const answer = await checkRewire(PATHS, { from: "cold", to: null, kind: "end" }, "deploy", check);
    expect(answer.ok).toBe(true);
    expect(answer.ok && step(answer.spec, "cold").next).toEqual(["deploy"]);
  });

  it("a cycle outside a Repeat is refused in the validator's words", async () => {
    const answer = await checkRewire(PATHS, { from: "hot", to: "deploy", kind: "next" }, "triage", check);
    expect(answer).toEqual({ ok: false, reason: "Unbounded cycle detected at step 'triage'; use bounded_loop" });
  });

  it("a link that leaves a step unreached (orphans it) is refused", async () => {
    const answer = await checkRewire(PATHS, { from: "ok", to: "hot", kind: "yes" }, "deploy", check);
    expect(answer.ok).toBe(false);
    expect(!answer.ok && answer.reason).toMatch(/^Step 'hot' cannot be reached/);
    const end = await checkRewire(PATHS, { from: "hot", to: "deploy", kind: "next" }, null, check);
    expect(!end.ok && end.reason).toMatch(/^Step 'deploy' cannot be reached/);
  });

  it("only problems the link would ADD refuse it; the draft's own do not, nor do warnings", () => {
    const old: Problem = { step_id: "ok", field: "expression", message: "condition step 'ok' requires a declarative expression." };
    const warn: Problem = { step_id: "hot", field: "captain", message: "not set up", severity: "warning" };
    expect(addedProblems([old], [old, warn])).toEqual([]);
    expect(addedProblems([old], [old, { ...old, step_id: "x" }])).toEqual([{ ...old, step_id: "x" }]);
  });

  it("uses the draft's last check when it has one: one request, for the flow as it would be", async () => {
    await checkRewire(PATHS, { from: "cold", to: null, kind: "end" }, "deploy", check, []);
    expect(check).toHaveBeenCalledTimes(1);
    await checkRewire(PATHS, { from: "cold", to: null, kind: "end" }, "deploy", check);
    expect(check).toHaveBeenCalledTimes(3);
  });

  it("a refusal of the model's asks nothing; a check that cannot be asked links nothing, and says so", async () => {
    expect(await checkRewire(PATHS, { from: "ok", to: "hot", kind: "yes" }, null, check))
      .toEqual({ ok: false, reason: "Each path of a Branch needs a step to go to" });
    expect(check).not.toHaveBeenCalled();
    const down = vi.fn(async () => { throw new Error("API returned 502"); });
    expect(await checkRewire(PATHS, { from: "cold", to: null, kind: "end" }, "deploy", down))
      .toEqual({ ok: false, reason: CHECK_FAILED });
  });
});

/* ── 3. The store ──────────────────────────────────────────────────────────────── */

describe("a link is one edit", () => {
  const loaded = () => editorReducer(emptyEditorState(),
    { type: "LOAD", spec: PATHS, meta: newMeta({ workflow_key: "paths" }) });

  it("REWIRE points the arrow; one UNDO puts it back; REDO again", () => {
    const s0 = loaded();
    const s1 = editorReducer(s0, { type: "REWIRE", link: { from: "cold", to: null, kind: "end" }, target: "deploy" });
    expect(step(s1.spec, "cold").next).toEqual(["deploy"]);
    expect(s1.past).toHaveLength(1);
    const s2 = editorReducer(s1, { type: "UNDO" });
    expect(s2.spec).toBe(PATHS);
    expect(step(editorReducer(s2, { type: "REDO" }).spec, "cold").next).toEqual(["deploy"]);
  });

  it("each kind is undone the same way", () => {
    for (const [link, target] of [
      [{ from: "ok", to: "hot", kind: "yes" }, "deploy"],
      [{ from: "ok", to: "cold", kind: "no" }, "deploy"],
      [{ from: "hot", to: "deploy", kind: "next" }, "cold"],
    ] as [FlowLink, string][]) {
      const s1 = editorReducer(loaded(), { type: "REWIRE", link, target });
      expect(s1.spec).not.toEqual(PATHS);
      expect(editorReducer(s1, { type: "UNDO" }).spec).toEqual(PATHS);
    }
    const spec: Spec = { ...BRANCHED, steps: [...BRANCHED.steps, cap("lint", "review", ["gather"])] };
    const b0 = editorReducer(emptyEditorState(), { type: "LOAD", spec, meta: newMeta({ workflow_key: "b" }) });
    const b1 = editorReducer(b0, { type: "REWIRE", link: { from: "fan", to: "test", kind: "branch" }, target: "lint" });
    expect(step(b1.spec, "fan").branches).toEqual(["review", "lint"]);
    expect(editorReducer(b1, { type: "UNDO" }).spec).toBe(spec);
  });

  it("a refused link changes nothing and leaves nothing to undo", () => {
    const s0 = loaded();
    expect(editorReducer(s0, { type: "REWIRE", link: { from: "ok", to: "hot", kind: "yes" }, target: null })).toBe(s0);
    expect(editorReducer(s0, { type: "REWIRE", link: { from: "ok", to: "hot", kind: "yes" }, target: START_TARGET })).toBe(s0);
  });
});

/* ── 4. The lane ───────────────────────────────────────────────────────────────── */

const WIDE = 4000;
let root: Root | null = null;
let host: HTMLElement | null = null;
const saved: Record<string, PropertyDescriptor | undefined> = {};

function stub(name: "clientWidth" | "scrollWidth", get: (el: HTMLElement) => number) {
  saved[name] ??= Object.getOwnPropertyDescriptor(HTMLElement.prototype, name);
  Object.defineProperty(HTMLElement.prototype, name, { configurable: true, get() { return get(this as HTMLElement); } });
}

function mountHost() {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
}

afterEach(async () => {
  await new Promise((r) => setTimeout(r, 0));
  act(() => root?.unmount());
  host?.remove();
  root = null; host = null;
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  for (const [name, d] of Object.entries(saved)) {
    if (d) Object.defineProperty(HTMLElement.prototype, name, d);
    else delete (HTMLElement.prototype as unknown as Record<string, unknown>)[name];
    delete saved[name];
  }
  document.querySelectorAll(".wf-pop, .wf-tip").forEach((n) => n.remove());
});

function pointer(type: string, target: EventTarget, x: number, y: number) {
  act(() => {
    target.dispatchEvent(new PointerEvent(type, {
      clientX: x, clientY: y, pointerId: 1, button: 0, pointerType: "mouse", bubbles: true, cancelable: true,
    }));
  });
}
const byTest = (root: ParentNode, id: string) => root.querySelector<HTMLElement>(`[data-testid="${id}"]`);
const scaleOf = (lane: HTMLElement) => Number(lane.querySelector<HTMLElement>(".wf-lane-canvas")!.dataset.scale) || 1;
/** The page point of a canvas point (the canvas's rect sits at 0,0 here). */
const page = (lane: HTMLElement, x: number, y: number) => ({ x: x * scaleOf(lane), y: y * scaleOf(lane) });
const gripAt = (lane: HTMLElement, key: string) => {
  const el = byTest(lane, `wf-edge-end-${key}`)!;
  return page(lane, parseFloat(el.style.left), parseFloat(el.style.top));
};
const centreOf = (lane: HTMLElement, sel: string) => {
  const el = lane.querySelector<HTMLElement>(sel)!;
  return page(lane, parseFloat(el.style.left) + parseFloat(el.style.width) / 2,
              parseFloat(el.style.top) + parseFloat(el.style.height) / 2);
};
/** Drag an arrow's end from its handle to a page point and let go. */
function dragEnd(lane: HTMLElement, key: string, to: { x: number; y: number }, before?: () => void) {
  const from = gripAt(lane, key);
  pointer("pointerdown", byTest(lane, `wf-edge-end-${key}`)!, from.x, from.y);
  pointer("pointermove", document.body, (from.x + to.x) / 2, (from.y + to.y) / 2);
  pointer("pointermove", document.body, to.x, to.y);
  before?.();
  pointer("pointerup", document.body, to.x, to.y);
}

describe("on the lane, an arrow's end is a handle", () => {
  beforeEach(() => {
    stub("clientWidth", (el) => (el.classList?.contains("wf-lane-viewport") ? WIDE : 0));
    stub("scrollWidth", (el) => (el.classList?.contains("wf-lane-viewport") ? WIDE : 0));
    mountHost();
  });
  const lane = (props: Record<string, unknown> = {}) => {
    act(() => root!.render(h(FlowLane, { spec: PATHS, palette: PALETTE, mode: "design", ...props })));
    return host!;
  };

  it("every arrow that can go elsewhere has one; a Repeat's step and a path into its join have none", () => {
    const el = lane({ onRewire: vi.fn() });
    const keys = [...el.querySelectorAll<HTMLElement>(".wf-edge-end")].map((b) => b.dataset.testid);
    expect(keys.sort()).toEqual(["wf-edge-end-cold>end", "wf-edge-end-deploy>end", "wf-edge-end-next:hot>deploy",
      "wf-edge-end-next:triage>ok", "wf-edge-end-no:ok>cold", "wf-edge-end-yes:ok>hot"].sort());
    act(() => root!.render(h(FlowLane, { spec: BRANCHED, palette: PALETTE, mode: "design", onRewire: vi.fn() })));
    const kinds = new Set([...host!.querySelectorAll<HTMLElement>(".wf-edge-end")].map((b) => b.dataset.kind));
    expect(kinds.has("join")).toBe(false);
    expect(kinds.has("branch")).toBe(true);
    // Read-only, or with nobody to ask: no handles at all.
    act(() => root!.render(h(FlowLane, { spec: PATHS, palette: PALETTE, mode: "design", onRewire: vi.fn(), readOnly: true })));
    expect(host!.querySelectorAll(".wf-edge-end")).toHaveLength(0);
    act(() => root!.render(h(FlowLane, { spec: PATHS, palette: PALETTE, mode: "design" })));
    expect(host!.querySelectorAll(".wf-edge-end")).toHaveLength(0);
  });

  it("dragged onto a step, it asks for that link; the line follows the pointer and the step says whether it takes it", () => {
    const onRewire = vi.fn();
    const el = lane({ onRewire });
    dragEnd(el, "cold>end", centreOf(el, '.wf-slot[data-slot="deploy"]'), () => {
      expect(byTest(el, "wf-relink-line")).not.toBeNull();
      expect(el.querySelector('.wf-slot[data-slot="deploy"]')!.getAttribute("data-link-target")).toBe("ok");
      expect(byTest(el, "wf-relink-tip")!.textContent).toBe("Deploy");
    });
    expect(onRewire).toHaveBeenCalledTimes(1);
    expect(onRewire).toHaveBeenCalledWith({ from: "cold", to: null, kind: "end" }, "deploy");
    expect(byTest(el, "wf-relink-line")).toBeNull();
  });

  it("a step the spec cannot link to says so while the end is over it", () => {
    const el = lane({ onRewire: vi.fn() });
    dragEnd(el, "yes:ok>hot", centreOf(el, '.wf-slot[data-slot="cold"]'), () => {
      expect(el.querySelector('.wf-slot[data-slot="cold"]')!.getAttribute("data-link-target")).toBe("no");
    });
  });

  it("onto End or Start it asks for those (the editor refuses Start); onto nothing, for nothing", () => {
    const onRewire = vi.fn();
    const el = lane({ onRewire });
    dragEnd(el, "next:hot>deploy", centreOf(el, ".wf-slot-end"));
    expect(onRewire).toHaveBeenLastCalledWith({ from: "hot", to: "deploy", kind: "next" }, null);
    dragEnd(el, "next:hot>deploy", centreOf(el, '[data-testid="wf-start"]'));
    expect(onRewire).toHaveBeenLastCalledWith({ from: "hot", to: "deploy", kind: "next" }, START_TARGET);
    onRewire.mockClear();
    dragEnd(el, "next:hot>deploy", page(el, 3000, 5));
    expect(onRewire).not.toHaveBeenCalled();
    expect(byTest(el, "wf-announce")!.textContent).toMatch(/unchanged/);
    // A press without a drag is a click, not a link.
    const at = gripAt(el, "next:hot>deploy");
    pointer("pointerdown", byTest(el, "wf-edge-end-next:hot>deploy")!, at.x, at.y);
    pointer("pointerup", document.body, at.x, at.y);
    expect(onRewire).not.toHaveBeenCalled();
  });

  it("from the keyboard: the selected step's handles are tab stops, and Enter opens a list of where it can go", () => {
    const onRewire = vi.fn();
    const el = lane({ onRewire, selection: "ok" });
    const yes = byTest(el, "wf-edge-end-yes:ok>hot")!;
    expect(yes.tabIndex).toBe(0);
    expect(yes.hasAttribute("data-on")).toBe(true);
    expect(byTest(el, "wf-edge-end-next:hot>deploy")!.tabIndex).toBe(-1);
    expect(yes.getAttribute("aria-label")).toMatch(/^The Yes path from .+ to .+press Enter to choose where it goes$/);
    act(() => yes.click());
    const list = byTest(el, "wf-link-target") as HTMLSelectElement;
    expect(document.activeElement).toBe(list);
    expect(list.value).toBe("hot");
    expect([...list.options].map((o) => o.value)).toEqual(["triage", "hot", "cold", "deploy", "__end__"]);
    act(() => {
      list.value = "deploy";
      list.dispatchEvent(new Event("change", { bubbles: true }));
    });
    expect(onRewire).toHaveBeenCalledWith({ from: "ok", to: "hot", kind: "yes" }, "deploy");
    expect(byTest(el, "wf-link-picker")).toBeNull();
    // Escape closes it without linking, back on the handle.
    act(() => yes.click());
    act(() => { byTest(el, "wf-link-target")!.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })); });
    expect(byTest(el, "wf-link-picker")).toBeNull();
    expect(document.activeElement).toBe(byTest(el, "wf-edge-end-yes:ok>hot"));
    expect(onRewire).toHaveBeenCalledTimes(1);
  });

  it("a click on the arrow itself picks it too", () => {
    const el = lane({ onRewire: vi.fn() });
    act(() => { byTest(el, "wf-edge-hit-no:ok>cold")!.dispatchEvent(new MouseEvent("click", { bubbles: true })); });
    expect(byTest(el, "wf-link-picker")).not.toBeNull();
    expect(el.querySelector('g[data-edge="no:ok>cold"]')!.hasAttribute("data-picked")).toBe(true);
  });
});

/* ── 5. The editor, end to end ─────────────────────────────────────────────────── */

describe("in the editor", () => {
  const OWN = { definition_id: "wfd_own", workflow_key: "paths", name: "Paths", is_catalogue: false, version: 1 };
  let api: FakeApi;
  let checks: Spec[];

  beforeEach(() => {
    stub("clientWidth", (el) => (el.classList?.contains("wf-lane-viewport") ? WIDE : 0));
    window.history.replaceState(null, "", "/?area=delivery_workflows&wf=paths");
    api = fakeWorkflowApi(PALETTE, [{ ...OWN, spec: PATHS }]);
    checks = [];
    vi.stubGlobal("fetch", vi.fn(async (input: unknown, init?: RequestInit) => {
      const body = init?.body ? JSON.parse(String(init.body)) as { action?: string; spec?: Spec } : {};
      if (body.action === "check" && body.spec) {
        checks.push(body.spec);
        return Response.json({ ok: !engineCheck(body.spec).length, problems: engineCheck(body.spec),
                               summary: { steps: body.spec.steps.length, agents: [], approvals: [], typical_minutes: null, needs: [] } });
      }
      return api.fetch(input, init);
    }));
    try { window.localStorage.clear(); } catch { /* none */ }
    mountHost();
  });

  async function settle(rounds = 10) {
    for (let i = 0; i < rounds; i += 1) await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
  }
  const laneEl = () => document.querySelector<HTMLElement>('[data-testid="wf-lane"]')!;
  const toast = () => byTest(document, "wf-toast")?.textContent ?? "";
  const edgeKeys = () => [...document.querySelectorAll<SVGGElement>("g[data-edge]")].map((g) => g.dataset.edge);

  it("a drag links, the validator having been asked; Undo unlinks", async () => {
    act(() => root!.render(h(WorkflowEditor, { projectId: "prj_1", workflowKey: "paths", onBack: vi.fn(), onStart: vi.fn() })));
    await settle();
    const lane = laneEl();
    dragEnd(lane, "cold>end", centreOf(lane, '.wf-slot[data-slot="deploy"]'));
    await settle();
    expect(checks.some((s) => (s.steps.find((x) => x.id === "cold")?.next as string[] | undefined)?.[0] === "deploy")).toBe(true);
    expect(edgeKeys()).toContain("next:cold>deploy");
    expect(toast()).toContain("Link changed");
    await act(async () => { byTest(document, "wf-toast-undo")!.click(); });
    await settle();
    expect(edgeKeys()).not.toContain("next:cold>deploy");
    expect(edgeKeys()).toContain("cold>end");
  });

  it("a cycle is refused in the validator's words, and nothing changes", async () => {
    act(() => root!.render(h(WorkflowEditor, { projectId: "prj_1", workflowKey: "paths", onBack: vi.fn(), onStart: vi.fn() })));
    await settle();
    const lane = laneEl();
    const before = edgeKeys();
    dragEnd(lane, "next:hot>deploy", centreOf(lane, '.wf-slot[data-slot="triage"]'));
    await settle();
    expect(toast()).toMatch(/cycle detected at step 'triage'/);
    expect(edgeKeys()).toEqual(before);
    expect(byTest(document, "wf-toast-undo")).toBeNull();
  });

  it("a path sent to End, or an arrow to Start, is refused without asking", async () => {
    act(() => root!.render(h(WorkflowEditor, { projectId: "prj_1", workflowKey: "paths", onBack: vi.fn(), onStart: vi.fn() })));
    await settle();
    const lane = laneEl();
    const asked = checks.length;
    dragEnd(lane, "yes:ok>hot", centreOf(lane, ".wf-slot-end"));
    await settle(3);
    expect(toast()).toBe("Each path of a Branch needs a step to go to");
    dragEnd(lane, "next:hot>deploy", centreOf(lane, '[data-testid="wf-start"]'));
    await settle(3);
    expect(toast()).toMatch(/Nothing can lead back to Start/);
    expect(checks.length).toBe(asked);
  });
});

/* ── 6. The `+` of a branched flow sits on its arrow, not against a step ───────────── */

describe("a + at a path's head or end", () => {
  beforeEach(() => {
    stub("clientWidth", (el) => (el.classList?.contains("wf-lane-viewport") ? WIDE : 0));
    mountHost();
  });

  it("sits on the middle of the arrow into (or out of) it, clear of every step, even in Compact", async () => {
    const { placeBlock } = await import("../lib/workflow-blocks");
    const { afterGap, linearOrder } = await import("../lib/workflow-graph");
    const base: Spec = { entry_step: "a", steps: [cap("a", "research", [])] };
    const { spec } = placeBlock(base, -1, "condition");
    act(() => root!.render(h(FlowLane, { spec, palette: PALETTE, mode: "design", density: "compact", onInsert: vi.fn(),
                                         renderGapAction: (n: number) => h("button", { "data-at": n }, "+") })));
    const order = linearOrder(spec);
    const heads = [order.indexOf("if_yes"), order.indexOf("if_no")];
    const ends = heads.map((i) => afterGap(i));
    const boxes = [...host!.querySelectorAll<HTMLElement>(".wf-slot[data-slot]")].map((s) => ({
      x: parseFloat(s.style.left), y: parseFloat(s.style.top), w: parseFloat(s.style.width), h: parseFloat(s.style.height) }));
    const clearance = (x: number, y: number) => Math.min(...boxes.map((b) =>
      Math.hypot(Math.max(b.x - x, 0, x - b.x - b.w), Math.max(b.y - y, 0, y - b.y - b.h))));
    for (const at of [...heads, ...ends]) {
      const gap = host!.querySelector<HTMLElement>(`.wf-gap[data-at="${at}"]`);
      expect(gap, `gap ${at}`).not.toBeNull();
      // The + is 18px across in Compact: its centre at least that far from any step's edge.
      expect(clearance(parseFloat(gap!.style.left), parseFloat(gap!.style.top)), `gap ${at}`).toBeGreaterThanOrEqual(18);
    }
  });
});
