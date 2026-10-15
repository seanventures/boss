// @vitest-environment happy-dom
/**
 * A step says what happens when it fails (#512 phase 2): how many tries, the wait between them,
 * its time limit, and where the run goes when it still fails (`on_error`).
 *
 *   1. THE NUMBERS (lib/workflow-failure.ts). Each field's range is the engine's; the wait between
 *      tries is min(initial × multiplier^(n-1), max), said in plain words.
 *   2. THE SPEC. A field left empty is omitted, never written as its default.
 *   3. THE ERROR PATH IS AN ARROW. `on_error` is laid out (a step only an error path reaches sits
 *      after the step that fails), renamed and pruned with its target, and pointed elsewhere
 *      through `rewireLink` like any arrow: Start and the step itself are refused in words; a
 *      cycle is the validator's to refuse; one undo puts it back.
 *   4. THE SETTINGS PANEL. Each field writes its spec field; a value out of range is said under
 *      its box and not written; the preview says the waits.
 *   5. THE LANE. The error path is a dashed error arrow labelled "if it fails", said in words to a
 *      screen reader, with a handle like any arrow; a step with a time limit or extra tries wears a
 *      badge; on a run, a step that took its error path is marked.
 *   6. THE EDITOR, end to end: "If it still fails, go to…" asks the validator, draws the arrow, and
 *      Undo takes it away; a cycle is refused in the validator's words.
 */
import React, { act, useReducer } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { CheckResult, FlowLink, Problem, Spec, SpecStep } from "../lib/workflow-editor-types";
import { fakeWorkflowApi, type FakeApi } from "./_helpers/fake-workflow-api";
import { PALETTE } from "./_helpers/canvas-fixtures";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const h = React.createElement;
const {
  backoffWaits, failureBadge, readNumber, readTimeLimit, saySeconds, sayWaits, unitFor, withBackoff,
} = await import("../lib/workflow-failure");
const { applyStepPatch } = await import("../lib/workflow-spec");
const {
  edgesOf, errorEdgesOf, errorLinkOf, layoutFlow, linksFrom, removeStep, renameStepId, rewireLink, START_TARGET,
} = await import("../lib/workflow-graph");
const { checkRewire } = await import("../lib/workflow-rewire");
const { editorReducer, emptyEditorState, newMeta, parseSpecText, specText } = await import("../lib/workflow-editor-store");
const { FailureSettings } = await import("../app/workflows/editor/inspector/failure-settings");
const { FlowLane } = await import("../app/workflows/flow/flow-lane");
const { WorkflowEditor } = await import("../app/workflows/editor/workflow-editor");
const { laneStateOf } = await import("../app/delivery-workflows-ui");

const cap = (id: string, capability: string, next?: string[], extra: Partial<SpecStep> = {}): SpecStep =>
  ({ id, kind: "capability", binding: { capability }, ...(next ? { next } : {}), ...extra });

/** research → build → deploy; when build fails for good, the run goes on at tell (Notify). */
const FAILS: Spec = {
  entry_step: "research",
  steps: [
    cap("research", "research", ["build"]),
    cap("build", "build", ["deploy"], { on_error: "tell", max_attempts: 5, timeout_seconds: 600 }),
    cap("deploy", "deploy"),
    cap("tell", "notify"),
  ],
};
/** The same flow with no error path yet. */
const PLAIN: Spec = { ...FAILS, steps: FAILS.steps.map((s) => (s.id === "build" ? cap("build", "build", ["deploy"]) : s)) };

const step = (spec: Spec, id: string) => spec.steps.find((s) => s.id === id)!;
const refusal = (spec: Spec, link: FlowLink, target: string | null) => {
  const out = rewireLink(spec, link, target);
  return "refused" in out ? out.refused : null;
};
const relinked = (spec: Spec, link: FlowLink, target: string | null): Spec => {
  const out = rewireLink(spec, link, target);
  if (!("spec" in out)) throw new Error(`refused: ${out.refused}`);
  return out.spec;
};

/** The engine's graph rules with the error path as an edge (a missing target, a cycle from the
 *  entry, a step nothing reaches). Stands in for POST check. */
