// @vitest-environment happy-dom
/**
 * "ONBOARD A CONFIG FILE" STARTS FROM ITS OWN FORM (dry run 3, gap #5).
 *
 * Its start form declares `directive` (its build reads one), so the composer took it for a
 * product brief and drew the app builder: Stack, Connectors (Stripe, PayPal, SMTP), Who is it
 * for. It now draws only its own inputs — Original file (an upload), Target path, Title — and
 * sends the workflow's own declared brief. New work's picker, which cannot start it from a
 * brief, opens that form (`start=<key>`).
 *
 * The schema is the catalogue's own, as published (tests/fixtures/catalogue-specs.json).
 */
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { CatalogueEntry, Installed } from "../app/delivery-workflows-ui";
import { IdentityProvider } from "../components/identity/identity-provider";
import type { MePermissionsResponse, MeResponse } from "../lib/identity/contract";
import { ROLE_PERMISSIONS } from "../lib/identity/permissions.gen";
import { parseWorkPlan } from "../lib/project-experience";
import {
  fileFormFields, fileStartExtras, startFormHref, startParam, startsFromABrief, startsFromAFile,
} from "../lib/workflow-inputs";
import fixture from "./fixtures/catalogue-specs.json";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const { NewProjectComposer } = await import("../app/workflows/new-project-composer");

const SPECS = new Map<string, unknown>((fixture as { catalogue: { workflow_key: string; spec: unknown }[] }).catalogue.map((w) => [w.workflow_key, w.spec]));
const SCHEMA = (SPECS.get("onboard_config_file") as { input_schema: unknown }).input_schema;
const MINOR = (SPECS.get("minor_feature") as { input_schema: unknown }).input_schema;

describe("a file workflow is not a product brief", () => {
  it("the catalogue's onboard schema starts from a file; an app brief does not", () => {
    expect(startsFromAFile(SCHEMA)).toBe(true);
    expect(startsFromABrief(SCHEMA)).toBe(false);
    expect(startsFromAFile(MINOR)).toBe(false);
    expect(startsFromABrief(MINOR)).toBe(true);
  });
  it("draws only Original file, Target path, Title", () => {
    const fields = fileFormFields(SCHEMA);
    expect(fields.map((f) => f.name)).toEqual(["start_from", "target_path", "title"]);
    expect(fields[0].label).toBe("Original file");
    expect(fields.every((f) => f.required)).toBe(true);
  });
  it("sends the uploaded text as given and the workflow's own brief", () => {
    const out = fileStartExtras(SCHEMA, { start_from_content: "a: 1\n" });
    expect(out.start_from_content).toBe("a: 1\n");
    expect(out.directive).toMatch(/repository's own checkers/);
  });
  it("New work links to the Start form, and the Workflows area reads it", () => {
    const href = startFormHref("northgate", "onboard_config_file");
    expect(startParam(href.slice(1))).toBe("onboard_config_file");
    expect(startParam("?area=project&start=x")).toBeNull();
    expect(parseWorkPlan({ options: [{ key: "onboard_config_file", name: "Onboard a config file", starts_from_file: true }] })
      .options[0].starts_from_file).toBe(true);
  });
});

// ── the real composer ────────────────────────────────────────────────────────────────────────

const STEPS = [{ id: "build", label: "Fix the file", kind: "capability", mine: false, approval: "auto", captain: "engineering_captain" }];
const WORKFLOWS = [{
  id: "wfd_onb", workflow_key: "onboard_config_file", name: "Onboard a config file", is_catalogue: true,
  forked_from_version_id: null, namespace: "product", input_schema: SCHEMA, step_count: 5, steps: STEPS,
}] as unknown as Installed[];
const CATALOGUE = [{
  key: "onboard_config_file", name: "Onboard a config file", description: "A file arrives.", work_class: "any",
  step_count: 5, steps: [{ id: "build", capability: "build" }], gates: [], automatic_approvals: [],
  requires_project: ["repository"], estimated_minutes: 20, input_schema: SCHEMA,
}] as unknown as CatalogueEntry[];

let posts: Array<Record<string, unknown> & { input?: Record<string, unknown> }> = [];
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
    if (url.pathname === "/v1/factory/apps") return json({ apps: [] });
    if (url.pathname === "/api/project-agents") return json({ default_roles: ["pm"] });
    if (url.pathname === "/api/agent") return json({ items: [] });
    const view = url.searchParams.get("view");
    if (view === "repositories") return json({ repositories: [{ id: "r1", full_name: "acme/gw", default_branch: "main" }],
                                                suggested: "acme/gw", suggested_source: "registry_primary" });
    if (view === "readiness") return json({ ready: true, checked: [], missing: [] });
    return json({ error: "unexpected" }, 404);
  }));
}
const ME = {
  principal_id: "usr_x", kind: "user", display_name: "Olu", email: "o@acme.dev", org: { id: "org_1", name: "Acme" }, status: "active",
  access_roles: ["operator"], sign_in: { password: true, totp: false, passkeys: 0, recovery_codes_remaining: 0, sso_links: [] },
  mfa_required: false, mfa_at: null, session_id: "s", session_expires_at: null, csrf_token: "c", epoch: 1, network_bound: false,
  banners: [], avatar_seed: null,
} as MeResponse;
const PERMS: MePermissionsResponse = { epoch: 1, scope: "org", permissions: [...ROLE_PERMISSIONS.operator], step_up_fresh_until: null, catalogue_fingerprint: "x" };

