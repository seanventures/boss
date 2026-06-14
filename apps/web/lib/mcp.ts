import { BossApiError, type McpServer } from "@boss/api-client";

import { client, PROJECT_ID, resolveProjectId } from "./client";

export type { McpServer } from "@boss/api-client";

export type McpFeed = {
  apiReachable: boolean;
  blockedReason?: string;
  items: McpServer[];
};

export async function fetchMcpServers(projectId = PROJECT_ID): Promise<McpFeed> {
  try {
    const data = await client.getMcpServers(resolveProjectId(projectId));
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
