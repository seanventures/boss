import {
  BossApiError,
  type EditionStatusResponse,
  type LicenseEntitlementSummary,
} from "@boss/api-client";

import { client, PROJECT_ID, resolveProjectId } from "./client";

export type CommercialEditionItem = {
  projectId: string;
  editionId: string;
  displayName: string;
  status: string;
  accountConnectRequired: boolean;
  accountConnectStatus: string;
  localAuthAllowed: boolean;
  hostedAuthRequired: boolean;
  connectionMode: string;
  licenseCandidate?: string;
  legalReviewRequired: boolean;
  includedCapabilities: string[];
  commercialCapabilities: string[];
  blockedReason?: string;
  sourcePack: string;
  accountConnectOrg?: {
    id: string;
    hostedOrgId: string;
    localOrgId: string;
    connectionMode: string;
    marketplaceIdentity: Record<string, unknown>;
    billingPortalRef?: string;
    connectorAuthorizationRefs: string[];
    optionalTelemetryEnabled: boolean;
    connectionRef?: string;
    status: string;
    linkedBy: string;
    linkedAt?: string;
    unlinkedBy?: string;
    unlinkedAt?: string;
    metadata: Record<string, unknown>;
  };
};

export type LicenseEntitlementItem = {
  id: string;
  editionId: string;
  capabilityKey: string;
  capabilities: string[];
  status: string;
  source: string;
  scopeType: string;
  scopeId: string;
  seats?: number;
  expiresAt?: string;
  metadata: Record<string, unknown>;
};

export type CommercialFeed = {
  projectId: string;
  apiReachable: boolean;
  blockedReason?: string;
  edition?: CommercialEditionItem;
  entitlements: LicenseEntitlementItem[];
};

function mapEdition(payload: EditionStatusResponse): CommercialEditionItem {
  return {
    projectId: payload.project_id,
    editionId: payload.edition_id,
    displayName: payload.display_name,
    status: payload.status,
    accountConnectRequired: payload.account_connect_required,
    accountConnectStatus: payload.account_connect_status,
    localAuthAllowed: payload.local_auth_allowed,
    hostedAuthRequired: payload.hosted_auth_required,
    connectionMode: payload.connection_mode,
    licenseCandidate: payload.license_candidate,
    legalReviewRequired: payload.legal_review_required,
    includedCapabilities: payload.included_capabilities,
    commercialCapabilities: payload.commercial_capabilities,
    blockedReason: payload.blocked_reason,
    sourcePack: payload.source_pack,
    accountConnectOrg: payload.account_connect_org
      ? {
          id: payload.account_connect_org.id,
          hostedOrgId: payload.account_connect_org.hosted_org_id,
          localOrgId: payload.account_connect_org.local_org_id,
          connectionMode: payload.account_connect_org.connection_mode,
          marketplaceIdentity: payload.account_connect_org.marketplace_identity,
          billingPortalRef: payload.account_connect_org.billing_portal_ref,
          connectorAuthorizationRefs:
            payload.account_connect_org.connector_authorization_refs,
          optionalTelemetryEnabled:
            payload.account_connect_org.optional_telemetry_enabled,
          connectionRef: payload.account_connect_org.connection_ref,
          status: payload.account_connect_org.status,
          linkedBy: payload.account_connect_org.linked_by,
          linkedAt: payload.account_connect_org.linked_at,
          unlinkedBy: payload.account_connect_org.unlinked_by,
          unlinkedAt: payload.account_connect_org.unlinked_at,
          metadata: payload.account_connect_org.metadata,
        }
      : undefined,
  };
}

function mapEntitlement(
  payload: LicenseEntitlementSummary,
): LicenseEntitlementItem {
  return {
    id: payload.id,
    editionId: payload.edition_id,
    capabilityKey: payload.capability_key,
    capabilities: payload.capabilities,
    status: payload.status,
    source: payload.source,
    scopeType: payload.scope_type,
    scopeId: payload.scope_id,
    seats: payload.seats,
    expiresAt: payload.expires_at,
    metadata: payload.metadata,
  };
}

export function emptyCommercialFeed(
  reason?: string,
  projectId = PROJECT_ID,
): CommercialFeed {
  return {
    projectId: resolveProjectId(projectId),
    apiReachable: false,
    blockedReason: reason,
    entitlements: [],
  };
}

export async function fetchCommercialFeed(
  projectId = PROJECT_ID,
): Promise<CommercialFeed> {
  const resolvedProjectId = resolveProjectId(projectId);
  try {
    const [edition, entitlementList] = await Promise.all([
      client.getEdition(resolvedProjectId),
      client.listLicenseEntitlements(resolvedProjectId, true),
    ]);
    return {
      projectId: resolvedProjectId,
      apiReachable: true,
      edition: mapEdition(edition),
      entitlements: entitlementList.items.map(mapEntitlement),
    };
  } catch (error) {
    if (error instanceof BossApiError) {
      return emptyCommercialFeed(`API returned ${error.status}`, resolvedProjectId);
    }
    return emptyCommercialFeed(
      error instanceof Error ? error.message : "Unknown API error",
      resolvedProjectId,
    );
  }
}
