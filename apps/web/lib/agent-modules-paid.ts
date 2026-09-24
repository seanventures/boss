import {
  BossApiError,
  type HostedMarketplaceListingSummary,
  type HostedMarketplaceSyncRunSummary,
} from "@boss/api-client";
import { client, PROJECT_ID, resolveProjectId } from "./client";
import { editionOptionalList } from "./edition-gating";
import { emptyAgentModuleFeed, fetchAgentModuleFeed, type AgentModuleFeed } from "./agent-modules";

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

export type PaidAgentModuleFeed = AgentModuleFeed & {
  hostedListings: HostedMarketplaceListingFeedItem[];
  marketplaceSyncRuns: HostedMarketplaceSyncRunFeedItem[];
};

export async function fetchAgentModuleFeedWithMarketplace(
  projectId = PROJECT_ID,
  active = true,
): Promise<PaidAgentModuleFeed> {
  const resolvedProjectId = resolveProjectId(projectId);
  if (!active) return {
    ...emptyAgentModuleFeed("not requested", resolvedProjectId),
    hostedListings: [],
    marketplaceSyncRuns: [],
  };
  try {
    const [core, listings, runs] = await Promise.all([
      fetchAgentModuleFeed(resolvedProjectId, active),
      editionOptionalList(client.listHostedMarketplaceListings(resolvedProjectId)),
      editionOptionalList(client.listHostedMarketplaceSyncRuns(resolvedProjectId)),
    ]);
    return {
      ...core,
      hostedListings: listings.items.map(mapHostedListing),
      marketplaceSyncRuns: runs.items.map(mapMarketplaceSyncRun),
    };
  } catch (error) {
    const reason = error instanceof BossApiError
      ? `API returned ${error.status}`
      : error instanceof Error ? error.message : "Unknown API error";
    return { ...emptyAgentModuleFeed(reason, resolvedProjectId), hostedListings: [], marketplaceSyncRuns: [] };
  }
}
