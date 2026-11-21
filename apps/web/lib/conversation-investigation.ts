/**
 * "INVESTIGATE WITH BOSS" FROM AN ADVISORY CHAT (contributed from an enterprise install,
 * 2026-11-20).
 *
 * A step failed; its "Ask <agent>" opened the 1:1 talk, which only advises (no tools, by design:
 * agent_converse.TALK_CANNOT_ACT). Asked to investigate and repair, it said "use Talk to BOSS"
 * and the sheet had no door there. Now a talk about a run or a step hands its subject to the
 * dock, where BOSS can read the logs and act with the operator's consent.
 *
 * Only for a talk scoped to a run or step: never an invented subject. The question is the
 * person's own (their last message in the talk), else a short one in their words — never an
 * instruction block. The rules for acting (newer attempts, consent, no claimed repairs, policy
 * changes are a person's) live in the server's answer contract (turn_subject.answer_contract),
 * so every Ask door gets them, not only this one.
 */
import type { ConversationContext, ConversationTurn } from "./agent-world-types";
import type { DockOpening } from "./dock-subject";

const ASK_STEP = "Why did this step fail, and what can be done?";
const ASK_RUN = "What went wrong in this run, and what can be done?";
/** A question the person typed reads as theirs up to this length; longer is cut at a word. */
const MAX_QUESTION = 300;

function theirWords(turns: ConversationTurn[] | undefined): string {
  const last = [...(turns ?? [])].reverse().find((t) => t.speakerKind === "person" && t.content.trim());
  const said = (last?.content ?? "").replace(/\s+/g, " ").trim();
  if (said.length <= MAX_QUESTION) return said;
  const cut = said.slice(0, MAX_QUESTION);
  return `${cut.slice(0, Math.max(cut.lastIndexOf(" "), 1))}…`;
}

export function conversationInvestigation(context: ConversationContext,
                                          turns?: ConversationTurn[]): DockOpening | null {
  if (!context.workflowRunId) return null;
  return {
    subject: context.stepId
      ? { kind: "workflow_step", id: context.workflowRunId, step: context.stepId }
      : { kind: "workflow_run", id: context.workflowRunId },
    // Ask BOSS: the agent that can act. The advisory chat's agent cannot.
    role: null,
    roleKey: null,
    fresh: true,
    actId: context.actId ?? null,
    question: theirWords(turns) || (context.stepId ? ASK_STEP : ASK_RUN),
  };
}
