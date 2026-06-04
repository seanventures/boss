import {
  BossApiError,
  type EnterpriseClusterSummary,
  type EnterpriseClusterValidationRunSummary,
  type EnterpriseIdentityRolloutPolicySummary,
  type EnterpriseIdentityProviderSummary,
  type EnterpriseScimGroupSummary,
  type EnterpriseSupportCaseSummary,
  type EnterpriseScimUserSummary,
} from "@boss/api-client";

import { client, PROJECT_ID, resolveProjectId } from "./client";

export type EnterpriseClusterItem = {
  id: string;
  deploymentMode: string;
  accountConnectMode: string;
  workerPoolStrategy: string;
  haProfile: string;
  dataResidency: string;
  identityControls: Record<string, unknown>;
  governanceControls: Record<string, unknown>;
  auditControls: Record<string, unknown>;
  privateWorkerPools: Array<Record<string, unknown>>;
  compliancePacks: string[];
  supportTier: string;
  supportSla: Record<string, unknown>;
  status: string;
  blockingGaps: string[];
  createdBy: string;
  updatedAt: string;
};

export type EnterpriseScimGroupItem = {
  id: string;
  externalId?: string;
  displayName: string;
  roleKeys: string[];
  permissions: string[];
  status: string;
  updatedAt: string;
};

export type EnterpriseIdentityProviderItem = {
  id: string;
  providerKey: string;
  providerType: string;
  displayName: string;
  issuer?: string;
  audience?: string;
  entityId?: string;
  ssoUrl?: string;
  jwksUrl?: string;
  certificateFingerprint?: string;
  scimBaseUrl?: string;
  secretRef?: string;
  status: string;
  testStatus: string;
  blockingGaps: string[];
  updatedAt: string;
};

export type EnterpriseIdentityRolloutPolicyItem = {
  id: string;
  providerType: string;
  runtimeMode: string;
  allowedDomains: string[];
  rolloutNotes?: string;
  status: string;
  createdBy: string;
  updatedAt: string;
};

export type EnterpriseScimUserItem = {
  id: string;
  externalId?: string;
  userName: string;
  displayName: string;
  memberId: string;
  roleKeys: string[];
  permissions: string[];
  groups: string[];
  status: string;
  active: boolean;
  updatedAt: string;
};

export type EnterpriseSupportCaseItem = {
  id: string;
  title: string;
  severity: string;
  category: string;
  description: string;
  status: string;
  slaDueAt?: string;
  responseTimeHours: number;
  enterpriseClusterId?: string;
  accountConnectOrgId?: string;
  hostedOrgId?: string;
  hostedCaseRef?: string;
  receiptId?: string;
  supportPacketArtifactId?: string;
  assignedTo?: string;
  createdBy: string;
  metadata: Record<string, unknown>;
  updatedAt: string;
};

export type EnterpriseClusterValidationRunItem = {
  id: string;
  enterpriseClusterId: string;
  validationKind: string;
  status: string;
  checks: Array<Record<string, unknown>>;
  evidenceRefs: string[];
  blockingGaps: string[];
  validatedBy: string;
  metadata: Record<string, unknown>;
  createdAt: string;
};

export type EnterpriseFeed = {
  projectId: string;
  apiReachable: boolean;
  blockedReason?: string;
  clusters: EnterpriseClusterItem[];
  clusterValidationRuns: EnterpriseClusterValidationRunItem[];
  identityProviders: EnterpriseIdentityProviderItem[];
  identityRolloutPolicies: EnterpriseIdentityRolloutPolicyItem[];
  scimGroups: EnterpriseScimGroupItem[];
  scimUsers: EnterpriseScimUserItem[];
  supportCases: EnterpriseSupportCaseItem[];
  summary: {
    clusterCount: number;
    identityProviderCount: number;
    activeIdentityProviders: number;
    activeScimUsers: number;
    blockingGapCount: number;
    supportCaseCount: number;
    openSupportCases: number;
    blockedSupportCases: number;
  };
};

