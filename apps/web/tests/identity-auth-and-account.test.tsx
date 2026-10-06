// @vitest-environment happy-dom
/**
 * The auth screens beyond sign-in, and My account, as they render:
 *   - Claim this BOSS asks for the proof its path needs (setup link, shared password, or the
 *     network origin it will bind to, confirmed), and is a short "Already claimed" without one;
 *   - enrolment ends on recovery codes shown once, with Continue locked until they are stored;
 *   - a used invitation, a dead magic link and a reset request each say the uniform thing;
 *   - a personal key's secret appears once, in a dialog that cannot close until stored;
 *   - every screen has one h1, every field a name, and keeps the identity copy budget.
 */
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { IdentityProvider } from "../components/identity/identity-provider";
import { NowProvider } from "../components/identity/kit";
import { ClaimFlow } from "../app/(auth)/claim/claim";
import { Enrol } from "../app/(auth)/enrol/enrol";
import { RecoveryCodesView } from "../app/(auth)/recovery-codes";
import { InviteAccept } from "../app/(auth)/invite/[token]/invite";
import { MagicLinkLanding } from "../app/(auth)/signin/link/link";
import { Reset } from "../app/(auth)/reset/reset";
import { SignUp } from "../app/(auth)/signup/signup";
import { MyKeys, presetScopes } from "../app/account/keys/my-keys";
import { MySessions } from "../app/account/sessions/sessions";
import { AgentsForMe } from "../app/account/agents/agents-for-me";
import { AccountSecurity } from "../app/account/security";
import type { MePermissionsResponse, MeResponse } from "../lib/identity/contract";
import { ROLE_PERMISSIONS } from "../lib/identity/permissions.gen";
import * as F from "../lib/identity/stub/fixtures";
import type { AuthOptions, IssuedSecret } from "../lib/identity/types";
import { identityCopyAudit } from "./_helpers/identity-copy";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }), useSearchParams: () => new URLSearchParams(), usePathname: () => "/" }));

const NOW = Date.parse("2026-10-06T11:00:00Z");
const CODES = ["7KQ2-MX4P-9TRA", "B3ZV-8WNE-QL2D", "H6YC-2PFK-UJ7S", "N9DE-4RXB-ZK8M", "Q2LW-7HTV-CY5F", "T8MS-3GQN-WD6P", "V5JR-9KCE-BX2H", "X4PA-6ZLU-MF9Q", "Z7GN-2SWD-RT3K", "E1HB-5VQY-PL8C"];
let root: Root | null = null;
let host: HTMLElement | null = null;
let calls: { method: string; url: string; body: unknown }[] = [];

type Answer = { status?: number; body: unknown };
function routes(table: Record<string, Answer | ((body: unknown) => Answer)>) {
  calls = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    const method = (init?.method ?? "GET").toUpperCase();
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    calls.push({ method, url, body });
    const key = Object.keys(table).find((k) => `${method} ${url}`.startsWith(k));
    const hit = key ? table[key] : { status: 404, body: { error: "not_found" } };
    const a = typeof hit === "function" ? hit(body) : hit;
    return new Response(JSON.stringify(a.body), { status: a.status ?? 200, headers: { "content-type": "application/json" } });
  }));
}

beforeEach(() => {
  // A browser with passkeys.
  vi.stubGlobal("PublicKeyCredential", function PublicKeyCredential() { /* present */ });
  Object.defineProperty(navigator, "credentials", { value: { get: vi.fn(), create: vi.fn() }, configurable: true });
  window.history.replaceState(null, "", "/");
});

afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  root = null;
  vi.unstubAllGlobals();
});

async function settle() {
  for (let i = 0; i < 6; i += 1) await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
}

async function mount(node: React.ReactElement) {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => root!.render(node));
  await settle();
  return host;
}

