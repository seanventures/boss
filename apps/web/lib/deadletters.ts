import { BossApiError, type DeadletterItem } from "@boss/api-client";

import { client, PROJECT_ID, resolveProjectId } from "./client";

export type DeadletterFeedItem = {
  id: string;
  sourceKind: string;
  sourceId: string;
  reason: string;
  status: string;
  incidentTaskId?: string;
  incidentArtifactId?: string;
  remediationDecisionId?: string;
  requeuedJobId?: string;
  resolvedBy?: string;
  resolvedAt?: string;
  createdAt: string;
};

export type DeadletterFeed = {
  projectId: string;
  apiReachable: boolean;
  blockedReason?: string;
  items: DeadletterFeedItem[];
};

function mapDeadletter(payload: DeadletterItem): DeadletterFeedItem {
  return {
    id: payload.id,
    sourceKind: payload.source_kind,
    sourceId: payload.source_id,
    reason: payload.reason,
    status: payload.status,
    incidentTaskId: payload.incident_task_id,
    incidentArtifactId: payload.incident_artifact_id,
    remediationDecisionId: payload.remediation_decision_id,
    requeuedJobId: payload.requeued_job_id,
    resolvedBy: payload.resolved_by,
    resolvedAt: payload.resolved_at,
    createdAt: payload.created_at,
  };
}

export function emptyDeadletterFeed(reason?: string, projectId = PROJECT_ID): DeadletterFeed {
  return {
    projectId: resolveProjectId(projectId),
    apiReachable: false,
    blockedReason: reason,
    items: [],
  };
}

export async function fetchDeadletterFeed(
  projectId = PROJECT_ID,
  limit = 25,
): Promise<DeadletterFeed> {
  try {
    const payload = await client.listDeadletters(resolveProjectId(projectId), limit);
    return {
      projectId: payload.project_id,
      apiReachable: true,
      items: payload.items.map(mapDeadletter),
    };
  } catch (error) {
    if (error instanceof BossApiError) {
      return emptyDeadletterFeed(`API returned ${error.status}`, projectId);
    }
    return emptyDeadletterFeed(
      error instanceof Error ? error.message : "Unknown API error",
      projectId,
    );
  }
}
