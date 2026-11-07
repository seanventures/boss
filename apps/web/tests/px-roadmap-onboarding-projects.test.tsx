// @vitest-environment happy-dom
/**
 * Screens 07 (Roadmap), 10 (Onboarding) and 02 (Projects) as the wireframes draw them, on the
 * engine's own shapes: the Plan's Now · Next · Later lanes from GET /v1/projects/{id}/plan (move a
 * card between lanes, start one through THE start door), recommendation filters in words and
 * cards as diagrams (evidence → detector → agent, ≈ cost, Why ›), the real "Accept backlog / Edit"
 * (reorder, drop, rename; nothing starts), and project cards with uptime ring, status lines, crew
 * faces, the Archived filter and "Not onboarded · Start onboarding".
 */
import React, { act } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(window.location.search),
  usePathname: () => "/",
}));

import { ProjectsHome } from "../app/projects/projects-home";
import { ProjectPage } from "../app/projects/project-page";
import {
  FX_DEPLOYMENTS, FX_HEALTH, FX_KNOWLEDGE, FX_ONBOARDING, FX_OVERVIEW, FX_ROADMAP, FX_SETTINGS, FX_SUMMARY, FX_WORK,
} from "../lib/project-experience-fixtures";
import { $$, mountAs, stubFetch, tid, unmount, type Call } from "./_helpers/multirepo-fixtures";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const P = "helpdesk-ai-demo";
const settle = async (n = 8) => { for (let i = 0; i < n; i++) await act(async () => { await new Promise((r) => setTimeout(r, 0)); }); };
const press = async (el: Element | null) => { if (!el) throw new Error("nothing to press"); await act(async () => { (el as HTMLElement).click(); }); await settle(); };
const type = async (el: Element | null, value: string) => {
  const input = el as HTMLInputElement;
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, value);
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
};
const at = (q: string) => window.history.replaceState(null, "", `/?org=org_1&${q}`);

const card = (id: string, label: string, lane: string, extra: Record<string, unknown> = {}) => ({
  id, label, title: label, lane, state: lane === "now" ? "running" : "planned", href: "", source: "roadmap",
  start: lane === "now" ? null : { href: `/v1/projects/${P}/work`, method: "POST", body: { kind: "feature", text: label, title: label, source_ref: `rec:${id}` } },
  ...extra,
});
const PLAN = {
  now: [card("wfr_search", "Search filters", "now", { agent: { name: "Forge", role_key: "builder" } }),
        card("wfr_visual", "Visual refresh", "now", { state: "needs_you" })],
  next: [card("wi_email", "Email intake", "next"), card("wi_sla", "Weekend SLA fix", "next"), card("wi_tests", "Fix 6 tests", "next")],
  later: [card("wi_reply", "Reply suggestions", "later"), card("wi_index", "Status index", "later")],
  counts: { now: 2, next: 3, later: 2 },
};
const REC = {
  id: "rec_sla", work_item_id: "rec_sla", title: "Regression test for SLA weekend pause", kind: "hardening", detector: "incident_cause",
  proposed_shape: "ticket", status: "open", rank: 1, score: 9, squad: ["qa"], why: "Two incidents trace to sla.mjs.",
  badges: { kind: "Hardening", value: 1.9, effort: 1, risk: 1, evidence: 3, sources: 2, cost_usd: 2, detector: "Incident hotspot" },
  strip: [{ type: "signal", label: "Incidents", badges: ["×2"] }, { type: "signal", label: "Code graph", badges: ["×1"] },
          { type: "agent", label: "Incident hotspot", badges: [], icon: "detective" }, { type: "action", label: "Hardening sla.mjs", badges: [] }],
};
const REC_CARD = { evidence: [{ label: "2 incidents", icon: "warning" }, { label: "sla.mjs hot", icon: "fire" }],
  detector: { label: "Incident hotspot", icon: "detective" }, does: "Test + fix", cost_usd: 2, cost_basis: "this item's estimate" };
