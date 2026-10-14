// @vitest-environment happy-dom
/**
 * BOSSAPP-526, the console half: several connectors picked in New app / Add a feature go into ONE
 * build through the compose sheet (POST /v1/templates/compose); one picked keeps its own sheet
 * and POST /v1/templates/{id}/apply. Against a stubbed API:
 *   - the picks the server would refuse are greyed with the reason (already in the app, the
 *     limit of six, no stack shared), and route clashes the manifests show are said before Start;
 *   - the sheet merges the secrets (a key asked for once, stored under the connector declaring
 *     it) and the hosts (accepted as a whole), and Start sends every connector in the order picked;
 *   - a 409 connector_conflict lists EVERY conflict, readably and focused, and nothing starts;
 *   - a 409 secrets_needed stores the typed keys, then composes again.
 */
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { IdentityProvider } from "../components/identity/identity-provider";
import type { MePermissionsResponse, MeResponse } from "../lib/identity/contract";
import { ROLE_PERMISSIONS } from "../lib/identity/permissions.gen";
import {
  MAX_CONNECTORS, connectorBlock, effectiveConnectors, sharedStacks,
} from "../components/v2/templates/catalogue";
import {
  ComposeSheet, composeBlocker, conflictLine, mergedEgress, mergedSecrets, routeClashes, routeShape, unmetRequires,
} from "../components/v2/templates/compose-sheet";
import { parseCard, type TemplateCard } from "../lib/templates";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const base = { version: "1.0.0", source: "builtin", status: "active", labels: [], adds: ["routes"], tested: ["node"],
               digest: "sha256:x", kind: "connector" };
const LOGIN = { ...base, id: "tpl_login", template_key: "login-oauth", name: "Login", summary: "Google or GitHub sign-in",
  stacks: ["node", "python"], egress: ["accounts.google.com"],
  secrets: [{ name: "SESSION_SECRET", purpose: "Signs sessions", test_default: "generated" },
            { name: "OAUTH_CLIENT_SECRET", purpose: "The OAuth app", test_default: "none" }] };
const EMAIL = { ...base, id: "tpl_email", template_key: "transactional-email", name: "Email", summary: "Sends mail",
  stacks: ["node"], egress: ["api.resend.com", "accounts.google.com"],
  secrets: [{ name: "RESEND_API_KEY", purpose: "Sends mail", test_default: "stub" },
            { name: "SESSION_SECRET", purpose: "", test_default: "none" }] };
const STRIPE = { ...base, id: "tpl_stripe", template_key: "stripe-checkout", name: "Stripe", summary: "Card payments",
  stacks: ["node", "python", "java"], egress: ["api.stripe.com"], secrets: [] };
const PAYPAL = { ...base, id: "tpl_paypal", template_key: "paypal-checkout", name: "PayPal", summary: "PayPal payments",
  stacks: ["node"], egress: ["api-m.sandbox.paypal.com"], secrets: [] };
const JAVA_ONLY = { ...base, id: "tpl_j", template_key: "java-thing", name: "Java thing", summary: "J", stacks: ["java"],
  egress: [], secrets: [] };
const ROUTES: Record<string, string[]> = {
  tpl_login: ["GET /auth/login", "GET /auth/callback"], tpl_email: ["POST /api/email/send"],
  tpl_stripe: ["POST /api/checkout", "GET /api/orders/:id"], tpl_paypal: ["POST /api/paypal/order", "GET /api/orders/{order_id}"],
};
const RAW: Record<string, Record<string, unknown>> = { tpl_login: LOGIN, tpl_email: EMAIL, tpl_stripe: STRIPE, tpl_paypal: PAYPAL };
const card = (raw: unknown): TemplateCard => parseCard(raw);

type Call = { method: string; path: string; body: Record<string, unknown> | undefined };
let calls: Call[] = [];
let composeAnswers: Array<[number, unknown]> = [];
function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}
beforeEach(() => {
  calls = [];
  composeAnswers = [[201, { workflow_run_id: "wfr_c1", app_key: "shop", order: ["login-oauth", "transactional-email"],
                             egress: [], connectors: [] }]];
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const method = (init?.method ?? "GET").toUpperCase();
    const path = String(input).replace(/^https?:\/\/[^/]+/, "");
    calls.push({ method, path, body: init?.body ? JSON.parse(String(init.body)) : undefined });
    if (path === "/v1/templates/compose") {
      const [status, body] = composeAnswers.shift() ?? [500, { error: "x" }];
      return json(body, status);
    }
    if (path.startsWith("/v1/templates/workflows")) return json({ error: "not_found" }, 404);
    if (path.startsWith("/v1/templates?") || path === "/v1/templates") {
      return json({ templates: Object.values(RAW), toolchains: [{ name: "node", installed: true }, { name: "python", installed: true }] });
    }
    const id = /^\/v1\/templates\/(tpl_\w+)$/.exec(path)?.[1];
    if (id && RAW[id]) return json({ ...RAW[id], manifest: { routes: ROUTES[id] ?? [], requires: [] }, validation: { ok: true }, files: [], versions: [] });
    if (path.startsWith("/v1/apps/shop/secrets/")) return json({ set: true });
    if (path.startsWith("/v1/stacks")) return json({ supported: { languages: [], adapters: [] } });
    return json({ error: "not_stubbed", path }, 404);
  }));
});

