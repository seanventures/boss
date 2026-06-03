import { BossApiError, type AgentModuleSummary } from "@boss/api-client";

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
  installed: boolean;
  installedBy?: string;
  installedAt?: string;
  config: Record<string, unknown>;
};

export type AgentModuleFeed = {
  projectId: string;
  apiReachable: boolean;
  blockedReason?: string;
  items: AgentModuleFeedItem[];
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
    installed: payload.installed,
    installedBy: payload.installed_by,
    installedAt: payload.installed_at,
    config: payload.config,
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
  };
}

export async function fetchAgentModuleFeed(
  projectId = PROJECT_ID,
): Promise<AgentModuleFeed> {
  try {
    const payload = await client.listAgentModules(resolveProjectId(projectId));
    return {
      projectId: payload.project_id,
      apiReachable: true,
      items: payload.items.map(mapModule),
    };
  } catch (error) {
    if (error instanceof BossApiError) {
      return emptyAgentModuleFeed(`API returned ${error.status}`, projectId);
    }
    return emptyAgentModuleFeed(
      error instanceof Error ? error.message : "Unknown API error",
      projectId,
    );
  }
}
