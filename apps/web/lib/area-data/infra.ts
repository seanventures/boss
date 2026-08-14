/**
 * BOSS-13 area loader: System Configuration / ProviderOps / infra surfaces.
 *
 * Covers ?area=infra panels: ProviderOps feed, infrastructure resources, MCP.
 */

import {
  fetchInfrastructureResourceFeed,
  type InfrastructureResourceFeed,
} from "../infrastructure";
import { fetchMcpServers, type McpFeed } from "../mcp";
import {
  emptyProviderOpsFeed,
  fetchProviderOpsFeed,
  type ProviderOpsFeed,
} from "../providerops";
import { isAreaActive, type AreaLoadOptions } from "./types";

export const INFRA_AREA_IDS = ["infra"] as const;

export type InfraAreaData = {
  providerOps: ProviderOpsFeed;
  infrastructureResources: InfrastructureResourceFeed;
  mcpServers: McpFeed;
  loaded: boolean;
};

function emptyInfraResources(projectId: string): InfrastructureResourceFeed {
  return {
    projectId,
    apiReachable: false,
    blockedReason: "Area data not loaded for inactive console area",
    items: [],
  };
}

export function shouldLoadInfraArea(requestedArea: string | undefined): boolean {
  return isAreaActive(requestedArea, INFRA_AREA_IDS);
}

export async function loadInfraAreaData(
  options: AreaLoadOptions,
): Promise<InfraAreaData> {
  const { projectId, active = true } = options;
  if (!active) {
    return {
      providerOps: emptyProviderOpsFeed(
        "Area data not loaded for inactive console area",
        projectId,
      ),
      infrastructureResources: emptyInfraResources(projectId),
      mcpServers: { apiReachable: false, items: [] },
      loaded: false,
    };
  }

  const [providerOps, infrastructureResources, mcpServers] = await Promise.all([
    fetchProviderOpsFeed(projectId),
    fetchInfrastructureResourceFeed(projectId),
    fetchMcpServers(projectId),
  ]);

  return {
    providerOps,
    infrastructureResources,
    mcpServers,
    loaded: true,
  };
}
