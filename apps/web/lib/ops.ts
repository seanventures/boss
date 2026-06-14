import { BossApiError, type OpsQueueInspectionResponse } from "@boss/api-client";

import { client, PROJECT_ID, resolveProjectId } from "./client";

export type OpsQueueFeed = {
  projectId: string;
  apiReachable: boolean;
  blockedReason?: string;
  generatedAt?: string;
  budgetState: string;
  queueState: Record<string, unknown>;
  jobStatusCounts: Record<string, number>;
  jobKindCounts: Record<string, number>;
  staleLeaseJobIds: string[];
  readyJobIds: string[];
  blockerCount: number;
  recommendedActions: string[];
  jobs: OpsQueueInspectionResponse["jobs"];
  deadletters: OpsQueueInspectionResponse["deadletters"];
  pendingDecisions: OpsQueueInspectionResponse["pending_decisions"];
};

function mapOpsQueue(payload: OpsQueueInspectionResponse): OpsQueueFeed {
  return {
    projectId: payload.project_id,
    apiReachable: true,
    generatedAt: payload.generated_at,
    budgetState: payload.budget_state,
    queueState: payload.queue_state,
    jobStatusCounts: payload.job_status_counts,
    jobKindCounts: payload.job_kind_counts,
    staleLeaseJobIds: payload.stale_lease_job_ids,
    readyJobIds: payload.ready_job_ids,
    blockerCount: payload.blockers.length,
    recommendedActions: payload.recommended_actions,
    jobs: payload.jobs,
    deadletters: payload.deadletters,
    pendingDecisions: payload.pending_decisions,
  };
}

export function emptyOpsQueueFeed(
  reason?: string,
  projectId = PROJECT_ID,
): OpsQueueFeed {
  return {
    projectId: resolveProjectId(projectId),
    apiReachable: false,
    blockedReason: reason,
    budgetState: "unknown",
    queueState: {},
    jobStatusCounts: {},
    jobKindCounts: {},
    staleLeaseJobIds: [],
    readyJobIds: [],
    blockerCount: 0,
    recommendedActions: [],
    jobs: [],
    deadletters: [],
    pendingDecisions: [],
  };
}

export async function fetchOpsQueueFeed(
  projectId = PROJECT_ID,
  limit = 8,
): Promise<OpsQueueFeed> {
  try {
    return mapOpsQueue(
      await client.inspectOpsQueue(resolveProjectId(projectId), 100, true),
    );
  } catch (error) {
    if (error instanceof BossApiError) {
      return emptyOpsQueueFeed(`API returned ${error.status}`, projectId);
    }
    return emptyOpsQueueFeed(
      error instanceof Error ? error.message : "Unknown API error",
      projectId,
    );
  }
}
