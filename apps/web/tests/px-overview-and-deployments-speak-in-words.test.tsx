// @vitest-environment happy-dom
/**
 * OVERVIEW (screen 04) and DEPLOYMENTS (screen 08) speak in words (GAPS 4–12, 17; PX-01/03/08/09/
 * 10/23/34). Rendered on the real ProjectPage with the engine's answers: Needs-you cards named for
 * the action with their mini diagram and a row that opens its drawer; journey nodes with icons
 * and tooltips (never a bare number); This week as a day-labelled timeline; the subscription
 * cost said as such; What's next as the top three with Accept; the Running tile opening its
 * item; deploy history in words linking to the real Infra deploy; Redeploy through the routed
 * Deploy; the server named, not its IP.
 */
import React, { act } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(window.location.search),
  usePathname: () => "/",
}));

import { ProjectPage } from "../app/projects/project-page";
import { FX_DEPLOYMENTS, FX_HEALTH, FX_KNOWLEDGE, FX_ONBOARDING, FX_OVERVIEW, FX_ROADMAP, FX_SETTINGS, FX_SUMMARY, FX_WORK } from "../lib/project-experience-fixtures";
import { $$, mountAs, stubFetch, tid, unmount, type Call } from "./_helpers/multirepo-fixtures";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const P = "helpdesk-ai-demo";
const settle = async (n = 8) => { for (let i = 0; i < n; i++) await act(async () => { await new Promise((r) => setTimeout(r, 0)); }); };
const press = async (el: Element | null) => { if (!el) throw new Error("nothing to press"); await act(async () => { (el as HTMLElement).click(); }); await settle(); };
const params = () => new URLSearchParams(window.location.search);
const DAY = 86_400_000;
const ago = (ms: number) => new Date(Date.now() - ms).toISOString();

