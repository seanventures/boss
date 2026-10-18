// @vitest-environment happy-dom
/**
 * APP › CONNECTORS AND SETTINGS (BOSSAPP-568), on HelpDesk AI's real shape: twelve build attempts of
 * "Login with Google, GitHub or BOSS", a flat list of env names, BOSS's own container address as the
 * preview URL, and a plain-http IP deploy.
 *
 *   - one card per connector: 12 attempts → 1 card, the newest attempt's state, 11 folded;
 *   - a row per provider (Google, GitHub, BOSS, Demo login) with its status;
 *   - a warning only where an address cannot work (http:// or an IP for Google; an address inside
 *     BOSS; no public address), and api:3001 never shown as something to use;
 *   - the Apps row's chip only when a connector needs the operator;
 *   - Settings split into Managed by BOSS and Your settings;
 *   - every capability of the old sheet still reachable (the parity list at the bottom).
 */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { IdentityProvider } from "../components/identity/identity-provider";
import type { MePermissionsResponse, MeResponse } from "../lib/identity/contract";
import { ROLE_PERMISSIONS } from "../lib/identity/permissions.gen";
import {
  addressProblem, connectorCards, isInternalHost, parseSetupNeeds, previewReachWords, providerCallbacks, providerRow,
  settingsSplit, setupChip, summaryLine, whereLine,
} from "../lib/app-connectors";
import { parseApplied, parseSecretsView, parseSetupGuides, type SecretBinding } from "../lib/templates";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }) }));
const useBoss = vi.fn(async () => ({ ok: true, message: "Sign in with BOSS is on" }));
const stopBoss = vi.fn(async () => ({ ok: true, message: "Sign in with BOSS is off for this app" }));
vi.mock("../app/factory/sign-in-actions", () => ({ useSignInWithBossAction: useBoss, stopSignInWithBossAction: stopBoss }));
vi.mock("../app/factory/links-actions", () => ({
  createLinkAction: vi.fn(), revokeLinkAction: vi.fn(), revealTestUsersAction: vi.fn(),
}));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const { AppSetup } = await import("../components/v2/templates/setup-sheet");
const { AppsTab } = await import("../app/factory/apps-tab");

// ---- HelpDesk AI, as the API answers it ------------------------------------------------------------

const PROVIDERS = [
  { provider: "google", id: "GOOGLE_CLIENT_ID", secret: "GOOGLE_CLIENT_SECRET" },
  { provider: "github", id: "GITHUB_CLIENT_ID", secret: "GITHUB_CLIENT_SECRET" },
  { provider: "boss", id: "OIDC_BOSS_CLIENT_ID", secret: "OIDC_BOSS_CLIENT_SECRET" },
];
const LOGIN_SECRETS = ["GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GITHUB_CLIENT_ID", "GITHUB_CLIENT_SECRET", "SESSION_SECRET",
  "OIDC_BOSS_CLIENT_ID", "OIDC_BOSS_CLIENT_SECRET"];
