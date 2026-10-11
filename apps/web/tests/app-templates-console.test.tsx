// @vitest-environment happy-dom
/**
 * THE TEMPLATE CONSOLE (APP_TEMPLATES_AND_SECURITY_LOOP.md §2.8; tester P0-5), against a stubbed
 * `/v1/templates*` and `/v1/apps/{key}/*`:
 *   - the gallery: approved cards only, stack chips greyed when the toolchain is off (the tooltip
 *     says how to turn it on), Insecure by design in amber, "Tested ✓";
 *   - a card's side sheet: what it adds, secrets (Test mode / Needs a key, a write-only field),
 *     Talks to (the hosts, ticked to start), Start = POST apply; the consent sheet on a 409
 *     consent_required, then the same apply once more; secrets_needed stores the typed test keys
 *     under the app's key (build + preview) and applies again; a value is never shown back;
 *   - App ▸ Setup: applied templates, secrets per environment, the guide with copy buttons;
 *   - Kit ▸ Templates: the list, import (reasons in words, no spec references), the review
 *     sheet, Approve greyed for the importer (D3) and for an Operator;
 *   - ROLE GATING: Owner / Operator / Viewer each see what they may do, and greyed with the
 *     reason what they may not.
 */
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { IdentityProvider } from "../components/identity/identity-provider";
import type { MePermissionsResponse, MeResponse } from "../lib/identity/contract";
import { CONSENT_EVENT, type ConsentRequest } from "../lib/identity/consent";
import { ROLE_PERMISSIONS } from "../lib/identity/permissions.gen";
import { needsWords } from "../lib/identity/words";
import { TemplateGallery, galleryCards, stackTip } from "../components/v2/templates/gallery";
import { TemplateSheet, applyBlocker } from "../components/v2/templates/template-sheet";
import { AppSetup, copyLabel, envCell } from "../components/v2/templates/setup-sheet";
import { KitTemplates, approveBlocker, importReasons, kitRows } from "../components/v2/templates/kit-templates";
import { githubAsMeNotice, parseCard, parseGithubAsMe, plainSentence, secretWord, testedWord, type TemplateCard } from "../lib/templates";
import { ConnectorsShelf, connectorRunHref } from "../app/command-center/connectors-shelf";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const STRIPE = {
  id: "tpl_stripe", template_key: "stripe-checkout", version: "1.0.0", source: "builtin", status: "active",
  name: "Stripe payments", summary: "Checkout + webhooks, test mode by default", stacks: ["java", "node", "python"],
  labels: [], adds: ["routes", "webhook"], egress: ["api.stripe.com"],
  secrets: [{ name: "STRIPE_SECRET_KEY", purpose: "Server-side API calls", test_default: "none" },
            { name: "STRIPE_WEBHOOK_SECRET", purpose: "Webhook signatures", test_default: "generated" }],
  tested: ["node", "python"], digest: "sha256:x", git_url: null, commit_sha: null, imported_by: null, approved_by: null,
};
const BANK = { ...STRIPE, id: "tpl_bank", template_key: "banking-starter-insecure", name: "Banking starter (insecure)",
  summary: "A ledger with deliberate flaws", stacks: ["java"], labels: ["insecure_by_design"], egress: [], secrets: [], tested: [] };
const PENDING = { ...STRIPE, id: "tpl_health", template_key: "health-endpoint", name: "Health endpoint", source: "git",
  status: "pending_review", summary: "GET /health", stacks: ["node", "python"], egress: [], secrets: [], tested: [],
  git_url: "https://git.example/acme/health", commit_sha: "abcdef1234567890", imported_by: "usr_me" };
const DETAIL = { ...STRIPE, manifest: { routes: ["POST /api/checkout", "POST /webhooks/stripe"], stacks: { node: { frameworks: ["express"] } } },
  brief: "Add Stripe checkout.", guide: "Steps", security_note: "Webhooks are verified with the signing secret.",
  files: [{ path: "boss-template.yaml", size: 900 }], validation: { ok: true, reasons: [], file_count: 12, total_bytes: 9000 },
  trial: { status: "passed", at: "2026-10-09T10:00:00Z", stacks: { node: { ok: true, tests: 6, passed: 6 } } }, versions: [] };

