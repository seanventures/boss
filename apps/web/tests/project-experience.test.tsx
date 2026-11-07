// @vitest-environment happy-dom
/**
 * THE PROJECT EXPERIENCE (BOSSAPP-914), screen by screen, rendered from the contract's fixtures
 * (docs/product/PROJECT_EXPERIENCE_CONTRACT.md, lib/project-experience-fixtures.ts) on the real
 * routes: every button does something real, every link has a target, permissions grey what the
 * viewer can't do, drawers deep-link and close on Esc and Back, the tabs are the URL, ⌘K finds
 * project records, and the first run keeps every Get started item (README parity table).
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
import { JumpResults } from "../app/projects/jump";
import { ProjectsNav } from "../app/projects/nav";
import { ProjectsOnServer } from "../app/infra/host-drawer";
import { projectHealthHref } from "../app/observability/pulse";
import { MovedToProjectSettings } from "../app/projects/moved";
import { revealedPassword } from "../app/projects/tab-settings";
import {
  FX_DEPLOYMENTS, FX_HEALTH, FX_JUMP, FX_KNOWLEDGE, FX_ONBOARDING, FX_OVERVIEW, FX_ROADMAP, FX_SETTINGS, FX_SUMMARY, FX_WORK,
} from "../lib/project-experience-fixtures";
import {
  attemptsPill, label4, parseOverview, parseSummary, parseWork, pxHref, readPxUrl, TABS,
} from "../lib/project-experience";
import { parseResident } from "../lib/infra-view";
import { rulesWire, historyWire } from "./_helpers/alerts-fixtures";
import { $, $$, mountAs, stubFetch, tid, unmount, type Call } from "./_helpers/multirepo-fixtures";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const P = "helpdesk-ai-demo";
const settle = async (n = 6) => { for (let i = 0; i < n; i++) await act(async () => { await new Promise((r) => setTimeout(r, 0)); }); };
const press = async (el: Element | null) => { if (!el) throw new Error("nothing to press"); await act(async () => { (el as HTMLElement).click(); }); await settle(); };
const params = () => new URLSearchParams(window.location.search);
const at = (q: string) => window.history.replaceState(null, "", `/?org=org_1&${q}`);

/** The engine, as the contract says it answers (and the existing routes the screens reuse). */
function engine(over: (c: Call) => { status?: number; body: unknown } | undefined = () => undefined) {
  return stubFetch((c) => {
    const o = over(c);
    if (o) return o;
    if (c.method !== "GET") return { body: { ok: true } };
    const m = c.path.match(/^\/v1\/projects\/([^/]+)\/(overview|work|knowledge|deployments|onboarding|settings|health|captain)$/);
    if (c.path === "/v1/projects/summary") return { body: FX_SUMMARY };
    if (m) {
      const id = m[1];
      const table: Record<string, unknown> = {
        overview: id === "northpeak-retail" ? { ...FX_OVERVIEW, project: { ...FX_OVERVIEW.project, id, name: "NorthPeak Retail", stage: "onboarding" }, needs_you: [] } : FX_OVERVIEW,
        work: FX_WORK, knowledge: FX_KNOWLEDGE, deployments: FX_DEPLOYMENTS,
        onboarding: id === "northpeak-retail" ? FX_ONBOARDING : { ...FX_ONBOARDING, state: "ready" }, settings: FX_SETTINGS, health: FX_HEALTH,
      };
      return { body: table[m[2]] };
    }
    if (c.path === "/v1/ambient/roadmap") return { body: FX_ROADMAP };
    if (c.path === "/v1/jump") return { body: FX_JUMP };
    if (/\/alert-rules$/.test(c.path)) return { body: rulesWire("tgt_staging") };
    if (/\/v1\/apps\/[^/]+\/alerts$/.test(c.path)) return { body: historyWire() };
    if (c.path === "/v1/setup" || c.path === "/api/setup") return { body: setupView(true) };
    return undefined;
  });
}

function setupView(done: boolean) {
  const pill = (tone: string, label: string) => ({ tone, label, tip: "" });
  return {
    project_id: P, ready: done, next: done ? null : "repository", verdict: { label: "", tone: "green", tip: "" }, problems: 0,
    secrets: { backend: "db", needs_consent: false },
    repository: { connection: null, repos: done ? [{ full_name: "paramg/helpdesk-ai-demo", binding_id: "b1", default: true, pill: pill("green", "Works") }] : [] },
    models: done ? [{ key: "claude", label: "Claude", kind: "subscription", pill: pill("green", "Signed in") }] : [],
    detected: [], catalogue: [], jobs: [], budget: {},
    try: { workflow_key: "minor_feature", last_run: done ? { run_id: "wfr_1", status: "done", pr_url: "https://github.com/x/y/pull/1", pr_number: 1, pill: pill("green", "PR #1") } : null },
    github_accounts: done ? [{ login: "paramg", name: "paramg", kind: "personal_signin", pill: pill("green", "Connected") }] : [],
    repositories: done ? [{ full_name: "paramg/helpdesk-ai-demo", binding_id: "b1", in_project: true, default: true }] : [],
  };
}

beforeEach(() => { window.history.replaceState(null, "", "/"); });
afterEach(() => { unmount(); });

// ── the contract client ────────────────────────────────────────────────────────────────────────

describe("the contract client", () => {
  it("parses every route's fixture, and a missing field is an empty value, never a crash", () => {
    const ov = parseOverview(FX_OVERVIEW);
    expect(ov.project.name).toBe("HelpDesk AI Demo");
    expect(ov.journey.map((j) => j.state)).toEqual(["done", "done", "done", "now", "done", "waiting"]);
    expect(ov.tiles.running?.current?.progress).toEqual([3, 6]);
    expect(parseSummary(FX_SUMMARY).items).toHaveLength(3);
    expect(parseWork(FX_WORK).items.find((w) => w.id === "wi_visual")?.links.pr?.number).toBe(3);
    const empty = parseOverview({});
    expect(empty.needs_you).toEqual([]);
    expect(empty.tiles.open_work).toBeNull();
    expect(parseSummary(null).items).toEqual([]);
  });

  it("keeps diagram labels to four words", () => {
    expect(label4("Merge the visual refresh pull request")).toBe("Merge the visual refresh…");
    // Needs-you labels are the engine's short names ("Deploy PR #5 to Staging"): kept whole, never cut with "…".
    expect(parseOverview({ needs_you: [{ id: "x", label: "Deploy PR #5 to Staging" }] }).needs_you[0].label).toBe("Deploy PR #5 to Staging");
  });

  it("reads tabs and drawers from the URL (§10)", () => {
    expect(TABS).toEqual(["overview", "work", "roadmap", "knowledge", "deployments", "health", "settings"]);
    expect(readPxUrl("?area=project&tab=work&drawer=work:wi_1")).toEqual({ tab: "work", firstRun: false, view: null, drawer: { kind: "work", id: "wi_1" } });
    expect(readPxUrl("?tab=nonsense").tab).toBe("overview");
  });

  it("keeps the org and the project on an in-console link, never an API host", () => {
    expect(pxHref("?area=infra&view=fleet", "?org=org_1&project_id=p1&drawer=work:x")).toBe("/?org=org_1&project_id=p1&area=infra&view=fleet");
    expect(pxHref("https://github.com/x/y/pull/3", "?org=o")).toBe("https://github.com/x/y/pull/3");
  });
});

// ── 2 · Projects ─────────────────────────────────────────────────────────────────────────────

