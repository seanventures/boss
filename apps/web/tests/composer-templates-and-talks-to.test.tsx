// @vitest-environment happy-dom
/**
 * The composer's connectors and ideas, and "Talks to". The Template tab is gone (operator,
 * 2026-10-13): login and payments are CONNECTORS either job adds, the starters are IDEAS a new
 * app starts from, and a chosen connector's Start opens its sheet with the brief in; "Talks to" makes
 * the link once the start is accepted — for an app that exists, from its build; for a new app,
 * under the title the start sends (`new_app`).
 */
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { CatalogueEntry, Installed } from "../app/delivery-workflows-ui";

const linked: FormData[] = [];
vi.mock("../app/factory/links-actions", () => ({
  createLinkAction: vi.fn(async (f: FormData) => { linked.push(f); return { ok: true, message: "Linked as BANK" }; }),
  revokeLinkAction: vi.fn(), revealTestUsersAction: vi.fn(),
}));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const { NewProjectComposer } = await import("../app/workflows/new-project-composer");
const { readComposeIntent } = await import("../app/url-intent");
const { resetCatalogue } = await import("../components/v2/templates/catalogue");
const { overBudget } = await import("./_helpers/copy-budget");

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
  { key: "crun_lead2", app: "Lead-Triage Inbox", app_key: "lead", run_id: "crun_lead2", builders: ["pm", "engineering_captain", "qa"],
    state: "pr_open", word: "PR open", builds: 2, run_ids: ["crun_lead2", "crun_lead1"], pr_number: 186, pr_state: "open",
    preview: { status: "running", url: "/v1/previews/prev_9/web/" },
    repo: "acme/sandbox", brief: "Lead triage", attempts: [
      { run_id: "crun_lead2", attempt: 1, word: "Failed", order: 2 },
      { run_id: "crun_lead1", attempt: 1, word: "PR open", order: 1, pr_number: 186, pr_state: "open" }] },
  { key: "crun_cal", app: "Change Calendar", run_id: "crun_cal", builders: ["pm"], state: "pr_open", word: "PR open",
    builds: 1, run_ids: ["crun_cal"], pr_number: 184, pr_state: "merged", repo: "acme/sandbox", attempts: [
      { run_id: "crun_cal", attempt: 1, word: "PR open", order: 1, pr_number: 184 }] },
] };


const TEMPLATES = { templates: [
  { id: "tpl_login", template_key: "login-oauth", version: "1.0.0", source: "builtin", status: "active", name: "Login",
    summary: "Google, GitHub or BOSS sign-in", stacks: ["node", "python"], labels: [], adds: ["routes"], egress: [],
    secrets: [], tested: ["node"], kind: "connector" },
  { id: "tpl_stripe", template_key: "stripe-checkout", version: "1.0.0", source: "builtin", status: "active", name: "Stripe Checkout",
    summary: "Card payments", stacks: ["node", "python"], labels: [], adds: ["routes"], egress: ["api.stripe.com"],
    secrets: [], tested: ["node"], kind: "connector" },
  { id: "tpl_hello", template_key: "hello", version: "1.0.0", source: "builtin", status: "active", name: "Hello endpoint",
    summary: "Adds GET /hello", stacks: ["node"], labels: [], adds: ["GET /hello"], egress: [],
    secrets: [], tested: ["node"], kind: "idea" },
  { id: "tpl_lab", template_key: "banking-starter-insecure", version: "1.0.0", source: "builtin", status: "active",
    name: "Banking lab", summary: "Planted flaws", stacks: ["java"], labels: ["insecure_by_design"], adds: [], egress: [],
    secrets: [], tested: [] },
] };

/** What GET /v1/stacks says BOSS builds (services/stacks.py ADAPTERS), two of them. */
const STACK_ADAPTERS = [
  { id: "react-vite+express+postgres", label: "React + Vite, Express, PostgreSQL", support_level: "experimental",
    languages: ["node"], ready: true },
  { id: "react-vite+fastapi+postgres", label: "React + Vite, FastAPI, PostgreSQL", support_level: "experimental",
    languages: ["node", "python"], ready: true },
];