type Call = { method: string; path: string; body: Record<string, unknown> | undefined };
let calls: Call[] = [];
let applyAnswers: Array<[number, unknown]> = [];
/** GET /v1/templates/workflows: null = an API without it (404), so no picker. */
let buildWorkflows: unknown[] | null = null;
/** GET /v1/templates/workflows' github_as_me; undefined = an API that does not say. */
let githubAsMe: Record<string, unknown> | undefined;
let toolchains: unknown[] = [{ name: "python", installed: true }, { name: "node", installed: true }, { name: "java", installed: false }];

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

beforeEach(() => {
  calls = [];
  buildWorkflows = null;
  githubAsMe = undefined;
  applyAnswers = [[201, { workflow_run_id: "wfr_9", application_id: "tap_1", app_key: "shop" }]];
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = (init?.method ?? "GET").toUpperCase();
    const path = url.replace(/^https?:\/\/[^/]+/, "");
    calls.push({ method, path, body: init?.body ? JSON.parse(String(init.body)) : undefined });
    if (path.startsWith("/v1/identity/me/delegations")) return json({ id: "grant_1" }, 201);
    if (path === "/v1/templates?status=hidden" || path === "/v1/templates?status=rejected") return json({ templates: [] });
    if (path === "/v1/templates") return json({ templates: [STRIPE, BANK, PENDING], toolchains });
    if (path === "/v1/templates?status=active") return json({ templates: [STRIPE, BANK], toolchains });
    if (path === "/v1/templates/import") return json({ error: "invalid", report: { ok: false, reasons: ["insecure_by_design needs template:approve and the acknowledgement (D3)", "LICENSE is missing"] } }, 422);
    if (path.startsWith("/v1/templates/workflows?project_id=p1") && buildWorkflows) {
      return json({ project_id: "p1", default: "fast_fix", workflows: buildWorkflows,
                    ...(githubAsMe ? { github_as_me: githubAsMe } : {}) });
    }
    if (path.startsWith("/v1/templates/tpl_stripe/apply")) {
      const [status, body] = applyAnswers.shift() ?? [500, { error: "x" }];
      return json(body, status);
    }
    if (path.startsWith("/v1/templates/tpl_health")) return json({ ...PENDING, manifest: {}, validation: { ok: true, reasons: [] }, files: [], versions: [] });
    if (path.startsWith("/v1/templates/tpl_")) return json(DETAIL);
    if (path.startsWith("/v1/apps/shop/templates")) return json({ app_key: "shop", templates: [
      { id: "tap_1", template_id: "tpl_stripe", template_key: "stripe-checkout", name: "Stripe payments", version: "1.0.0", stack: "node", status: "pr_open" }] });
    if (path.startsWith("/v1/apps/shop/secrets/")) return json({ name: "STRIPE_SECRET_KEY", set: true });
    if (path.startsWith("/v1/apps/shop/secrets")) return json({ app_key: "shop",
      secrets: [{ name: "STRIPE_WEBHOOK_SECRET", env: "build", mode: "generated", shape: "unknown" },
                { name: "STRIPE_WEBHOOK_SECRET", env: "preview", mode: "generated", shape: "unknown" }],
      declared: [{ name: "STRIPE_SECRET_KEY", purpose: "Server-side API calls", test_default: "none", bound: false },
                 { name: "STRIPE_WEBHOOK_SECRET", purpose: "Webhook signatures", test_default: "generated", bound: true }] });
    if (path.startsWith("/v1/apps/shop/setup-guide")) return json({ app_key: "shop", guides: [
      { template_key: "stripe-checkout", name: "Stripe payments", version: "1.0.0", status: "pr_open", guide: "## Webhooks\n\nPoint Stripe at the webhook URL.",
        copy: { preview_url: "https://boss.example/v1/previews/prev_1/web", "webhook_url:stripe": "https://boss.example/v1/previews/prev_1/web/webhooks/stripe", public_url: "(BOSS_PUBLIC_URL is not set)" } }] });
    return json({ error: "not_stubbed", path }, 404);
  }));
});