describe("Projects (screen 2)", () => {
  it("draws one card per project, needs-you first, and every card and button goes somewhere", async () => {
    engine();
    at("area=projects");
    await mountAs(<ProjectsHome projectId={P} actor="ceo" projects={[]} />);
    await settle();
    const cards = $$("[data-testid^='px-card-'][data-link]");
    expect(cards.map((c) => c.dataset.testid)).toEqual(["px-card-helpdesk-ai-demo", "px-card-northpeak-retail", "px-card-hello-world"]);
    expect(tid("px-card-helpdesk-ai-demo")!.textContent).toContain("2 need you");
    const open = tid("px-card-open-helpdesk-ai-demo") as HTMLAnchorElement;
    expect(new URLSearchParams(open.getAttribute("href")!.split("?")[1]).get("area")).toBe("project");
    // Filters narrow the cards; Add project opens the sheet's Existing door.
    await press(tid("px-filter-onboarding"));
    expect($$("[data-testid^='px-card-'][data-link]").map((c) => c.dataset.testid)).toEqual(["px-card-northpeak-retail"]);
    await press(tid("px-filter-all"));
    await press(tid("px-add-project"));
    expect(params().get("new_project")).toBe("import");
    window.history.replaceState(null, "", "/?area=projects");
    await press(tid("px-card-add"));
    expect(params().get("new_project")).toBe("import");
  });

  it("an engine without §1 still lists the projects (names only) and says why", async () => {
    engine((c) => (c.path === "/v1/projects/summary" ? { status: 404, body: { detail: "Not Found" } } : undefined));
    at("area=projects");
    await mountAs(<ProjectsHome projectId={P} actor="ceo" projects={[{ id: "a", name: "Alpha" }, { id: "b", name: "Beta" }]} />);
    await settle();
    expect(tid("px-projects-note")!.textContent).toMatch(/didn't answer with the project summary/);
    expect(tid("px-card-a")!.textContent).toContain("Alpha");
  });

  it("a viewer sees Add project greyed with the reason", async () => {
    engine();
    at("area=projects");
    await mountAs(<ProjectsHome projectId={P} actor="ceo" projects={[]} />, "viewer");
    await settle();
    expect(tid("px-add-project")!.getAttribute("aria-disabled")).toBe("true");
    await press(tid("px-add-project"));
    expect(params().get("new_project")).toBeNull();
  });
});

// ── 0a / 0b · First run ──────────────────────────────────────────────────────────────────────

describe("the first run (screens 0a, 0b) keeps every Get started item", () => {
  const setupGo = (id: string) => new URLSearchParams(tid(id)!.getAttribute("href")!.split("?")[1]);

  it("0a: a fresh install gets the three steps; GitHub and the model go to Repo and models, never a picker here", async () => {
    engine((c) => (c.path === "/v1/projects/summary" ? { body: { items: [] } } : (c.path === "/v1/setup" || c.path === "/api/setup") ? { body: setupView(false) } : undefined));
    at("area=projects");
    await mountAs(<ProjectsHome projectId={P} actor="ceo" projects={[]} />);
    await settle();
    expect(tid("px-first-run")!.dataset.step).toBe("1");
    expect(tid("px-fr-github")!.dataset.state).toBe("todo");
    expect(tid("px-fr-model")!.dataset.state).toBe("todo");
    // The first step not done is the primary action; each goes to its section of Repo and models.
    expect(tid("px-fr-github-go")!.dataset.kind).toBe("primary");
    expect(tid("px-fr-model-go")!.dataset.kind).toBeUndefined();
    expect([setupGo("px-fr-github-go").get("area"), setupGo("px-fr-github-go").get("focus")]).toEqual(["setup", "repository"]);
    expect([setupGo("px-fr-model-go").get("area"), setupGo("px-fr-model-go").get("focus")]).toEqual(["setup", "models"]);
    expect($$("[data-testid='px-first-run'] select, [data-testid='px-first-run'] [role=listbox], [data-testid='px-first-run'] [role=dialog]")).toEqual([]);
    // Step 3: Existing repo, From an idea, Try a sample (the sheet's doors), One small PR (Get started's quick PR).
    await press(tid("px-fr-existing")); expect(params().get("new_project")).toBe("import");
    await press(tid("px-fr-idea")); expect(params().get("new_project")).toBe("idea");
    await press(tid("px-fr-sample")); expect(params().get("new_project")).toBe("sample");
    expect([setupGo("px-fr-quick-pr").get("area"), setupGo("px-fr-quick-pr").get("focus")]).toEqual(["welcome", "try"]);
    // What happens to each piece of work, as a flow; Repo and models is one link away.
    expect($$("[data-testid='px-fr-flow'] .px-fl-n").map((n) => n.textContent)).toEqual(["Idea", "Plan", "You approve", "Build", "Review", "Pull request", "Live preview"]);
    expect(setupGo("px-fr-setup").get("area")).toBe("setup");
  });

  it("0a: configured steps say so (account · repos, model · mode) with Manage ›; Add a project is then the primary", async () => {
    engine((c) => (c.path === "/v1/projects/summary" ? { body: { items: [] } } : undefined));
    at("area=projects");
    await mountAs(<ProjectsHome projectId={P} actor="ceo" projects={[]} />);
    await settle();
    expect(tid("px-fr-github")!.dataset.state).toBe("configured");
    expect(tid("px-fr-github-done")!.textContent).toMatch(/Connected · paramg · 1 repo/);
    expect(tid("px-fr-model-done")!.textContent).toMatch(/Claude · subscription/);
    expect(tid("px-fr-github-go")).toBeNull();
    expect(tid("px-fr-model-go")).toBeNull();
    expect(setupGo("px-fr-model-manage").get("focus")).toBe("models");
    expect(setupGo("px-fr-github-manage").get("focus")).toBe("repository");
    expect($$("[data-testid='px-fr-strip'] [data-s='done']").map((n) => n.dataset.testid)).toEqual(expect.arrayContaining(["px-fr-strip-github", "px-fr-strip-model"]));
    expect(tid("px-fr-strip-project")!.dataset.s).toBe("now");
    expect(tid("px-fr-existing")!.dataset.kind).toBe("primary");
    expect(tid("px-fr-last-pr")!.getAttribute("href")).toBe("https://github.com/x/y/pull/1");
  });

  it("0a: while the status loads, and when it can't be read, nothing claims Configured", async () => {
    engine((c) => (c.path === "/v1/projects/summary" ? { body: { items: [] } } : c.path === "/api/setup" ? { status: 500, body: { error: "down" } } : undefined));
    at("area=projects");
    await mountAs(<ProjectsHome projectId={P} actor="ceo" projects={[]} />);
    await settle();
    expect(tid("px-first-run")!.dataset.status).toBe("failed");
    for (const id of ["github", "model"]) {
      expect(tid(`px-fr-${id}`)!.dataset.state).toBe("unknown");
      expect(tid(`px-fr-${id}-check`)!.textContent).toBe("Check in Repo and models ›");
      expect(tid(`px-fr-${id}-done`)).toBeNull();
    }
    expect(tid("px-fr-existing")!.dataset.kind).toBe("primary");
  });

  it("0a: coming back from Repo and models re-reads the status (no manual refresh)", async () => {
    let connected = false;
    engine((c) => (c.path === "/v1/projects/summary" ? { body: { items: [] } } : c.path === "/api/setup" ? { body: setupView(connected) } : undefined));
    at("area=projects");
    await mountAs(<ProjectsHome projectId={P} actor="ceo" projects={[]} />);
    await settle();
    expect(tid("px-fr-model")!.dataset.state).toBe("todo");
    connected = true;
    await act(async () => { window.dispatchEvent(new Event("focus")); });
    await settle();
    expect(tid("px-fr-model")!.dataset.state).toBe("configured");
  });

  it("0b: the project's guide starts at Add project; the workspace is a read-only row linking to Setup; no pickers", async () => {
    const calls = engine();
    at("area=projects&first_run=1&project_id=northpeak-retail");
    await mountAs(<ProjectsHome projectId={P} actor="ceo" projects={[]} />);
    await settle();
    expect(tid("px-first-run-guided")).not.toBeNull();
    expect($$("[data-testid='px-guide-steps'] li b").map((b) => b.textContent)).toEqual(["Add project", "Helm onboards", "Ready for work", "First work"]);
    expect(tid("px-guide-ws-github")!.textContent).toContain("GitHub ✓ paramg");
    expect(tid("px-guide-ws-model")!.textContent).toContain("Model ✓ Claude");
    expect(setupGo("px-guide-ws-model").get("focus")).toBe("models");
    expect(tid("px-guide-account")).toBeNull();
    expect(tid("px-guide-model")).toBeNull();
    expect($$("[data-testid='px-first-run-guided'] select, [data-testid='px-first-run-guided'] [role=listbox]")).toEqual([]);
    // 00b-13..19: the live diagram — Helm → Stack found · Mapping code · Tests run → Ready check.
    expect(tid("px-ob-live")).not.toBeNull();
    expect(tid("px-ob-live-ready")).not.toBeNull();
    expect(tid("px-guide-you-secrets")!.textContent).toContain("App secret");
    // Configured workspace chips MANAGE in Repo and models; they never open an add sheet (PX-30).
    expect(setupGo("px-guide-ws-github").get("open")).toBeNull();
    expect(tid("px-guide-marshal")!.getAttribute("title")).toBe("Ranked by Marshal");
    // "Start when ready" waits for Ready (4 of 6 here), then starts the engine's own first work.
    expect((tid("px-guide-start") as HTMLButtonElement).disabled).toBe(true);
    expect(tid("px-guide-start")!.getAttribute("title")).toBe("Starts when Ready is done");
    unmount();
    const after = engine((c) => (c.path === "/v1/projects/northpeak-retail/onboarding"
      ? { body: { ...FX_ONBOARDING, stages: FX_ONBOARDING.stages.map((x) => (x.key === "ready" ? { ...x, state: "done" } : x)) } } : undefined));
    await mountAs(<ProjectsHome projectId={P} actor="ceo" projects={[]} />);
    await settle();
    await press(tid("px-guide-start"));
    // The engine's own start for its first work (§14): the door's kind, text, title and source_ref.
    expect(after.find((c) => c.method === "POST" && c.path === "/v1/projects/northpeak-retail/work")?.body).toEqual({
      kind: "bug", text: "Make the default branch's CI pass again. Failing: 6 tests.", title: "Fix 6 failing tests", source_ref: "ci:main" });
    expect(calls.some((c) => c.method === "POST" && c.path.endsWith("/work"))).toBe(false);
  });

  it("0b: a missing model or GitHub account is an Only-you card that links to Setup, never a picker", async () => {
    engine((c) => (c.path === "/api/setup" ? { body: setupView(false) } : undefined));
    at("area=projects&first_run=1&project_id=northpeak-retail");
    await mountAs(<ProjectsHome projectId={P} actor="ceo" projects={[]} />);
    await settle();
    expect(tid("px-guide-you-model")!.textContent).toContain("Connect a model in Setup ›");
    expect(new URLSearchParams(tid("px-guide-you-model")!.querySelector("a")!.getAttribute("href")!.split("?")[1]).get("focus")).toBe("models");
    expect(new URLSearchParams(tid("px-guide-you-github")!.querySelector("a")!.getAttribute("href")!.split("?")[1]).get("focus")).toBe("repository");
  });
});

// ── 4-13 · the Project page ──────────────────────────────────────────────────────────────────

async function openProject(q = "", role: "owner" | "viewer" = "owner", id = P) {
  at(`project_id=${id}&area=project${q}`);
  await mountAs(<ProjectPage projectId={id} actor="ceo" orgId="org_1" />, role);
  await settle(8);
}

describe("the header's health chip opens Health from every tab (12-24)", () => {
  for (const tab of ["overview", "work", "roadmap", "knowledge", "deployments", "settings", "health"]) {
    it(`from ${tab}`, async () => {
      engine();
      await openProject(`&tab=${tab}`);
      const chip = tid("px-head-health")!;
      expect(chip.tagName).toBe("A");
      expect(new URLSearchParams(chip.getAttribute("href")!.split("?")[1]).get("tab")).toBe("health");
      await press(chip);
      expect(params().get("tab")).toBe("health");
    });
  }
  it("is there before anything is deployed, saying so", async () => {
    engine((c) => (c.path === `/v1/projects/${P}/overview`
      ? { body: { ...FX_OVERVIEW, project: { ...FX_OVERVIEW.project, live_url: "", health: "unknown" }, tiles: { ...FX_OVERVIEW.tiles, staging: null } } } : undefined));
    await openProject("&tab=work");
    expect(tid("px-head-health")!.textContent).toBe("No health yet");
    await press(tid("px-head-health"));
    expect(params().get("tab")).toBe("health");
  });
});

describe("the Project page header, Needs you and tabs (screen 4)", () => {
  it("has the repo, live URL, health, status, captain with Ask, and New work", async () => {
    engine();
    await openProject();
    // X-33: the repo chip opens Repo & knowledge (GitHub is one link away there).
    expect(tid("px-head-repo")!.textContent).toContain("paramg/helpdesk-ai-demo");
    expect(new URLSearchParams(tid("px-head-repo")!.getAttribute("href")!.split("?")[1]).get("tab")).toBe("knowledge");
    expect(tid("px-head-live")!.getAttribute("href")).toBe("http://203.0.113.20:8004");
    await press(tid("px-head-health"));
    expect(params().get("tab")).toBe("health");
    await press(tid("px-head-ask"));
    expect(params().get("drawer")).toBe("captain"); // the captain drawer (screen 09), not the generic dock
    expect(tid("px-drawer-captain")).not.toBeNull();
    window.history.replaceState(null, "", `/?org=org_1&project_id=${P}&area=project`);
    await act(async () => { window.dispatchEvent(new PopStateEvent("popstate")); });
    await settle();
    await press(tid("px-head-new-work"));
    expect([params().get("tab"), params().get("compose")]).toEqual(["work", null]); // compose consumed by the composer
    expect(document.activeElement?.getAttribute("data-testid")).toBe("px-compose-text");
  });

  it("Needs you is on every tab and each item opens its drawer; Esc and Back close it", async () => {
    engine();
    await openProject("&tab=deployments");
    expect(tid("px-needs-bar")!.textContent).toContain("2 need you");
    await press(tid("px-needs-dec_merge3"));
    expect(params().get("drawer")).toBe("needs:dec_merge3");
    expect(tid("px-drawer-needs")).not.toBeNull();
    // It acts in place (PX-05): the primary is named for the action; the PR is a link beside it.
    expect(tid("px-drawer-needs-approve")!.textContent).toContain("Merge PR #3");
    expect(tid("px-drawer-needs-review")!.getAttribute("href")).toBe("https://github.com/paramg/helpdesk-ai-demo/pull/3");
    await act(async () => { document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })); });
    await settle();
    expect(params().get("drawer")).toBeNull();
    expect(tid("px-drawer-needs")).toBeNull();
    await press(tid("px-needs-dec_prodhost"));
    expect(tid("px-drawer-needs")).not.toBeNull();
    await act(async () => { window.history.back(); });
    await settle(10);
    expect(params().get("drawer")).toBeNull();
  });

  it("the seven tabs are the URL; arrow keys move between them", async () => {
    engine();
    await openProject();
    expect($$("[role=tab]").map((t) => t.textContent!.replace(/\d+$/, ""))).toEqual(
      ["Overview", "Work", "Roadmap", "Repo & knowledge", "Deployments", "Health", "Settings"]);
    for (const t of ["work", "roadmap", "knowledge", "deployments", "health", "settings"]) {
      await press(tid(`px-tab-${t}`));
      expect(params().get("tab")).toBe(t);
      expect(tid(`px-panel-${t}`)).not.toBeNull();
      expect(tid("px-crumbs")).toBeNull(); // the top bar carries the breadcrumb (wireframe 04)
    }
    await act(async () => { tid("px-tabs")!.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true })); });
    await settle();
    expect(params().get("tab")).toBeNull(); // wraps to Overview
  });
});

