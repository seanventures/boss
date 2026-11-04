// @vitest-environment happy-dom
/**
 * Govern › Spend (the landing) and Govern › Limits (BOSSAPP-807), rendered from the API's own
 * shapes (GET /v1/govern with `at_limit` and per-period `forecast_usd_micros`, GET
 * /v1/govern/spend?window=month with `by_project`, `by_model_all`, `included_calls`, GET
 * /v1/govern/project-limits). A first-time user answers "how much this month, on which model
 * and project, and what happens at the cap" from Screen A; Screen B saves the caps, the
 * at-limit choice and per-project limits in one Save; every old Govern place stays one click
 * away under More.
 */
import React, { act } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { setConsoleLive } from "../lib/console-live";
import { overBudget } from "./_helpers/copy-budget";
import { fakeGovernApi, governView, newGovern, type FakeGovern } from "./_helpers/govern-fixtures";
import { $, $$, click, mount, settle, unmount } from "./_helpers/live-render";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
vi.mock("../lib/client", () => ({
  API_URL: "https://boss-api.test",
  PROJECT_ID: "p1",
  authHeaders: (extra?: Record<string, string>) => ({ Authorization: "Bearer k", ...(extra ?? {}) }),
  resolveProjectId: (p?: string) => p || "p1",
}));
const { GovernHome } = await import("../app/govern/govern-home");
const { POST } = await import("../app/api/govern/route");
const { badgeOf } = await import("../app/govern/spend-home");

const M = 1_000_000;
const t = (sel: string) => $(sel)?.textContent ?? null;

/** GET /v1/govern as govern_view.build answers it, at $412 of $1,000 this month, $38 of $100 today. */
function view(over: Record<string, unknown> = {}, at: "pause" | "cheaper" | "stop" = "pause") {
  const base = governView() as Record<string, any>;
  const controls = (base.controls as Record<string, any>[]).map((c) =>
    c.key === "monthly_cap" ? { ...c, value_effective: 1000 * M, value_stored: 1000 * M }
      : c.key === "daily_cap" ? { ...c, value_effective: 100 * M, value_stored: 100 * M }
        : c.key === "burst" ? { ...c, value_effective: 2 * M, value_stored: 2 * M } : c);
  return governView({
    controls,
    meter: { ...base.meter, period: "month", cap_usd_micros: 1000 * M, actual_usd_micros: 412 * M,
             committed_usd_micros: 412 * M, utilization_ratio: 0.412,
             periods: [
               { period: "day", cap_usd_micros: 100 * M, actual_usd_micros: 38 * M, reserved_usd_micros: 0,
                 committed_usd_micros: 38 * M, utilization_ratio: 0.38, budget_state: "green",
                 resets_at: "2026-11-05T00:00:00+00:00", resets_at_label: "00:00 UTC", state_label: "Under cap",
                 forecast_usd_micros: 60 * M, spent_usd_micros: 38 * M, held_usd_micros: 0 },
               { period: "month", cap_usd_micros: 1000 * M, actual_usd_micros: 412 * M, reserved_usd_micros: 0,
                 committed_usd_micros: 412 * M, utilization_ratio: 0.412, budget_state: "green",
                 resets_at: "2026-12-01T00:00:00+00:00", resets_at_label: "00:00 UTC on 1 December",
                 state_label: "Under cap", forecast_usd_micros: 860 * M, spent_usd_micros: 412 * M, held_usd_micros: 0 },
             ] },
    at_limit: { value: at, choices: ["pause", "cheaper", "stop"], default: "pause",
                source: at === "pause" ? "default" : "set", version: "unset", changed_by: null },
    ...over,
  });
}