let root: Root | null = null;
let host: HTMLElement | null = null;
afterEach(() => { act(() => root?.unmount()); host?.remove(); root = null; document.body.innerHTML = ""; vi.unstubAllGlobals(); });

function perms(role: keyof typeof ROLE_PERMISSIONS): MePermissionsResponse {
  return { epoch: 1, scope: "org", permissions: [...ROLE_PERMISSIONS[role]], step_up_fresh_until: null, catalogue_fingerprint: "x" };
}
const ME = {
  principal_id: "usr_me", kind: "user", display_name: "Pat", email: "p@acme.dev", org: { id: "org_1", name: "Acme" }, status: "active",
  access_roles: ["owner"], sign_in: { password: true, totp: false, passkeys: 0, recovery_codes_remaining: 0, sso_links: [] },
  mfa_required: false, mfa_at: null, session_id: "s", session_expires_at: null, csrf_token: "c", epoch: 1, network_bound: false, banners: [], avatar_seed: null,
} as MeResponse;

async function mount(node: React.ReactElement, role: keyof typeof ROLE_PERMISSIONS) {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => root!.render(<IdentityProvider me={ME} org={perms(role)}>{node}</IdentityProvider>));
  await settle();
}
async function settle() { for (let i = 0; i < 8; i += 1) await act(async () => { await new Promise((r) => setTimeout(r, 0)); }); }
const $ = (sel: string) => document.querySelector(sel) as HTMLElement;
const $$ = (sel: string) => [...document.querySelectorAll(sel)] as HTMLElement[];
const writes = () => calls.filter((c) => c.method !== "GET");
async function press(sel: string) { await act(async () => $(sel).click()); await settle(); }
async function type(sel: string, value: string) {
  const el = $(sel) as HTMLInputElement;
  const proto = el.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
  await act(async () => {
    Object.getOwnPropertyDescriptor(proto, "value")!.set!.call(el, value);
    el.dispatchEvent(new Event("input", { bubbles: true }));
  });
  await settle();
}
async function tick(sel: string) { await act(async () => ($(sel) as HTMLInputElement).click()); await settle(); }

const card = (raw: unknown): TemplateCard => parseCard(raw);

describe("the gallery", () => {
  it("shows approved templates only, with stacks, labels and the trial", async () => {
    let picked: TemplateCard | null = null;
    await mount(<TemplateGallery projectId="p1" onPick={(t) => { picked = t; }} />, "operator");
    expect($('[data-testid="tpl-card-stripe-checkout"]')).toBeTruthy();
    expect($('[data-testid="tpl-card-health-endpoint"]')).toBeNull();         // pending review: not in the gallery
    const bank = $('[data-testid="tpl-card-banking-starter-insecure"]');
    const amber = [...bank.querySelectorAll(".tpl-chip")].find((c) => c.textContent === "Insecure by design")!;
    expect(amber.getAttribute("data-tone")).toBe("amber");
    const java = [...bank.querySelectorAll(".tpl-chip")].find((c) => c.textContent?.startsWith("Java"))!;
    expect(java.getAttribute("data-off")).toBe("true");                       // the java toolchain is off
    expect(java.getAttribute("title")).toContain("boss toolchains enable java");
    expect($('[data-testid="tpl-card-stripe-checkout"]').textContent).toContain("Tested ✓ Node, Python");
    await press('[data-testid="tpl-card-stripe-checkout"]');
    expect(picked!.key).toBe("stripe-checkout");
  });

  it("filters by stack; the add-to-an-app gallery shows only the app's stack", async () => {
    await mount(<TemplateGallery projectId="p1" stack="node" onPick={() => {}} />, "operator");
    expect($('[data-testid="tpl-card-stripe-checkout"]')).toBeTruthy();
    expect($('[data-testid="tpl-card-banking-starter-insecure"]')).toBeNull();
    expect($('[data-testid="tpl-gallery"]').textContent).toContain("Node only");
    expect(galleryCards([card(STRIPE), card(BANK)], "java").map((t) => t.key)).toEqual(["stripe-checkout", "banking-starter-insecure"]);
    expect(stackTip("java", null)).toBe("Java reference and tests");
  });
});