const STATUSES = ["blocked", "building", "failed", "applied", "failed", "failed", "blocked", "failed", "failed", "failed", "failed", "failed"];
const TEMPLATES = {
  app_key: "helpdesk ai",
  templates: STATUSES.map((status, i) => ({
    id: `tap_${i}`, template_id: "tpl_login", template_key: "login-oauth", name: "Login with Google, GitHub or BOSS",
    version: "1.0.0", stack: "node", status, created_at: `2026-10-${String(15 - i).padStart(2, "0")}T10:00:00Z`,
    blocked_reason: status === "blocked" ? "Blocked: the plan needs approval" : null,
    secrets: LOGIN_SECRETS, sign_in_providers: PROVIDERS,
  })),
};
const stub = (name: string, env: string) => ({ name, env, mode: "stub", shape: "unknown" });
const gen = (name: string, env: string) => ({ name, env, mode: "generated", shape: "unknown" });
const SECRETS = {
  app_key: "helpdesk ai",
  secrets: [
    ...["build", "preview"].flatMap((e) => ["GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GITHUB_CLIENT_ID", "GITHUB_CLIENT_SECRET",
      "OIDC_BOSS_CLIENT_ID", "OIDC_BOSS_CLIENT_SECRET"].map((n) => stub(n, e))),
    gen("SESSION_SECRET", "build"), gen("SESSION_SECRET", "preview"), gen("SESSION_SECRET", "deploy"),
    gen("DEMO_PASSWORD", "preview"), gen("DEMO_PASSWORD", "deploy"),
    { name: "DATABASE_URL", env: "deploy", mode: "test", shape: "unknown" },
    { name: "ALLOW_DEMO_SEED", env: "deploy", mode: "test", shape: "unknown" },
  ],
  declared: [
    ...["GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GITHUB_CLIENT_ID", "GITHUB_CLIENT_SECRET", "OIDC_BOSS_CLIENT_ID", "OIDC_BOSS_CLIENT_SECRET"]
      .map((name) => ({ name, purpose: "", test_default: "stub", required: false, bound: true })),
    { name: "SESSION_SECRET", purpose: "Signs the session cookie", test_default: "generated", required: true, bound: true },
  ],
  deploy_values: ["ALLOW_DEMO_SEED", "DATABASE_URL", "DEMO_PASSWORD"],
};
const GUIDE = {
  app_key: "helpdesk ai", boss_public_url: null,
  preview: { running: true, url: null, reach: "inside_only" },
  guides: [{ template_key: "login-oauth", name: "Login with Google, GitHub or BOSS", version: "1.0.0", status: "blocked",
    guide: "# Login with Google, GitHub or BOSS: setup\n\n## Google\n\n1. Open the Google Cloud console.",
    copy: { preview_url: "(the preview is reachable inside BOSS only: set BOSS's public address)",
            public_url: "(BOSS has no public address yet: set BOSS_PUBLIC_URL)",
            redirect_uri: "<preview>/auth/callback", deploy_url: "http://203.0.113.10:8000",
            "callback_url:google": "http://203.0.113.10:8000/auth/callback/google",
            "callback_url:github": "http://203.0.113.10:8000/auth/callback/github",
            "callback_url:boss": "http://203.0.113.10:8000/auth/callback/boss" } }],
};
const BOSS_OFF = { app_key: "helpdesk ai", template_applied: true, connected: false, secrets: { preview: [], deploy: [] }, client: null };

type Call = { method: string; path: string; body: Record<string, unknown> | undefined };
let calls: Call[] = [];
let guide: unknown = GUIDE;
function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}
beforeEach(() => {
  calls = [];
  guide = GUIDE;
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input).replace(/^https?:\/\/[^/]+/, "");
    const method = (init?.method ?? "GET").toUpperCase();
    calls.push({ method, path, body: init?.body ? JSON.parse(String(init.body)) : undefined });
    const app = "/v1/apps/helpdesk%20ai";
    if (path.startsWith(`${app}/templates`)) return json(TEMPLATES);
    if (path.startsWith(`${app}/secrets/`)) return json({ set: true });
    if (path.startsWith(`${app}/secrets`)) return json(SECRETS);
    if (path.startsWith(`${app}/setup-guide`)) return json(guide);
    if (path.startsWith(`${app}/sign-in-with-boss`)) return json(BOSS_OFF);
    if (path.startsWith(`${app}/demo-logins`)) {
      // env=deploy also answers the value a stack deploy generated on a TARGET, labelled by it.
      return json(path.includes("env=deploy")
        ? { demo_logins: [{ name: "DEMO_PASSWORD", value: "pw-staging", label: "Staging", target_id: "deptgt_s" }] }
        : { demo_logins: [{ name: "DEMO_PASSWORD", value: "pw-generated" }] });
    }
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
async function settle() { for (let i = 0; i < 10; i += 1) await act(async () => { await new Promise((r) => setTimeout(r, 0)); }); }
async function mount(node: React.ReactElement, role: keyof typeof ROLE_PERMISSIONS = "owner") {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => root!.render(<IdentityProvider me={ME} org={perms(role)}>{node}</IdentityProvider>));
  await settle();
}
const $ = (sel: string) => document.querySelector(sel) as HTMLElement;
const $$ = (sel: string) => [...document.querySelectorAll(sel)] as HTMLElement[];
async function press(sel: string) { await act(async () => $(sel).click()); await settle(); }
async function type(sel: string, value: string) {
  const el = $(sel) as HTMLInputElement;
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(el, value);
    el.dispatchEvent(new Event("input", { bubbles: true }));
  });
  await settle();
}
async function choose(sel: string, value: string) {
  const el = $(sel) as HTMLSelectElement;
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value")!.set!.call(el, value);
    el.dispatchEvent(new Event("change", { bubbles: true }));
  });
  await settle();
}
const writes = () => calls.filter((c) => c.method !== "GET");