describe("Overview (screen 4)", () => {
  it("draws the journey, four tiles, Needs you with its why, captain, recent work, what's next and try it — each a link", async () => {
    const calls = engine();
    await openProject();
    expect($$("[data-testid^='px-journey-']").length).toBe(6);
    await press(tid("px-journey-working"));
    expect(params().get("tab")).toBe("work");
    window.history.replaceState(null, "", `/?org=org_1&project_id=${P}&area=project`);
    await act(async () => { window.dispatchEvent(new PopStateEvent("popstate")); });
    await press(tid("px-tile-running"));
    expect([params().get("tab"), params().get("drawer")]).toEqual(["work", "work:wi_search"]);
    window.history.replaceState(null, "", `/?org=org_1&project_id=${P}&area=project`);
    await act(async () => { window.dispatchEvent(new PopStateEvent("popstate")); });
    await settle();
    await press(tid("px-tile-staging"));
    expect(params().get("tab")).toBe("health");
    window.history.replaceState(null, "", `/?org=org_1&project_id=${P}&area=project`);
    await act(async () => { window.dispatchEvent(new PopStateEvent("popstate")); });
    await settle();
    expect(tid("px-ov-ny-act-dec_merge3")!.getAttribute("href")).toContain("/pull/3");
    await press(tid("px-ov-work-wi_visual"));
    expect(params().get("drawer")).toBe("work:wi_visual");
    await press(tid("px-ov-accept-rec_weekend"));
    expect(calls.some((c) => c.method === "POST" && c.path === "/v1/ambient/roadmap/rec_weekend/accept")).toBe(true);
    await press(tid("px-ov-plan-week"));
    expect(params().get("drawer")).toBe("captain"); // the captain drawer sends it once (screen 09)
    expect(tid("px-drawer-captain")).not.toBeNull();
    expect(tid("px-ov-try-url")!.getAttribute("href")).toBe("http://203.0.113.20:8004");
  });

  it("an engine without §2 builds the page from the existing start view", async () => {
    engine((c) => {
      if (/^\/v1\/projects\/[^/]+\/(overview|work)$/.test(c.path)) return { status: 404, body: { detail: "Not Found" } };
      if (c.path === `/v1/projects/${P}/start`) {
        return { body: { project_id: P, name: "Order service", repo: { full_name: "acme/orders", branch: "main" },
                         work: [{ run_id: "wfr_1", title: "Add tests", workflow: "Minor", status: "waiting", state: "", tone: "amber", action: { kind: "approve", label: "Review" } }], next: [] } };
      }
      return undefined;
    });
    await openProject();
    expect(tid("px-project-note")).not.toBeNull();
    expect($("h1")!.textContent).toBe("Order service");
    expect(tid("px-head-repo")!.textContent).toContain("acme/orders");
    expect(tid("px-needs-bar")!.textContent).toContain("1 needs you");
  });

  it("a failed read says so and retries", async () => {
    let n = 0;
    engine((c) => (c.path === `/v1/projects/${P}/overview` && n++ === 0 ? { status: 500, body: { detail: "boom" } } : undefined));
    await openProject();
    expect(tid("px-error")!.textContent).toContain("boom");
    await press(tid("px-retry"));
    await settle(6);
    expect(tid("px-overview")).not.toBeNull();
  });
});

