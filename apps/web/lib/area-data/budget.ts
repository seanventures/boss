/**
 * BOSS-13 area loader: Budget + FinOps surfaces.
 *
 * Covers ?area=budget panels: budget status/intensity/allocations, FinOps
 * summary, commitment reconciliations, cost insights, and LLM spend.
 */

import {
  fetchBudgetAllocationFeed,
  type BudgetAllocationFeed,
} from "../budget-allocations";
import {
  fetchBudgetIntensity,
  fetchBudgetStatus,
  fetchCostInsights,
  fetchLlmSpend,
  type BudgetStatusCard,
  type CostInsightsFeed,
  type LlmSpendFeed,
} from "../budget-status";
import {
  fetchCommitmentReconciliations,
  fetchFinopsSummary,
  type CommitmentReconciliation,
  type FinopsSummary,
} from "../finops";
import { isAreaActive, type AreaLoadOptions } from "./types";

export const BUDGET_AREA_IDS = ["budget", "reports", "infra"] as const;

export type BudgetAreaData = {
  budgetStatus: BudgetStatusCard;
  budgetIntensity: number | null;
  budgetAllocations: BudgetAllocationFeed;
  commitmentRecons: CommitmentReconciliation[];
  finopsSummary: FinopsSummary;
  costInsights: CostInsightsFeed;
  llmSpend: LlmSpendFeed;
  loaded: boolean;
};

function emptyBudgetStatus(projectId: string): BudgetStatusCard {
  return {
    scopeType: "project",
    scopeId: projectId,
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
    blockedReason: "Area data not loaded for inactive console area",
  };
}

function emptyFinops(projectId: string): FinopsSummary {
  return {
    projectId,
    days: 30,
    periodStart: "",
    periodEnd: "",
    spendByProvider: [],
    spendByDay: [],
    anomaliesOpenCount: 0,
    commitmentUtilization: [],
    topResources: [],
    apiReachable: false,
  };
}

function emptyAllocations(projectId: string): BudgetAllocationFeed {
  return {
    projectId,
    apiReachable: false,
    blockedReason: "Area data not loaded for inactive console area",
    items: [],
  };
}

export function shouldLoadBudgetArea(requestedArea: string | undefined): boolean {
  return isAreaActive(requestedArea, BUDGET_AREA_IDS);
}

export async function loadBudgetAreaData(
  options: AreaLoadOptions,
): Promise<BudgetAreaData> {
  const { projectId, active = true } = options;
  if (!active) {
    return {
      budgetStatus: emptyBudgetStatus(projectId),
      budgetIntensity: null,
      budgetAllocations: emptyAllocations(projectId),
      commitmentRecons: [],
      finopsSummary: emptyFinops(projectId),
      costInsights: {
        totalSpendUsdMicros: 0,
        insights: [],
        apiReachable: false,
      },
      llmSpend: {
        byRole: [],
        byModel: [],
        cache: null,
        apiReachable: false,
      },
      loaded: false,
    };
  }

  const [
    budgetStatus,
    budgetIntensity,
    budgetAllocations,
    commitmentRecons,
    finopsSummary,
    costInsights,
    llmSpend,
  ] = await Promise.all([
    fetchBudgetStatus(projectId),
    fetchBudgetIntensity(projectId),
    fetchBudgetAllocationFeed(projectId),
    fetchCommitmentReconciliations(projectId),
    fetchFinopsSummary(projectId, 30),
    fetchCostInsights(projectId),
    fetchLlmSpend(projectId),
  ]);

  return {
    budgetStatus,
    budgetIntensity,
    budgetAllocations,
    commitmentRecons,
    finopsSummary,
    costInsights,
    llmSpend,
    loaded: true,
  };
}
