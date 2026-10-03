/**
 * The workflow graph: what the lane draws, and the only edits a canvas may make to it.
 *
 * The old builder rewrote every step's `next` from the order steps were drawn in, so a saved
 * parallel review came back as a straight line. These hold the replacement to the runtime's
 * own reading of a spec, and hold every structural edit to a linear stretch — an edit that
 * would touch a branch, a join or a loop body must hand back the very same spec.
 */
import { describe, expect, it } from "vitest";

import {
  canDuplicate, canMove, canRemove, duplicateStep, edgesOf, insertStep, isLinear, layoutFlow, linearOrder, moveStep,
  removeStep, renameStepId, upstreamOf, wireInputs,
} from "../lib/workflow-graph";
import type { Spec, SpecStep } from "../lib/workflow-editor-types";
import { BRANCHES, LINEAR, PALETTE, PARALLEL, clone } from "./_helpers/workflow-fixtures";

const step = (spec: Spec, id: string) => spec.steps.find((s) => s.id === id)!;
const fresh = (id: string, extra: Partial<SpecStep> = {}): SpecStep =>
  ({ id, kind: "capability", binding: { capability: "notify", inputs: {} }, next: [], ...extra });
/** The chain from the entry, following `next`. */
const chain = (spec: Spec) => {
  const out: string[] = [];
  let at: string | undefined = spec.entry_step;
  while (at && !out.includes(at)) {
    out.push(at);
    const next = step(spec, at).next;
    at = Array.isArray(next) ? next[0] : next || undefined;
  }
  return out;
};

describe("edges are the runtime's", () => {
  it("draws a parallel as branches into a join, each wait_for once", () => {
    expect(edgesOf(PARALLEL)).toEqual([
      { from: "intake", to: "fan", kind: "next" },
      { from: "fan", to: "review_a", kind: "branch" },
      { from: "fan", to: "review_b", kind: "branch" },
      { from: "review_a", to: "review_a2", kind: "next" },
      { from: "review_a2", to: "merge", kind: "join" },
      { from: "review_b", to: "merge", kind: "join" },
      { from: "merge", to: "ship", kind: "next" },
      { from: "ship", to: "tell", kind: "next" },
    ]);
  });

  it("draws a condition's paths as yes/no and a loop's body as a loop edge", () => {
    const edges = edgesOf(BRANCHES);
    expect(edges).toContainEqual({ from: "check", to: "hotfix", kind: "yes" });
    expect(edges).toContainEqual({ from: "check", to: "plan", kind: "no" });
    expect(edges).toContainEqual({ from: "poll", to: "probe", kind: "loop" });
    expect(edges).toContainEqual({ from: "poll", to: "done", kind: "next" });
  });

  it("ignores a reference to a step that does not exist (half-typed JSON)", () => {
    const spec = clone(LINEAR);
    step(spec, "deploy").next = ["nowhere"];
    expect(edgesOf(spec).some((e) => e.to === "nowhere")).toBe(false);
  });

  it("is linear only for one unbroken chain with no branch constructs", () => {
    expect(isLinear(LINEAR)).toBe(true);
    expect(isLinear({ steps: [] })).toBe(true);
    expect(isLinear(PARALLEL)).toBe(false);
    expect(isLinear(BRANCHES)).toBe(false);
    const broken = clone(LINEAR);
    step(broken, "build").next = [];
    expect(isLinear(broken)).toBe(false);
    const stringNext = clone(LINEAR);
    step(stringNext, "research").next = "build";
    expect(isLinear(stringNext)).toBe(true);
  });
});