describe("Work (screens 5, 5b)", () => {
  it("New work posts §3 and the rows open their drawer with every link", async () => {
    const calls = engine();
    await openProject("&tab=work");
    await press(tid("px-compose-bug"));
    const box = tid("px-compose-text") as HTMLInputElement;
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(box, "SLA ignores weekends");
      box.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await press(tid("px-compose-start"));
    expect(calls.find((c) => c.method === "POST" && c.path === `/v1/projects/${P}/work`)?.body).toEqual({ kind: "bug", text: "SLA ignores weekends" });
    expect(tid("px-said")!.textContent).toMatch(/Started/);
    expect(tid("px-work-pr-wi_visual")!.getAttribute("href")).toBe("https://github.com/paramg/helpdesk-ai-demo/pull/3");
    expect(tid("px-work-run-wi_visual")!.getAttribute("href")).toContain("area=delivery_workflows");
    await press(tid("px-work-act-wi_visual"));
    expect(tid("px-drawer-work")).not.toBeNull();
    // The real steps only (a skipped station is not a step), each with its face (screen 05b).
    expect($$("[data-testid='px-drawer-work-steps'] .px-step").length).toBe(6);
    expect(tid("px-drawer-work-merge")!.textContent).toContain("Merge PR #3");
    expect(tid("px-drawer-link-build")!.getAttribute("href")).toContain("area=code_tasks");
    await press(tid("px-work-board"));
    expect(tid("px-work-columns")).not.toBeNull();
  });

  it("on an engine without §3's POST it uses the project's start door, with the checklist and Start anyway", async () => {
    let refused = true;
    const calls = engine((c) => {
      if (c.method === "POST" && c.path === `/v1/projects/${P}/work`) return { status: 404, body: { detail: "Not Found" } };
      if (c.path === `/v1/projects/${P}/start`) return { body: { project_id: P, name: "HelpDesk", repo: { full_name: "paramg/helpdesk-ai-demo", branch: "main" }, start: { workflow_key: "minor_feature" }, work: [], next: [] } };
      if (c.method === "POST" && c.path === "/api/delivery-workflows") {
        if (refused) { refused = false; return { status: 409, body: { error: "not ready", readiness: { ready: false, checked: [], missing: [{ key: "secret", label: "A secret", fix: "Add it", ok: false, needed_by: [] }] } } }; }
        return { body: { run_id: "wfr_9" } };
      }
      return undefined;
    });
    await openProject("&tab=work");
    const box = tid("px-compose-text") as HTMLInputElement;
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(box, "Add CSV export");
      box.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await press(tid("px-compose-start"));
    expect(tid("px-compose-readiness")).not.toBeNull();
    expect(tid("px-compose-start")!.textContent).toContain("Start anyway");
    const start = calls.find((c) => c.method === "POST" && c.path === "/api/delivery-workflows");
    expect((start!.body as { action: string; project_id: string }).project_id).toBe(P);
  });

  it("a viewer cannot start work: Start and Start now are greyed", async () => {
    engine();
    await openProject("&tab=work", "viewer");
    expect(tid("px-compose-start")!.getAttribute("aria-disabled")).toBe("true");
    expect(tid("px-work-start-wi_email")!.getAttribute("aria-disabled")).toBe("true");
    expect(tid("px-head-new-work")!.getAttribute("aria-disabled")).toBe("true");
  });

  it("a build, its retries and follow-ups are ONE row: clean title, attempt pill, Stalled since, never Running", async () => {
    engine();
    await openProject("&tab=work");
    // One row for the whole chain; no attempt is a row of its own.
    expect(tid("px-work-crun_aml")).not.toBeNull();
    for (const id of ["crun_aml_f3", "crun_aml_f2", "crun_aml_r1"]) expect(tid(`px-work-${id}`)).toBeNull();
    const row = tid("px-work-crun_aml")!;
    expect(tid("px-work-open-crun_aml")!.textContent).toBe("AML flagged line by kind");
    expect(row.textContent).not.toContain("Follow-up: checks failing");
    // Human words, never the raw source.
    expect(row.textContent).toContain("Build");
    expect(row.textContent).not.toContain("code_task");
    expect(tid("px-work-stalled-crun_aml")!.textContent).toMatch(/^Stalled · since /);
    expect(row.getAttribute("data-s")).toBe("stalled");
    // Counted as Stalled, not Running; the chips match the rows.
    expect(tid("px-work-f-stalled")!.textContent).toContain("1");
    expect(tid("px-work-f-running")!.textContent).toContain("1");
    await press(tid("px-work-f-running"));
    expect(tid("px-work-crun_aml")).toBeNull();
    await press(tid("px-work-f-stalled"));
    expect($$("[data-testid='px-work-rows'] .px-wk").length).toBe(1);
    await press(tid("px-work-f-all"));
    // The pill expands the attempts, newest first, each labelled and openable.
    const pill = tid("px-work-attempts-crun_aml")!;
    expect(pill.textContent).toContain("7 attempts");
    expect(pill.getAttribute("aria-expanded")).toBe("false");
    expect((tid("px-work-attempts-list-crun_aml") as HTMLElement).hidden).toBe(true);
    await press(pill);
    expect(pill.getAttribute("aria-expanded")).toBe("true");
    expect((tid("px-work-attempts-list-crun_aml") as HTMLElement).hidden).toBe(false);
    const labels = $$("[data-testid='px-work-attempts-list-crun_aml'] .px-att b").map((b) => b.textContent);
    expect(labels).toEqual(["Follow-up 3 · checks failing", "Follow-up 2 · retry 1", "Follow-up 2 · checks failing",
                            "Follow-up 1 · retry 1", "Follow-up 1 · checks failing", "Retry 1", "Build"]);
    expect(tid("px-attempt-crun_aml_f3")!.textContent).toContain("Stalled");
    expect(tid("px-attempt-crun_aml_f2")!.textContent).toContain("Handed on");
    expect(tid("px-attempt-pr-crun_aml_r1")!.getAttribute("href")).toContain("/pull/6");
    // An attempt opens as itself in the drawer.
    await press(tid("px-attempt-open-crun_aml_r1"));
    expect(params().get("drawer")).toBe("work:crun_aml_r1");
    expect(tid("px-drawer-work")!.textContent).toContain("Retry 1");
    expect(tid("px-drawer-link-build")!.getAttribute("href")).toContain("build=crun_aml_r1");
  });

  it("the group's drawer says it is stalled and lists every attempt; the board shows the pill", async () => {
    engine();
    await openProject("&tab=work&drawer=build:crun_aml");
    expect(tid("px-drawer-work-stalled")!.textContent).toMatch(/Stalled since/);
    expect($$("[data-testid='px-drawer-work-attempts'] .px-li").length).toBe(7);
    unmount();
    engine();
    await openProject("&tab=work");
    await press(tid("px-work-board"));
    expect(tid("px-work-columns")!.className).toContain("px-g5");
    expect(tid("px-board-attempts-crun_aml")!.textContent).toContain("7 attempts");
  });

  it("the attempt pill says follow-ups when there are only follow-ups", () => {
    expect(attemptsPill({ attempt_count: 3, followups: 2, retries: 0 })).toBe("↻ 2 follow-ups");
    expect(attemptsPill({ attempt_count: 2, followups: 1, retries: 0 })).toBe("↻ 1 follow-up");
    expect(attemptsPill({ attempt_count: 4, followups: 2, retries: 1 })).toBe("4 attempts");
    expect(attemptsPill({ attempt_count: 1, followups: 0, retries: 0 })).toBe("");
  });

  it("a drawer opened from a link lands open (deep link) and a stale one says so", async () => {
    engine();
    await openProject("&tab=work&drawer=work:wi_ui");
    expect(tid("px-drawer-work")!.textContent).toContain("UI refresh");
    unmount();
    engine();
    await openProject("&tab=work&drawer=work:gone");
    expect(tid("px-drawer-missing")).not.toBeNull();
    await press(tid("px-drawer-missing-close"));
    expect(params().get("drawer")).toBeNull();
  });
});