function engineCheck(spec: Spec): Problem[] {
  const byId = new Map(spec.steps.map((s) => [s.id, s]));
  const targets = (s: SpecStep): string[] => {
    const out = typeof s.next === "string" ? [s.next] : Array.isArray(s.next) ? s.next.map(String) : [];
    if (typeof s.on_error === "string") out.push(s.on_error);
    return out.filter(Boolean);
  };
  for (const s of spec.steps) {
    for (const t of targets(s)) {
      if (!byId.has(t)) return [{ step_id: s.id, field: "on_error", message: `step '${s.id}' references missing step '${t}'.` }];
    }
  }
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
  if (cycle) return [{ step_id: cycle, field: "on_error", message: `unbounded cycle detected at step '${cycle}'.` }];
  return spec.steps.filter((s) => !visited.has(s.id)).map((s) => ({ step_id: s.id, field: null,
    message: `step '${s.id}' cannot be reached from the entry step: no step leads to it.` }));
}
const result = (spec: Spec): CheckResult => ({ ok: !engineCheck(spec).length, problems: engineCheck(spec),
  summary: { steps: spec.steps.length, agents: [], approvals: [], typical_minutes: null, needs: [] } });

/* ── 1. The numbers ─────────────────────────────────────────────────────────────── */

describe("the numbers", () => {
  it("each field takes the engine's range, and says why not in words", () => {
    expect(readNumber("max_attempts", "5")).toEqual({ value: 5 });
    expect(readNumber("max_attempts", "")).toEqual({ value: null });
    expect(readNumber("max_attempts", "26")).toEqual({ error: "Enter 1 to 25" });
    expect(readNumber("max_attempts", "0")).toEqual({ error: "Enter 1 to 25" });
    expect(readNumber("max_attempts", "2.5")).toEqual({ error: "Enter a whole number" });
    expect(readNumber("max_attempts", "lots")).toEqual({ error: "Enter a number" });
    expect(readNumber("initial_seconds", "0")).toEqual({ value: 0 });
    expect(readNumber("initial_seconds", "3601")).toEqual({ error: "Enter 0 to 3600" });
    expect(readNumber("multiplier", "1.5")).toEqual({ value: 1.5 });
    expect(readNumber("multiplier", "0.5")).toEqual({ error: "Enter 1 to 10" });
    expect(readNumber("max_seconds", "86400")).toEqual({ value: 86400 });
    expect(readNumber("max_seconds", "86401")).toEqual({ error: "Enter 0 to 86400" });
  });

  it("a time limit is typed in seconds, minutes or hours and kept as seconds, 1s to 24h", () => {
    expect(readTimeLimit("5", "m")).toEqual({ value: 300 });
    expect(readTimeLimit("2", "h")).toEqual({ value: 7200 });
    expect(readTimeLimit("1.5", "m")).toEqual({ value: 90 });
    expect(readTimeLimit("", "m")).toEqual({ value: null });
    expect(readTimeLimit("25", "h")).toEqual({ error: "Enter 1 second to 24 hours" });
    expect(readTimeLimit("0", "s")).toEqual({ error: "Enter a time above zero" });
    expect(readTimeLimit("1.5", "s")).toEqual({ error: "Enter a whole number of seconds" });
    expect(unitFor(7200)).toBe("h");
    expect(unitFor(90)).toBe("s");
    expect(unitFor(600)).toBe("m");
  });

  it("the wait before try n+1 is min(initial × multiplier^(n-1), max), one fewer than the tries", () => {
    expect(backoffWaits(4, { initial_seconds: 10 })).toEqual([10, 20, 40]);
    expect(backoffWaits(5, { initial_seconds: 10, multiplier: 3, max_seconds: 100 })).toEqual([10, 30, 90, 100]);
    expect(backoffWaits(null, {})).toEqual([0, 0]); // the engine's defaults: 3 tries, no wait
    expect(backoffWaits(1, { initial_seconds: 10 })).toEqual([]);
  });

  it("and says them in plain words", () => {
    expect(sayWaits(4, { initial_seconds: 10 })).toBe("Waits 10s, then 20s, then 40s");
    expect(sayWaits(3, { initial_seconds: 90, multiplier: 2 })).toBe("Waits 1m 30s, then 3m");
    expect(sayWaits(8, { initial_seconds: 10 })).toBe("Waits 10s, then 20s, then 40s, … then 10m 40s");
    expect(sayWaits(null, {})).toBe("Tries again straight away, 2 times");
    expect(sayWaits(2, {})).toBe("Tries again straight away");
    expect(sayWaits(1, { initial_seconds: 10 })).toBe("Tried once: no retries");
    expect(saySeconds(3600)).toBe("1h");
    expect(saySeconds(86400 + 7200)).toBe("1d 2h");
  });

  it("a badge only for a step that fails other than the default way", () => {
    expect(failureBadge(step(FAILS, "build"))).toBe("Tried up to 5 times · Time limit 10m");
    expect(failureBadge(cap("x", "build", [], { max_attempts: 3 }))).toBeNull();
    expect(failureBadge(cap("x", "build", [], { max_attempts: 2 }))).toBeNull();
    expect(failureBadge(cap("x", "build", [], { timeout_seconds: 45 }))).toBe("Time limit 45s");
  });
});

