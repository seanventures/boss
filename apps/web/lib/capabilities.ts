import { BossApiError, type CapabilitiesResponse } from "@boss/api-client";

import { client, PROJECT_ID, resolveProjectId } from "./client";

export type { NativeTool, McpServerWithTools, McpServerTool, SkillSummary } from "@boss/api-client";

export type CapabilitiesFeed = {
  apiReachable: boolean;
  blockedReason?: string;
  engineLabel?: string;
  engineModel?: string;
  nativeTools: CapabilitiesResponse["native_tools"];
  skills: CapabilitiesResponse["skills"];
  mcpServers: CapabilitiesResponse["mcp_servers"];
};

export async function fetchCapabilities(
  projectId = PROJECT_ID,
): Promise<CapabilitiesFeed> {
  try {
    const data = await client.getCapabilities(resolveProjectId(projectId));
    return {
      apiReachable: true,
      engineLabel: data.engine_label,
      engineModel: data.engine_model,
      nativeTools: data.native_tools,
      skills: data.skills ?? [],
      mcpServers: data.mcp_servers,
    };
  } catch (error) {
    const reason =
      error instanceof BossApiError
        ? `API returned ${error.status}`
        : error instanceof Error
          ? error.message
          : "Unknown API error";
    return { apiReachable: false, blockedReason: reason, nativeTools: [], skills: [], mcpServers: [] };
  }
}
