// @vitest-environment happy-dom
/**
 * The meter (spec §4.4.2): committed money and holds against the binding cap, the thresholds
 * drawn at their REAL percentages, the period and its reset said in words, the other period
 * beneath — and what subscriptions ran shown beside the bar, never inside it.
 */
import React from "react";
import { afterEach, describe, expect, it } from "vitest";

import type { LegacyFinancialHold, LegacyProviderHold, Meter } from "../lib/govern";
import { governView } from "./_helpers/govern-fixtures";
import { $, mount, unmount } from "./_helpers/live-render";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const { GovernMeter } = await import("../app/govern/meter");

const METER = governView().meter as Meter;
const LEGACY_HOLD: LegacyFinancialHold = {
  reservation_id: "old-reservation", lease_id: "old-lease", invocation_id: "unfinished-call", receipt_id: "old-receipt",
  original_reservation_status: "released", original_estimate_usd_micros: 80_000,
  decision_id: "review-old-call", message: "Historical exposure needs reconciliation.",
};
const PROVIDER_HOLD: LegacyProviderHold = {
  reservation_id: "old-provider-reservation", changeset_id: "old-change", operation_id: "old-operation",
  resource_receipt_id: "old-resource-receipt", receipt_id: "old-provider-receipt",
  original_reservation_status: "expired", original_estimate_usd_micros: 70_000,
  decision_id: "review-old-provider", message: "Earlier provider acceptance needs reconciliation.",
};
afterEach(() => unmount());

