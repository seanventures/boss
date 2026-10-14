// @vitest-environment happy-dom
/**
 * The workflow designer's canvas (#551): steps can be put anywhere and their arrows follow, no
 * words are drawn over each other, and every kind of block has a shape of its own.
 *
 *   1. MOVING. A step dragged and let go AWAY from every connector stays where it was let go —
 *      one `onPlace` with its offset from its place on the grid — and while it is dragged it and
 *      its arrows are drawn where it would land. Let go on a connector, it is reordered as before.
 *      Let go next to its own place, it goes back on the grid. Alt+Shift+arrows do the same from
 *      the keyboard; Tidy puts every step back.
 *   2. WORDS. The "parallel" label sits under its fork, never across the arrows fanning out of
 *      it; several ends get room before End; a loop's body is drawn at the bottom of its column,
 *      so its way back never runs through another step; a `+` is never drawn on a step.
 *   3. SHAPES. Branch is a diamond, Do at the same time a fork bar, its join a join bar, Repeat
 *      until a hexagon, Notify a send shape — each said to a screen reader too.
 *
 * Real pointer events against the real lane in happy-dom, as tests/lane-drag.test.ts does.
 */
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { readFileSync } from "node:fs";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Spec } from "../lib/workflow-editor-types";
import { BRANCHED, linear, PALETTE } from "./_helpers/canvas-fixtures";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const h = React.createElement;
const { FlowLane } = await import("../app/workflows/flow/flow-lane");
const { StepNode, nodeShape } = await import("../app/workflows/flow/step-node");
const { chooseDensity, CONN, END_ROOM, NODE, nodeHeight } = await import("../app/workflows/flow/geometry");
const { afterGap, insertStep, layoutFlow, linearOrder } = await import("../lib/workflow-graph");
const { gapAfterSelection, placeBlock } = await import("../lib/workflow-blocks");
const { stepView } = await import("../lib/step-view");

const WIDE = 4000;
const LANE = { top: 0, height: 600 };

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

function pointer(type: string, target: EventTarget, x: number, y: number) {
  act(() => {
    target.dispatchEvent(new PointerEvent(type, {
      clientX: x, clientY: y, pointerId: 1, button: 0, pointerType: "mouse", bubbles: true, cancelable: true,
    }));
  });
}
const frames = () => act(async () => { await new Promise((r) => setTimeout(r, 40)); });

const node = (lane: HTMLElement, id: string) => lane.querySelector<HTMLElement>(`.wf-node[data-step-id="${id}"]`)!;
const slot = (lane: HTMLElement, id: string) => lane.querySelector<HTMLElement>(`.wf-slot[data-slot="${id}"]`)!;
const at = (lane: HTMLElement, id: string) => ({ x: parseFloat(slot(lane, id).style.left), y: parseFloat(slot(lane, id).style.top) });

/** A press on a node at canvas (x, y): the mocked node rect sits at 0,0, so the grab offset is
 *  (x, y) and the step's top-left lands where the pointer goes minus that. */
function press(lane: HTMLElement, id: string) {
  const home = at(lane, id);
  const start = { x: home.x + 20, y: home.y + 20 };
  pointer("pointerdown", node(lane, id), start.x, start.y);
  return start;
}

/* ── 1. Moving ─────────────────────────────────────────────────────────────────── */

