// @vitest-environment happy-dom
/**
 * Govern › Spend keeps what the old card said (D4) and reads like people talk (D15):
 * BY AGENT and BY MODEL with money and Included apart; model ids as names; tooltips within 14
 * words; a daily chart scaled to the spend with a far-above cap named, not flattening the bars.
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { setConsoleLive } from "../lib/console-live";
import { modelName, tip14 } from "../lib/govern";
import { fakeGovernApi, newGovern } from "./_helpers/govern-fixtures";
import { $, $$, click, mount, unmount } from "./_helpers/live-render";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const { GovernHome } = await import("../app/govern/govern-home");
const { DailyBars } = await import("../app/govern/cap-preview");

afterEach(() => { unmount(); vi.unstubAllGlobals(); });

const LONG = "Cache hit ratio is twelve percent across a great many tokens in the last seven days of work by every agent";

function spendView() {
  const g = newGovern();
  g.spend = {
    window: "7d", money_usd_micros: 3_000_000, included_usd_micros: 5_000_000,
    daily: Array.from({ length: 30 }, (_, i) => ({ day: `2026-09-${String(i + 1).padStart(2, "0")}`,
                                                    money_usd_micros: (i % 5) * 1_000_000, included_usd_micros: 0 })),
    daily_cap_usd_micros: 252_000_000,
    scorecard: { calls: 9 },
    savings: [{ kind: "cache_leverage", severity: "medium", finding: LONG, action: LONG }],
    top_drivers: [{ id: "claude-sonnet-4-5-20250929", amount_usd_micros: 3_000_000 }],
    by_agent: [{ id: "software_engineer", calls: 2, money_usd_micros: 2_000_000, included_usd_micros: 5_000_000 },
               { id: "qa_engineer", calls: 1, money_usd_micros: 1_000_000, included_usd_micros: 0 }],
    by_model: [{ id: "claude-opus-4-1", calls: 1, money_usd_micros: 2_000_000, included_usd_micros: 0 },
               { id: "gpt-4o", calls: 2, money_usd_micros: 1_000_000, included_usd_micros: 5_000_000 }],
    spent_vs_cap: g.view.meter,
  };
  return g;
}

async function openSpend() {
  vi.stubGlobal("fetch", fakeGovernApi(spendView()));
  setConsoleLive(true);
  window.history.replaceState({}, "", "/?area=budget");
  await mount(<GovernHome projectId="p1" actor="ceo" />);
  await click($("[data-testid=gv-tab-spend]"));
}

describe("the Spend tab", () => {
  it("says spend by agent and by model, money and Included apart", async () => {
    await openSpend();
    const agents = $$("[data-testid=gv-by-agent] li:not(.gv-spend-head)").map((li) => li.textContent);
    expect(agents[0]).toContain("$2.00");
    expect(agents[0]).toContain("$5.00");
    expect(agents[0]).not.toContain("software_engineer");
    const models = $$("[data-testid=gv-by-model] li:not(.gv-spend-head)").map((li) => li.firstElementChild?.textContent);
    expect(models).toEqual(["Claude Opus 4.1", "GPT-4o"]);
  });

  it("names the top driver, and keeps every tooltip within 14 words", async () => {
    await openSpend();
    expect($("[data-testid=gv-drivers] li span")?.textContent).toBe("Claude Sonnet 4.5");
    for (const el of $$("[data-testid=gv-spend] [title]")) {
      expect(el.getAttribute("title")!.split(/\s+/).length, el.getAttribute("title")!).toBeLessThanOrEqual(14);
    }
  });

  it("scales the daily bars to the spend, naming a cap far above it", async () => {
    await openSpend();
    const bars = $$("[data-testid=gv-daily] .gv-chart-bar").map((b) => Number(b.getAttribute("height")));
    expect(Math.max(...bars)).toBeGreaterThan(40); // the tallest day uses the chart, not 1px of it
    expect($("[data-testid=gv-daily] .gv-chart-cap")).toBeNull();
    expect($("[data-testid=gv-daily-cap-above]")?.textContent).toBe("Cap $252 ↑");
  });
});

describe("helpers", () => {
  it.each([["claude-sonnet-4-5-20250929", "Claude Sonnet 4.5"], ["claude-3-5-haiku-latest", "Claude 3.5 Haiku"],
           ["claude-opus", "Claude Opus"], ["gpt-4o-mini", "GPT-4o-mini"], ["repo_scan", "Repo scan"]])("%s → %s", (id, name) => {
    expect(modelName(id)).toBe(name);
  });
  it("tip14 keeps at most 14 words", () => {
    expect(tip14(LONG)!.split(/\s+/).length).toBeLessThanOrEqual(14);
    expect(tip14("short")).toBe("short");
  });
  it("a cap near the data joins the scale and is drawn as a line", async () => {
    const daily = [{ day: "2026-09-01", money_usd_micros: 9_000_000, included_usd_micros: 0 }];
    await mount(<DailyBars daily={daily} cap={10_000_000} />);
    expect($("[data-testid=gv-daily] .gv-chart-cap")).not.toBeNull();
  });
});
