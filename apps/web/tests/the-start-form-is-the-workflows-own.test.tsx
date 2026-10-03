// @vitest-environment happy-dom
/**
 * Start opens the SELECTED workflow's own start form.
 *
 * boss-1 (2026-10-03): every non-software workflow was started through a software wizard.
 * "Trend to content" opened on "What should it do?" / "Who is it for?" / acceptance criteria
 * (ticked ✓ while empty), asked for a repository it would never use, never drew its own Trend
 * and Reviewer email fields, and said "Needs Reviewer email, Trend" under a Start that could not
 * be pressed. The subtitle promised a panel, a build and a deploy for all of them.
 *
 * This mounts the real composer against a fake /api at the fetch boundary, for each of the five
 * bundled examples' start forms exactly as their setup scripts publish them
 * (_helpers/example-start-forms.json, generated from examples/* by
 * _helpers/gen-example-start-forms.py; the last block checks it still matches), and for a
 * catalogue product template, which keeps the product questions and the repository.
 */
import { readFileSync } from "node:fs";
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import { overBudget } from "./_helpers/copy-budget";
import FORMS from "./_helpers/example-start-forms.json";
import type { CatalogueEntry, Installed } from "../app/delivery-workflows-ui";
import { NewProjectComposer } from "../app/workflows/new-project-composer";
import { workflowInputFields } from "../lib/workflow-inputs";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

type Form = { name: string; description: string; input_schema: Record<string, unknown>;
              steps: { id: string; kind: string; capability: string | null }[] };
const EXAMPLES = FORMS as Record<string, Form>;

/** The catalogue's product brief, as workflow_catalogue.input_schema_for builds it. */
const PRODUCT_SCHEMA = {
  type: "object",
  required: ["directive", "title"],
  properties: {
    directive: { type: "string", format: "textarea", title: "What should it do?",
                 description: "Who it is for and what a person can do with it.",
                 examples: ["A shift-swap board."] },
    title: { type: "string", title: "Name", examples: ["Shift swap board"] },
    repository: { type: "string", title: "Repository (optional)" },
    acceptance_criteria: { type: "array", items: { type: "string" },
                           title: "How you will know it works (optional)" },
    reference_links: { type: "array", items: { type: "string", format: "uri" },
                       title: "Links to read first (optional)" },
    notify_channel: { type: "string", title: "Who to tell",
                      examples: ["email:team@example.com"] },
  },
};
const PRODUCT: CatalogueEntry = {
  key: "standard_product_delivery", name: "Standard Product Delivery",
  description: "Idea to shipped, watched software. The full path, with gates.",
  work_class: "project.new", step_count: 9, steps: [], gates: ["release"],
  requires_project: ["repository"], needs: [{ key: "repository", label: "A GitHub repository" }],
  estimated_minutes: 60, input_schema: PRODUCT_SCHEMA,
};

const need = (key: string) => ({
  key, label: key, fix: "", area: null, ok: true, detail: "", needed_by: [],
  first_step_label: "", after_a_person: false,
});

type Posted = Record<string, unknown> & { input?: Record<string, unknown> };

function fakeApi(checked: Record<string, string[]>) {
  const posts: Posted[] = [];
  const fetchFake = vi.fn(async (input: unknown, init?: RequestInit) => {
    const url = new URL(String(input), "http://console.test");
    const json = (body: unknown, status = 200) =>
      new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
    if (init?.method === "POST" && url.pathname === "/api/delivery-workflows") {
      posts.push(JSON.parse(String(init.body)));
      return json({ run: { id: "wfr_started" } });
    }
    if (url.pathname === "/api/project-agents") return json({ default_roles: [] });
    if (url.pathname === "/api/agent") return json({ items: [] });
    const view = url.searchParams.get("view");
    if (view === "repositories") return json({ repositories: [], suggested: "acme/app",
                                                suggested_source: "registry_primary" });
    if (view === "readiness") {
      const keys = checked[url.searchParams.get("key") ?? ""] ?? [];
      return json({ ready: true, checked: keys.map(need), missing: [] });
    }
    return json({ error: "unexpected" }, 404);
  });
  return { posts, fetchFake };
}