describe("a step can be put anywhere, and its arrows follow", () => {
  it("let go away from every connector, it is placed there: one onPlace with its offset, no MOVE", async () => {
    const onPlace = vi.fn();
    const onMove = vi.fn();
    const lane = render(h(FlowLane, { spec: linear(5), palette: PALETTE, mode: "design", onMove, onPlace }));
    const home = at(lane, "meeting");
    const start = press(lane, "meeting");
    // Straight down from where it was grabbed: far below the connectors (they sit on the row).
    pointer("pointermove", document.body, start.x + 40, start.y + 200);
    await frames();
    // Placing, the step is drawn where it would land and the lane draws curved arrows to it.
    expect(lane.querySelector<HTMLElement>('[data-testid="wf-caret"]')!.hidden).toBe(true);
    expect(slot(lane, "meeting").hasAttribute("data-origin")).toBe(true);
    expect(at(lane, "meeting")).toEqual({ x: 40, y: 200 });
    expect(lane.querySelector('[data-testid="wf-lane"]')!.getAttribute("data-linear")).toBe("false");
    const into = lane.querySelector<SVGPathElement>('.wf-edges g[data-kind="next"] path');
    expect(into).not.toBeNull();
    // Nothing else moved out of the way: a placed step does not reorder anything.
    expect(slot(lane, "spec_review").style.transform).toBe("");
    pointer("pointerup", document.body, start.x + 40, start.y + 200);
    expect(onMove).not.toHaveBeenCalled();
    expect(onPlace).toHaveBeenCalledTimes(1);
    expect(onPlace).toHaveBeenCalledWith("meeting", { dx: 40 - home.x, dy: 200 - home.y });
    expect(lane.querySelector('[data-testid="wf-announce"]')!.textContent).toBe("Placed Convene a panel");
  });

  it("let go on a connector, it is still reordered, not placed", () => {
    const onPlace = vi.fn();
    const onMove = vi.fn();
    const spec = linear(6);
    const lane = render(h(FlowLane, { spec, palette: PALETTE, mode: "design", onMove, onPlace }));
    const start = press(lane, "meeting");
    // The connector between build and review: just left of review's slot, on the row.
    const review = at(lane, "review");
    const y = review.y + parseFloat(slot(lane, "review").style.height) / 2;
    const target = review.x - CONN.comfortable / 2 - (parseFloat(slot(lane, "meeting").style.width) + CONN.comfortable);
    pointer("pointermove", document.body, target, y);
    pointer("pointermove", document.body, target + 1, y);
    pointer("pointerup", document.body, target + 1, y);
    expect(onPlace).not.toHaveBeenCalled();
    expect(onMove).toHaveBeenCalledTimes(1);
  });

  it("let go next to its own place, it goes back on the grid (onPlace with null)", () => {
    const onPlace = vi.fn();
    const spec: Spec = { ...linear(4), layout: { positions: { meeting: { dx: 0, dy: 300 } } } };
    const lane = render(h(FlowLane, { spec, palette: PALETTE, mode: "design", onMove: vi.fn(), onPlace }));
    const grid = chooseDensity(layoutFlow(linear(4)), new Map(), WIDE).boxes.get("meeting")!;
    const start = press(lane, "meeting");
    // The mocked node rect sits at 0,0, so the step's corner lands at (pointer - press): 3px off
    // its grid place, with the pointer itself far below the row's connectors.
    const to = { x: start.x + grid.x + 3, y: start.y + grid.y + 3 };
    pointer("pointermove", document.body, to.x, to.y);
    pointer("pointermove", document.body, to.x, to.y);
    pointer("pointerup", document.body, to.x, to.y);
    expect(onPlace).toHaveBeenCalledTimes(1);
    expect(onPlace.mock.calls[0]).toEqual(["meeting", null]);
    expect(lane.querySelector('[data-testid="wf-announce"]')!.textContent).toBe("Convene a panel is back in its place");
  });

  it("draws a placed step where the spec puts it, over the grid's steps, with its arrows following", () => {
    const spec: Spec = { ...linear(4), layout: { positions: { meeting: { dx: 30, dy: 200 } } } };
    const lane = render(h(FlowLane, { spec, palette: PALETTE, mode: "design", onMove: vi.fn(), onPlace: vi.fn(),
                                      onResetLayout: vi.fn() }));
    const grid = chooseDensity(layoutFlow(linear(4)), new Map(), WIDE).boxes.get("meeting")!;
    expect(at(lane, "meeting")).toEqual({ x: grid.x + 30, y: grid.y + 200 });
    expect(slot(lane, "meeting").dataset.placed).toBe("true");
    expect(slot(lane, "research").dataset.placed).toBeUndefined();
    // Curved arrows into and out of it, not the row's straight connectors.
    expect(lane.querySelector(".wf-conn")).toBeNull();
    const paths = [...lane.querySelectorAll<SVGPathElement>(".wf-edges path.wf-edge")].map((p) => p.getAttribute("d")!);
    const into = paths.find((d) => d.endsWith(`${grid.x + 30},${grid.y + 200 + grid.h / 2}`));
    expect(into).toBeDefined();
    expect(readFileSync("app/workflows/flow.css", "utf8")).toMatch(/\.wf-slot\[data-placed\]\s*\{\s*z-index:\s*1/);
  });

  it("Tidy puts every step back, and only shows when something was moved", () => {
    const onResetLayout = vi.fn();
    const plain = render(h(FlowLane, { spec: linear(3), palette: PALETTE, mode: "design", onMove: vi.fn(),
                                       onPlace: vi.fn(), onResetLayout }));
    expect(plain.querySelector('[data-testid="wf-tidy"]')).toBeNull();
    const spec: Spec = { ...linear(3), layout: { positions: { meeting: { dx: 30, dy: 200 } } } };
    const lane = render(h(FlowLane, { spec, palette: PALETTE, mode: "design", onMove: vi.fn(), onPlace: vi.fn(),
                                      onResetLayout }));
    act(() => lane.querySelector<HTMLElement>('[data-testid="wf-tidy"]')!.click());
    expect(onResetLayout).toHaveBeenCalledTimes(1);
    const ro = render(h(FlowLane, { spec, palette: PALETTE, mode: "design", readOnly: true, onResetLayout }));
    expect(ro.querySelector('[data-testid="wf-tidy"]')).toBeNull();
  });

  it("Alt+Shift+arrows nudge a step off the grid from the keyboard", () => {
    const onPlace = vi.fn();
    const onMove = vi.fn();
    const lane = render(h(FlowLane, { spec: linear(4), palette: PALETTE, mode: "design", onMove, onPlace }));
    act(() => { node(lane, "meeting").dispatchEvent(new KeyboardEvent("keydown",
      { key: "ArrowDown", altKey: true, shiftKey: true, bubbles: true, cancelable: true })); });
    expect(onMove).not.toHaveBeenCalled();
    expect(onPlace).toHaveBeenCalledWith("meeting", { dx: 0, dy: 16 });
  });

  it("a run draws the saved positions but nothing can be moved there", () => {
    const onPlace = vi.fn();
    const spec: Spec = { ...linear(3), layout: { positions: { meeting: { dx: 0, dy: 150 } } } };
    const lane = render(h(FlowLane, { spec, palette: PALETTE, mode: "run", runState: {}, onPlace }));
    const grid = chooseDensity(layoutFlow(linear(3)), new Map(), WIDE).boxes.get("meeting")!;
    expect(at(lane, "meeting").y).toBe(grid.y + 150);
    const start = press(lane, "meeting");
    pointer("pointermove", document.body, start.x, start.y + 200);
    pointer("pointerup", document.body, start.x, start.y + 200);
    expect(onPlace).not.toHaveBeenCalled();
  });
});

/* ── 2. Words that never sit on each other ──────────────────────────────────────── */

describe("no words are drawn over each other", () => {
  it("the parallel label sits under its fork, clear of every arrow leaving it", () => {
    const lane = render(h(FlowLane, { spec: BRANCHED, palette: PALETTE, mode: "design" }));
    const label = [...lane.querySelectorAll<SVGTextElement>(".wf-edge-label")].find((t) => t.textContent === "parallel")!;
    const fan = at(lane, "fan");
    const fanBottom = fan.y + parseFloat(slot(lane, "fan").style.height);
    expect(Number(label.getAttribute("y"))).toBeGreaterThan(fanBottom);
    expect(label.getAttribute("text-anchor")).toBe("end");
    expect(Number(label.getAttribute("x"))).toBe(fan.x + parseFloat(slot(lane, "fan").style.width));
  });

  it("several ends get room to meet before End", () => {
    const layout = layoutFlow(BRANCHED);
    expect(layout.terminal.length).toBeGreaterThan(1);
    const g = chooseDensity(layout, new Map(), WIDE);
    const right = Math.max(...layout.terminal.map((id) => g.boxes.get(id)!.x + g.boxes.get(id)!.w));
    expect(g.end.x - right).toBe(g.conn + END_ROOM);
    const one = chooseDensity(layoutFlow(linear(3)), new Map(), WIDE);
    expect(one.end.x - Math.max(...[...one.boxes.values()].map((b) => b.x + b.w))).toBe(one.conn);
  });

  it("a loop's body is drawn at the bottom of its column, so its way back crosses no step", () => {
    const spec = placeBlock(placeBlock({ entry_step: "a", steps: [
      { id: "a", kind: "capability", binding: { capability: "research" } }] }, -1, "bounded_loop").spec,
    -1, "condition").spec;
    // The loop's next step and its body share a column: the body goes under it.
    const layout = layoutFlow(spec);
    const body = layout.nodes.find((n) => n.id === "repeat_step")!;
    const sibling = layout.nodes.find((n) => n.column === body.column && n.id !== "repeat_step")!;
    expect(sibling).toBeDefined();
    expect(body.row).toBeGreaterThan(sibling.row);
    const lane = render(h(FlowLane, { spec, palette: PALETTE, mode: "design" }));
    const back = lane.querySelector<SVGPathElement>('.wf-edge[data-kind="back"]')!.getAttribute("d")!;
    const [, x, y] = /^M([\d.]+),([\d.]+)/.exec(back)!.map(Number);
    for (const s of lane.querySelectorAll<HTMLElement>(".wf-slot[data-slot]")) {
      if (s.dataset.slot === "repeat_step" || s.classList.contains("wf-slot-end")) continue;
      const left = parseFloat(s.style.left);
      const top = parseFloat(s.style.top);
      const crosses = x > left && x < left + parseFloat(s.style.width) && top + parseFloat(s.style.height) > y;
      expect(crosses, `the way back from the body runs through ${s.dataset.slot}`).toBe(false);
    }
  });

  it("a node is as tall as its rows at the console's root font, so a title never runs into the agent", () => {
    // app/layout-scale.css: 19px to 1440px, 22.8px at 1920, 24.7px from 2560.
    for (const rem of [16, 19, 22.8, 24.7]) {
      const compact = 2 * 0.875 * rem * 1.2 + Math.max(20, 0.875 * rem * 1.25) + 20 + 2 * 5 + 2 * 7 + 2;
      const comfy = 2 * 0.875 * rem * 1.25 + Math.max(20, 0.75 * rem * 1.25) + 16 + 2 * 5 + 2 * 10 + 2;
      expect(nodeHeight("compact", rem)).toBeGreaterThanOrEqual(Math.ceil(compact));
      expect(nodeHeight("comfortable", rem)).toBeGreaterThanOrEqual(Math.ceil(comfy));
    }
    // At a 16px root nothing changes from the sizes the lane was drawn at.
    expect(nodeHeight("compact")).toBe(NODE.compact.h);
    expect(nodeHeight("comfortable")).toBe(NODE.comfortable.h);
    // The lane reads the root font it is shown at.
    document.documentElement.style.fontSize = "22.8px";
    try {
      const lane = render(h(FlowLane, { spec: linear(3), palette: PALETTE, mode: "design" }));
      expect(parseFloat(slot(lane, "meeting").style.height)).toBe(nodeHeight("comfortable", 22.8));
    } finally {
      document.documentElement.style.fontSize = "";
    }
  });

  it("a + is never drawn on a step", () => {
    const spec: Spec = { ...BRANCHED, layout: { positions: { deploy: { dx: -260, dy: 0 } } } };
    const lane = render(h(FlowLane, { spec, palette: PALETTE, mode: "design", onInsert: vi.fn(), onMove: vi.fn(),
                                      renderGapAction: (n: number) => h("button", { "data-at": n }, "+") }));
    const boxes = [...lane.querySelectorAll<HTMLElement>(".wf-slot[data-slot]")].filter((s) => !s.classList.contains("wf-slot-end"))
      .map((s) => ({ x: parseFloat(s.style.left), y: parseFloat(s.style.top), w: parseFloat(s.style.width), h: parseFloat(s.style.height) }));
    for (const g of lane.querySelectorAll<HTMLElement>(".wf-gap")) {
      const x = parseFloat(g.style.left);
      const y = parseFloat(g.style.top);
      expect(boxes.some((b) => x > b.x + 4 && x < b.x + b.w - 4 && y > b.y + 4 && y < b.y + b.h - 4)).toBe(false);
    }
  });
});

/* ── 3. Shapes ─────────────────────────────────────────────────────────────────── */

describe("every kind of block has a shape of its own", () => {
  const LOOP: Spec = { entry_step: "again", steps: [
    { id: "again", kind: "bounded_loop", body: "build", max_iterations: 5, expression: "x" },
    { id: "build", kind: "capability", binding: { capability: "build" } },
  ] };

  it("Branch is a diamond, Do at the same time a fork, its join a join, Repeat a hexagon, Notify a send shape", () => {
    const lane = render(h(FlowLane, { spec: BRANCHED, palette: PALETTE, mode: "design" }));
    const shape = (id: string) => node(lane, id).dataset.shape;
    expect(shape("ok")).toBe("diamond");
    expect(shape("fan")).toBe("fork");
    expect(shape("gather")).toBe("join");
    expect(shape("notify")).toBe("send");
    expect(shape("research")).toBeUndefined();
    expect(shape("deploy")).toBeUndefined();
    for (const id of ["ok", "fan", "gather", "notify"]) {
      expect(node(lane, id).querySelector('[data-testid="wf-node-shape"]')).not.toBeNull();
    }
    expect(node(lane, "research").querySelector('[data-testid="wf-node-shape"]')).toBeNull();
    // The fork and the join carry their bar; the outline is never a plain rectangle path.
    expect(node(lane, "fan").querySelector(".wf-node-bar")).not.toBeNull();
    expect(node(lane, "gather").querySelector(".wf-node-bar")).not.toBeNull();
    const loop = render(h(FlowLane, { spec: LOOP, palette: PALETTE, mode: "design" }));
    expect(node(loop, "again").dataset.shape).toBe("hexagon");
  });

  it("the shape is said to a screen reader too, never only drawn", () => {
    const lane = render(h(FlowLane, { spec: BRANCHED, palette: PALETTE, mode: "design" }));
    const said = (id: string) => node(lane, id).querySelector(".wf-sr")?.textContent;
    expect(said("ok")).toBe("Branch: ");
    expect(said("fan")).toBe("Do at the same time: ");
    expect(said("gather")).toBe("Wait for branches: ");   // titled "Gather", from its id
    expect(said("notify")).toBeUndefined();   // titled "Notify"
    const loop = render(h(FlowLane, { spec: LOOP, palette: PALETTE, mode: "design" }));
    expect(node(loop, "again").querySelector(".wf-sr")?.textContent).toBe("Repeat until: ");
  });

  it("nodeShape names one shape per kind of block and none for a step that does work", () => {
    const view = (step: Spec["steps"][number]) => stepView(step, PALETTE);
    expect(nodeShape(view({ id: "c", kind: "condition" }))?.shape).toBe("diamond");
    expect(nodeShape(view({ id: "p", kind: "parallel" }))?.shape).toBe("fork");
    expect(nodeShape(view({ id: "j", kind: "join" }))?.shape).toBe("join");
    expect(nodeShape(view({ id: "l", kind: "bounded_loop" }))?.shape).toBe("hexagon");
    expect(nodeShape(view({ id: "n", kind: "capability", binding: { capability: "notify" } }))?.shape).toBe("send");
    expect(nodeShape(view({ id: "b", kind: "capability", binding: { capability: "build" } }))).toBeNull();
    expect(nodeShape(view({ id: "g", kind: "decision" }))).toBeNull();
  });

  it("a run colours the shape's outline by phase and keeps the outcome mark", () => {
    const view = stepView({ id: "ok", kind: "condition" }, PALETTE);
    const out = render(h(StepNode, { view, mode: "run", run: { phase: "failed", meta: "" } }));
    const btn = out.querySelector<HTMLElement>(".wf-node")!;
    expect(btn.dataset.shape).toBe("diamond");
    expect(btn.dataset.phase).toBe("failed");
    expect(out.querySelector('[data-testid="wf-node-mark"]')).not.toBeNull();
    const css = readFileSync("app/workflows/flow.css", "utf8");
    for (const s of ["diamond", "fork", "join", "hexagon", "send"]) expect(css).toContain(`[data-shape="${s}"]`);
    expect(css).toMatch(/\.wf-node\[data-shape\]\[data-phase="failed"\] \.wf-node-outline \{ stroke: var\(--red\)/);
  });
});

/* ── Where a palette pick lands ─────────────────────────────────────────────────── */

describe("a step picked from the palette goes after the selected one", () => {
  const BASE: Spec = { entry_step: "a", steps: [{ id: "a", kind: "capability", binding: { capability: "research" } }] };
  const probe = { id: "probe", kind: "capability", binding: { capability: "notify" } };

  it("after a whole Repeat, not into its body (which used to be refused)", () => {
    const spec = placeBlock(BASE, -1, "bounded_loop").spec;
    const order = linearOrder(spec);
    expect(insertStep(spec, order.indexOf("repeat") + 1, probe)).toBe(spec);   // the old gap
    const gap = gapAfterSelection(spec, "repeat");
    const placed = insertStep(spec, gap, probe);
    expect(placed).not.toBe(spec);
    expect(placed.steps.find((s) => s.id === "repeat")!.next).toEqual(["probe"]);
  });

  it("after a whole Do at the same time, past its join", () => {
    const spec = placeBlock(BASE, -1, "parallel").spec;
    const placed = insertStep(spec, gapAfterSelection(spec, "at_the_same_time"), probe);
    expect(placed.steps.find((s) => s.id === "wait_for_branches")!.next).toEqual(["probe"]);
  });

  it("after the last step of a path, on that path, not at the head of the next one", () => {
    const spec = placeBlock(BASE, -1, "condition").spec;
    const order = linearOrder(spec);
    expect(gapAfterSelection(spec, "if_yes")).toBe(afterGap(order.indexOf("if_yes")));
    const placed = insertStep(spec, gapAfterSelection(spec, "if_yes"), probe);
    expect(placed.steps.find((s) => s.id === "if_yes")!.next).toEqual(["probe"]);
    expect(placed.steps.find((s) => s.id === "branch")!.on_false).toBe("if_no");
  });

  it("a plain step hands to the next; nothing selected is the end", () => {
    const spec = linear(4);
    expect(gapAfterSelection(spec, "research")).toBe(1);
    expect(gapAfterSelection(spec, null)).toBe(-1);
  });
});