/** GET /v1/govern/spend?window=month as govern_view.spend answers it. */
function spend(meter: unknown) {
  const days = [...Array.from({ length: 26 }, (_, i) => `2026-10-${String(i + 6).padStart(2, "0")}`),
                "2026-11-01", "2026-11-02", "2026-11-03", "2026-11-04"];
  return {
    project_id: "p1", window: "month", money_usd_micros: 412 * M, included_usd_micros: 90 * M,
    metered_calls: 310, included_calls: 1240, not_under_cap_usd_micros: 0,
    daily: days.map((day, i) => ({ day, money_usd_micros: (20 + (i % 9) * 10) * M, included_usd_micros: 0 })),
    daily_cap_usd_micros: 100 * M, scorecard: { calls: 1550 }, savings: [], top_drivers: [],
    by_agent: [{ id: "software_engineer", calls: 300, money_usd_micros: 300 * M, included_usd_micros: 0 }],
    by_model: [{ id: "claude-opus-4-1", calls: 200, money_usd_micros: 300 * M, included_usd_micros: 0 }],
    by_model_spent: [{ id: "claude-opus-4-8[1m]", calls: 200, money_usd_micros: 390 * M, included_usd_micros: 0 },
                     { id: "gpt-4o", calls: 50, money_usd_micros: 20 * M, included_usd_micros: 0 },
                     { id: "other", calls: 0, money_usd_micros: 2 * M, included_usd_micros: 0 },
                     { id: "claude-sonnet-4-5-20250929", calls: 1240, money_usd_micros: 0, included_usd_micros: 90 * M },
                     { id: "codex", calls: 3, money_usd_micros: 0, included_usd_micros: 0 }],
    by_model_all: [{ id: "claude-opus-4-1", calls: 260, money_usd_micros: 390 * M, included_usd_micros: 0 },
                   { id: "gpt-4o", calls: 50, money_usd_micros: 22 * M, included_usd_micros: 0 },
                   { id: "claude-sonnet-4-5-20250929", calls: 1240, money_usd_micros: 0, included_usd_micros: 90 * M }],
    by_project: [{ id: "p1", name: "HelpDesk AI", money_usd_micros: 412 * M, included_usd_micros: 90 * M, calls: 1550 },
                 { id: "p2", name: "Billing", money_usd_micros: 172 * M, included_usd_micros: 0, calls: 650 },
                 { id: "p3", name: "Sandbox", money_usd_micros: 0, included_usd_micros: 0, calls: 0 }],
    spent_vs_cap: meter,
  };
}

const LIMITS = { project_id: "p1", projects: [
  { project_id: "p1", name: "HelpDesk AI", limit_usd_micros: 300 * M, envelope_id: "bua_1", used_usd_micros: 412 * M },
  { project_id: "p2", name: "Billing", limit_usd_micros: null, envelope_id: null, used_usd_micros: 172 * M },
] };

let g: FakeGovern;
let toasts: string[];
const onToast = (e: Event) => toasts.push((e as CustomEvent).detail.message);
beforeEach(() => {
  g = newGovern({ view: view() });
  g.spend = spend((g.view as Record<string, unknown>).meter);
  g.projectLimits = LIMITS;
  vi.stubGlobal("fetch", fakeGovernApi(g));
  setConsoleLive(true);
  toasts = [];
  window.addEventListener("boss:toast", onToast);
  window.history.replaceState({}, "", "/?area=budget");
});
afterEach(() => { unmount(); vi.unstubAllGlobals(); window.removeEventListener("boss:toast", onToast); });

