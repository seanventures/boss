import { BossApiError, type KpiSummary } from "@boss/api-client";

import { client, PROJECT_ID, resolveProjectId } from "./client";

export type KpiFeedItem = {
  id: string;
  kpiKey: string;
  name: string;
  description?: string;
  ownerRoleId?: string;
  targetValue: number;
  currentValue: number;
  unit: string;
  status: string;
  updatedAt: string;
};

export type KpiFeed = {
  projectId: string;
  apiReachable: boolean;
  blockedReason?: string;
  items: KpiFeedItem[];
};

function mapKpi(payload: KpiSummary): KpiFeedItem {
  return {
    id: payload.id,
    kpiKey: payload.kpi_key,
    name: payload.name,
    description: payload.description,
    ownerRoleId: payload.owner_role_id,
    targetValue: payload.target_value,
    currentValue: payload.current_value,
    unit: payload.unit,
    status: payload.status,
    updatedAt: payload.updated_at,
  };
}

export function emptyKpiFeed(reason?: string, projectId = PROJECT_ID): KpiFeed {
  return {
    projectId: resolveProjectId(projectId),
    apiReachable: false,
    blockedReason: reason,
    items: [],
  };
}

export async function fetchKpiFeed(projectId = PROJECT_ID): Promise<KpiFeed> {
  try {
    const payload = await client.listKpis(resolveProjectId(projectId));
    return {
      projectId: payload.project_id,
      apiReachable: true,
      items: payload.items.map(mapKpi),
    };
  } catch (error) {
    if (error instanceof BossApiError) {
      return emptyKpiFeed(`API returned ${error.status}`, projectId);
    }
    return emptyKpiFeed(
      error instanceof Error ? error.message : "Unknown API error",
      projectId,
    );
  }
}