// ---- the rules ---------------------------------------------------------------------------------------

const applied = parseApplied(TEMPLATES);
const secrets = parseSecretsView(SECRETS);
const bind = (name: string, env: string, mode: string, liveApproved: boolean | null = null): SecretBinding =>
  ({ name, env, mode, shape: "unknown", setAt: "", lastUsedAt: "", liveApproved });
const GOOGLE = PROVIDERS[0];

describe("one card per connector", () => {
  it("12 build attempts are one card: the newest attempt's state, the other 11 folded", () => {
    const cards = connectorCards(applied);
    expect(cards).toHaveLength(1);
    expect(cards[0].latest.status).toBe("blocked");
    expect(cards[0].latest.createdAt).toBe("2026-10-15T10:00:00Z");
    expect(cards[0].earlier).toHaveLength(11);
    expect(cards[0].inApp).toBe(true);
    expect(cards[0].providers.map((p) => p.provider)).toEqual(["google", "github", "boss"]);
  });
  it("a connector whose every attempt failed is not in the app", () => {
    const failed = connectorCards(applied.map((a) => ({ ...a, status: "failed" })));
    expect(failed[0].inApp).toBe(false);
    expect(summaryLine({ card: failed[0], rows: [], keys: [], bindings: [], hasDemo: false })).toMatch(/^Not in the app/);
  });
  it("two connectors stay two cards, in the order of their newest attempt", () => {
    const two = connectorCards([...applied, { ...applied[0], id: "tap_s", key: "stripe-checkout", name: "Stripe", createdAt: "2026-10-16T00:00:00Z" }]);
    expect(two.map((c) => c.key)).toEqual(["stripe-checkout", "login-oauth"]);
  });
});

