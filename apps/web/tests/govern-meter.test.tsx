// @vitest-environment happy-dom
/**
 * The meter (spec §4.4.2): committed money and holds against the binding cap, the thresholds
 * drawn at their REAL percentages, the period and its reset said in words, the other period
 * beneath — and what subscriptions ran shown beside the bar, never inside it.
 */
import React from "react";
import { afterEach, describe, expect, it } from "vitest";

import type { Meter } from "../lib/govern";
import { governView } from "./_helpers/govern-fixtures";
import { $, mount, unmount } from "./_helpers/live-render";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const { GovernMeter } = await import("../app/govern/meter");

const METER = governView().meter as Meter;
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
});