let root: Root | null = null;
let host: HTMLElement | null = null;
afterEach(() => { act(() => root?.unmount()); host?.remove(); root = null; document.body.innerHTML = ""; vi.unstubAllGlobals(); });
const ME = {
  principal_id: "usr_me", kind: "user", display_name: "Pat", email: "p@acme.dev", org: { id: "org_1", name: "Acme" }, status: "active",
  access_roles: ["owner"], sign_in: { password: true, totp: false, passkeys: 0, recovery_codes_remaining: 0, sso_links: [] },
  mfa_required: false, mfa_at: null, session_id: "s", session_expires_at: null, csrf_token: "c", epoch: 1, network_bound: false, banners: [], avatar_seed: null,
} as MeResponse;
const perms = (role: keyof typeof ROLE_PERMISSIONS): MePermissionsResponse =>
  ({ epoch: 1, scope: "org", permissions: [...ROLE_PERMISSIONS[role]], step_up_fresh_until: null, catalogue_fingerprint: "x" });
async function settle() { for (let i = 0; i < 10; i += 1) await act(async () => { await new Promise((r) => setTimeout(r, 0)); }); }
async function mount(node: React.ReactElement, role: keyof typeof ROLE_PERMISSIONS = "owner") {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => root!.render(<IdentityProvider me={ME} org={perms(role)}>{node}</IdentityProvider>));
  await settle();
}
const $ = (sel: string) => document.querySelector(sel) as HTMLElement;
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