let root: Root | null = null;
let host: HTMLElement | null = null;
afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  root = null; host = null;
  vi.unstubAllGlobals();
});

const $ = (sel: string) => host!.querySelector(sel) as HTMLElement | null;
const $$ = (sel: string) => [...host!.querySelectorAll(sel)] as HTMLElement[];
const settle = () => act(async () => { for (let i = 0; i < 5; i++) await Promise.resolve(); });

async function mount(o: { workflows: Installed[]; catalogue?: CatalogueEntry[];
                          startWith?: string; checked: Record<string, string[]> }) {
  const api = fakeApi(o.checked);
  vi.stubGlobal("fetch", api.fetchFake);
  const started: Array<string | null> = [];
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => {
    root!.render(
      <NewProjectComposer projectId="p1" actor="ceo" workflows={o.workflows}
                          catalogue={o.catalogue ?? []} startWith={o.startWith}
                          onStarted={(id) => started.push(id)} />);
  });
  await settle();
  return { ...api, started };
}

async function type(el: HTMLElement | null, value: string) {
  expect(el, "field to type into").not.toBeNull();
  const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype
    : HTMLInputElement.prototype;
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
const rail = () => $$('[data-testid="dw-wiz-rail"] li').map((li) => li.textContent?.replace(/^[\d✓]+/, ""));
const ticked = () => $$('[data-testid="dw-wiz-rail"] li').map((li) => li.dataset.state);

/** A well-formed value for a field, by the kind the composer draws for it. */
function valueFor(kind: string, name: string): string {
  if (kind === "email") return "reviewer@example.com";
  if (kind === "url") return "https://example.com/clip.mp4";
  if (kind === "number") return "3";
  return `the ${name} value`;
}

function installed(key: string, form: Form): Installed {
  return { id: `wf_${key}`, workflow_key: key, name: form.name, is_catalogue: false,
           forked_from_version_id: null, namespace: "product", description: form.description,
           input_schema: form.input_schema };
}

describe.each(Object.entries(EXAMPLES))("the %s example", (key, form) => {
  const fields = workflowInputFields(form.input_schema);
  const required = fields.filter((f) => f.required);

  it("opens on its own start form, with no product questions and no repository step", async () => {
    await mount({ workflows: [installed(key, form)], startWith: key,
                  checked: { [key]: ["model", "email"] } });
    for (const f of fields) {
      const control = $(`[data-testid="dw-workflow-input-${f.name}"]`);
      expect(control, f.name).not.toBeNull();
      expect($(`label[for="dw-workflow-input-${f.name}"]`)?.textContent).toBe(f.label);
      expect(control!.getAttribute("aria-required")).toBe(f.required ? "true" : null);
      if (f.kind === "multiline") expect(control!.tagName).toBe("TEXTAREA");
      if (f.kind === "email") expect(control!.getAttribute("type")).toBe("email");
      if (f.kind === "url") expect(control!.getAttribute("type")).toBe("url");
    }
    for (const id of ["dw-new-directive", "dw-new-audience", "dw-new-criterion-0"]) {
      expect($(`[data-testid="${id}"]`), id).toBeNull();
    }
    expect(host!.textContent).not.toContain("What should it do?");
    expect(rail()).toEqual(["What it needs", "How it runs", "Review"]);
  });

  it("says what THIS workflow does, on one line, never the product boilerplate", async () => {
    await mount({ workflows: [installed(key, form)], startWith: key, checked: {} });
    const sub = $('[data-testid="dw-new-subtitle"]')!;
    expect(sub.textContent).toBe(form.description.split("\n")[0].trim());
    expect(sub.getAttribute("title")).toBe(sub.textContent);
    expect(host!.textContent).not.toMatch(/panel specs it|builds, previews/i);
    expect($('[data-testid="dw-new-heading"]')?.textContent).toBe(form.name);
  });

  it("is not ✓ and cannot start until its required fields are filled", async () => {
    await mount({ workflows: [installed(key, form)], startWith: key, checked: {} });
    expect($('[data-testid="dw-wiz-start-now"]')!.hasAttribute("disabled")).toBe(true);
    expect($('[data-testid="dw-wiz-next"]')!.hasAttribute("disabled")).toBe(true);
    expect($('[data-testid="dw-wiz-blocked"]')?.textContent)
      .toBe(`Needs ${required.map((f) => f.label).join(", ")}`);
    for (const f of required) await type($(`[data-testid="dw-workflow-input-${f.name}"]`), valueFor(f.kind, f.name));
    expect($('[data-testid="dw-wiz-blocked"]')).toBeNull();
    expect($('[data-testid="dw-wiz-start-now"]')!.hasAttribute("disabled")).toBe(false);
    await click($('[data-testid="dw-wiz-next"]'));
    expect(ticked()).toEqual(["done", "current", "todo"]);
  });

  it("posts exactly its declared keys, with the values typed", async () => {
    const api = await mount({ workflows: [installed(key, form)], startWith: key, checked: {} });
    const expected: Record<string, string> = {};
    for (const f of fields) {
      expected[f.name] = valueFor(f.kind, f.name);
      await type($(`[data-testid="dw-workflow-input-${f.name}"]`), expected[f.name]);
    }
    // The review lists what it will be given.
    await click($('[data-testid="dw-wiz-next"]'));
    await click($('[data-testid="dw-wiz-next"]'));
    for (const f of fields) {
      expect($(`[data-testid="dw-review-input-${f.name}"]`)?.textContent).toContain(expected[f.name]);
    }
    expect($('[data-testid="dw-review-repo"]')).toBeNull();
    await click($('[data-testid="dw-new-start"]'));
    expect(api.posts).toHaveLength(1);
    expect(api.posts[0]).toMatchObject({ action: "start", project_id: "p1", workflow_key: key });
    // Nothing it did not ask for: no directive, no run title passed off as a YouTube title.
    expect(api.posts[0].input).toEqual(expected);
    expect(api.started).toEqual(["wfr_started"]);
  });

  it("leaves an optional field out rather than sending it blank", async () => {
    const api = await mount({ workflows: [installed(key, form)], startWith: key, checked: {} });
    for (const f of required) await type($(`[data-testid="dw-workflow-input-${f.name}"]`), valueFor(f.kind, f.name));
    await click($('[data-testid="dw-wiz-start-now"]'));
    expect(Object.keys(api.posts[0].input ?? {}).sort()).toEqual(required.map((f) => f.name).sort());
  });

  it("keeps its copy to labels and tooltips", async () => {
    await mount({ workflows: [installed(key, form)], startWith: key, checked: {} });
    for (const sel of ['[data-testid="dw-wiz-step-brief"]', ".dw-new-head", '[data-testid="dw-wiz-rail"]']) {
      expect(overBudget($(sel)!), sel).toEqual([]);
    }
  });
});

describe("a malformed value is caught at the form", () => {
  it("an email field that is not an address blocks Start and says which", async () => {
    const form = EXAMPLES["trend-to-content"];
    await mount({ workflows: [installed("trend", form)], startWith: "trend", checked: {} });
    await type($('[data-testid="dw-workflow-input-trend"]'), "AI coding setups");
    await type($('[data-testid="dw-workflow-input-to"]'), "not an address");
    expect($('[data-testid="dw-wiz-blocked"]')?.textContent).toBe("Check Reviewer email");
    expect($('[data-testid="dw-wiz-start-now"]')!.hasAttribute("disabled")).toBe(true);
  });
});

describe("the start form's own types and defaults", () => {
  const schema = {
    required: ["count"],
    properties: {
      count: { type: "integer", title: "How many", default: 5 },
      urgent: { type: "boolean", title: "Urgent", description: "Skip the queue." },
    },
  };
  const wf: Installed = { id: "wf_x", workflow_key: "typed", name: "Typed", is_catalogue: false,
                          forked_from_version_id: null, namespace: "product",
                          description: "Counts things.", input_schema: schema };

  it("draws a number with its default and a yes/no, and sends them typed", async () => {
    const api = await mount({ workflows: [wf], startWith: "typed", checked: {} });
    const count = $('[data-testid="dw-workflow-input-count"]') as HTMLInputElement;
    expect(count.type).toBe("number");
    expect(count.value).toBe("5");
    const urgent = $('[data-testid="dw-workflow-input-urgent"]') as HTMLInputElement;
    expect(urgent.type).toBe("checkbox");
    await click(urgent);
    await click($('[data-testid="dw-wiz-start-now"]'));
    expect(api.posts[0].input).toEqual({ count: 5, urgent: true });
  });
});

describe("a catalogue product template keeps the product brief", () => {
  const spd: Installed = { id: "wf_spd", workflow_key: PRODUCT.key, name: PRODUCT.name,
                           is_catalogue: true, forked_from_version_id: null, namespace: "product",
                           input_schema: PRODUCT_SCHEMA };

  it("asks the product questions, its other fields, and where it lives", async () => {
    await mount({ workflows: [spd], catalogue: [PRODUCT], startWith: PRODUCT.key,
                  checked: { [PRODUCT.key]: ["repository", "model", "deployment_target"] } });
    expect($('[data-testid="dw-new-directive"]')).not.toBeNull();
    expect($('[data-testid="dw-new-audience"]')).not.toBeNull();
    expect($('[data-testid="dw-new-criterion-0"]')).not.toBeNull();
    // What the product questions and "Where it lives" collect is not asked twice.
    expect($$('[data-testid^="dw-workflow-input-"]').map((e) => e.dataset.testid))
      .toEqual(["dw-workflow-input-notify_channel"]);
    expect(rail()).toEqual(["What you are building", "How it gets built", "Where it lives", "Review"]);
    expect($('[data-testid="dw-new-subtitle"]')?.textContent)
      .toBe("Idea to shipped, watched software. The full path, with gates.");
  });

  it("starts with the brief's own keys and the repository", async () => {
    const api = await mount({ workflows: [spd], catalogue: [PRODUCT], startWith: PRODUCT.key,
                              checked: { [PRODUCT.key]: ["repository"] } });
    expect($('[data-testid="dw-wiz-blocked"]')?.textContent).toMatch(/^Describe what you want built/);
    await type($('[data-testid="dw-new-directive"]'), "A shift-swap board for the duty team.");
    await type($('[data-testid="dw-workflow-input-notify_channel"]'), "email:team@example.com");
    await click($('[data-testid="dw-wiz-next"]'));
    await click($('[data-testid="dw-wiz-next"]'));
    expect($('[data-testid="dw-wiz-step-where"]')).not.toBeNull();
    await click($('[data-testid="dw-wiz-next"]'));
    expect($('[data-testid="dw-review-repo"]')?.textContent).toBe("acme/app");
    await click($('[data-testid="dw-new-start"]'));
    expect(api.posts[0]).toMatchObject({ workflow_key: PRODUCT.key });
    expect(api.posts[0].input).toEqual({
      title: "A shift-swap board for the duty team.",
      directive: "A shift-swap board for the duty team.",
      notify_channel: "email:team@example.com",
    });
  });

  it("drops the repository step when the readiness check says no step needs one", async () => {
    // Research-Led Discovery and the like end in a decision, not in code.
    await mount({ workflows: [spd], catalogue: [PRODUCT], startWith: PRODUCT.key,
                  checked: { [PRODUCT.key]: ["model"] } });
    expect(rail()).toEqual(["What you are building", "How it gets built", "Review"]);
  });
});

describe("the fixture is what the example setup scripts publish", () => {
  it.each(Object.entries(EXAMPLES))("%s", (key, form) => {
    const source = readFileSync(`../../examples/${key}/setup.py`, "utf8");
    const schema = form.input_schema as { required?: string[]; properties: Record<string, { title?: string; format?: string }> };
    expect(source).toContain(`"required": ${JSON.stringify(schema.required ?? []).replace(/,/g, ", ")}`);
    for (const [name, prop] of Object.entries(schema.properties)) {
      expect(source).toMatch(new RegExp(`"${name}": \\{`));
      if (prop.title) expect(source).toContain(`"title": ${JSON.stringify(prop.title)}`);
      if (prop.format) expect(source).toContain(`"format": "${prop.format}"`);
    }
    expect(source).toContain(`"${form.name}"`);
  });
});
