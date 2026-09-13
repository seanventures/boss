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

export async function fetchAuditSummary(
  projectId = PROJECT_ID,
  active = true,
): Promise<AuditSummary> {
  // Not rendered this request. Same shape the unreachable path returns, no round trip.
  if (!active) return emptyAuditSummary("not requested", projectId);
  try {
    // Counts, not the export. This panel shows eight integers; asking for the export got them
    // by serialising every row in the project — 192 MB in 62 seconds, measured — and that was
    // the entire reason the Ops area took ~25 seconds to render.
    const payload = await client.summarizeAuditTrail(resolveProjectId(projectId));
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
