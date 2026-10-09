// @vitest-environment happy-dom
/**
 * THE COMPOSER'S "TALKS TO" (APP_TEMPLATES_AND_SECURITY_LOOP.md §5.5): when creating an app or
 * adding to one, pick the project's other apps it should call — app, alias, Preview/Deployed,
 * test users. Gated on `app_link:manage`.
 *
 *   - An existing app: each row becomes POST /v1/apps/{app}/links (createLinkAction) BEFORE the
 *     change starts, so its build already has BOSS_LINK_<ALIAS>_*. A refused link says which and
 *     why and starts nothing.
 *   - A new app: it does not exist until its first build, so the rows travel with the start as
 *     `input.app_links` (the API checks them and the first build makes them:
 *     apps/api/tests/test_start_requests_app_links.py).
 *   - Without `app_link:manage` the field is greyed with the reason, and a press adds nothing.
 *
 * The real composer, under the real IdentityProvider, against a fake API at the fetch boundary.
 */
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { CatalogueEntry, Installed } from "../app/delivery-workflows-ui";
import { IdentityProvider } from "../components/identity/identity-provider";
import type { MePermissionsResponse, MeResponse } from "../lib/identity/contract";
import { ROLE_PERMISSIONS } from "../lib/identity/permissions.gen";
import { needsWords } from "../lib/identity/words";

const linked: Record<string, string>[] = [];
let refuse: string | null = null;
vi.mock("../app/factory/links-actions", () => ({
  createLinkAction: vi.fn(async (form: FormData) => {
    const sent = Object.fromEntries([...form.entries()].filter(([k]) => !k.startsWith("__")).map(([k, v]) => [k, String(v)]));
    if (refuse && sent.alias === refuse) return { ok: false, error: "this app already has an active link with that alias" };
    linked.push(sent);
    return { ok: true, message: `Linked as ${sent.alias}` };
  }),
  revokeLinkAction: vi.fn(), revealTestUsersAction: vi.fn(),
}));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const { NewProjectComposer } = await import("../app/workflows/new-project-composer");
const { aliasFor, linkRequests, talksToProblem } = await import("../app/workflows/talks-to");

const BRIEF = { type: "object", required: ["directive", "title"],
                properties: { directive: { type: "string", format: "textarea" }, title: { type: "string" } } };
const WORKFLOWS: Installed[] = [{
  id: "wfd_minor", workflow_key: "minor_feature", name: "minor feature", is_catalogue: true,
  forked_from_version_id: null, namespace: "product", input_schema: BRIEF, step_count: 1,
  steps: [{ id: "build", label: "build", kind: "capability", mine: false, approval: "auto", captain: "engineering_captain" }],
}, {
  id: "wfd_std", workflow_key: "standard_product_delivery", name: "standard", is_catalogue: true,
  forked_from_version_id: null, namespace: "product", input_schema: BRIEF, step_count: 1,
  steps: [{ id: "build", label: "build", kind: "capability", mine: false, approval: "auto", captain: "engineering_captain" }],
}] as Installed[];
const CATALOGUE: CatalogueEntry[] = WORKFLOWS.map((w) => ({
  key: w.workflow_key, name: w.name, description: "", work_class: "project.new", step_count: 1,
  steps: [{ id: "build", capability: "build" }], gates: [], automatic_approvals: [],
  requires_project: ["repository"], estimated_minutes: 30, input_schema: BRIEF,
})) as CatalogueEntry[];
const APPS = { apps: [
  { key: "crun_bank", app: "Banking", run_id: "crun_bank", builders: ["pm"], state: "pr_open", word: "PR open",
    builds: 1, run_ids: ["crun_bank"], attempts: [{ run_id: "crun_bank", attempt: 1, word: "PR open", order: 1 }] },
  { key: "crun_att", app: "Bank attacker", run_id: "crun_att", builders: ["pm"], state: "pr_open", word: "PR open",
    builds: 1, run_ids: ["crun_att"], attempts: [{ run_id: "crun_att", attempt: 1, word: "PR open", order: 1 }] },
] };

