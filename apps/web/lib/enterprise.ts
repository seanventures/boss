import {
  BossApiError,
  type EnterpriseClusterSummary,
  type EnterpriseScimGroupSummary,
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

export type EnterpriseFeed = {
  projectId: string;
  apiReachable: boolean;
  blockedReason?: string;
  clusters: EnterpriseClusterItem[];
  scimGroups: EnterpriseScimGroupItem[];
  scimUsers: EnterpriseScimUserItem[];
  summary: {
    clusterCount: number;
    activeScimUsers: number;
    blockingGapCount: number;
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

export function emptyEnterpriseFeed(
  reason?: string,
  projectId = PROJECT_ID,
): EnterpriseFeed {
  return {
    projectId: resolveProjectId(projectId),
    apiReachable: false,
    blockedReason: reason,
    clusters: [],
    scimGroups: [],
    scimUsers: [],
    summary: {
      clusterCount: 0,
      activeScimUsers: 0,
      blockingGapCount: 0,
    },
  };
}

export async function fetchEnterpriseFeed(
  projectId = PROJECT_ID,
): Promise<EnterpriseFeed> {
  const resolvedProjectId = resolveProjectId(projectId);
  try {
    const [clusters, scimGroups, scimUsers] = await Promise.all([
      client.listEnterpriseClusters(resolvedProjectId),
      client.listEnterpriseScimGroups(resolvedProjectId),
      client.listEnterpriseScimUsers(resolvedProjectId),
    ]);
    const clusterItems = clusters.items.map(mapCluster);
    const userItems = scimUsers.items.map(mapScimUser);
    return {
      projectId: resolvedProjectId,
      apiReachable: true,
      clusters: clusterItems,
      scimGroups: scimGroups.items.map(mapScimGroup),
      scimUsers: userItems,
      summary: {
        clusterCount: clusterItems.length,
        activeScimUsers: userItems.filter((user) => user.active).length,
        blockingGapCount: clusterItems.reduce(
          (total, cluster) => total + cluster.blockingGaps.length,
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
