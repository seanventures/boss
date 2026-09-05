/** The workflow page answers two questions, and opened on neither.
 *
 *  "Start a project" occupied the whole top — a composer above the fold, pushing what is
 *  actually running below it — and every run below was collapsed. So an operator arriving to
 *  find out whether anything needed them had to scroll past a form and then click runs open one
 *  at a time to look.
 *
 *  The composer is now one line until it is wanted, and a run opens on arrival. Which run is
 *  the interesting part: the newest is the obvious choice and the wrong one. A run WAITING ON A
 *  DECISION is the only thing on the page that will not move until a person acts.
 */
import { describe, it, expect } from "vitest";
import { runToOpen } from "../app/delivery-workflows-ui";

type Run = Parameters<typeof runToOpen>[0][number];

function run(over: Partial<Run>): Run {
  return {
    id: "wfr_x", status: "completed", workflow_key: "standard_product_delivery",
    workflow_name: "Standard Product Delivery", steps: [], input: {},
    created_at: "2026-09-01T00:00:00Z", updated_at: "2026-09-01T00:00:00Z",
    ...over,
  } as Run;
}

/** A step sitting on a decision — the shape `needsYou` recognises. */
function gate() {
  return { step_id: "spec_review", kind: "capability", status: "waiting",
           decision_id: "dec_1", output: {}, evidence: [], error: null,
           approval: {}, attempt: 1, started_at: null, completed_at: null };
}

describe("which run the page opens", () => {
  it("opens nothing when there are no runs", () => {
    expect(runToOpen([])).toBeNull();
  });

  it("prefers a run waiting on a decision over a newer running one", () => {
    // The newest is the obvious choice and the wrong one: a build in progress needs nobody.
    const runs = [
      run({ id: "wfr_newer", status: "running" }),
      run({ id: "wfr_gated", status: "waiting", steps: [gate()] as never }),
    ];
    expect(runToOpen(runs)).toBe("wfr_gated");
  });

  it("falls back to a live run when nothing is gated", () => {
    const runs = [
      run({ id: "wfr_done", status: "completed" }),
      run({ id: "wfr_live", status: "running" }),
    ];
    expect(runToOpen(runs)).toBe("wfr_live");
  });

  it("falls back to the first run when nothing is live", () => {
    const runs = [run({ id: "wfr_a" }), run({ id: "wfr_b" })];
    expect(runToOpen(runs)).toBe("wfr_a");
  });

  it("does not treat a finished gate as needing anybody", () => {
    // `needsYou` is decision_id AND waiting; an approved gate has a decision and has moved on.
    const approved = { ...gate(), status: "succeeded" };
    const runs = [
      run({ id: "wfr_approved", status: "completed", steps: [approved] as never }),
      run({ id: "wfr_live", status: "running" }),
    ];
    expect(runToOpen(runs)).toBe("wfr_live");
  });
});
