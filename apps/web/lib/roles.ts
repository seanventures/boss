import { BossApiError, type RoleSummary } from "@boss/api-client";

import { client, PROJECT_ID, resolveProjectId } from "./client";

export type RoleFeedItem = {
  id: string;
  roleKey: string;
  displayName: string;
  autonomyLevel: number;
  sourceModuleId?: string;
  sourceModuleVersion?: string;
  trustScore: number;
  successfulRuns: number;
  policyViolationCount: number;
  duplicateEffectAttemptCount: number;
  scopeViolationCount: number;
  lastReviewedAt?: string;
  lastViolationAt?: string;
  createdAt: string;
};

export type RoleFeed = {
  projectId: string;
  apiReachable: boolean;
  blockedReason?: string;
  items: RoleFeedItem[];
};

function mapRole(payload: RoleSummary): RoleFeedItem {
  return {
    id: payload.id,
    roleKey: payload.role_key,
    displayName: payload.display_name,
    autonomyLevel: payload.autonomy_level,
    sourceModuleId: payload.source_module_id,
    sourceModuleVersion: payload.source_module_version,
    trustScore: payload.trust_score,
    successfulRuns: payload.successful_runs,
    policyViolationCount: payload.policy_violation_count,
    duplicateEffectAttemptCount: payload.duplicate_effect_attempt_count,
    scopeViolationCount: payload.scope_violation_count,
    lastReviewedAt: payload.last_reviewed_at,
    lastViolationAt: payload.last_violation_at,
    createdAt: payload.created_at,
  };
}

export function emptyRoleFeed(reason?: string, projectId = PROJECT_ID): RoleFeed {
  return {
    projectId: resolveProjectId(projectId),
    apiReachable: false,
    blockedReason: reason,
    items: [],
  };
}

export async function fetchRoleFeed(projectId = PROJECT_ID): Promise<RoleFeed> {
  try {
    const payload = await client.listRoles(resolveProjectId(projectId));
    return {
      projectId: payload.project_id,
      apiReachable: true,
      items: payload.items.map(mapRole),
    };
  } catch (error) {
    if (error instanceof BossApiError) {
      return emptyRoleFeed(`API returned ${error.status}`, projectId);
    }
    return emptyRoleFeed(
      error instanceof Error ? error.message : "Unknown API error",
      projectId,
    );
  }
}
