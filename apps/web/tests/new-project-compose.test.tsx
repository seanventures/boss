// @vitest-environment happy-dom
/**
 * New project: one screen, "✦ A new app" | "＋ Add to an existing app" (operator-approved
 * mockups, 2026-10-05). Adding a feature to an app that exists is the everyday job, so it is a
 * mode of this screen and every path it offers builds ONTO the app:
 *
 *   Quick change  → the feedback proposal on the app's own run, confirmed inline;
 *   Small / Major → their workflow, started with `extends_run_id` (the engine's build step builds
 *                   on that run's branch — apps/api/tests/test_a_feature_builds_onto_its_app.py);
 *   Spec it first → the panel, with `[boss:extend_run=…]`.
 *
 * The real composer, mounted against a fake /api at the fetch boundary.
 */
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { CatalogueEntry, Installed } from "../app/delivery-workflows-ui";

const proposed: FormData[] = [];
const confirmed: FormData[] = [];
const convened: FormData[] = [];
vi.mock("../app/actions", async (original) => ({
  ...(await original<typeof import("../app/actions")>()),
  submitCodeTaskFeedbackAction: async (_p: unknown, form: FormData) => {
    proposed.push(form);
    return { thread_id: "fbt_1", status: "awaiting_confirmation",
             proposal: { classification: "scoped_tweak", fix_note: "Observed behavior: snooze", profile: "balanced",
                         repair_retries: 1, path: "inline" } };
  },
  confirmCodeTaskFeedbackAction: async (_p: unknown, form: FormData) => {
    confirmed.push(form);
    return { run_id: "crun_change", path: "inline", workflow_run_id: "wfr_change" };
  },
  conveneMeetingAction: async (form: FormData) => { convened.push(form); return { ok: true }; },
}));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const { NewProjectComposer } = await import("../app/workflows/new-project-composer");
const plan = await import("../app/workflows/compose-plan");
const { readComposeIntent } = await import("../app/url-intent");
const { resolveLegacyArea } = await import("../app/legacy-areas");

// ── Fixtures: the five catalogue paths as a real install lists them ─────────────────────────

const BRIEF = { type: "object", required: ["directive", "title"],
                properties: { directive: { type: "string", format: "textarea" }, title: { type: "string" },
                              acceptance_criteria: { type: "array", items: { type: "string" } },
                              reference_links: { type: "array", items: { type: "string" } },
                              repository: { type: "string" } } };
type S = [string, string | null, string];
const STEPS: Record<string, S[]> = {
  fast_fix: [["build", "engineering_captain", "auto"], ["report", null, "auto"], ["deploy", null, "manual"], ["notify", null, "auto"]],
  minor_feature: [["meeting", "pm", "auto"], ["build", "engineering_captain", "auto"], ["preview", "qa", "auto"],
                  ["report", null, "auto"], ["deploy", null, "manual"], ["notify", null, "auto"]],
  standard_product_delivery: [["research", "pm", "auto"], ["meeting", "pm", "auto"], ["spec_review", null, "manual"],
                              ["build", "engineering_captain", "auto"], ["preview", "qa", "auto"], ["plrr", "sre", "auto"],
                              ["report", null, "auto"], ["deploy", null, "manual"], ["monitor", "sre", "auto"], ["notify", null, "auto"]],
  major_feature: [["research", "pm", "auto"], ["meeting", "pm", "auto"], ["spec_review", null, "manual"],
                  ["build", "engineering_captain", "auto"], ["preview", "qa", "auto"], ["ux_review", null, "manual"],
                  ["report", null, "auto"], ["deploy", null, "manual"], ["monitor", "sre", "auto"]],
  research_led_discovery: [["research", "pm", "auto"], ["meeting", "pm", "auto"], ["spec_review", null, "manual"],
                           ["document", "docs_writer", "auto"]],
};
const WORKFLOWS: Installed[] = Object.entries(STEPS).map(([key, steps]) => ({
  id: `wfd_${key}`, workflow_key: key, name: key.replace(/_/g, " "), is_catalogue: true,
  forked_from_version_id: null, namespace: "product", input_schema: BRIEF, step_count: steps.length,
  steps: steps.map(([id, captain, approval]) => ({ id, label: id, kind: "capability", mine: false, approval,
                                                    ...(captain ? { captain } : {}) })),
}));
/** The catalogue's gates are where a PERSON is asked; the PRD approval is the panel's. */
const CATALOGUE: CatalogueEntry[] = Object.entries(STEPS).map(([key, steps]) => ({
  key, name: key, description: `${key} description`, work_class: "project.new", step_count: steps.length,
  steps: steps.map(([id]) => ({ id, capability: id })),
  gates: steps.filter(([id, , a]) => a === "manual" && id !== "spec_review").map(([id]) => id),
  automatic_approvals: steps.filter(([id]) => id === "spec_review").map(([id]) => id),
  requires_project: ["repository"], estimated_minutes: 30, input_schema: BRIEF,
}));