const READY_ROWS = [
  ["repo_access", "Repo access", "done", "read · branch · PR", "engineering_captain"], ["baseline_build", "Builds clean", "done", "make setup · 1m 40s", "builder"],
  ["tests", "Tests run", "done", "6 failing → backlog", "qa"], ["workflow", "Workflow picked", "done", "Minor feature", "engineering_captain"],
  ["deploy_target", "Staging host", "done", "port 8004", "sre"], ["secrets", "App secrets", "done", "OPENAI_API_KEY set", "sre"],
].map(([key, label, state, sub, agent]) => ({ key, label, state, sub, agent, owner: "agent", detail: sub }));
const BACKLOG = [
  { id: "c1", label: "Fix 6 failing tests", detail: "CI fails", source: "ci", kind: "bug", ref: "ci:main", state: "proposed" },
  { id: "c2", label: "SLA ignores weekends", detail: "#12", source: "bug", kind: "bug", ref: "issue:12", external_url: "https://github.com/x/y/issues/12", state: "proposed" },
  { id: "c3", label: "SLA timers", detail: "#9", source: "ticket", kind: "ticket", ref: "issue:9", state: "proposed" },
  { id: "c4", label: "UI refresh", detail: "7 TODOs in web/src.", source: "roadmap", kind: "feature", ref: "todo:web", state: "proposed" },
  { id: "c5", label: "Status index", detail: "no index", source: "roadmap", kind: "chore", ref: "scan:index", state: "accepted" },
  { id: "c6", label: "Add a README", detail: "none", source: "roadmap", kind: "chore", ref: "scan:readme", state: "proposed" },
];

function engine(opts: { ready?: boolean; summary?: unknown; plan?: unknown } = {}) {
  return stubFetch((c: Call) => {
    if (c.method !== "GET") {
      if (c.path.endsWith("/onboarding/backlog/accept")) return { body: { accepted: (c.body as { ids: string[] }).ids, already: [], started: [], lane: "next" } };
      return { body: { ok: true, ...PLAN } };
    }
    if (c.path === "/v1/projects/summary") return { body: opts.summary ?? FX_SUMMARY };
    if (c.path === `/v1/projects/${P}/plan` || c.path === "/v1/projects/northpeak-retail/plan") return { body: opts.plan ?? PLAN };
    const m = c.path.match(/^\/v1\/projects\/([^/]+)\/(overview|work|knowledge|deployments|onboarding|settings|health|captain)$/);
    if (m) {
      const onboarding = opts.ready ? { ...FX_ONBOARDING, ready: READY_ROWS, backlog: BACKLOG, backlog_total: 12, accepted: ["c5"] } : FX_ONBOARDING;
      const table: Record<string, unknown> = {
        overview: m[1] === "northpeak-retail" ? { ...FX_OVERVIEW, project: { ...FX_OVERVIEW.project, id: m[1], name: "NorthPeak Retail", stage: "onboarding" }, needs_you: [] } : FX_OVERVIEW,
        work: FX_WORK, knowledge: FX_KNOWLEDGE, deployments: FX_DEPLOYMENTS, onboarding, settings: FX_SETTINGS, health: FX_HEALTH,
      };
      return { body: table[m[2]] };
    }
    if (c.path === "/v1/ambient/roadmap/rec_sla" && c.method === "GET") {
      return { body: { ...REC, card: REC_CARD, detail: { why: "Two incidents trace to sla.mjs. The weekend pause skips Saturday and Sunday, so a ticket opened Friday breaches on Monday.",
        evidence: [{ kind: "incident", ref: "inc_1", note: "Weekend ticket breached SLA" }, { kind: "incident", ref: "inc_2", note: "Second weekend breach" }],
        facts: { incident_cause: { incidents: 2 } }, value: 72, effort_units: 2, evidence_strength: 0.8 } } };
    }
    if (c.path === "/v1/ambient/roadmap") {
      return { body: { ...FX_ROADMAP, items: [REC, ...((FX_ROADMAP as { items: unknown[] }).items ?? [])], counts: { open: 5, accepted: 7, rejected: 2, snoozed: 1, resolved_by_change: 0 } } };
    }
    return undefined;
  });
}

beforeEach(() => { window.history.replaceState(null, "", "/"); });
afterEach(() => { unmount(); });