describe("Roadmap (screen 7)", () => {
  it("Accept makes it work; Reject asks a reason in one tap; Snooze is in the ⋯ menu", async () => {
    const calls = engine();
    await openProject("&tab=roadmap");
    expect($$("[data-testid^='px-rec-rec_']").length).toBeGreaterThanOrEqual(4);
    await press(tid("px-rec-accept-rec_weekend"));
    expect(calls.find((c) => c.path === "/v1/ambient/roadmap/rec_weekend/accept")?.body).toMatchObject({ project_id: P, as: "ticket" });
    await press(tid("px-rec-reject-rec_reply"));
    // One tap, never silent (round 3, tester 07-14): no reason is preselected; Reject waits for one.
    expect(tid("px-rec-why-not_valuable")!.getAttribute("aria-checked")).toBe("false");
    expect((tid("px-rec-reject-confirm-rec_reply") as HTMLButtonElement).disabled).toBe(true);
    await press(tid("px-rec-why-duplicate"));
    await press(tid("px-rec-reject-confirm-rec_reply"));
    expect(calls.find((c) => c.path === "/v1/ambient/roadmap/rec_reply/reject")?.body).toMatchObject({ category: "duplicate" });
    await press(tid("px-rec-more-rec_index")); // snooze lives in the card's ⋯ menu
    await press(tid("px-rec-snooze-rec_index"));
    await press(tid("px-rec-snooze-confirm-rec_index")); // how long (2 weeks by default) and why, then Snooze
    expect(calls.some((c) => c.path === "/v1/ambient/roadmap/rec_index/snooze")).toBe(true);
    await press(tid("px-rm-plan-ask"));
    expect(params().get("drawer")).toBe("captain"); // asking the captain opens its drawer (screen 09)
  });
});

describe("Repo & knowledge (screen 6)", () => {
  it("shows the repo's access as a strip, the dossier, the graph snippet and its links", async () => {
    engine();
    await openProject("&tab=knowledge");
    expect($$("[data-testid='px-kn-repos'] .px-fl-n").map((n) => n.textContent)).toEqual(["Read", "Branch", "Open PR", "Merge: you", "Staging"]);
    expect(tid("px-kn-dossier")!.textContent).toContain("142 / 148");
    expect(tid("px-kn-open-graph")!.getAttribute("href")).toContain("area=knowledge");
    expect(tid("px-kn-add-repo")!.getAttribute("href")).toContain("focus=repository");
  });
});

describe("Deployments (screen 8) and the Infra cross-links (screen 14)", () => {
  it("environment cards open the app and say which server they run on, in Infra", async () => {
    engine();
    await openProject("&tab=deployments");
    expect(tid("px-env-url-staging")!.getAttribute("href")).toBe("http://203.0.113.20:8004");
    const host = tid("px-env-host-staging")!.getAttribute("href")!;
    expect(new URLSearchParams(host.split("?")[1]).get("area")).toBe("infra");
    expect(new URLSearchParams(host.split("?")[1]).get("host")).toBe("h_202");
    expect(tid("px-dep-prod-act")!.getAttribute("href")).toContain("area=infra");
    // A history row opens that deploy (its href; Infra's deploy view on real data).
    expect(tid("px-dep-h-dep_2")!.tagName).toBe("A");
    expect(tid("px-dep-h-dep_2")!.getAttribute("href")).toContain("deploy");
  });

  it("a server in Infra lists the projects on it, each linking to that Project › Deployments", async () => {
    const residents = [
      parseResident({ kind: "target", id: "t1", name: "helpdesk", env: "staging", port: 8004, health: { word: "Healthy", tone: "green" }, project: { id: P, name: "HelpDesk AI Demo" } }),
      parseResident({ kind: "preview", id: "p1", name: "hello", env: "preview", project_id: "hello-world", project_name: "Hello World" }),
      parseResident({ kind: "monitoring", id: "m", name: "Monitoring" }),
    ];
    await mountAs(<ProjectsOnServer residents={residents} />);
    const link = tid(`inf-host-project-link-${P}`)!.getAttribute("href")!;
    const q = new URLSearchParams(link.split("?")[1]);
    expect([q.get("project_id"), q.get("area"), q.get("tab")]).toEqual([P, "project", "deployments"]);
    expect(tid("inf-host-project-hello-world")).not.toBeNull();
  });
});

