// @vitest-environment happy-dom
/**
 * Govern's data is the screen's, not the page render's (spec §6.2, RC12).
 *
 * The old Govern was eleven server fetches on every render (one a POST, the budget status read
 * twice) loaded by whatever area the server happened to render — so arriving by the rail after
 * another area showed "FinOps summary unavailable". Mounting Govern now reads `/v1/govern` once,
 * client-side; Spend and Envelopes read only when opened; nothing posts on read; and the page
 * render fetches nothing for it.
 */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { setConsoleLive } from "../lib/console-live";
import { fakeGovernApi, newGovern, type FakeGovern } from "./_helpers/govern-fixtures";
import { $, click, mount, unmount } from "./_helpers/live-render";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const { GovernHome } = await import("../app/govern/govern-home");

let g: FakeGovern;
beforeEach(() => {
  g = newGovern();
  vi.stubGlobal("fetch", fakeGovernApi(g));
  setConsoleLive(true);
  // Arrived by the rail from another area: nothing for Govern was loaded by the render.
  window.history.replaceState({}, "", "/?area=budget");
});
afterEach(() => { unmount(); vi.unstubAllGlobals(); });

describe("Govern reads its own data", () => {
  it("one GET opens it; no POST; no 'not loaded'", async () => {
    await mount(<GovernHome projectId="p1" actor="ceo" />);
    expect(g.calls.map((c) => `${c.method} ${c.url}`)).toEqual(["GET /api/govern?project_id=p1&view=limits"]);
    expect($("[data-testid=gv]")!.textContent).not.toMatch(/not loaded|unavailable/i);
  });

  it("a tab reads only when opened", async () => {
    g.spend = { window: "7d", money_usd_micros: 0, included_usd_micros: 0, daily: [], daily_cap_usd_micros: 0,
                scorecard: { calls: 0 }, savings: [], top_drivers: [], spent_vs_cap: g.view.meter };
    await mount(<GovernHome projectId="p1" actor="ceo" />);
    await click($("[data-testid=gv-tab-spend]"));
    expect(g.calls.map((c) => c.url)).toEqual(["/api/govern?project_id=p1&view=limits",
                                               "/api/govern?project_id=p1&view=spend&window=7d"]);
    expect($("[data-testid=gv-spend-empty]")!.textContent).toBe("No model spend yet");
    expect(new URLSearchParams(window.location.search).get("tab")).toBe("spend");
    expect(g.calls.some((c) => c.method === "POST")).toBe(false);
  });

  it("the page render fetches nothing for Govern", () => {
    const page = readFileSync(resolve(__dirname, "../app/page.tsx"), "utf8");
    for (const gone of ["loadBudgetAreaData", "fetchTokenRecommendations", "fetchBudgetStatus", "fetchFinopsSummary",
                        "fetchCostInsights", "fetchLlmSpend", "fetchBudgetAllocationFeed"]) {
      expect(page).not.toContain(gone);
    }
    expect(page).toContain("<GovernHome projectId={activeProjectId} actor={activeActorId} />");
  });

  it("Spend links spent-vs-cap on to Observability › Models", async () => {
    g.spend = { window: "7d", money_usd_micros: 3_000_000, included_usd_micros: 7_000_000,
                daily: [{ day: "2026-10-04", money_usd_micros: 3_000_000, included_usd_micros: 7_000_000 }],
                daily_cap_usd_micros: 10_000_000, scorecard: { calls: 9, cache_ratio: 0.2, prompt_tokens: 100, cache_read_tokens: 20 },
                savings: [], top_drivers: [{ id: "claude-sonnet", amount_usd_micros: 3_000_000 }], spent_vs_cap: g.view.meter };
    await mount(<GovernHome projectId="p1" actor="ceo" />);
    await click($("[data-testid=gv-tab-spend]"));
    const models = $("[data-testid=gv-models-link] a")!;
    const q = new URLSearchParams(models.getAttribute("href")!.split("?")[1]);
    expect([q.get("area"), q.get("tab")]).toEqual(["observability", "models"]);
    expect($("[data-testid=gv-spend-total]")!.textContent).toBe("$3.00 spent · Included $7.00");
    expect($("[data-testid=gv-cache] .gv-low")!.getAttribute("title")).toBe("Under 30% of input came from cache");
  });

  it("Envelopes keeps the FinOps honesty words: not loaded and no events are never $0", async () => {
    for (const [state, word, tip] of [
      ["not_loaded", "Not loaded · not $0", "Cloud spend is not loaded. This is not $0.00."],
      ["no_events", "No cost events · not $0", "No cost events in this window. Absence is not $0.00 spend."],
    ]) {
      g.envelopes = { allocations: [], finops: { state, summary: state === "not_loaded" ? null : { anomalies_open_count: 0 } },
                      reconciliations: [], initiative_rule: { tier_min: 2, amount_usd_micros: 50_000_000 } };
      await mount(<GovernHome projectId="p1" actor="ceo" />);
      await click($("[data-testid=gv-tab-envelopes]"));
      const honesty = $("[data-testid=gv-finops-honesty]")!;
      expect([honesty.textContent, honesty.getAttribute("title")]).toEqual([word, tip]);
      expect($("[data-testid=gv-init-rule]")!.textContent).toBe("Tier 2+ over $50.00 needs approval");
      expect($("[data-testid=gv-no-envelopes]")!.textContent).toBe("No envelopes");
      unmount();
    }
  });
});