const OVERVIEW = {
  ...FX_OVERVIEW,
  roadmap_count: 5,
  journey: [
    { key: "onboarding", label: "Onboarded", state: "done", sub: "Nov 2", detail: "Onboarded Nov 2 — FastAPI · React", href: "?project_id=helpdesk-ai-demo&area=project&drawer=stage%3Aonboarding" },
    { key: "ready", label: "Ready", state: "done", sub: "6/6", detail: "Ready for work: 6 of 6 checks", href: "?project_id=helpdesk-ai-demo&area=project&drawer=stage%3Aready" },
    { key: "backlog", label: "Backlog", state: "done", sub: "7 of 12", detail: "Backlog: 12 items proposed, you accepted 7", href: "?project_id=helpdesk-ai-demo&area=project&tab=roadmap&drawer=stage%3Abacklog" },
    { key: "working", label: "Working", state: "now", sub: "3 merged", detail: "Working: 3 merged, 1 building, 1 waiting on you", href: "?project_id=helpdesk-ai-demo&area=project&tab=work&drawer=stage%3Aworking" },
    { key: "staging", label: "Staging", state: "done", sub: "2h ago", detail: "Staging last deployed 2h ago", href: "?project_id=helpdesk-ai-demo&area=project&tab=deployments&drawer=stage%3Astaging" },
    { key: "production", label: "Production", state: "waiting", sub: "needs you", detail: "Production needs a person: pick a host, then approve", href: "?project_id=helpdesk-ai-demo&area=project&tab=deployments&drawer=stage%3Aproduction" },
  ],
  needs_you: [
    { id: "dec_merge3", kind: "merge", label: "Merge PR #3", detail: "Approve code change (PR #3)",
      mini: [{ label: "Tests 148", state: "done" }, { label: "Review", state: "done" }, { label: "Merge", state: "waiting" }],
      action: { label: "Review PR", href: "?project_id=helpdesk-ai-demo&area=decisions&decision=dec_merge3", decision_id: "dec_merge3" } },
    { id: "dec_dep5", kind: "deploy", label: "Deploy PR #5 to Staging", detail: "Deploy acme/helpdesk@f3f130c9a1b2 to 203.0.113.10?",
      mini: [{ label: "PR #5 merged", state: "done" }, { label: "Staging · you", state: "waiting" }, { label: "Smoke test", state: "todo" }],
      action: { label: "Approve deploy", href: "?project_id=helpdesk-ai-demo&area=decisions&decision=dec_dep5", decision_id: "dec_dep5" } },
    { id: "prod:host", kind: "setup", label: "Pick a prod host", detail: "Beacon can't choose this", icon: "cloud", mini: [],
      action: { label: "Pick host", href: "?project_id=helpdesk-ai-demo&area=infra&add=target", decision_id: null } },
  ],
  tiles: {
    ...FX_OVERVIEW.tiles,
    running: { count: 1, href: "?project_id=helpdesk-ai-demo&area=project&tab=work",
               current: { id: "wfr_search", label: "Ticket search filters", progress: [3, 6], sub: "Building · 12m", agent: { name: "Forge", role_key: "engineer" } } },
    cost_week: { usd: 0, delta_pct: null, cap_usd: 50, series: [0, 0, 0, 0, 0, 0, 0], on_subscription: true, list_usd: 57.55,
                 list_series: [0, 0, 0, 12, 20, 15.5, 10.05], href: "?project_id=helpdesk-ai-demo&area=budget&tab=limits" },
  },
  next: [
    { id: "wi_intake", label: "Email-to-ticket intake", kind: "feature", source: "roadmap", state: "next", accepted: true, accept: null,
      href: "?project_id=helpdesk-ai-demo&area=project&tab=roadmap&drawer=rec%3Awi_intake" },
    { id: "wi_sla", label: "SLA weekend pause test", kind: "incident_hotspot", source: "roadmap", state: "next",
      accept: { href: "/v1/ambient/roadmap/wi_sla/accept", method: "POST", body: { as: "ticket", project_id: P } },
      href: "?project_id=helpdesk-ai-demo&area=project&tab=roadmap&drawer=rec%3Awi_sla" },
    { id: "wi_alert", label: "Alert on /api/tickets 5xx", kind: "alert_gap", source: "roadmap", state: "next",
      accept: { href: "/v1/ambient/roadmap/wi_alert/accept", method: "POST", body: { as: "ticket", project_id: P } },
      href: "?project_id=helpdesk-ai-demo&area=project&tab=roadmap&drawer=rec%3Awi_alert" },
  ],
  week: [
    { at: ago(6 * DAY), kind: "merge", label: "PR #1 merged", href: "https://github.com/acme/helpdesk/pull/1" },
    { at: ago(5 * DAY), kind: "deploy", label: "Staging deploy", href: "?project_id=helpdesk-ai-demo&area=infra&view=deploys&deploy=stack%3Asdep_1" },
    { at: ago(3 * DAY), kind: "merge", label: "PR #2 merged", href: "https://github.com/acme/helpdesk/pull/2" },
    { at: ago(2 * DAY), kind: "idea", label: "5 ideas found", href: "" },
    { at: ago(60_000), kind: "pr", label: "PR #3 opened", href: "https://github.com/acme/helpdesk/pull/3" },
  ],
};

const DEPLOYMENTS = {
  ...FX_DEPLOYMENTS,
  environments: [{ ...FX_DEPLOYMENTS.environments[0], target_id: "dept_1", host: "203.0.113.20", host_name: "Staging server", host_projects: 3,
                   p95_source: "app",
                   last_deploy: { commit: "a41c9e2", at: ago(2 * 3_600_000), by: "Beacon", label: "PR #2", pr: 2, pr_url: "https://github.com/acme/helpdesk/pull/2",
                                  href: "?project_id=helpdesk-ai-demo&area=infra&view=deploys&deploy=stack%3Asdep_2" } }],
  production: { state: "none", action: { label: "Set up production", href: "?project_id=helpdesk-ai-demo&area=infra&add=target" } },
  history: [
    { id: "sdep_2", label: "Staging · PR #2 UI refresh", status: "healthy", state: "done", state_word: "Live", duration: "3m 10s", smoke: "smoke ✓",
      by: "Pat Owner", by_agent: false, at: ago(DAY), href: "?project_id=helpdesk-ai-demo&area=infra&view=deploys&deploy=stack%3Asdep_2" },
    { id: "sdep_0", label: "Staging · first deploy", status: "failed", state: "recovered", recovered: true, state_word: "Failed",
      reason: "Port 8000 was taken on the host. Beacon moved the app to 8004", at: ago(8 * DAY), by: "boss", by_agent: true,
      href: "?project_id=helpdesk-ai-demo&area=infra&view=deploys&deploy=stack%3Asdep_0" },
  ],
};