async function openProject(q = "", id = P) {
  at(`project_id=${id}&area=project${q}`);
  await mountAs(<ProjectPage projectId={id} actor="ceo" orgId="org_1" />, "owner");
  await settle(10);
}

describe("Roadmap (screen 07): the Plan's lanes", () => {
  it("draws Now · Next · Later from the plan read, with counts, glyphs and faces", async () => {
    engine();
    await openProject("&tab=roadmap");
    const lane = (k: string) => $$(`[data-testid='px-rm-${k}'] .px-rm-t`).map((b) => b.textContent);
    expect(lane("now")).toEqual(["Search filters", "Visual refresh"]);
    expect(lane("next")).toEqual(["Email intake", "Weekend SLA fix", "Fix 6 tests"]);
    expect(lane("later")).toEqual(["Reply suggestions", "Status index"]);
    expect(tid("px-rm-item-wfr_visual")!.querySelector(".px-rm-glyph")!.getAttribute("aria-label")).toBe("needs you");
    expect(tid("px-rm-item-wfr_search")!.querySelector(".px-rm-glyph")!.getAttribute("aria-label")).toBe("running");
    // An item is in exactly one lane (PX-38).
    const all = [...lane("now"), ...lane("next"), ...lane("later")];
    expect(new Set(all).size).toBe(all.length);
  });

  it("moves a card to Later and starts one through the start door (nothing else posts)", async () => {
    const calls = engine();
    await openProject("&tab=roadmap");
    await press(tid("px-rm-move-wi_email"));
    expect(calls.find((c) => c.path === `/v1/projects/${P}/plan/wi_email`)?.body).toEqual({ lane: "later" });
    await press(tid("px-rm-start-wi_sla"));
    expect(calls.find((c) => c.method === "POST" && c.path === `/v1/projects/${P}/work`)?.body).toMatchObject({ title: "Weekend SLA fix", source_ref: "rec:wi_sla" });
  });

  it("an empty lane offers Ask Helm to plan and a drag hint", async () => {
    engine({ plan: { ...PLAN, later: [] } });
    await openProject("&tab=roadmap");
    expect(tid("px-rm-later-empty")!.textContent).toContain("Nothing here yet");
    expect(tid("px-rm-later-empty")!.textContent).toContain("or drag an item here");
    await press(tid("px-rm-later-ask"));
    expect(new URLSearchParams(window.location.search).get("drawer") ?? new URLSearchParams(window.location.search).get("agent")).toBeTruthy();
  });
});

describe("Roadmap (screen 07): recommendations", () => {
  it("filters in words with real counts, and draws each card as evidence → detector → agent with ≈ cost and Why", async () => {
    const calls = engine();
    await openProject("&tab=roadmap");
    const filters = $$("[data-testid^='px-rm-filter-']").map((b) => b.textContent);
    expect(filters).toEqual(["Open 5", "Accepted 7", "Rejected 2", "Snoozed 1"]);
    expect(document.body.textContent).not.toMatch(/resolved_by_change|Resolved_by_change/);
    const flow = tid("px-rec-flow-rec_sla")!.textContent!;
    expect(flow).toContain("2 incidents");
    expect(flow).toContain("Incident hotspot");
    expect(flow).toContain("Test + fix");
    expect(tid("px-rec-cost-rec_sla")!.textContent).toBe("≈ $2");
    await press(tid("px-rec-whybtn-rec_sla"));
    expect(tid("px-rec-why-text-rec_sla")!.textContent).toContain("Two incidents");
    await press(tid("px-rm-filter-accepted"));
    expect(calls.some((c) => c.path === "/v1/ambient/roadmap" && c.search?.includes("status=accepted"))).toBe(true);
  });

  it("Reject always carries a reason chip; Snooze is in the ⋯ menu; Accept shows Accepted → On Work · Next", async () => {
    const calls = engine();
    await openProject("&tab=roadmap");
    await press(tid("px-rec-reject-rec_sla"));
    expect(document.body.textContent).toContain("Reject because");
    await press(tid("px-rec-why-wrong"));
    await press(tid("px-rec-reject-confirm-rec_sla"));
    expect(calls.find((c) => c.path === "/v1/ambient/roadmap/rec_sla/reject")?.body).toMatchObject({ category: "wrong" });
    unmount();
    engine();
    await openProject("&tab=roadmap");
    await press(tid("px-rec-accept-rec_sla"));
    expect(tid("px-rec-rec_sla")!.textContent).toContain("On Work · Next");
    expect(tid("px-rec-open-rec_sla")).not.toBeNull();
  });
});

