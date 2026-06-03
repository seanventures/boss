import type { CommandProposeRequest } from "@boss/api-client";

import { PROJECT_ID } from "./client";

export function buildCommandRequest(
  commandInput: FormDataEntryValue | null,
  deadlineInput: FormDataEntryValue | null,
): CommandProposeRequest {
  const command =
    typeof commandInput === "string" ? commandInput.trim() : "";
  if (!command) {
    throw new Error("command is required");
  }

  const constraints: Record<string, unknown> = {};
  if (typeof deadlineInput === "string" && deadlineInput.trim()) {
    constraints.deadline = deadlineInput.trim();
  }

  return {
    project_id: PROJECT_ID,
    command,
    constraints,
  };
}
