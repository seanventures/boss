// @vitest-environment happy-dom
/**
 * Govern for a preview team: a team budget and the "$2,000 pilot" preset (BOSSAPP-1295), this
 * month's forecast with the costliest runs, and Pilot metrics — before/after, sources, export and
 * the typed-in baseline (BOSSAPP-1292). Every button carries an icon and a console button class.
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ForecastCard, forecastBadge, runHref, type Forecast } from "../app/govern/forecast-card";
import { PilotMetrics, pilotQuery, versus, type PilotReport } from "../app/govern/pilot-metrics";
import { ScopedBudgetsCard, describeScope, type ScopedBudget } from "../app/govern/scoped-budgets";
import { $, mount, settle, unmount } from "./_helpers/live-render";

afterEach(() => { unmount(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

const TEAMS = { teams: [{ id: "grp_1", key: "preview", name: "Preview team", members: 12, projects: ["p1"] }],
  presets: [{ key: "pilot_2000", label: "$2,000 pilot" }] };
const json = (b: unknown, status = 200) => new Response(JSON.stringify(b), { status, headers: { "Content-Type": "application/json" } });
const $$ = (sel: string) => [...document.querySelectorAll<HTMLElement>(sel)];

function set(el: HTMLInputElement | HTMLSelectElement, value: string) {
  const proto = el instanceof HTMLSelectElement ? HTMLSelectElement.prototype : HTMLInputElement.prototype;
  Object.getOwnPropertyDescriptor(proto, "value")!.set!.call(el, value);
  el.dispatchEvent(new Event(el instanceof HTMLSelectElement ? "change" : "input", { bubbles: true }));
}

/** Every button and button-styled link in the card: a console class and an icon. */
function expectStyledButtons(root: HTMLElement | null) {
  const buttons = [...(root?.querySelectorAll<HTMLElement>("button, a.ag-btn") ?? [])]
    .filter((b) => !b.closest(".ag-seg"));
  expect(buttons.length).toBeGreaterThan(0);
  for (const b of buttons) {
    expect(b.className, b.textContent ?? "").toMatch(/\bag-btn\b/);
    expect(b.querySelector("svg"), `${b.textContent} has no icon`).toBeTruthy();
  }
}

describe("team budgets and the pilot preset", () => {
  it("names a team budget", () => {
    const p: ScopedBudget = { id: "sbp_t", scope_type: "team", scope_id: "grp_1", project_id: null, period: "month",
      cap_usd_micros: 2_000_000_000, warn_pct: 0.8, hard_stop: true, scope_name: "Preview team" };
    expect(describeScope(p)).toBe("Team Preview team");
  });

  it("adds the $2,000 pilot preset for a picked team", async () => {
    const posts: Record<string, unknown>[] = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      const method = (init?.method ?? "GET").toUpperCase();
      if (method === "POST") { posts.push(JSON.parse(String(init?.body ?? "{}"))); return json({ id: "sbp_2" }, 201); }
      if (String(url).startsWith("/v1/budget-policies/scoped/teams")) return json(TEAMS);
      if (String(url).startsWith("/v1/budget-policies/scoped")) return json({ policies: [] });
      return json({ items: [] });
    }));
    await mount(<ScopedBudgetsCard projectId="p1" actor="ceo" />);
    set($('[data-testid="gv-scoped-scope"]') as HTMLSelectElement, "team");
    await settle();
    expect(($('[data-testid="gv-scoped-preset"]') as HTMLButtonElement).disabled).toBe(true);
    set($('[data-testid="gv-scoped-target"]') as HTMLSelectElement, "grp_1");
    await settle();
    ($('[data-testid="gv-scoped-preset"]') as HTMLButtonElement).click();
    await settle();
    expect(posts).toEqual([expect.objectContaining({ project_id: "p1", preset: "pilot_2000", scope_type: "team", scope_id: "grp_1" })]);
    expectStyledButtons($('[data-testid="gv-scoped-budgets"]'));
  });
});

