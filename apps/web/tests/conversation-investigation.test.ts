import { describe, expect, it } from "vitest";
import { conversationInvestigation } from "../lib/conversation-investigation";
import { dockParams } from "../lib/dock-subject";

describe("advisory conversation investigation handoff", () => {
  it("does not invent a run for an unscoped chat", () => {
    expect(conversationInvestigation({ from: "chip" })).toBeNull();
  });

  it("preserves the exact step and opens the tool-enabled agent without stale role or mode", () => {
    const opening = conversationInvestigation({ from: "run_step", workflowRunId: "wfr_owned",
      stepId: "preview", actId: "act_owned" });
    expect(opening).not.toBeNull();
    expect(dockParams(opening!)).toMatchObject({ agent: "workflow_step:wfr_owned:preview",
      dock_role: null, dock_role_key: null, dock_mode: null, dock_fresh: "1", dock_act: "act_owned" });
    expect(opening!.question).toContain("newer attempt already recovered");
    expect(opening!.question).toContain("for approval when required");
    expect(opening!.question).toContain("create a human action item");
  });

  it("keeps whole-run context when no step was selected", () => {
    expect(conversationInvestigation({ from: "run_step", workflowRunId: "wfr_owned" })?.subject)
      .toEqual({ kind: "workflow_run", id: "wfr_owned" });
  });
});