describe("a template's sheet", () => {
  async function openNew(role: keyof typeof ROLE_PERMISSIONS, onStarted = vi.fn()) {
    await mount(<TemplateSheet projectId="p1" card={card(STRIPE)} target={{ kind: "new" }} onStarted={onStarted} onClose={() => {}} />, role);
    return onStarted;
  }

  it("says what it adds, needs and talks to; Start waits for a name, the hosts and a test key", async () => {
    await openNew("operator");
    const sheet = $('[data-testid="tpl-sheet"]');
    expect(sheet.classList.contains("tpl-sheet")).toBe(true);
    expect(sheet.textContent).toContain("POST /api/checkout");
    expect($('[data-testid="tpl-secret-STRIPE_SECRET_KEY"]').textContent).toContain("Needs a key");
    expect($('[data-testid="tpl-secret-STRIPE_WEBHOOK_SECRET"]').textContent).toContain("Test mode");
    expect($('[data-testid="tpl-egress"]').textContent).toContain("api.stripe.com");
    expect($('[data-testid="tpl-security-note"]').hasAttribute("open")).toBe(false);   // folded
    expect(($('[data-testid="tpl-stack-java"]')).getAttribute("aria-disabled")).toBe("true");
    expect($('[data-testid="tpl-blocked"]').textContent).toBe("Name the new app.");
    await type('[data-testid="tpl-app-name"]', "Shop");
    expect($('[data-testid="tpl-blocked"]').textContent).toBe("Allow the hosts it talks to.");
    await tick('[data-testid="tpl-egress-ok"]');
    expect($('[data-testid="tpl-blocked"]').textContent).toBe("Paste a test key for STRIPE_SECRET_KEY.");
    expect(($('[data-testid="tpl-start"]') as HTMLButtonElement).disabled).toBe(true);
    await press('[data-testid="tpl-start"]');
    expect(writes()).toEqual([]);
  });

  it("Start applies with the hosts accepted and the new app named", async () => {
    const started = await openNew("operator");
    applyAnswers = [[409, { error: "secrets_needed", message: "set a test value for: STRIPE_SECRET_KEY", secrets: ["STRIPE_SECRET_KEY"], app_key: "shop" }],
                    [201, { workflow_run_id: "wfr_9", application_id: "tap_1", app_key: "shop" }]];
    await type('[data-testid="tpl-app-name"]', "Shop");
    await tick('[data-testid="tpl-egress-ok"]');
    await type('[data-testid="tpl-secret-input-STRIPE_SECRET_KEY"]', "sk_test_abc123");
    await press('[data-testid="tpl-start"]');
    const w = writes();
    expect(w[0]).toMatchObject({ method: "POST", path: "/v1/templates/tpl_stripe/apply",
      body: { project_id: "p1", stack: "node", new_app: { name: "Shop" }, accept_egress: ["api.stripe.com"] } });
    // secrets_needed: the typed key is stored under the app's key ONCE, for build and preview
    // together (tester N8: it was two PUTs), then the same apply.
    expect(w.slice(1, 2).map((c) => [c.method, c.path, c.body?.envs, c.body?.mode])).toEqual([
      ["PUT", "/v1/apps/shop/secrets/STRIPE_SECRET_KEY", ["build", "preview"], "test"]]);
    expect(w[2].path).toBe("/v1/templates/tpl_stripe/apply");
    expect(started).toHaveBeenCalledWith("wfr_9");
  });

  it("an Operator's apply that needs consent opens the consent sheet, then sends the same apply", async () => {
    const started = await openNew("operator");
    applyAnswers = [[409, { detail: { error: "consent_required", task: "Shop: add Stripe payments", task_run_id: "pending:x1",
                                       agents: [{ id: "agt_1", name: "Helm", role_key: "software_engineer" }],
                                       permissions: ["code_task:create"], projects: ["p1"], durations: [{ kind: "task", label: "For this build only" }] } }],
                    [201, { workflow_run_id: "wfr_10", application_id: "tap_2", app_key: "shop" }]];
    const asked: ConsentRequest[] = [];
    const listen = (e: Event) => { const d = (e as CustomEvent<ConsentRequest>).detail; asked.push(d); d.onDecided?.("grant_1"); };
    window.addEventListener(CONSENT_EVENT, listen);
    await type('[data-testid="tpl-app-name"]', "Shop");
    await tick('[data-testid="tpl-egress-ok"]');
    await type('[data-testid="tpl-secret-input-STRIPE_SECRET_KEY"]', "sk_test_abc123");
    await press('[data-testid="tpl-start"]');
    window.removeEventListener(CONSENT_EVENT, listen);
    expect(asked).toHaveLength(1);
    expect(asked[0].task).toBe("Shop: add Stripe payments");
    expect(writes().filter((c) => c.path.endsWith("/apply"))).toHaveLength(2);
    expect(started).toHaveBeenCalledWith("wfr_10");
    expect(document.body.textContent).not.toContain("consent_required");
  });

  it("Workflow: the project's build workflows, default first; a chosen one is sent as workflow_key", async () => {
    buildWorkflows = [{ workflow_key: "fast_fix", name: "Fast Fix / Hotfix", default: true },
                      { workflow_key: "helpdesk_delivery_d1", name: "HelpDesk Delivery (D1)", default: false }];
    const started = await openNew("operator");
    const pick = $('[data-testid="tpl-workflow"]') as HTMLSelectElement;
    expect([...pick.options].map((o) => o.textContent)).toEqual(["Fast Fix / Hotfix (default)", "HelpDesk Delivery (D1)"]);
    expect(pick.value).toBe("fast_fix");
    expect(pick.closest("label")!.getAttribute("title")).toContain("build step");
    await type('[data-testid="tpl-app-name"]', "HelpDesk");
    await tick('[data-testid="tpl-egress-ok"]');
    await type('[data-testid="tpl-secret-input-STRIPE_SECRET_KEY"]', "sk_test_abc123");
    await act(async () => {
      pick.value = "helpdesk_delivery_d1";
      pick.dispatchEvent(new Event("change", { bubbles: true }));
    });
    await settle();
    await press('[data-testid="tpl-start"]');
    const apply = writes().find((c) => c.path.endsWith("/apply"))!;
    expect(apply.body).toMatchObject({ new_app: { name: "HelpDesk" }, workflow_key: "helpdesk_delivery_d1" });
    expect(started).toHaveBeenCalledWith("wfr_9");
  });

  it("Workflow left on the default sends no workflow_key; with no list there is no picker", async () => {
    buildWorkflows = [{ workflow_key: "fast_fix", name: "Fast Fix / Hotfix", default: true },
                      { workflow_key: "major_feature", name: "Major Feature", default: false }];
    await mount(<TemplateSheet projectId="p1" card={card(STRIPE)} target={{ kind: "add", runId: "crun_1", appKey: "shop", name: "Shop", stack: "node" }}
                               onStarted={() => {}} onClose={() => {}} />, "owner");
    expect($('[data-testid="tpl-workflow"]')).toBeTruthy();                     // an existing app gets it too
    await tick('[data-testid="tpl-egress-ok"]');
    await press('[data-testid="tpl-start"]');
    const apply = writes().find((c) => c.path.endsWith("/apply"))!;
    expect(apply.body).not.toHaveProperty("workflow_key");
    act(() => root?.unmount()); host?.remove();
    buildWorkflows = null;
    await openNew("operator");
    expect($('[data-testid="tpl-workflow"]')).toBeNull();
  });

  it("a refusal is the API's sentence, without spec references", async () => {
    await mount(<TemplateSheet projectId="p1" card={card(STRIPE)} target={{ kind: "add", runId: "crun_1", appKey: "shop", name: "Shop", stack: "node" }}
                               onStarted={() => {}} onClose={() => {}} />, "owner");
    applyAnswers = [[409, { error: "requires_unmet", message: "apply these templates to the app first: login-oauth (§2.3)" }]];
    await tick('[data-testid="tpl-egress-ok"]');
    await press('[data-testid="tpl-start"]');
    expect($('[data-testid="tpl-error"]').textContent).toBe("Apply these templates to the app first: login-oauth");
    expect(writes()[0].body).toMatchObject({ extends_run_id: "crun_1" });
  });

  it("a Viewer sees Start greyed with the reason, and nothing is sent", async () => {
    await openNew("viewer");
    const start = $('[data-testid="tpl-start"]');
    expect(start.getAttribute("aria-disabled")).toBe("true");
    expect(start.getAttribute("title")).toBe(needsWords("workflow_run:start"));
    await press('[data-testid="tpl-start"]');
    expect(writes()).toEqual([]);
  });

  it("the blocker rules, in words", () => {
    const base = { target: { kind: "new" as const }, name: "x", stack: "node", egress: [], egressOk: false, needs: [], typed: {}, stackOff: false };
    expect(applyBlocker(base)).toBe("");
    expect(applyBlocker({ ...base, stackOff: true })).toBe("Node is off on this install.");
    expect(applyBlocker({ ...base, target: { kind: "add", runId: "r", name: "A" }, name: "" })).toBe("");
    // Into an app whose stack is unknown, the person picks it: never the first in the list.
    expect(applyBlocker({ ...base, target: { kind: "add", runId: "r", name: "A" }, stack: "" })).toBe("Choose the app's stack.");
  });
});

