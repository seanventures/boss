import { BossApiError, type InfraResourceSummary } from "@boss/api-client";

import { client, PROJECT_ID, resolveProjectId } from "./client";

export type InfrastructureResourceFeedItem = {
  id: string;
  provider: string;
  resourceType: string;
  resourceName: string;
  externalResourceId: string;
  region: string;
  size: string;
  status: string;
  ttlMinutes: number;
  expiresAt: string;
  terminatedAt?: string;
  terminationReason?: string;
  dataBoundary: string;
  providerRef?: string;
};

export type InfrastructureResourceFeed = {
  projectId: string;
  apiReachable: boolean;
  blockedReason?: string;
  items: InfrastructureResourceFeedItem[];
};

function mapResource(
  payload: InfraResourceSummary,
): InfrastructureResourceFeedItem {
  return {
    id: payload.id,
    provider: payload.provider,
    resourceType: payload.resource_type,
    resourceName: payload.resource_name,
    externalResourceId: payload.external_resource_id,
    region: payload.region,
    size: payload.size,
    status: payload.status,
    ttlMinutes: payload.ttl_minutes,
    expiresAt: payload.expires_at,
    terminatedAt: payload.terminated_at,
    terminationReason: payload.termination_reason,
    dataBoundary: payload.data_boundary,
    providerRef:
      typeof payload.metadata.provider_url === "string"
        ? payload.metadata.provider_url
        : undefined,
  };
}

function emptyInfrastructureFeed(
  reason?: string,
  projectId = PROJECT_ID,
): InfrastructureResourceFeed {
  return {
    projectId: resolveProjectId(projectId),
    apiReachable: false,
    blockedReason: reason,
    items: [],
  };
}

export async function fetchInfrastructureResourceFeed(
  projectId = PROJECT_ID,
  limit = 10,
): Promise<InfrastructureResourceFeed> {
  try {
    const payload = await client.listInfraResources(resolveProjectId(projectId), limit);
    return {
      projectId: payload.project_id,
      apiReachable: true,
      items: payload.items.map(mapResource),
    };
  } catch (error) {
    if (error instanceof BossApiError) {
      return emptyInfrastructureFeed(`API returned ${error.status}`, projectId);
    }
    return emptyInfrastructureFeed(
      error instanceof Error ? error.message : "Unknown API error",
      projectId,
    );
  }
}