describe("the meter draws real numbers", () => {
  it("names the period, the amount against the cap, the state and the reset", async () => {
    await mount(<GovernMeter meter={METER} error="" onRetry={() => {}} />);
    expect($("[data-testid=gv-meter-period]")!.textContent).toBe("TODAY");
    expect($("[data-testid=gv-meter-amount]")!.textContent).toBe("$3.52 of $10.00");
    expect($("[data-testid=gv-meter-state]")!.textContent).toBe("Under cap");
    expect($(".gv-reset")!.textContent).toBe("resets 00:00 UTC");
    expect($("[data-testid=gv-meter-month]")!.textContent).toContain("$41.80 of $200");
  });

  it("one hold is a hold", async () => {
    await mount(<GovernMeter meter={{ ...METER, active_reservations_count: 1 } as Meter} error="" onRetry={() => {}} />);
    expect($("[data-testid=gv-meter]")!.textContent).toContain("· 1 hold");
    expect($("[data-testid=gv-meter]")!.textContent).not.toContain("1 holds");
  });

  it("ticks sit at the thresholds' own percentages", async () => {
    await mount(<GovernMeter meter={{ ...METER, soft_cap_pct: 0.6 }} error="" onRetry={() => {}} />);
    expect($("[data-testid=gv-tick-soft_cap_pct]")!.getAttribute("style")).toContain("left: 60.00%");
    expect($("[data-testid=gv-tick-freeze_tier2_pct]")!.getAttribute("style")).toContain("left: 95.00%");
    expect($("[data-testid=gv-tick-auto_lite_mode_pct]")!.getAttribute("title")).toBe("Cheaper builds 85%");
  });

  it("settled money is solid, holds are hatched beside it", async () => {
    await mount(<GovernMeter meter={METER} error="" onRetry={() => {}} />);
    expect($("[data-testid=gv-bar-fill]")!.getAttribute("style")).toContain("width: 31.20%");
    const held = $("[data-testid=gv-bar-held]")!;
    expect(held.getAttribute("style")).toContain("left: 31.20%");
    expect(held.getAttribute("style")).toContain("width: 4.00%");
  });

  it("included in plan is beside the bar, never in it", async () => {
    await mount(<GovernMeter meter={{ ...METER, actual_usd_micros: 0, reserved_usd_micros: 0, committed_usd_micros: 0 }}
                             error="" onRetry={() => {}} />);
    expect($("[data-testid=gv-bar-fill]")!.getAttribute("style")).toContain("width: 0.00%");
    expect($("[data-testid=gv-included]")!.textContent).toBe("Included in plan $12.30");
    expect($("[data-testid=gv-included]")!.getAttribute("title")).toBe("Ran on a subscription. Not money, not counted.");
    expect($("[data-testid=gv-meter-amount]")!.textContent).toBe("$0.00 of $10.00");
  });

  it("the forecast marker appears only when there is a forecast", async () => {
    await mount(<GovernMeter meter={{ ...METER, forecasted_24h_usd_micros: 0 }} error="" onRetry={() => {}} />);
    expect($("[data-testid=gv-forecast]")).toBeNull();
    unmount();
    await mount(<GovernMeter meter={METER} error="" onRetry={() => {}} />);
    expect($("[data-testid=gv-forecast]")!.getAttribute("title")).toBe("Spend over 24h plus reservations, projected");
  });

  it("shows a historical paid-work hold even when settled money and active reservations are zero", async () => {
    await mount(<GovernMeter meter={{ ...METER, actual_usd_micros: 0, reserved_usd_micros: 0, committed_usd_micros: 0,
      active_reservations_count: 0, legacy_financial_hold: LEGACY_HOLD }} error="" onRetry={() => {}} />);
    expect($("[data-testid=gv-meter-state]")!.textContent).toBe("Paid work held");
    expect($("[data-testid=gv-meter-state]")!.getAttribute("data-state")).toBe("held");
    expect($("[data-testid=gv-legacy-hold]")!.textContent).toContain("Earlier estimate $0.08;");
    expect($("[data-testid=gv-legacy-hold]")!.textContent).toContain("actual charge unknown.");
    expect($("[data-testid=gv-legacy-hold]")!.textContent).toContain("A cap reset will not clear this hold.");
    expect($("[data-testid=gv-meter-amount]")!.textContent).toBe("$0.00 of $10.00");
    expect($("[data-testid=gv-bar-fill]")!.getAttribute("style")).toContain("width: 0.00%");
    expect($("[data-testid=gv-bar-held]")).toBeNull();
  });

  it("keeps the reconciliation link in the current organization and names the recorded decision", async () => {
    const previous = window.location.href;
    window.history.replaceState(null, "", "?org=my-org&project=p1&area=budget&run=old-run");
    try {
      await mount(<GovernMeter meter={{ ...METER, legacy_financial_hold: LEGACY_HOLD }} error="" onRetry={() => {}} />);
      const link = $("[data-testid=gv-legacy-hold] a")!;
      const target = new URL(link.getAttribute("href")!, window.location.href);
      expect(target.searchParams.get("org")).toBe("my-org");
      expect(target.searchParams.get("project")).toBe("p1");
      expect(target.searchParams.get("area")).toBe("decisions");
      expect(target.searchParams.get("decision")).toBe(LEGACY_HOLD.decision_id);
      expect(target.searchParams.has("run")).toBe(false);
    } finally { window.history.replaceState(null, "", previous); }
  });

  it("does not offer a made-up decision or a resume action when the hold has no committed decision", async () => {
    await mount(<GovernMeter meter={{ ...METER, legacy_financial_hold: { ...LEGACY_HOLD, decision_id: null } }} error="" onRetry={() => {}} />);
    expect($("[data-testid=gv-legacy-hold]")).not.toBeNull();
    expect($("[data-testid=gv-legacy-hold] a")).toBeNull();
    expect($("[data-testid=gv-legacy-hold] button")).toBeNull();
  });

  it("preserves ordinary cap presentation when there is no historical hold", async () => {
    await mount(<GovernMeter meter={{ ...METER, legacy_financial_hold: null, legacy_provider_hold: null }} error="" onRetry={() => {}} />);
    expect($("[data-testid=gv-legacy-hold]")).toBeNull();
    expect($("[data-testid=gv-legacy-provider-hold]")).toBeNull();
    expect($("[data-testid=gv-meter-state]")!.textContent).toBe("Under cap");
  });

  it("shows an uncertain earlier provider action without inventing settled or reserved money", async () => {
    await mount(<GovernMeter meter={{ ...METER, actual_usd_micros: 0, reserved_usd_micros: 0, committed_usd_micros: 0,
      active_reservations_count: 0, legacy_financial_hold: null, legacy_provider_hold: PROVIDER_HOLD }} error="" onRetry={() => {}} />);
    expect($("[data-testid=gv-meter-state]")!.textContent).toBe("Paid work held");
    expect($("[data-testid=gv-meter-state]")!.getAttribute("data-state")).toBe("held");
    const banner = $("[data-testid=gv-legacy-provider-hold]")!;
    expect(banner.textContent).toContain("An earlier provider action may have been accepted.");
    expect(banner.textContent).toContain("Earlier estimate $0.07;");
    expect(banner.textContent).toContain("actual charge unknown.");
    expect(banner.textContent).toContain("A cap reset will not clear this hold.");
    expect($("[data-testid=gv-legacy-hold]")).toBeNull();
    expect($("[data-testid=gv-meter-amount]")!.textContent).toBe("$0.00 of $10.00");
    expect($("[data-testid=gv-bar-fill]")!.getAttribute("style")).toContain("width: 0.00%");
    expect($("[data-testid=gv-bar-held]")).toBeNull();
  });

  it("opens the provider's recorded decision with the current organization and project", async () => {
    const previous = window.location.href;
    window.history.replaceState(null, "", "?org=my-org&project=p1&area=budget&run=old-run");
    try {
      await mount(<GovernMeter meter={{ ...METER, legacy_provider_hold: PROVIDER_HOLD }} error="" onRetry={() => {}} />);
      const link = $("[data-testid=gv-legacy-provider-hold] a")!;
      const target = new URL(link.getAttribute("href")!, window.location.href);
      expect(target.searchParams.get("org")).toBe("my-org");
      expect(target.searchParams.get("project")).toBe("p1");
      expect(target.searchParams.get("area")).toBe("decisions");
      expect(target.searchParams.get("decision")).toBe(PROVIDER_HOLD.decision_id);
      expect(target.searchParams.has("run")).toBe(false);
      expect(link.textContent).toContain("Review provider attempt");
    } finally { window.history.replaceState(null, "", previous); }
  });

  it("keeps simultaneous model and provider uncertainty separate without adding their estimates to money", async () => {
    await mount(<GovernMeter meter={{ ...METER, legacy_financial_hold: LEGACY_HOLD,
      legacy_provider_hold: PROVIDER_HOLD }} error="" onRetry={() => {}} />);
    expect($("[data-testid=gv-legacy-hold]")!.textContent).toContain("Earlier estimate $0.08;");
    expect($("[data-testid=gv-legacy-provider-hold]")!.textContent).toContain("Earlier estimate $0.07;");
    expect($("[data-testid=gv-legacy-hold] a")!.getAttribute("href")).toContain("decision=review-old-call");
    expect($("[data-testid=gv-legacy-provider-hold] a")!.getAttribute("href")).toContain("decision=review-old-provider");
    expect($("[data-testid=gv-meter-state]")!.textContent).toBe("Paid work held");
    expect($("[data-testid=gv-meter-amount]")!.textContent).toBe("$3.52 of $10.00");
    expect($("[data-testid=gv-bar-fill]")!.getAttribute("style")).toContain("width: 31.20%");
    expect($("[data-testid=gv-bar-held]")!.getAttribute("style")).toContain("width: 4.00%");
    expect(document.querySelectorAll(".gv-legacy-hold button")).toHaveLength(0);
  });

  it("keeps an uncommitted provider hold visible without an invented decision or resume control", async () => {
    await mount(<GovernMeter meter={{ ...METER, legacy_provider_hold: { ...PROVIDER_HOLD, decision_id: null } }}
      error="" onRetry={() => {}} />);
    expect($("[data-testid=gv-legacy-provider-hold]")).not.toBeNull();
    expect($("[data-testid=gv-legacy-provider-hold] a")).toBeNull();
    expect($("[data-testid=gv-legacy-provider-hold] button")).toBeNull();
    expect($("[data-testid=gv-meter-state]")!.textContent).toBe("Paid work held");
  });
});