function mapCluster(payload: EnterpriseClusterSummary): EnterpriseClusterItem {
  return {
    id: payload.id,
    deploymentMode: payload.deployment_mode,
    accountConnectMode: payload.account_connect_mode,
    workerPoolStrategy: payload.worker_pool_strategy,
    haProfile: payload.ha_profile,
    dataResidency: payload.data_residency,
    identityControls: payload.identity_controls,
    governanceControls: payload.governance_controls,
    auditControls: payload.audit_controls,
    privateWorkerPools: payload.private_worker_pools,
    compliancePacks: payload.compliance_packs,
    supportTier: payload.support_tier,
    supportSla: payload.support_sla,
    status: payload.status,
    blockingGaps: payload.blocking_gaps,
    createdBy: payload.created_by,
    updatedAt: payload.updated_at,
  };
}

function mapClusterValidationRun(
  payload: EnterpriseClusterValidationRunSummary,
): EnterpriseClusterValidationRunItem {
  return {
    id: payload.id,
    enterpriseClusterId: payload.enterprise_cluster_id,
    validationKind: payload.validation_kind,
    status: payload.status,
    checks: payload.checks,
    evidenceRefs: payload.evidence_refs,
    blockingGaps: payload.blocking_gaps,
    validatedBy: payload.validated_by,
    metadata: payload.metadata,
    createdAt: payload.created_at,
  };
}

function mapIdentityProvider(
  payload: EnterpriseIdentityProviderSummary,
): EnterpriseIdentityProviderItem {
  return {
    id: payload.id,
    providerKey: payload.provider_key,
    providerType: payload.provider_type,
    displayName: payload.display_name,
    issuer: payload.issuer,
    audience: payload.audience,
    entityId: payload.entity_id,
    ssoUrl: payload.sso_url,
    jwksUrl: payload.jwks_url,
    certificateFingerprint: payload.certificate_fingerprint,
    scimBaseUrl: payload.scim_base_url,
    secretRef: payload.secret_ref,
    status: payload.status,
    testStatus: payload.test_status,
    blockingGaps: payload.blocking_gaps,
    updatedAt: payload.updated_at,
  };
}

function mapIdentityRolloutPolicy(
  payload: EnterpriseIdentityRolloutPolicySummary,
): EnterpriseIdentityRolloutPolicyItem {
  return {
    id: payload.id,
    providerType: payload.provider_type,
    runtimeMode: payload.runtime_mode,
    allowedDomains: payload.allowed_domains,
    rolloutNotes: payload.rollout_notes,
    status: payload.status,
    createdBy: payload.created_by,
    updatedAt: payload.updated_at,
  };
}

function mapScimGroup(
  payload: EnterpriseScimGroupSummary,
): EnterpriseScimGroupItem {
  return {
    id: payload.id,
    externalId: payload.external_id,
    displayName: payload.display_name,
    roleKeys: payload.role_keys,
    permissions: payload.permissions,
    status: payload.status,
    updatedAt: payload.updated_at,
  };
}

function mapScimUser(payload: EnterpriseScimUserSummary): EnterpriseScimUserItem {
  return {
    id: payload.id,
    externalId: payload.external_id,
    userName: payload.user_name,
    displayName: payload.display_name,
    memberId: payload.member_id,
    roleKeys: payload.role_keys,
    permissions: payload.permissions,
    groups: payload.groups,
    status: payload.status,
    active: payload.active,
    updatedAt: payload.updated_at,
  };
}

function mapSupportCase(
  payload: EnterpriseSupportCaseSummary,
): EnterpriseSupportCaseItem {
  return {
    id: payload.id,
    title: payload.title,
    severity: payload.severity,
    category: payload.category,
    description: payload.description,
    status: payload.status,
    slaDueAt: payload.sla_due_at,
    responseTimeHours: payload.response_time_hours,
    enterpriseClusterId: payload.enterprise_cluster_id,
    accountConnectOrgId: payload.account_connect_org_id,
    hostedOrgId: payload.hosted_org_id,
    hostedCaseRef: payload.hosted_case_ref,
    receiptId: payload.receipt_id,
    supportPacketArtifactId: payload.support_packet_artifact_id,
    assignedTo: payload.assigned_to,
    createdBy: payload.created_by,
    metadata: payload.metadata,
    updatedAt: payload.updated_at,
  };
}

