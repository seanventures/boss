import type { ConversationContext } from "./agent-world-types";
import type { DockOpening } from "./dock-subject";

export function conversationInvestigation(context: ConversationContext): DockOpening | null {
  if (!context.workflowRunId) return null;
  return {
    subject: context.stepId
      ? { kind: "workflow_step", id: context.workflowRunId, step: context.stepId }
      : { kind: "workflow_run", id: context.workflowRunId },
    role: null,
    roleKey: null,
    fresh: true,
    actId: context.actId ?? null,
    question: "Investigate this run using its current status, latest attempts and logs. " +
      "Check whether a newer attempt already recovered before retrying. " +
      "If it is still failing, perform only permitted recovery actions; propose an action " +
      "for approval when required. If an installation or network policy change is needed, " +
      "create a human action item with the evidence, exact required change and verification " +
      "step. Do not weaken sandbox, TLS or deployment controls. Report what actually ran " +
      "and verify the outcome; do not claim an unexecuted repair succeeded.",
  };
}