/* ── 2. The spec ────────────────────────────────────────────────────────────────── */

describe("a field left empty is omitted, never written as its default", () => {
  const plain = cap("build", "build", ["deploy"]);

  it("each patch writes its field; null takes it out again", () => {
    const set = applyStepPatch(plain, { max_attempts: 5, timeout_seconds: 300,
                                       retry_backoff: { initial_seconds: 10 } });
    expect(set).toMatchObject({ max_attempts: 5, timeout_seconds: 300, retry_backoff: { initial_seconds: 10 } });
    const unset = applyStepPatch(set, { max_attempts: null, timeout_seconds: null, retry_backoff: null });
    expect(unset).toEqual(plain);
    expect(Object.keys(unset)).not.toContain("retry_backoff");
  });

  it("the wait keeps only the parts that were set", () => {
    expect(withBackoff(plain, "initial_seconds", 10)).toEqual({ initial_seconds: 10 });
    const step2 = { ...plain, retry_backoff: { initial_seconds: 10, multiplier: 3 } };
    expect(withBackoff(step2, "multiplier", null)).toEqual({ initial_seconds: 10 });
    expect(withBackoff({ ...plain, retry_backoff: { multiplier: 3 } }, "multiplier", null)).toBeNull();
  });

  it("a config patch cannot write them past their own fields", () => {
    expect(applyStepPatch(plain, { config: { on_error: "x", timeout_seconds: 1, retry_backoff: {} } })).toBe(plain);
  });

  it("round-trips: every field survives the JSON view and the store untouched", () => {
    const spec: Spec = { ...FAILS, steps: FAILS.steps.map((s) => (s.id === "build"
      ? { ...s, retry_backoff: { initial_seconds: 10, multiplier: 1.5, max_seconds: 120 } } : s)) };
    const parsed = parseSpecText(specText(spec));
    expect("spec" in parsed && parsed.spec).toEqual(spec);
    let s = editorReducer(emptyEditorState(), { type: "LOAD", spec, meta: newMeta({ workflow_key: "f" }) });
    s = editorReducer(s, { type: "PATCH_STEP", id: "build", patch: { title: "Build it" } });
    expect(step(s.spec, "build")).toEqual({ ...step(spec, "build"), title: "Build it" });
  });
});

/* ── 3. The error path is an arrow ──────────────────────────────────────────────── */