describe("App ▸ Setup", () => {
  it("lists applied templates, secrets per environment, and the guide with copy buttons", async () => {
    await mount(<AppSetup projectId="p1" appKey="shop" />, "owner");
    expect($('[data-testid="tpl-applied-stripe-checkout"]').textContent).toContain("PR open");
    expect($('[data-testid="tpl-env-STRIPE_SECRET_KEY-build"]').textContent).toBe("Build · Needs a key");
    expect($('[data-testid="tpl-env-STRIPE_WEBHOOK_SECRET-preview"]').textContent).toBe("Preview · Test mode");
    expect($('[data-testid="tpl-env-STRIPE_SECRET_KEY-deploy"]').textContent).toBe("Deploy · Not set");
    expect($('[data-testid="tpl-copy-stripe-checkout-webhook_url:stripe"]').textContent).toContain("/webhooks/stripe");
    expect($('[data-testid="tpl-copy-stripe-checkout-webhook_url:stripe"] button').getAttribute("aria-label")).toBe("Copy Webhook URL (stripe)");
    expect($('[data-testid="tpl-copy-stripe-checkout-public_url"]')).toBeNull();     // unfilled: said, not copied
    expect($('[data-testid="tpl-guide-stripe-checkout"]').textContent).toContain("Point Stripe at the webhook URL.");
  });

  it("an Owner sets a test key for build and preview; the field clears and no value is shown", async () => {
    await mount(<AppSetup projectId="p1" appKey="shop" />, "owner");
    await press('[data-testid="tpl-set-test-STRIPE_SECRET_KEY"]');
    await type('[data-testid="tpl-secret-value-STRIPE_SECRET_KEY"]', "sk_test_zzz999");
    await press('[data-testid="tpl-secret-save-STRIPE_SECRET_KEY"]');
    expect(writes().map((c) => [c.method, c.body?.envs, c.body?.mode])).toEqual([["PUT", ["build", "preview"], "test"]]);
    expect(document.body.innerHTML).not.toContain("sk_test_zzz999");
    expect($('[data-testid="tpl-setup-note"]').textContent).toContain("stored for builds and previews");
  });

  it("an Operator sets test keys but a live key is greyed; a Viewer sees why the secrets are hidden", async () => {
    await mount(<AppSetup projectId="p1" appKey="shop" />, "operator");
    expect($('[data-testid="tpl-set-test-STRIPE_SECRET_KEY"]').getAttribute("data-can")).toBe("yes");
    const live = $('[data-testid="tpl-set-live-STRIPE_SECRET_KEY"]');
    expect(live.getAttribute("aria-disabled")).toBe("true");
    expect(live.getAttribute("title")).toBe(needsWords("app_secret:live"));
    act(() => root?.unmount()); host?.remove(); document.body.innerHTML = "";
    calls = [];
    await mount(<AppSetup projectId="p1" appKey="shop" />, "viewer");
    expect($('[data-testid="tpl-setup-secrets-why"]').textContent).toBe(needsWords("app_secret:read_metadata"));
    expect(calls.some((c) => c.path.startsWith("/v1/apps/shop/secrets"))).toBe(false);
    expect($('[data-testid="tpl-guide-stripe-checkout"]')).toBeTruthy();                  // project:read still shows the guide
  });

  it("names its copy values and environment cells in words", () => {
    expect(copyLabel("redirect_uri")).toBe("Redirect URI");
    expect(copyLabel("deploy_url")).toBe("Deployed URL");
    expect(copyLabel("callback_url:github")).toBe("Callback URL for GitHub");
    expect(copyLabel("callback_url:boss")).toBe("Callback URL for BOSS");
    expect(envCell({ env: "deploy", testDefault: "none", binding: { name: "K", env: "deploy", mode: "live", shape: "x", setAt: "", lastUsedAt: "", liveApproved: false } }).word)
      .toBe("Live · awaiting approval");
    expect(secretWord({ testDefault: "stub" }).word).toBe("Test mode");
  });
});

