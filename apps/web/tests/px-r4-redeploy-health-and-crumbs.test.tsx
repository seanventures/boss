// @vitest-environment happy-dom
/**
 * Tester round 2 (BOSSAPP-913/914), on the real ProjectPage with the engine's answers:
 *   08-05  Redeploy opens a confirm sheet (what, where, which commit, who approves) and sends
 *          nothing until confirmed; a refusal ("Paused in Community") shows red, never green.
 *   12-05  the header says the project's health from the one state (Degraded + why), never
 *          "Healthy" beside a breached SLO; the tile says the same word in its colour.
 *   15-03  one p95 with its window ("p95 207ms · 24 h") on the tile, the header and Deployments.
 *   12-09  Health's alert rows use the same 24-hour local clock as its incidents ("09:52").
 *   14-01  a server's drawer in Infra reads "Infra / Servers / <host>".
 */
import React, { act } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(window.location.search),
  usePathname: () => "/",
}));

import { ProjectPage } from "../app/projects/project-page";
import { clockTime } from "../app/projects/tab-health";
import { crumbsFor } from "../app/shell-crumbs";
import { setCrumbTrail, crumbTrail } from "../app/shell-crumb-here";
import { FX_DEPLOYMENTS, FX_HEALTH, FX_KNOWLEDGE, FX_ONBOARDING, FX_OVERVIEW, FX_ROADMAP, FX_SETTINGS, FX_WORK } from "../lib/project-experience-fixtures";
import { mountAs, stubFetch, tid, unmount, type Call } from "./_helpers/multirepo-fixtures";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const P = "helpdesk-ai-demo";
const settle = async (n = 8) => { for (let i = 0; i < n; i++) await act(async () => { await new Promise((r) => setTimeout(r, 0)); }); };
const press = async (el: Element | null) => { if (!el) throw new Error("nothing to press"); await act(async () => { (el as HTMLElement).click(); }); await settle(); };

const OVERVIEW = {
  ...FX_OVERVIEW,
  project: { ...FX_OVERVIEW.project, health: "degraded", health_reason: "SLO breached: 98.13% up, target 99.5%" },
  tiles: { ...FX_OVERVIEW.tiles,
           staging: { health: "degraded", health_reason: "SLO breached: 98.13% up, target 99.5%", uptime_pct: 98.13, p95_ms: 207,
                      p95_source: "probe", window: "24 h", series: [1, 2, 3], label: "Staging", href: "?project_id=helpdesk-ai-demo&area=project&tab=health" } },
};
const DEPLOYMENTS = {
  ...FX_DEPLOYMENTS,
  environments: [{ ...FX_DEPLOYMENTS.environments[0], target_id: "dept_1", app: "HelpDesk AI Demo", host: "203.0.113.20", host_name: "Staging server",
                   health: "degraded", health_reason: "SLO breached: 98.13% up, target 99.5%", p95_ms: 207, p95_source: "probe", window: "24 h", policy: "person",
                   last_deploy: { commit: "f3f130c", at: new Date(Date.now() - 7_200_000).toISOString(), by: "Beacon", label: "PR #4", pr: 4, pr_url: "",
                                  href: "?project_id=helpdesk-ai-demo&area=infra&view=deploys&deploy=stack%3Asdep_4" } }],
};

function engine(deploy: (c: Call) => { status?: number; body: unknown }) {
  return stubFetch((c) => {
    if (c.method === "POST" && c.path === "/v1/infra/targets/dept_1/deploy") return deploy(c);
    if (c.method !== "GET") return { body: { ok: true } };
    const m = c.path.match(/^\/v1\/projects\/([^/]+)\/(overview|work|knowledge|deployments|onboarding|settings|health|captain)$/);
    if (m) {
      const table: Record<string, unknown> = { overview: OVERVIEW, work: FX_WORK, knowledge: FX_KNOWLEDGE, deployments: DEPLOYMENTS,
        onboarding: { ...FX_ONBOARDING, state: "ready" }, settings: FX_SETTINGS, health: FX_HEALTH };
      return { body: table[m[2]] };
    }
    if (c.path === "/v1/ambient/roadmap") return { body: FX_ROADMAP };
    return undefined;
  });
}

async function openProject(q = "") {
  window.history.replaceState(null, "", `/?org=org_1&project_id=${P}&area=project${q}`);
  await mountAs(<ProjectPage projectId={P} actor="ceo" orgId="org_1" />, "owner");
  await settle(10);
}

beforeEach(() => { window.history.replaceState(null, "", "/"); });
afterEach(() => { unmount(); });

