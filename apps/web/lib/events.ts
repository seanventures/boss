import { BossApiError, type EventItem } from "@boss/api-client";

import { client, PROJECT_ID } from "./client";

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

export function emptyActivityFeed(reason?: string): ActivityFeed {
  return {
    projectId: PROJECT_ID,
    apiReachable: false,
    blockedReason: reason,
    items: [],
  };
}

export async function fetchActivityFeed(limit = 8): Promise<ActivityFeed> {
  try {
    const payload = await client.listEvents(PROJECT_ID, limit);
    return {
      projectId: payload.project_id,
      apiReachable: true,
      items: payload.items.map(mapEvent),
    };
  } catch (error) {
    if (error instanceof BossApiError) {
      return emptyActivityFeed(`API returned ${error.status}`);
    }
    return emptyActivityFeed(
      error instanceof Error ? error.message : "Unknown API error",
    );
  }
}
