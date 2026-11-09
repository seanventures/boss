// @vitest-environment happy-dom
/**
 * Tester round 2 (px-r4), screen 04 and X-75, rendered on the real ProjectPage:
 *  04-15 the cost tile always carries its delta (or says "first week") and the "% of cap" line,
 *        on a subscription week too (the list-price delta, "0% of $50 cap");
 *  04-34 the crew row ends with Marshal "Ranks" (its own tooltip); every label and its tooltip
 *        read the ONE verb the engine sends (Beacon "Deploys" both places);
 *  X-75  one time basis for a piece of work: a finished item is placed by when it finished
 *        (workWhen), the same on Work rows and Overview's Recent work.
 */
import React, { act } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(window.location.search),
  usePathname: () => "/",
}));

import { ProjectPage } from "../app/projects/project-page";
import { workWhen } from "../lib/project-experience";
import { FX_DEPLOYMENTS, FX_HEALTH, FX_KNOWLEDGE, FX_ONBOARDING, FX_OVERVIEW, FX_ROADMAP, FX_SETTINGS, FX_SUMMARY, FX_WORK } from "../lib/project-experience-fixtures";
import { mountAs, stubFetch, tid, unmount } from "./_helpers/multirepo-fixtures";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const P = "helpdesk-ai-demo";
const H = 3_600_000;
const settle = async (n = 10) => { for (let i = 0; i < n; i++) await act(async () => { await new Promise((r) => setTimeout(r, 0)); }); };
const ago = (ms: number) => new Date(Date.now() - ms).toISOString();

function serve(overview: unknown, work: unknown = FX_WORK) {
  return stubFetch((c) => {
    if (c.method !== "GET") return { body: { ok: true } };
    if (c.path === "/v1/projects/summary") return { body: FX_SUMMARY };
    const m = c.path.match(/^\/v1\/projects\/([^/]+)\/(overview|work|knowledge|deployments|onboarding|settings|health|captain)$/);
    if (m) {
      const table: Record<string, unknown> = { overview, work, knowledge: FX_KNOWLEDGE, deployments: FX_DEPLOYMENTS,
        onboarding: { ...FX_ONBOARDING, state: "ready" }, settings: FX_SETTINGS, health: FX_HEALTH };
      return { body: table[m[2]] };
    }
    if (c.path === "/v1/ambient/roadmap") return { body: FX_ROADMAP };
    return undefined;
  });
}

async function open(q = "") {
  window.history.replaceState(null, "", `/?org=org_1&project_id=${P}&area=project${q}`);
  await mountAs(<ProjectPage projectId={P} actor="ceo" orgId="org_1" />, "owner");
  await settle();
}

beforeEach(() => { window.history.replaceState(null, "", "/"); });
afterEach(() => { unmount(); });

describe("04-15 the cost tile: delta and % of cap, always", () => {
  it("money week: the delta tag and the engine's cap %", async () => {
    serve({ ...FX_OVERVIEW, tiles: { ...FX_OVERVIEW.tiles, cost_week: { usd: 14.2, delta_pct: -18, cap_usd: 50, cap_pct: 28, series: [2, 1, 3, 2, 2, 2, 1], href: "?area=budget" } } });
    await open();
    const t = tid("px-tile-cost")!;
    expect(t.textContent).toContain("$14.20");
    expect(t.textContent).toContain("−18%");
    expect([...t.querySelectorAll(".px-tag[data-tone='green']")].map((g) => g.textContent)).toContain("−18%");
    expect(tid("px-tile-cost-cap")!.textContent).toBe("28% of $50 cap");
  });

  it("a first week says so instead of dropping the delta", async () => {
    serve({ ...FX_OVERVIEW, tiles: { ...FX_OVERVIEW.tiles, cost_week: { usd: 3, delta_pct: null, first_week: true, cap_usd: null, series: [0, 0, 0, 0, 0, 0, 3], href: "?area=budget" } } });
    await open();
    expect(tid("px-tile-cost")!.textContent).toContain("first week");
    expect(tid("px-tile-cost-cap")!.textContent).toBe("No weekly cap set");
  });

  it("a subscription week carries the list-price delta and 0% of the cap", async () => {
    serve({ ...FX_OVERVIEW, tiles: { ...FX_OVERVIEW.tiles, cost_week: { usd: 0, delta_pct: null, list_delta_pct: 12, cap_usd: 50, series: [0, 0, 0, 0, 0, 0, 0],
      on_subscription: true, list_usd: 57.55, list_series: [0, 0, 0, 12, 20, 15.5, 10.05], href: "?area=budget" } } });
    await open();
    const t = tid("px-tile-cost")!;
    expect(t.textContent).toContain("On subscription");
    expect(t.textContent).toContain("+12%");
    expect(t.textContent).toContain("0% of $50 cap");
  });
});

describe("04-34 the crew row", () => {
  it("ends with Marshal Ranks, and each label matches its tooltip", async () => {
    serve(FX_OVERVIEW);
    await open();
    const crew = tid("px-ov-crew")!;
    const names = [...crew.querySelectorAll("a")].map((a) => a.textContent);
    expect(names).toEqual(["JunoPlans", "ForgeBuilds", "VeraTests", "BeaconDeploys", "MarshalRanks"]);
    expect(tid("px-ov-crew-chief_of_staff")!.getAttribute("title")).toBe("Chief of Staff ranks this project's backlog against the others");
    expect(tid("px-ov-crew-sre")!.getAttribute("title")).toBe("Beacon deploys — opens Agent Studio");
    expect(tid("px-ov-crew-sre")!.getAttribute("href")).toContain("area=workforce&role=sre");
  });
});

describe("X-75 one time basis for a piece of work", () => {
  it("workWhen: finished work by when it finished, open work by its age", () => {
    const now = Date.parse("2026-11-06T12:00:00Z");
    expect(workWhen({ state: "done", age: "4d", created_at: "2026-11-02T12:00:00Z", updated_at: "2026-11-06T00:00:00Z" }, now)).toBe("12h");
    expect(workWhen({ state: "failed", age: "4d", updated_at: "2026-11-06T11:40:00Z" }, now)).toBe("20m");
    expect(workWhen({ state: "running", age: "12m", updated_at: "2026-11-06T11:59:00Z" }, now)).toBe("12m");
    expect(workWhen({ state: "done", age: "9d", updated_at: "2026-11-03T12:00:00Z" }, now)).toMatch(/^(Mon|Tue|Wed|Thu|Fri|Sat|Sun)$/);
    expect(workWhen({ state: "done", age: "30d", updated_at: "2026-10-01T12:00:00Z" }, now)).toMatch(/^Oct \d+$/);
  });

  it("Recent work and the Work list show the same time for the same finished item", async () => {
    const done = { ...FX_WORK.items.find((w: { state: string }) => w.state === "done")!, age: "4d", created_at: ago(96 * H), updated_at: ago(12 * H) };
    serve({ ...FX_OVERVIEW, recent_work: [done] }, { ...FX_WORK, items: [done] });
    await open();
    expect(tid(`px-ov-work-${done.id}`)!.textContent).toContain("12h");
    expect(tid(`px-ov-work-${done.id}`)!.textContent).not.toContain("4d");
  });
});