let posts: Record<string, unknown>[] = [];
function stubApi() {
  posts = [];
  vi.stubGlobal("fetch", vi.fn(async (input: unknown, init?: RequestInit) => {
    const url = new URL(String(input), "http://console.test");
    const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
    if (init?.method === "POST" && url.pathname === "/api/delivery-workflows") {
      posts.push(JSON.parse(String(init.body)));
      return json({ run: { id: "wfr_started" } });
    }
    if (url.pathname === "/v1/factory/apps") return json(APPS);
    if (url.pathname === "/v1/templates") return json(TEMPLATES);
    if (url.pathname.startsWith("/v1/templates/")) return json({ ...TEMPLATES.templates[0], manifest: {}, validation: { ok: true }, files: [], versions: [] });
    if (url.pathname === "/v1/apps/lead/templates") {
      return json({ templates: [{ id: "a1", template_key: "login-oauth", name: "Login", version: "1.0.0", stack: "node", status: "applied" }] });
    }
    if (url.pathname.startsWith("/v1/apps/") && url.pathname.endsWith("/templates")) return json({ templates: [{ id: "t", template_key: "x", stack: "node", status: "applied" }] });
    if (url.pathname === "/v1/stacks") return json({ supported: { languages: [], adapters: STACK_ADAPTERS } });
    if (url.pathname === "/api/project-agents") return json({ default_roles: ["pm"] });
    if (url.pathname === "/api/agent") return json({ items: [] });
    const view = url.searchParams.get("view");
    if (view === "repositories") return json({ repositories: [{ id: "r1", full_name: "acme/sandbox", default_branch: "main" }],
                                                suggested: "acme/sandbox", suggested_source: "registry_primary" });
    if (view === "readiness") return json({ ready: true, checked: [], missing: [] });
    return json({ error: "unexpected" }, 404);
  }));
}

let root: Root | null = null;
let host: HTMLElement | null = null;
const started: Array<string | null> = [];
beforeEach(() => { started.length = 0; linked.length = 0; resetCatalogue(); try { window.localStorage.clear(); } catch { /* none */ } });
afterEach(() => { act(() => root?.unmount()); host?.remove(); root = null; host = null; document.body.innerHTML = ""; vi.unstubAllGlobals(); });
const $ = (sel: string) => document.querySelector(sel) as HTMLElement | null;
const settle = () => act(async () => { for (let i = 0; i < 12; i++) await new Promise((r) => setTimeout(r, 0)); });

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
async function click(el: HTMLElement | null) { expect(el).not.toBeNull(); await act(async () => { el!.click(); }); await settle(); }
async function type(el: HTMLElement | null, value: string) {
  const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
  await act(async () => { Object.getOwnPropertyDescriptor(proto, "value")!.set!.call(el, value); el!.dispatchEvent(new Event("input", { bubbles: true })); });
  await settle();
}
async function choose(el: HTMLElement | null, value: string) {
  await act(async () => { (el as HTMLSelectElement).value = value; el!.dispatchEvent(new Event("change", { bubbles: true })); });
  await settle();
}

