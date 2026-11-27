// @vitest-environment happy-dom
/**
 * BOSSAPP-782: Govern › Spend renders the per-project Routing Savings card (metered cash vs
 * subscription capacity saved, % calls/tokens routed down, cost per merged change, escalation
 * recovery, and one-page export link).
 */
import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import { SpendTab } from "../app/govern/spend-tab";

let container: HTMLDivElement | null = null;
let root: ReturnType<typeof createRoot> | null = null;

afterEach(() => {
  if (root) {
    act(() => { root?.unmount(); });
    root = null;
  }
  container?.remove();
  container = null;
  vi.restoreAllMocks();
});

describe("Govern › Spend routing savings card", () => {
  it("renders metered vs subscription savings and the one-page export link", async () => {
    const payload = {
      window: "30d",
      money_usd_micros: 12_000_000,
      included_usd_micros: 8_000_000,
      daily: [{ day: "2026-11-26", money_usd_micros: 12_000_000, included_usd_micros: 8_000_000 }],
      daily_cap_usd_micros: 50_000_000,
      scorecard: { calls: 40, ok_calls: 38, failed_calls: 2, cache_ratio: 0.65 },
      savings: [],
      top_drivers: [{ id: "claude-sonnet-5", amount_usd_micros: 12_000_000 }],
      by_agent: [],
      by_model: [],
      spent_vs_cap: {
        period: "month", state: "green", state_label: "Under cap",
        cap_usd_micros: 200_000_000, actual_usd_micros: 12_000_000,
        reserved_usd_micros: 0, committed_usd_micros: 12_000_000,
        available_usd_micros: 188_000_000, utilization_ratio: 0.06,
        active_reservations_count: 0, burn_rate_usd_micros_per_hour: 0,
        burn_window_hours: 24, forecasted_24h_usd_micros: 0,
        time_to_cap_seconds: null, resets_at: "2026-12-01T00:00:00Z",
        resets_at_label: "Dec 1", included_in_subscription_usd_micros: 8_000_000,
        soft_cap_pct: 0.7, auto_lite_mode_pct: 0.85,
        freeze_provisioning_pct: 0.95, freeze_tier2_pct: 1,
        tier2_frozen: false, top_cost_drivers: [], periods: [],
      },
      routing_savings: {
        project_id: "proj_1",
        days: 30,
        baseline_model: "claude-opus-4-6",
        baseline_all_frontier_usd: 42.0,
        actual_list_value_usd: 20.0,
        total_saved_usd: 22.0,
        total_savings_pct: 0.5238,
        metered: { actual_usd: 12.0, baseline_usd: 25.0, dollars_saved_usd: 13.0 },
        subscription: { actual_list_usd: 8.0, baseline_list_usd: 17.0, capacity_saved_usd: 9.0 },
        total_calls: 40,
        routed_down_calls: 28,
        pct_calls_routed_down: 0.7,
        total_tokens: 4_000_000,
        routed_down_tokens: 3_000_000,
        pct_tokens_routed_down: 0.75,
        merged_changes: 5,
        cost_per_merged_change_usd: 4.0,
        baseline_cost_per_merged_change_usd: 8.4,
        escalations: {
          total: 4, rate: 0.1, rescued: 3, recovery_rate: 0.75,
          total_cost_usd: 3.0, cost_per_rescued_build_usd: 1.0,
        },
        review_verdicts_by_tier: [
          { tier: "fast", label: "Fast", approved: 0, changes_requested: 0, total: 0, approval_rate: null },
          { tier: "balanced", label: "Balanced", approved: 2, changes_requested: 0, total: 2, approval_rate: 1.0 },
          { tier: "frontier", label: "Frontier", approved: 8, changes_requested: 2, total: 10, approval_rate: 0.8 },
        ],
      },
    };
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify(payload), { status: 200, headers: { "Content-Type": "application/json" } }),
    );
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
    await act(async () => {
      root!.render(<SpendTab projectId="proj_1" paused={true} />);
    });
    await act(async () => { await Promise.resolve(); });

    const card = container.querySelector('[data-testid="gv-routing-savings"]');
    expect(card).not.toBeNull();
    expect(card!.textContent).toContain("$22.00");
    expect(card!.textContent).toContain("$13.00");
    expect(card!.textContent).toContain("$9.00");
    expect(card!.textContent).toContain("70% calls, 75% tokens");
    expect(card!.textContent).toContain("$4.00");
    const link = container.querySelector('[data-testid="gv-routing-savings-export"]') as HTMLAnchorElement;
    expect(link).not.toBeNull();
    expect(link.getAttribute("href")).toContain("view=routing_savings_export");
    expect(link.getAttribute("href")).toContain("project_id=proj_1");
  });
});