describe("Roadmap (screen 07): round 3 — every decision says itself", () => {
  it("Reject waits for a reason: none is preselected, and the button says which one it sends", async () => {
    const calls = engine();
    await openProject("&tab=roadmap");
    await press(tid("px-rec-reject-rec_sla"));
    expect($$("[data-testid^='px-rec-why-'][aria-checked='true']")).toHaveLength(0);
    const confirm = tid("px-rec-reject-confirm-rec_sla") as HTMLButtonElement;
    expect(confirm.disabled).toBe(true);
    await press(confirm);
    expect(calls.some((c) => c.path.endsWith("/reject"))).toBe(false);
    await press(tid("px-rec-why-duplicate"));
    expect(confirm.disabled).toBe(false);
    expect(confirm.getAttribute("title")).toBe("Reject: Duplicate");
  });

  it("Not now asks how long and why, shows the date, and sends both", async () => {
    const calls = engine();
    await openProject("&tab=roadmap");
    await press(tid("px-rec-more-rec_sla"));
    await press(tid("px-rec-snooze-rec_sla"));
    expect(tid("px-rec-snooze-form-rec_sla")).not.toBeNull();
    await press(tid("px-rec-snooze-30"));
    expect(tid("px-rec-snooze-until-rec_sla")!.textContent).toBe(new Date(Date.now() + 30 * 86_400_000).toLocaleDateString(undefined, { day: "numeric", month: "short" }));
    const input = document.querySelector<HTMLInputElement>("[aria-label='Why not now']")!;
    await act(async () => { const set = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!; set.call(input, "after the launch"); input.dispatchEvent(new Event("input", { bubbles: true })); });
    await press(tid("px-rec-snooze-confirm-rec_sla"));
    const body = calls.find((c) => c.path === "/v1/ambient/roadmap/rec_sla/snooze")?.body as { until: string; reason: string };
    expect(body.reason).toBe("after the launch");
    expect(Math.round((Date.parse(body.until) - Date.now()) / 86_400_000)).toBe(30);
  });

  it("an accepted card stays as the green Accepted → On Work · Next card after the list re-reads", async () => {
    engine();
    await openProject("&tab=roadmap");
    await press(tid("px-rec-accept-rec_sla"));
    await settle(10);
    expect(tid("px-rec-rec_sla")!.getAttribute("data-s")).toBe("accepted");
    expect(tid("px-rec-rec_sla")!.textContent).toContain("On Work · Next");
  });

  it("Why › Full reasoning is the recommendation's own record: its diagram, ≈ cost, the whole why and its evidence", async () => {
    engine();
    await openProject("&tab=roadmap&drawer=rec%3Arec_sla");
    await settle(10);
    const d = tid("px-drawer-rec")!;
    expect(d.textContent).not.toContain("Recommendations are ranked, accepted and rejected on the Roadmap.");
    expect(tid("px-drawer-rec-why")!.textContent).toBe("Two incidents trace to sla.mjs. The weekend pause skips Saturday and Sunday, so a ticket opened Friday breaches on Monday.");
    expect(tid("px-drawer-rec-flow")!.textContent).toContain("Incident hotspot");
    expect(tid("px-drawer-rec-cost")!.textContent).toContain("≈ $2");
    expect(tid("px-drawer-rec-evidence")!.textContent).toContain("Weekend ticket breached SLA");
    expect(tid("px-drawer-rec-rank")!.textContent).toContain("72");
  });
});

describe("Overview (screen 04): What's next accepts once", () => {
  it("an accepted item turns into Next at once; there is no second Accept to press (tester 07-03)", async () => {
    const calls = engine();
    await openProject("");
    const btn = $$("[data-testid^='px-ov-accept-']")[0];
    expect(btn).toBeTruthy();
    const id = btn.getAttribute("data-testid")!.replace("px-ov-accept-", "");
    await press(btn);
    expect(tid(`px-ov-accept-${id}`)).toBeNull();
    const posts = calls.filter((c) => c.method === "POST" && c.path.includes("/accept"));
    expect(posts).toHaveLength(1);
  });
});