describe("Connectors and ideas (operator, 2026-10-13: there is no Template tab)", () => {
  it("the mode switch has two jobs; an old Template choice or link lands on New app", async () => {
    window.localStorage.setItem("boss.compose.mode", "template");
    await mount();
    expect($('[data-testid="np-mode-template"]')).toBeNull();
    expect($('[data-testid="np-mode-new"]')?.getAttribute("aria-checked")).toBe("true");
    expect(readComposeIntent("?compose=template").mode).toBe("new");
  });

  it("New app starts from an idea: the catalogue's starter first, one list, never a training lab", async () => {
    await mount();
    expect($('[data-testid="np-ideas-open"]')?.textContent).toContain("Start from an idea");
    await click($('[data-testid="np-ideas-open"]'));
    const ideas = $('[data-testid="np-ideas"]')!;
    const titles = [...ideas.querySelectorAll(".np-idea b")].map((b) => b.textContent);
    expect(titles[0]).toBe("Hello endpoint");                   // the starter, before the written briefs
    expect(titles.length).toBeGreaterThan(1);                    // …and the written briefs after it
    expect(document.body.textContent).not.toContain("Banking lab");
    // The filter tabs: a starter app has a reference; a team tool is a written brief.
    await click($('[data-testid="np-ideas-tab-starter"]'));
    expect([...$('[data-testid="np-ideas"]')!.querySelectorAll(".np-idea b")].map((b) => b.textContent)).toEqual(["Hello endpoint"]);
    await click($('[data-testid="np-ideas-tab-team"]'));
    expect($('[data-testid="np-idea-hello-endpoint"]')).toBeNull();
    await click($('[data-testid="np-ideas-tab-all"]'));
    // A drawn preview where the mockup has one; the glyph cover otherwise.
    expect($('[data-testid="np-idea-hello-endpoint"] svg')?.getAttribute("aria-label")).toBe("Hello endpoint preview");
    await click($('[data-testid="np-idea-hello-endpoint"]'));
    expect($('[data-testid="np-ideas-dialog"]')).toBeNull();
    expect(($('[data-testid="dw-new-directive"]') as HTMLTextAreaElement).value).toContain("Build Hello endpoint");
    expect(($('[data-testid="dw-new-title"]') as HTMLInputElement).value).toBe("Hello endpoint");
    await click($('[data-testid="np-idea-reference"]'));         // its tested reference, with its tests as a gate
    expect($('[data-testid="tpl-sheet"]')?.textContent).toContain("Hello endpoint");
    expect(posts).toEqual([]);
  });

  it("a connector chosen in New app: Start opens its sheet with the name and brief already in", async () => {
    await mount();
    const row = $('[data-testid="np-connectors"]')!;
    expect(row.textContent).toContain("Connectors");
    expect(row.textContent).not.toContain("Hello endpoint");    // an idea is not a connector
    const chip = $('[data-testid="np-connector-login-oauth"]')!;
    expect(chip.getAttribute("title")).toContain("Built in your app's own look");
    expect(chip.textContent).toContain("+ Add");
    await click(chip);
    expect(chip.getAttribute("aria-pressed")).toBe("true");
    expect(chip.textContent).toContain("Added ✓");
    await type($('[data-testid="dw-new-title"]'), "HelpDesk AI");
    await type($('[data-testid="dw-new-directive"]'), "A support desk for customers and agents, with tickets.");
    await click($('[data-testid="dw-new-start"]'));
    expect(posts).toEqual([]);                                   // Start is the connector sheet's own
    const sheet = $('[data-testid="tpl-sheet"]')!;
    expect(sheet.textContent).toContain("Login");
    expect((sheet.querySelector('[data-testid="tpl-note"]') as HTMLTextAreaElement).value).toContain("A support desk");
  });

  it("one connector per build: once one is added, the others say why they wait", async () => {
    await mount();
    await click($('[data-testid="np-connector-login-oauth"]'));
    const stripe = $('[data-testid="np-connector-stripe-checkout"]')!;
    expect(stripe.getAttribute("aria-disabled")).toBe("true");
    expect(stripe.getAttribute("title")).toContain("One connector per build");
    await click(stripe);
    expect(stripe.getAttribute("aria-pressed")).toBe("false");
    expect($('[data-testid="np-connector-login-oauth"]')!.getAttribute("aria-pressed")).toBe("true");
    await click($('[data-testid="np-connector-login-oauth"]'));   // removing it frees the others
    expect(stripe.getAttribute("aria-disabled")).toBeNull();
  });

  it("the stack chosen travels: in the brief the builder reads, and into the connector sheet", async () => {
    await mount();
    const stack = $('[data-testid="np-stack"]') as HTMLSelectElement;
    expect(stack.value).toBe("");
    expect([...stack.options].map((o) => o.textContent)).toContain("Node · React + Vite, Express, PostgreSQL");
    expect(stack.getAttribute("title")).toContain("React + Vite, FastAPI, PostgreSQL: experimental");
    await choose(stack, "react-vite+fastapi+postgres");
    await type($('[data-testid="dw-new-title"]'), "HelpDesk AI");
    await type($('[data-testid="dw-new-directive"]'), "A support desk for customers and agents, with tickets.");
    await click($('[data-testid="np-connector-login-oauth"]'));
    await click($('[data-testid="dw-new-start"]'));
    const sheet = $('[data-testid="tpl-sheet"]')!;
    expect((sheet.querySelector('[data-testid="tpl-note"]') as HTMLTextAreaElement).value)
      .toContain("Stack: React + Vite, FastAPI, PostgreSQL.");
    expect(sheet.querySelector('[data-testid="tpl-stack-python"]')?.getAttribute("aria-checked")).toBe("true");
  });

  it("keeps its copy to labels and tooltips: New app, the ideas dialog, Add a feature", async () => {
    // What this redesign drew; the steps' own lines and Start's reason are the timeline's copy.
    const drawn = () => [$('[data-testid="np-brief"]'), $(".np-head"), $(".np-plan-head"), $('[data-testid="np-flow"]'),
                         $('[data-testid="np-deploy-note"]'), $(".np-foot .np-actions")]
      .flatMap((el) => (el ? overBudget(el) : []));
    await mount();
    expect(drawn()).toEqual([]);
    await click($('[data-testid="np-ideas-open"]'));
    expect(overBudget($('[data-testid="np-ideas-dialog"]')!)).toEqual([]);
    await click($('[data-testid="np-ideas-close"]'));
    expect($('[data-testid="np-ideas-dialog"]')).toBeNull();
    await click($('[data-testid="np-mode-add"]'));
    await click($('[data-testid="np-app-crun_lead2"]'));
    expect(drawn()).toEqual([]);
  });

  it("New app's Start sends the stack chosen in the brief", async () => {
    await mount();
    await choose($('[data-testid="np-stack"]'), "react-vite+express+postgres");
    await type($('[data-testid="dw-new-title"]'), "HelpDesk AI");
    await type($('[data-testid="dw-new-directive"]'), "A support desk for customers and agents, with tickets.");
    await click($('[data-testid="dw-new-start"]'));
    expect(posts).toHaveLength(1);
    expect(String((posts[0].input as Record<string, unknown>).directive))
      .toBe("A support desk for customers and agents, with tickets.\n\nStack: React + Vite, Express, PostgreSQL.");
  });

  it("Add a feature: a connector the app already carries is shown, disabled", async () => {
    await mount({ mode: "add", appKey: "crun_lead2" });
    const login = $('[data-testid="np-connector-login-oauth"]')!;
    expect(login.textContent).toContain("Already in the app");
    expect(login.getAttribute("aria-disabled")).toBe("true");
    await click(login);
    expect(login.getAttribute("aria-pressed")).toBe("false");
  });

  it("Add a feature offers the connectors that fit the app, after the app is picked", async () => {
    await mount({ mode: "add" });
    expect($('[data-testid="np-add-connectors"]')).toBeNull();
    await click($('[data-testid="np-app-crun_cal"]'));
    await click($('[data-testid="np-connector-login-oauth"]'));
    await type($('[data-testid="np-gain"]'), "Let staff sign in");
    await click($('[data-testid="dw-new-start"]'));
    expect($('[data-testid="tpl-sheet"]')?.textContent).toContain("Change Calendar");
    expect(posts).toEqual([]);
  });
});

// "Talks to" is tested in composer-talks-to.test.tsx (one implementation: a new app's links travel
// with its start; an existing app's are made before the change starts).