function engine(over: (c: Call) => { status?: number; body: unknown } | undefined = () => undefined) {
  return stubFetch((c) => {
    const o = over(c);
    if (o) return o;
    if (c.method !== "GET") return { body: { ok: true, ref: "stack:sdep_9" } };
    if (c.path === "/v1/projects/summary") return { body: FX_SUMMARY };
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

describe("Overview speaks in words (screen 04)", () => {
  it("Needs you names the action, draws its mini flow, and the row opens its drawer", async () => {
    engine();
    await openProject();
    const card = tid("px-ov-needs")!;
    expect(card.textContent).toContain("Deploy PR #5 to Staging");
    expect(card.textContent).not.toMatch(/@[0-9a-f]{7,}|203\.0\.113|Answer/);
    expect(tid("px-ov-ny-act-dec_dep5")!.textContent).toBe("Approve deploy");
    expect(tid("px-ov-ny-act-dec_merge3")!.textContent).toBe("Review PR");
    expect(tid("px-ov-ny-act-prod:host")!.textContent).toBe("Pick host");
    const flow = tid("px-ov-ny-flow-dec_dep5")!;
    expect([...flow.querySelectorAll(".px-fl-n")].map((n) => n.textContent)).toEqual(["PR #5 merged", "Staging · you", "Smoke test"]);
    expect(flow.querySelector('[data-t="waiting"]')!.textContent).toContain("Staging · you");
    expect(card.textContent).toContain("Beacon can't choose this");
    await press(tid("px-ov-ny-dec_dep5"));
    expect(params().get("drawer")).toBe("needs:dec_dep5");
  });

  it("the journey shows each stage's words and tooltip; production waits on you; no bare numbers", async () => {
    engine();
    await openProject();
    const prod = tid("px-journey-production")!;
    expect(prod.getAttribute("data-s")).toBe("waiting");
    expect(prod.getAttribute("title")).toBe("Production needs a person: pick a host, then approve");
    expect(prod.textContent).toContain("needs you");
    expect(tid("px-journey-working")!.getAttribute("data-s")).toBe("now");
    expect(tid("px-journey-ready")!.textContent).toContain("6/6");
    for (const n of $$(".px-jr-node .px-jr-shape")) expect(n.textContent ?? "").not.toMatch(/^\d+$/);
  });

  it("This week is a timeline oldest first with day labels, five points, no repeats", async () => {
    engine();
    await openProject();
    const tl = tid("px-ov-week")!;
    const points = [...tl.querySelectorAll(".px-tl-p")];
    expect(points).toHaveLength(5);
    expect(points.map((p) => p.querySelector(".px-tl-cap")!.textContent)).toEqual(["PR #1 merged", "Staging deploy", "PR #2 merged", "5 ideas found", "PR #3 opened"]);
    expect(points[4].querySelector(".px-tl-day")!.textContent).toBe("Today");
    expect(points[0].querySelector(".px-tl-day")!.textContent).toMatch(/^[A-Z][a-z]{2}/);
    expect(tl.closest("section")!.className).toContain("px-hide-phone");
  });

  it("a week on a subscription says so with the list price, never a bare $0.00", async () => {
    engine();
    await openProject();
    const cost = tid("px-tile-cost")!;
    expect(cost.textContent).toContain("On subscription");
    expect(cost.textContent).toContain("≈ $57.55 at list price");
    expect(cost.textContent).not.toContain("$0.00");
  });

  it("What's next is the top three: an accepted one tagged Next, the others with Accept", async () => {
    const calls = engine();
    await openProject();
    const card = tid("px-ov-next")!;
    expect(tid("px-ov-next-0")!.textContent).toContain("Feature · accepted");
    expect(tid("px-ov-next-0")!.textContent).toContain("Next");
    expect(tid("px-ov-next-1")!.textContent).toContain("Incident hotspot");
    expect(tid("px-ov-accept-wi_sla")!.textContent).toBe("Accept");
    expect(card.querySelectorAll("[data-testid^=px-ov-accept-]")).toHaveLength(2);
    await press(tid("px-ov-accept-wi_sla"));
    expect(calls.some((c) => c.method === "POST" && c.path === "/v1/ambient/roadmap/wi_sla/accept")).toBe(true);
  });

  it("the Running tile opens the running item's drawer; the Roadmap tab carries its count", async () => {
    engine();
    await openProject();
    expect(tid("px-tile-running")!.textContent).toContain("Building · 12m");
    await press(tid("px-tile-running"));
    expect(params().get("drawer")).toBe("work:wfr_search");
    expect(tid("px-tab-roadmap")!.textContent).toContain("5");
  });

  it("Try it links to Settings › Demo logins", async () => {
    engine();
    await openProject();
    await press(tid("px-ov-try-settings"));
    expect(params().get("tab")).toBe("settings");
    expect(params().get("section")).toBe("demo_logins");
  });
});

describe("Deployments speak in words (screen 08)", () => {
  it("history rows read in words and open the real Infra deploy; a recovered failure is amber", async () => {
    engine();
    await openProject("&tab=deployments");
    const row = tid("px-dep-h-sdep_2")!;
    expect(row.tagName).toBe("A");
    expect(row.getAttribute("href")).toContain("deploy=stack%3Asdep_2");
    expect(row.textContent).toContain("Staging · PR #2 UI refresh");
    expect(row.textContent).toContain("3m 10s · smoke ✓");
    const first = tid("px-dep-h-sdep_0")!;
    expect(first.getAttribute("data-s")).toBe("recovered");
    expect(first.textContent).toContain("Beacon moved the app to 8004");
    expect(tid("px-dep-history")!.textContent).not.toMatch(/awaiting_approval|usr_|healthy ·/);
  });

  it("the env card names its server, links the PR, says where p95 comes from, and Redeploy plans through the routed Deploy", async () => {
    const calls = engine();
    await openProject("&tab=deployments");
    const host = tid("px-env-host-staging")!;
    expect(host.textContent).toContain("runs on Staging server");
    expect(host.getAttribute("title")).toContain("shared by 3 projects");
    const card = tid("px-env-staging")!;
    expect(card.querySelector('a[href="/changes/github/acme/helpdesk/2"]')).not.toBeNull();   // BOSSAPP-954: the PR opens in BOSS
    expect(card.querySelector("dt")!.getAttribute("title")).toContain("app's own requests");
    await press(tid("px-env-redeploy-staging"));
    // 08-05: nothing is sent before the confirm sheet says what, where, which commit, who approves.
    expect(calls.find((c) => c.method === "POST" && c.path === "/v1/infra/targets/dept_1/deploy")).toBeFalsy();
    expect(tid("px-env-redeploy-sheet-staging")).not.toBeNull();
    await press(tid("px-env-redeploy-sheet-staging-go"));
    await settle();
    const post = calls.find((c) => c.method === "POST" && c.path === "/v1/infra/targets/dept_1/deploy");
    expect(post).toBeTruthy();
    expect(card.textContent).toContain("Redeploy planned");
  });

  it("production that isn't set up offers Pick host into Infra's add-target sheet", async () => {
    engine();
    await openProject("&tab=deployments");
    const act_ = tid("px-dep-prod-act")!;
    expect(act_.textContent).toContain("Pick host");
    expect(act_.getAttribute("href")).toContain("add=target");
    expect(tid("px-dep-prod")!.textContent).toContain("Pick a host for production");
  });
});
