// @vitest-environment happy-dom

import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { readFileSync } from "node:fs";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { BRANCHED, linear, PALETTE, STRETCH } from "./_helpers/canvas-fixtures";
import { afterGap } from "../lib/workflow-graph";

/**
 * Drag and drop on the flow lane, and the keyboard alternative.
 *
 * Real pointer events against the real lane in happy-dom. The rule under test is the one an
 * operator feels: a press that moves less than 4px is a click; past that, the step follows the
 * pointer, the lane opens a gap, and letting go over gap k is exactly one MOVE to k (or one
 * INSERT at k from the palette). Esc, letting go away from the lane, or a gap across a branch
 * drops nothing at all.
 */

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const h = React.createElement;
const { FlowLane } = await import("../app/workflows/flow/flow-lane");
const { StepPalette } = await import("../app/workflows/editor/palette");
const { planDrag } = await import("../app/workflows/flow/drag-plan");
const { DRAG_THRESHOLD, nearestGap } = await import("../app/workflows/flow/use-lane-drag");
const { chooseDensity } = await import("../app/workflows/flow/geometry");
const { layoutFlow, linearOrder } = await import("../lib/workflow-graph");

/** Wide enough that the lane draws at scale 1 and the canvas starts at x = 0. */
const WIDE = 4000;
const LANE = { top: 0, height: 200 };

let root: Root | null = null;
let host: HTMLElement | null = null;
const saved: Record<string, PropertyDescriptor | undefined> = {};

function stub(name: "clientWidth" | "scrollWidth", get: (el: HTMLElement) => number) {
  saved[name] ??= Object.getOwnPropertyDescriptor(HTMLElement.prototype, name);
  Object.defineProperty(HTMLElement.prototype, name, { configurable: true, get() { return get(this as HTMLElement); } });
}

beforeEach(() => {
  stub("clientWidth", (el) => (el.classList?.contains("wf-lane-viewport") ? WIDE : 0));
  stub("scrollWidth", (el) => (el.classList?.contains("wf-lane-viewport") ? WIDE : 0));
  vi.spyOn(Element.prototype, "getBoundingClientRect").mockImplementation(function rect(this: Element) {
    const el = this as HTMLElement;
    const wide = el.classList.contains("wf-lane-viewport") || el.classList.contains("wf-lane-canvas");
    const r = wide ? { left: 0, top: LANE.top, width: WIDE, height: LANE.height }
                   : { left: 0, top: 0, width: 100, height: 30 };
    return { ...r, x: r.left, y: r.top, right: r.left + r.width, bottom: r.top + r.height, toJSON() { return r; } } as DOMRect;
  });
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
});

afterEach(async () => {
  // The click that follows a drag is swallowed until the next tick; let that tick pass.
  await new Promise((r) => setTimeout(r, 0));
  act(() => root?.unmount());
  host?.remove();
  root = null; host = null;
  vi.restoreAllMocks();
  for (const [name, d] of Object.entries(saved)) {
    if (d) Object.defineProperty(HTMLElement.prototype, name, d);
    else delete (HTMLElement.prototype as unknown as Record<string, unknown>)[name];
    delete saved[name];
  }
  document.querySelectorAll(".wf-ghost").forEach((g) => g.remove());
});

function render(el: React.ReactElement) {
  act(() => root!.render(el));
  return host!;
}

function pointer(type: string, target: EventTarget, x: number, y: number, extra: Partial<PointerEventInit> = {}) {
  act(() => {
    target.dispatchEvent(new PointerEvent(type, {
      clientX: x, clientY: y, pointerId: 1, button: 0, pointerType: "mouse", bubbles: true, cancelable: true, ...extra,
    }));
  });
}

const node = (root: HTMLElement, id: string) => root.querySelector<HTMLElement>(`.wf-node[data-step-id="${id}"]`)!;
const slot = (root: HTMLElement, id: string) => root.querySelector<HTMLElement>(`.wf-slot[data-slot="${id}"]`)!;
const centreOf = (root: HTMLElement, id: string) => {
  const s = slot(root, id);
  return { x: parseFloat(s.style.left) + 20, y: 50 };
};

