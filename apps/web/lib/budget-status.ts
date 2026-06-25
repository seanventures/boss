import { BossApiError, type BudgetStatusResponse } from "@boss/api-client";
import { API_URL, client, PROJECT_ID, resolveProjectId, authHeaders} from "./client";

// operational_intensity isn't in the typed client yet — read it directly. Returns 0-100 or null.
export async function fetchBudgetIntensity(projectId = PROJECT_ID): Promise<number | null> {
  try {
    const r = await fetch(
      `${API_URL}/v1/budget/status?scope_type=project&scope_id=${encodeURIComponent(resolveProjectId(projectId))}`,
      { cache: "no-store", headers: authHeaders() },
    );
    if (!r.ok) return null;
    const v = (await r.json()).operational_intensity;
    return typeof v === "number" ? v : null;
  } catch {
    return null;
  }
}

export type BudgetDriver = {
  skillOrModelId: string;
  amountUsdMicros: number;
};

export type BudgetStatusCard = {
  scopeType: string;
  scopeId: string;
  budgetState: string;
  capUsdMicros: number;
  reservedUsdMicros: number;
  actualUsdMicros: number;
  availableUsdMicros: number;
  committedUsdMicros: number;
  burnApproxUsdMicros: number;
  burnWindowHours: number;
  burnRateUsdMicrosPerHour: number;
  forecasted24hUsdMicros: number;
  timeToCapSeconds?: number | null;
  utilizationRatio: number;
  activeReservationsCount: number;
  topCostDrivers: BudgetDriver[];
  apiReachable: boolean;
  blockedReason?: string;
};

function mapBudgetStatus(payload: BudgetStatusResponse): BudgetStatusCard {
  return {
    scopeType: payload.scope_type,
    scopeId: payload.scope_id,
    budgetState: payload.budget_state,
    capUsdMicros: payload.cap_usd_micros,
    reservedUsdMicros: payload.reserved_usd_micros,
    actualUsdMicros: payload.actual_usd_micros,
    availableUsdMicros: payload.available_usd_micros,
    committedUsdMicros: payload.committed_usd_micros,
    burnApproxUsdMicros: payload.burn_approx_usd_micros,
    burnWindowHours: payload.burn_window_hours,
    burnRateUsdMicrosPerHour: payload.burn_rate_usd_micros_per_hour,
    forecasted24hUsdMicros: payload.forecasted_24h_usd_micros,
    timeToCapSeconds: payload.time_to_cap_seconds,
    utilizationRatio: payload.utilization_ratio,
    activeReservationsCount: payload.active_reservations_count,
    topCostDrivers: payload.top_cost_drivers.map((driver) => ({
      skillOrModelId: driver.skill_or_model_id,
      amountUsdMicros: driver.amount_usd_micros,
    })),
    apiReachable: true,
  };
}

function unavailableBudgetStatus(
  reason: string,
  projectId = PROJECT_ID,
): BudgetStatusCard {
  const resolvedProjectId = resolveProjectId(projectId);
  return {
    scopeType: "project",
    scopeId: resolvedProjectId,
    budgetState: "unknown",
    capUsdMicros: 0,
    reservedUsdMicros: 0,
    actualUsdMicros: 0,
    availableUsdMicros: 0,
    committedUsdMicros: 0,
    burnApproxUsdMicros: 0,
    burnWindowHours: 24,
    burnRateUsdMicrosPerHour: 0,
    forecasted24hUsdMicros: 0,
    timeToCapSeconds: null,
    utilizationRatio: 0,
    activeReservationsCount: 0,
    topCostDrivers: [],
    apiReachable: false,
    blockedReason: reason,
  };
}

export async function fetchBudgetStatus(
  projectId = PROJECT_ID,
): Promise<BudgetStatusCard> {
  try {
    return mapBudgetStatus(
      await client.getBudgetStatus("project", resolveProjectId(projectId)),
    );
  } catch (error) {
    if (error instanceof BossApiError) {
      return unavailableBudgetStatus(`API returned ${error.status}`, projectId);
    }
    return unavailableBudgetStatus(
      error instanceof Error ? error.message : "Unknown API error",
      projectId,
    );
  }
}