describe("order and layout", () => {
  it("keeps each branch contiguous, in the order the parallel names them", () => {
    expect(linearOrder(PARALLEL)).toEqual(
      ["intake", "fan", "review_a", "review_a2", "review_b", "merge", "ship", "tell"]);
  });

  it("puts every step in a column by its longest path, siblings stacked", () => {
    const layout = layoutFlow(PARALLEL);
    const at = Object.fromEntries(layout.nodes.map((n) => [n.id, n]));
    expect(at.fan.column).toBe(1);
    expect([at.review_a.column, at.review_b.column]).toEqual([2, 2]);
    expect([at.review_a.row, at.review_b.row]).toEqual([0, 1]);
    expect(at.review_a.rowsInColumn).toBe(2);
    expect(at.review_a2.column).toBe(3);
    // The join waits for the longest branch.
    expect(at.merge.column).toBe(4);
    expect(layout.columns).toBe(7);
    expect(layout.maxRows).toBe(2);
    expect(layout.linear).toBe(false);
    expect(layout.terminal).toEqual(["tell"]);
  });

  it("lays a straight line out in one row", () => {
    const layout = layoutFlow(LINEAR);
    expect(layout.nodes.map((n) => [n.id, n.column, n.row]))
      .toEqual([["research", 0, 0], ["build", 1, 0], ["deploy", 2, 0]]);
    expect(layout.linear).toBe(true);
  });

  it("does not call a loop body an end of the flow", () => {
    expect(layoutFlow(BRANCHES).terminal).toEqual(["done"]);
  });

  it("survives a cycle typed into the JSON", () => {
    const spec = clone(LINEAR);
    step(spec, "deploy").next = ["research"];
    const layout = layoutFlow(spec);
    expect(layout.nodes).toHaveLength(3);
    expect(new Set(layout.nodes.map((n) => n.column)).size).toBe(3);
  });

  it("lists what runs before a step on any path into it, in flow order", () => {
    expect(upstreamOf(PARALLEL, "merge")).toEqual(
      ["intake", "fan", "review_a", "review_a2", "review_b"]);
    expect(upstreamOf(PARALLEL, "review_b")).toEqual(["intake", "fan"]);
    expect(upstreamOf(BRANCHES, "probe")).toEqual(["check", "hotfix", "plan", "poll"]);
  });
});

describe("renaming a step rewrites every reference to it", () => {
  it("next, entry_step and from_step", () => {
    const renamed = renameStepId(LINEAR, "research", "dig");
    expect(renamed.entry_step).toBe("dig");
    expect(step(renamed, "dig").next).toEqual(["build"]);
    expect((step(renamed, "build").binding as { inputs: Record<string, unknown> }).inputs.brief)
      .toEqual({ from_step: "dig", field: "dossier" });
  });

  it("branches, join, wait_for, on_true, on_false and body", () => {
    const p = renameStepId(renameStepId(PARALLEL, "review_b", "qa_pass"), "merge", "gather");
    expect(step(p, "fan").branches).toEqual(["review_a", "qa_pass"]);
    expect(step(p, "fan").join).toBe("gather");
    expect(step(p, "gather").wait_for).toEqual(["review_a2", "qa_pass"]);
    expect(step(p, "qa_pass").next).toEqual(["gather"]);
    const b = renameStepId(renameStepId(renameStepId(BRANCHES, "hotfix", "fast"), "plan", "slow"),
                           "probe", "ping");
    expect(step(b, "check")).toMatchObject({ on_true: "fast", on_false: "slow" });
    expect(step(b, "poll").body).toBe("ping");
  });

  it("expression paths, so a loop's exit test still reads its body", () => {
    const b = renameStepId(BRANCHES, "probe", "ping");
    expect(step(b, "poll").expression).toEqual({ path: "steps.ping.output.ok", equals: false });
    // A path that does not read the renamed step is the same object.
    expect(step(b, "check").expression).toBe(step(BRANCHES, "check").expression);
  });

  it("a typed decision's routing, through all/any/not and the $ prefix", () => {
    const spec: Spec = { entry_step: "route", steps: [
      { id: "route", kind: "capability", next: ["pick"],
        binding: { typed_decision: { question: "Which team?", options: ["billing", "tech"] },
                   inputs: {} } },
      { id: "pick", kind: "condition", on_true: "bill", on_false: "tech", next: [],
        expression: { all: [
          { path: "steps.route.output.answer", op: "eq", value: "billing" },
          { not: { any: [{ path: "$.steps.route.output.confident", op: "eq", value: false },
                         { path: "steps.router.output.x" }, { path: "input.route" }] } },
        ] } },
      fresh("bill"), fresh("tech"),
    ] };
    const renamed = renameStepId(spec, "route", "triage");
    expect(step(renamed, "pick").expression).toEqual({ all: [
      { path: "steps.triage.output.answer", op: "eq", value: "billing" },
      { not: { any: [{ path: "$.steps.triage.output.confident", op: "eq", value: false },
                     { path: "steps.router.output.x" }, { path: "input.route" }] } },
    ] });
    expect(step(renamed, "triage").next).toEqual(["pick"]);
  });

  it("refuses a name another step has, and leaves the spec as it was", () => {
    expect(renameStepId(LINEAR, "research", "build")).toBe(LINEAR);
  });
});

