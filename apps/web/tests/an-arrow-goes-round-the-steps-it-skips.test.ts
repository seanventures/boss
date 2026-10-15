/**
 * AN ARROW NEVER RUNS THROUGH A STEP (#551 follow-up). An arrow that skips a column — a Branch path
 * rejoining the other path further on, a short path ending at End past a longer one, a path
 * meeting a stretch it skipped — used to be a plain curve from its step to its target, drawn
 * straight through the steps between. It now goes round them, in a clear lane between rows or
 * over the top / under the bottom (geometry.ts `forwardRoute`).
 *
 * Every arrow is drawn from its route, so the check is on what is drawn: no point along any
 * arrow is inside a step other than the two it joins, at either density.
 */
import { describe, expect, it } from "vitest";

import type { Spec, SpecStep, StepView } from "../lib/workflow-editor-types";
import { BRANCHED, STRETCH } from "./_helpers/canvas-fixtures";

const { layoutFlow } = await import("../lib/workflow-graph");
const geometry = await import("../app/workflows/flow/geometry");
const { drawEdges } = await import("../app/workflows/flow/flow-lane");

const cap = (id: string, capability: string, next?: string[], extra: Partial<SpecStep> = {}): SpecStep =>
  ({ id, kind: "capability", binding: { capability }, ...(next ? { next } : {}), ...extra });

/** triage → ok? yes: fix → test → ship; no: ship. The No path skips fix and test. */
const REJOIN: Spec = {
  entry_step: "triage",
  steps: [
    cap("triage", "research", ["ok"]),
    { id: "ok", kind: "condition", expression: "needs a fix", on_true: "fix", on_false: "ship" },
    cap("fix", "build", ["test"]),
    cap("test", "test", ["ship"]),
    cap("ship", "deploy"),
  ],
};

/** a → b → c → d, and a goes straight to d as well: the fan-out skips b and c in one row. */
const SKIP: Spec = {
  entry_step: "a",
  steps: [cap("a", "research", ["b", "d"]), cap("b", "build", ["c"]), cap("c", "test", ["d"]), cap("d", "deploy")],
};

/** A short No path ends at End while the Yes path runs on: notify → End passes the long path. */
const SHORT_END: Spec = {
  entry_step: "ok",
  steps: [
    { id: "ok", kind: "condition", expression: "go", on_true: "a", on_false: "notify" },
    cap("a", "build", ["b"]), cap("b", "test", ["c"]), cap("c", "deploy"), cap("notify", "notify"),
  ],
};

/** An error path to a step several columns on: it curves there or runs under the steps between. */
const ERROR_SKIP: Spec = {
  entry_step: "a",
  steps: [cap("a", "research", ["b"], { on_error: "d" }), cap("b", "build", ["c"]), cap("c", "test", ["d"]), cap("d", "notify")],
};

const types = (spec: Spec) => new Map(spec.steps.map((s) => [s.id, (s.kind === "timer" ? "wait" : "step") as StepView["type"]]));

function lane(spec: Spec, density: "comfortable" | "compact") {
  const layout = layoutFlow(spec);
  const g = geometry.laneGeometry(layout, types(spec), density);
  return { layout, g, edges: drawEdges(layout, g, spec) };
}

const FLOWS: [string, Spec][] = [
  ["a Branch path rejoining the other", REJOIN], ["a fan-out skipping a stretch", SKIP],
  ["a short path ending at End", SHORT_END], ["an error path several columns on", ERROR_SKIP],
  ["the branched fixture", BRANCHED], ["a branch with a stretch inside it", STRETCH],
];

describe("no arrow runs through a step it does not join", () => {
  for (const [name, spec] of FLOWS) {
    for (const density of ["comfortable", "compact"] as const) {
      it(`${name} (${density})`, () => {
        const { g, edges } = lane(spec, density);
        expect(edges.length).toBeGreaterThan(0);
        for (const e of edges) {
          expect({ arrow: e.key, crosses: geometry.stepsCrossed(e.route, g.boxes, e.ends) })
            .toEqual({ arrow: e.key, crosses: [] });
          // What is checked is what is drawn.
          expect(e.d).toBe(geometry.routePath(e.route));
        }
      });
    }
  }
});

