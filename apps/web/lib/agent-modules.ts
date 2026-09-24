import {
  BossApiError,
  type AgentModuleInstallRecordSummary,
  type AgentModuleSummary,
} from "@boss/api-client";

import { client, PROJECT_ID, resolveProjectId } from "./client";

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

export type AgentModuleFeed = {
  projectId: string;
  apiReachable: boolean;
  blockedReason?: string;
  items: AgentModuleFeedItem[];
  installRecords: AgentModuleInstallRecordFeedItem[];
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
    const [payload, installRecords] = await Promise.all([
      client.listAgentModules(resolvedProjectId),
      client.listAgentModuleInstallRecords(resolvedProjectId),
    ]);
    return {
      projectId: payload.project_id,
      apiReachable: true,
      items: payload.items.map(mapModule),
      installRecords: installRecords.items.map(mapInstallRecord),
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