describe("wiring follows the server's rule", () => {
  it("reads from the nearest earlier step that makes the input", () => {
    const spec = insertStep(LINEAR, 3, fresh("smoke", { binding: { capability: "smoke_test",
                                                                   inputs: {} } }));
    const result = wireInputs(spec, PALETTE, ["smoke"]);
    expect(result.wired).toEqual([{ step_id: "smoke", input: "url",
                                    source: { from_step: "deploy", field: "url" } }]);
  });

  it("falls back to the start form only when the start form declares the input", () => {
    const declared = wireInputs({ ...LINEAR, steps: [fresh("r", {
      binding: { capability: "meeting", inputs: {} } })], entry_step: "r" }, PALETTE);
    expect(declared.wired).toEqual(
      [{ step_id: "r", input: "directive", source: { from_input: "directive" } }]);
    // `message` and `channel` are neither made upstream nor declared: left unset, on purpose.
    const undeclared = wireInputs({ entry_step: "n", steps: [fresh("n")] }, PALETTE);
    expect(undeclared.wired).toEqual([]);
    expect(undeclared.spec).toEqual({ entry_step: "n", steps: [fresh("n")] });
  });

  it("never reads from a sibling branch", () => {
    const spec = clone(PARALLEL);
    step(spec, "review_a").binding = { capability: "research", inputs: { topic: "x" } };
    step(spec, "review_b").binding = { capability: "smoke_test", inputs: {} };
    // review_a makes `dossier`, not `url`; and even a producer in review_a would be a sibling.
    expect(wireInputs(spec, PALETTE, ["review_b"]).wired).toEqual([]);
  });

  it("leaves an input that is already set alone", () => {
    expect(wireInputs(LINEAR, PALETTE).spec).toBe(LINEAR);
  });
});

describe("inserting", () => {
  it("splices into a straight line at any gap, and nothing else changes", () => {
    const at1 = insertStep(LINEAR, 1, fresh("note"));
    expect(chain(at1)).toEqual(["research", "note", "build", "deploy"]);
    expect(step(at1, "build")).toBe(step(LINEAR, "build"));
    const front = insertStep(LINEAR, 0, fresh("note"));
    expect(front.entry_step).toBe("note");
    expect(chain(front)).toEqual(["note", "research", "build", "deploy"]);
    const end = insertStep(LINEAR, -1, fresh("note"));
    expect(chain(end)).toEqual(["research", "build", "deploy", "note"]);
    expect(end.input_schema).toBe(LINEAR.input_schema);
  });

  it("starts an empty workflow", () => {
    expect(insertStep({ steps: [] }, -1, fresh("first")))
      .toEqual({ steps: [{ ...fresh("first"), next: [] }], entry_step: "first" });
  });

  it("splices into a linear stretch of a branched flow and keeps every branch", () => {
    const at = linearOrder(PARALLEL).indexOf("review_a2");
    const spec = insertStep(PARALLEL, at, fresh("lint"));
    expect(step(spec, "review_a").next).toEqual(["lint"]);
    expect(step(spec, "lint").next).toEqual(["review_a2"]);
    expect(step(spec, "fan")).toBe(step(PARALLEL, "fan"));
    expect(step(spec, "merge")).toBe(step(PARALLEL, "merge"));
    const after = insertStep(PARALLEL, linearOrder(PARALLEL).indexOf("tell"), fresh("pause"));
    expect(step(after, "ship").next).toEqual(["pause"]);
  });

  it("refuses a gap that crosses a branch boundary", () => {
    const order = linearOrder(PARALLEL);
    for (const before of ["review_a", "review_b", "merge"]) {
      expect(insertStep(PARALLEL, order.indexOf(before), fresh("x"))).toBe(PARALLEL);
    }
    // After the condition, into a merge point, and after a flow with two ends.
    expect(insertStep(BRANCHES, linearOrder(BRANCHES).indexOf("hotfix"), fresh("x"))).toBe(BRANCHES);
    expect(insertStep(BRANCHES, linearOrder(BRANCHES).indexOf("poll"), fresh("x"))).toBe(BRANCHES);
  });

  it("refuses an id the spec already has", () => {
    expect(insertStep(LINEAR, 0, fresh("build"))).toBe(LINEAR);
  });
});