describe("Onboarding (screen 10): Accept backlog / Edit", () => {
  it("is locked until Ready is complete", async () => {
    engine();
    await openProject("", "northpeak-retail");
    expect((tid("px-ob-accept") as HTMLButtonElement).disabled).toBe(true);
    expect((tid("px-ob-edit") as HTMLButtonElement).disabled).toBe(true);
  });

  it("accepts the not-yet-accepted items onto Next, shows a toast with a Roadmap link, and starts nothing", async () => {
    const calls = engine({ ready: true });
    await openProject("", "northpeak-retail");
    expect($$("[data-testid='px-ob-checklist'] .px-ck-t").map((x) => x.firstChild?.textContent)).toEqual(
      ["Repo access", "Builds clean", "Tests run", "Workflow picked", "Staging host", "App secrets"]);
    expect(tid("px-ob-backlog-1")!.textContent).toContain("issue #12");
    expect(tid("px-ob-backlog-3")!.textContent).toContain("TODO ×7");
    expect(tid("px-ob-more")!.textContent).toContain("+8 more");
    expect(tid("px-ob-marshal")!.getAttribute("title")).toBe("Ranked by Marshal, Chief of Staff");
    await press(tid("px-ob-accept"));
    const posted = calls.find((c) => c.path === "/v1/projects/northpeak-retail/onboarding/backlog/accept");
    expect(posted?.body).toEqual({ ids: ["c1", "c2", "c3", "c4", "c6"], titles: {} });
    expect(tid("px-ob-said")!.textContent).toContain("Nothing has started");
    expect(tid("px-ob-said-roadmap")).not.toBeNull();
    expect(calls.some((c) => c.path.endsWith("/work") && c.method === "POST")).toBe(false);
  });

  it("Edit reorders, drops and renames before accepting", async () => {
    const calls = engine({ ready: true });
    await openProject("", "northpeak-retail");
    await press(tid("px-ob-edit"));
    expect(tid("px-ob-editor")).not.toBeNull();
    await press(tid("px-ob-ed-1")!.querySelector("[aria-label='Move SLA ignores weekends up']"));
    await act(async () => { (tid("px-ob-ed-2")!.querySelector("input[type=checkbox]") as HTMLInputElement).click(); });
    await type(tid("px-ob-ed-0")!.querySelector("input.px-input"), "Weekend SLA pause");
    await press(tid("px-ob-editor-accept"));
    expect(calls.find((c) => c.path === "/v1/projects/northpeak-retail/onboarding/backlog/accept")?.body)
      .toEqual({ ids: ["c2", "c1", "c4", "c6"], titles: { c2: "Weekend SLA pause" } });
  });
});

