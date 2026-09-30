import { describe, expect, it } from "vitest";

import { baseWorkflowName, uniqueWorkflowName } from "../app/workflow-names";

/**
 * "Standard Product Delivery (yours) (copy) v1".
 *
 * Forking appended "(yours)", copying appended "(copy)", and the result was three words of
 * bookkeeping inside the one string an operator reads — none of which said anything the row
 * did not already show.
 */
describe("naming a workflow made from another", () => {
  it("calls a copy what it is a copy of", () => {
    expect(uniqueWorkflowName("Standard Product Delivery", [])).toBe("Standard Product Delivery");
  });

  it("numbers a genuine collision instead of decorating it", () => {
    expect(uniqueWorkflowName("Minor Feature", ["Minor Feature"])).toBe("Minor Feature 2");
    expect(uniqueWorkflowName("Minor Feature", ["Minor Feature", "Minor Feature 2"]))
      .toBe("Minor Feature 3");
  });

  it("never compounds", () => {
    // Copying a copy produced "Minor Feature (copy) (copy)", and the next one was worse.
    const first = uniqueWorkflowName("Minor Feature", ["Minor Feature"]);
    const second = uniqueWorkflowName(first, ["Minor Feature", first]);
    expect(second).toBe("Minor Feature 3");
    expect(second).not.toContain("(");
  });

  it("cleans up the names earlier versions produced", () => {
    for (const ugly of [
      "Standard Product Delivery (yours) (copy)",
      "Standard Product Delivery (copy 2)",
      "Standard Product Delivery (yours)",
      "Standard Product Delivery (custom)",
    ]) {
      expect(baseWorkflowName(ugly)).toBe("Standard Product Delivery");
    }
  });

  it("leaves a name that was never decorated alone", () => {
    expect(baseWorkflowName("Security Hardening")).toBe("Security Hardening");
    expect(baseWorkflowName("Release 2024")).toBe("Release 2024");
  });

  it("does not strip a name into nothing", () => {
    // A workflow someone genuinely called "(copy)" still needs to be called something.
    expect(baseWorkflowName("(copy)")).toBe("(copy)");
  });

  it("ignores surrounding whitespace when deciding a name is taken", () => {
    expect(uniqueWorkflowName("Minor Feature", ["  Minor Feature  "])).toBe("Minor Feature 2");
  });
});