async function setValue(el: Element | null, value: string) {
  await act(async () => {
    const input = el as HTMLInputElement;
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, value);
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
  await settle();
}
const posts = (op: string) => g.calls.filter((c) => c.method === "POST" && c.op === op);
async function wait(ms: number) { await act(async () => { await new Promise((r) => setTimeout(r, ms)); }); await settle(); }

describe("Screen A — Govern › Spend is the landing", () => {
  it("answers how much, of what cap, and how it is going, in the hero", async () => {
    await mount(<GovernHome projectId="p1" actor="ceo" />);
    expect($("[data-testid=gv-tab-overview]")!.getAttribute("aria-selected")).toBe("true");
    expect(t("[data-testid=gv-hero-amount]")).toBe("$412");
    expect(t("[data-testid=gv-hero-of]")).toBe("of $1,000 this month");
    expect(t("[data-testid=gv-hero-badge]")).toBe("On track · forecast $860");
    expect($("[data-testid=gv-hero-badge]")!.getAttribute("data-state")).toBe("green");
    expect($("[data-testid=gv-hero-fill]")!.getAttribute("style")).toContain("41.20%");
    expect($("[data-testid=gv-hero-forecast]")).not.toBeNull();
    expect(t("[data-testid=gv-hero-sub]")).toBe("$38.00 today of $100" + "1,240 subscription calls — included, $0" + "Resets Dec 1");
    // Two reads open it — the limits view and this month's spend — and nothing is written.
    expect(g.calls.map((c) => `${c.method} ${c.url}`)).toEqual([
      "GET /api/govern?project_id=p1&view=limits", "GET /api/govern?project_id=p1&view=spend&window=month"]);
  });

  it("draws each day of the month with today marked and the daily cap named", async () => {
    await mount(<GovernHome projectId="p1" actor="ceo" />);
    const bars = $$("[data-testid=gv-days-chart] .gv-chart-bar");
    expect(bars).toHaveLength(30); // every day of November at its date; days to come are empty
    expect(bars.findIndex((b) => b.hasAttribute("data-today"))).toBe(3);
    expect(bars.slice(4).every((b) => Number(b.getAttribute("height")) === 0)).toBe(true);
    expect(t("[data-testid=gv-days-chart-cap-label]")).toBe("daily cap $100");
    // The cap is a dashed line at its height, drawn even when it is far above every day.
    expect($("[data-testid=gv-days-chart] .gv-chart-cap")).not.toBeNull();
    expect($$("[data-testid=gv-days-axis] span").map((e) => e.textContent)).toEqual(["Nov 1", "Nov 15", "Nov 30"]);
    expect($("[data-testid=gv-days-chart]")!.getAttribute("aria-label")).toBe("30 days of spend");
    await click($("[data-testid=gv-period-week]"));
    expect($$("[data-testid=gv-days-chart] .gv-chart-bar")).toHaveLength(7);
    expect($$("[data-testid=gv-days-axis] span").map((e) => e.textContent)).toEqual(["Oct 29", "Nov 1", "Nov 4"]);
  });

  it("says spend by model and by project, across projects, and opens the detail one click away", async () => {
    await mount(<GovernHome projectId="p1" actor="ceo" />);
    const rows = (id: string) => $$(`[data-testid=${id}-row]`).map((li) => [li.children[0].textContent, li.children[2].textContent]);
    // This project's models add up to the hero ($412); the context size reads (1M); no Unknown;
    // a model with nothing spent or included is folded into a count.
    expect(rows("gv-home-models")).toEqual([["Claude Opus 4.8 (1M)", "$390"], ["GPT-4o", "$20.00"],
                                            ["Other spend", "$2.00"], ["Claude Sonnet 4.5", "Included"]]);
    expect(t("[data-testid=gv-home-models-quiet]")).toBe("+1 with no spend");
    expect(rows("gv-home-projects")).toEqual([["HelpDesk AI", "$412"], ["Billing", "$172"]]);
    expect(t("[data-testid=gv-hero-scope]")).toBe("This project · HelpDesk AI");
    await click($("[data-testid=gv-where]"));
    expect($("[data-testid=gv-spend]")).not.toBeNull();
    expect($("[data-testid=gv-by-agent]")).not.toBeNull();
    expect(new URLSearchParams(window.location.search).get("tab")).toBe("spend");
  });

  it("the period switch reads that window; Today is today's cap", async () => {
    await mount(<GovernHome projectId="p1" actor="ceo" />);
    await click($("[data-testid=gv-period-today]"));
    expect(g.calls.some((c) => c.url === "/api/govern?project_id=p1&view=spend&window=day")).toBe(true);
    expect($$("[data-testid=gv-days-chart] .gv-chart-bar")).toHaveLength(1);
    expect(t("[data-testid=gv-hero-amount]")).toBe("$38.00");
    expect(t("[data-testid=gv-hero-of]")).toBe("of $100 today");
    await click($("[data-testid=gv-period-week]"));
    expect(g.calls.some((c) => c.url === "/api/govern?project_id=p1&view=spend&window=7d")).toBe(true);
    expect(t("[data-testid=gv-hero-of]")).toBe("this week");
  });

  it("one figure: hero, today, By project, By model and Limits' used agree; holds are said apart", async () => {
    const v = view() as Record<string, any>;
    // Holds are active reservations: the same $40 in today's and this month's cap.
    v.meter.periods = v.meter.periods.map((p: any) => ({ ...p, held_usd_micros: 40 * M, reserved_usd_micros: 40 * M,
                                                         committed_usd_micros: p.spent_usd_micros + 40 * M }));
    g.view = v;
    await mount(<GovernHome projectId="p1" actor="ceo" />);
    const hero = t("[data-testid=gv-hero-amount]");
    const mine = $$("[data-testid=gv-home-projects-row]").find((li) => li.hasAttribute("data-mine"))!;
    expect(mine.children[2].textContent).toBe(hero);
    const models = (g.spend as any).by_model_spent.reduce((n: number, r: any) => n + r.money_usd_micros, 0);
    expect(`$${models / M}`).toBe(hero);
    expect(t("[data-testid=gv-hero-held-line]")).toBe("$40.00 held");
    await click($("[data-testid=gv-tab-limits]"));
    expect(t("[data-testid=gv-hint-month]")).toBe(`Used ${hero} · resets Dec 1`);
    expect($("[data-testid=gv-proj-p1]")!.textContent).toContain(`${hero} used`);
  });

  it("the badge says near the cap, and at the cap what the at-limit choice does", () => {
    const v = view() as Record<string, any>;
    const month = v.meter.periods[1];
    expect(badgeOf(v.meter, { ...month, forecast_usd_micros: 1200 * M }, "pause")).toEqual({ tone: "yellow", text: "Near the cap" });
    const at = { ...v.meter, periods: [v.meter.periods[0], { ...month, actual_usd_micros: 1000 * M, spent_usd_micros: 1000 * M }] };
    expect(badgeOf(at, at.periods[1], "pause").text).toBe("At the cap — new work paused");
    expect(badgeOf(at, at.periods[1], "cheaper").text).toBe("At the cap — cheaper models");
    expect(badgeOf(at, at.periods[1], "stop")).toEqual({ tone: "red", text: "At the cap — stopped" });
  });

  it("keeps every old Govern place one click away under More", async () => {
    await mount(<GovernHome projectId="p1" actor="ceo" />);
    const more = $("[data-testid=gv-more]")!;
    for (const id of ["gv-tab-spend", "gv-tab-board", "gv-tab-envelopes", "gv-tab-sharing", "gv-tab-selffix", "gv-changes", "gv-stopped"]) {
      expect(more.querySelector(`[data-testid=${id}]`), id).not.toBeNull();
    }
    await click($("[data-testid=gv-tab-board]"));
    for (const key of ["daily_cap", "monthly_cap", "burst", "pace", "code_agent_max_concurrency", "code_agent_max_turns",
                       "code_review_inrun_max_rounds", "captain_leads_projects", "captain_auto_improve"]) {
      expect($(`[data-testid=gv-control-${key}]`), key).not.toBeNull();
    }
    expect($$("[data-testid^=gv-threshold-]")).toHaveLength(4);
    expect($("[data-testid=gv-control-token_per_step]")).not.toBeNull();
    expect($("[data-testid=gv-meter]")).not.toBeNull();
  });

  it("stays short: no paragraph, no run of copy over six words", async () => {
    await mount(<GovernHome projectId="p1" actor="ceo" />);
    expect(overBudget($("[data-testid=gv]")!)).toEqual([]);
    expect($$("[data-testid=gv] p")).toHaveLength(0);
  });
});

describe("Screen B — Govern › Limits", () => {
  async function openLimits() {
    await mount(<GovernHome projectId="p1" actor="ceo" />);
    await click($("[data-testid=gv-tab-limits]"));
  }

  it("shows the three caps with one hint each, the choice, and each project's limit", async () => {
    await openLimits();
    const value = (id: string) => ($(`[data-testid=${id}]`) as HTMLInputElement).value;
    expect([value("gv-input-month"), value("gv-input-day"), value("gv-input-burst")]).toEqual(["1000", "100", "2"]);
    expect(t("[data-testid=gv-hint-month]")).toBe("Used $412 · resets Dec 1");
    expect(t("[data-testid=gv-hint-day]")).toBe("Used $38.00 today");
    expect(t("[data-testid=gv-hint-burst]")).toBe("Stops a runaway loop fast");
    expect(($("[data-testid=gv-atlimit-pause]") as HTMLInputElement).checked).toBe(true);
    expect($$("[data-testid=gv-atlimit] .gv-radio-label").map((e) => e.textContent)).toEqual(
      ["Pause new work and ask me", "Switch to cheaper models", "Stop everything"]);
    expect(value("gv-proj-input-p1")).toBe("300");
    expect(value("gv-proj-input-p2")).toBe("");
    expect(($("[data-testid=gv-proj-input-p2]") as HTMLInputElement).placeholder).toBe("No limit");
    expect($("[data-testid=gv-proj-p1]")!.textContent).toContain("$412 used");
    await wait(350);
    expect(t("[data-testid=gv-paused-days]")).toBe("With these limits, last month would have paused work on 3 days");
    expect(($("[data-testid=gv-save-limits]") as HTMLButtonElement).disabled).toBe(true); // nothing changed yet
    expect(overBudget($("[data-testid=gv]")!)).toEqual([]);
  });

  it("one Save sends only what changed, with the version shown, then each changed project", async () => {
    await openLimits();
    await setValue($("[data-testid=gv-input-day]"), "80");
    await click($("[data-testid=gv-atlimit-cheaper]"));
    await setValue($("[data-testid=gv-proj-input-p1]"), "");
    await setValue($("[data-testid=gv-proj-input-p2]"), "150");
    await click($("[data-testid=gv-save-limits]"));
    expect(posts("limits").map((c) => [c.body, c.ifMatch])).toEqual([
      [{ op: "limits", project_id: "p1", actor: "ceo", daily_cap_usd: 80, at_limit: "cheaper" }, "7"]]);
    expect(posts("project_limit").map((c) => c.body)).toEqual([
      { op: "project_limit", project_id: "p1", actor: "ceo", monthly_limit_usd: null },
      { op: "project_limit", project_id: "p2", actor: "ceo", monthly_limit_usd: 150 }]);
    expect(toasts).toContain("Limits saved");
  });

  it("Pace is the first card: moving it fills the caps below, which can be fine-tuned, and Save writes both", async () => {
    await openLimits();
    expect($("[data-testid=gv-limits] .gv-card")!.getAttribute("data-testid")).toBe("gv-pace-card");
    const slider = $("[data-testid=gv-limits-pace]") as HTMLInputElement;
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(slider, "60");
      slider.dispatchEvent(new Event("input", { bubbles: true }));
      slider.dispatchEvent(new Event("change", { bubbles: true }));
    });
    await wait(400);
    expect(t("[data-testid=gv-limits-pace-sets]")).toBe("Sets $5,040/month · $252/day · $5.25/min burst · 11 jobs · 15 workers");
    const value = (id: string) => ($(`[data-testid=${id}]`) as HTMLInputElement).value;
    expect([value("gv-input-month"), value("gv-input-day"), value("gv-input-burst")]).toEqual(["5040", "252", "5.25"]);
    // The 30-day replay follows the caps Pace set.
    await wait(400);
    expect(posts("cap_preview").some((c) => c.body?.cap_usd === 5040)).toBe(true);
    await setValue($("[data-testid=gv-input-day]"), "200");
    await click($("[data-testid=gv-save-limits]"));
    expect(posts("pace").map((c) => [c.body, c.ifMatch])).toEqual([
      [{ op: "pace", project_id: "p1", actor: "ceo", intensity: 60 }, "7"]]);
    expect(posts("limits").map((c) => [c.body, c.ifMatch ?? null])).toEqual([
      [{ op: "limits", project_id: "p1", actor: "ceo", daily_cap_usd: 200 }, null]]);
  });

  it("a bad number says why and cannot be saved", async () => {
    await openLimits();
    await setValue($("[data-testid=gv-input-month]"), "0");
    expect(t("[data-testid=gv-hint-month]")).toBe("$0.01–$1,000,000");
    expect(($("[data-testid=gv-save-limits]") as HTMLButtonElement).disabled).toBe(true);
    expect(posts("limits")).toHaveLength(0);
  });

  it("a stale save says who changed it", async () => {
    g.writeStatus = 412;
    g.writeBody = { error: "Changed by ana just now", refusal: { changed_by: "ana" } };
    await openLimits();
    await setValue($("[data-testid=gv-input-month]"), "900");
    await click($("[data-testid=gv-save-limits]"));
    expect(t("[data-testid=gv-fail]")).toContain("Changed by ana just now");
  });

  it("Advanced opens today's board; ← Spend goes back; the red banner lands on the monthly cap", async () => {
    await openLimits();
    await click($("[data-testid=gv-advanced]"));
    expect($("[data-testid=gv-board]")).not.toBeNull();
    await click($("[data-testid=gv-tab-limits]"));
    await click($("[data-testid=gv-back]"));
    expect($("[data-testid=gv-home]")).not.toBeNull();
    unmount();
    g.view = view({ meter: { ...(view().meter as object), state: "red", state_label: "Frozen" } });
    vi.stubGlobal("fetch", fakeGovernApi(g));
    await mount(<GovernHome projectId="p1" actor="ceo" />);
    await click($("[data-testid=gv-frozen] button"));
    expect($("[data-testid=gv-limits]")).not.toBeNull();
    expect(document.activeElement).toBe($("[data-testid=gv-input-month]"));
  });
});