describe("what the picker knows before the server", () => {
  it("greys a pick the server would refuse, with the reason; a picked card can always be unpicked", () => {
    const [login, email, stripe] = [card(LOGIN), card(EMAIL), card(STRIPE)];
    expect(connectorBlock(stripe, [login, email], [])).toBe("");
    expect(connectorBlock(login, [login], ["login-oauth"], "Shop")).toBe("Already in Shop: added in an earlier build.");
    expect(connectorBlock(login, [login, email], [])).toBe("");                     // picked: unpick it
    expect(connectorBlock(card(JAVA_ONLY), [email], [])).toBe("No stack fits Java thing and Email together. Add it in a build of its own.");
    const six = Array.from({ length: MAX_CONNECTORS }, (_, i) => card({ ...STRIPE, id: `t${i}`, template_key: `k${i}` }));
    expect(connectorBlock(login, six, [])).toContain(`Up to ${MAX_CONNECTORS} connectors in one build`);
  });

  it("the build's connectors are the ones on offer and new to the app, in the order picked", () => {
    const all = [card(LOGIN), card(EMAIL), card(STRIPE)];
    const picked = [card(STRIPE), card(LOGIN), card(EMAIL)];
    expect(effectiveConnectors(picked, all, { inApp: ["login-oauth"], stack: "node" }).map((c) => c.key))
      .toEqual(["stripe-checkout", "transactional-email"]);
    expect(effectiveConnectors(picked, all, { inApp: [], stack: "python" }).map((c) => c.key))
      .toEqual(["stripe-checkout", "login-oauth"]);
    expect(sharedStacks([card(LOGIN), card(STRIPE)])).toEqual(["node", "python"]);
    expect(sharedStacks([card(EMAIL), card(JAVA_ONLY)])).toEqual([]);
  });

  it("reads routes the way the server does, and finds the clash Stripe and PayPal have", () => {
    expect(routeShape("GET /api/orders/{order_id}/")).toBe("GET /api/orders/:");
    expect(routeShape("get /api/orders/:id")).toBe("GET /api/orders/:");
    const clash = routeClashes([{ key: "stripe-checkout", routes: ROUTES.tpl_stripe }, { key: "paypal-checkout", routes: ROUTES.tpl_paypal }]);
    expect(clash).toEqual([{ kind: "route", what: "GET /api/orders/:id", connectors: ["stripe-checkout", "paypal-checkout"] }]);
    expect(routeClashes([{ key: "login-oauth", routes: ROUTES.tpl_login }, { key: "stripe-checkout", routes: ROUTES.tpl_stripe }])).toEqual([]);
    expect(conflictLine(clash[0], { "stripe-checkout": "Stripe", "paypal-checkout": "PayPal" }))
      .toBe("Same route: GET /api/orders/:id (Stripe and PayPal)");
    expect(conflictLine({ kind: "file", what: "stacks/node/payments/store.js", connectors: ["a", "b"] })).toBe("Same file: stacks/node/payments/store.js (a and b)");
  });

  it("merges secrets and hosts as the server does: a key needed only when no connector has a test default", () => {
    const s = mergedSecrets([card(LOGIN), card(EMAIL)]);
    expect(s.map((x) => [x.name, x.testDefault, x.templateId])).toEqual([
      ["SESSION_SECRET", "generated", "tpl_login"], ["OAUTH_CLIENT_SECRET", "none", "tpl_login"], ["RESEND_API_KEY", "stub", "tpl_email"]]);
    expect(s[0].by).toEqual(["Login", "Email"]);
    expect(mergedEgress([card(LOGIN), card(EMAIL)])).toEqual(["accounts.google.com", "api.resend.com"]);
    expect(unmetRequires([{ key: "stripe-checkout", name: "Stripe", requires: ["catalog-cart", "login-oauth"] },
                          { key: "login-oauth", name: "Login", requires: [] }], ["catalog-cart"])).toEqual([]);
    expect(unmetRequires([{ key: "stripe-checkout", name: "Stripe", requires: ["catalog-cart"] }], [])).toEqual(["catalog-cart"]);
  });

  it("says what stops Start, as the thing to do", () => {
    const ok = { target: { kind: "new" as const }, name: "Shop", stacks: ["node"], stack: "node", stackOff: false, clashes: [],
                 unmet: [], egress: ["a.example"], egressOk: true, needs: ["K"], typed: { K: "v" } };
    expect(composeBlocker(ok)).toBe("");
    expect(composeBlocker({ ...ok, stacks: [] })).toBe("These connectors share no stack: add them in separate builds.");
    expect(composeBlocker({ ...ok, clashes: [{ kind: "route", what: "GET /x", connectors: ["a", "b"] }] }))
      .toBe("Two connectors serve the same route: leave one out.");
    expect(composeBlocker({ ...ok, unmet: ["catalog-cart"] })).toBe("Add catalog-cart too, or first: a connector here needs it.");
    expect(composeBlocker({ ...ok, name: " " })).toBe("Name the new app.");
    expect(composeBlocker({ ...ok, egressOk: false })).toBe("Allow the hosts they talk to.");
    expect(composeBlocker({ ...ok, typed: {} })).toBe("Paste a test key for K.");
  });
});

