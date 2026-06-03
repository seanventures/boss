import { BossApiError, type RoleSummary } from "@boss/api-client";

import { client, PROJECT_ID } from "./client";

export type RoleFeedItem = {
  id: string;
  roleKey: string;
  displayName: string;
  autonomyLevel: number;
  createdAt: string;
};

export type RoleFeed = {
  projectId: string;
  apiReachable: boolean;
  blockedReason?: string;
  items: RoleFeedItem[];
};

function mapRole(payload: RoleSummary): RoleFeedItem {
  return {
    id: payload.id,
    roleKey: payload.role_key,
    displayName: payload.display_name,
    autonomyLevel: payload.autonomy_level,
    createdAt: payload.created_at,
  };
}

export function emptyRoleFeed(reason?: string): RoleFeed {
  return {
    projectId: PROJECT_ID,
    apiReachable: false,
    blockedReason: reason,
    items: [],
  };
}

export async function fetchRoleFeed(): Promise<RoleFeed> {
  try {
    const payload = await client.listRoles(PROJECT_ID);
    return {
      projectId: payload.project_id,
      apiReachable: true,
      items: payload.items.map(mapRole),
    };
  } catch (error) {
    if (error instanceof BossApiError) {
      return emptyRoleFeed(`API returned ${error.status}`);
    }
    return emptyRoleFeed(
      error instanceof Error ? error.message : "Unknown API error",
    );
  }
}
