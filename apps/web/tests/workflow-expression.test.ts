/**
 * What a condition checks, built from parts and written in the engine's own expression language
 * (workflows.py `evaluate_expression`). The builder reads back what it wrote; what it cannot show
 * as written (a group inside a group) is left to JSON rather than flattened.
 */
import { describe, expect, it } from "vitest";

import {
  expressionSources, fromExpression, parseList, parseValue, pathLabel, toExpression,
  type Condition,
} from "../lib/workflow-expression";
import { placeBlock } from "../lib/workflow-blocks";
import type { Spec } from "../lib/workflow-editor-types";
import { PALETTE } from "./_helpers/workflow-fixtures";

describe("to and from the engine's expression", () => {
  const cases: [string, Condition, unknown][] = [
    ["one comparison is written bare",
     { match: "all", negate: false, rules: [{ path: "input.urgent", op: "truthy" }] },
     { path: "input.urgent", op: "truthy" }],
    ["several are all / any",
     { match: "any", negate: false, rules: [{ path: "input.a", op: "eq", value: "x" },
                                             { path: "input.b", op: "gt", value: 2 }] },
     { any: [{ path: "input.a", op: "eq", value: "x" }, { path: "input.b", op: "gt", value: 2 }] }],
    ["a list compares as a list",
     { match: "all", negate: false, rules: [{ path: "input.team", op: "in", value: ["a", "b"] }] },
     { path: "input.team", op: "in", value: ["a", "b"] }],
    ["until is the engine's not",
     { match: "all", negate: true, rules: [{ path: "context.ok", op: "eq", value: true }] },
     { not: { path: "context.ok", op: "eq", value: true } }],
  ];
  it.each(cases)("%s", (_name, condition, expression) => {
    expect(toExpression(condition)).toEqual(expression);
    expect(fromExpression(expression)).toEqual(condition);
  });

  it("writes nothing to check as {}, which the validator refuses", () => {
    expect(toExpression({ match: "all", negate: false, rules: [] })).toEqual({});
    expect(fromExpression({})).toEqual({ match: "all", negate: false, rules: [] });
  });

  it("reads a comparison with no op as the engine does: truthy", () => {
    expect(fromExpression({ path: "input.x" })?.rules).toEqual([{ path: "input.x", op: "truthy" }]);
  });

  it("leaves to JSON what it cannot show as written", () => {
    for (const expression of [
      { all: [{ any: [{ path: "a" }] }] },           // a group inside a group
      { path: "input.x", equals: true },             // a key the builder does not draw
      { path: "input.x", op: "like" },               // a comparison the engine does not know
      { path: "input.x", op: "in", value: "a" },     // `in` with no list
      { not: { not: { path: "a" } } },
      { not: {} },
      [],
    ]) expect(fromExpression(expression)).toBeNull();
  });
});

describe("values as typed", () => {
  it("a number is a number only when it reads as one", () => {
    expect(parseValue("3.5", "number")).toBe(3.5);
    expect(parseValue("three", "number")).toBeNull();
    expect(parseValue("", "number")).toBeNull();
    expect(parseValue("true", "yesno")).toBe(true);
    expect(parseValue("3", "text")).toBe("3");
    expect(parseList("billing, , support", "text")).toEqual(["billing", "support"]);
    expect(parseList("1, x, 2", "number")).toEqual([1, 2]);
  });
});

describe("what a condition can read", () => {
  const start: Spec = {
    entry_step: "route", input_schema: { ticket: "string" },
    steps: [
      { id: "route", kind: "capability", next: ["draft"], binding: {
        typed_decision: { question: "Which team?", options: { billing: "Payments", technical: "Bugs" } },
        inputs: { state: { from_input: "ticket" } } } },
      { id: "draft", kind: "capability", next: [], title: "Draft a reply",
        binding: { agent_work: true, produces: ["reply"], inputs: { instructions: "Reply" } } },
    ],
  };

  it("the start form, then what each earlier step makes, with a typed decision's answers", () => {
    const { spec, head } = placeBlock(start, -1, "condition");
    const sources = expressionSources(spec, head!, PALETTE);
    expect(sources.map((s) => s.path)).toEqual([
      "input.ticket", "steps.route.output.answer", "steps.route.output.confident",
      "steps.draft.output.reply"]);
    expect(sources[1].suggestions).toEqual(["billing", "technical"]);
    expect(sources[3].label).toBe("Draft a reply · reply");
  });

  it("a loop reads its repeated step's latest value, not a list of rounds", () => {
    const { spec, head } = placeBlock(start, -1, "bounded_loop");
    const withProduces: Spec = { ...spec, steps: spec.steps.map((s) => (s.id === "repeat_step"
      ? { ...s, binding: { ...(s.binding ?? {}), produces: ["approved"] } } : s)) };
    expect(expressionSources(withProduces, head!, PALETTE).map((s) => s.path))
      .toContain("context.approved");
  });

  it("names a path no source names", () => {
    expect(pathLabel("steps.review.output.score")).toBe("review · score");
    expect(pathLabel("input.title")).toBe("Start form · title");
    expect(pathLabel("context.ok")).toBe("Latest ok");
  });
});
