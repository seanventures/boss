import { BossApiError, type ApprovalRulesResponse } from "@boss/api-client";

import { client, PROJECT_ID, resolveProjectId } from "./client";

export type {
  ApprovalRulesResponse,
  ApprovalToolRule,
  ApprovalServiceGate,
} from "@boss/api-client";

export type ApprovalRulesFeed = {
  apiReachable: boolean;
  blockedReason?: string;
  rules?: ApprovalRulesResponse;
};

export async function fetchApprovalRules(projectId = PROJECT_ID): Promise<ApprovalRulesFeed> {
  try {
    const rules = await client.getApprovalRules(resolveProjectId(projectId));
    return { apiReachable: true, rules };
  } catch (error) {
    const reason =
      error instanceof BossApiError
        ? `API returned ${error.status}`
        : error instanceof Error
          ? error.message
          : "Unknown API error";
    return { apiReachable: false, blockedReason: reason };
  }
}
