import {
  BossApiError,
  type AgentModuleInstallRecordSummary,
  type AgentModuleSummary,
  type HostedMarketplaceListingSummary,
  type HostedMarketplaceSyncRunSummary,
} from "@boss/api-client";

import { client, PROJECT_ID, resolveProjectId } from "./client";
import { editionOptionalList } from "./edition-gating";

export type AgentModuleFeedItem = {
  moduleId: string;
  version: string;
  displayName: string;
  description: string;
  publisher: string;
  category: string;
  status: string;
  roleKey?: string;
  jobKinds: string[];
  defaultSkillIds: string[];
  tags: string[];
  priceModel?: string;
  priceUsdMicros?: number;
  requiredEntitlements: string[];
  entitlementStatus: string;
  installed: boolean;
  installedBy?: string;
  installedAt?: string;
  config: Record<string, unknown>;
};

export type AgentModuleInstallRecordFeedItem = {
  id: string;
  moduleId: string;
  action: string;
  fromVersion?: string;
  toVersion?: string;
  actor: string;
  status: string;
  entitlementResult: Record<string, unknown>;
  rollbackSnapshot: Record<string, unknown>;
  metadata: Record<string, unknown>;
  createdAt: string;
};

export type HostedMarketplaceListingFeedItem = {
  id: string;
  hostedOrgId?: string;
  listingId: string;
  packId: string;
  version: string;
  displayName: string;
  publisher: string;
  category: string;
  description?: string;
  status: string;
  priceModel?: string;
  priceUsdMicros?: number;
  requiredEntitlements: string[];
  capabilities: string[];
  sourceUrl?: string;
  metadata: Record<string, unknown>;
  lastSyncRunId?: string;
  updatedAt: string;
};

export type HostedMarketplaceSyncRunFeedItem = {
  id: string;
  hostedOrgId?: string;
  status: string;
  listingCount: number;
  providerRef?: string;
  syncPacketArtifactId?: string;
  error?: Record<string, unknown>;
  syncedBy: string;
  metadata: Record<string, unknown>;
  createdAt: string;
};

export type AgentModuleFeed = {
  projectId: string;
  apiReachable: boolean;
  blockedReason?: string;
  items: AgentModuleFeedItem[];
  installRecords: AgentModuleInstallRecordFeedItem[];
  hostedListings: HostedMarketplaceListingFeedItem[];
  marketplaceSyncRuns: HostedMarketplaceSyncRunFeedItem[];
};

function mapModule(payload: AgentModuleSummary): AgentModuleFeedItem {
  return {
    moduleId: payload.module_id,
    version: payload.version,
    displayName: payload.display_name,
    description: payload.description,
    publisher: payload.publisher,
    category: payload.category,
    status: payload.status,
    roleKey: payload.role_key,
    jobKinds: payload.job_kinds,
    defaultSkillIds: payload.default_skill_ids,
    tags: payload.tags,
    priceModel: payload.price_model,
    priceUsdMicros: payload.price_usd_micros,
    requiredEntitlements: payload.required_entitlements,
    entitlementStatus: payload.entitlement_status,
    installed: payload.installed,
    installedBy: payload.installed_by,
    installedAt: payload.installed_at,
    config: payload.config,
  };
}

function mapInstallRecord(
  payload: AgentModuleInstallRecordSummary,
): AgentModuleInstallRecordFeedItem {
  return {
    id: payload.id,
    moduleId: payload.module_id,
    action: payload.action,
    fromVersion: payload.from_version,
    toVersion: payload.to_version,
    actor: payload.actor,
    status: payload.status,
    entitlementResult: payload.entitlement_result,
    rollbackSnapshot: payload.rollback_snapshot,
    metadata: payload.metadata,
    createdAt: payload.created_at,
  };
}

function mapHostedListing(
  payload: HostedMarketplaceListingSummary,
): HostedMarketplaceListingFeedItem {
  return {
    id: payload.id,
    hostedOrgId: payload.hosted_org_id,
    listingId: payload.listing_id,
    packId: payload.pack_id,
    version: payload.version,
    displayName: payload.display_name,
    publisher: payload.publisher,
    category: payload.category,
    description: payload.description,
    status: payload.status,
    priceModel: payload.price_model,
    priceUsdMicros: payload.price_usd_micros,
    requiredEntitlements: payload.required_entitlements,
    capabilities: payload.capabilities,
    sourceUrl: payload.source_url,
    metadata: payload.metadata,
    lastSyncRunId: payload.last_sync_run_id,
    updatedAt: payload.updated_at,
  };
}

function mapMarketplaceSyncRun(
  payload: HostedMarketplaceSyncRunSummary,
): HostedMarketplaceSyncRunFeedItem {
  return {
    id: payload.id,
    hostedOrgId: payload.hosted_org_id,
    status: payload.status,
    listingCount: payload.listing_count,
    providerRef: payload.provider_ref,
    syncPacketArtifactId: payload.sync_packet_artifact_id,
    error: payload.error,
    syncedBy: payload.synced_by,
    metadata: payload.metadata,
    createdAt: payload.created_at,
  };
}

export function emptyAgentModuleFeed(
  reason?: string,
  projectId = PROJECT_ID,
): AgentModuleFeed {
  return {
    projectId: resolveProjectId(projectId),
    apiReachable: false,
    blockedReason: reason,
    items: [],
    installRecords: [],
    hostedListings: [],
    marketplaceSyncRuns: [],
  };
}

export async function fetchAgentModuleFeed(
  projectId = PROJECT_ID,
  active = true,
): Promise<AgentModuleFeed> {
  // Not rendered this request. Same shape the unreachable path returns, no round trip.
  if (!active) return emptyAgentModuleFeed("not requested", resolveProjectId(projectId));
  const resolvedProjectId = resolveProjectId(projectId);
  try {
    // The marketplace routes are hosted/paid; on Community they 404. A bare Promise.all threw
    // away `listAgentModules` and `listAgentModuleInstallRecords` — both of which return 200 —
    // which is why "Available agent modules" reported an API error while its endpoint was fine.
    const [payload, installRecords, hostedListings, syncRuns] = await Promise.all([
      client.listAgentModules(resolvedProjectId),
      client.listAgentModuleInstallRecords(resolvedProjectId),
      editionOptionalList(client.listHostedMarketplaceListings(resolvedProjectId)),
      editionOptionalList(client.listHostedMarketplaceSyncRuns(resolvedProjectId)),
    ]);
    return {
      projectId: payload.project_id,
      apiReachable: true,
      items: payload.items.map(mapModule),
      installRecords: installRecords.items.map(mapInstallRecord),
      hostedListings: hostedListings.items.map(mapHostedListing),
      marketplaceSyncRuns: syncRuns.items.map(mapMarketplaceSyncRun),
    };
  } catch (error) {
    if (error instanceof BossApiError) {
      return emptyAgentModuleFeed(`API returned ${error.status}`, resolvedProjectId);
    }
    return emptyAgentModuleFeed(
      error instanceof Error ? error.message : "Unknown API error",
      resolvedProjectId,
    );
  }
}