describe("Kit ▸ Templates", () => {
  it("lists every template with its status, pending first", async () => {
    await mount(<KitTemplates projectId="p1" />, "owner");
    const rows = $$('[data-testid^="tpl-kit-row-"]').map((r) => r.getAttribute("data-testid"));
    expect(rows[0]).toBe("tpl-kit-row-health-endpoint");
    expect($('[data-testid="tpl-kit-row-health-endpoint"]').textContent).toContain("Needs review");
    expect($('[data-testid="tpl-kit-row-stripe-checkout"]').textContent).toContain("Built-in");
  });

  it("import says why it was refused, in words, without spec references", async () => {
    await mount(<KitTemplates projectId="p1" />, "owner");
    await press('[data-testid="tpl-import-open"]');
    await type('[data-testid="tpl-import-url"]', "https://git.example/acme/insecure");
    await press('[data-testid="tpl-import-go"]');
    expect(writes()[0]).toMatchObject({ path: "/v1/templates/import", body: { git_url: "https://git.example/acme/insecure" } });
    expect($('[data-testid="tpl-import-error"]').textContent).toBe("Refused: it did not pass the checks.");
    expect($('[data-testid="tpl-import-reasons"]').textContent).not.toContain("(D3)");
    expect($('[data-testid="tpl-import-reasons"]').textContent).toContain("LICENSE is missing");
  });

  it("the importer cannot approve their own import: greyed, with the reason", async () => {
    await mount(<KitTemplates projectId="p1" />, "owner");
    await press('[data-testid="tpl-kit-open-health-endpoint"]');
    expect($('[data-testid="tpl-seat"]').textContent).toContain("You imported it");
    expect(($('[data-testid="tpl-approve"]') as HTMLButtonElement).disabled).toBe(true);
    expect(writes()).toEqual([]);
  });

  it("an Operator imports but cannot approve; a Viewer cannot import", async () => {
    await mount(<KitTemplates projectId="p1" />, "operator");
    expect($('[data-testid="tpl-import-open"]').getAttribute("data-can")).toBe("yes");
    await press('[data-testid="tpl-kit-open-stripe-checkout"]');
    const trial = $('[data-testid="tpl-trial"]');
    expect(trial.getAttribute("aria-disabled")).toBe("true");
    expect(trial.getAttribute("title")).toBe(needsWords("template:approve"));
    act(() => root?.unmount()); host?.remove(); document.body.innerHTML = "";
    await mount(<KitTemplates projectId="p1" />, "viewer");
    const imp = $('[data-testid="tpl-import-open"]');
    expect(imp.getAttribute("aria-disabled")).toBe("true");
    expect(imp.getAttribute("title")).toBe(needsWords("template:import"));
  });

  it("its rules", () => {
    expect(kitRows([[card(STRIPE), card(PENDING as never)], [card(PENDING as never)]]).map((t) => t.key)).toEqual(["health-endpoint", "stripe-checkout"]);
    expect(approveBlocker(card(PENDING as never), "usr_other")).toBe("");
    expect(importReasons({ report: { reasons: ["needs approve (D3)"] } })).toEqual(["needs approve"]);
    expect(plainSentence("the importer cannot approve it (D3)")).toBe("The importer cannot approve it");
    expect(testedWord([])).toBe("");
  });
});

