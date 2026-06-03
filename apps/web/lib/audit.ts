import { BossApiError } from "@boss/api-client";

import { client, PROJECT_ID, resolveProjectId } from "./client";

export type AuditSummary = {
  projectId: string;
  apiReachable: boolean;
  blockedReason?: string;
  generatedAt?: string;
  counts: {
    tasks: number;
    jobs: number;
    decisions: number;
    artifacts: number;
    receipts: number;
    budgetReservations: number;
    routingLogs: number;
    events: number;
  };
};

export function emptyAuditSummary(reason?: string, projectId = PROJECT_ID): AuditSummary {
  return {
    projectId: resolveProjectId(projectId),
    apiReachable: false,
    blockedReason: reason,
    counts: {
      tasks: 0,
      jobs: 0,
      decisions: 0,
      artifacts: 0,
      receipts: 0,
      budgetReservations: 0,
      routingLogs: 0,
      events: 0,
    },
  };
}

export async function fetchAuditSummary(projectId = PROJECT_ID): Promise<AuditSummary> {
  try {
    const payload = await client.exportAuditTrail(resolveProjectId(projectId));
    return {
      projectId: payload.project_id,
      apiReachable: true,
      generatedAt: payload.generated_at,
      counts: {
        tasks: payload.counts.tasks,
        jobs: payload.counts.jobs,
        decisions: payload.counts.decisions,
        artifacts: payload.counts.artifacts,
        receipts: payload.counts.receipts,
        budgetReservations: payload.counts.budget_reservations,
        routingLogs: payload.counts.routing_logs,
        events: payload.counts.events,
      },
    };
  } catch (error) {
    if (error instanceof BossApiError) {
      return emptyAuditSummary(`API returned ${error.status}`, projectId);
    }
    return emptyAuditSummary(
      error instanceof Error ? error.message : "Unknown API error",
      projectId,
    );
  }
}