describe("Health (screen 12) and the Observability cross-links (screen 15)", () => {
  it("keeps Observability's alert rules with every action: Tune, Silence, Respond automatically, New alert rule", async () => {
    engine();
    await openProject("&tab=health");
    expect(tid("px-h-env-staging")!.textContent).toContain("0.4%");
    const rules = tid("px-h-rules-tgt_staging")!;
    expect(rules.querySelector("[data-testid='obs-app-alerts']")).not.toBeNull();
    const ids = new Set([...rules.querySelectorAll("[data-testid]")].map((e) => e.getAttribute("data-testid")));
    for (const action of ["obs-rule-add", "obs-rule-edit", "obs-rule-silence", "obs-rule-respond", "obs-rule-state", "obs-app-sources", "obs-app-history"]) {
      expect(ids, action).toContain(action);
    }
    expect($$("[data-testid='px-h-rules-tgt_staging'] [data-testid^='obs-rule-'][data-state]").length).toBe(5);
    expect(tid("px-h-rules-obs-tgt_staging")!.getAttribute("href")).toContain("area=observability");
    expect(tid("px-h-observability")!.getAttribute("href")).toContain("area=observability");
    expect(tid("px-h-inc-graph-wi_inc1")!.getAttribute("href")).toContain("area=agent_ops");
  });

  it("Observability's apps link to Project › Health", () => {
    window.history.replaceState(null, "", `/?org=org_1&project_id=${P}&area=observability&app=t1`);
    const q = new URLSearchParams(projectHealthHref().slice(1));
    expect([q.get("project_id"), q.get("area"), q.get("tab"), q.get("app")]).toEqual([P, "project", "health", null]);
  });
});

describe("Settings (screen 13): demo logins with user names, reveal behind the step-up", () => {
  it("lists the users; Reveal shows the password for 30s; Copy copies it", async () => {
    const calls = engine((c) => (c.path.endsWith("/demo-logins") ? { body: { demo_logins: [{ name: "DEMO_PASSWORD", value: "horse-42" }] } } : undefined));
    await openProject("&tab=settings");
    expect($$("[data-testid='px-demo-logins'] .px-cred").map((r) => r.dataset.testid)).toEqual([
      "px-login-admin@helpdesk.demo", "px-login-agent@helpdesk.demo", "px-login-customer@helpdesk.demo"]);
    await press(tid("px-login-reveal-admin@helpdesk.demo"));
    expect(calls.some((c) => c.path === "/v1/apps/helpdesk/demo-logins")).toBe(true);
    expect(tid("px-login-password")!.textContent).toBe("horse-42");
    for (const s of ["deploy_values", "targets", "captain", "autonomy", "repos", "notifications"]) expect(tid(`px-set-${s}`), s).not.toBeNull();
  });

  it("a stale second factor opens the step-up sheet, never a raw error", async () => {
    engine((c) => (c.path.endsWith("/demo-logins") ? { status: 401, body: { error: "step_up_required" } } : undefined));
    await openProject("&tab=settings");
    await press(tid("px-login-reveal-agent@helpdesk.demo"));
    await settle(6);
    expect(document.querySelector('[data-testid="step-up-sheet"]')).not.toBeNull();
  });

  it("a viewer cannot reveal or change the captain", async () => {
    engine();
    await openProject("&tab=settings", "viewer");
    expect(tid("px-login-reveal-admin@helpdesk.demo")!.getAttribute("aria-disabled")).toBe("true");
    expect(tid("px-set-captain-change")!.getAttribute("aria-disabled")).toBe("true");
  });

  it("Change captain posts §7", async () => {
    const calls = engine();
    await openProject("&tab=settings");
    await press(tid("px-set-captain-change"));
    // Captain roles first (PX-13); every other agent is behind "Show all agents (N)".
    if (tid("px-set-captain-all")) await press(tid("px-set-captain-all"));
    const juno = [...tid("px-set-captain-pick")!.querySelectorAll("button")].find((b) => b.textContent!.includes("Juno"))!;
    await press(juno);
    expect(calls.find((c) => c.method === "POST" && c.path === `/v1/projects/${P}/captain`)?.body).toEqual({ agent_id: "pm" });
  });

  it("the old homes link here", async () => {
    await mountAs(<MovedToProjectSettings section="demo_logins" />);
    expect(tid("moved-to-project-settings")!.textContent).toContain("Moved to Project › Settings");
    const q = new URLSearchParams(tid("moved-to-project-settings")!.getAttribute("href")!.split("?")[1]);
    expect([q.get("area"), q.get("tab"), q.get("section")]).toEqual(["project", "settings", "demo_logins"]);
    expect(revealedPassword({ password: "x" }, "a")).toBe("x");
    expect(revealedPassword({ demo_logins: [{ user: "b", password: "y" }, { user: "a", password: "z" }] }, "a")).toBe("z");
  });
});

describe("Onboarding in progress (screen 10, §8)", () => {
  it("shows the stages, what the agent is doing, the checklist with only-you items in amber, and a locked Accept backlog", async () => {
    const calls = engine();
    await openProject("", "owner", "northpeak-retail");
    expect(tid("px-onboarding")).not.toBeNull();
    expect($$("[data-testid='px-ob-stages'] .px-ob-cap").map((x) => x.textContent)).toEqual(["Discover", "Ready for work", "Propose backlog", "Work it", "Deploy"]);
    expect(tid("px-ob-now")!.textContent).toContain("Beacon is checking staging port 8004");
    expect(tid("px-ob-item-secrets")!.dataset.s).toBe("you");
    expect(tid("px-ob-act-secrets")!.getAttribute("href")).toContain("area=infra");
    // Screen 10 draws no Needs-you bar: its "Only you can" card is that list (the same items the
    // project's Needs you and the bell carry).
    expect(tid("px-needs-bar")).toBeNull();
    expect(tid("px-ob-you-secrets")).not.toBeNull();
    expect(tid("px-ob-live")).not.toBeNull();
    expect(tid("px-ob-first")).not.toBeNull();
    expect(tid("px-ob-workspace")).not.toBeNull();
    // Accept backlog / Edit unlock only when Ready is complete (4 of 6 here); nothing starts.
    expect((tid("px-ob-accept") as HTMLButtonElement).disabled).toBe(true);
    expect(tid("px-ob-accept")!.getAttribute("title")).toContain("Unlocks when Ready is 6/6");
    expect(calls.some((c) => c.method === "POST" && c.path === "/v1/projects/northpeak-retail/work")).toBe(false);
  });
});

describe("⌘K jump (screen 11, §9) and the rail", () => {
  it("asks §9 and lists project records by group, plus Ask and New work", async () => {
    const calls = engine();
    window.history.replaceState(null, "", `/?org=org_1&project_id=${P}&area=project`);
    const done = vi.fn();
    const open = vi.spyOn(window, "open").mockImplementation(() => null);
    await mountAs(<JumpResults query="pr 3" projectId={P} projects={[{ id: P, name: "HelpDesk AI Demo" }]} onDone={done} />);
    await act(async () => { await new Promise((r) => setTimeout(r, 200)); });
    await settle();
    expect(calls.find((c) => c.path === "/v1/jump")?.search).toContain("project_id=helpdesk-ai-demo");
    expect(tid("palette-jump-pr")!.textContent).toContain("PR #3");
    await press(tid("palette-jump-pr"));
    expect(open).toHaveBeenCalledWith("https://github.com/paramg/helpdesk-ai-demo/pull/3", "_blank", "noopener,noreferrer");
    await press(tid("palette-jump-ask"));
    expect(params().get("agent")).toBe(`project:${P}`);
    expect(tid("palette-jump-new-work")).not.toBeNull();
    open.mockRestore();
  });

  it("the rail lists projects with their state, and Add project opens the sheet", async () => {
    engine();
    window.history.replaceState(null, "", `/?org=org_1&project_id=${P}&area=project`);
    await mountAs(<ProjectsNav items={[]} active="project" activeProjectId={P} onGo={() => {}} open onToggle={() => {}} />);
    await settle();
    expect(tid(`nav-project-${P}`)!.getAttribute("aria-current")).toBe("page");
    expect(tid("nav-project-northpeak-retail")).not.toBeNull();
    await press(tid("nav-add-project"));
    expect(params().get("new_project")).toBe("import");
  });
});

