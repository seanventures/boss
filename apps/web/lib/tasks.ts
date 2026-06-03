import { BossApiError, type TaskSummary } from "@boss/api-client";

import { client, PROJECT_ID } from "./client";

export type TaskFeedItem = {
  id: string;
  title: string;
  status: string;
  priority: string;
  riskTier: number;
  updatedAt: string;
};

export type TaskFeed = {
  projectId: string;
  apiReachable: boolean;
  blockedReason?: string;
  items: TaskFeedItem[];
};

function mapTask(payload: TaskSummary): TaskFeedItem {
  return {
    id: payload.id,
    title: payload.title,
    status: payload.status,
    priority: payload.priority,
    riskTier: payload.risk_tier,
    updatedAt: payload.updated_at,
  };
}

export function emptyTaskFeed(reason?: string): TaskFeed {
  return {
    projectId: PROJECT_ID,
    apiReachable: false,
    blockedReason: reason,
    items: [],
  };
}

export async function fetchTaskFeed(limit = 8): Promise<TaskFeed> {
  try {
    const payload = await client.listTasks(PROJECT_ID);
    return {
      projectId: PROJECT_ID,
      apiReachable: true,
      items: payload.items.slice(0, limit).map(mapTask),
    };
  } catch (error) {
    if (error instanceof BossApiError) {
      return emptyTaskFeed(`API returned ${error.status}`);
    }
    return emptyTaskFeed(
      error instanceof Error ? error.message : "Unknown API error",
    );
  }
}
