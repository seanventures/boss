/**
 * Dry run 4, gap #8: the Start form drew "Merge (you approve)" before Review and a second,
 * lowercase "merge" for a workflow that has its own merge step; the target path was typed by
 * hand although the uploaded file's name and the field's own example already say it.
 */
import { describe, expect, it } from "vitest";

import { counts, timelineOf, type Ask, type InstalledStep } from "../app/workflows/compose-plan";
import { suggestedTarget, type WorkflowInputField } from "../lib/workflow-inputs";

const ONBOARD: InstalledStep[] = [
  { id: "build", approval: "auto", capability: "build" },
  { id: "review", approval: "auto" },
  { id: "merge", label: "merge", approval: "auto" },
  { id: "staging", label: "Staging", approval: "manual", capability: "deploy" },
  { id: "contract_tests", label: "Contract tests", approval: "auto" },
];

describe("a workflow with its own merge step is asked to merge there, once", () => {
  it("draws Merge in its own place, after Review, never twice", () => {
    const asks: Ask[] = [{ step_id: "build", kind: "merge", who: "you" },
                         { step_id: "staging", kind: "gate", who: "you" }];
    const t = timelineOf({ steps: ONBOARD, mode: "new", asks });
    expect(t.map((s) => s.name)).toEqual(["Build", "Review", "Merge", "Staging", "Contract tests"]);
    expect(t.find((s) => s.id === "merge")!.who.kind).toBe("you");
    expect(counts(t)).toEqual({ steps: 5, approvals: 2 });
  });

  it("a workflow without one still asks after its build", () => {
    const t = timelineOf({ steps: ONBOARD.filter((s) => s.id !== "merge"), mode: "new" });
    expect(t.map((s) => s.name).slice(0, 2)).toEqual(["Build", "Merge"]);
  });
});

describe("the target path starts from the uploaded file", () => {
  const field = { name: "target_path", label: "Where the fixed file goes", required: true, example: "",
                  help: "Its path in the repository, e.g. config/tenants/name.yaml." } as WorkflowInputField;
  it("is the field's folder plus the file's name", () => {
    expect(suggestedTarget("lumen-pay.yaml", field)).toBe("config/tenants/lumen-pay.yaml");
  });
  it("never replaces a path already typed", () => {
    expect(suggestedTarget("lumen-pay.yaml", field, "config/prod/x.yaml")).toBe("config/prod/x.yaml");
  });
  it("is the file's name when the field names no folder", () => {
    expect(suggestedTarget("lumen-pay.yaml", { ...field, help: "Where it goes." })).toBe("lumen-pay.yaml");
  });
});
