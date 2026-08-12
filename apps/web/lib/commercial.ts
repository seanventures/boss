import {
  type AccountConnectAuthSessionSummary,
  type AccountConnectBillingPortalSessionSummary,
  type AccountConnectHealthCheckSummary,
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

export type AccountConnectHealthCheckItem = {
  id: string;
  hostedOrgId?: string;
  checkKind: string;
  status: string;
  latencyMs?: number;
  providerRef?: string;
  readinessPacketArtifactId?: string;
  responseMetadata: Record<string, unknown>;
  error?: Record<string, unknown>;
  checkedBy: string;
  metadata: Record<string, unknown>;
  createdAt: string;
};

export type CloudFeatureItem = {
  key: string;
  title: string;
  description?: string;
  status: string;
  availableNow: boolean;
  communityLocal?: boolean;
  unlock?: string;
};

export type CloudPendingSession = {
  sessionId: string;
  status: string;
  method: string;
  userCode?: string;
  verificationUri?: string;
  authorizationUrl?: string;
  expiresAt?: string;
  message?: string;
};

export type CloudProductSnapshot = {
  independent: boolean;
  linked: boolean;
  message: string;
  easyConnectReady: boolean;
  easyConnectBlockedReason?: string;
  pendingSessions: CloudPendingSession[];
  features: CloudFeatureItem[];
  comparisonRows: Array<{
    capability: string;
    community: string;
    cloud: string;
    enterprise: string;
    status: string;
  }>;
  managedWorkers: {
    status: string;
    availableNow: boolean;
    message?: string;
  };
  capabilitySummary?: {
    localCount: number;
    cloudUnlockedCount: number;
    cloudLockedCount: number;
  };
};

export type CommercialFeed = {
  projectId: string;
  apiReachable: boolean;
  blockedReason?: string;
  edition?: CommercialEditionItem;
  entitlements: LicenseEntitlementItem[];
  healthChecks: AccountConnectHealthCheckItem[];
  authSessions: AccountConnectAuthSessionItem[];
  teamInvites: AccountConnectTeamInviteItem[];
  billingPortalSessions: AccountConnectBillingPortalSessionItem[];
  /** Community-safe Cloud product surface (catalog/status/comparison). */
  cloud?: CloudProductSnapshot;
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

function mapHealthCheck(
  payload: AccountConnectHealthCheckSummary,
): AccountConnectHealthCheckItem {
  return {
    id: payload.id,
    hostedOrgId: payload.hosted_org_id,
    checkKind: payload.check_kind,
    status: payload.status,
    latencyMs: payload.latency_ms,
    providerRef: payload.provider_ref,
    readinessPacketArtifactId: payload.readiness_packet_artifact_id,
    responseMetadata: payload.response_metadata,
    error: payload.error,
    checkedBy: payload.checked_by,
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
    healthChecks: [],
    authSessions: [],
    teamInvites: [],
    billingPortalSessions: [],
  };
}

async function settleOptional<T>(
  promise: Promise<T>,
): Promise<T | undefined> {
  try {
    return await promise;
  } catch (error) {
    // Community edition does not mount paid Account Connect routes (404).
    if (error instanceof BossApiError && [401, 403, 404, 405].includes(error.status)) {
      return undefined;
    }
    return undefined;
  }
}

function mapCloudSnapshot(
  catalog: Awaited<ReturnType<typeof client.getCloudCatalog>>,
  status: Awaited<ReturnType<typeof client.getCloudStatus>>,
  comparison?: Awaited<ReturnType<typeof client.getCloudComparison>>,
  capabilities?: Awaited<ReturnType<typeof client.getCloudCapabilities>>,
  workers?: Awaited<ReturnType<typeof client.getCloudWorkers>>,
): CloudProductSnapshot {
  const pendingRaw = (status.easy_connect?.pending_sessions as
    | Array<Record<string, unknown>>
    | undefined) ?? [];
  return {
    independent: status.independent,
    linked: status.linked,
    message: status.message,
    easyConnectReady: Boolean(status.easy_connect?.ready),
    easyConnectBlockedReason:
      typeof status.easy_connect?.blocked_reason === "string"
        ? status.easy_connect.blocked_reason
        : undefined,
    pendingSessions: pendingRaw.map((row) => ({
      sessionId: String(row.session_id ?? ""),
      status: String(row.status ?? "pending"),
      method: String(row.method ?? "browser"),
      userCode:
        typeof row.user_code === "string" ? row.user_code : undefined,
      verificationUri:
        typeof row.verification_uri === "string"
          ? row.verification_uri
          : undefined,
      authorizationUrl:
        typeof row.authorization_url === "string"
          ? row.authorization_url
          : undefined,
      expiresAt:
        typeof row.expires_at === "string" ? row.expires_at : undefined,
      message: typeof row.message === "string" ? row.message : undefined,
    })),
    features: (catalog.cloud_features ?? []).map((feature) => ({
      key: String(feature.key ?? ""),
      title: String(feature.title ?? feature.key ?? ""),
      description:
        typeof feature.description === "string"
          ? feature.description
          : undefined,
      status: String(feature.status ?? "planned"),
      availableNow: Boolean(feature.available_now),
      communityLocal: Boolean(feature.community_local),
      unlock:
        typeof feature.unlock === "string" ? feature.unlock : undefined,
    })),
    comparisonRows: (comparison?.rows ?? []).map((row) => ({
      capability: String(row.capability ?? ""),
      community: String(row.community ?? ""),
      cloud: String(row.cloud ?? ""),
      enterprise: String(row.enterprise ?? ""),
      status: String(row.status ?? ""),
    })),
    managedWorkers: {
      status: String(workers?.status ?? status.managed_workers?.status ?? "planned"),
      availableNow: Boolean(
        workers?.available_now ?? status.managed_workers?.available_now,
      ),
      message:
        typeof workers?.managed_capacity?.message === "string"
          ? workers.managed_capacity.message
          : typeof (status.managed_workers as { managed_capacity?: { message?: string } } | undefined)
                ?.managed_capacity?.message === "string"
            ? (status.managed_workers as { managed_capacity: { message: string } })
                .managed_capacity.message
            : undefined,
    },
    capabilitySummary: capabilities
      ? {
          localCount: Number(capabilities.summary.local_count ?? 0),
          cloudUnlockedCount: Number(
            capabilities.summary.cloud_unlocked_count ?? 0,
          ),
          cloudLockedCount: Number(capabilities.summary.cloud_locked_count ?? 0),
        }
      : undefined,
  };
}

export async function fetchCommercialFeed(
  projectId = PROJECT_ID,
): Promise<CommercialFeed> {
  const resolvedProjectId = resolveProjectId(projectId);
  try {
    // Community-safe core: edition + entitlements + /v1/cloud/* always mount.
    // Paid Account Connect list routes are optional (404 on community edition).
    // Edition + entitlements are the Community-safe core. Cloud routes are optional
    // so older APIs / Community-only deploys still load Editions like the site describes.
    const [
      edition,
      entitlementList,
      catalog,
      cloudStatus,
      comparison,
      capabilities,
      workers,
      healthChecks,
      authSessions,
      teamInvites,
      billingPortalSessions,
    ] = await Promise.all([
      client.getEdition(resolvedProjectId),
      client.listLicenseEntitlements(resolvedProjectId, true),
      settleOptional(client.getCloudCatalog(resolvedProjectId)),
      settleOptional(client.getCloudStatus(resolvedProjectId)),
      settleOptional(client.getCloudComparison(resolvedProjectId)),
      settleOptional(client.getCloudCapabilities(resolvedProjectId)),
      settleOptional(client.getCloudWorkers(resolvedProjectId)),
      settleOptional(client.listAccountConnectHealthChecks(resolvedProjectId)),
      settleOptional(client.listAccountConnectAuthSessions(resolvedProjectId)),
      settleOptional(client.listAccountConnectTeamInvites(resolvedProjectId)),
      settleOptional(
        client.listAccountConnectBillingPortalSessions(resolvedProjectId),
      ),
    ]);
    const cloud =
      catalog && cloudStatus
        ? mapCloudSnapshot(
            catalog,
            cloudStatus,
            comparison,
            capabilities,
            workers,
          )
        : {
            // Fallback when /v1/cloud/* is not deployed yet — still match website claims.
            independent:
              !edition.account_connect_required ||
              edition.account_connect_status === "linked" ||
              edition.account_connect_status === "not_required",
            linked: edition.account_connect_status === "linked",
            message:
              edition.edition_id === "community"
                ? "This install runs independently. Connect to BOSS Cloud only when you want hosted team, marketplace, or managed capacity."
                : edition.blocked_reason || "Edition status from control plane.",
            easyConnectReady: false,
            easyConnectBlockedReason:
              "Cloud Easy Connect APIs not available on this control plane yet — redeploy API with /v1/cloud/*.",
            pendingSessions: [],
            features: [
              {
                key: "durable_control_plane",
                title: "Durable task & decision state",
                status: "shipped",
                availableNow: true,
                communityLocal: true,
              },
              {
                key: "approvals_policy",
                title: "Approvals & policy management",
                status: "shipped",
                availableNow: true,
                communityLocal: true,
              },
              {
                key: "managed_workers",
                title: "Managed workers & elastic infra",
                status: "planned",
                availableNow: false,
                unlock: "connect_cloud",
              },
            ],
            comparisonRows: [],
            managedWorkers: {
              status: "planned",
              availableNow: false,
              message: "Use local Community runners until Cloud fleets ship.",
            },
          };
    return {
      projectId: resolvedProjectId,
      apiReachable: true,
      edition: mapEdition(edition),
      entitlements: entitlementList.items.map(mapEntitlement),
      healthChecks: (healthChecks?.items ?? []).map(mapHealthCheck),
      authSessions: (authSessions?.items ?? []).map(mapAuthSession),
      teamInvites: (teamInvites?.items ?? []).map(mapTeamInvite),
      billingPortalSessions: (billingPortalSessions?.items ?? []).map(
        mapBillingPortalSession,
      ),
      cloud,
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
