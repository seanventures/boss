import { BossApiError, type JobListItem } from "@boss/api-client";

import { client, PROJECT_ID, resolveProjectId } from "./client";

export type LiveJobsFeed = {
  projectId: string;
  apiReachable: boolean;
  blockedReason?: string;
  items: JobListItem[];
};

export function emptyLiveJobsFeed(
  reason?: string,
  projectId = PROJECT_ID,
): LiveJobsFeed {
  return {
    projectId: resolveProjectId(projectId),
    apiReachable: false,
    blockedReason: reason,
    items: [],
  };
}

export async function fetchLiveJobsFeed(
  projectId = PROJECT_ID,
  limit = 12,
): Promise<LiveJobsFeed> {
  const pid = resolveProjectId(projectId);
  try {
    const payload = await client.listJobs(pid, {
      include_terminal: false,
      sort: "updated",
      limit,
    });
    return {
      projectId: payload.project_id ?? pid,
      apiReachable: true,
      items: payload.items ?? [],
    };
  } catch (error) {
    if (error instanceof BossApiError) {
      return emptyLiveJobsFeed(`API returned ${error.status}`, projectId);
    }
    return emptyLiveJobsFeed(
      error instanceof Error ? error.message : "Unknown API error",
      projectId,
    );
  }
}