describe("branch structure is never placed or copied visually", () => {
  it("insert refuses a parallel, condition, join or loop, or a step carrying their keys", () => {
    for (const extra of [{ kind: "parallel", branches: [] }, { kind: "condition" },
                         { kind: "join", wait_for: ["build"] }, { kind: "bounded_loop" },
                         { body: "build" }, { on_true: "build" }, { wait_for: [] }]) {
      expect(insertStep(LINEAR, -1, fresh("x", extra as Partial<SpecStep>))).toBe(LINEAR);
    }
    expect(insertStep({ steps: [] }, 0, fresh("x", { kind: "parallel" }))).toEqual({ steps: [] });
  });

  it("a step in a linear stretch duplicates; a branch construct does not", () => {
    const copied = duplicateStep(PARALLEL, "review_a", "review_a_2");
    expect(linearOrder(copied).slice(2, 5)).toEqual(["review_a", "review_a_2", "review_a2"]);
    expect(canDuplicate(PARALLEL, "review_a")).toBe(true);
    for (const id of ["fan", "merge"]) {
      expect(duplicateStep(PARALLEL, id, `${id}_2`)).toBe(PARALLEL);
      expect(canDuplicate(PARALLEL, id)).toBe(false);
    }
    for (const id of ["check", "poll"]) expect(canDuplicate(BRANCHES, id)).toBe(false);
    expect(duplicateStep(LINEAR, "ghost", "ghost_2")).toBe(LINEAR);
  });
});

describe("moving", () => {
  it("leaves the step at the index asked for (array-move)", () => {
    expect(chain(moveStep(LINEAR, "research", 1))).toEqual(["build", "research", "deploy"]);
    expect(chain(moveStep(LINEAR, "research", 2))).toEqual(["build", "deploy", "research"]);
    expect(chain(moveStep(LINEAR, "deploy", 0))).toEqual(["deploy", "research", "build"]);
    expect(moveStep(LINEAR, "deploy", 0).entry_step).toBe("deploy");
  });

  it("keeps a string `next` a string", () => {
    const spec = clone(LINEAR);
    for (const s of spec.steps) s.next = Array.isArray(s.next) ? (s.next[0] ?? "") : s.next;
    const moved = moveStep(spec, "research", 1);
    expect(step(moved, "build").next).toBe("research");
    expect(step(moved, "research").next).toBe("deploy");
  });

  it("is refused onto its own place, out of range, or across a branch", () => {
    expect(moveStep(LINEAR, "build", 1)).toBe(LINEAR);
    expect(moveStep(LINEAR, "build", 9)).toBe(LINEAR);
    const order = linearOrder(PARALLEL);
    expect(canMove(PARALLEL, "ship", order.indexOf("review_b"))).toBe(false);
    expect(canMove(PARALLEL, "review_b", order.indexOf("tell"))).toBe(false);
    expect(canMove(PARALLEL, "fan", 0)).toBe(false);
    // Within the flow's own straight tail it is allowed.
    expect(canMove(PARALLEL, "tell", order.indexOf("ship"))).toBe(true);
  });
});

describe("removing", () => {
  it("reconnects the step before to the step after", () => {
    const spec = removeStep(LINEAR, "build");
    expect(chain(spec)).toEqual(["research", "deploy"]);
  });

  it("unsets an input that read from the removed step, so it shows as unwired", () => {
    const spec = removeStep(LINEAR, "build");
    const inputs = (step(spec, "deploy").binding as { inputs: Record<string, unknown> }).inputs;
    expect("run_id" in inputs).toBe(false);
  });

  it("moves the entry on when the first step goes, and empties cleanly", () => {
    expect(removeStep(LINEAR, "research").entry_step).toBe("build");
    const one: Spec = { entry_step: "a", steps: [fresh("a")] };
    expect(removeStep(one, "a")).toEqual({ steps: [] });
  });

  it("reconnects every path into a merge point", () => {
    const spec = clone(BRANCHES);
    spec.steps.push(fresh("tail"));
    step(spec, "hotfix").next = ["mid"];
    step(spec, "plan").next = ["mid"];
    spec.steps.push(fresh("mid", { next: ["poll"] }));
    const removed = removeStep(spec, "mid");
    expect(step(removed, "hotfix").next).toEqual(["poll"]);
    expect(step(removed, "plan").next).toEqual(["poll"]);
  });

  it("is refused for anything a branch names", () => {
    for (const id of ["fan", "review_a", "review_a2", "review_b", "merge"]) {
      expect(canRemove(PARALLEL, id)).toBe(false);
      expect(removeStep(PARALLEL, id)).toBe(PARALLEL);
    }
    for (const id of ["check", "hotfix", "poll", "probe"]) {
      expect(removeStep(BRANCHES, id)).toBe(BRANCHES);
    }
    expect(canRemove(PARALLEL, "tell")).toBe(true);
  });
});
