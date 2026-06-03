import { BossApiError, type RoutingLogItem } from "@boss/api-client";

import { client, PROJECT_ID } from "./client";

export type RoutingLogFeedItem = {
  id: string;
  skillId?: string;
  taskKind: string;
  workloadClass: string;
  selectedSourceId?: string;
  selectedModelId?: string;
  denialReason?: string;
  createdAt: string;
};

export type RoutingLogFeed = {
  projectId: string;
  apiReachable: boolean;
  blockedReason?: string;
  items: RoutingLogFeedItem[];
};

function mapLog(payload: RoutingLogItem): RoutingLogFeedItem {
  return {
    id: payload.id,
    skillId: payload.skill_id,
    taskKind: payload.task_kind,
    workloadClass: payload.workload_class,
    selectedSourceId: payload.selected_source_id,
    selectedModelId: payload.selected_model_id,
    denialReason: payload.denial_reason,
    createdAt: payload.created_at,
  };
}

export function emptyRoutingLogFeed(reason?: string): RoutingLogFeed {
  return {
    projectId: PROJECT_ID,
    apiReachable: false,
    blockedReason: reason,
    items: [],
  };
}

export async function fetchRoutingLogFeed(limit = 8): Promise<RoutingLogFeed> {
  try {
    const payload = await client.listRoutingLogs(PROJECT_ID, limit);
    return {
      projectId: payload.project_id,
      apiReachable: true,
      items: payload.items.map(mapLog),
    };
  } catch (error) {
    if (error instanceof BossApiError) {
      return emptyRoutingLogFeed(`API returned ${error.status}`);
    }
    return emptyRoutingLogFeed(
      error instanceof Error ? error.message : "Unknown API error",
    );
  }
}