const APPS = { apps: [
  { key: "crun_lead2", app: "Lead-Triage Inbox", run_id: "crun_lead2", builders: ["pm", "engineering_captain", "qa"],
    state: "pr_open", word: "PR open", builds: 2, run_ids: ["crun_lead2", "crun_lead1"], pr_number: 186, pr_state: "open",
    repo: "acme/sandbox", brief: "Lead triage", attempts: [
      { run_id: "crun_lead2", attempt: 1, word: "Failed", order: 2 },
      { run_id: "crun_lead1", attempt: 1, word: "PR open", order: 1, pr_number: 186, pr_state: "open" }] },
  { key: "crun_cal", app: "Change Calendar", run_id: "crun_cal", builders: ["pm"], state: "pr_open", word: "PR open",
    builds: 1, run_ids: ["crun_cal"], pr_number: 184, repo: "acme/sandbox", attempts: [
      { run_id: "crun_cal", attempt: 1, word: "PR open", order: 1, pr_number: 184 }] },
] };

type Posted = Record<string, unknown> & { input?: Record<string, unknown> };
let posts: Posted[] = [];
let costs: Record<string, unknown> = {};

function stubApi() {
  posts = [];
  vi.stubGlobal("fetch", vi.fn(async (input: unknown, init?: RequestInit) => {
    const url = new URL(String(input), "http://console.test");
    const json = (body: unknown, status = 200) =>
      new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
    if (init?.method === "POST" && url.pathname === "/api/delivery-workflows") {
      posts.push(JSON.parse(String(init.body)));
      return json({ run: { id: "wfr_started" } });
    }
    if (url.pathname === "/v1/factory/apps") return json(APPS);
    if (url.pathname === "/api/project-agents") return json({ default_roles: ["pm", "engineering_captain", "qa", "release_manager"] });
    if (url.pathname === "/api/agent") return json({ items: [] });
    const view = url.searchParams.get("view");
    if (view === "repositories") return json({ repositories: [{ id: "r1", full_name: "acme/sandbox", default_branch: "main" }],
                                                suggested: "acme/sandbox", suggested_source: "registry_primary" });
    if (view === "costs") return json({ costs });
    if (view === "readiness") {
      const need = (k: string, label: string) => ({ key: k, label, fix: "", area: null, ok: true, detail: "",
                                                     needed_by: [], first_step_label: "", after_a_person: false });
      return json({ ready: true, checked: [need("model", "Models ready"), need("repository", "Repo connected")], missing: [] });
    }
    return json({ error: "unexpected" }, 404);
  }));
}

let root: Root | null = null;
let host: HTMLElement | null = null;
const started: Array<string | null> = [];
beforeEach(() => {
  proposed.length = 0; confirmed.length = 0; convened.length = 0; started.length = 0;
  costs = {};
  try { window.localStorage.clear(); } catch { /* none */ }
});
afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  root = null; host = null;
  vi.unstubAllGlobals();
});
const $ = (sel: string) => host!.querySelector(sel) as HTMLElement | null;
const $$ = (sel: string) => [...host!.querySelectorAll(sel)] as HTMLElement[];
const settle = () => act(async () => { for (let i = 0; i < 8; i++) await Promise.resolve(); });

