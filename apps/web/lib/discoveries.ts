import { BossApiError, type DiscoveryRun } from "@boss/api-client";

import { client, PROJECT_ID, resolveProjectId } from "./client";

export type DiscoveryFeed = {
  projectId: string;
  apiReachable: boolean;
  blockedReason?: string;
  items: DiscoveryRun[];
};

export function emptyDiscoveryFeed(reason?: string, projectId = PROJECT_ID): DiscoveryFeed {
  return {
    projectId: resolveProjectId(projectId),
    apiReachable: false,
    blockedReason: reason,
    items: [],
  };
}

export async function fetchDiscoveriesFeed(projectId = PROJECT_ID): Promise<DiscoveryFeed> {
  const resolved = resolveProjectId(projectId);
  try {
    const res = await client.getDiscoveries(resolved);
    return { projectId: resolved, apiReachable: true, items: res.discoveries ?? [] };
  } catch (error) {
    if (error instanceof BossApiError) {
      return emptyDiscoveryFeed(error.message, resolved);
    }
    return emptyDiscoveryFeed("API unreachable", resolved);
  }
}
