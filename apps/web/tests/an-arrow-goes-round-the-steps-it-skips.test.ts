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
