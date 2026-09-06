import { describe, expect, it } from "vitest";

import { decidedButNotMoved, needsYou, type RunStep } from "../app/delivery-workflows-ui";
import { codeOnly } from "./_helpers/source";

/**
 * "When I clicked on Approve and Decide, after approving from Approval page it comes back to
 * the Workflow page and displays the same thing again."
 *
 * The step was still `waiting · gate · release_manager` with the same "Review and decide →"
 * button, so the approval looked like it had not landed. It had: approval advances the step,
 * but a WORKER does the advancing — measured on live rows, 50 seconds on a good run and hours
 * on a slow one. This view read `step.status === "waiting"` and nothing else, so an answered
 * gate was indistinguishable from an unanswered one for that entire window.
 *
 * Worse for a gate that EXPIRED: two live steps sit waiting on decisions whose status is
 * `expired`, and the button offered them for ever.
 */
const step = (over: Partial<RunStep> = {}): RunStep => ({
  step_id: "deploy", kind: "gate", status: "waiting", capability: "deploy",
  output: {}, evidence: [], error: null, approval: {}, due_at: null,
  timeout_applied: null, attempt: 1, started_at: null, completed_at: null,
  decision_id: "dec_1", ...over,
} as RunStep);

describe("a gate that can still be answered", () => {
  it("is offered", () => {
    expect(needsYou(step({ decision_status: "pending" }))).toBe(true);
  });

  it("including one that was delegated", () => {
    expect(needsYou(step({ decision_status: "delegated" }))).toBe(true);
  });

  it("and one whose status the API did not send", () => {
    // Offering an answerable gate is recoverable; hiding one is not.
    expect(needsYou(step({ decision_status: undefined }))).toBe(true);
  });
});

describe("a gate that has been answered", () => {
  it("is not offered again", () => {
    expect(needsYou(step({ decision_status: "approved" }))).toBe(false);
  });

  it("says the click landed", () => {
    expect(decidedButNotMoved(step({ decision_status: "approved" }))).toBe("approved");
  });

  it("and an expired one says that instead", () => {
    expect(needsYou(step({ decision_status: "expired" }))).toBe(false);
    expect(decidedButNotMoved(step({ decision_status: "expired" }))).toBe("expired");
  });

  it("a rejected gate is neither offered nor silent", () => {
    expect(needsYou(step({ decision_status: "rejected" }))).toBe(false);
    expect(decidedButNotMoved(step({ decision_status: "rejected" }))).toBe("rejected");
  });
});

describe("steps that are not gates at all", () => {
  it("no decision, nothing to say", () => {
    expect(needsYou(step({ decision_id: null }))).toBe(false);
    expect(decidedButNotMoved(step({ decision_id: null }))).toBe("");
  });

  it("a step that has moved on is done with its gate", () => {
    expect(needsYou(step({ status: "succeeded", decision_status: "approved" }))).toBe(false);
    expect(decidedButNotMoved(step({ status: "succeeded", decision_status: "approved" })))
      .toBe("");
  });

  it("an open gate is not also 'decided'", () => {
    expect(decidedButNotMoved(step({ decision_status: "pending" }))).toBe("");
  });
});

describe("what the operator is told", () => {
  const UI = codeOnly("app/delivery-workflows-ui.tsx");

  it("approved says it is starting, not that it is waiting", () => {
    expect(UI).toContain("Approved — starting.");
    expect(UI).toContain("as soon as a worker picks it up");
  });

  it("expired says nothing was applied, and offers to reopen", () => {
    // The hold default means expiry applied nothing, so the question is still answerable —
    // opening the decision reopens it.
    expect(UI).toContain("The gate expired before anyone answered it.");
    expect(UI).toContain("Answer it now →");
  });
});
