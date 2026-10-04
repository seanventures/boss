import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

(globalThis as Record<string, unknown>).React = React;

/**
 * Every budget cap measures its own period (engine budget.period_spend). The daily cap was
 * compared with every cost receipt the project ever had, so an install that reached it stayed
 * red for ever. The card now says whose money the numbers are — today's or this month's — and
 * when that period resets, and shows the period that does not bind beside the one that does.
 */

vi.mock("../lib/client", () => ({
  PROJECT_ID: "boss-dogfood",
  resolveProjectId: (projectId?: string) => projectId?.trim() || "boss-dogfood",
  client: { getBudgetStatus: vi.fn() },
}));

const PERIOD = {
  actual_usd_micros: 0, reserved_usd_micros: 0, available_usd_micros: 0,
  period_start: "2031-05-01T00:00:00+00:00", resets_at: "2031-06-01T00:00:00+00:00",
};

describe("the budget card names its period", async () => {
  const { client } = await import("../lib/client");
  const { fetchBudgetStatus } = await import("../lib/budget-status");
  const { BudgetThresholdsCard } = await import("../app/cost-governance-ui");

  it("maps the binding period, its reset and each capped period", async () => {
    vi.mocked(client.getBudgetStatus).mockResolvedValueOnce({
      scope_type: "project", scope_id: "boss-dogfood", budget_state: "red",
      cap_usd_micros: 20_000_000, reserved_usd_micros: 0, actual_usd_micros: 19_000_000,
      available_usd_micros: 1_000_000, committed_usd_micros: 19_000_000,
      burn_approx_usd_micros: 19_000_000, burn_window_hours: 24,
      burn_rate_usd_micros_per_hour: 0, forecasted_24h_usd_micros: 0,
      time_to_cap_seconds: null, utilization_ratio: 0.95, active_reservations_count: 0,
      top_cost_drivers: [],
      period: "month", cap_name: "monthly cap", resets_at_label: "00:00 UTC on 1 June",
      periods: {
        day: { ...PERIOD, period: "day", cap_usd_micros: 100_000_000,
               committed_usd_micros: 3_000_000, utilization_ratio: 0.03,
               budget_state: "green", resets_at_label: "00:00 UTC", cap_name: "daily cap" },
        month: { ...PERIOD, period: "month", cap_usd_micros: 20_000_000,
                 committed_usd_micros: 19_000_000, utilization_ratio: 0.95,
                 budget_state: "red", resets_at_label: "00:00 UTC on 1 June",
                 cap_name: "monthly cap" },
      },
    });
    const card = await fetchBudgetStatus();
    expect(card.period).toBe("month");
    expect(card.resetsAtLabel).toBe("00:00 UTC on 1 June");
    expect(card.periods.map((p) => p.period).sort()).toEqual(["day", "month"]);

    const html = renderToStaticMarkup(<BudgetThresholdsCard status={card} />);
    expect(html).toContain("95% of this month&#x27;s cap");
    expect(html).toContain("resets at 00:00 UTC on 1 June");
  });

  it("an older API without periods reads as today's cap", async () => {
    vi.mocked(client.getBudgetStatus).mockResolvedValueOnce({
      scope_type: "project", scope_id: "boss-dogfood", budget_state: "green",
      cap_usd_micros: 10_000_000, reserved_usd_micros: 0, actual_usd_micros: 0,
      available_usd_micros: 10_000_000, committed_usd_micros: 0, burn_approx_usd_micros: 0,
      burn_window_hours: 24, burn_rate_usd_micros_per_hour: 0, forecasted_24h_usd_micros: 0,
      time_to_cap_seconds: null, utilization_ratio: 0, active_reservations_count: 0,
      top_cost_drivers: [],
    });
    const card = await fetchBudgetStatus();
    expect(card.period).toBe("day");
    expect(card.resetsAtLabel).toBe("00:00 UTC");
    expect(card.periods).toEqual([]);
  });
});