type Posted = Record<string, unknown> & { input?: Record<string, unknown> };
let posts: Posted[] = [];
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
    if (url.pathname === "/api/project-agents") return json({ default_roles: ["pm"] });
    if (url.pathname === "/api/agent") return json({ items: [] });
    const view = url.searchParams.get("view");
    if (view === "repositories") return json({ repositories: [{ id: "r1", full_name: "acme/sandbox", default_branch: "main" }],
                                                suggested: "acme/sandbox", suggested_source: "registry_primary" });
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
/** An operator; `withoutLinks` is a custom role that may start work but not link apps. */
function perms(withoutLinks = false): MePermissionsResponse {
  const all = [...ROLE_PERMISSIONS.operator].filter((p) => !(withoutLinks && p === "app_link:manage"));
  return { epoch: 1, scope: "org", permissions: all, step_up_fresh_until: null, catalogue_fingerprint: "x" };
}

let root: Root | null = null;
let host: HTMLElement | null = null;
const started: Array<string | null> = [];
beforeEach(() => { linked.length = 0; started.length = 0; refuse = null; try { window.localStorage.clear(); } catch { /* none */ } });
afterEach(() => { act(() => root?.unmount()); host?.remove(); root = null; host = null; vi.unstubAllGlobals(); });

const $ = (sel: string) => host!.querySelector(sel) as HTMLElement | null;
const settle = () => act(async () => { for (let i = 0; i < 10; i++) await Promise.resolve(); });
async function mount(props: Partial<React.ComponentProps<typeof NewProjectComposer>>, withoutLinks = false) {
  stubApi();
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => {
    root!.render(
      <IdentityProvider me={ME} org={perms(withoutLinks)}>
        <NewProjectComposer projectId="p1" actor="ceo" workflows={WORKFLOWS} catalogue={CATALOGUE}
                            onStarted={(id) => started.push(id)} onCancel={() => {}} {...props} />
      </IdentityProvider>);
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
async function choose(el: HTMLElement | null, value: string) {
  expect(el, "select to choose in").not.toBeNull();
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value")!.set!.call(el, value);
    el!.dispatchEvent(new Event("change", { bubbles: true }));
  });
  await settle();
}
async function click(el: HTMLElement | null) {
  expect(el, "element to click").not.toBeNull();
  await act(async () => { el!.click(); });
  await settle();
}

describe("Talks to: the rules", () => {
  it("an alias comes from the app's name, as the API takes it", () => {
    expect(aliasFor("Banking")).toBe("BANKING");
    expect(aliasFor("Bank attacker")).toBe("BANK_ATTACKER");
    expect(aliasFor("3D viewer")).toBe("APP_3D_VIEWER");
    expect(aliasFor("A very long application name that goes on and on")).toMatch(/^[A-Z][A-Z0-9_]{1,30}$/);
  });
  it("says what is wrong before anything is sent", () => {
    const row = { provider: "crun_bank", alias: "BANK", env: "preview" as const, testUsers: true };
    expect(talksToProblem([row])).toBe("");
    expect(talksToProblem([{ ...row, provider: "" }])).toMatch(/pick the app/);
    expect(talksToProblem([{ ...row, alias: "b" }])).toMatch(/A-Z, 0-9/);
    expect(talksToProblem([row, row])).toMatch(/BANK is used twice/);
    expect(talksToProblem([row, { ...row, env: "deploy" }])).toBe("");
    expect(linkRequests([row, { ...row, alias: "LEDGER", testUsers: false, env: "deploy" }])).toEqual([
      { provider_app_key: "crun_bank", alias: "BANK", env: "preview", credentials: "test_users" },
      { provider_app_key: "crun_bank", alias: "LEDGER", env: "deploy", credentials: "none" }]);
  });
});