const FORECAST: Forecast = {
  scope_type: "team", scope_id: "grp_1", label: "Team Preview team", period_start: "2026-11-01T00:00:00+00:00",
  period_end: "2026-12-01T00:00:00+00:00", days_elapsed: 24.5, days_left: 5.5, spent_usd_micros: 1_500_000_000,
  run_rate_usd_micros_per_day: 61_000_000, forecast_usd_micros: 1_835_000_000, cap_usd_micros: 2_000_000_000, warn_pct: 0.8,
  hard_stop: true, state: "near", daily: [{ day: "2026-11-03", money_usd_micros: 40_000_000 }],
  by_project: [{ id: "p1", name: "Gateway", money_usd_micros: 1_500_000_000 }], run_count: 7, avg_run_usd_micros: 12_000_000,
  runs: Array.from({ length: 7 }, (_, i) => ({ run_id: `ctr_${i}`, kind: i === 6 ? "agent" as const : "build" as const,
    project_id: "p1", title: i === 0 ? "Onboard payments API" : null, money_usd_micros: (20 - i) * 1_000_000, calls: 3,
    started_at: null })),
};

describe("this month's forecast", () => {
  it("says spent, forecast, cap and the costliest runs for a team", async () => {
    const urls: string[] = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string) => {
      urls.push(String(url));
      if (String(url).includes("/teams")) return json(TEAMS);
      return json(String(url).includes("scope_type=team") ? FORECAST : { ...FORECAST, scope_type: "org", cap_usd_micros: null, state: "no_cap" });
    }));
    await mount(<ForecastCard projectId="p1" />);
    expect($('[data-testid="gv-fc-cap"]')?.textContent).toBe("None");
    set($('[data-testid="gv-fc-scope"]') as HTMLSelectElement, "team:grp_1");
    await settle();
    expect(urls.some((u) => u.includes("scope_type=team&scope_id=grp_1"))).toBe(true);
    expect($('[data-testid="gv-fc-spent"]')?.textContent).toBe("$1,500");
    expect($('[data-testid="gv-fc-forecast"]')?.textContent).toBe("$1,835");
    expect($('[data-testid="gv-fc-cap"]')?.textContent).toBe("$2,000");
    expect($('[data-testid="gv-fc-badge"]')?.getAttribute("data-state")).toBe("yellow");
    expect($('[data-testid="gv-fc-runs-line"]')?.textContent).toBe("7 runs, avg $12.00");
    expect($$('[data-testid="gv-fc-run"]')).toHaveLength(5);
    expect($('[data-testid="gv-fc-run"] a')?.getAttribute("href")).toBe("/?area=code_tasks&run=ctr_0");
    ($('[data-testid="gv-fc-more"]') as HTMLButtonElement).click();
    await settle();
    expect($$('[data-testid="gv-fc-run"]')).toHaveLength(7);
    expectStyledButtons($('[data-testid="gv-forecast-card"]'));
  });

  it("badges and run links", () => {
    expect(forecastBadge({ state: "at_cap", forecast_usd_micros: 0 }).tone).toBe("red");
    expect(forecastBadge({ state: "over_forecast", forecast_usd_micros: 2_100_000_000 }).text).toBe("Heading over, $2,100");
    expect(forecastBadge({ state: "no_cap", forecast_usd_micros: 0 }).tone).toBe("unknown");
    expect(runHref({ run_id: "ar_1", kind: "agent" })).toBe("");
  });
});

const REPORT: PilotReport = {
  label: "Whole org", days: 30, since: "2026-10-26T00:00:00Z", until: "2026-11-25T00:00:00Z",
  review: { opened: 3, approved: 2, waiting: 1, median_hours: 4, p90_hours: 6, mean_hours: 4, items: [
    { pull_request_url: "https://example.test/pr/1", run_id: "ctr_a", project_id: "p1", opened_at: "2026-11-20T00:00:00Z", approved_at: "2026-11-20T02:00:00Z", hours: 2 },
    { pull_request_url: "https://example.test/pr/2", run_id: "ctr_b", project_id: "p1", opened_at: "2026-11-21T00:00:00Z", approved_at: "2026-11-21T06:00:00Z", hours: 6 },
    { pull_request_url: null, run_id: "ctr_c", project_id: "p1", opened_at: "2026-11-22T00:00:00Z", approved_at: null, hours: null }] },
  regressions: { blocked_before_merge: 2, stopped_after_merge: 1, total: 3,
    before_merge_items: [{ run_id: "ctr_r", project_id: "p1", title: "Rate limits", at: null, gates_failed: ["tests_passed"] }],
    after_merge_items: [{ kind: "deploy.rolled_back", run_id: "ctr_d", change_set_id: null, project_id: "p1", at: null }] },
  post_deploy: { alerted: 4, notified: 3, auto_remediated: 2, auto_remediated_pct: 50, items: [
    { alert_id: "alr_1", project_id: "p1", alertname: "HighErrorRate", fired_at: null, notified: true, incident_id: "inc_1",
      auto_remediated: true, fix_run_id: "ctr_fix", handled_by_person: false }] },
  baseline: { values: { review_hours_median: 30, auto_remediated_pct: 0 }, note: "team tracker", updated_by: "ceo", updated_at: null },
  after: { review_hours_median: 4, regressions_caught_per_month: 3, post_deploy_alerts_per_month: 4, auto_remediated_pct: 50 },
};

