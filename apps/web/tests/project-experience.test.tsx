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
  label4, parseOverview, parseSummary, parseWork, pxHref, readPxUrl, TABS,
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
    expect(parseOverview({ needs_you: [{ id: "x", label: "one two three four five six" }] }).needs_you[0].label.split(" ").length).toBeLessThanOrEqual(5);
  });

  it("reads tabs and drawers from the URL (§10)", () => {
    expect(TABS).toEqual(["overview", "work", "roadmap", "knowledge", "deployments", "health", "settings"]);
    expect(readPxUrl("?area=project&tab=work&drawer=work:wi_1")).toEqual({ tab: "work", firstRun: false, drawer: { kind: "work", id: "wi_1" } });
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
    expect(tid("px-projects-note")!.textContent).toMatch(/next BOSS update/);
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
    expect(tid("px-discover")).not.toBeNull();
    expect(tid("px-guide-you-secrets")!.textContent).toContain("App secret");
    await press(tid("px-guide-start"));
    expect(calls.find((c) => c.method === "POST" && c.path === "/v1/projects/northpeak-retail/work")?.body).toEqual({ kind: "ticket", text: "Fix 6 failing tests" });
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

describe("the Project page header, Needs you and tabs (screen 4)", () => {
  it("has the repo, live URL, health, status, captain with Ask, and New work", async () => {
    engine();
    await openProject();
    expect(tid("px-head-repo")!.getAttribute("href")).toBe("https://github.com/paramg/helpdesk-ai-demo");
    expect(tid("px-head-repo")!.getAttribute("target")).toBe("_blank");
    expect(tid("px-head-live")!.getAttribute("href")).toBe("http://203.0.113.20:8004");
    await press(tid("px-head-health"));
    expect(params().get("tab")).toBe("health");
    await press(tid("px-head-ask"));
    expect(params().get("agent")).toBe(`project:${P}`);
    expect(params().get("dock_role_key")).toBe("engineering_captain");
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
    expect(tid("px-drawer-needs-act")!.getAttribute("href")).toBe("https://github.com/paramg/helpdesk-ai-demo/pull/3");
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
      expect(tid("px-crumbs")!.textContent).toContain("HelpDesk AI Demo");
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
    expect(params().get("dock_ask")).toBe("Plan next week for this project."); // the shell's dock reads it
    expect(params().get("agent")).toBe(`project:${P}`);
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
    expect(tid("px-head-repo")!.getAttribute("href")).toBe("https://github.com/acme/orders");
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
    expect($$("[data-testid='px-drawer-work-steps'] .px-step").length).toBe(6);
    expect(tid("px-drawer-work-merge")!.getAttribute("href")).toContain("/pull/3");
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
  it("Accept makes it work; Reject asks a reason in one tap; Not now snoozes", async () => {
    const calls = engine();
    await openProject("&tab=roadmap");
    expect($$("[data-testid^='px-rec-rec_']").length).toBeGreaterThanOrEqual(4);
    await press(tid("px-rec-accept-rec_weekend"));
    expect(calls.find((c) => c.path === "/v1/ambient/roadmap/rec_weekend/accept")?.body).toMatchObject({ project_id: P, as: "ticket" });
    await press(tid("px-rec-reject-rec_reply"));
    expect((tid("px-rec-reject-confirm-rec_reply") as HTMLButtonElement).disabled).toBe(true);
    await press(tid("px-rec-why-duplicate"));
    await press(tid("px-rec-reject-confirm-rec_reply"));
    expect(calls.find((c) => c.path === "/v1/ambient/roadmap/rec_reply/reject")?.body).toMatchObject({ category: "duplicate" });
    await press(tid("px-rec-snooze-rec_index"));
    expect(calls.some((c) => c.path === "/v1/ambient/roadmap/rec_index/snooze")).toBe(true);
    await press(tid("px-rm-plan-ask"));
    expect(params().get("agent")).toBe(`project:${P}`);
  });
});

describe("Repo & knowledge (screen 6)", () => {
  it("shows the repo's access as a strip, the dossier, the graph snippet and its links", async () => {
    engine();
    await openProject("&tab=knowledge");
    expect($$("[data-testid='px-kn-repos'] .px-fl-n").map((n) => n.textContent)).toEqual(["Read", "Branch", "Open PR", "Merge: you"]);
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
    await press(tid("px-dep-h-dep_2"));
    expect(tid("px-drawer-deploy")).not.toBeNull();
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
  it("shows the stages, what the agent is doing, the checklist with only-you items in amber, and Start", async () => {
    const calls = engine();
    await openProject("", "owner", "northpeak-retail");
    expect(tid("px-onboarding")).not.toBeNull();
    expect($$("[data-testid='px-ob-stages'] .px-ob-cap").map((x) => x.textContent)).toEqual(["Discover", "Ready for work", "Propose backlog", "Work it", "Deploy"]);
    expect(tid("px-ob-now")!.textContent).toContain("Beacon is checking staging port 8004");
    expect(tid("px-ob-item-secrets")!.dataset.s).toBe("you");
    expect(tid("px-ob-act-secrets")!.getAttribute("href")).toContain("area=infra");
    expect(tid("px-needs-ob-secrets")).not.toBeNull(); // the only-you item is also in Needs you
    await press(tid("px-ob-start"));
    expect(calls.some((c) => c.method === "POST" && c.path === "/v1/projects/northpeak-retail/work")).toBe(true);
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
