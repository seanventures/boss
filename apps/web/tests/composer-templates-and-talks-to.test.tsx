// @vitest-environment happy-dom
/**
 * The composer's template door and "Talks to" (APP_TEMPLATES_AND_SECURITY_LOOP.md §2.8, §5.5;
 * tester P0-5): the mode switch is A new app · Add to an existing app · From a template; From a
 * template is the gallery (a card opens its sheet; ⌘↵ in the composer starts nothing); "+ Template"
 * in add mode opens the gallery for the chosen app, greyed until an app is picked; "Talks to" makes
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
    secrets: [], tested: ["node"] },
] };

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
    if (url.pathname.startsWith("/v1/apps/") && url.pathname.endsWith("/templates")) return json({ templates: [{ id: "t", template_key: "x", stack: "node", status: "applied" }] });
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
beforeEach(() => { started.length = 0; linked.length = 0; try { window.localStorage.clear(); } catch { /* none */ } });
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

describe("From a template", () => {
  it("is the third job of the mode switch, opens the gallery, and a card its sheet", async () => {
    await mount();
    expect($('[data-testid="np-mode-template"]')?.textContent).toContain("From a template");
    await click($('[data-testid="np-mode-template"]'));
    expect($('[data-testid="dw-new-project"]')?.dataset.mode).toBe("template");
    // One job is chosen at a time: "A new app" is not lit beside it.
    expect($('[data-testid="np-mode-new"]')?.getAttribute("aria-checked")).toBe("false");
    expect($('[data-testid="np-mode-template"]')?.getAttribute("aria-checked")).toBe("true");
    expect($('[data-testid="np-templates"] [data-testid="tpl-card-login-oauth"]')).not.toBeNull();
    expect($('[data-testid="dw-new-start"]')).toBeNull();          // Start is the sheet's own
    await act(async () => { $('[data-testid="np-templates"]')!.dispatchEvent(new KeyboardEvent("keydown", { bubbles: true, key: "Enter", metaKey: true })); });
    await settle();
    expect(posts).toEqual([]);
    await click($('[data-testid="tpl-card-login-oauth"]'));
    expect($('[data-testid="tpl-sheet"]')?.textContent).toContain("Login");
    expect(window.localStorage.getItem("boss.compose.mode")).toBe("template");
  });

  it("a link can open it: ?compose=template", () => {
    expect(readComposeIntent("?compose=template").mode).toBe("template");
  });

  it("+ Template waits for an app, then opens the gallery for that app", async () => {
    await mount({ mode: "add" });
    const chip = $('[data-testid="np-add-template"]')!;
    expect(chip.getAttribute("aria-disabled")).toBe("true");
    expect(chip.getAttribute("title")).toBe("Pick the app first");
    await click(chip);
    expect($('[data-testid="np-template-picker"]')).toBeNull();
    await click($('[data-testid="np-app-crun_cal"]'));
    await click($('[data-testid="np-add-template"]'));
    expect($('[data-testid="np-template-picker"]')).not.toBeNull();
    await click($('[data-testid="tpl-card-login-oauth"]'));
    expect($('[data-testid="tpl-sheet"]')?.textContent).toContain("Change Calendar");
  });
});

// "Talks to" is tested in composer-talks-to.test.tsx (one implementation: a new app's links travel
// with its start; an existing app's are made before the change starts).