describe("a row per provider, with its status", () => {
  it("stubs alone are Needs keys; BOSS with no public address can't work yet", () => {
    const rows = PROVIDERS.map((spec) => providerRow({ spec, bindings: secrets.bindings, bossPublicUrl: null, deployLabel: "Staging" }));
    expect(rows.map((r) => [r.name, r.word])).toEqual([["Google", "Needs keys"], ["GitHub", "Needs keys"], ["BOSS", "Can't work yet"]]);
    expect(rows.every((r) => r.needsYou)).toBe(true);
    expect(providerRow({ spec: PROVIDERS[2], bindings: [], bossPublicUrl: "https://boss.acme.dev" }).word).toBe("Not connected");
  });
  it("preview keys, deploy keys, both, and a live key awaiting approval", () => {
    const pv = [bind("GOOGLE_CLIENT_ID", "preview", "test"), bind("GOOGLE_CLIENT_SECRET", "preview", "test")];
    const dp = [bind("GOOGLE_CLIENT_ID", "deploy", "live", true), bind("GOOGLE_CLIENT_SECRET", "deploy", "live", true)];
    const row = (b: SecretBinding[]) => providerRow({ spec: GOOGLE, bindings: b, bossPublicUrl: null, deployLabel: "Staging" });
    expect(row(pv).word).toBe("Preview only · Staging needs keys");
    expect(row(pv).needsYou).toBe(false);
    expect(row(dp).word).toBe("Staging only");
    expect(row([...pv, ...dp]).word).toBe("Ready");
    expect(row([...pv, bind("GOOGLE_CLIENT_ID", "deploy", "live", false), bind("GOOGLE_CLIENT_SECRET", "deploy", "live", false)]).word)
      .toBe("Live key awaiting approval");
  });
  it("the card's summary and Where line", () => {
    const card = connectorCards(applied)[0];
    const rows = card.providers.map((spec) => providerRow({ spec, bindings: secrets.bindings, bossPublicUrl: null }));
    expect(summaryLine({ card, rows, keys: [], bindings: secrets.bindings, hasDemo: true }))
      .toBe("Works in the preview now with the demo login. Real sign-in needs 3 providers set up.");
    expect(whereLine({ card, rows, keys: [], bindings: secrets.bindings, hasDemo: true, deployLabel: "Staging" })
      .map((w) => `${w.env} · ${w.word}`)).toEqual(["Preview · demo login", "Staging · needs keys"]);
  });
});