describe("08-05 Redeploy asks first, and a refusal is an error", () => {
  it("the sheet says what, where, which commit and who approves; nothing is sent before Redeploy", async () => {
    const calls = engine(() => ({ body: { ok: true, ref: "stack:sdep_9" } }));
    await openProject("&tab=deployments");
    await press(tid("px-env-redeploy-staging"));
    const sheet = tid("px-env-redeploy-sheet-staging")!;
    expect(sheet).not.toBeNull();
    expect(sheet.getAttribute("role")).toBe("dialog");
    expect(tid("px-env-redeploy-sheet-staging-what")!.textContent).toBe("HelpDesk AI Demo on Staging");
    expect(tid("px-env-redeploy-sheet-staging-where")!.textContent).toContain("Staging server");
    expect(tid("px-env-redeploy-sheet-staging-commit")!.textContent).toBe("f3f130c · PR #4");
    expect(tid("px-env-redeploy-sheet-staging-approval")!.textContent).toContain("A person approves it");
    expect(calls.some((c) => c.method === "POST" && c.path.endsWith("/deploy"))).toBe(false);
    await press(tid("px-env-redeploy-sheet-staging-go"));
    const post = calls.find((c) => c.method === "POST" && c.path === "/v1/infra/targets/dept_1/deploy");
    expect((post!.body as { ref: string }).ref).toBe("f3f130c");
    expect(tid("px-env-redeploy-sheet-staging")).toBeNull();
    const said = tid("px-env-staging")!.querySelector("[data-testid='px-said']")!;
    expect(said.textContent).toContain("Redeploy planned");
    expect((said as HTMLElement).style.color).toBe("var(--green)");
  });

  it("Cancel sends nothing", async () => {
    const calls = engine(() => ({ body: { ok: true } }));
    await openProject("&tab=deployments");
    await press(tid("px-env-redeploy-staging"));
    await press([...tid("px-env-redeploy-sheet-staging")!.querySelectorAll("button")].find((b) => b.textContent === "Cancel")!);
    expect(tid("px-env-redeploy-sheet-staging")).toBeNull();
    expect(calls.some((c) => c.method === "POST" && c.path.endsWith("/deploy"))).toBe(false);
  });

  it("a refusal stays in the sheet as a red alert and the card line is red, never green", async () => {
    engine(() => ({ status: 409, body: { detail: { error: "paused", message: "Paused in Community" } } }));
    await openProject("&tab=deployments");
    await press(tid("px-env-redeploy-staging"));
    await press(tid("px-env-redeploy-sheet-staging-go"));
    const err = tid("px-env-redeploy-sheet-staging-error")!;
    expect(err.getAttribute("role")).toBe("alert");
    expect(err.textContent).toContain("Paused in Community");
    const said = tid("px-env-staging")!.querySelector("[data-testid='px-said']") as HTMLElement;
    expect(said.textContent).toContain("Not redeployed");
    expect(said.textContent).not.toContain("planned");
    expect(said.style.color).toBe("var(--red)");
    expect(said.getAttribute("role")).toBe("alert");
  });
});

describe("12-05 / 15-03 one health word and one p95 with its window", () => {
  it("the header says Degraded with why, never Healthy beside a breached SLO; the p95 carries its window", async () => {
    engine(() => ({ body: {} }));
    await openProject();
    const chip = tid("px-head-health")!;
    expect(chip.textContent).toBe("Degraded");
    expect(chip.getAttribute("data-s")).toBe("degraded");
    expect(chip.getAttribute("title")).toContain("SLO breached");
    expect(chip.getAttribute("title")).toContain("p95 207ms · 24 h");
    expect(tid("px-tile-staging-health")!.textContent).toBe("Degraded");
    expect(tid("px-tile-staging-p95")!.textContent).toBe("p95 207ms · 24 h");
  });

  it("Deployments labels the p95 with the same window and the same number", async () => {
    engine(() => ({ body: {} }));
    await openProject("&tab=deployments");
    expect(tid("px-env-p95-staging")!.textContent).toBe("207ms");
    expect(tid("px-env-staging")!.querySelector("dt")!.textContent).toBe("p95 · 24 h");
  });
});

describe("12-09 one clock on Health", () => {
  it("today is 24-hour hh:mm, the same as the incidents and the parity component", () => {
    const now = new Date();
    const at = new Date(now); at.setHours(9, 52, 0, 0);
    expect(clockTime(at.toISOString(), now)).toBe("09:52");
    expect(clockTime(at.toISOString(), now)).not.toMatch(/AM|PM/);
  });
});

describe("14-01 a server reads Infra / Servers / <host>", () => {
  it("the trail an open server drawer sets replaces '<project> / Infra'", () => {
    const base = crumbsFor({ active: "infra", areaLabel: "Infra", routeMode: false, hasProject: true, tab: null });
    expect(base.map((c) => c.kind)).toEqual(["project", "here"]);
    setCrumbTrail({ area: "infra", trail: [{ label: "Servers", href: "?area=infra&view=fleet" }, { label: "203.0.113.20" }] });
    const c = crumbsFor({ active: "infra", areaLabel: "Infra", routeMode: false, hasProject: true, tab: null, trail: crumbTrail() });
    expect(c.map((x) => x.label)).toEqual(["Infra", "Servers", "203.0.113.20"]);
    expect(c.map((x) => x.kind)).toEqual(["href", "href", "here"]);
    // Another area never borrows Infra's trail.
    expect(crumbsFor({ active: "observability", areaLabel: "Observability", routeMode: false, hasProject: true, tab: null, trail: crumbTrail() })
      .map((x) => x.label)).not.toContain("Servers");
    setCrumbTrail(null);
  });
});