describe("the route round", () => {
  it("is taken only where the plain curve would cross a step", () => {
    const { layout, g, edges } = lane(REJOIN, "comfortable");
    const rejoin = edges.find((e) => e.key === "no:ok>ship")!;
    const box = (id: string) => g.boxes.get(id)!;
    const a = { x: box("ok").x + box("ok").w, y: box("ok").y + box("ok").h / 2 };
    const b = { x: box("ship").x, y: box("ship").y + box("ship").h / 2 };
    // The case is real: the plain curve runs through Build or Test.
    expect(geometry.stepsCrossed(geometry.curveRoute(a, b), g.boxes, ["ok", "ship"]).length).toBeGreaterThan(0);
    expect(rejoin.route.segs.length).toBe(3);
    // Its handle stays on it, near its head, as every arrow's does.
    expect(rejoin.grip!.x).toBeGreaterThan(box("test").x + box("test").w);
    // An arrow with nothing in the way is the plain curve, as before.
    const yes = edges.find((e) => e.key === "yes:ok>fix")!;
    expect(yes.route.segs.length).toBe(1);
    expect(layout.columns).toBeGreaterThan(3);
  });

  it("runs in a lane inside the drawing", () => {
    for (const [, spec] of FLOWS) {
      const { g, edges } = lane(spec, "compact");
      for (const e of edges) {
        for (const p of geometry.routeSamples(e.route)) {
          expect(p.y).toBeGreaterThanOrEqual(0);
          expect(p.y).toBeLessThanOrEqual(g.height);
        }
      }
    }
  });

  it("goes round steps moved into its way too", () => {
    const layout = layoutFlow(SKIP);
    const g = geometry.placedGeometry(geometry.laneGeometry(layout, types(SKIP), "comfortable"),
                                      new Map([["b", { dx: 0, dy: 40 }]]));
    for (const e of drawEdges(layout, g, SKIP)) expect(geometry.stepsCrossed(e.route, g.boxes, e.ends)).toEqual([]);
  });
});

describe("a Branch's paths keep to their labels", () => {
  it("a No path that goes round runs below, where its label is; a Yes path above", () => {
    const { edges } = lane(REJOIN, "comfortable");
    const no = edges.find((e) => e.key === "no:ok>ship")!;
    const lanes = no.route.segs.filter((s) => "line" in s).map((s) => ("line" in s ? s.line.y : 0));
    expect(lanes.length).toBe(1);
    expect(lanes[0]).toBeGreaterThan(no.route.from.y);
    expect(no.label!.y).toBeGreaterThan(no.route.from.y);
    const flipped: Spec = { ...REJOIN, steps: REJOIN.steps.map((s) => (s.id === "ok"
      ? { ...s, on_true: "ship", on_false: "fix" } : s)) };
    const yes = lane(flipped, "comfortable").edges.find((e) => e.key === "yes:ok>ship")!;
    const above = yes.route.segs.filter((s) => "line" in s).map((s) => ("line" in s ? s.line.y : 0));
    expect(above[0]).toBeLessThan(yes.route.from.y);
    expect(yes.label!.y).toBeLessThan(yes.route.from.y);
  });
});

describe("a step put down close under the one before it", () => {
  it("is reached by a tighter curve, not one that bows through the step beside it", () => {
    // The walk's case: Repeat → Notify, with Notify put down under Repeat's own step, a few px
    // to the right of Repeat. The plain curve bows out through that step.
    const boxes = new Map([
      ["repeat", { x: 900, y: 228, w: 103, h: 88 }],
      ["body", { x: 1010, y: 276, w: 96, h: 40 }],
      ["notify", { x: 1020, y: 320, w: 104, h: 86 }],
    ]);
    const g = { boxes, conn: 56, height: 500 };
    const a = { x: 1003, y: 272 };
    const b = { x: 1020, y: 363 };
    expect(geometry.stepsCrossed(geometry.curveRoute(a, b), boxes, ["repeat", "notify"])).toEqual(["body"]);
    const r = geometry.forwardRoute(g, a, b, ["repeat", "notify"]);
    expect(geometry.stepsCrossed(r, boxes, ["repeat", "notify"])).toEqual([]);
  });
});

describe("a step put down over another", () => {
  it("is still reached: the step lying over where its arrow arrives is not counted as crossed", () => {
    // The walk's shape: Notify put down over Repeat's own step, its left edge inside that step.
    const boxes = new Map([
      ["repeat", { x: 608, y: 76, w: 104, h: 107 }],
      ["body", { x: 728, y: 137, w: 104, h: 107 }],
      ["notify", { x: 756, y: 182, w: 104, h: 107 }],
    ]);
    const a = { x: 712, y: 129 };
    const b = { x: 756, y: 235 };
    const r = geometry.forwardRoute({ boxes, conn: 56, height: 400 }, a, b, ["repeat", "notify"]);
    expect(geometry.stepsCrossed(r, boxes, ["repeat", "notify"])).toEqual([]);
    // A step it could have gone round still counts.
    const blocker = new Map([...boxes, ["other", { x: 300, y: 76, w: 104, h: 107 }]]);
    expect(geometry.stepsCrossed(geometry.curveRoute({ x: 200, y: 129 }, { x: 500, y: 129 }), blocker, [])).toEqual(["other"]);
  });
});
