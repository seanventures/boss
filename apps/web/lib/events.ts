import { BossApiError, type EventItem } from "@boss/api-client";

import { client, PROJECT_ID, resolveProjectId } from "./client";

export type ActivityFeedItem = {
  id: string;
  eventType: string;
  actor: string;
  entityType: string;
  entityId: string;
  createdAt: string;
};

export type ActivityFeed = {
  projectId: string;
  apiReachable: boolean;
  blockedReason?: string;
  items: ActivityFeedItem[];
};

function mapEvent(payload: EventItem): ActivityFeedItem {
  return {
    id: payload.id,
    eventType: payload.event_type,
    actor: payload.actor,
    entityType: payload.entity_type,
    entityId: payload.entity_id,
    createdAt: payload.created_at,
  };
}

export function emptyActivityFeed(reason?: string, projectId = PROJECT_ID): ActivityFeed {
  return {
    projectId: resolveProjectId(projectId),
    apiReachable: false,
    blockedReason: reason,
    items: [],
  };
}

export async function fetchActivityFeed(
  projectId = PROJECT_ID,
  limit = 8,
): Promise<ActivityFeed> {
  try {
    const payload = await client.listEvents(resolveProjectId(projectId), limit);
    return {
      projectId: payload.project_id,
      apiReachable: true,
      items: payload.items.map(mapEvent),
    };
  } catch (error) {
    if (error instanceof BossApiError) {
      return emptyActivityFeed(`API returned ${error.status}`, projectId);
    }
    return emptyActivityFeed(
      error instanceof Error ? error.message : "Unknown API error",
      projectId,
    );
  }
}