async function mount(props: Partial<React.ComponentProps<typeof NewProjectComposer>> = {}) {
  stubApi();
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => {
    root!.render(<NewProjectComposer projectId="p1" actor="ceo" workflows={WORKFLOWS} catalogue={CATALOGUE}
                                     onStarted={(id) => started.push(id)} onCancel={() => {}} {...props} />);
  });
  await settle();
}
async function type(el: HTMLElement | null, value: string) {
  expect(el, "field to type into").not.toBeNull();
  const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
  await act(async () => {
    Object.getOwnPropertyDescriptor(proto, "value")!.set!.call(el, value);
    el!.dispatchEvent(new Event("input", { bubbles: true }));
  });
  await settle();
}
async function click(el: HTMLElement | null) {
  expect(el, "element to click").not.toBeNull();
  await act(async () => { el!.click(); });
  await settle();
}
async function key(el: HTMLElement | null, init: KeyboardEventInit) {
  await act(async () => { (el ?? document).dispatchEvent(new KeyboardEvent("keydown", { bubbles: true, ...init })); });
  await settle();
}
const steps = () => $$('[data-testid="np-timeline"] li').map((li) => ({
  id: li.dataset.testid?.replace("np-step-", ""), who: li.dataset.who, text: li.textContent ?? "" }));

// ── The plan, as pure rules ──────────────────────────────────────────────────────────────────

describe("the plan's rules", () => {
  const keys = WORKFLOWS.map((w) => w.workflow_key);

  it("offers the five paths for a new app, Standard recommended, and four ways to add a feature", () => {
    expect(plan.pathCards("new", keys).map((p) => p.short)).toEqual(["Fast fix", "Small", "Standard", "Major", "Discovery"]);
    expect(plan.defaultPath("new", keys)).toBe("standard_product_delivery");
    expect(plan.pathCards("add", keys).map((p) => p.short)).toEqual(["Quick change", "Small", "Spec it first", "Major"]);
    expect(plan.defaultPath("add", keys)).toBe("minor_feature");
    // A project without the workflows still has the two routes that are not workflows.
    expect(plan.pathCards("add", []).map((p) => p.key)).toEqual([plan.QUICK, plan.SPEC]);
    expect(plan.defaultPath("add", [])).toBe(plan.QUICK);
    expect(plan.defaultPath("new", [])).toBe("");
  });

  it("draws who does each step: the captain, BOSS, or you at a gate the catalogue says a person answers", () => {
    const w = WORKFLOWS.find((x) => x.workflow_key === "standard_product_delivery")!;
    const c = CATALOGUE.find((x) => x.key === "standard_product_delivery")!;
    const t = plan.timelineOf({ steps: w.steps!, gates: c.gates, automatic: c.automatic_approvals, mode: "new" });
    expect(t.map((s) => s.name)).toEqual(["Research", "Panel", "Spec sign-off", "Build", "Preview", "Launch check",
                                          "Report", "Deploy", "Watch", "Notify"]);
    expect(t.find((s) => s.id === "build")!.who).toEqual({ kind: "agent", role: "engineering_captain" });
    expect(t.find((s) => s.id === "report")!.who).toEqual({ kind: "boss" });
    expect(t.find((s) => s.id === "deploy")!.who).toEqual({ kind: "you" });
    // The PRD approval is the panel's own decision, not one of yours.
    expect(t.find((s) => s.id === "spec_review")!.who.kind).not.toBe("you");
    expect(plan.counts(t)).toEqual({ steps: 10, approvals: 1 });
  });

  it("says a change as a change, and keeps what works after the build", () => {
    const w = WORKFLOWS.find((x) => x.workflow_key === "minor_feature")!;
    const t = plan.timelineOf({ steps: w.steps!, gates: ["deploy"], mode: "add", app: "Lead-Triage Inbox" });
    expect(t.map((s) => s.name)).toEqual(["Scope the change", "Build on its branch", "Keep what works", "Preview",
                                          "Report", "Deploy", "Notify"]);
    expect(t[1].line).toBe("Adds to Lead-Triage Inbox — no re-scaffold");
    expect(plan.counts(t)).toEqual({ steps: 6, approvals: 1 });
    for (const route of [plan.QUICK, plan.SPEC]) {
      expect(plan.routeTimeline(route, "X").some((s) => s.keep)).toBe(true);
    }
  });

  it("prices a path from its history, and says nothing without one", () => {
    expect(plan.costRange({ runs: 6, low_usd: 4.2, high_usd: 7.4 })).toBe("$4–$7");
    expect(plan.costRange({ runs: 1, low_usd: 3.1, high_usd: 3.4 })).toBe("$3");
    expect(plan.costRange(null)).toBeNull();
    expect(plan.costRange({ runs: 0, low_usd: 0, high_usd: 0 })).toBeNull();
    expect(plan.costKey(plan.QUICK)).toBe("@change");
  });

  it("finds an app's cover by its template, else a monogram; and the app a link names by any build", () => {
    expect(plan.matchIdea("Lead-Triage Inbox", [{ title: "Lead triage inbox" }])?.title).toBe("Lead triage inbox");
    expect(plan.matchIdea("Zebra", [{ title: "Lead triage inbox" }])).toBeNull();
    expect(plan.monogram("Vendor Status Watcher").initials).toBe("VS");
    const rows = [{ key: "crun_b", builds: [{ runId: "crun_b" }, { runId: "crun_a" }] }];
    expect(plan.findApp(rows, "crun_a")?.key).toBe("crun_b");
    expect(plan.findApp(rows, "crun_x")).toBeNull();
  });

  it("sends what the engine builds onto: extends_run_id, the checks and the links", () => {
    const input = plan.featureInput({ app: "Lead-Triage Inbox", runId: "crun_lead1", gain: "Let a rep snooze a lead.",
                                      kinds: ["Store data"], checks: ["A snoozed lead comes back", plan.KEEPS_WORKING, " "],
                                      links: ["https://docs.example.com", "not a link"] });
    expect(input).toEqual({
      title: "Lead-Triage Inbox: Let a rep snooze a lead", directive: "Let a rep snooze a lead.\nKind of change: Store data",
      extends_run_id: "crun_lead1", acceptance_criteria: ["A snoozed lead comes back", plan.KEEPS_WORKING],
      reference_links: ["https://docs.example.com"],
    });
  });

  it("a change-kind chip adds its outcome as a check and takes it away again", () => {
    const on = plan.checksWithKind([plan.KEEPS_WORKING], "the page loads", true);
    expect(on).toEqual(["The page loads", plan.KEEPS_WORKING]);
    expect(plan.checksWithKind(on, "the page loads", false)).toEqual([plan.KEEPS_WORKING]);
  });
});

