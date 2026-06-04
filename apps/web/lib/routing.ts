import {
  BossApiError,
  type ModelRouteDecisionListFilters,
  type ModelRouteDecisionSummary,
} from "@boss/api-client";

import { client, PROJECT_ID, resolveProjectId } from "./client";

export type RoutingLogFeedItem = {
  id: string;
  skillId?: string;
  taskKind: string;
  workloadClass: string;
  dataBoundary: string;
  selectedSourceId?: string;
  selectedProvider?: string;
  selectedModelId?: string;
  denialReason?: string;
  selectedSource?: Record<string, unknown>;
  rejectedSources: Array<Record<string, unknown>>;
  routingMode: string;
  budgetState: string;
  policyAllowed: boolean;
  reservationRequired: boolean;
  createdAt: string;
};

export type RoutingLogFeed = {
  projectId: string;
  apiReachable: boolean;
  blockedReason?: string;
  items: RoutingLogFeedItem[];
};

export type RoutingLogFeedFilters = Pick<
  ModelRouteDecisionListFilters,
  | "provider"
  | "source_id"
  | "model_id"
  | "task_kind"
  | "workload_class"
  | "budget_state"
  | "data_boundary"
  | "risk_tier"
  | "denial_reason"
  | "denied_only"
  | "selected_only"
>;

function mapLog(payload: ModelRouteDecisionSummary): RoutingLogFeedItem {
  const policyResult = payload.policy_result ?? {};
  return {
    id: payload.id,
    skillId: payload.skill_id,
    taskKind: payload.task_kind,
    workloadClass: payload.workload_class,
    dataBoundary: payload.data_boundary,
    selectedSourceId: payload.selected_source_id,
    selectedProvider: payload.selected_provider,
    selectedModelId: payload.selected_model_id,
    denialReason:
      typeof policyResult.denial_reason === "string"
        ? policyResult.denial_reason
        : undefined,
    selectedSource:
      payload.capacity_snapshot && typeof payload.capacity_snapshot === "object"
        ? payload.capacity_snapshot
        : undefined,
    rejectedSources: payload.rejected_sources,
    routingMode: payload.routing_mode,
    budgetState: payload.budget_state,
    policyAllowed: policyResult.allowed === true,
    reservationRequired: policyResult.reservation_required === true,
    createdAt: payload.created_at,
  };
}

export function emptyRoutingLogFeed(reason?: string, projectId = PROJECT_ID): RoutingLogFeed {
  return {
    projectId: resolveProjectId(projectId),
    apiReachable: false,
    blockedReason: reason,
    items: [],
  };
}

export async function fetchRoutingLogFeed(
  projectId = PROJECT_ID,
  limit = 8,
  filters: RoutingLogFeedFilters = {},
): Promise<RoutingLogFeed> {
  try {
    const payload = await client.listModelRouteDecisions(resolveProjectId(projectId), {
      ...filters,
      limit,
    });
    return {
      projectId: payload.project_id,
      apiReachable: true,
      items: payload.items.map(mapLog),
    };
  } catch (error) {
    if (error instanceof BossApiError) {
      return emptyRoutingLogFeed(`API returned ${error.status}`, projectId);
    }
    return emptyRoutingLogFeed(
      error instanceof Error ? error.message : "Unknown API error",
      projectId,
    );
  }
}
