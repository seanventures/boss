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
const { parseStackSupport } = await import("../lib/templates");
const { adapterLanguage, adapterWord } = await import("../components/v2/templates/stack-warning");
const { distinctTemplates, shortList } = await import("../app/workflows/app-context");
const { readFileSync } = await import("node:fs");

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
const comp = (role: string, language: string) => ({ role, language });
const STACK_ADAPTERS = [
  { id: "react-vite+express+postgres", label: "React + Vite, Express, PostgreSQL", support_level: "experimental",
    languages: ["node"], components: [comp("frontend", "node"), comp("backend", "node")], ready: true },
  { id: "react-vite+fastapi+postgres", label: "React + Vite, FastAPI, PostgreSQL", support_level: "experimental",
    languages: ["node", "python"], components: [comp("frontend", "node"), comp("backend", "python")], ready: true },
  // Java first, its React front end last: the LAST language is Node, the builder is Java.
  { id: "java-spring", label: "Java Spring Boot (with an optional React + Vite front end)", support_level: "experimental",
    languages: ["java", "node"], components: [comp("backend", "java"), comp("frontend", "node")], ready: true },
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
    // Every card shows a drawing of its app's screen, never the plain glyph cover.
    expect($('[data-testid="np-idea-hello-endpoint"] svg')?.getAttribute("aria-label"))
      .toMatch(/^Hello endpoint preview: a terminal/);
    const cards = [...$('[data-testid="np-ideas"]')!.querySelectorAll(".np-idea")];
    expect(cards.length).toBeGreaterThan(16);
    expect(cards.filter((c) => !c.querySelector("svg.ip-svg")).map((c) => c.querySelector("b")?.textContent)).toEqual([]);
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

  it("several connectors: each card toggles, says its place, and Start opens ONE build for them all (BOSSAPP-526)", async () => {
    await mount();
    const login = $('[data-testid="np-connector-login-oauth"]')!;
    const stripe = $('[data-testid="np-connector-stripe-checkout"]')!;
    expect($('[data-testid="np-connectors-note"]')!.textContent).toBe("Up to 6 in one build");
    await click(login);
    await click(stripe);
    expect(stripe.getAttribute("aria-disabled")).toBeNull();
    expect(stripe.getAttribute("aria-pressed")).toBe("true");
    expect(login.textContent).toContain("Added ✓ · 1 of 2");
    expect(stripe.textContent).toContain("Added ✓ · 2 of 2");
    expect($('[data-testid="np-connectors-note"]')!.textContent).toBe("2 added · one build");
    await click(login);                                             // unpicking one keeps the other
    expect(login.getAttribute("aria-pressed")).toBe("false");
    expect(stripe.textContent).toContain("Added ✓");
    await click(login);                                             // picked again: now second
    await type($('[data-testid="dw-new-title"]'), "Shop");
    await type($('[data-testid="dw-new-directive"]'), "A shop with sign-in and card payments.");
    await click($('[data-testid="dw-new-start"]'));
    expect(posts).toEqual([]);
    expect($('[data-testid="tpl-sheet"]')).toBeNull();              // not the single connector's sheet
    const sheet = $('[data-testid="cmp-sheet"]')!;
    expect([...sheet.querySelectorAll('[data-testid^="cmp-connector-"]')].map((e) => e.getAttribute("data-testid")))
      .toEqual(["cmp-connector-stripe-checkout", "cmp-connector-login-oauth"]);
    expect((sheet.querySelector('[data-testid="cmp-app-name"]') as HTMLInputElement).value).toBe("Shop");
    expect((sheet.querySelector('[data-testid="cmp-note"]') as HTMLTextAreaElement).value).toContain("sign-in and card payments");
  });

  it("several connectors in Add a feature: the app already carries one, which never counts", async () => {
    await mount({ mode: "add", appKey: "crun_lead2" });
    await click($('[data-testid="np-connector-stripe-checkout"]'));
    await type($('[data-testid="np-gain"]'), "Take card payments");
    await click($('[data-testid="dw-new-start"]'));
    // ONE new connector (Login is in the app): its own sheet, the unchanged single path.
    expect($('[data-testid="tpl-sheet"]')?.textContent).toContain("Stripe Checkout");
    expect($('[data-testid="cmp-sheet"]')).toBeNull();
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

// ── The independent walk's findings (fix/new-project-redesign 1c9f3ea8), each as a class ────────

/** compose.css with comments removed, and its phone block. */
const CSS = readFileSync("app/workflows/compose.css", "utf8").replace(/\/\*[\s\S]*?\*\//g, "");
const PHONE = CSS.slice(CSS.indexOf("@media (max-width: 720px)"));
const ruleFor = (css: string, selector: string) => {
  const i = css.indexOf(`${selector} {`);
  return i < 0 ? "" : css.slice(i, css.indexOf("}", i));
};

describe("the walk's findings", () => {
  it("B1: a dialog taller than the screen never starts off it — the card is viewport-high and only its cards scroll", async () => {
    // Two classes beat the later `.np-fs { place-items: center }`, and the overlay itself never scrolls.
    expect(ruleFor(CSS, ".np-fs.np-ideas-fs")).toContain("overflow: hidden");
    expect(ruleFor(CSS, ".np-ideas-card")).toMatch(/max-height: calc\(100d?vh/);
    expect(ruleFor(CSS, ".np-ideas-scroll")).toContain("overflow-y: auto");
    expect(ruleFor(PHONE, ".np-ideas-card")).toContain("max-height: 100dvh");
    await mount();
    await click($('[data-testid="np-ideas-open"]'));
    const scroll = $('[data-testid="np-ideas-scroll"]')!;
    // The header, Close and the filters are outside what scrolls; the cards inside it.
    for (const sel of ["#np-ideas-h", '[data-testid="np-ideas-close"]', '[data-testid="np-ideas-tab-all"]']) {
      expect(scroll.contains($(sel)), sel).toBe(false);
    }
    expect(scroll.contains($('[data-testid="np-ideas"]'))).toBe(true);
  });

  it("M2: nothing the composer needs is hidden on a phone; Change workflow sits outside the fold", async () => {
    const hidden = [...PHONE.matchAll(/([^{}]+)\{[^}]*display:\s*none/g)].map((m) => m[1].trim());
    // Only the keyboard hint and the plan's folded body (its toggle opens it) may vanish.
    expect(hidden.sort()).toEqual([".np-kbd", ".np-plan:not([data-open]) .np-plan-body"]);
    await mount();
    const change = $('[data-testid="np-all-paths"]')!;
    expect(change.closest(".np-plan-body")).toBeNull();
    await click(change);                                          // it opens the fold, with the paths
    expect($(".np-plan")?.hasAttribute("data-open")).toBe(true);
    expect($('[data-testid="np-chooser"]')).not.toBeNull();
  });

  it("M1: a connector choice never crosses jobs or apps, and one the app has never counts", async () => {
    await mount();
    await click($('[data-testid="np-connector-login-oauth"]'));     // chosen for a NEW app…
    await click($('[data-testid="np-mode-add"]'));
    await click($('[data-testid="np-app-crun_lead2"]'));            // …which this app already carries
    const login = $('[data-testid="np-connector-login-oauth"]')!;
    const stripe = $('[data-testid="np-connector-stripe-checkout"]')!;
    expect(login.getAttribute("aria-pressed")).toBe("false");
    expect(login.textContent).toContain("Already in the app");
    expect(stripe.getAttribute("aria-disabled")).toBeNull();       // the in-app one does not count
    // Start starts the feature: no connector sheet.
    await type($('[data-testid="np-gain"]'), "Let a sales rep snooze a lead until a date.");
    await click($('[data-testid="dw-new-start"]'));
    expect($('[data-testid="tpl-sheet"]')).toBeNull();
    expect(posts).toHaveLength(1);
  });

  it("M1: switching app forgets the connector chosen for the last one", async () => {
    await mount({ mode: "add", appKey: "crun_cal" });
    await click($('[data-testid="np-connector-stripe-checkout"]'));
    expect($('[data-testid="np-connector-stripe-checkout"]')!.getAttribute("aria-pressed")).toBe("true");
    await click($('[data-testid="np-app-change"]'));
    await click($('[data-testid="np-app-crun_lead2"]'));
    expect($('[data-testid="np-connector-stripe-checkout"]')!.getAttribute("aria-pressed")).toBe("false");
  });

  it("M3: a stack is named and sent by its backend's language, never by the order of its languages", () => {
    const { adapters } = parseStackSupport({ supported: { languages: [], adapters: STACK_ADAPTERS } });
    const java = adapters.find((a) => a.id === "java-spring")!;
    expect(adapterLanguage(java)).toBe("java");
    expect(adapterWord(java)).toBe("Java Spring Boot (with an optional React + Vite front end)");
    expect(adapterLanguage(adapters.find((a) => a.id === "react-vite+fastapi+postgres")!)).toBe("python");
    expect(adapterWord(adapters.find((a) => a.id === "react-vite+express+postgres")!)).toBe("Node · React + Vite, Express, PostgreSQL");
  });

  it("the app card lists each connector once and keeps long lists short, the rest in a tooltip", () => {
    const a = (key: string, version: string, createdAt: string, status = "applied") =>
      ({ id: `${key}${version}`, templateId: "", key, name: key, version, stack: "node", status, workflowRunId: "", codeTaskRunId: "", createdAt,
         blockedReason: "", secrets: [], signInProviders: [] });
    expect(distinctTemplates([a("login", "1.0.0", "2026-10-01"), a("login", "1.1.0", "2026-10-05"), a("x", "1", "", "abandoned")])
      .map((t) => t.version)).toEqual(["1.1.0"]);
    expect(shortList(["A", "B", "C", "D", "E"])).toBe("A, B, C +2");
    expect(shortList(["A", "B"])).toBe("A, B");
  });

  it("picking an idea over a typed brief offers Undo, which puts the brief and name back", async () => {
    await mount();
    await type($('[data-testid="dw-new-title"]'), "Desk");
    await type($('[data-testid="dw-new-directive"]'), "My own brief, written by hand, for a support desk.");
    await click($('[data-testid="np-ideas-open"]'));
    await click($('[data-testid="np-idea-incident-timeline-builder"]'));
    expect(($('[data-testid="dw-new-directive"]') as HTMLTextAreaElement).value).toMatch(/^Build an incident timeline builder/);
    await click($('[data-testid="np-idea-undo"]'));
    expect(($('[data-testid="dw-new-directive"]') as HTMLTextAreaElement).value).toBe("My own brief, written by hand, for a support desk.");
    expect(($('[data-testid="dw-new-title"]') as HTMLInputElement).value).toBe("Desk");
    expect($('[data-testid="np-idea-undo"]')).toBeNull();
    // Over an empty brief there is nothing to undo.
    await type($('[data-testid="dw-new-directive"]'), "");
    await click($('[data-testid="np-ideas-open"]'));
    await click($('[data-testid="np-idea-incident-timeline-builder"]'));
    expect($('[data-testid="np-idea-undo"]')).toBeNull();
  });

  it("names, tooltips and placeholders say what they are", async () => {
    await mount();
    const plan = $('[data-testid="np-plan-agent"]')!;
    expect(plan.getAttribute("aria-label")).toBe("Plan it with an agent");
    expect(document.getElementById(plan.getAttribute("aria-describedby")!)?.textContent).toContain("a PRD, then the build");
    expect($('[data-testid="dw-new-title"]')!.getAttribute("placeholder")).toBe("Your app's name");
    expect($('[data-testid="np-team-names"]')!.getAttribute("title")).toBeTruthy();
    const repo = $('[data-testid="np-repo-name"]')!;
    expect(repo.classList.contains("np-fact-name")).toBe(true);
    expect(repo.getAttribute("title")).toBe("acme/sandbox");
    expect(ruleFor(CSS, ".np-fact b.np-fact-name")).toContain("white-space: nowrap");
    expect(ruleFor(CSS, '.np-conn-card[data-in-app] .np-conn-state')).toContain("white-space: normal");
  });
});

// "Talks to" is tested in composer-talks-to.test.tsx (one implementation: a new app's links travel
// with its start; an existing app's are made before the change starts).