describe("the links into it", () => {
  it("?compose=add&app= opens add mode on that app, and is consumed", () => {
    const i = readComposeIntent("?org=o1&area=delivery_workflows&compose=add&app=crun_lead1&preview=prev_1");
    expect(i).toMatchObject({ open: true, mode: "add", app: "crun_lead1", preview: "prev_1" });
    expect(i.nextSearch).toBe("?org=o1&area=delivery_workflows");
    expect(readComposeIntent("?new=1").mode).toBeNull();
    expect(readComposeIntent("?area=x").open).toBe(false);
  });

  it("area=workflows (the Command Center's link) is the Workflows area, carrying the rest", () => {
    const next = resolveLegacyArea(new URLSearchParams("org=o1&area=workflows&compose=add&app=crun_lead1"));
    expect(next?.get("area")).toBe("delivery_workflows");
    expect(next?.get("compose")).toBe("add");
    expect(next?.get("app")).toBe("crun_lead1");
  });
});

// ── A new app ────────────────────────────────────────────────────────────────────────────────

describe("a new app", () => {
  it("opens on Standard with its real steps, who does each, the team and where it lives", async () => {
    await mount();
    expect($('[data-testid="dw-new-project"]')?.dataset.mode).toBe("new");
    expect($('[data-testid="np-mode-new"]')?.getAttribute("aria-checked")).toBe("true");
    expect($('[data-testid="np-path-standard_product_delivery"]')?.getAttribute("aria-checked")).toBe("true");
    expect($('[data-testid="np-path-standard_product_delivery"]')?.textContent).toContain("Recommended");
    expect(steps().map((s) => s.id)).toEqual(STEPS.standard_product_delivery.map(([id]) => id));
    expect(steps().find((s) => s.id === "deploy")?.who).toBe("you");
    expect(steps().find((s) => s.id === "report")?.who).toBe("boss");
    expect(steps().find((s) => s.id === "build")?.who).toBe("agent");
    expect($('[data-testid="np-lives"]')?.textContent).toContain("acme/sandbox");
    expect($('[data-testid="np-lives"]')?.textContent).toContain("main");
    expect($$('[data-testid="np-team"] .np-face')).toHaveLength(4);
    expect($('[data-testid="np-ready"]')?.textContent).toContain("Models ready");
    // No history: no price is guessed.
    expect($('[data-testid="np-cost"]')?.textContent).not.toContain("cost");
    expect($('[data-testid="np-cost"]')?.textContent).toContain("10 steps · you approve once · nothing deploys without you");
  });

  it("switching path redraws the plan; All workflows offers the rest", async () => {
    await mount();
    await click($('[data-testid="np-path-fast_fix"]'));
    expect(steps().map((s) => s.id)).toEqual(["build", "report", "deploy", "notify"]);
    await click($('[data-testid="np-all-paths"]'));
    expect($('[data-testid="dw-new-workflow-default"]')).not.toBeNull();
    await click($('[data-testid="dw-new-workflow-major_feature"]'));
    expect($('[data-testid="np-path-major_feature"]')?.getAttribute("aria-checked")).toBe("true");
  });

  it("an idea fills the brief; the checks and links are sent; ⌘↵ starts", async () => {
    costs = { standard_product_delivery: { runs: 5, low_usd: 4, high_usd: 7, metered: true } };
    await mount();
    expect($('[data-testid="np-cost"]')?.textContent).toContain("Builds like this cost $4–$7");
    await click($('[data-testid="np-idea-incident-timeline-builder"]'));
    expect(($('[data-testid="dw-new-directive"]') as HTMLTextAreaElement).value).toMatch(/^Build an incident timeline builder/);
    await click($('[data-testid="dw-new-criterion-add"]'));
    await type($('[data-testid="dw-new-criterion-0"]'), "A real log produces all four milestones");
    await click($('[data-testid="dw-new-link-add"]'));
    await type($('[data-testid="dw-new-link-input"]'), "https://docs.example.com/alerts");
    await key($('[data-testid="dw-new-link-input"]'), { key: "Enter" });
    expect($('[data-testid="dw-new-link-0"]')?.textContent).toContain("docs.example.com/alerts");
    await key($('[data-testid="dw-new-directive"]'), { key: "Enter", metaKey: true });
    expect(posts).toHaveLength(1);
    expect(posts[0]).toMatchObject({ action: "start", workflow_key: "standard_product_delivery" });
    expect(posts[0].input).toMatchObject({ title: "Incident timeline builder",
                                           acceptance_criteria: ["A real log produces all four milestones"],
                                           reference_links: ["https://docs.example.com/alerts"] });
    expect(started).toEqual(["wfr_started"]);
  });

  it("the full-screen editor keeps the text, and Esc closes it first", async () => {
    await mount();
    await type($('[data-testid="dw-new-directive"]'), "A shift-swap board.");
    await click($('[data-testid="dw-new-directive-full"]'));
    const big = document.querySelector('[data-testid="np-fullscreen-text"]') as HTMLTextAreaElement;
    expect(big.value).toBe("A shift-swap board.");
    await type(big, "A shift-swap board for the duty team.");
    await key(big, { key: "Escape" });
    expect(document.querySelector('[data-testid="np-fullscreen"]')).toBeNull();
    expect(($('[data-testid="dw-new-directive"]') as HTMLTextAreaElement).value).toBe("A shift-swap board for the duty team.");
  });

  it("remembers the last mode used", async () => {
    await mount();
    await click($('[data-testid="np-mode-add"]'));
    expect(window.localStorage.getItem("boss.compose.mode")).toBe("add");
    act(() => root?.unmount());
    await mount();
    expect($('[data-testid="dw-new-project"]')?.dataset.mode).toBe("add");
  });
});

