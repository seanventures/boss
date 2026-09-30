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

export async function fetchApprovalRules(projectId = PROJECT_ID, active = true): Promise<ApprovalRulesFeed> {
  // Not rendered this request. Same shape the unreachable path returns, no round trip.
  if (!active) return { apiReachable: false, blockedReason: "not requested" };
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