describe("sample mode (?px_demo=1)", () => {
  it("renders the contract's sample, says so, and sends nothing", async () => {
    const calls = engine();
    await openProject("&px_demo=1&tab=work");
    expect(tid("px-demo-banner")).not.toBeNull();
    const box = tid("px-compose-text") as HTMLInputElement;
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(box, "x");
      box.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await press(tid("px-compose-start"));
    expect(tid("px-said")!.textContent).toMatch(/Sample mode/);
    expect(calls.filter((c) => c.method !== "GET")).toEqual([]);
  });
});

// ── the engine's own answers ───────────────────────────────────────────────────────────────────
// The shapes apps/api/app/routers/project_experience.py serves (captured from a seeded database,
// trimmed): every screen must run on THESE, not only on the contract's sample.

const Q = (o: Record<string, string>) => `?${new URLSearchParams({ project_id: P, ...o })}`;
const API = {
  overview: {
    ...FX_OVERVIEW,
    needs_you: [{ id: "onb:secrets", kind: "setup", label: "App secret", detail: "OPENAI_API_KEY", mini: [], action: { label: "Add", href: Q({ area: "project", tab: "settings", drawer: "section:deploy_values" }), decision_id: null } }],
    next: [
      { id: "wi_rec1", label: "Email-to-ticket intake", detail: "Customers email support today.", source: "roadmap", state: "next", href: Q({ area: "project", tab: "roadmap", drawer: "rec:wi_rec1" }),
        accept: { href: "/v1/ambient/roadmap/wi_rec1/accept", method: "POST", body: { as: "ticket", project_id: P } } },
      { id: "cand_1", label: "Fix 2 failing checks", source: "ci", state: "proposed", href: Q({ area: "project", tab: "roadmap" }),
        accept: { href: `/v1/projects/${P}/work`, method: "POST", body: { kind: "bug", text: "Make CI pass again.", title: "Fix 2 failing checks", source_ref: "ci:main" } } },
    ],
    week: [{ at: "2026-11-06T10:00:00Z", kind: "pr", label: "PR #5 opened", external_url: "https://github.com/acme/helpdesk-ai-demo/pull/5", href: Q({ area: "code_tasks", view: "apps", build: "crun_5" }) }],
  },
  work: { items: [{ ...FX_WORK.items[0], id: "wfr_run1", href: Q({ area: "project", tab: "work", drawer: "run:wfr_run1" }),
                    links: { ...FX_WORK.items[0].links, decision: Q({ area: "decisions", decision: "dec_1" }) } }], counts: {} },
  knowledge: {
    repos: [{ full_name: "acme/helpdesk-ai-demo", branch: "main", access: { read: true, write: true, pr: true }, external_url: "https://github.com/acme/helpdesk-ai-demo" }],
    dossier: { stack: ["JavaScript"], commands: { build: null, test: null, run: "pnpm start" }, tests: { files: 2 },
               hotspots: [{ path: "server/sla.mjs", label: "Sla", why: "40 lines changed in 180 days, 120 lines, no tests", has_tests: false }],
               built_at: "2026-11-06T10:00:00Z", href: Q({ area: "project", tab: "knowledge", drawer: "dossier" }) },
    graph: { nodes: [], edges: [], total_nodes: 412, href: Q({ area: "knowledge" }) },
    docs: [{ label: "README", kind: "readme", detail: "README.md", external_url: "https://github.com/acme/helpdesk-ai-demo/blob/HEAD/README.md", href: Q({ area: "project", tab: "knowledge", drawer: "doc:readme" }) }],
    decisions: [],
  },
  deployments: {
    environments: [{ key: "staging", label: "Staging", app: "helpdesk-ai-demo", target_id: "dept_1", url: "http://203.0.113.10:8004", health: "healthy", status: "up",
                     uptime_pct: 100, p95_ms: 204, host: "203.0.113.10", host_id: "dhost_1", host_href: Q({ area: "infra", host: "dhost_1" }),
                     last_deploy: { commit: "f3f130c", status: "healthy", at: "2026-11-04T10:00:00Z", by: "ceo", href: Q({ area: "infra", view: "deploys", deploy: "sdep_1" }) },
                     demo_logins: [], policy: "person", href: Q({ area: "project", tab: "deployments", drawer: "env:dept_1" }), manage_href: Q({ area: "infra", target: "dept_1" }) }],
    production: { state: "none", action: { label: "Set up production", href: Q({ area: "infra", add: "target" }) } },
    ship_path: [{ label: "PR merged", state: "done" }, { label: "Image built", state: "done" }, { label: "Staging · you approve", state: "done" },
                { label: "Smoke test", state: "done" }, { label: "Prod · you approve", state: "waiting" }],
    history: [{ id: "sdep_1", label: "Staging f3f130c", env: "staging", status: "healthy", commit: "f3f130c", by: "ceo", at: "2026-11-04T10:00:00Z", href: Q({ area: "infra", view: "deploys", deploy: "sdep_1" }) },
              { id: "sdep_0", label: "Staging a1b2c3d", env: "staging", status: "failed", commit: "a1b2c3d", by: "ceo", at: "2026-11-03T10:00:00Z", href: Q({ area: "infra", view: "deploys", deploy: "sdep_0" }) }],
  },
  settings: { sections: [
    { key: "targets", label: "Deploy targets", icon: "server",
      items: [{ kind: "target", id: "dept_1", app: "helpdesk-ai-demo", env: "staging", env_label: "Staging", host: "203.0.113.10", host_name: "demo-host",
                host_href: Q({ area: "infra", host: "dhost_1" }), words: "docker · port 8004", state: "done", policy: "person", paused: false, href: Q({ area: "infra", target: "dept_1" }),
                actions: [{ label: "Edit", method: "PATCH", href: "/v1/deployment-targets/dept_1", open: Q({ area: "infra", target: "dept_1" }) }] },
              { kind: "missing", id: "production", env: "production", env_label: "Production", words: "no host yet", state: "you",
                action: { label: "Pick host", open: Q({ area: "infra", add: "target" }) } },
              { kind: "host", id: "dhost_1", name: "demo-host", address: "203.0.113.10", status: "verified", shared: false, href: Q({ area: "infra", host: "dhost_1" }) }],
      actions: [{ label: "Add a target", method: "POST", href: "/v1/deployment-targets", requires: "step_up", open: Q({ area: "infra", add: "target" }) },
                { label: "Add a server", method: "POST", href: "/v1/deployment-hosts", open: Q({ area: "infra", add: "host" }) }] },
    { key: "repos", label: "Repositories", icon: "git-branch",
      items: [{ full_name: "acme/helpdesk-ai-demo", branch: "main", primary: true, external_url: "https://github.com/acme/helpdesk-ai-demo", href: "https://github.com/acme/helpdesk-ai-demo",
                actions: [{ label: "Remove", method: "DELETE", href: `/v1/projects/${P}/repos/ghb_1`, open: Q({ area: "setup", focus: "repository" }) }] }],
      actions: [{ label: "Add a repository", method: "PUT", href: `/v1/projects/${P}/repos/{repo_id}`, open: Q({ area: "setup", focus: "repository", open: "add" }) },
                { label: "Repository mode", method: "PUT", href: `/v1/projects/${P}/repo-mode`, open: Q({ area: "setup", focus: "repository" }) }] },
    { key: "autonomy", label: "Autonomy & budget", icon: "gauge", items: [], ambient: { daily_cap_usd: 5, max_concurrent: 2 }, budget: null,
      actions: [{ label: "Ambient budget", method: "PUT", href: "/v1/ambient/settings", open: Q({ area: "budget", tab: "autonomy" }) }] },
  ] },
  onboarding: { state: "not_started", run_id: null, stages: [{ key: "discover", label: "Discover", state: "todo", sub: "" }], discover: { nodes: [] },
                checklist: [], only_you: [], first_work: null, backlog: [], elapsed_s: 0, start: { href: `/v1/projects/${P}/onboarding/start`, method: "POST" } },
};
function realEngine(over: (c: Call) => { status?: number; body: unknown } | undefined = () => undefined) {
  return engine((c) => {
    const o = over(c);
    if (o) return o;
    const m = c.method === "GET" ? c.path.match(/^\/v1\/projects\/[^/]+\/(overview|work|knowledge|deployments|settings)$/) : null;
    if (m) return { body: (API as Record<string, unknown>)[m[1]] };
    if (c.method === "GET" && c.path === `/v1/projects/${P}/onboarding`) return { body: { ...API.onboarding, state: "ready" } };
    return undefined;
  });
}

