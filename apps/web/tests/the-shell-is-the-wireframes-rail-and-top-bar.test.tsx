// @vitest-environment happy-dom
/**
 * THE SHELL IS THE WIREFRAMES' RAIL AND TOP BAR (BOSSAPP-913/914, wireframes 01/02/04/11; operator
 * walk 2026-11-06: "the UI is not working at all").
 *
 *  - The rail shows EVERY area at every height: twelve projects with long names pushed every area
 *    after Code Tasks off a 900px rail. The project list is capped at five (needs-you first, then
 *    recent, the open one always in), then "All projects (N) ›" and "+ Add project".
 *  - The top bar is breadcrumb · Jump to… ⌘K · Live · + · bell · avatar. Every control of the old
 *    bar is still reachable (the parity list below).
 *  - The "Project: X ← Back to Y" banner is gone: the breadcrumb says the scope.
 */
import React, { act, type ReactNode } from "react";
import { existsSync, readFileSync } from "node:fs";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const router = vi.hoisted(() => ({ refresh: vi.fn(), replace: vi.fn(), push: vi.fn() }));
vi.mock("next/navigation", () => ({
  useRouter: () => router,
  useSearchParams: () => new URLSearchParams(window.location.search),
}));
vi.mock("../lib/use-agent-identities", () => ({ useLoadIdentities: () => undefined, useIdentityVersion: () => 0 }));
vi.mock("../lib/agents-live", () => ({
  useAttention: () => ({ count: 3, items: [{ id: "dec_1", title: "Merge PR #3", at: "", roleKey: null }] }),
  useLivePresence: () => new Map(),
}));
vi.mock("../app/agent-ui", () => ({ AgentChat: () => null }));
vi.mock("../app/project-strip", () => ({ ProjectStrip: () => null }));
vi.mock("../app/agents/converse-host", () => ({ ConverseHost: () => null }));
vi.mock("../components/identity/consent-sheet", () => ({ ConsentHost: () => null }));
vi.mock("../components/identity/identity-provider", () => {
  const allowed = { allowed: true, reason: "", needsStepUp: false };
  const identity = { me: null, check: () => allowed, stub: false, mirrored: true };
  return {
    IdentityProvider: ({ children }: { children: ReactNode }) => children,
    useIdentity: () => identity,
    useCan: () => allowed,
  };
});

import ConsoleShell from "../app/ConsoleShell";
import { crumbsFor } from "../app/shell-crumbs";
import { railProjects, RAIL_PROJECTS_MAX, type RailProject } from "../app/projects/nav";

const NAMES = ["HelpDesk AI Demo", "NorthPeak Lending (console)", "northpeak-bank-demo", "BOSS dogfood bootstrap",
  "northpeak-bank-a", "northpeak-bank-ap", "northpeak-bank-b", "NorthPeak Bank", "NorthPeak Lending",
  "BOSS platform", "HelpDesk dry run", "HelpDesk AI"];
const id = (n: string) => n.toLowerCase().replace(/[^a-z0-9]+/g, "-");
const CARDS = NAMES.map((name, i) => ({
  id: id(name), name, initials: "XX", repo: "acme/x", stage: i === 3 ? "onboarding" : "working", journey: [],
  needs_you: i === 0 ? 2 : i === 11 ? 1 : 0, running: i === 2 ? 1 : 0, health: "unknown", live_url: "", captain: null,
  cost_week: 0, series: [], updated_at: `2026-11-0${(i % 6) + 1}T10:00:00Z`, href: "",
}));
// The open project is old and quiet: it would not make the top five on its own.
const OPEN = id("BOSS dogfood bootstrap");

let host: HTMLDivElement;
let root: Root;
const $ = (sel: string) => host.querySelector<HTMLElement>(sel);
const $$ = (sel: string) => [...host.querySelectorAll<HTMLElement>(sel)];

function Shell({ area }: { area: string }) {
  return <ConsoleShell initialArea={area} loadedAreas={[area]}
    scope={{ project: "BOSS dogfood bootstrap", actor: "Param", projectId: OPEN }}
    org={{ id: "org_boss", name: "BOSS" }}
    projects={{ items: CARDS.map((c) => ({ id: c.id, name: c.name })), homeId: id("BOSS platform") }}
    status={{ apiReachable: true, budgetState: "green", deadletters: 0, pendingDecisions: 2 }}
    signOutAvailable>
    <section data-area={area}>{area}</section>
  </ConsoleShell>;
}

async function mount(area: string, search = "") {
  window.history.replaceState(null, "", `/?org=org_boss&area=${area}${search}`);
  await act(async () => root.render(<Shell area={area} />));
  await act(async () => { await new Promise((r) => setTimeout(r, 30)); });
}

beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  window.localStorage.clear();
  vi.stubGlobal("fetch", vi.fn(async (url: string) => {
    const body = String(url).includes("/v1/projects/summary") || String(url).includes("summary") ? { items: CARDS } : {};
    return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
  }));
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
});

afterEach(async () => {
  await act(async () => root.unmount());
  host.remove();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

const WIREFRAME_RAIL = ["overview", "projects", "code_tasks", "agent_ops", "delivery_workflows", "workforce", "knowledge",
  "decisions", "infra", "observability", "self_fixes", "setup", "skills", "budget", "enterprise"];

describe("the rail", () => {
  it("shows every area in the wireframe's order, with twelve projects", async () => {
    await mount("code_tasks");
    const order = $$(".bx-nav [data-area-nav]").map((e) => e.dataset.areaNav!);
    // Activity log stays (parity: it was on the rail before), between Observability and Self-fixes.
    expect(order.filter((a) => a !== "activity")).toEqual(WIREFRAME_RAIL);
    expect($('[data-area-nav="decisions"]')!.title).toBe("Approvals — Explicit approvals before real effects");
    expect($('[data-testid="nav-badge-decisions"]')!.textContent).toBe("2");
    expect($('[data-testid="nav-projects-new"]')).not.toBeNull();
  });

  it("caps the project list at five, keeps the open one, and links to All projects and Add project", async () => {
    await mount("project");
    const rows = $$('[data-testid^="nav-project-"]').map((e) => e.dataset.testid!.replace("nav-project-", ""));
    expect(rows.length).toBe(RAIL_PROJECTS_MAX);
    expect(rows[0]).toBe(id("HelpDesk AI Demo"));        // most needs-you first
    expect(rows[1]).toBe(id("HelpDesk AI"));
    expect(rows).toContain(OPEN);                        // the open project, though quiet and old
    expect($(`[data-testid="nav-project-${OPEN}"]`)!.getAttribute("aria-current")).toBe("page");
    expect($('[data-testid="nav-all-projects"]')!.textContent).toContain("All projects (12)");
    expect($('[data-testid="nav-add-project"]')!.title).toBe("Add a project — new from an idea, or an existing repo");
    // A state dot on every row: amber needs you, teal running, grey not started.
    expect($(`[data-testid="nav-project-${id("HelpDesk AI Demo")}"] .bx-nav-dot`)!.dataset.s).toBe("you");
  });

  it("railProjects: needs-you first, then most recent, the open one always in", () => {
    const p = (name: string, needs: number, at: string): RailProject => ({ id: name, name, needs, at, dot: "done" });
    const list = [p("a", 0, "1"), p("b", 0, "9"), p("c", 1, "2"), p("d", 0, "8"), p("e", 0, "7"), p("f", 0, "6"), p("g", 0, "5")];
    expect(railProjects(list, null).map((x) => x.id)).toEqual(["c", "b", "d", "e", "f"]);
    expect(railProjects(list, "a").map((x) => x.id)).toEqual(["c", "b", "d", "e", "a"]);
  });

  it("fits a 900px window: compact rows, a scrolling area list, a footer that stays, a full-height colour", () => {
    const css = readFileSync("app/shell.css", "utf8");
    const px = (re: RegExp) => Number(css.match(re)?.[1]);
    const item = px(/\.bx-shell \.bx-nav-item \{\s*min-height: (\d+)px/);
    const child = px(/\.bx-shell \.bx-nav-child \{\s*min-height: (\d+)px/);
    const brand = px(/\.bx-shell \.bx-brand \{ height: (\d+)px/);
    // 16 areas + Projects' 5 rows, All projects and Add project + 3 group labels (≤28px) + footer (≤60px).
    expect(brand + 16 * item + 7 * child + 3 * 28 + 60).toBeLessThanOrEqual(900);
    expect(css).toMatch(/\.bx-shell \.bx-nav \{[^}]*overflow-y: auto/);
    expect(css).toMatch(/\.bx-shell \.bx-sidebar-foot \{ flex: none/);
    expect(css).toMatch(/\.bx-rail-bg \{[^}]*position: absolute;[^}]*top: 0; bottom: 0/);
    expect(readFileSync("app/ConsoleShell.tsx", "utf8")).toContain('<div className="bx-rail-bg" aria-hidden />');
  });

  it("collapses to a 60px icon rail on ⌘\\ and remembers it", async () => {
    await mount("overview");
    await act(async () => { window.dispatchEvent(new KeyboardEvent("keydown", { key: "\\", metaKey: true })); });
    expect($(".bx-shell")!.className).toContain("nav-docked");
    expect(window.localStorage.getItem("boss.nav.docked")).toBe("1");
    expect(readFileSync("app/shell.css", "utf8")).toContain(".bx-shell.nav-docked { grid-template-columns: 3.15789rem");
  });
});

describe("the top bar", () => {
  it("crumbs: Projects / Project / Tab on a project page, Project / Area elsewhere, the area alone without a project", () => {
    const base = { areaLabel: "Code Tasks", routeMode: false, hasProject: true };
    const words = (c: ReturnType<typeof crumbsFor>) => c.map((x) => x.key);
    expect(words(crumbsFor({ ...base, active: "projects", tab: null }))).toEqual(["area"]);
    expect(words(crumbsFor({ ...base, active: "project", tab: null }))).toEqual(["projects", "project"]);
    expect(crumbsFor({ ...base, active: "project", tab: "work" }).at(-1)).toEqual({ key: "tab", label: "Work", kind: "here" });
    expect(words(crumbsFor({ ...base, active: "code_tasks", tab: null }))).toEqual(["project", "area"]);
    expect(words(crumbsFor({ ...base, active: "enterprise", routeMode: true, tab: null }))).toEqual(["area"]);
  });

  it("draws the crumbs, and ⌘K and the Jump box open the palette", async () => {
    await mount("project", "&project_id=x&tab=work");
    expect($('[data-testid="topbar-crumbs"]')!.textContent!.replace(/\s+/g, " ")).toBe("Projects/BOSS dogfood bootstrap/Work");
    expect($('[data-testid="crumb-project"]')!.getAttribute("href")).toContain(`project_id=${OPEN}`);
    expect($('[aria-label="Jump to area"]')).toBeNull();
    await act(async () => { window.dispatchEvent(new KeyboardEvent("keydown", { key: "k", metaKey: true })); });
    expect($('[aria-label="Jump to area"]')).not.toBeNull();
    await act(async () => { window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" })); });
    expect($('[aria-label="Jump to area"]')).toBeNull();
    await act(async () => { $('[data-testid="topbar-jump"]')!.click(); });
    expect($('[aria-label="Jump to area"]')).not.toBeNull();
    expect($('[data-testid="topbar-jump"]')!.title).toBe("Jump to any project, run, PR, deploy or agent");
  });

  it("the bell carries the needs-you count and opens the list with the way to Approvals", async () => {
    await mount("overview");
    const bell = $('[data-testid="topbar-bell"]')!;
    expect(bell.title).toBe("Approvals waiting on you");
    expect($('[data-testid="topbar-bell-count"]')!.textContent).toBe("3");
    await act(async () => { bell.click(); });
    expect($('[data-testid="ag-crew-need-dec_1"]')!.textContent).toContain("Merge PR #3");
    expect($('[data-testid="topbar-bell-approvals"]')).not.toBeNull();
  });

  it("keeps every capability of the old bar (parity)", async () => {
    await mount("overview");
    // Old bar → new home: New build (+), Live, the needs-you pill (bell), the org switcher, the
    // project menu, the System menu (edition, budget, console version, sign out).
    expect($('[data-testid="topbar-new-project"]')!.getAttribute("aria-label")).toBe("New build");
    expect($(".bx-live-status")!.textContent).toContain("Live");
    expect($('[data-testid="topbar-bell"]')).not.toBeNull();
    const menu = $(".bx-user-fallback")!;
    for (const tid of ["menu-switch-org", "menu-new-build", "menu-system", "project-menu", "new-project", "bx-sign-out"]) {
      expect(menu.querySelector(`[data-testid="${tid}"]`), tid).not.toBeNull();
    }
    expect(menu.querySelector('[data-testid="menu-system"]')!.textContent).toMatch(/Edition[\s\S]*Budget[\s\S]*green[\s\S]*Console/);
    // No second copy of the project menu, the org chip or the System menu in the bar itself.
    expect($(".bx-topbar > .bx-project-menu, .bx-topbar > .bx-org-chip, .bx-topbar > .bx-system-menu")).toBeNull();
  });
});

describe("no scope banner", () => {
  it("is gone from every page, and nothing still points at it", async () => {
    expect(existsSync("app/project-away.tsx")).toBe(false);
    const page = readFileSync("app/page.tsx", "utf8");
    expect(page).not.toMatch(/ProjectAway|project-away/);
    for (const area of ["project", "projects", "code_tasks"]) {
      await mount(area, area === "projects" ? "" : `&project_id=${OPEN}`);
      expect($('[data-testid="project-away"]')).toBeNull();
      expect(host.textContent).not.toMatch(/← Back to/);
    }
  });
});
