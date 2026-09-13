import { BossApiError, type TaskOutput } from "@boss/api-client";

import { client, PROJECT_ID, resolveProjectId } from "./client";

export type { TaskOutput } from "@boss/api-client";

export type TaskOutputsFeed = {
  apiReachable: boolean;
  blockedReason?: string;
  items: TaskOutput[];
};

export async function fetchTaskOutputs(
  projectId = PROJECT_ID,
  active = true,
): Promise<TaskOutputsFeed> {
  // Not rendered this request. Same shape the unreachable path returns, no round trip.
  if (!active) return { apiReachable: false, blockedReason: "not requested", items: [] };
  try {
    const data = await client.getTaskOutputs(resolveProjectId(projectId));
    return { apiReachable: true, items: data.items };
  } catch (error) {
    if (error instanceof BossApiError) {
      return { apiReachable: false, blockedReason: `API returned ${error.status}`, items: [] };
    }
    return {
      apiReachable: false,
      blockedReason: error instanceof Error ? error.message : "Unknown API error",
      items: [],
    };
  }
}