describe("the compose sheet", () => {
  it("one build for Login + Email: merged keys and hosts, every connector sent in the order picked", async () => {
    const started = vi.fn();
    composeAnswers = [[409, { error: "secrets_needed", message: "set a test value for: OAUTH_CLIENT_SECRET",
                              secrets: ["OAUTH_CLIENT_SECRET"], app_key: "shop" }],
                      [201, { workflow_run_id: "wfr_c1", app_key: "shop", order: ["login-oauth", "transactional-email"], egress: [], connectors: [] }]];
    await mount(<ComposeSheet projectId="p1" cards={[card(LOGIN), card(EMAIL)]} target={{ kind: "new" }}
                              onStarted={started} onClose={() => {}} />);
    const sheet = $('[data-testid="cmp-sheet"]');
    expect(sheet.getAttribute("role")).toBe("dialog");
    expect(sheet.textContent).toContain("Login + Email");
    expect([...$('[data-testid="cmp-order"]').querySelectorAll("li")].map((li) => li.getAttribute("data-testid")))
      .toEqual(["cmp-connector-login-oauth", "cmp-connector-transactional-email"]);
    expect(document.activeElement).toBe($('[data-testid="cmp-app-name"]'));      // focus goes in
    expect($('[data-testid="cmp-stack-node"]').getAttribute("aria-checked")).toBe("true");   // the one they share
    expect($('[data-testid="cmp-stack-python"]')).toBeNull();
    // SESSION_SECRET: Email has no default but Login generates one, so it is never asked for.
    expect($('[data-testid="cmp-secret-input-SESSION_SECRET"]')).toBeNull();
    expect($('[data-testid="cmp-secret-input-OAUTH_CLIENT_SECRET"]').getAttribute("aria-label")).toBe("Test key for OAUTH_CLIENT_SECRET");
    expect($('[data-testid="cmp-egress"]').textContent).toContain("these 2 hosts");
    expect($('[data-testid="cmp-blocked"]').textContent).toBe("Name the new app.");
    await type('[data-testid="cmp-app-name"]', "Shop");
    expect($('[data-testid="cmp-blocked"]').textContent).toBe("Allow the hosts they talk to.");
    await press('[data-testid="cmp-egress-ok"]');
    expect($('[data-testid="cmp-blocked"]').textContent).toBe("Paste a test key for OAUTH_CLIENT_SECRET.");
    await press('[data-testid="cmp-start"]');
    expect(writes()).toEqual([]);                                   // Start waits; nothing sent
    await type('[data-testid="cmp-secret-input-OAUTH_CLIENT_SECRET"]', "test-client-secret");
    await press('[data-testid="cmp-start"]');
    const w = writes();
    expect(w[0]).toMatchObject({ method: "POST", path: "/v1/templates/compose", body: {
      project_id: "p1", stack: "node", templates: ["tpl_login", "tpl_email"], new_app: { name: "Shop" },
      accept_egress: ["accounts.google.com", "api.resend.com"] } });
    expect([w[1].method, w[1].path, w[1].body?.template_id, w[1].body?.envs]).toEqual(
      ["PUT", "/v1/apps/shop/secrets/OAUTH_CLIENT_SECRET", "tpl_login", ["build", "preview"]]);
    expect(w[2].path).toBe("/v1/templates/compose");
    expect(started).toHaveBeenCalledWith("wfr_c1");
    expect(document.body.textContent).not.toContain("test-client-secret");
  });

  it("a connector_conflict lists every conflict, focused and in words, and nothing starts", async () => {
    const started = vi.fn();
    composeAnswers = [[409, { error: "connector_conflict", message: "these connectors cannot be applied together as they are",
      conflicts: [{ kind: "webhook", what: "/webhooks/payments", connectors: ["stripe-checkout", "login-oauth"] },
                  { kind: "file", what: "stacks/node/payments/store.js", connectors: ["stripe-checkout", "login-oauth"] }] }]];
    await mount(<ComposeSheet projectId="p1" cards={[card(STRIPE), card(LOGIN)]}
                              target={{ kind: "add", runId: "crun_1", appKey: "shop", name: "Shop", stack: "node" }}
                              onStarted={started} onClose={() => {}} />);
    expect($('[data-testid="cmp-conflicts"]')).toBeNull();
    await press('[data-testid="cmp-egress-ok"]');
    await press('[data-testid="cmp-start"]');
    expect(writes()[0].body).toMatchObject({ extends_run_id: "crun_1", templates: ["tpl_stripe", "tpl_login"] });
    const box = $('[data-testid="cmp-conflicts"]');
    expect(box.getAttribute("role")).toBe("alert");
    expect([...box.querySelectorAll("li")].map((li) => li.textContent)).toEqual([
      "Same webhook: /webhooks/payments (Stripe and Login)",
      "Same file: stacks/node/payments/store.js (Stripe and Login)"]);
    expect(document.activeElement).toBe(box);
    expect($('[data-testid="cmp-error"]').textContent).toContain("cannot go into one build");
    expect(document.body.textContent).not.toContain("connector_conflict");
    expect(started).not.toHaveBeenCalled();
  });

  it("Stripe + PayPal: the route clash their manifests show is said before Start, which stays off", async () => {
    await mount(<ComposeSheet projectId="p1" cards={[card(STRIPE), card(PAYPAL)]}
                              target={{ kind: "add", runId: "crun_1", appKey: "shop", name: "Shop", stack: "node" }}
                              onStarted={() => {}} onClose={() => {}} />);
    expect($('[data-testid="cmp-conflicts"]').textContent).toContain("Same route: GET /api/orders/:id (Stripe and PayPal)");
    expect($('[data-testid="cmp-blocked"]').textContent).toBe("Two connectors serve the same route: leave one out.");
    expect(($('[data-testid="cmp-start"]') as HTMLButtonElement).disabled).toBe(true);
    await press('[data-testid="cmp-start"]');
    expect(writes()).toEqual([]);
  });

  it("any other refusal is the API's sentence", async () => {
    composeAnswers = [[409, { error: "requires_unmet", message: "stripe-checkout needs these applied to the app first: catalog-cart" }]];
    await mount(<ComposeSheet projectId="p1" cards={[card(STRIPE), card(LOGIN)]}
                              target={{ kind: "add", runId: "crun_1", appKey: "shop", name: "Shop", stack: "node" }}
                              onStarted={() => {}} onClose={() => {}} />);
    await press('[data-testid="cmp-egress-ok"]');
    await press('[data-testid="cmp-start"]');
    expect($('[data-testid="cmp-error"]').textContent).toBe("stripe-checkout needs these applied to the app first: catalog-cart");
    expect($('[data-testid="cmp-conflicts"]')).toBeNull();
  });
});