describe("the error path is an arrow", () => {
  it("is not one of the structural edges, but is laid out and drawn", () => {
    expect(edgesOf(FAILS).some((e) => e.to === "tell")).toBe(false);
    expect(errorEdgesOf(FAILS)).toEqual([{ from: "build", to: "tell", kind: "error" }]);
    const layout = layoutFlow(FAILS);
    expect(layout.edges).toContainEqual({ from: "build", to: "tell", kind: "error" });
    const at = new Map(layout.nodes.map((n) => [n.id, n]));
    // Only the error path reaches tell: it sits after build, as a branch's path does.
    expect(at.get("tell")!.column).toBe(at.get("build")!.column + 1);
    expect(at.get("deploy")!.column).toBe(2);
    expect(layout.terminal.sort()).toEqual(["deploy", "tell"]);
    // A step whose only way out is its error path still ends the flow when it works.
    expect(layoutFlow({ entry_step: "a", steps: [cap("a", "research", [], { on_error: "b" }), cap("b", "notify")] })
      .terminal).toEqual(["a", "b"]);
  });

  it("an error path back to an earlier step moves nothing", () => {
    const back: Spec = { ...PLAIN, steps: PLAIN.steps.map((s) => (s.id === "deploy" ? { ...s, on_error: "research" } : s)) };
    const cols = (spec: Spec) => layoutFlow(spec).nodes.filter((n) => n.id !== "tell").map((n) => [n.id, n.column]);
    expect(cols(back)).toEqual(cols(PLAIN));
    expect(layoutFlow(back).edges).toContainEqual({ from: "deploy", to: "research", kind: "error" });
  });

  it("a step pointing at itself or at nothing has no error path", () => {
    const self: Spec = { steps: [cap("a", "research", [], { on_error: "a" }), cap("b", "notify")] };
    expect(errorEdgesOf(self)).toEqual([]);
    expect(errorEdgesOf({ steps: [cap("a", "research", [], { on_error: "gone" })] })).toEqual([]);
  });

  it("is one of a step's links; one with none is a link to nothing", () => {
    expect(linksFrom(FAILS, "build")).toEqual([
      { from: "build", to: "deploy", kind: "next" }, { from: "build", to: "tell", kind: "error" }]);
    expect(linksFrom(FAILS, "deploy")).toEqual([{ from: "deploy", to: null, kind: "end" }]);
    expect(errorLinkOf(PLAIN, "build")).toEqual({ from: "build", to: null, kind: "error" });
  });

  it("follows its target's rename, and goes when its target is removed", () => {
    expect(step(renameStepId(FAILS, "tell", "alert"), "build").on_error).toBe("alert");
    const removed = removeStep(FAILS, "tell");
    expect(removed.steps.map((s) => s.id)).not.toContain("tell");
    expect("on_error" in step(removed, "build")).toBe(false);
  });

  it("is set, pointed elsewhere and unset by rewireLink, touching only on_error", () => {
    const set = relinked(PLAIN, { from: "build", to: null, kind: "error" }, "tell");
    expect(step(set, "build").on_error).toBe("tell");
    expect(set.steps.filter((s) => s.id !== "build")).toEqual(PLAIN.steps.filter((s) => s.id !== "build"));
    const moved = relinked(FAILS, { from: "build", to: "tell", kind: "error" }, "deploy");
    expect(step(moved, "build")).toMatchObject({ on_error: "deploy", next: ["deploy"], max_attempts: 5 });
    const unset = relinked(FAILS, { from: "build", to: "tell", kind: "error" }, null);
    expect("on_error" in step(unset, "build")).toBe(false);
    expect((rewireLink(FAILS, { from: "build", to: "tell", kind: "error" }, "tell") as { spec: Spec }).spec).toBe(FAILS);
  });

  it("refuses Start, the step itself, a join and a stale arrow, in words", () => {
    const link: FlowLink = { from: "build", to: "tell", kind: "error" };
    expect(refusal(FAILS, link, START_TARGET)).toMatch(/Nothing can lead back to Start/);
    expect(refusal(FAILS, link, "build")).toMatch(/cannot go to itself when it fails/);
    expect(refusal(FAILS, link, "nowhere")).toBe("Link it to a step, or Stop the run");
    expect(refusal(FAILS, { from: "build", to: "deploy", kind: "error" }, "research")).toBe("That arrow is not there any more");
    expect(refusal(PLAIN, link, "deploy")).toBe("That arrow is not there any more");
    const joined: Spec = { ...FAILS, steps: [...FAILS.steps, { id: "j", kind: "join", wait_for: [] }] };
    expect(refusal(joined, link, "j")).toMatch(/lead into a join/);
  });

  it("leaves a cycle to the validator, which refuses it in its own words", async () => {
    const check = vi.fn(async (spec: Spec) => result(spec));
    expect(refusal(FAILS, { from: "deploy", to: null, kind: "error" }, "research")).toBeNull();
    const cycle = await checkRewire(FAILS, { from: "deploy", to: null, kind: "error" }, "research", check);
    expect(cycle).toEqual({ ok: false, reason: "Unbounded cycle detected at step 'research'" });
    const fine = await checkRewire(PLAIN, { from: "build", to: null, kind: "error" }, "deploy", check);
    expect(fine.ok).toBe(true);
  });

  it("is one edit: one undo puts it back; removing a block or branch prunes error paths into it", () => {
    const s0 = editorReducer(emptyEditorState(), { type: "LOAD", spec: PLAIN, meta: newMeta({ workflow_key: "p" }) });
    const s1 = editorReducer(s0, { type: "REWIRE", link: { from: "build", to: null, kind: "error" }, target: "tell" });
    expect(step(s1.spec, "build").on_error).toBe("tell");
    expect(editorReducer(s1, { type: "UNDO" }).spec).toBe(PLAIN);
    expect(editorReducer(s1, { type: "REWIRE", link: { from: "build", to: "tell", kind: "error" }, target: "build" })).toBe(s1);
    const s2 = editorReducer(s1, { type: "REMOVE", id: "tell" });
    expect("on_error" in step(s2.spec, "build")).toBe(false);
  });
});

