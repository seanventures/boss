// Assigning a bank's outside agent to a step from the console (2026-10-31). Setting only the
// captain left kind "capability" and no contract, and publishing refused the step.
import { describe, expect, it } from "vitest";

import { applyStepPatch } from "../lib/workflow-spec";

describe("assigning an outside agent", () => {
  it("makes the step an external_agent step with a default contract", () => {
    const step = { id: "bank_review", kind: "capability", binding: { capability: "review" }, next: ["x"] };
    const out = applyStepPatch(step as never, { externalAgent: "ext_123" } as never) as Record<string, unknown>;
    expect(out.kind).toBe("external_agent");
    expect(out.captain).toEqual({ external_agent: "ext_123" });
    expect(out.binding).toBeUndefined();
    expect(out.next).toEqual(["x"]);
    expect(out.contract).toEqual({ reads: [], writes: [{ kind: "agent_decision", required: true, decision_type: "bank_review" }] });
  });

  it("keeps a contract the step already has", () => {
    const contract = { reads: [{ kind: "clause" }], writes: [{ kind: "agent_decision", required: true, decision_type: "x" }] };
    const step = { id: "s", kind: "external_agent", captain: { external_agent: "ext_1" }, contract };
    const out = applyStepPatch(step as never, { externalAgent: "ext_2" } as never) as Record<string, unknown>;
    expect(out.contract).toEqual(contract);
  });
});