describe("GitHub as me, before Start", () => {
  const sheet = () => <TemplateSheet projectId="p1" card={card(STRIPE)} target={{ kind: "new" }} onStarted={() => {}} onClose={() => {}} />;
  it("says up front, with a link, when the build needs the person's own GitHub and it is not linked", async () => {
    buildWorkflows = [{ workflow_key: "fast_fix", name: "Fast Fix / Hotfix", default: true }];
    githubAsMe = { required: true, linked: false, href: "/account/agents" };
    await mount(sheet(), "operator");
    const note = $('[data-testid="tpl-github-as-me"]');
    expect(note.textContent).toContain("isn't linked");
    expect(($('[data-testid="tpl-github-link"]') as HTMLAnchorElement).getAttribute("href")).toBe("/account/agents");
    expect(writes()).toEqual([]);                                   // said before anything is started
  });
  it("says nothing when it is linked, not required, or the API does not say", async () => {
    buildWorkflows = [{ workflow_key: "fast_fix", name: "Fast Fix / Hotfix", default: true }];
    githubAsMe = { required: true, linked: true, href: "/account/agents" };
    await mount(sheet(), "operator");
    expect($('[data-testid="tpl-github-as-me"]')).toBeNull();
    expect(githubAsMeNotice(parseGithubAsMe({ github_as_me: { required: false, linked: false } }))).toBe("");
    expect(githubAsMeNotice(parseGithubAsMe({}))).toBe("");
    expect(githubAsMeNotice(null)).toBe("");
  });
});

