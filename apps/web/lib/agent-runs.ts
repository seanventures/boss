import { BossApiError, type AgentRun } from "@boss/api-client";

import { client, PROJECT_ID, resolveProjectId } from "./client";

export type { AgentRun } from "@boss/api-client";

export type AgentRunsFeed = {
  apiReachable: boolean;
  blockedReason?: string;
  items: AgentRun[];
};

export async function fetchAgentRuns(
  projectId = PROJECT_ID,
): Promise<AgentRunsFeed> {
  try {
    const data = await client.getAgentRuns(resolveProjectId(projectId));
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
