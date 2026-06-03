import { BossApiError, type DeadletterItem } from "@boss/api-client";

import { client, PROJECT_ID } from "./client";

export type DeadletterFeedItem = {
  id: string;
  sourceKind: string;
  sourceId: string;
  reason: string;
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
    createdAt: payload.created_at,
  };
}

export function emptyDeadletterFeed(reason?: string): DeadletterFeed {
  return {
    projectId: PROJECT_ID,
    apiReachable: false,
    blockedReason: reason,
    items: [],
  };
}

export async function fetchDeadletterFeed(limit = 8): Promise<DeadletterFeed> {
  try {
    const payload = await client.listDeadletters(PROJECT_ID, limit);
    return {
      projectId: payload.project_id,
      apiReachable: true,
      items: payload.items.map(mapDeadletter),
    };
  } catch (error) {
    if (error instanceof BossApiError) {
      return emptyDeadletterFeed(`API returned ${error.status}`);
    }
    return emptyDeadletterFeed(
      error instanceof Error ? error.message : "Unknown API error",
    );
  }
}
