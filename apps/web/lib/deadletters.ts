import {
  BossApiError,
  type DeadletterItem,
  type DeadletterRemediationItem,
} from "@boss/api-client";

import { client, PROJECT_ID, resolveProjectId } from "./client";

export type DeadletterRemediationFeedItem = {
  id: string;
  action: string;
  status: string;
  actor: string;
  reason?: string;
  requeuedJobId?: string;
  decisionId?: string;
  error?: string;
  createdAt: string;
};

export type DeadletterFeedItem = {
  id: string;
  sourceKind: string;
  sourceId: string;
  relatedEntityType?: string;
  relatedEntityId?: string;
  relatedStatus?: string;
  reason: string;
  status: string;
  incidentTaskId?: string;
  incidentArtifactId?: string;
  remediationDecisionId?: string;
  requeuedJobId?: string;
  resolvedBy?: string;
  resolvedAt?: string;
  createdAt: string;
  remediations: DeadletterRemediationFeedItem[];
  latestRemediation?: DeadletterRemediationFeedItem;
};

export type DeadletterFeed = {
  projectId: string;
  apiReachable: boolean;
  blockedReason?: string;
  items: DeadletterFeedItem[];
};

function mapRemediation(payload: DeadletterRemediationItem): DeadletterRemediationFeedItem {
  return {
    id: payload.id,
    action: payload.action,
    status: payload.status,
    actor: payload.actor,
    reason: payload.reason || undefined,
    requeuedJobId: payload.requeued_job_id || undefined,
    decisionId: payload.decision_id || undefined,
    error: payload.error || undefined,
    createdAt: payload.created_at,
  };
}

function mapDeadletter(
  payload: DeadletterItem,
  remediations: DeadletterRemediationFeedItem[] = [],
): DeadletterFeedItem {
  return {
    id: payload.id,
    sourceKind: payload.source_kind,
    sourceId: payload.source_id,
    relatedEntityType: payload.related_entity_type,
    relatedEntityId: payload.related_entity_id,
    relatedStatus: payload.related_status,
    reason: payload.reason,
    status: payload.status,
    incidentTaskId: payload.incident_task_id,
    incidentArtifactId: payload.incident_artifact_id,
    remediationDecisionId: payload.remediation_decision_id,
    requeuedJobId: payload.requeued_job_id,
    resolvedBy: payload.resolved_by,
    resolvedAt: payload.resolved_at,
    createdAt: payload.created_at,
    remediations,
    latestRemediation: remediations[0],
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
  const resolved = resolveProjectId(projectId);
  try {
    const payload = await client.listDeadletters(resolved, limit);
    let byDeadletter = new Map<string, DeadletterRemediationFeedItem[]>();
    try {
      const remediations = await client.listDeadletterRemediations(resolved, {
        limit: 200,
      });
      for (const item of remediations.items) {
        const mapped = mapRemediation(item);
        const bucket = byDeadletter.get(item.deadletter_id) ?? [];
        bucket.push(mapped);
        byDeadletter.set(item.deadletter_id, bucket);
      }
    } catch (error) {
      // Older live tags 404 the remediations router. List still works.
      if (!(error instanceof BossApiError) || error.status !== 404) {
        throw error;
      }
      byDeadletter = new Map();
    }
    return {
      projectId: payload.project_id,
      apiReachable: true,
      items: payload.items.map((item) =>
        mapDeadletter(item, byDeadletter.get(item.id) ?? []),
      ),
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
