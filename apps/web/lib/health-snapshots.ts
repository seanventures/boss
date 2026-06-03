import {
  BossApiError,
  type HealthSnapshotSummary,
} from "@boss/api-client";

import { client, PROJECT_ID } from "./client";

export type HealthSnapshotFeedItem = {
  id: string;
  generatedBy: string;
  budgetState: string;
  queueState: Record<string, unknown>;
  workloadState: Record<string, unknown>;
  incidentState: Record<string, unknown>;
  topRisks: Array<Record<string, unknown>>;
  summary: Record<string, unknown>;
  createdAt: string;
};

export type HealthSnapshotFeed = {
  projectId: string;
  apiReachable: boolean;
  blockedReason?: string;
  items: HealthSnapshotFeedItem[];
};

function mapSnapshot(payload: HealthSnapshotSummary): HealthSnapshotFeedItem {
  return {
    id: payload.id,
    generatedBy: payload.generated_by,
    budgetState: payload.budget_state,
    queueState: payload.queue_state,
    workloadState: payload.workload_state,
    incidentState: payload.incident_state,
    topRisks: payload.top_risks,
    summary: payload.summary,
    createdAt: payload.created_at,
  };
}

export function emptyHealthSnapshotFeed(reason?: string): HealthSnapshotFeed {
  return {
    projectId: PROJECT_ID,
    apiReachable: false,
    blockedReason: reason,
    items: [],
  };
}

export async function fetchHealthSnapshotFeed(): Promise<HealthSnapshotFeed> {
  try {
    const payload = await client.listHealthSnapshots(PROJECT_ID);
    return {
      projectId: payload.project_id,
      apiReachable: true,
      items: payload.items.map(mapSnapshot),
    };
  } catch (error) {
    if (error instanceof BossApiError) {
      return emptyHealthSnapshotFeed(`API returned ${error.status}`);
    }
    return emptyHealthSnapshotFeed(
      error instanceof Error ? error.message : "Unknown API error",
    );
  }
}
