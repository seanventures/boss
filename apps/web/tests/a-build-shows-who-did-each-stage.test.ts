/**
 * A build's stage ribbon shows the agent that did each stage, from its signed work — so the
 * engineer the operator assigned is the face on Implement, not whoever a stage→role table names
 * (spec §3.7; risk 7: telemetry misattribution).
 */
import { describe, expect, it } from "vitest";

import { shipItChain, wireAct } from "./_helpers/run-acts-fixtures";

const { toAct, toChain } = await import("../app/agents/act-wire");
const { stageActsByPhase, stageOf } = await import("../app/agents/build-faces");

/** The ribbon's phases and the stages each covers, as the code-task screen groups them. */
const PHASE_STAGES: Record<string, string[]> = {
  design: ["standards", "spec", "plan", "review_plan", "design"],
  write_tests: ["write_tests"],
  implement: ["implement", "solo", "repair", "guest"],
  run_tests: ["check_tests", "run_all_tests"],
  review_panel: ["review_code", "security_check", "consistency_check"],
};

describe("a build's faces come from its work", () => {
  it("credits the assigned engineer on Implement and the reviewer on Review", () => {
    const stages = toChain(shipItChain()).acts.filter((a) => a.kind === "stage");
    const by = stageActsByPhase(stages, PHASE_STAGES);
    expect(by.implement.roleKey).toBe("backend_engineer");   // STAGE_ROLES would say software_engineer
    expect(by.implement.actId).toBe("act_s5");               // the latest round, not the first
    expect(by.review_panel.roleKey).toBe("adversarial_reviewer");
    expect(by.design).toBeUndefined();                       // no work, no face
  });

  it("reads the stage from its link, else from a stage named in its label", () => {
    expect(stageOf(toAct(wireAct({ kind: "stage", stage: "write_tests", label: "Build · whatever" })), ["write_tests"]))
      .toBe("write_tests");
    expect(stageOf(toAct(wireAct({ kind: "stage", label: "Build · security_check · Ship it" })), ["security_check"]))
      .toBe("security_check");
    expect(stageOf(toAct(wireAct({ kind: "stage", label: "Build · Ship it" })), ["implement"])).toBeNull();
  });
});
