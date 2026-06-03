import { BossApiError, type EntitlementSourceSummary } from "@boss/api-client";

import { client, PROJECT_ID, resolveProjectId } from "./client";

export type EntitlementSourceFeedItem = {
  id: string;
  provider: string;
  sourceId: string;
  sourceKind: string;
  status: string;
  effectiveStatus: string;
  runtimeSupported: boolean;
  credentialPresent: boolean;
  credentialRef?: string;
  supportedModels: string[];
};

export type EntitlementFeed = {
  projectId: string;
  apiReachable: boolean;
  blockedReason?: string;
  items: EntitlementSourceFeedItem[];
};

function mapSource(payload: EntitlementSourceSummary): EntitlementSourceFeedItem {
  return {
    id: payload.id,
    provider: payload.provider,
    sourceId: payload.source_id,
    sourceKind: payload.source_kind,
    status: payload.status,
    effectiveStatus: payload.effective_status,
    runtimeSupported: payload.runtime_supported,
    credentialPresent: Boolean(payload.observed_capacity?.credential_present),
    credentialRef: payload.credential_ref,
    supportedModels: payload.supported_models,
  };
}

export function emptyEntitlementFeed(reason?: string, projectId = PROJECT_ID): EntitlementFeed {
  return {
    projectId: resolveProjectId(projectId),
    apiReachable: false,
    blockedReason: reason,
    items: [],
  };
}

export async function fetchEntitlementFeed(
  projectId = PROJECT_ID,
): Promise<EntitlementFeed> {
  try {
    const payload = await client.listEntitlementSources(resolveProjectId(projectId));
    return {
      projectId: payload.project_id,
      apiReachable: true,
      items: payload.items.map(mapSource),
    };
  } catch (error) {
    if (error instanceof BossApiError) {
      return emptyEntitlementFeed(`API returned ${error.status}`, projectId);
    }
    return emptyEntitlementFeed(
      error instanceof Error ? error.message : "Unknown API error",
      projectId,
    );
  }
}
