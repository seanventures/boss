/**
 * "Investigate with BOSS" from an advisory talk (contributed from an enterprise install): only a
 * talk about a run or step has it, and it asks the person's own question — never an instruction
 * block (dock-subject.ts: facts and instructions do not go in the question; the server's answer
 * contract carries the rules for acting).
 */
import { describe, expect, it } from "vitest";
import type { ConversationTurn } from "../lib/agent-world-types";
import { conversationInvestigation } from "../lib/conversation-investigation";
import { dockParams } from "../lib/dock-subject";

const turn = (speakerKind: ConversationTurn["speakerKind"], content: string): ConversationTurn => ({
  turnId: `t_${content.length}_${speakerKind}`, seq: 1, speakerKind, roleKey: null, addressedTo: [], kind: "message",
  content, status: "complete", citedActs: [], tokensIn: 0, tokensOut: 0, costUsd: null, createdAt: "",
});

describe("advisory conversation investigation handoff", () => {
  it("does not invent a run for an unscoped chat", () => {
    expect(conversationInvestigation({ from: "chip" })).toBeNull();
    expect(conversationInvestigation({ from: "roster" }, [turn("person", "Why did it fail?")])).toBeNull();
  });

  it("keeps the exact step, opens the agent that can act, and asks a short question", () => {
    const opening = conversationInvestigation({ from: "run_step", workflowRunId: "wfr_owned",
      stepId: "preview", actId: "act_owned" });
    expect(opening).not.toBeNull();
    expect(dockParams(opening!)).toMatchObject({ agent: "workflow_step:wfr_owned:preview",
      dock_role: null, dock_role_key: null, dock_mode: null, dock_fresh: "1", dock_act: "act_owned" });
    expect(opening!.question).toBe("Why did this step fail, and what can be done?");
  });

  it("asks in the person's own words: their last message in the talk", () => {
    const turns = [turn("person", "first"), turn("agent", "I can't do that here."),
                   turn("person", "  Can you   fix the preview?  ")];
    expect(conversationInvestigation({ from: "run_step", workflowRunId: "wfr_owned", stepId: "preview" }, turns)!.question)
      .toBe("Can you fix the preview?");
    const long = "why ".repeat(200);
    const q = conversationInvestigation({ from: "run_step", workflowRunId: "wfr_owned" }, [turn("person", long)])!.question;
    expect(q.length).toBeLessThanOrEqual(301);
    expect(q.endsWith("…")).toBe(true);
  });

  it("never carries instructions: the rules for acting are the server's", () => {
    const q = conversationInvestigation({ from: "run_step", workflowRunId: "wfr_owned" })!.question;
    expect(q).toBe("What went wrong in this run, and what can be done?");
    expect(q).not.toMatch(/do not|never|approval|sandbox|TLS|claim/i);
  });

  it("keeps whole-run context when no step was selected", () => {
    expect(conversationInvestigation({ from: "run_step", workflowRunId: "wfr_owned" })?.subject)
      .toEqual({ kind: "workflow_run", id: "wfr_owned" });
  });
});
