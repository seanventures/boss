import {
  type AccountConnectAuthSessionSummary,
  type AccountConnectBillingPortalSessionSummary,
  type AccountConnectTeamInviteSummary,
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

export type AccountConnectTeamInviteItem = {
  id: string;
  hostedOrgId: string;
  email: string;
  displayName?: string;
  roleKeys: string[];
  permissions: string[];
  inviteUrl?: string;
  hostedInviteRef?: string;
  status: string;
  deliveryStatus: string;
  receiptId?: string;
  createdBy: string;
  acceptedAt?: string;
  revokedAt?: string;
  metadata: Record<string, unknown>;
  createdAt: string;
};

export type AccountConnectBillingPortalSessionItem = {
  id: string;
  hostedOrgId: string;
  portalUrl?: string;
  returnUrl?: string;
  providerRef?: string;
  status: string;
  receiptId?: string;
  createdBy: string;
  expiresAt?: string;
  metadata: Record<string, unknown>;
  createdAt: string;
};

export type AccountConnectAuthSessionItem = {
  id: string;
  hostedOrgId?: string;
  authorizationUrl?: string;
  returnUrl?: string;
  providerRef?: string;
  state: string;
  status: string;
  receiptId?: string;
  createdBy: string;
  expiresAt?: string;
  completedAt?: string;
  metadata: Record<string, unknown>;
  createdAt: string;
};

export type CommercialFeed = {
  projectId: string;
  apiReachable: boolean;
  blockedReason?: string;
  edition?: CommercialEditionItem;
  entitlements: LicenseEntitlementItem[];
  authSessions: AccountConnectAuthSessionItem[];
  teamInvites: AccountConnectTeamInviteItem[];
  billingPortalSessions: AccountConnectBillingPortalSessionItem[];
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

function mapTeamInvite(
  payload: AccountConnectTeamInviteSummary,
): AccountConnectTeamInviteItem {
  return {
    id: payload.id,
    hostedOrgId: payload.hosted_org_id,
    email: payload.email,
    displayName: payload.display_name,
    roleKeys: payload.role_keys,
    permissions: payload.permissions,
    inviteUrl: payload.invite_url,
    hostedInviteRef: payload.hosted_invite_ref,
    status: payload.status,
    deliveryStatus: payload.delivery_status,
    receiptId: payload.receipt_id,
    createdBy: payload.created_by,
    acceptedAt: payload.accepted_at,
    revokedAt: payload.revoked_at,
    metadata: payload.metadata,
    createdAt: payload.created_at,
  };
}

function mapBillingPortalSession(
  payload: AccountConnectBillingPortalSessionSummary,
): AccountConnectBillingPortalSessionItem {
  return {
    id: payload.id,
    hostedOrgId: payload.hosted_org_id,
    portalUrl: payload.portal_url,
    returnUrl: payload.return_url,
    providerRef: payload.provider_ref,
    status: payload.status,
    receiptId: payload.receipt_id,
    createdBy: payload.created_by,
    expiresAt: payload.expires_at,
    metadata: payload.metadata,
    createdAt: payload.created_at,
  };
}

function mapAuthSession(
  payload: AccountConnectAuthSessionSummary,
): AccountConnectAuthSessionItem {
  return {
    id: payload.id,
    hostedOrgId: payload.hosted_org_id,
    authorizationUrl: payload.authorization_url,
    returnUrl: payload.return_url,
    providerRef: payload.provider_ref,
    state: payload.state,
    status: payload.status,
    receiptId: payload.receipt_id,
    createdBy: payload.created_by,
    expiresAt: payload.expires_at,
    completedAt: payload.completed_at,
    metadata: payload.metadata,
    createdAt: payload.created_at,
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
    authSessions: [],
    teamInvites: [],
    billingPortalSessions: [],
  };
}

export async function fetchCommercialFeed(
  projectId = PROJECT_ID,
): Promise<CommercialFeed> {
  const resolvedProjectId = resolveProjectId(projectId);
  try {
    const [
      edition,
      entitlementList,
      authSessions,
      teamInvites,
      billingPortalSessions,
    ] = await Promise.all([
      client.getEdition(resolvedProjectId),
      client.listLicenseEntitlements(resolvedProjectId, true),
      client.listAccountConnectAuthSessions(resolvedProjectId),
      client.listAccountConnectTeamInvites(resolvedProjectId),
      client.listAccountConnectBillingPortalSessions(resolvedProjectId),
    ]);
    return {
      projectId: resolvedProjectId,
      apiReachable: true,
      edition: mapEdition(edition),
      entitlements: entitlementList.items.map(mapEntitlement),
      authSessions: authSessions.items.map(mapAuthSession),
      teamInvites: teamInvites.items.map(mapTeamInvite),
      billingPortalSessions: billingPortalSessions.items.map(
        mapBillingPortalSession,
      ),
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