/** Canvas x of the gap a drop at final position `to` uses, as the lane computes it. */
function gapX(spec: Parameters<typeof layoutFlow>[0], source: Parameters<typeof planDrag>[0]["source"], to: number) {
  const layout = layoutFlow(spec);
  const types = new Map(spec.steps.map((s) => [s.id, "builtin" as const]));
  const geometry = chooseDensity(layout, types, WIDE);
  const plan = planDrag({ layout, geometry, order: linearOrder(spec), source, canMove: () => true, canInsert: () => true });
  return plan.gaps.find((g) => g.to === to)!.x;
}

describe("where a drag can land", () => {
  const spec = linear(6);
  const layout = layoutFlow(spec);
  const geometry = chooseDensity(layout, new Map(), WIDE);
  const order = linearOrder(spec);

  it("on a move, gap k among the remaining steps is final position k", () => {
    const plan = planDrag({ layout, geometry, order, source: { kind: "node", id: "meeting" },
                            canMove: () => true, canInsert: () => true });
    expect(plan.gaps.map((g) => g.to)).toEqual([0, 1, 2, 3, 4, 5]);
    expect(plan.gaps.filter((g) => g.noop).map((g) => g.to)).toEqual([1]);
    const xs = plan.gaps.map((g) => g.x);
    expect(xs).toEqual([...xs].sort((a, b) => a - b));
    // The dragged step's slot closes up: everything after it slides left by its width and a connector.
    expect(plan.base.get("research")).toBeUndefined();
    expect(plan.base.get("spec_review")).toBeLessThan(0);
  });

  it("from the palette, there is a gap before every step and one at the end", () => {
    const plan = planDrag({ layout, geometry, order, source: { kind: "item", item: { capability: "build" } },
                            canMove: () => true, canInsert: () => true });
    expect(plan.gaps.map((g) => g.to)).toEqual([0, 1, 2, 3, 4, 5, -1]);
  });

  it("in a branched flow, a gap the model refuses is offered as refused", () => {
    const bl = layoutFlow(BRANCHED);
    const bg = chooseDensity(bl, new Map(), WIDE);
    const plan = planDrag({ layout: bl, geometry: bg, order: linearOrder(BRANCHED), source: { kind: "node", id: "review" },
                            canMove: () => false, canInsert: () => false });
    expect(plan.gaps.filter((g) => g.allowed && !g.noop)).toEqual([]);
  });

  it("picks the nearest gap", () => {
    expect(nearestGap(52, [{ x: 0, to: 0, allowed: true }, { x: 60, to: 1, allowed: true }, { x: 200, to: 2, allowed: true }])).toBe(1);
    expect(nearestGap(5, [])).toBeNull();
  });
});