describe("the Advanced board still works from More", () => {
  it("Pace's slider previews and applies; the monthly cap replays the last 30 days", async () => {
    await mount(<GovernHome projectId="p1" actor="ceo" />);
    await click($("[data-testid=gv-tab-board]"));
    await click($("[data-testid=gv-more-pace]"));
    await click($("[data-testid=gv-edit-pace]"));
    await wait(200);
    expect($("[data-testid=gv-pace-slider]")).not.toBeNull();
    expect(t("[data-testid=gv-pace-preview]")).toContain("Daily $252");
    await click($("[data-testid=gv-pace-apply]"));
    expect(posts("pace")).toHaveLength(1);
    await click($("[data-testid=gv-more-monthly_cap]"));
    await click($("[data-testid=gv-edit-monthly_cap]"));
    await wait(350);
    expect(t("[data-testid=gv-would-stop]")).toBe("Would have stopped 3 days");
    expect($("[data-testid=gv-preview-chart]")).not.toBeNull();
  });
});

describe("the route sends the new writes to the engine", () => {
  async function routeSends(op: string, body: Record<string, unknown>, ifMatch?: string) {
    const sent: { url: string; method: string; body: unknown; ifMatch: string | null }[] = [];
    vi.stubGlobal("fetch", vi.fn(async (input: unknown, init?: RequestInit) => {
      sent.push({ url: String(input), method: String(init?.method), body: JSON.parse(String(init?.body ?? "null")),
                  ifMatch: new Headers(init?.headers as HeadersInit).get("If-Match") });
      return Response.json({ ok: true });
    }));
    await POST(new Request("http://console.test/api/govern", {
      method: "POST", headers: { "Content-Type": "application/json", ...(ifMatch ? { "If-Match": ifMatch } : {}) },
      body: JSON.stringify({ op, ...body }),
    }));
    return sent[0];
  }

  it("limits: POST /v1/govern/limits with only the sent fields and the If-Match", async () => {
    const api = await routeSends("limits", { project_id: "p1", actor: "ceo", daily_cap_usd: 80, at_limit: "stop" }, "7");
    expect([api.method, api.url, api.body, api.ifMatch]).toEqual(["POST", "https://boss-api.test/v1/govern/limits",
      { project_id: "p1", actor: "ceo", daily_cap_usd: 80, at_limit: "stop" }, "7"]);
  });

  it("project_limit: POST /v1/govern/project-limit; no limit is null", async () => {
    const api = await routeSends("project_limit", { project_id: "p2", actor: "ceo", monthly_limit_usd: null });
    expect([api.method, api.url, api.body]).toEqual(["POST", "https://boss-api.test/v1/govern/project-limit",
      { project_id: "p2", actor: "ceo", monthly_limit_usd: null }]);
  });
});