function type(el: Element | null, value: string) {
  if (!el) throw new Error("no field");
  const input = el as HTMLInputElement;
  const proto = input instanceof HTMLSelectElement ? HTMLSelectElement.prototype : input instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
  act(() => {
    Object.getOwnPropertyDescriptor(proto, "value")!.set!.call(input, value);
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
}

async function click(el: Element | null) {
  if (!el) throw new Error("nothing to click");
  await act(async () => { (el as HTMLElement).click(); });
  await settle();
}

/** One h1; every visible field named; no copy over budget. */
function accessible(el: HTMLElement) {
  expect(el.querySelectorAll("h1").length).toBe(1);
  for (const f of el.querySelectorAll("input:not([type=hidden]), select, textarea")) {
    const named = (f as HTMLInputElement).labels?.length || f.getAttribute("aria-label") || f.getAttribute("aria-labelledby");
    expect(named, f.outerHTML.slice(0, 120)).toBeTruthy();
  }
  expect(identityCopyAudit(el)).toEqual({ labels: [], prose: [], tips: [] });
}

function options(claim: AuthOptions["claim"], extra: Partial<AuthOptions> = {}): AuthOptions {
  return { org_name: "Acme BOSS", host: "boss.acme.dev", providers: [], smtp: true, signup: false, legacy_password: false, claim, ...extra };
}
const SEATS = ["ceo", "cto"];

describe("Claim this BOSS", () => {
  it("setup link: reads the token from the fragment and asks only for who you are", async () => {
    window.history.replaceState(null, "", "/claim#t=setup_abc");
    const el = await mount(<ClaimFlow options={options({ path: "setup_token", origin: null, expires_at: new Date(Date.now() + 52 * 60_000).toISOString(), seats: SEATS })} />);
    expect(el.querySelector("[data-testid='claim-no-token']")).toBeNull();
    expect(el.textContent).toMatch(/setup link · expires in 5\d min/);
    expect(el.textContent).toContain("Create a passkey and continue");
    expect(el.querySelector("#claim-seat")?.textContent).toContain("ceo · approvals and history will show your name");
    accessible(el);
  });

  it("setup link with no token says how to print a new one", async () => {
    window.history.replaceState(null, "", "/claim");
    const el = await mount(<ClaimFlow options={options({ path: "setup_token", origin: null, expires_at: null, seats: [] })} />);
    expect(el.querySelector("[data-testid='claim-no-token']")).not.toBeNull();
    expect(el.textContent).toContain("boss identity setup-link");
  });

  it("legacy password: the shared password is the proof, with where to find it", async () => {
    const el = await mount(<ClaimFlow options={options({ path: "legacy_password", origin: null, expires_at: null, seats: SEATS })} />);
    expect(el.querySelector("[data-testid='claim-legacy-password']")).not.toBeNull();
    expect(el.querySelector("label[for='claim-legacy']")?.textContent).toBe("Shared console password");
    expect(el.querySelector("[data-testid='console-password-hint']")?.textContent).toContain("console_password.py show");
    accessible(el);
  });

  it("network bridge: shows the origin it binds to and will not go on until it is confirmed", async () => {
    routes({
      "POST /v1/auth/setup/claim": { body: { next: "enrol", methods: ["passkey"], who: null } },
      "GET /v1/identity/me": { body: { csrf_token: "csrf_1" } },
      "POST /v1/identity/me/passkeys/options": { body: { stub: true, challenge: "x", rp: { name: "BOSS" }, user: { id: "dQ", name: "p", displayName: "P" }, pubKeyCredParams: [] } },
      "POST /v1/identity/me/passkeys": { status: 201, body: { ok: true } },
      "POST /v1/identity/me/recovery-codes": { body: { codes: CODES } },
    });
    const origin = "https://shared-server.tail4b2c.ts.net";
    const el = await mount(<ClaimFlow options={options({ path: "network_bridge", origin, expires_at: null, seats: SEATS })} />);
    expect(el.querySelector("[data-testid='claim-origin']")?.textContent).toContain(origin);
    type(el.querySelector("#claim-name"), "Param Gnanaguru");
    type(el.querySelector("#claim-email"), "param@kaappu.org");
    const submit = el.querySelector("[data-testid='claim-submit']") as HTMLButtonElement;
    expect(submit.disabled).toBe(true);
    await click(el.querySelector("[data-testid='claim-origin-confirm']"));
    expect(submit.disabled).toBe(false);
    await click(submit);
    const claim = calls.find((c) => c.url === "/v1/auth/setup/claim");
    expect(claim?.body).toMatchObject({ origin, name: "Param Gnanaguru", email: "param@kaappu.org", seat: "ceo" });
    expect(calls.some((c) => c.url === "/v1/identity/me/passkeys" && c.method === "POST")).toBe(true);
    expect(el.querySelector("[data-testid='recovery-codes']")?.textContent).toContain(CODES[0]);
  });

  it("no claim pending: a short panel that sends you to sign in", async () => {
    const el = await mount(<ClaimFlow options={options(null)} />);
    expect(el.querySelector("h1")?.textContent).toBe("Already claimed");
    accessible(el);
  });
});

describe("Enrolment and recovery codes", () => {
  it("recovery codes: Continue stays locked until they are stored", async () => {
    const done = vi.fn();
    const el = await mount(<RecoveryCodesView codes={CODES} host="boss.acme.dev" onContinue={done} />);
    expect(el.querySelectorAll(".idn-codes li").length).toBe(10);
    const next = el.querySelector("[data-testid='codes-continue']") as HTMLButtonElement;
    expect(next.disabled).toBe(true);
    await click(el.querySelector("[data-testid='codes-stored']"));
    expect(next.disabled).toBe(false);
    await click(next);
    expect(done).toHaveBeenCalledOnce();
  });

  it("a passkey, then the codes — shown once", async () => {
    routes({
      "GET /v1/identity/me": { body: { csrf_token: "csrf_1" } },
      "POST /v1/identity/me/passkeys/options": { body: { stub: true, challenge: "x", rp: { name: "BOSS" }, user: { id: "dQ", name: "p", displayName: "P" }, pubKeyCredParams: [] } },
      "POST /v1/identity/me/passkeys": { status: 201, body: { ok: true } },
      "POST /v1/identity/me/recovery-codes": { body: { codes: CODES } },
    });
    const el = await mount(<Enrol host="boss.acme.dev" next="/identity" who={{ name: "Sam Okafor", email: "sam@acme.dev", hue: 6 }} />);
    accessible(el);
    await click(el.querySelector("[data-testid='enrol-passkey']"));
    expect(el.textContent).toContain(CODES[9]);
    expect(calls.filter((c) => c.url === "/v1/identity/me/recovery-codes").length).toBe(1);
  });
});

describe("Invitations and email landings", () => {
  it("a used or expired invitation says so", async () => {
    const el = await mount(<InviteAccept token="expired" invitation={null} options={options(null)} />);
    expect(el.querySelector("[data-testid='invite-expired']")?.textContent).toContain("used already or it expired");
    accessible(el);
  });

  it("an open invitation needs a name and a 12-character password", async () => {
    const inv = { org: "Acme BOSS", inviter: "Priya Raman", email: "ana@acme.dev", roles: ["Operator · acme-web"], expires_at: null, sso: [] };
    const el = await mount(<InviteAccept token="inv_1" invitation={inv} options={options(null)} />);
    const go = el.querySelector("[data-testid='invite-accept']") as HTMLButtonElement;
    type(el.querySelector("#inv-name"), "Ana Costa");
    type(el.querySelector("#inv-pw"), "short");
    expect(go.disabled).toBe(true);
    type(el.querySelector("#inv-pw"), "a long enough phrase");
    expect(go.disabled).toBe(false);
    accessible(el);
  });

  it("a magic link opened without its token fails the same way every time", async () => {
    window.history.replaceState(null, "", "/signin/link");
    const el = await mount(<MagicLinkLanding host="boss.acme.dev" next="/" />);
    expect(el.querySelector("[data-testid='link-failed']")?.textContent).toContain("opened in another browser");
  });

  it("forgot password answers uniformly", async () => {
    routes({ "POST /v1/auth/password/forgot": { status: 202, body: { sent: true } } });
    const el = await mount(<Reset host="boss.acme.dev" smtp email="" />);
    accessible(el);
    type(el.querySelector("#reset-email"), "nobody@acme.dev");
    await click(el.querySelector("button[type=submit]"));
    expect(el.querySelector("[data-testid='reset-sent']")?.textContent).toContain("If an account exists for that address");
  });

  it("self sign-up off says to ask an admin", async () => {
    const el = await mount(<SignUp host="boss.acme.dev" org="Acme BOSS" enabled={false} />);
    expect(el.querySelector("[data-testid='signup-off']")).not.toBeNull();
    accessible(el);
  });
});

function perms(role: keyof typeof ROLE_PERMISSIONS): MePermissionsResponse {
  return { epoch: 1, scope: "org", permissions: [...ROLE_PERMISSIONS[role]], step_up_fresh_until: null, catalogue_fingerprint: "x" };
}
const ME: MeResponse = {
  principal_id: "usr_sam", kind: "user", display_name: "Sam Okafor", email: "sam@acme.dev", org: F.ORG, status: "active", access_roles: ["operator"],
  sign_in: { password: true, totp: true, passkeys: 1, recovery_codes_remaining: 8, sso_links: ["Okta"] }, mfa_required: false, mfa_at: null,
  session_id: "ses_1", session_expires_at: null, csrf_token: "c", epoch: 1, network_bound: false, banners: [],
};
function asOperator(node: React.ReactElement) {
  return <IdentityProvider me={ME} org={perms("operator")}><NowProvider now={NOW}>{node}</NowProvider></IdentityProvider>;
}

describe("My account", () => {
  it("a personal key's secret is shown once, and the dialog won't close until it is stored", async () => {
    const keys = F.personalKeys(NOW).usr_sam;
    const issued: IssuedSecret = { key: { ...keys[0], id: "key_n3wk3y", name: "Laptop" }, secret: "boss_pat_SECRETSECRETSECRET", replaces: null };
    routes({ "POST /v1/identity/me/api-keys": { status: 201, body: issued }, "GET /v1/identity/me/api-keys": { body: { keys: [issued.key, ...keys] } } });
    const el = await mount(asOperator(<MyKeys keys={keys} />));
    expect(el.textContent).not.toContain("boss_pat_");
    await click(el.querySelector("[data-testid='new-key']"));
    type(document.querySelector("#key-name"), "Laptop");
    await click(document.querySelector("[data-testid='create-key']"));
    const dialog = document.querySelector("[data-testid='shown-once']");
    expect(dialog?.textContent).toContain("boss_pat_SECRETSECRETSECRET");
    const done = document.querySelector("[data-testid='done']") as HTMLButtonElement;
    expect(done.disabled).toBe(true);
    await act(async () => { document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" })); });
    expect(document.querySelector("[data-testid='shown-once']")).not.toBeNull();
    await click(document.querySelector("[data-testid='stored']"));
    await click(done);
    expect(document.body.textContent).not.toContain("boss_pat_SECRETSECRETSECRET");
    expect(calls.find((c) => c.url === "/v1/identity/me/api-keys" && c.method === "POST")?.body).toMatchObject({ name: "Laptop" });
  });

  it("key presets never reach past what you hold", () => {
    const held = [...ROLE_PERMISSIONS.viewer];
    expect(presetScopes("ci", held)).toEqual(["deployment:read", "code_task:read"]);
    expect(presetScopes("read", held).every((p) => p.endsWith(":read"))).toBe(true);
  });

  it("security, sessions and agents render within the copy budget", async () => {
    const sessions = F.sessions(NOW).usr_sam;
    const el = await mount(asOperator(
      <>
        <AccountSecurity me={ME} factors={F.factors(NOW).usr_sam} identities={[]} host="boss.acme.dev" />
        <MySessions sessions={sessions} />
        <AgentsForMe delegations={F.delegations(NOW).filter((d) => d.user.id === "usr_sam")} agents={null} connections={[]} untilRevokedAllowed={false} openNew={false} />
      </>,
    ));
    expect(el.textContent).toContain("MacBook Pro · Touch ID");
    expect(el.textContent).toContain("Sign out of all others");
    expect(el.querySelector("[data-testid='grant-dlg_01JB7Q']")).not.toBeNull();
    expect(identityCopyAudit(el)).toEqual({ labels: [], prose: [], tips: [] });
  });

  it("revoking a grant shows the cascade first", async () => {
    routes({ "POST /v1/identity/me/delegations/dlg_01JB7Q/revoke": { body: { halts: 3, items: [{ kind: "runs", label: "Halts running agents", detail: "Atlas, Forge, Vera", count: 3 }] } } });
    const el = await mount(asOperator(<AgentsForMe delegations={F.delegations(NOW).filter((d) => d.user.id === "usr_sam")} agents={null} connections={[]} untilRevokedAllowed={false} openNew={false} />));
    await click(el.querySelector("[aria-label='Revoke grant dlg_01JB7Q']"));
    expect(document.querySelector("[data-testid='cascade']")?.textContent).toContain("Halts running agents");
    expect(document.querySelector("[data-testid='revoke-confirm']")?.textContent).toContain("Revoke and halt 3 runs");
    expect(calls[0].body).toEqual({ dry_run: true });
  });
});
