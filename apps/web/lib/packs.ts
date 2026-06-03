import { BossApiError, type PackSummary } from "@boss/api-client";

import { client, PROJECT_ID, resolveProjectId } from "./client";

export type PackFeedItem = {
  packId: string;
  version: string;
  description: string;
  status: string;
  installed: boolean;
  installedBy?: string;
  installedAt?: string;
  roles: string[];
};

export type PackFeed = {
  projectId: string;
  apiReachable: boolean;
  blockedReason?: string;
  items: PackFeedItem[];
};

function mapPack(payload: PackSummary): PackFeedItem {
  return {
    packId: payload.pack_id,
    version: payload.version,
    description: payload.description,
    status: payload.status,
    installed: payload.installed,
    installedBy: payload.installed_by,
    installedAt: payload.installed_at,
    roles: payload.roles,
  };
}

export function emptyPackFeed(reason?: string, projectId = PROJECT_ID): PackFeed {
  return {
    projectId: resolveProjectId(projectId),
    apiReachable: false,
    blockedReason: reason,
    items: [],
  };
}

export async function fetchPackFeed(projectId = PROJECT_ID): Promise<PackFeed> {
  try {
    const payload = await client.listPacks(resolveProjectId(projectId));
    return {
      projectId: payload.project_id,
      apiReachable: true,
      items: payload.items.map(mapPack),
    };
  } catch (error) {
    if (error instanceof BossApiError) {
      return emptyPackFeed(`API returned ${error.status}`, projectId);
    }
    return emptyPackFeed(
      error instanceof Error ? error.message : "Unknown API error",
      projectId,
    );
  }
}