describe("dragging a step", () => {
  it("dispatches exactly one MOVE to the gap it was dropped on", () => {
    const onMove = vi.fn();
    const spec = linear(6);
    const lane = render(h(FlowLane, { spec, palette: PALETTE, mode: "design", onMove, onSelect: vi.fn() }));
    const start = centreOf(lane, "meeting");
    pointer("pointerdown", node(lane, "meeting"), start.x, start.y);
    pointer("pointermove", document.body, start.x + DRAG_THRESHOLD - 2, start.y);
    expect(document.querySelector(".wf-ghost")).toBeNull();

    const target = gapX(spec, { kind: "node", id: "meeting" }, 3);
    pointer("pointermove", document.body, target, start.y);
    pointer("pointermove", document.body, target + 1, start.y);
    expect(document.querySelector(".wf-ghost")).not.toBeNull();
    pointer("pointerup", document.body, target + 1, start.y);

    expect(onMove).toHaveBeenCalledTimes(1);
    expect(onMove).toHaveBeenCalledWith("meeting", 3);
    expect(document.querySelector(".wf-ghost")).toBeNull();
    expect([...lane.querySelectorAll<HTMLElement>(".wf-slot")].every((s) => !s.style.transform)).toBe(true);
    expect(lane.querySelector('[data-testid="wf-announce"]')!.textContent).toBe("Moved Convene a panel to 4 of 6");
  });

  it("opens the gap while dragging: an accent caret, and the steps after it slide apart", async () => {
    const spec = linear(5);
    const lane = render(h(FlowLane, { spec, palette: PALETTE, mode: "design", onMove: vi.fn() }));
    const start = centreOf(lane, "research");
    pointer("pointerdown", node(lane, "research"), start.x, start.y);
    const target = gapX(spec, { kind: "node", id: "research" }, 2);
    pointer("pointermove", document.body, target, start.y);
    await act(async () => { await new Promise((r) => setTimeout(r, 40)); });
    const caret = lane.querySelector<HTMLElement>('[data-testid="wf-caret"]')!;
    expect(caret.hidden).toBe(false);
    expect(caret.dataset.allowed).toBe("true");
    expect(slot(lane, "research").hasAttribute("data-origin")).toBe(true);
    expect(slot(lane, "build").style.transform).toMatch(/translateX/);
    pointer("pointerup", document.body, target, 1000);
  });

  it("treats a press that moves less than 4px as a click", () => {
    const onMove = vi.fn();
    const onSelect = vi.fn();
    const lane = render(h(FlowLane, { spec: linear(4), palette: PALETTE, mode: "design", onMove, onSelect }));
    const start = centreOf(lane, "meeting");
    pointer("pointerdown", node(lane, "meeting"), start.x, start.y);
    pointer("pointermove", document.body, start.x + 3, start.y);
    pointer("pointerup", document.body, start.x + 3, start.y);
    act(() => node(lane, "meeting").click());
    expect(onMove).not.toHaveBeenCalled();
    expect(onSelect).toHaveBeenCalledWith("meeting");
  });

  it("drops nothing on Esc", () => {
    const onMove = vi.fn();
    const spec = linear(5);
    const lane = render(h(FlowLane, { spec, palette: PALETTE, mode: "design", onMove }));
    const start = centreOf(lane, "meeting");
    pointer("pointerdown", node(lane, "meeting"), start.x, start.y);
    pointer("pointermove", document.body, gapX(spec, { kind: "node", id: "meeting" }, 4), start.y);
    act(() => { window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })); });
    pointer("pointerup", document.body, gapX(spec, { kind: "node", id: "meeting" }, 4), start.y);
    expect(onMove).not.toHaveBeenCalled();
    expect(document.querySelector(".wf-ghost")).toBeNull();
  });

  it("drops nothing when released away from the lane", () => {
    const onMove = vi.fn();
    const spec = linear(5);
    const lane = render(h(FlowLane, { spec, palette: PALETTE, mode: "design", onMove }));
    const start = centreOf(lane, "meeting");
    pointer("pointerdown", node(lane, "meeting"), start.x, start.y);
    pointer("pointermove", document.body, start.x + 80, start.y);
    pointer("pointerup", document.body, start.x + 80, LANE.height + 400);
    expect(onMove).not.toHaveBeenCalled();
  });

  it("refuses a drag across a branch: a grey caret that says why, and no MOVE", async () => {
    const onMove = vi.fn();
    const lane = render(h(FlowLane, { spec: BRANCHED, palette: PALETTE, mode: "design", onMove }));
    const start = centreOf(lane, "review");
    pointer("pointerdown", node(lane, "review"), start.x, start.y);
    const far = parseFloat(slot(lane, "deploy").style.left) - 4;
    pointer("pointermove", document.body, far, start.y);
    await act(async () => { await new Promise((r) => setTimeout(r, 40)); });
    const caret = lane.querySelector<HTMLElement>('[data-testid="wf-caret"]')!;
    expect(caret.dataset.allowed).toBe("false");
    expect(caret.dataset.tip).toBe("Edit branches in JSON");
    // The reason shows mid-drag, as the shared tooltip pinned to the caret: fixed on the page,
    // placed inside the window, so the lane's clipping edge can never hide it.
    const bubble = document.querySelector<HTMLElement>('.wf-tip[role="tooltip"]')!;
    expect(bubble).not.toBeNull();
    expect(bubble.hidden).toBe(false);
    expect(bubble.textContent).toBe("Edit branches in JSON");
    expect(bubble.parentElement).toBe(document.body);
    const top = parseFloat(bubble.style.top);
    const left = parseFloat(bubble.style.left);
    expect(top).toBeGreaterThanOrEqual(0);
    expect(top).toBeLessThan(window.innerHeight);
    expect(left).toBeGreaterThanOrEqual(0);
    expect(left).toBeLessThan(window.innerWidth);
    // Hovering something else mid-drag does not take it away; a drop target that is allowed does.
    act(() => { node(lane, "research").dispatchEvent(new PointerEvent("pointerover", { bubbles: true })); });
    expect(bubble.hidden).toBe(false);
    pointer("pointerup", document.body, far, start.y);
    expect(onMove).not.toHaveBeenCalled();
    expect(bubble.hidden).toBe(true);
    // And no label of the caret's own: a pseudo-element above it sat outside the lane and was clipped.
    expect(readFileSync("app/workflows/flow.css", "utf8")).not.toMatch(/\.wf-drop-caret[^{]*::?(after|before)/);
  });

  it("scrolls the lane when the pointer nears its edge", async () => {
    stub("clientWidth", (el) => (el.classList?.contains("wf-lane-viewport") ? 600 : 0));
    stub("scrollWidth", (el) => (el.classList?.contains("wf-lane-viewport") ? 3000 : 0));
    vi.mocked(Element.prototype.getBoundingClientRect).mockImplementation(function rect(this: Element) {
      const r = (this as HTMLElement).classList.contains("wf-lane-viewport")
        ? { left: 0, top: 0, width: 600, height: 200 } : { left: 0, top: 0, width: 100, height: 30 };
      return { ...r, x: r.left, y: r.top, right: r.left + r.width, bottom: r.top + r.height, toJSON() { return r; } } as DOMRect;
    });
    const lane = render(h(FlowLane, { spec: linear(24), palette: PALETTE, mode: "design", onMove: vi.fn() }));
    const viewport = lane.querySelector<HTMLElement>(".wf-lane-viewport")!;
    const start = centreOf(lane, "research");
    pointer("pointerdown", node(lane, "research"), start.x, start.y);
    pointer("pointermove", document.body, 595, 50);
    await act(async () => { await new Promise((r) => setTimeout(r, 120)); });
    expect(viewport.scrollLeft).toBeGreaterThan(0);
    pointer("pointerup", document.body, 595, 1000);
  });
});