describe("a warning only when an address cannot work", () => {
  it("Google: http:// off localhost, and a bare IP", () => {
    expect(addressProblem("http://203.0.113.10:8000/auth/callback/google", "google")).toMatch(/only https:\/\/ callbacks on a domain name/);
    expect(addressProblem("http://helpdesk.acme.dev/auth/callback/google", "google")).toMatch(/only https:\/\//);
    expect(addressProblem("https://203.0.113.9/auth/callback/google", "google")).toMatch(/bare IP/);
    expect(addressProblem("http://localhost:3000/auth/callback/google", "google")).toBe("");
    expect(addressProblem("https://helpdesk.acme.dev/auth/callback/google", "google")).toBe("");
  });
  it("GitHub accepts http; nobody can reach an address inside BOSS", () => {
    expect(addressProblem("http://203.0.113.10:8000/auth/callback/github", "github")).toBe("");
    expect(addressProblem("http://api:3001/v1/previews/p1/web/auth/callback/github", "github")).toMatch(/inside BOSS/);
    expect(isInternalHost("api")).toBe(true);
    expect(isInternalHost("localhost")).toBe(false);
    expect(isInternalHost("boss.acme.dev")).toBe(false);
  });
  it("no public address: the preview is said to be inside BOSS only, never given as api:3001", () => {
    const g = parseSetupGuides(GUIDE);
    expect(previewReachWords(g.addresses)).toBe("The preview is reachable inside BOSS only: set BOSS's public address.");
    const old = parseSetupGuides({ ...GUIDE, preview: { running: true, url: "http://api:3001/v1/previews/p1/web", reach: "public" } });
    expect(previewReachWords(old.addresses)).toMatch(/inside BOSS only/);
    const cbs = providerCallbacks({ provider: "google", guide: { ...g.guides[0], copy: { ...g.guides[0].copy, redirect_uri: "http://api:3001/v1/previews/p1/web/auth/callback" } }, addresses: old.addresses });
    expect(cbs.callbacks.map((c) => c.url)).toEqual(["http://203.0.113.10:8000/auth/callback/google"]);
    expect(cbs.missing[0]).toMatch(/inside BOSS only/);
  });
  it("a public preview gives its callback, with no warning for an https address", () => {
    const g = parseSetupGuides({ ...GUIDE, boss_public_url: "https://boss.acme.dev",
      preview: { running: true, url: "https://previews.acme.dev/v1/previews/p1/web", reach: "public" } });
    const cbs = providerCallbacks({ provider: "google", guide: { ...g.guides[0], copy: { ...g.guides[0].copy, redirect_uri: "https://previews.acme.dev/v1/previews/p1/web/auth/callback" } }, addresses: g.addresses });
    expect(cbs.callbacks[0]).toEqual({ env: "preview", url: "https://previews.acme.dev/v1/previews/p1/web/auth/callback/google", problem: "" });
  });
});

describe("the Apps row's chip", () => {
  it("only when a connector needs the operator", () => {
    expect(setupChip(parseSetupNeeds({ providers: ["google", "github"], keys: [] }))).toEqual({
      text: "Set up sign-in · 2 providers", tip: "Google, GitHub need keys before real people can sign in" });
    expect(setupChip(parseSetupNeeds({ providers: [], keys: ["STRIPE_SECRET_KEY"] }))!.text).toBe("Set up · 1 key");
    expect(setupChip(parseSetupNeeds({ providers: [], keys: [] }))).toBeNull();
    expect(setupChip(parseSetupNeeds(null))).toBeNull();
  });
  it("is on the row, and opens the app's Connectors", async () => {
    const row = (setup: unknown) => ({ run_id: "crun_1", key: "crun_1", app: "HelpDesk AI", app_key: "helpdesk ai", word: "PR open",
      builders: [], attempts: [], deploys: [{ env: "staging", ok: true }], setup });
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path.startsWith("/v1/factory/apps")) return json({ apps: [row({ providers: ["google", "github"], keys: [] }), { ...row(null), run_id: "crun_2", key: "crun_2", app: "Notes", app_key: "notes" }] });
      if (path.includes("/templates")) return json(TEMPLATES);
      if (path.includes("/setup-guide")) return json(GUIDE);
      if (path.includes("/secrets")) return json(SECRETS);
      if (path.includes("/sign-in-with-boss")) return json(BOSS_OFF);
      return json({}, 404);
    }));
    await mount(<AppsTab projectId="p1" runs={[]} previews={[]} query="" onOpen={() => {}} onNew={() => {}} />);
    expect($('[data-testid="fx-app-setup-chip-crun_1"]').textContent).toBe("Set up sign-in · 2 providers");
    expect($('[data-testid="fx-app-setup-chip-crun_1"]').tagName).toBe("BUTTON");
    expect($('[data-testid="fx-app-setup-chip-crun_2"]')).toBeNull();
    // The chip sits UNDER the name, never in its place: the name and the row's ⋯ always show.
    const title = $('[data-testid="fx-app-setup-chip-crun_1"]').closest(".fx-app-title")!;
    expect(title.querySelector(".fx-app-name")!.textContent).toBe("HelpDesk AI");
    expect($('[data-testid="fx-app-more-crun_1"]')).toBeTruthy();
    const css = readFileSync(resolve(__dirname, "../app/factory/factory.css"), "utf8");
    expect(css).toMatch(/\.fx-app-title\s*\{[^}]*flex-direction:\s*column/);
    expect(css.match(/\.fx-setup-chip\s*\{[^}]*\}/)![0]).not.toMatch(/flex-shrink:\s*0/);
    await press('[data-testid="fx-app-setup-chip-crun_1"]');
    expect($('[data-testid="tpl-setup-sheet"]')).toBeTruthy();
    expect($('[data-testid="tpl-tab-connectors"]').getAttribute("aria-selected")).toBe("true");
    expect($('[data-testid="tpl-where-login-oauth"]').textContent).toContain("Staging · needs keys");
  });
});

describe("settings split", () => {
  it("Managed by BOSS vs Your settings", () => {
    const { managed, yours } = settingsSplit({ secrets, cards: connectorCards(applied) });
    expect(managed.map((r) => [r.name, r.how])).toEqual([["DEMO_PASSWORD", "Generated by BOSS · demo login"], ["SESSION_SECRET", "Generated by BOSS"]]);
    expect(yours.map((r) => r.name)).toEqual(["ALLOW_DEMO_SEED", "DATABASE_URL"]);
  });
  it("a connector's secret a person replaced is theirs", () => {
    const mine = { ...secrets, bindings: [...secrets.bindings.filter((b) => b.name !== "SESSION_SECRET"), bind("SESSION_SECRET", "deploy", "test")] };
    expect(settingsSplit({ secrets: mine, cards: connectorCards(applied) }).yours.map((r) => r.name)).toContain("SESSION_SECRET");
  });
});