// ── Adding to an app ─────────────────────────────────────────────────────────────────────────

describe("adding a feature to an app that exists", () => {
  it("a link's app is chosen; the plan says what it builds on and how it lands", async () => {
    await mount({ mode: "add", appKey: "crun_lead1" });
    expect($('[data-testid="np-app-crun_lead2"]')?.getAttribute("aria-checked")).toBe("true");
    const on = $('[data-testid="np-builds-on"]')!.textContent!;
    expect(on).toContain("Lead-Triage Inbox");
    expect(on).toContain("acme/sandbox · PR #186 branch · 2 builds so far");
    expect(on).toContain("Working today — the change is added, nothing is rebuilt");
    expect($('[data-testid="np-lands"]')?.textContent).toContain("A new PR on top of #186");
    expect($('[data-testid="np-team"]')?.textContent).toContain("The crew that built it");
    expect($('[data-testid="np-path-minor_feature"]')?.getAttribute("aria-checked")).toBe("true");
    expect(steps().map((s) => s.id)).toEqual(["meeting", "build", "keep", "preview", "report", "deploy", "notify"]);
    // The first check is the one every change keeps.
    expect(($('[data-testid="np-check-0"]') as HTMLTextAreaElement).value).toBe(plan.KEEPS_WORKING);
  });

  it("Small starts minor_feature ON the app: its newest working build, never a new app", async () => {
    await mount({ mode: "add", appKey: "crun_lead2" });
    expect($('[data-testid="dw-new-start"]')!.hasAttribute("disabled")).toBe(true);
    await type($('[data-testid="np-gain"]'), "Let a sales rep snooze a lead until a date.");
    await click($('[data-testid="np-kind-data"]'));
    expect($$('[data-testid^="np-check-"]').filter((e) => e.tagName === "TEXTAREA").map((e) => (e as HTMLTextAreaElement).value))
      .toEqual(["The data survives a reload and a restart, through a migration rather than local state", plan.KEEPS_WORKING]);
    expect($('[data-testid="dw-new-start"]')?.textContent).toContain("Add the feature");
    await click($('[data-testid="dw-new-start"]'));
    expect(posts).toHaveLength(1);
    expect(posts[0]).toMatchObject({ action: "start", workflow_key: "minor_feature" });
    // The newest build FAILED; the one that works (crun_lead1) is what it builds on.
    expect(posts[0].input).toMatchObject({ extends_run_id: "crun_lead1",
                                           title: "Lead-Triage Inbox: Let a sales rep snooze a lead until a date" });
    expect((posts[0].input!.acceptance_criteria as string[])).toContain(plan.KEEPS_WORKING);
    expect(started).toEqual(["wfr_started"]);
  });

  it("Quick change: the agent's proposal shows inline, and only Start runs it", async () => {
    await mount({ mode: "add", appKey: "crun_cal", previewId: "prev_7" });
    await click($('[data-testid="np-path-quick"]'));
    expect(steps().map((s) => s.id)).toEqual(["scope", "confirm", "build", "keep", "preview"]);
    await type($('[data-testid="np-gain"]'), "Show the week view by default.");
    await click($('[data-testid="dw-new-start"]'));
    expect(proposed).toHaveLength(1);
    expect(proposed[0].get("run_id")).toBe("crun_cal");
    expect(proposed[0].get("anchor")).toBe("/v1/previews/prev_7/web/");
    expect(proposed[0].get("expected")).toBe(plan.KEEPS_WORKING);
    expect(confirmed).toHaveLength(0);
    expect($('[data-testid="np-proposal"]')?.textContent).toContain("Small change");
    expect($('[data-testid="dw-new-start"]')?.textContent).toContain("Start the change");
    await click($('[data-testid="dw-new-start"]'));
    expect(confirmed.map((f) => f.get("thread_id"))).toEqual(["fbt_1"]);
    expect(started).toEqual(["wfr_change"]);
  });

  it("Spec it first convenes the panel onto this app's branch", async () => {
    await mount({ mode: "add", appKey: "crun_cal" });
    await click($('[data-testid="np-path-spec"]'));
    await type($('[data-testid="np-gain"]'), "Recurring changes that repeat weekly.");
    await click($('[data-testid="dw-new-start"]'));
    expect(convened).toHaveLength(1);
    expect(String(convened[0].get("directive"))).toContain("[boss:extend_run=crun_cal]");
    expect(String(convened[0].get("directive"))).toContain("Recurring changes that repeat weekly.");
  });

  it("finds an app by name, and says what is missing before Start", async () => {
    await mount({ mode: "add" });
    expect($('[data-testid="np-blocked"]')?.textContent).toBe("Pick the app it goes into.");
    await type($('[data-testid="np-app-find"]'), "calendar");
    expect($$('[data-testid^="np-app-crun_"]').map((e) => e.dataset.testid)).toEqual(["np-app-crun_cal"]);
    await click($('[data-testid="np-app-crun_cal"]'));
    expect($('[data-testid="np-blocked"]')?.textContent).toBe("Say what it should gain, in a sentence.");
  });
});
