import { describe, expect, it, vi } from "vitest";

import { BossApiError } from "@boss/api-client";

vi.mock("../lib/client", () => ({
  PROJECT_ID: "boss-dogfood",
  resolveProjectId: (projectId?: string) => projectId?.trim() || "boss-dogfood",
  client: {
    getBudgetStatus: vi.fn(),
  },
}));

describe("budget status feed", async () => {
  const { client } = await import("../lib/client");
  const { fetchBudgetStatus } = await import("../lib/budget-status");

  it("maps budget status forecast fields into a UI card", async () => {
    vi.mocked(client.getBudgetStatus).mockResolvedValueOnce({
      scope_type: "project",
      scope_id: "boss-dogfood",
      budget_state: "yellow",
      cap_usd_micros: 1_000_000,
      reserved_usd_micros: 100_000,
      actual_usd_micros: 240_000,
      available_usd_micros: 660_000,
      committed_usd_micros: 340_000,
      burn_approx_usd_micros: 340_000,
      burn_window_hours: 24,
      burn_rate_usd_micros_per_hour: 10_000,
      forecasted_24h_usd_micros: 340_000,
      time_to_cap_seconds: 237_600,
      utilization_ratio: 0.34,
      active_reservations_count: 1,
      top_cost_drivers: [
        {
          skill_or_model_id: "gpt-5.2-codex",
          amount_usd_micros: 240_000,
        },
      ],
    });

    const card = await fetchBudgetStatus();

    expect(card.apiReachable).toBe(true);
    expect(card.budgetState).toBe("yellow");
    expect(card.forecasted24hUsdMicros).toBe(340_000);
    expect(card.timeToCapSeconds).toBe(237_600);
    expect(card.topCostDrivers[0]).toEqual({
      skillOrModelId: "gpt-5.2-codex",
      amountUsdMicros: 240_000,
    });
  });

  it("returns an unavailable control-plane state when the API fails", async () => {
    vi.mocked(client.getBudgetStatus).mockRejectedValueOnce(
      new BossApiError("request failed", 503, {}),
    );

    const card = await fetchBudgetStatus();

    expect(card.apiReachable).toBe(false);
    expect(card.scopeId).toBe("boss-dogfood");
    expect(card.blockedReason).toBe("API returned 503");
  });
});