describe("dragging from the palette", () => {
  it("inserts at the gap it is dropped on", () => {
    const onInsert = vi.fn();
    const spec = linear(5);
    const view = render(h("div", null,
      h(StepPalette, { palette: PALETTE, onPick: vi.fn(), onNewStep: vi.fn(), onEditStep: vi.fn() }),
      h(FlowLane, { spec, palette: PALETTE, mode: "design", onInsert, onMove: vi.fn() })));
    const row = view.querySelector<HTMLElement>('[data-testid="wf-pal-cap:build"]')!;
    pointer("pointerdown", row, 10, 10);
    const target = gapX(spec, { kind: "item", item: { capability: "build" } }, 2);
    pointer("pointermove", document.body, target, 60);
    pointer("pointerup", document.body, target, 60);
    expect(onInsert).toHaveBeenCalledTimes(1);
    expect(onInsert).toHaveBeenCalledWith(2, { capability: "build" });
  });

  it("does not drag a step that cannot be used", () => {
    const onInsert = vi.fn();
    const view = render(h("div", null,
      h(StepPalette, { palette: PALETTE, onPick: vi.fn(), onNewStep: vi.fn(), onEditStep: vi.fn() }),
      h(FlowLane, { spec: linear(3), palette: PALETTE, mode: "design", onInsert })));
    const row = view.querySelector<HTMLElement>('[data-testid="wf-pal-custom:broken"]')!;
    pointer("pointerdown", row, 10, 10);
    pointer("pointermove", document.body, 300, 60);
    pointer("pointerup", document.body, 300, 60);
    expect(onInsert).not.toHaveBeenCalled();
  });
});