// ---- the screen ------------------------------------------------------------------------------------

describe("HelpDesk AI's Connectors", () => {
  it("one card, four provider rows, the folded history, and no internal address anywhere", async () => {
    await mount(<AppSetup projectId="p1" appKey="helpdesk ai" deployLabel="Staging" />);
    expect($$('[data-testid^="tpl-connector-login-oauth"]').filter((e) => e.tagName === "ARTICLE")).toHaveLength(1);
    expect($('[data-testid="tpl-applied-login-oauth"]').textContent).toContain("Blocked");
    expect($('[data-testid="tpl-connector-history-login-oauth"] summary').textContent).toBe("11 earlier build attempts");
    expect($('[data-testid="tpl-connector-history-login-oauth"]').hasAttribute("open")).toBe(false);
    expect($$('[data-testid^="tpl-provider-login-oauth-"]').map((e) => e.getAttribute("data-testid")))
      .toEqual(["tpl-provider-login-oauth-google", "tpl-provider-login-oauth-github", "tpl-provider-login-oauth-boss", "tpl-provider-login-oauth-demo"]);
    expect($('[data-testid="tpl-provider-status-login-oauth-boss"]').textContent).toBe("Can't work yet");
    // Every row is one line until opened; only one opens at a time.
    expect($('[data-testid="tpl-boss-no-address"]')).toBeNull();
    expect($('[data-testid="tpl-provider-form-login-oauth-google"]')).toBeNull();
    await press('[data-testid="tpl-provider-open-login-oauth-boss"]');  // rows start collapsed (the mockup)
    expect($('[data-testid="tpl-boss-no-address"]').textContent).toContain("BOSS_PUBLIC_URL");
    await press('[data-testid="tpl-provider-open-login-oauth-google"]');  // rows start collapsed (the mockup)
    expect($('[data-testid="tpl-boss-no-address"]')).toBeNull();
    expect($('[data-testid="tpl-provider-login-oauth-google"]').hasAttribute("data-open")).toBe(true);
    expect($('[data-testid="tpl-summary-login-oauth"]').textContent).toContain("Works in the preview now with the demo login.");
    // Google warns about http + IP; GitHub, on the same address, does not.
    expect($('[data-testid="tpl-warning-login-oauth-google-deploy"]').textContent).toMatch(/https/);
    expect($('[data-testid="tpl-warning-login-oauth-github-deploy"]')).toBeNull();
    expect($('[data-testid="tpl-callback-login-oauth-google-deploy"]').textContent).toContain("http://203.0.113.10:8000/auth/callback/google");
    expect($('[data-testid="tpl-callback-missing-login-oauth-google"]').textContent).toMatch(/inside BOSS only/);
    expect(document.body.textContent).not.toContain("api:3001");
    // No per-secret Build/Preview/Deploy chips for the provider secrets.
    expect($('[data-testid="tpl-env-GOOGLE_CLIENT_ID-build"]')).toBeNull();
  });

  it("an internal address from an older API is never shown", async () => {
    guide = { ...GUIDE, preview: { running: true, url: "http://api:3001/v1/previews/p1/web", reach: "public" },
      guides: [{ ...GUIDE.guides[0], copy: { ...GUIDE.guides[0].copy, preview_url: "http://api:3001/v1/previews/p1/web",
        redirect_uri: "http://api:3001/v1/previews/p1/web/auth/callback" } }] };
    await mount(<AppSetup projectId="p1" appKey="helpdesk ai" />);
    expect(document.body.textContent).not.toContain("api:3001");
    expect($('[data-testid="tpl-preview-reach-login-oauth"]').textContent).toMatch(/inside BOSS only/);
  });

  it("Google's keys: Client ID + secret, Use for Both = one guarded call, test keys for previews and live keys for the deploy", async () => {
    await mount(<AppSetup projectId="p1" appKey="helpdesk ai" deployLabel="Staging" />);
    await press('[data-testid="tpl-provider-open-login-oauth-google"]');  // rows start collapsed (the mockup)
    expect($('[data-testid="tpl-provider-save-login-oauth-google"]').hasAttribute("disabled")).toBe(true);
    expect($('label[for]').textContent).toBe("Client ID");
    await type('[data-testid="tpl-provider-id-login-oauth-google"]', "123-abc.apps.googleusercontent.com");
    await type('[data-testid="tpl-provider-secret-login-oauth-google"]', "GOCSPX-abcdefghijklmnopqrstuvwx");
    await choose('[data-testid="tpl-provider-use-login-oauth-google"]', "both");
    await press('[data-testid="tpl-provider-save-login-oauth-google"]');
    expect(writes().map((c) => [c.path.split("?")[0], c.body?.envs, c.body?.mode])).toEqual([
      ["/v1/apps/helpdesk%20ai/secrets/GOOGLE_CLIENT_ID", ["build", "preview"], "test"],
      ["/v1/apps/helpdesk%20ai/secrets/GOOGLE_CLIENT_SECRET", ["build", "preview"], "test"],
      ["/v1/apps/helpdesk%20ai/secrets/GOOGLE_CLIENT_ID", ["deploy"], "live"],
      ["/v1/apps/helpdesk%20ai/secrets/GOOGLE_CLIENT_SECRET", ["deploy"], "live"],
    ]);
    expect(document.body.innerHTML).not.toContain("GOCSPX-abcdefghijklmnopqrstuvwx");
    expect($('[data-testid="tpl-setup-note"]').textContent).toContain("Google's keys are stored for previews and the deploy.");
    expect($('[data-testid="tpl-setup-note"]').textContent).toContain("Restart the preview");
  });

  it("an Operator stores preview keys but a deploy (live) key is greyed with the reason", async () => {
    await mount(<AppSetup projectId="p1" appKey="helpdesk ai" />, "operator");
    await press('[data-testid="tpl-provider-open-login-oauth-github"]');  // rows start collapsed (the mockup)
    await type('[data-testid="tpl-provider-id-login-oauth-github"]', "Iv1.abc");
    await type('[data-testid="tpl-provider-secret-login-oauth-github"]', "s3cr3t");
    expect($('[data-testid="tpl-provider-save-login-oauth-github"]').getAttribute("data-can")).toBe("yes");
    await choose('[data-testid="tpl-provider-use-login-oauth-github"]', "deploy");
    expect($('[data-testid="tpl-provider-save-login-oauth-github"]').getAttribute("aria-disabled")).toBe("true");
  });

  it("the demo login row shows the generated password on request", async () => {
    await mount(<AppSetup projectId="p1" appKey="helpdesk ai" />);
    expect($('[data-testid="tpl-provider-login-oauth-demo"]').textContent).toContain("Ready");
    await press('[data-testid="tpl-demo-logins-row-show"]');
    expect($('[data-testid="tpl-demo-login-preview-DEMO_PASSWORD"]')).toBeTruthy();
    // The staging deploy's own generated password has its row, named for the target's environment.
    const staging = $('[data-testid="tpl-demo-login-deploy-DEMO_PASSWORD-deptgt_s"]');
    expect(staging).toBeTruthy();
    expect(staging.textContent).toContain("Staging");
  });

  it("Settings: BOSS's values read-only, the person's with Edit and Remove, and Add setting", async () => {
    await mount(<AppSetup projectId="p1" appKey="helpdesk ai" initialTab="settings" />);
    expect($('[data-testid="tpl-settings-managed"]').textContent).toContain("SESSION_SECRET");
    expect($('[data-testid="tpl-managed-DEMO_PASSWORD"]').textContent).toContain("Generated by BOSS");
    expect($('[data-testid="tpl-managed-DEMO_PASSWORD"] button')).toBeNull();            // read-only
    expect($('[data-testid="tpl-deploy-value-edit-ALLOW_DEMO_SEED"]')).toBeTruthy();
    await press('[data-testid="tpl-deploy-value-remove-DATABASE_URL"]');
    await press('[data-testid="tpl-deploy-value-remove-yes-DATABASE_URL"]');
    expect(writes().map((c) => [c.method, c.path.split("?")[0]])).toEqual([["DELETE", "/v1/apps/helpdesk%20ai/secrets/DATABASE_URL"]]);
    await press('[data-testid="tpl-deploy-value-edit-ALLOW_DEMO_SEED"]');
    await type('[data-testid="tpl-deploy-value-edit-value-ALLOW_DEMO_SEED"]', "true");
    await press('[data-testid="tpl-deploy-value-edit-save-ALLOW_DEMO_SEED"]');
    expect(writes().slice(-1).map((c) => [c.method, c.body?.envs, c.body?.mode])).toEqual([["PUT", ["deploy"], "test"]]);
    expect($('[data-testid="tpl-demo-logins-show"]')).toBeTruthy();
  });
});