/* ── DOM helpers ───────────────────────────────────────────────────────────────── */

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

const byTest = (root: ParentNode, id: string) => root.querySelector<HTMLElement>(`[data-testid="${id}"]`);
function type(el: HTMLElement, value: string) {
  act(() => {
    const input = el as HTMLInputElement;
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;
    setter.call(input, value);
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
}
function choose(el: HTMLElement, value: string) {
  act(() => {
    (el as HTMLSelectElement).value = value;
    el.dispatchEvent(new Event("change", { bubbles: true }));
  });
}

/* ── 4. The settings panel ─────────────────────────────────────────────────────── */

describe("the When it fails settings", () => {
  let latest: Spec = PLAIN;
  let rewires: [FlowLink, string | null][] = [];
  function Harness({ spec, id }: { spec: Spec; id: string }) {
    const [state, dispatch] = useReducer(editorReducer,
      editorReducer(emptyEditorState(), { type: "LOAD", spec, meta: newMeta({ workflow_key: "f" }) }));
    latest = state.spec;
    return h(FailureSettings, {
      step: step(state.spec, id), spec: state.spec, palette: PALETTE, problems: [],
      onPatch: (patch) => dispatch({ type: "PATCH_STEP", id, patch }),
      onErrorPath: (target) => {
        const link = errorLinkOf(state.spec, id);
        rewires.push([link, target]);
        dispatch({ type: "REWIRE", link, target });
      },
    });
  }
  beforeEach(() => { mountHost(); latest = PLAIN; rewires = []; });
  const panel = (spec: Spec = PLAIN, id = "build") => {
    act(() => root!.render(h(Harness, { spec, id })));
    return host!;
  };

  it("shows the defaults as placeholders and writes nothing until something is typed", () => {
    const el = panel();
    expect((byTest(el, "wf-fail-attempts") as HTMLInputElement).value).toBe("");
    expect((byTest(el, "wf-fail-attempts") as HTMLInputElement).placeholder).toBe("3");
    expect((byTest(el, "wf-fail-multiplier") as HTMLInputElement).placeholder).toBe("2");
    expect((byTest(el, "wf-fail-max") as HTMLInputElement).placeholder).toBe("3600");
    expect((byTest(el, "wf-fail-on-error") as HTMLSelectElement).value).toBe("");
    expect(byTest(el, "wf-fail-preview")!.textContent).toBe("Tries again straight away, 2 times");
    expect(latest).toBe(PLAIN);
  });

  it("tries: written as typed, refused out of range with words, unset when cleared", () => {
    const el = panel();
    const box = byTest(el, "wf-fail-attempts")!;
    type(box, "4");
    expect(step(latest, "build").max_attempts).toBe(4);
    type(box, "30");
    expect(byTest(el, "wf-fail-attempts-error")!.textContent).toBe("Enter 1 to 25");
    expect(box.getAttribute("aria-invalid")).toBe("true");
    expect((box as HTMLInputElement).value).toBe("30");
    expect(step(latest, "build").max_attempts).toBe(4);
    type(box, "");
    expect(byTest(el, "wf-fail-attempts-error")).toBeNull();
    expect("max_attempts" in step(latest, "build")).toBe(false);
  });

  it("the wait: each part written alone, and the preview says the waits", () => {
    const el = panel();
    type(byTest(el, "wf-fail-attempts")!, "4");
    type(byTest(el, "wf-fail-initial")!, "10");
    expect(step(latest, "build").retry_backoff).toEqual({ initial_seconds: 10 });
    expect(byTest(el, "wf-fail-preview")!.textContent).toBe("Waits 10s, then 20s, then 40s");
    type(byTest(el, "wf-fail-multiplier")!, "3");
    type(byTest(el, "wf-fail-max")!, "60");
    expect(step(latest, "build").retry_backoff).toEqual({ initial_seconds: 10, multiplier: 3, max_seconds: 60 });
    expect(byTest(el, "wf-fail-preview")!.textContent).toBe("Waits 10s, then 30s, then 1m");
    type(byTest(el, "wf-fail-multiplier")!, "0.5");
    expect(byTest(el, "wf-fail-multiplier-error")!.textContent).toBe("Enter 1 to 10");
    expect(step(latest, "build").retry_backoff).toMatchObject({ multiplier: 3 });
    type(byTest(el, "wf-fail-initial")!, "4000");
    expect(byTest(el, "wf-fail-initial-error")!.textContent).toBe("Enter 0 to 3600");
    for (const id of ["wf-fail-initial", "wf-fail-multiplier", "wf-fail-max"]) type(byTest(el, id)!, "");
    expect("retry_backoff" in step(latest, "build")).toBe(false);
  });

  it("the time limit: a number and its unit, kept as seconds, refused past 24 hours", () => {
    const el = panel();
    expect((byTest(el, "wf-fail-timeout-unit") as HTMLSelectElement).value).toBe("m");
    type(byTest(el, "wf-fail-timeout")!, "5");
    expect(step(latest, "build").timeout_seconds).toBe(300);
    choose(byTest(el, "wf-fail-timeout-unit")!, "h");
    expect(step(latest, "build").timeout_seconds).toBe(18000);
    type(byTest(el, "wf-fail-timeout")!, "25");
    expect(byTest(el, "wf-fail-timeout-error")!.textContent).toBe("Enter 1 second to 24 hours");
    expect(step(latest, "build").timeout_seconds).toBe(18000);
    type(byTest(el, "wf-fail-timeout")!, "");
    expect("timeout_seconds" in step(latest, "build")).toBe(false);
  });

  it("a stored time limit is shown in the unit that says it whole", () => {
    const el = panel(FAILS);
    expect((byTest(el, "wf-fail-timeout") as HTMLInputElement).value).toBe("10");
    expect((byTest(el, "wf-fail-timeout-unit") as HTMLSelectElement).value).toBe("m");
    expect((byTest(el, "wf-fail-attempts") as HTMLInputElement).value).toBe("5");
  });

  it("If it still fails, go to…: every other step, or Stop the run; set and unset as an arrow", () => {
    const el = panel();
    const pick = byTest(el, "wf-fail-on-error") as HTMLSelectElement;
    expect([...pick.options].map((o) => [o.value, o.textContent])).toEqual([
      ["", "Stop the run"], ["research", "Research"], ["deploy", "Deploy"], ["tell", "Notify"]]);
    choose(pick, "tell");
    expect(rewires[0]).toEqual([{ from: "build", to: null, kind: "error" }, "tell"]);
    expect(step(latest, "build").on_error).toBe("tell");
    expect(pick.value).toBe("tell");
    choose(pick, "");
    expect(rewires[1]).toEqual([{ from: "build", to: "tell", kind: "error" }, null]);
    expect("on_error" in step(latest, "build")).toBe(false);
  });
});

/* ── 5. The lane ───────────────────────────────────────────────────────────────── */

function pointer(type_: string, target: EventTarget, x: number, y: number) {
  act(() => {
    target.dispatchEvent(new PointerEvent(type_, {
      clientX: x, clientY: y, pointerId: 1, button: 0, pointerType: "mouse", bubbles: true, cancelable: true,
    }));
  });
}
const scaleOf = (lane: HTMLElement) => Number(lane.querySelector<HTMLElement>(".wf-lane-canvas")!.dataset.scale) || 1;
const page = (lane: HTMLElement, x: number, y: number) => ({ x: x * scaleOf(lane), y: y * scaleOf(lane) });
const centreOf = (lane: HTMLElement, sel: string) => {
  const el = lane.querySelector<HTMLElement>(sel)!;
  return page(lane, parseFloat(el.style.left) + parseFloat(el.style.width) / 2,
              parseFloat(el.style.top) + parseFloat(el.style.height) / 2);
};
function dragEnd(lane: HTMLElement, key: string, to: { x: number; y: number }) {
  const grip = byTest(lane, `wf-edge-end-${key}`)!;
  const from = page(lane, parseFloat(grip.style.left), parseFloat(grip.style.top));
  pointer("pointerdown", grip, from.x, from.y);
  pointer("pointermove", document.body, (from.x + to.x) / 2, (from.y + to.y) / 2);
  pointer("pointermove", document.body, to.x, to.y);
  pointer("pointerup", document.body, to.x, to.y);
}

describe("on the lane", () => {
  beforeEach(() => {
    stub("clientWidth", (el) => (el.classList?.contains("wf-lane-viewport") ? WIDE : 0));
    stub("scrollWidth", (el) => (el.classList?.contains("wf-lane-viewport") ? WIDE : 0));
    mountHost();
  });
  const lane = (props: Record<string, unknown> = {}) => {
    act(() => root!.render(h(FlowLane, { spec: FAILS, palette: PALETTE, mode: "design", ...props })));
    return host!;
  };

  it("an error path is its own dashed error arrow, labelled, and said in words", () => {
    const el = lane();
    const g = el.querySelector<SVGGElement>('g[data-kind="error"]')!;
    expect(g).not.toBeNull();
    expect(g.querySelector(".wf-edge")!.getAttribute("data-kind")).toBe("error");
    expect(g.querySelector(".wf-edge-label")!.textContent).toBe("if it fails");
    expect(g.querySelector(".wf-edge")!.getAttribute("marker-end")).toMatch(/-arrow-error\)$/);
    expect(byTest(el, "wf-error-path-build")!.textContent).toBe("If Build, review and test fails, go to Notify");
    // A straight flow with an error path is drawn with arrows, so the error path can be.
    expect(el.querySelector('[data-testid="wf-lane"]')!.getAttribute("data-linear")).toBe("false");
    // And its plain arrows are still there.
    expect(el.querySelector('g[data-edge="next:build>deploy"]')).not.toBeNull();
  });

  it("a flow without one draws no error arrow", () => {
    act(() => root!.render(h(FlowLane, { spec: PLAIN, palette: PALETTE, mode: "design" })));
    expect(host!.querySelector('g[data-kind="error"]')).toBeNull();
    expect(byTest(host!, "wf-error-paths")).toBeNull();
  });

  it("a step with extra tries or a time limit wears a badge that says so", () => {
    const el = lane();
    const badge = el.querySelector('.wf-node[data-step-id="build"] [data-testid="wf-badge-failure"]')!;
    expect(badge.getAttribute("aria-label")).toBe("Tried up to 5 times · Time limit 10m");
    expect(el.querySelector('.wf-node[data-step-id="deploy"] [data-testid="wf-badge-failure"]')).toBeNull();
  });

  it("its end is a handle like any arrow: dragged to another step it asks for that error path", () => {
    const onRewire = vi.fn();
    const el = lane({ onRewire, selection: "build" });
    const grip = byTest(el, "wf-edge-end-error:build>tell")!;
    expect(grip.getAttribute("aria-label")).toMatch(/^If Build, review and test fails, it goes to Notify\. Drag/);
    expect(grip.tabIndex).toBe(0);
    dragEnd(el, "error:build>tell", centreOf(el, '.wf-slot[data-slot="research"]'));
    expect(onRewire).toHaveBeenCalledWith({ from: "build", to: "tell", kind: "error" }, "research");
    dragEnd(el, "error:build>tell", centreOf(el, ".wf-slot-end"));
    expect(onRewire).toHaveBeenLastCalledWith({ from: "build", to: "tell", kind: "error" }, null);
  });

  it("from the keyboard its list offers every other step, and Stop the run", () => {
    const onRewire = vi.fn();
    const el = lane({ onRewire, selection: "build" });
    act(() => byTest(el, "wf-edge-end-error:build>tell")!.click());
    const list = byTest(el, "wf-link-target") as HTMLSelectElement;
    expect(list.value).toBe("tell");
    expect([...list.options].at(-1)!.textContent).toBe("Stop the run");
    expect(byTest(el, "wf-link-picker")!.textContent).toContain("If it fails, go to");
    choose(list, "__end__");
    expect(onRewire).toHaveBeenCalledWith({ from: "build", to: "tell", kind: "error" }, null);
  });

  it("on a run, a step that failed and went to its error path is marked, and its arrow drawn as taken", () => {
    act(() => root!.render(h(FlowLane, { spec: FAILS, palette: PALETTE, mode: "run", runState: {
      research: { phase: "done", meta: "" }, build: { phase: "failed", meta: "timed out" },
      deploy: { phase: "future", meta: "" }, tell: { phase: "done", meta: "" } } })));
    const el = host!;
    expect(el.querySelector('g[data-kind="error"]')!.hasAttribute("data-taken")).toBe(true);
    expect(el.querySelector('.wf-slot[data-slot="build"]')!.hasAttribute("data-error-path")).toBe(true);
    expect(el.querySelector('.wf-node[data-step-id="build"]')!.getAttribute("data-error-path")).toBe("true");
    expect(byTest(el, "wf-error-path-build")!.textContent).toMatch(/it failed, and the run went there$/);
    // Not taken while its target has not run.
    act(() => root!.render(h(FlowLane, { spec: FAILS, palette: PALETTE, mode: "run", runState: {
      research: { phase: "done", meta: "" }, build: { phase: "failed", meta: "" } } })));
    expect(host!.querySelector('g[data-kind="error"]')!.hasAttribute("data-taken")).toBe(false);
  });

  it("a step that ran past its time limit says timed out", () => {
    const base = { step_id: "build", kind: "capability", status: "failed", capability: "build", output: {},
                   evidence: [], approval: {}, due_at: null, timeout_applied: null, attempt: 3,
                   started_at: null, completed_at: null };
    expect(laneStateOf({ ...base, error: "step_timeout: ran longer than 600s" } as never))
      .toMatchObject({ phase: "failed", meta: "timed out" });
    expect(laneStateOf({ ...base, error: null, output: { reason: "step_timeout" } } as never).meta).toBe("timed out");
    expect(laneStateOf({ ...base, error: "boom" } as never).meta).not.toBe("timed out");
  });
});

/* ── 6. The editor, end to end ─────────────────────────────────────────────────── */

describe("in the editor", () => {
  const OWN = { definition_id: "wfd_own", workflow_key: "fails", name: "Fails", is_catalogue: false, version: 1 };
  let api: FakeApi;
  let checks: Spec[];

  beforeEach(() => {
    stub("clientWidth", (el) => (el.classList?.contains("wf-lane-viewport") ? WIDE : 0));
    window.history.replaceState(null, "", "/?area=delivery_workflows&wf=fails");
    // tell is reached only once build has an error path: it starts as a stray the check flags.
    api = fakeWorkflowApi(PALETTE, [{ ...OWN, spec: PLAIN }]);
    checks = [];
    vi.stubGlobal("fetch", vi.fn(async (input: unknown, init?: RequestInit) => {
      const body = init?.body ? JSON.parse(String(init.body)) as { action?: string; spec?: Spec } : {};
      if (body.action === "check" && body.spec) {
        checks.push(body.spec);
        return Response.json(result(body.spec));
      }
      return api.fetch(input, init);
    }));
    try { window.localStorage.clear(); } catch { /* none */ }
    mountHost();
  });

  async function settle(rounds = 10) {
    for (let i = 0; i < rounds; i += 1) await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
  }
  const toast = () => byTest(document, "wf-toast")?.textContent ?? "";
  const select = async (id: string) => {
    await act(async () => { document.querySelector<HTMLElement>(`.wf-node[data-step-id="${id}"]`)!.click(); });
    await settle(3);
  };

  it("If it still fails, go to… asks the validator, draws the error arrow, and Undo takes it away", async () => {
    act(() => root!.render(h(WorkflowEditor, { projectId: "prj_1", workflowKey: "fails", onBack: vi.fn(), onStart: vi.fn() })));
    await settle();
    await select("build");
    const pick = byTest(document, "wf-fail-on-error") as HTMLSelectElement;
    expect(pick).not.toBeNull();
    choose(pick, "tell");
    await settle();
    expect(checks.some((s) => s.steps.find((x) => x.id === "build")?.on_error === "tell")).toBe(true);
    expect(document.querySelector('g[data-edge="error:build>tell"]')).not.toBeNull();
    expect(toast()).toContain("Link changed");
    await act(async () => { byTest(document, "wf-toast-undo")!.click(); });
    await settle();
    expect(document.querySelector('g[data-edge="error:build>tell"]')).toBeNull();
  });

  it("an error path that makes a cycle is refused in the validator's words, and nothing changes", async () => {
    act(() => root!.render(h(WorkflowEditor, { projectId: "prj_1", workflowKey: "fails", onBack: vi.fn(), onStart: vi.fn() })));
    await settle();
    await select("deploy");
    choose(byTest(document, "wf-fail-on-error")!, "research");
    await settle();
    expect(toast()).toMatch(/cycle detected at step 'research'/);
    expect(document.querySelector('g[data-kind="error"]')).toBeNull();
    expect((byTest(document, "wf-fail-on-error") as HTMLSelectElement).value).toBe("");
  });

  it("a number typed in the panel lands in the spec and on the node's badge", async () => {
    act(() => root!.render(h(WorkflowEditor, { projectId: "prj_1", workflowKey: "fails", onBack: vi.fn(), onStart: vi.fn() })));
    await settle();
    await select("build");
    type(byTest(document, "wf-fail-timeout")!, "2");
    await settle(3);
    expect(document.querySelector('.wf-node[data-step-id="build"] [data-testid="wf-badge-failure"]')!
      .getAttribute("aria-label")).toBe("Time limit 2m");
  });
});
