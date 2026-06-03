import { BossApiError, type EntitlementSourceSummary } from "@boss/api-client";

import { client, PROJECT_ID } from "./client";

export type EntitlementSourceFeedItem = {
  id: string;
  provider: string;
  sourceId: string;
  sourceKind: string;
  status: string;
  effectiveStatus: string;
  runtimeSupported: boolean;
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
    supportedModels: payload.supported_models,
  };
}

export function emptyEntitlementFeed(reason?: string): EntitlementFeed {
  return {
    projectId: PROJECT_ID,
    apiReachable: false,
    blockedReason: reason,
    items: [],
  };
}

export async function fetchEntitlementFeed(): Promise<EntitlementFeed> {
  try {
    const payload = await client.listEntitlementSources(PROJECT_ID);
    return {
      projectId: payload.project_id,
      apiReachable: true,
      items: payload.items.map(mapSource),
    };
  } catch (error) {
    if (error instanceof BossApiError) {
      return emptyEntitlementFeed(`API returned ${error.status}`);
    }
    return emptyEntitlementFeed(
      error instanceof Error ? error.message : "Unknown API error",
    );
  }
}
