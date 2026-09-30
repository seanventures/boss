import { BossApiError, type BudgetAllocationSummary } from "@boss/api-client";

import { client, PROJECT_ID, resolveProjectId } from "./client";

export type BudgetAllocationFeedItem = {
  id: string;
  parentAllocationId?: string;
  projectId: string;
  scopeType: string;
  scopeId: string;
  amountUsdMicros: number;
  childAllocatedUsdMicros: number;
  reservedUsdMicros: number;
  actualUsdMicros: number;
  availableUsdMicros: number;
  periodStart: string;
  periodEnd: string;
  status: string;
  createdAt: string;
};

export type BudgetAllocationFeed = {
  projectId: string;
  apiReachable: boolean;
  blockedReason?: string;
  items: BudgetAllocationFeedItem[];
};

function mapAllocation(
  payload: BudgetAllocationSummary,
): BudgetAllocationFeedItem {
  return {
    id: payload.id,
    parentAllocationId: payload.parent_allocation_id,
    projectId: payload.project_id,
    scopeType: payload.scope_type,
    scopeId: payload.scope_id,
    amountUsdMicros: payload.amount_usd_micros,
    childAllocatedUsdMicros: payload.child_allocated_usd_micros,
    reservedUsdMicros: payload.reserved_usd_micros,
    actualUsdMicros: payload.actual_usd_micros,
    availableUsdMicros: payload.available_usd_micros,
    periodStart: payload.period_start,
    periodEnd: payload.period_end,
    status: payload.status,
    createdAt: payload.created_at,
  };
}

function emptyBudgetAllocationFeed(
  reason?: string,
  projectId = PROJECT_ID,
): BudgetAllocationFeed {
  return {
    projectId: resolveProjectId(projectId),
    apiReachable: false,
    blockedReason: reason,
    items: [],
  };
}

export async function fetchBudgetAllocationFeed(
  projectId = PROJECT_ID,
): Promise<BudgetAllocationFeed> {
  try {
    const payload = await client.listBudgetAllocations(resolveProjectId(projectId));
    return {
      projectId: payload.project_id,
      apiReachable: true,
      items: payload.items.map(mapAllocation),
    };
  } catch (error) {
    if (error instanceof BossApiError) {
      return emptyBudgetAllocationFeed(`API returned ${error.status}`, projectId);
    }
    return emptyBudgetAllocationFeed(
      error instanceof Error ? error.message : "Unknown API error",
      projectId,
    );
  }
}
