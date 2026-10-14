/**
 * A step dragged off the lane's grid stays where it was put — in the spec, so a save and a
 * reload bring it back (#551) — and the spec stays one the engine runs unchanged.
 *
 * The rule: `layout.positions[stepId] = { dx, dy }`, an offset from the step's place on the grid
 * in Comfortable canvas units. Optional, drawing-only, and dropped for steps that are gone. The
 * store's PLACE / RESET_LAYOUT are undoable edits like any other; MOVE (a reorder) puts the step
 * back on the grid; REMOVE and RENAME_ID keep the layout in step with the steps.
 */
import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

import type { Spec } from "../lib/workflow-editor-types";
import {
  clearLayout, hasPlacedSteps, layoutOffsets, MAX_OFFSET, placeStep, pruneLayout, renameInLayout,
} from "../lib/workflow-layout";
import { linear } from "./_helpers/canvas-fixtures";
import { makeState, reducer } from "./_helpers/workflow-editor-fakes";
import {
  chooseDensity, offsetFromCanvas, offsetToCanvas, placedGeometry, PAD_X,
} from "../app/workflows/flow/geometry";
import { layoutFlow } from "../lib/workflow-graph";

const SPEC: Spec = linear(4);

describe("the layout in the spec", () => {
  it("a spec with no layout has no placed steps, and draws as it always has", () => {
    expect(layoutOffsets(SPEC).size).toBe(0);
    expect(hasPlacedSteps(SPEC)).toBe(false);
    expect("layout" in SPEC).toBe(false);
  });

  it("placing a step records its offset; placing it home removes the layout entirely", () => {
    const placed = placeStep(SPEC, "meeting", { dx: 40, dy: -64 });
    expect(placed.layout).toEqual({ positions: { meeting: { dx: 40, dy: -64 } } });
    expect(placed.steps).toBe(SPEC.steps);
    expect(layoutOffsets(placed).get("meeting")).toEqual({ dx: 40, dy: -64 });
    const home = placeStep(placed, "meeting", null);
    expect("layout" in home).toBe(false);
    expect(placeStep(placed, "meeting", { dx: 0, dy: 0 }).layout).toBeUndefined();
  });

  it("returns the same spec when nothing changes, and refuses a step it does not have", () => {
    const placed = placeStep(SPEC, "meeting", { dx: 40, dy: 8 });
    expect(placeStep(placed, "meeting", { dx: 40, dy: 8 })).toBe(placed);
    expect(placeStep(SPEC, "meeting", null)).toBe(SPEC);
    expect(placeStep(SPEC, "nope", { dx: 1, dy: 1 })).toBe(SPEC);
    expect(clearLayout(SPEC)).toBe(SPEC);
  });

  it("keeps what else the layout says, rounds and clamps offsets, and ignores junk", () => {
    const spec: Spec = { ...SPEC, layout: { zoom: "full", positions: {
      research: { dx: 10.6, dy: "x" }, meeting: { dx: 99999, dy: -99999 }, ghost: { dx: 5, dy: 5 },
      spec_review: "left",
    } } };
    const offsets = layoutOffsets(spec);
    expect([...offsets.keys()]).toEqual(["meeting"]);
    expect(offsets.get("meeting")).toEqual({ dx: MAX_OFFSET, dy: -MAX_OFFSET });
    const placed = placeStep(spec, "research", { dx: 10.6, dy: 3.2 });
    expect(placed.layout).toEqual({ zoom: "full", positions: {
      research: { dx: 11, dy: 3 }, meeting: { dx: MAX_OFFSET, dy: -MAX_OFFSET } } });
    expect(clearLayout(spec).layout).toEqual({ zoom: "full" });
  });

  it("prunes positions for steps that are gone, and a renamed step keeps its place", () => {
    const placed = placeStep(placeStep(SPEC, "meeting", { dx: 1, dy: 2 }), "research", { dx: 3, dy: 4 });
    const fewer = { ...placed, steps: placed.steps.filter((s) => s.id !== "research") };
    expect(pruneLayout(fewer).layout).toEqual({ positions: { meeting: { dx: 1, dy: 2 } } });
    expect(pruneLayout(placed)).toBe(placed);
    const renamed = { ...placed, steps: placed.steps.map((s) => (s.id === "meeting" ? { ...s, id: "panel" } : s)) };
    expect(renameInLayout(renamed, "meeting", "panel").layout)
      .toEqual({ positions: { panel: { dx: 1, dy: 2 }, research: { dx: 3, dy: 4 } } });
  });

  it("the engine carries a top-level key it does not read: the spec is normalised as {**spec, ...}", () => {
    // engine/boss_engine/services/workflows.py normalise_workflow_spec; pinned in Python by
    // engine/tests/test_a_designed_layout_is_carried_not_run.py.
    const engine = readFileSync("../../engine/boss_engine/services/workflows.py", "utf8");
    expect(engine).toMatch(/normalized = \{\s*\*\*spec,/);
  });
});

describe("the store", () => {
  it("PLACE and RESET_LAYOUT are edits: dirty, undoable, refused under broken JSON", () => {
    let s = makeState(SPEC);
    s = reducer(s, { type: "PLACE", id: "meeting", offset: { dx: 30, dy: 50 } });
    expect(s.spec.layout).toEqual({ positions: { meeting: { dx: 30, dy: 50 } } });
    expect(s.past).toHaveLength(1);
    s = reducer(s, { type: "RESET_LAYOUT" });
    expect("layout" in s.spec).toBe(false);
    s = reducer(s, { type: "UNDO" });
    expect(s.spec.layout).toEqual({ positions: { meeting: { dx: 30, dy: 50 } } });
    const broken = reducer(s, { type: "SET_JSON_TEXT", text: "{ nope" });
    expect(reducer(broken, { type: "PLACE", id: "meeting", offset: null })).toBe(broken);
  });

  it("a reordered step goes back on the grid; a removed one takes its place with it; a renamed one keeps it", () => {
    let s = makeState(SPEC);
    s = reducer(s, { type: "PLACE", id: "meeting", offset: { dx: 30, dy: 50 } });
    s = reducer(s, { type: "PLACE", id: "build", offset: { dx: -10, dy: 20 } });
    const moved = reducer(s, { type: "MOVE", id: "meeting", to: 2 });
    expect(moved.spec.layout).toEqual({ positions: { build: { dx: -10, dy: 20 } } });
    const removed = reducer(s, { type: "REMOVE", id: "build" });
    expect(removed.spec.layout).toEqual({ positions: { meeting: { dx: 30, dy: 50 } } });
    const renamed = reducer(s, { type: "RENAME_ID", from: "meeting", to: "panel" });
    expect(Object.keys((renamed.spec.layout as { positions: object }).positions).sort()).toEqual(["build", "panel"]);
  });
});

describe("the geometry", () => {
  const layout = layoutFlow(SPEC);
  const grid = chooseDensity(layout, new Map(), 4000);

  it("a stored offset is Comfortable units, scaled to Compact and back", () => {
    const o = { dx: 244, dy: 132 };
    expect(offsetToCanvas(o, "comfortable")).toEqual(o);
    const compact = offsetToCanvas(o, "compact");
    expect(compact).toEqual({ dx: 120, dy: 116 });
    expect(offsetFromCanvas(compact, "compact")).toEqual(o);
  });

  it("moves the placed box, grows the drawing to hold it, and keeps End to the right of every step", () => {
    const g = placedGeometry(grid, new Map([["build", { dx: 600, dy: 300 }]]));
    const before = grid.boxes.get("build")!;
    const after = g.boxes.get("build")!;
    expect(after).toEqual({ ...before, x: before.x + 600, y: before.y + 300 });
    expect(g.boxes.get("research")).toEqual(grid.boxes.get("research"));
    expect(g.height).toBeGreaterThan(after.y + after.h);
    expect(g.end.x).toBeGreaterThan(after.x + after.w);
    expect(g.width).toBeGreaterThan(g.end.x);
    expect(placedGeometry(grid, new Map())).toBe(grid);
  });

  it("never puts a step past the lane's top-left padding", () => {
    const g = placedGeometry(grid, new Map([["research", { dx: -5000, dy: -5000 }]]));
    const b = g.boxes.get("research")!;
    expect(b.x).toBe(PAD_X);
    expect(b.y).toBeGreaterThanOrEqual(0);
  });
});