describe("Talks to in the composer", () => {
  it("adding to an app: offers the OTHER apps, links them before the change starts", async () => {
    await mount({ mode: "add", appKey: "crun_att" });
    const field = $('[data-testid="np-talks-to"]')!;
    expect(field.textContent).toContain("Talks to");
    await click($('[data-testid="np-talk-add"]'));
    const options = [...host!.querySelectorAll('[data-testid="np-talk-app-0"] option')].map((o) => o.getAttribute("value"));
    expect(options).toEqual(["", "crun_bank"]);            // never the app itself
    await choose($('[data-testid="np-talk-app-0"]'), "crun_bank");
    expect(($('[data-testid="np-talk-alias-0"]') as HTMLInputElement).value).toBe("BANKING");
    await type($('[data-testid="np-talk-alias-0"]'), "bank");
    expect(($('[data-testid="np-talk-alias-0"]') as HTMLInputElement).value).toBe("BANK");
    await type($('[data-testid="np-gain"]'), "Try to read another customer's balance.");
    await click($('[data-testid="dw-new-start"]'));
    expect(linked).toEqual([{ project_id: "p1", app_key: "crun_att", provider_app_key: "crun_bank", alias: "BANK",
                              env: "preview", test_users: "on" }]);
    expect(posts).toHaveLength(1);                         // and then the change starts
    expect(posts[0].input).not.toHaveProperty("app_links");
    expect($('[data-testid="np-talk-0"]')).toBeNull();     // made: it leaves the form
  });

  it("a refused link says which and why, and starts nothing", async () => {
    refuse = "LEDGER";
    await mount({ mode: "add", appKey: "crun_att" });
    await click($('[data-testid="np-talk-add"]'));
    await choose($('[data-testid="np-talk-app-0"]'), "crun_bank");
    await click($('[data-testid="np-talk-add"]'));
    await choose($('[data-testid="np-talk-app-1"]'), "crun_bank");
    await type($('[data-testid="np-talk-alias-1"]'), "LEDGER");
    await choose($('[data-testid="np-talk-env-1"]'), "deploy");
    await type($('[data-testid="np-gain"]'), "Try to read another customer's balance.");
    await click($('[data-testid="dw-new-start"]'));
    expect(linked.map((l) => l.alias)).toEqual(["BANKING"]);
    expect(posts).toHaveLength(0);
    const error = $('[data-testid="dw-new-error"]')!.textContent!;
    expect(error).toContain("LEDGER was not linked: this app already has an active link with that alias");
    expect(error).toContain("Nothing was started; BANKING is linked");
    // The one made leaves the form; the refused one stays to fix.
    expect(($('[data-testid="np-talk-alias-0"]') as HTMLInputElement).value).toBe("LEDGER");
    expect($('[data-testid="np-talk-1"]')).toBeNull();
  });

  it("a new app: the links travel with its start, for its first build to make", async () => {
    await mount({ mode: "new" });
    await type($('[data-testid="dw-new-directive"]'), "A tester that tries to break into the banking app's API.");
    await click($('[data-testid="np-talk-add"]'));
    const options = [...host!.querySelectorAll('[data-testid="np-talk-app-0"] option')].map((o) => o.getAttribute("value"));
    expect(options).toEqual(["", "crun_bank", "crun_att"]);
    await choose($('[data-testid="np-talk-app-0"]'), "crun_bank");
    await click($('[data-testid="np-talk-users-0"]'));     // no test users for this one
    await click($('[data-testid="dw-new-start"]'));
    expect(linked).toEqual([]);                            // nothing to link to yet
    expect(posts).toHaveLength(1);
    expect(posts[0].input!.app_links).toEqual([
      { provider_app_key: "crun_bank", alias: "BANKING", env: "preview", credentials: "none" }]);
  });

  it("a half-filled row stops Start and says so", async () => {
    await mount({ mode: "new" });
    await type($('[data-testid="dw-new-directive"]'), "A tester that tries to break into the banking app's API.");
    await click($('[data-testid="np-talk-add"]'));
    expect($('[data-testid="np-blocked"]')!.textContent).toContain("Talks to: pick the app");
    expect($('[data-testid="dw-new-start"]')!.hasAttribute("disabled")).toBe(true);
  });

  it("without app_link:manage the field is greyed with the reason, and a press adds nothing", async () => {
    await mount({ mode: "add", appKey: "crun_att" }, true);
    const add = $('[data-testid="np-talk-add"]')!;
    expect(add.getAttribute("aria-disabled")).toBe("true");
    expect(add.getAttribute("title")).toBe(needsWords("app_link:manage"));
    await click(add);
    expect($('[data-testid="np-talk-0"]')).toBeNull();
    // The rest of the composer still works for a person who may start work.
    await type($('[data-testid="np-gain"]'), "Show the balance on the home page.");
    await click($('[data-testid="dw-new-start"]'));
    expect(posts).toHaveLength(1);
    expect(linked).toEqual([]);
  });
});
