import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

import { ReadinessPanel, type Readiness } from "../app/workflow-readiness-ui";

(globalThis as Record<string, unknown>).React = React;

/**
 * Start says what the budget will do to the run — boss-1, 2026-10-03.
 *
 * A red budget held every non-P0 job, and three research runs sat queued under "no worker has
 * picked it up". Start had said nothing. The engine's preflight now returns the budget as a need
 * when red (with the fix: Budget) and as a note when orange or yellow; this is the form drawing
 * both. And the budget card shows what subscriptions did at list price as "included", never as
 * money and never dropped.
 */

const RED: Readiness = {
  ready: false,
  checked: [],
  missing: [{
    key: "budget", label: "Budget is red — work will be held",
    fix: "Raise the cap in Budget. Calls on a subscription cost no money and do not count "
         + "against it; only metered API spend does.",
    area: "budget", ok: false,
    detail: "Held — the budget is red (95% of $10 used), so only P0 work runs",
    needed_by: [{ step_id: "research", label: "Research" }], first_step_label: "Research",
    after_a_person: false,
  }],
};
RED.checked = RED.missing;

const ORANGE: Readiness = {
  ready: true,
  checked: [{ ...RED.missing[0], key: "model", label: "A connected model", ok: true,
              detail: "a model is connected", area: "setup" }],
  missing: [],
  notes: [{ key: "budget", level: "orange", area: "budget",
            text: "Held — the budget is orange (87% of $10 used), so only P0 and P1 work runs. "
                  + "Raise the cap in Budget to let it run." }],
};

describe("readiness at Start reads the budget", () => {
  it("a red budget is a need, with the fix linked to Budget", () => {
    const html = renderToStaticMarkup(
      <ReadinessPanel readiness={RED} accepted={[]} onAccept={() => {}} />);
    expect(html).toContain("Budget is red — work will be held");
    expect(html).toContain("95% of $10 used");
    expect(html).toMatch(/href="\/\?area=budget"/);
  });

  it("an orange budget is a note beside a ready checklist", () => {
    const html = renderToStaticMarkup(
      <ReadinessPanel readiness={ORANGE} accepted={[]} onAccept={() => {}} />);
    expect(html).toContain('data-testid="dw-readiness-ok"');
    expect(html).toContain('data-testid="dw-readiness-note-budget"');
    expect(html).toContain("the budget is orange (87% of $10 used)");
  });
});

vi.mock("../lib/client", () => ({
  PROJECT_ID: "boss-dogfood",
  resolveProjectId: (projectId?: string) => projectId?.trim() || "boss-dogfood",
  client: { getBudgetStatus: vi.fn() },
}));

describe("the budget card keeps a subscription's value apart from money", async () => {
  const { client } = await import("../lib/client");
  const { fetchBudgetStatus } = await import("../lib/budget-status");

  it("maps what subscriptions did at list price as included, not spent", async () => {
    vi.mocked(client.getBudgetStatus).mockResolvedValueOnce({
      scope_type: "project", scope_id: "boss-dogfood", budget_state: "green",
      cap_usd_micros: 10_000_000, reserved_usd_micros: 0, actual_usd_micros: 0,
      available_usd_micros: 10_000_000, committed_usd_micros: 0, burn_approx_usd_micros: 0,
      burn_window_hours: 24, burn_rate_usd_micros_per_hour: 0, forecasted_24h_usd_micros: 0,
      time_to_cap_seconds: null, utilization_ratio: 0, active_reservations_count: 0,
      top_cost_drivers: [],
      included_in_subscription_usd_micros: 9_080_000,
      included_cost_drivers: [{ skill_or_model_id: "anthropic:claude-sonnet",
                                amount_usd_micros: 9_080_000 }],
    });
    const card = await fetchBudgetStatus();
    expect(card.actualUsdMicros).toBe(0);
    expect(card.includedInSubscriptionUsdMicros).toBe(9_080_000);
    expect(card.includedCostDrivers[0].skillOrModelId).toBe("anthropic:claude-sonnet");
  });
});