describe("pilot metrics", () => {
  it("shows before and after, opens sources, exports, and saves the baseline", async () => {
    const posts: { url: string; body: Record<string, unknown> }[] = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      if ((init?.method ?? "GET").toUpperCase() === "POST") {
        posts.push({ url: String(url), body: JSON.parse(String(init?.body ?? "{}")) });
        return json({ values: {}, note: "" });
      }
      if (String(url).includes("/teams")) return json(TEAMS);
      return json(REPORT);
    }));
    await mount(<PilotMetrics projectId="p1" actor="ceo" />);
    expect($('[data-testid="gv-pm-review-value"]')?.textContent).toBe("4h");
    expect($('[data-testid="gv-pm-review-vs"]')?.textContent).toBe("-87% vs before");
    expect($('[data-testid="gv-pm-review-vs"]')?.getAttribute("data-state")).toBe("green");
    expect($('[data-testid="gv-pm-regressions-value"]')?.textContent).toBe("3");
    expect($('[data-testid="gv-pm-errors-value"]')?.textContent).toBe("50%");
    expect($('[data-testid="gv-pm-review-chart"]')).toBeTruthy();
    ($('[data-testid="gv-pm-review-items-toggle"]') as HTMLButtonElement).click();
    await settle();
    expect($$('[data-testid="gv-pm-review-items"] a').map((a) => a.getAttribute("href")))
      .toEqual(["/?area=code_tasks&run=ctr_a", "/?area=code_tasks&run=ctr_b", "/?area=code_tasks&run=ctr_c"]);
    ($('[data-testid="gv-pm-err-items-toggle"]') as HTMLButtonElement).click();
    await settle();
    expect($('[data-testid="gv-pm-err-items"] a')?.getAttribute("href")).toBe("/?area=observability&view=alerts&incident=inc_1");
    expect($('[data-testid="gv-pm-export-csv"]')?.getAttribute("href"))
      .toBe("/v1/pilot-metrics/export?project_id=p1&scope_type=org&scope_id=&days=30&format=csv");
    ($('[data-testid="gv-pm-baseline-edit"]') as HTMLButtonElement).click();
    await settle();
    set($('[data-testid="gv-pm-input-post_deploy_alerts_per_month"]') as HTMLInputElement, "12");
    await settle();
    ($('[data-testid="gv-pm-baseline-save"]') as HTMLButtonElement).click();
    await settle();
    expect(posts).toHaveLength(1);
    expect(posts[0].url).toBe("/v1/pilot-metrics/baseline");
    expect(posts[0].body).toMatchObject({ project_id: "p1", note: "team tracker",
      values: { review_hours_median: 30, auto_remediated_pct: 0, post_deploy_alerts_per_month: 12 } });
    expectStyledButtons($('[data-testid="gv-pilot"]'));
  });

  it("compares in the right direction", () => {
    expect(versus("review_hours_median", 10, 5)).toEqual({ text: "-50% vs before", better: true, up: false });
    expect(versus("auto_remediated_pct", 20, 50)).toEqual({ text: "+150% vs before", better: true, up: true });
    expect(versus("post_deploy_alerts_per_month", 4, 8)?.better).toBe(false);
    expect(versus("review_hours_median", undefined, 5)).toBeNull();
    expect(pilotQuery("p1", "team:grp_1", 7)).toBe("project_id=p1&scope_type=team&scope_id=grp_1&days=7");
  });
});