export function emptyEnterpriseFeed(
  reason?: string,
  projectId = PROJECT_ID,
): EnterpriseFeed {
  return {
    projectId: resolveProjectId(projectId),
    apiReachable: false,
    blockedReason: reason,
    clusters: [],
    clusterValidationRuns: [],
    identityProviders: [],
    identityRolloutPolicies: [],
    scimGroups: [],
    scimUsers: [],
    supportCases: [],
    summary: {
      clusterCount: 0,
      identityProviderCount: 0,
      activeIdentityProviders: 0,
      activeScimUsers: 0,
      blockingGapCount: 0,
      supportCaseCount: 0,
      openSupportCases: 0,
      blockedSupportCases: 0,
    },
  };
}

export async function fetchEnterpriseFeed(
  projectId = PROJECT_ID,
): Promise<EnterpriseFeed> {
  const resolvedProjectId = resolveProjectId(projectId);
  try {
    const [
      clusters,
      clusterValidationRuns,
      identityProviders,
      identityRolloutPolicies,
      scimGroups,
      scimUsers,
      supportCases,
    ] = await Promise.all([
      client.listEnterpriseClusters(resolvedProjectId),
      client.listEnterpriseClusterValidationRuns(resolvedProjectId),
      client.listEnterpriseIdentityProviders(resolvedProjectId),
      client.listEnterpriseIdentityRolloutPolicies(resolvedProjectId),
      client.listEnterpriseScimGroups(resolvedProjectId),
      client.listEnterpriseScimUsers(resolvedProjectId),
      client.listEnterpriseSupportCases(resolvedProjectId),
    ]);
    const clusterItems = clusters.items.map(mapCluster);
    const validationItems = clusterValidationRuns.items.map(
      mapClusterValidationRun,
    );
    const providerItems = identityProviders.items.map(mapIdentityProvider);
    const userItems = scimUsers.items.map(mapScimUser);
    const supportItems = supportCases.items.map(mapSupportCase);
    return {
      projectId: resolvedProjectId,
      apiReachable: true,
      clusters: clusterItems,
      clusterValidationRuns: validationItems,
      identityProviders: providerItems,
      identityRolloutPolicies: identityRolloutPolicies.items.map(
        mapIdentityRolloutPolicy,
      ),
      scimGroups: scimGroups.items.map(mapScimGroup),
      scimUsers: userItems,
      supportCases: supportItems,
      summary: {
        clusterCount: clusterItems.length,
        identityProviderCount: providerItems.length,
        activeIdentityProviders: providerItems.filter(
          (provider) => provider.status === "active",
        ).length,
        activeScimUsers: userItems.filter((user) => user.active).length,
        supportCaseCount: supportItems.length,
        openSupportCases: supportItems.filter((item) =>
          ["open", "in_progress", "failed", "blocked_configuration"].includes(
            item.status,
          ),
        ).length,
        blockedSupportCases: supportItems.filter((item) =>
          item.status.startsWith("blocked") || item.status === "failed",
        ).length,
        blockingGapCount:
          clusterItems.reduce(
            (total, cluster) => total + cluster.blockingGaps.length,
            0,
          ) +
          validationItems.reduce(
            (total, run) => total + run.blockingGaps.length,
            0,
          ) +
          providerItems.reduce(
            (total, provider) => total + provider.blockingGaps.length,
            0,
          ),
      },
    };
  } catch (error) {
    if (error instanceof BossApiError) {
      return emptyEnterpriseFeed(`API returned ${error.status}`, resolvedProjectId);
    }
    return emptyEnterpriseFeed(
      error instanceof Error ? error.message : "Unknown API error",
      resolvedProjectId,
    );
  }
}