describe("every capability of the old Setup sheet is still reachable", () => {
  // old → new: each old control's job, and where it is now.
  it("on a sign-in connector", async () => {
    await mount(<AppSetup projectId="p1" appKey="helpdesk ai" />);
    const reach = {
      "connector list + state": '[data-testid="tpl-applied-login-oauth"]',
      "every build attempt": '[data-testid="tpl-connector-history-login-oauth"]',
      "set a provider's test key": '[data-testid="tpl-provider-form-login-oauth-google"]',
      "setup guide steps": '[data-testid="tpl-guide-login-oauth"]',
      "callback URL with Copy": '[data-testid="tpl-callback-login-oauth-github-deploy"] button',
      "deployed URL with Copy": '[data-testid="tpl-copy-login-oauth-deploy_url"] button',
      "demo login passwords": '[data-testid="tpl-demo-logins-row-show"]',
      "BOSS address / how to set it": '[data-testid="tpl-boss-no-address"]',
    };
    // Provider details are behind each row's Set up: open the row a job lives in, then look.
    const row: Record<string, string> = {
      "set a provider's test key": "google", "callback URL with Copy": "github",
      "BOSS address / how to set it": "boss",
    };
    for (const [job, sel] of Object.entries(reach)) {
      if (row[job]) await press(`[data-testid="tpl-provider-open-login-oauth-${row[job]}"]`);
      expect($(sel), job).toBeTruthy();
    }
    await press('[data-testid="tpl-tab-settings"]');
    for (const [job, sel] of Object.entries({
      "app settings for deploy: add": '[data-testid="tpl-deploy-value-form"]',
      "app settings for deploy: remove": '[data-testid="tpl-deploy-value-remove-ALLOW_DEMO_SEED"]',
      "a generated secret, replace with your own": '[data-testid="tpl-set-test-SESSION_SECRET"]',
      "…or a live key": '[data-testid="tpl-set-live-SESSION_SECRET"]',
      "demo login passwords": '[data-testid="tpl-demo-logins-show"]',
    })) expect($(sel), job).toBeTruthy();
  });
  it("the BOSS row's Sign in with BOSS once BOSS has a public address", async () => {
    guide = { ...GUIDE, boss_public_url: "https://boss.acme.dev" };
    await mount(<AppSetup projectId="p1" appKey="helpdesk ai" />);
    expect($('[data-testid="tpl-provider-status-login-oauth-boss"]').textContent).toBe("Not connected");
    await press('[data-testid="tpl-provider-open-login-oauth-boss"]');  // rows start collapsed (the mockup)
    await press('[data-testid="fx-signin-use"]');
    expect(useBoss).toHaveBeenCalledTimes(1);
  });
});
