/**
 * The designer places Branch, Do at the same time and Repeat until — and only ever writes a spec
 * the engine runs.
 *
 * Each block lands whole, shaped the way engine/boss_engine/services/workflows.py reads it: a
 * condition whose two paths each start with a step of their own (a condition skips the first
 * step of the path it does not take, for good), a parallel naming its join, a join waiting for
 * every branch, a loop around one step. Removing a block takes all of it; a path keeps at least
 * one step.
 *
 * THE ROUND TRIP. The specs these edits produce are written to
 * tests/fixtures/designer-control-flow.json (UPDATE_DESIGNER_FIXTURE=1 rewrites it); this test
 * refuses a stale file, and engine/tests/test_a_designed_branch_is_one_the_runtime_can_run.py
 * runs every spec in it through the engine's own validator. Each condition reads back into the
 * builder it was made from.
 */
import { readFileSync, writeFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

import type { EditorAction, EditorState, Spec, SpecStep } from "../lib/workflow-editor-types";
import { editorReducer, emptyEditorState, newMeta } from "../lib/workflow-editor-store";
import {
  addBranch, blockRegion, canRemoveBlock, placeBlock, removeBlock, removeBranch, removeRefusal,
} from "../lib/workflow-blocks";
import { afterGap, edgesOf, linearOrder } from "../lib/workflow-graph";
import { fromExpression, toExpression, type Condition } from "../lib/workflow-expression";
import { PALETTE } from "./_helpers/workflow-fixtures";

const step = (spec: Spec, id: string) => spec.steps.find((s) => s.id === id) as SpecStep;
const task = (id: string, next: string[] = []): SpecStep => ({
  id, kind: "capability", next,
  binding: { agent_work: true, inputs: { instructions: { from_input: "instructions" } } },
});
const ONE: Spec = { entry_step: "draft", input_schema: { instructions: "string" }, steps: [task("draft")] };
const TWO: Spec = { entry_step: "draft", input_schema: { instructions: "string" },
                    steps: [task("draft", ["ship"]), task("ship")] };

const meta = newMeta({ name: "Designed" });
const load = (spec: Spec): EditorState =>
  editorReducer(emptyEditorState(), { type: "LOAD", spec: JSON.parse(JSON.stringify(spec)), meta });
const apply = (state: EditorState, ...actions: EditorAction[]) => actions.reduce(editorReducer, state);
const set = (id: string, config: Record<string, unknown>): EditorAction =>
  ({ type: "PATCH_STEP", id, patch: { config } });

describe("Branch", () => {
  it("lands as a condition with a step of its own on each path, both going on to what came next", () => {
    const { spec, head } = placeBlock(TWO, 1, "condition");
    expect(head).toBe("branch");
    expect(step(spec, "draft").next).toEqual(["branch"]);
    expect(step(spec, "branch")).toMatchObject({ kind: "condition", expression: {},
                                                 on_true: "if_yes", on_false: "if_no", next: [] });
    expect(step(spec, "if_yes")).toMatchObject({ title: "If yes", next: ["ship"] });
    expect(step(spec, "if_no")).toMatchObject({ title: "If no", next: ["ship"] });
    // Neither path's first step is reached from the other: the runtime would skip it for good.
    expect(edgesOf(spec).filter((e) => e.to === "if_no")).toEqual([{ from: "branch", to: "if_no", kind: "no" }]);
  });

  it("at the end of the flow, both paths end it", () => {
    const { spec } = placeBlock(ONE, -1, "condition");
    expect(step(spec, "if_yes").next).toEqual([]);
    expect(step(spec, "if_no").next).toEqual([]);
  });
});

describe("Do at the same time", () => {
  it("lands as a parallel naming its join, each branch into the join, the join waiting for all", () => {
    const { spec, head } = placeBlock(TWO, 1, "parallel");
    expect(step(spec, head!)).toMatchObject({ kind: "parallel", branches: ["branch_1", "branch_2"],
                                              join: "wait_for_branches" });
    expect(step(spec, "branch_1").next).toEqual(["wait_for_branches"]);
    expect(step(spec, "wait_for_branches")).toMatchObject({
      kind: "join", wait_for: ["branch_1", "branch_2"], success_policy: "all", next: ["ship"] });
  });

  it("gains and loses branches, never below two", () => {
    const placed = placeBlock(ONE, -1, "parallel").spec;
    const { spec, added } = addBranch(placed, "at_the_same_time");
    expect(added).toBe("branch_3");
    expect(step(spec, "at_the_same_time").branches).toEqual(["branch_1", "branch_2", "branch_3"]);
    expect(step(spec, "wait_for_branches").wait_for).toEqual(["branch_1", "branch_2", "branch_3"]);
    const fewer = removeBranch(spec, "at_the_same_time", "branch_2");
    expect(step(fewer, "at_the_same_time").branches).toEqual(["branch_1", "branch_3"]);
    expect(step(fewer, "wait_for_branches").wait_for).toEqual(["branch_1", "branch_3"]);
    expect(fewer.steps.some((s) => s.id === "branch_2")).toBe(false);
    expect(removeBranch(fewer, "at_the_same_time", "branch_1")).toBe(fewer);
  });
});

describe("Repeat until", () => {
  it("lands as a loop around one step, at most three times, going on to what came next", () => {
    const { spec, head } = placeBlock(TWO, 1, "bounded_loop");
    expect(step(spec, head!)).toMatchObject({ kind: "bounded_loop", body: "repeat_step",
                                              max_iterations: 3, expression: {}, next: ["ship"] });
    // The body returns to its loop: its own `next` is never followed, so it has none.
    expect(step(spec, "repeat_step").next).toEqual([]);
  });
});

describe("where a block may go", () => {
  it("at the head of another block's path, and after a path's last step", () => {
    const branched = placeBlock(ONE, -1, "condition").spec;
    const order = linearOrder(branched);
    const nested = placeBlock(branched, order.indexOf("if_yes"), "bounded_loop");
    expect(nested.head).toBe("repeat");
    expect(step(nested.spec, "branch").on_true).toBe("repeat");
    expect(step(nested.spec, "repeat").next).toEqual(["if_yes"]);
    const after = placeBlock(branched, afterGap(order.indexOf("if_no")), "parallel");
    expect(step(after.spec, "if_no").next).toEqual(["at_the_same_time"]);
  });

  it("never where its paths would end in another parallel's join", () => {
    const parallel = placeBlock(ONE, -1, "parallel").spec;
    const order = linearOrder(parallel);
    expect(placeBlock(parallel, afterGap(order.indexOf("branch_1")), "condition"))
      .toEqual({ spec: parallel, head: null });
    // A plain step there is fine: the join waits for it instead.
    expect(placeBlock(parallel, order.indexOf("wait_for_branches"), "condition").head).toBeNull();
  });
});

describe("removing a block", () => {
  it("takes the head, its paths and its join, and closes the flow up behind it", () => {
    for (const block of ["condition", "parallel", "bounded_loop"] as const) {
      const { spec, head } = placeBlock(TWO, 1, block);
      expect(canRemoveBlock(spec, head!)).toBe(true);
      expect(removeBlock(spec, head!)).toEqual(TWO);
    }
  });

  it("from the reducer: REMOVE on a head removes the block, and undo brings it back", () => {
    const placed = apply(load(TWO), { type: "PLACE_BLOCK", at: 1, block: "condition" });
    expect(placed.selection).toBe("branch");
    const removed = apply(placed, { type: "REMOVE", id: "branch" });
    expect(removed.spec.steps.map((s) => s.id)).toEqual(["draft", "ship"]);
    expect(apply(removed, { type: "UNDO" }).spec).toEqual(placed.spec);
  });

  it("is refused for a block whose paths are reached from outside it", () => {
    const spec = placeBlock(TWO, 1, "condition").spec;
    const tangled: Spec = { ...spec, steps: spec.steps.map((s) => (s.id === "draft"
      ? { ...s, next: ["branch", "if_no"] } : s)) };
    expect(blockRegion(tangled, "branch")).toBeNull();
    expect(removeBlock(tangled, "branch")).toBe(tangled);
  });

  it("says why a step inside a block cannot go", () => {
    const branch = placeBlock(ONE, -1, "condition").spec;
    expect(removeRefusal(branch, "if_yes")).toBe("Each path needs a step of its own: add another first");
    const loop = placeBlock(ONE, -1, "bounded_loop").spec;
    expect(removeRefusal(loop, "repeat_step")).toBe("A Repeat needs its one step: remove the Repeat instead");
    const parallel = placeBlock(ONE, -1, "parallel").spec;
    expect(removeRefusal(parallel, "wait_for_branches"))
      .toBe("Remove its Do at the same time step to remove this");
  });
});

/* ── The round trip: designer → JSON → engine validator → designer ─────────────────── */

const URGENT: Condition = { match: "any", negate: false, rules: [
  { path: "input.instructions", op: "contains", value: "urgent" },
  { path: "steps.draft.output.risk", op: "gte", value: 3 },
] };
const APPROVED: Condition = { match: "all", negate: true, rules: [
  { path: "context.approved", op: "eq", value: true },
] };
const TEAMS: Condition = { match: "all", negate: false, rules: [
  { path: "input.team", op: "in", value: ["billing", "support"] },
  { path: "steps.draft.output.risk", op: "exists" },
] };

/** Every spec the designer's own edits make, from a start form and one agent task. */
function designed(): Record<string, Spec> {
  const start = apply(load(ONE), { type: "PATCH_STEP", id: "draft", patch: { produces: ["risk"] } });
  const branch = apply(start, { type: "PLACE_BLOCK", at: -1, block: "condition" },
                       set("branch", { expression: toExpression(URGENT) }));
  const parallel = apply(start, { type: "PLACE_BLOCK", at: -1, block: "parallel" },
                         { type: "ADD_BRANCH", id: "at_the_same_time" },
                         set("wait_for_branches", { success_policy: "any" }));
  const loop = apply(start, { type: "PLACE_BLOCK", at: -1, block: "bounded_loop" },
                     { type: "PATCH_STEP", id: "repeat_step", patch: { produces: ["approved"] } },
                     set("repeat", { expression: toExpression(APPROVED), max_iterations: 5 }));
  // All three, nested: a Repeat at the head of the yes path, a parallel after the no path's last
  // step, and a condition reading a start-form field the flow declares.
  let nested = apply(branch, set("branch", { expression: toExpression(TEAMS) }),
                     { type: "SET_INPUT_SCHEMA", schema: { instructions: "string", team: "string" } });
  const order = linearOrder(nested.spec);
  nested = apply(nested, { type: "PLACE_BLOCK", at: order.indexOf("if_yes"), block: "bounded_loop" },
                 set("repeat", { expression: toExpression(APPROVED) }));
  const again = linearOrder(nested.spec);
  nested = apply(nested, { type: "PLACE_BLOCK", at: afterGap(again.indexOf("if_no")), block: "parallel" });
  const removed = apply(nested, { type: "REMOVE", id: "repeat" });
  return { branch: branch.spec, parallel: parallel.spec, loop: loop.spec, nested: nested.spec,
           nested_then_removed: removed.spec };
}

const FIXTURE = new URL("./fixtures/designer-control-flow.json", import.meta.url);

describe("the round trip", () => {
  const specs = designed();

  it("matches the fixture the engine validates (UPDATE_DESIGNER_FIXTURE=1 rewrites it)", () => {
    const body = { generated_by: "apps/web/tests/workflow-blocks.test.ts", specs };
    if (process.env.UPDATE_DESIGNER_FIXTURE) writeFileSync(FIXTURE, `${JSON.stringify(body, null, 2)}\n`);
    expect(JSON.parse(readFileSync(FIXTURE, "utf8"))).toEqual(body);
  });

  it("every condition reads back into the builder it was made from", () => {
    const fixture = JSON.parse(readFileSync(FIXTURE, "utf8")) as { specs: Record<string, Spec> };
    expect(fromExpression(step(fixture.specs.branch, "branch").expression)).toEqual(URGENT);
    expect(fromExpression(step(fixture.specs.loop, "repeat").expression)).toEqual(APPROVED);
    expect(fromExpression(step(fixture.specs.nested, "branch").expression)).toEqual(TEAMS);
  });

  it("declares the start-form field every placed agent task asks for", () => {
    for (const spec of Object.values(specs)) expect(spec.input_schema).toHaveProperty("instructions");
  });
});

it("the palette's fixture offers no branch blocks unless the server lists them", () => {
  expect(PALETTE.controls).toBeUndefined();
});