let root: Root | null = null;
let host: HTMLElement | null = null;
afterEach(() => { act(() => root?.unmount()); host?.remove(); root = null; host = null; vi.unstubAllGlobals(); });
const $ = (sel: string) => host!.querySelector(sel) as HTMLElement | null;
const settle = () => act(async () => { for (let i = 0; i < 10; i++) await Promise.resolve(); });
async function type(el: HTMLElement | null, value: string) {
  expect(el).not.toBeNull();
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(el, value);
    el!.dispatchEvent(new Event("input", { bubbles: true }));
  });
  await settle();
}

describe("its Start form in the composer", () => {
  it("shows only its own inputs and starts with the file as given", async () => {
    stubApi();
    host = document.createElement("div");
    document.body.appendChild(host);
    root = createRoot(host);
    await act(async () => {
      root!.render(
        <IdentityProvider me={ME} org={PERMS}>
          <NewProjectComposer projectId="p1" actor="ceo" workflows={WORKFLOWS} catalogue={CATALOGUE}
                              startWith="onboard_config_file" onStarted={() => {}} onCancel={() => {}} />
        </IdentityProvider>);
    });
    await settle();
    const names = [...host.querySelectorAll('[data-testid="dw-workflow-inputs"] .dw-field')]
      .map((el) => el.querySelector("input,textarea")?.getAttribute("id"));
    expect(names).toEqual(["dw-workflow-input-start_from", "dw-workflow-input-target_path", "dw-workflow-input-title"]);
    expect(($('[data-testid="dw-workflow-input-start_from"]') as HTMLInputElement).type).toBe("file");
    const text = host.textContent ?? "";
    for (const appBuilder of ["Stack", "Connectors", "Stripe", "PayPal", "SMTP", "Who is it for", "What should the fix do"]) {
      expect(text, appBuilder).not.toContain(appBuilder);
    }
    expect($("#np-stack")).toBeNull();
    expect($('[data-testid="dw-new-audience"]')).toBeNull();

    const upload = $('[data-testid="dw-workflow-input-start_from"]') as HTMLInputElement;
    const file = new File(["tenant: lumen-pay\n"], "lumen-pay.yaml", { type: "text/yaml" });
    Object.defineProperty(upload, "files", { value: [file], configurable: true });
    await act(async () => { upload.dispatchEvent(new Event("change", { bubbles: true })); });
    await settle();
    expect($('[data-testid="dw-workflow-input-start_from-name"]')?.textContent).toBe("lumen-pay.yaml");
    await type($('[data-testid="dw-workflow-input-target_path"]'), "config/tenants/lumen-pay.yaml");
    await type($('[data-testid="dw-workflow-input-title"]'), "Lumen Pay tenant onboarding");
    await act(async () => { $('[data-testid="dw-new-start"]')!.click(); });
    await settle();
    const started = posts.find((p) => p.input)!;
    expect(started, "a start was posted").toBeTruthy();
    expect(started.input).toMatchObject({
      start_from: "lumen-pay.yaml", start_from_content: "tenant: lumen-pay\n",
      target_path: "config/tenants/lumen-pay.yaml", title: "Lumen Pay tenant onboarding",
    });
    expect(String(started.input!.directive)).toMatch(/repository's own checkers/);
    expect(started.input).not.toHaveProperty("acceptance_criteria");
  });
});