describe("Projects (screen 02): cards", () => {
  const items = [
    { id: "hd", name: "HelpDesk AI Demo", repo: "paramg/helpdesk-ai-demo", stage: "working", needs_you: 2, running: 1, health: "healthy",
      journey: [{ key: "onboarding", label: "Onboarded", state: "done" }, { key: "ready", label: "Ready", state: "done" }, { key: "backlog", label: "Backlog", state: "done" },
                { key: "working", label: "Working", state: "waiting" }, { key: "staging", label: "Staging", state: "done" }, { key: "production", label: "Production", state: "todo" }],
      uptime_pct: 99.9, lines: ["staging healthy", "3 merged · 1 building"], line_tone: "done", status_label: "Working",
      crew: [{ name: "Juno", role_key: "pm", state: "idle" }, { name: "Forge", role_key: "software_engineer", state: "working" }],
      live_agents: [{ name: "Forge", role_key: "software_engineer", state: "working" }], cost_week: 14.2, series: [1, 2, 3, 2, 4, 1, 1],
      captain: { name: "Helm", role_key: "engineering_captain" }, updated_at: "2026-11-06T10:00:00Z" },
    { id: "np", name: "NorthPeak Retail", repo: "northpeak/saleor", stage: "onboarding", needs_you: 0, running: 0, health: "unknown", journey: [],
      onboarding_active: true, onboarding_progress: [4, 6], lines: ["mapping code", "1,284 nodes so far"], line_tone: "now", status_label: "Onboarding",
      crew: [], cost_week: 6.9, series: [], captain: { name: "Helm", role_key: "engineering_captain" }, updated_at: "2026-11-05T10:00:00Z" },
    { id: "hw", name: "Hello World", repo: "paramg/hello-world", stage: "onboarding", needs_you: 0, running: 0, health: "unknown", journey: [],
      status_label: "Not onboarded", start_onboarding: { href: "/v1/projects/hw/onboarding/start", method: "POST" }, lines: ["not deployed yet", "idle"],
      crew: [], cost_week: 0, series: [], captain: null, updated_at: "2026-11-04T10:00:00Z" },
    { id: "old", name: "Old Thing", repo: "paramg/old", stage: "ready", needs_you: 0, running: 0, health: "unknown", journey: [], archived: true,
      lines: [], crew: [], cost_week: null, series: [], captain: null, updated_at: "2026-10-01T10:00:00Z" },
  ];

  it("shows the uptime ring, the two status lines, live crew faces, the cap, and an Archived filter", async () => {
    engine({ summary: { items, week_cap_usd: 150 } });
    at("area=projects");
    await mountAs(<ProjectsHome projectId={P} actor="ceo" projects={[]} />);
    await settle(10);
    expect($$("[data-testid^='px-card-'][data-link]").map((c) => c.dataset.testid)).toEqual(["px-card-hd", "px-card-np", "px-card-hw"]);
    expect(tid("px-card-hd")!.querySelector(".px-ring")!.textContent).toBe("99.9");
    expect(tid("px-card-np")!.querySelector(".px-ring")!.textContent).toBe("4/6");
    expect(tid("px-card-lines-hd")!.textContent).toContain("staging healthy");
    expect(tid("px-card-lines-hd")!.textContent).toContain("3 merged · 1 building");
    expect(tid("px-card-crew-hd")).not.toBeNull();
    // 02-05 the Running tile shows who is acting now (live_agents): Forge, not the whole crew.
    expect(tid("px-stat-running-faces")!.querySelectorAll(".px-face, [class*='avatar'], img, svg").length).toBeGreaterThan(0);
    expect(tid("px-stat-running-faces")!.innerHTML).toContain("Forge");
    expect(tid("px-stat-running-faces")!.innerHTML).not.toContain("Juno");
    // 02-16 the card's crew is always drawn, the working face only on who acts now.
    expect(tid("px-card-crew-hd")!.getAttribute("title")).toBe("Juno, Forge (working now)");
    expect(tid("px-stat-cost")!.textContent).toContain("of $150 cap");
    expect(tid("px-stat-projects")!.textContent).toContain("3");
    expect(tid("px-card-journey-hd")!.textContent).toBe("OnboardReadyWorkingDeployed");
    expect(document.body.textContent).not.toContain("Helm is reading it");
    await act(async () => { (tid("px-filter-archived") as HTMLElement).click(); });
    await settle();
    expect($$("[data-testid^='px-card-'][data-link]").map((c) => c.dataset.testid)).toEqual(["px-card-old"]);
  });

  it("a never-onboarded project says so and Start onboarding starts it", async () => {
    const calls = engine({ summary: { items, week_cap_usd: null } });
    at("area=projects");
    await mountAs(<ProjectsHome projectId={P} actor="ceo" projects={[]} />);
    await settle(10);
    expect(tid("px-card-hw")!.textContent).toContain("Not onboarded");
    await press(tid("px-card-onboard-hw"));
    expect(calls.some((c) => c.method === "POST" && c.path === "/v1/projects/hw/onboarding/start")).toBe(true);
    // The onboarding filter holds only the project whose onboarding is moving.
    await press(tid("px-filter-onboarding"));
    expect($$("[data-testid^='px-card-'][data-link]").map((c) => c.dataset.testid)).toEqual(["px-card-np"]);
  });
});