describe("Command Center › Browse templates", () => {
  it("shows the app connectors (code templates) next to the briefs, and a card opens its sheet", async () => {
    await mount(<ConnectorsShelf projectId="p1" />, "operator");
    const shelf = $('[data-testid="cc-connectors"]');
    expect(shelf.textContent).toContain("App connectors");
    expect($('[data-testid="tpl-card-stripe-checkout"]')).toBeTruthy();
    await press('[data-testid="tpl-card-stripe-checkout"]');
    expect($('[data-testid="tpl-sheet"]')).toBeTruthy();
    expect(connectorRunHref("p1", "wfr_9")).toContain("wfr=wfr_9");
  });
  it("groups both kinds under Browse templates", async () => {
    const { readFileSync } = await import("node:fs");
    const src = readFileSync(`${process.cwd()}/app/command-center-ui.tsx`, "utf8");
    const block = src.slice(src.indexOf('data-testid="cc-template-details"'));
    expect(block.indexOf("<TemplatesShelf />")).toBeGreaterThan(0);
    expect(block.indexOf("<ConnectorsShelf")).toBeGreaterThan(block.indexOf("<TemplatesShelf />"));
    expect(block).toContain("templatesOpen ? <ConnectorsShelf");
    expect(block.indexOf("<ConnectorsShelf")).toBeLessThan(block.indexOf("</details>"));
  });
});
