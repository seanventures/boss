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
  /** Subscription and local-model calls at API list price — "included in your subscription",
   *  never money and never in a cap (engine included_spend.py). */
  includedInSubscriptionUsdMicros: number;
  includedCostDrivers: BudgetDriver[];
  // BOSS-281: the enforced thresholds — shown so operators see REAL knobs
  softCapPct?: number | null;
  autoLiteModePct?: number | null;
  freezeProvisioningPct?: number | null;
  freezeTier2Pct?: number | null;
  tier2Frozen?: boolean | null;
  /** The period the numbers above measure — every cap measures its own (engine
   *  budget.period_spend): "day" or "month", and when it resets ("00:00 UTC"). */
  period: "day" | "month";
  resetsAtLabel: string;
  /** Each capped period, so the card can show the one that does not bind too. */
  periods: BudgetPeriodLine[];
  apiReachable: boolean;
  blockedReason?: string;
};

export type BudgetPeriodLine = {
  period: "day" | "month";
  capUsdMicros: number;
  committedUsdMicros: number;
  utilizationRatio: number;
  budgetState: string;
  resetsAtLabel: string;
};

/** "today" / "this month" — whose money a number is. */
export function periodWords(period: string): { whose: string; when: string; cap: string } {
  return period === "month"
    ? { whose: "this month's", when: "this month", cap: "monthly cap" }
    : { whose: "today's", when: "today", cap: "daily cap" };
}

function asPeriod(value: unknown): "day" | "month" {
  return value === "month" ? "month" : "day";
}

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
    includedInSubscriptionUsdMicros: payload.included_in_subscription_usd_micros ?? 0,
    includedCostDrivers: (payload.included_cost_drivers ?? []).map((driver) => ({
      skillOrModelId: driver.skill_or_model_id,
      amountUsdMicros: driver.amount_usd_micros,
    })),
    softCapPct: (payload as Record<string, unknown>).soft_cap_pct as number | null,
    autoLiteModePct: (payload as Record<string, unknown>).auto_lite_mode_pct as number | null,
    freezeProvisioningPct:
      (payload as Record<string, unknown>).freeze_provisioning_pct as number | null,
    freezeTier2Pct: (payload as Record<string, unknown>).freeze_tier2_pct as number | null,
    tier2Frozen: (payload as Record<string, unknown>).tier2_frozen as boolean | null,
    period: asPeriod(payload.period),
    resetsAtLabel: payload.resets_at_label || "00:00 UTC",
    periods: Object.values(payload.periods ?? {})
      .filter((p) => p.cap_usd_micros > 0)
      .map((p) => ({
        period: asPeriod(p.period),
        capUsdMicros: p.cap_usd_micros,
        committedUsdMicros: p.committed_usd_micros,
        utilizationRatio: p.utilization_ratio,
        budgetState: p.budget_state,
        resetsAtLabel: p.resets_at_label,
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
    includedInSubscriptionUsdMicros: 0,
    includedCostDrivers: [],
    period: "day",
    resetsAtLabel: "00:00 UTC",
    periods: [],
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


// ---- BOSS-281: cost insights + real LLM spend segmentation -------------------

export type CostInsight = {
  kind: string;
  severity: string;
  finding: string;
  action: string;
  potentialSavingUsdMicros: number;
};

export type CostInsightsFeed = {
  totalSpendUsdMicros: number;
  insights: CostInsight[];
  apiReachable: boolean;
};

export async function fetchCostInsights(projectId = PROJECT_ID): Promise<CostInsightsFeed> {
  try {
    const r = await fetch(
      `${API_URL}/v1/budget/insights?project_id=${encodeURIComponent(resolveProjectId(projectId))}`,
      { cache: "no-store", headers: authHeaders() },
    );
    if (!r.ok) return { totalSpendUsdMicros: 0, insights: [], apiReachable: false };
    const d = await r.json();
    return {
      totalSpendUsdMicros: d.total_spend_usd_micros ?? 0,
      insights: (d.insights ?? []).map((i: Record<string, unknown>) => ({
        kind: String(i.kind ?? ""),
        severity: String(i.severity ?? "medium"),
        finding: String(i.finding ?? ""),
        action: String(i.action ?? ""),
        potentialSavingUsdMicros: Number(i.potential_saving_usd_micros ?? 0),
      })),
      apiReachable: true,
    };
  } catch {
    return { totalSpendUsdMicros: 0, insights: [], apiReachable: false };
  }
}

export type LlmRoleSpend = { role: string; calls: number; spendUsdMicros: number; tokens: number };
export type LlmModelSpend = {
  model: string; calls: number; spendUsdMicros: number; estimatedCalls: number;
};
export type LlmSpendFeed = {
  byRole: LlmRoleSpend[];
  byModel: LlmModelSpend[];
  cache: {
    freshInputTokens: number;
    cacheReadTokens: number;
    cacheWriteTokens: number;
    cacheHitRatio: number | null;
  } | null;
  apiReachable: boolean;
};

export async function fetchLlmSpend(projectId = PROJECT_ID): Promise<LlmSpendFeed> {
  try {
    const r = await fetch(
      `${API_URL}/v1/reports/summary?project_id=${encodeURIComponent(resolveProjectId(projectId))}`,
      { cache: "no-store", headers: authHeaders() },
    );
    if (!r.ok) return { byRole: [], byModel: [], cache: null, apiReachable: false };
    const ls = (await r.json()).llm_spend ?? {};
    return {
      byRole: (ls.by_role ?? []).map((x: Record<string, unknown>) => ({
        role: String(x.role ?? ""), calls: Number(x.calls ?? 0),
        spendUsdMicros: Number(x.spend_usd_micros ?? 0), tokens: Number(x.tokens ?? 0),
      })),
      byModel: (ls.by_model ?? []).map((x: Record<string, unknown>) => ({
        model: String(x.model ?? ""), calls: Number(x.calls ?? 0),
        spendUsdMicros: Number(x.spend_usd_micros ?? 0),
        estimatedCalls: Number(x.estimated_calls ?? 0),
      })),
      cache: ls.cache
        ? {
            freshInputTokens: Number(ls.cache.fresh_input_tokens ?? 0),
            cacheReadTokens: Number(ls.cache.cache_read_tokens ?? 0),
            cacheWriteTokens: Number(ls.cache.cache_write_tokens ?? 0),
            cacheHitRatio: ls.cache.cache_hit_ratio ?? null,
          }
        : null,
      apiReachable: true,
    };
  } catch {
    return { byRole: [], byModel: [], cache: null, apiReachable: false };
  }
}