describe("inside a branch", () => {
  it("offers the gaps the model allows, and a palette drop there inserts at that place in the flow", () => {
    const onInsert = vi.fn();
    const order = linearOrder(STRETCH);
    const between = order.indexOf("a2");
    const view = render(h("div", null,
      h(StepPalette, { palette: PALETTE, onPick: vi.fn(), onNewStep: vi.fn(), onEditStep: vi.fn() }),
      h(FlowLane, { spec: STRETCH, palette: PALETTE, mode: "design", onInsert, onMove: vi.fn(),
                    renderGapAction: (at: number) => h("button", { "data-at": at }, "+") })));
    const gap = view.querySelector<HTMLElement>(`.wf-gap[data-at="${between}"]`)!;
    expect(gap).not.toBeNull();
    // At the head of a branch (the step takes the branch over), but never into the join.
    expect(view.querySelector(`.wf-gap[data-at="${order.indexOf("a1")}"]`)).not.toBeNull();
    expect(view.querySelector(`.wf-gap[data-at="${order.indexOf("gather")}"]`)).toBeNull();
    // After each branch's last step, where the gap before the join cannot serve.
    expect(view.querySelector(`.wf-gap[data-at="${afterGap(order.indexOf("a2"))}"]`)).not.toBeNull();
    expect(view.querySelector(`.wf-gap[data-at="${afterGap(order.indexOf("b1"))}"]`)).not.toBeNull();
    const x = parseFloat(gap.style.left);
    const y = parseFloat(gap.style.top);
    pointer("pointerdown", view.querySelector<HTMLElement>('[data-testid="wf-pal-cap:build"]')!, 10, 10);
    pointer("pointermove", document.body, x, y);
    pointer("pointerup", document.body, x, y);
    expect(onInsert).toHaveBeenCalledWith(between, { capability: "build" });
  });
});

describe("the keyboard", () => {
  function key(target: HTMLElement, k: string, init: KeyboardEventInit = {}) {
    act(() => { target.dispatchEvent(new KeyboardEvent("keydown", { key: k, bubbles: true, cancelable: true, ...init })); });
  }

  it("Alt+→ moves the step one place right, and says so", () => {
    const onMove = vi.fn();
    const lane = render(h(FlowLane, { spec: linear(5), palette: PALETTE, mode: "design", onMove }));
    key(node(lane, "meeting"), "ArrowRight", { altKey: true });
    expect(onMove).toHaveBeenCalledTimes(1);
    expect(onMove).toHaveBeenCalledWith("meeting", 2);
    expect(lane.querySelector('[data-testid="wf-announce"]')!.textContent).toBe("Moved Convene a panel to 3 of 5");
  });

  it("Alt+← at the start goes nowhere", () => {
    const onMove = vi.fn();
    const lane = render(h(FlowLane, { spec: linear(5), palette: PALETTE, mode: "design", onMove }));
    key(node(lane, "research"), "ArrowLeft", { altKey: true });
    expect(onMove).not.toHaveBeenCalled();
  });

  it("will not move a step across a branch", () => {
    const onMove = vi.fn();
    const lane = render(h(FlowLane, { spec: BRANCHED, palette: PALETTE, mode: "design", onMove }));
    key(node(lane, "review"), "ArrowRight", { altKey: true });
    expect(onMove).not.toHaveBeenCalled();
    expect(lane.querySelector('[data-testid="wf-announce"]')!.textContent).toBe("Edit branches in JSON");
  });

  it("arrows move focus along the flow, and between parallel siblings", () => {
    const lane = render(h(FlowLane, { spec: BRANCHED, palette: PALETTE, mode: "design", onMove: vi.fn() }));
    node(lane, "research").focus();
    key(node(lane, "research"), "ArrowRight");
    expect(document.activeElement).toBe(node(lane, "fan"));
    key(node(lane, "fan"), "ArrowRight");
    expect(document.activeElement).toBe(node(lane, "review"));
    key(node(lane, "review"), "ArrowDown");
    expect(document.activeElement).toBe(node(lane, "test"));
  });

  it("does nothing on a run: the lane is not editable there", () => {
    const onMove = vi.fn();
    const lane = render(h(FlowLane, { spec: linear(4), palette: PALETTE, mode: "run", runState: {}, onMove }));
    key(node(lane, "meeting"), "ArrowRight", { altKey: true });
    const start = centreOf(lane, "meeting");
    pointer("pointerdown", node(lane, "meeting"), start.x, start.y);
    pointer("pointermove", document.body, start.x + 200, start.y);
    pointer("pointerup", document.body, start.x + 200, start.y);
    expect(onMove).not.toHaveBeenCalled();
  });
});