describe("the engine's own answers (what the API serves, not the sample)", () => {
  it("Settings never links to an API route: each action opens its console home; rows open theirs", async () => {
    realEngine();
    await openProject("&tab=settings");
    const hrefs = $$("[data-testid='px-settings'] a").map((a) => a.getAttribute("href") ?? "");
    expect(hrefs.filter((h) => h.startsWith("/v1/"))).toEqual([]);
    // Screen 13: "Staging ✓ docker · port 8004" with its server's Infra drawer; Production "! no host yet · Pick host".
    expect(tid("px-set-target-staging")!.textContent).toContain("docker · port 8004");
    expect(new URLSearchParams(tid("px-set-host-staging")!.getAttribute("href")!.split("?")[1]).get("host")).toBe("dhost_1");
    expect(tid("px-set-target-production")!.textContent).toContain("no host yet");
    expect(new URLSearchParams(tid("px-set-pick-host")!.getAttribute("href")!.split("?")[1]).get("add")).toBe("target");
    expect(tid("px-set-repos")!.textContent).toContain("acme/helpdesk-ai-demo");
    expect(tid("px-set-autonomy")!.textContent).toContain("$5/day");
  });

  it("the engine's drawer links open: run:<id> is the work drawer, env:<target> the environment, section:<key> is Settings", async () => {
    realEngine();
    await openProject("&tab=work&drawer=run:wfr_run1");
    expect(tid("px-drawer-work")).not.toBeNull();
    // One action (05b): with a PR it is "Merge PR #n", which approves the merge question in BOSS.
    expect(new URLSearchParams((tid("px-drawer-work-decide") ?? tid("px-drawer-work-merge"))!.getAttribute("href")!.split("?")[1]).get("decision")).toBe("dec_1");
    unmount(); realEngine();
    await openProject("&tab=deployments&drawer=env:dept_1");
    expect(tid("px-drawer-env")).not.toBeNull();
    expect(new URLSearchParams(tid("px-drawer-env-host")!.getAttribute("href")!.split("?")[1]).get("host")).toBe("dhost_1");
    unmount(); realEngine();
    await openProject("&tab=knowledge&drawer=section:repos");
    expect(params().get("tab")).toBe("settings");
    expect(params().get("section")).toBe("repos");
    expect(params().get("drawer")).toBeNull();
    unmount(); realEngine();
    // The wireframes draw no onboarding drawer: the old link opens screen 10's own page.
    await openProject("&drawer=onboarding");
    expect(params().get("tab")).toBe("onboarding");
    expect(params().get("drawer")).toBeNull();
    expect(tid("px-onboarding")).not.toBeNull();
    expect(tid("px-tabs")).toBeNull();
  });

  it("What's next accepts each item its own way: a roadmap item is planned, a proposed one starts through the door", async () => {
    const calls = realEngine();
    await openProject();
    await press(tid("px-ov-accept-wi_rec1"));
    expect(calls.find((c) => c.method === "POST" && c.path === "/v1/ambient/roadmap/wi_rec1/accept")?.body).toMatchObject({ as: "ticket", project_id: P });
    await press(tid("px-ov-accept-cand_1"));
    expect(calls.find((c) => c.method === "POST" && c.path === `/v1/projects/${P}/work`)?.body).toEqual({
      kind: "bug", text: "Make CI pass again.", title: "Fix 2 failing checks", source_ref: "ci:main" });
    // This week's events open their record.
    expect(tid("px-ov-week-0")!.querySelector("a")!.getAttribute("href")).toContain("build=crun_5");
  });

  it("Repo & knowledge reads the engine's counts: graph size, test files, hotspots, docs on GitHub", async () => {
    realEngine();
    await openProject("&tab=knowledge");
    expect(tid("px-kn-graph")!.textContent).toContain("412 nodes");
    expect(tid("px-kn-graph")!.textContent).not.toContain("rules");
    expect(tid("px-kn-test-files")!.textContent).toContain("2");
    expect(tid("px-kn-dossier")!.textContent).toContain("no tests");
    expect(tid("px-kn-dossier")!.textContent).not.toContain("Build");
    // A doc opens in its drawer (`doc:readme`), which links to where it lives on GitHub.
    await press(tid("px-kn-doc-0"));
    expect(params().get("drawer")).toBe("doc:readme");
    expect(tid("px-drawer-doc-open")!.getAttribute("href")).toBe("https://github.com/acme/helpdesk-ai-demo/blob/HEAD/README.md");
  });

  it("Deployments: the ship path, a failed deploy in the history, the server's own Infra drawer", async () => {
    realEngine();
    await openProject("&tab=deployments");
    expect(tid("px-dep-ship")!.textContent).toContain("Prod · you approve");
    expect(tid("px-dep-h-sdep_0")!.textContent).toContain("Failed");
    expect(new URLSearchParams(tid("px-env-host-staging")!.getAttribute("href")!.split("?")[1]).get("host")).toBe("dhost_1");
    expect(tid("px-dep-prod")!.textContent).toContain("Pick a host for production");
    await press(tid("px-env-open-staging"));
    expect(params().get("drawer")).toBe("env:dept_1");
    expect(tid("px-drawer-env")).not.toBeNull();
  });

  it("an onboarding item waiting on you is listed once in Needs you, never twice", async () => {
    realEngine((c) => (c.method === "GET" && c.path === `/v1/projects/${P}/onboarding`
      ? { body: { ...FX_ONBOARDING, state: "needs_you" } } : undefined));
    await openProject("", "owner");
    expect($$("[data-testid='px-needs-bar'] .px-needs-item").filter((x) => /App secret/.test(x.textContent ?? ""))).toHaveLength(1);
  });

  it("an onboarding that never started offers Start onboarding, which posts §8's start", async () => {
    const calls = realEngine((c) => {
      if (c.method === "GET" && c.path === `/v1/projects/${P}/onboarding`) return { body: API.onboarding };
      if (c.method === "GET" && c.path === `/v1/projects/${P}/overview`) return { body: { ...API.overview, project: { ...FX_OVERVIEW.project, stage: "onboarding" } } };
      return undefined;
    });
    await openProject();
    expect(tid("px-onboarding")).not.toBeNull();
    await press(tid("px-ob-restart"));
    expect(calls.some((c) => c.method === "POST" && c.path === `/v1/projects/${P}/onboarding/start`)).toBe(true);
  });

  it("New work the project isn't ready for shows its checklist; Start anyway re-sends with the ticks", async () => {
    const need = { key: "deploy_target", label: "Somewhere to deploy", fix: "Add a target", area: "infra", ok: false, detail: "", needed_by: [], first_step_label: "Deploy" };
    const readiness = { ready: false, checked: [need], missing: [need] };
    let refused = false;
    const calls = realEngine((c) => {
      if (c.method === "POST" && c.path === `/v1/projects/${P}/work` && !refused) {
        refused = true;
        return { status: 409, body: { detail: { message: "This project isn't ready for that workflow.", readiness } } };
      }
      return undefined;
    });
    await openProject("&tab=work");
    const input = tid("px-compose-text") as HTMLInputElement;
    await act(async () => {
      const set = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;
      set.call(input, "Add CSV export");
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await press(tid("px-compose-start"));
    expect(tid("px-compose-readiness")).not.toBeNull();
    await press(tid("dw-readiness-start-anyway"));
    await press(tid("px-compose-start"));
    const posts = calls.filter((c) => c.method === "POST" && c.path === `/v1/projects/${P}/work`);
    expect(posts).toHaveLength(2);
    expect((posts[1].body as { start_anyway?: string[] }).start_anyway).toEqual(["deploy_target"]);
  });
});
