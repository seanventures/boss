// @vitest-environment happy-dom
/**
 * PASSWORD FIRST, PASSKEY ON REQUEST (BOSSAPP-812, operator 2026-11-04 on boss-1).
 *
 * A fresh install let the first Owner claim with a passkey only. In any other browser the sign-in
 * page started a passkey ceremony ("Insert your security key") and there was no password to fall
 * back to. The standard model (GitHub, Google) now holds:
 *
 *   - sign-in never starts a WebAuthn ceremony by itself; Password is the default tab, email focused;
 *   - "Sign in with a passkey" is a whole sign-in, started by its button; a cancel says
 *     "No passkey here? Use your password instead.";
 *   - passkeys saved here may appear in the email field's autofill (conditional mediation, no modal);
 *   - email + password → the second step: the code, or the passkey on a click, switchable, plus
 *     "Try another way" and a recovery code;
 *   - the claim asks name + email + PASSWORD first, then "Protect your account";
 *   - a passkey-only account sees "Set password" in Account › Security and a banner to add one;
 *   - an enrolment without a password (emailed sign-up) chooses one after the factor.
 */
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { SignIn } from "../app/(auth)/login/sign-in";
import { ClaimFlow } from "../app/(auth)/claim/claim";
import { Enrol } from "../app/(auth)/enrol/enrol";
import { AccountSecurity } from "../app/account/security";
import { IdentityBanners } from "../components/identity/account-menu";
import { IdentityProvider } from "../components/identity/identity-provider";
import type { AuthOptions } from "../lib/identity/types";
import type { MeResponse } from "../lib/identity/contract";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }), useSearchParams: () => new URLSearchParams(), usePathname: () => "/" }));

const PW = "violet-otter-harbour-1987";
const CODES = ["7KQ2-MX4P-9TRA", "B3ZV-8WNE-QL2D", "H6YC-2PFK-UJ7S"];
const ON: AuthOptions = {
  org_name: "Acme", host: "boss.example.com", smtp: false, signup: false, legacy_password: false, claim: null,
  passkey: true, passkey_reason: null, public_origin: "https://boss.example.com", providers: [],
};

let root: Root | null = null;
let host: HTMLElement | null = null;
let calls: { method: string; url: string; body: unknown }[] = [];
let get: ReturnType<typeof vi.fn>;
let create: ReturnType<typeof vi.fn>;

type Answer = { status?: number; body: unknown };
function routes(table: Record<string, Answer>) {
  calls = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    const method = (init?.method ?? "GET").toUpperCase();
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    calls.push({ method, url: String(url), body });
    const key = Object.keys(table).find((k) => `${method} ${url}`.startsWith(k));
    const a = key ? table[key] : { status: 404, body: { error: "not_found" } };
    return new Response(JSON.stringify(a.body), { status: a.status ?? 200, headers: { "content-type": "application/json" } });
  }));
}

function browserWithPasskeys(conditional = false) {
  const PKC = function PublicKeyCredential() { /* present */ } as unknown as { isConditionalMediationAvailable?: () => Promise<boolean> };
  if (conditional) PKC.isConditionalMediationAvailable = async () => true;
  vi.stubGlobal("PublicKeyCredential", PKC);
  get = vi.fn(() => new Promise(() => { /* the browser waits */ }));
  create = vi.fn();
  Object.defineProperty(navigator, "credentials", { value: { get, create }, configurable: true });
}

beforeEach(() => { browserWithPasskeys(); window.history.replaceState(null, "", "/"); });
afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  root = null;
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
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
  act(() => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, value);
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
}
async function click(el: Element | null) {
  if (!el) throw new Error("nothing to click");
  await act(async () => { (el as HTMLElement).click(); });
  await settle();
}
const button = (el: HTMLElement, text: string) =>
  [...el.querySelectorAll("button")].find((b) => b.textContent?.includes(text)) ?? null;

async function signInWithPassword(el: HTMLElement) {
  type(el.querySelector("#email"), "owner@example.com");
  type(el.querySelector("#password"), PW);
  await act(async () => { el.querySelector<HTMLFormElement>("form")!.requestSubmit(); });
  await settle();
}

describe("sign-in never starts a passkey by itself", () => {
  it("opens on Password with the email field focused, and asks the device nothing", async () => {
    routes({});
    const el = await mount(<SignIn options={ON} next="/" />);
    expect(el.querySelector("#tab-password")?.getAttribute("aria-selected")).toBe("true");
    expect(document.activeElement?.id).toBe("email");
    expect(el.querySelector("#password")).not.toBeNull();
    expect(el.querySelector("[data-testid='passkey-sign-in']")).not.toBeNull();
    expect(get).not.toHaveBeenCalled();
    expect(calls.some((c) => c.url.includes("/webauthn/"))).toBe(false);
    expect(el.textContent).not.toContain("Waiting for your device");
  });

  it("the passkey button is a whole sign-in; a cancel comes back calmly to the password", async () => {
    routes({ "POST /v1/auth/webauthn/options": { body: { publicKey: { challenge: "AAAA" } } } });
    get.mockImplementation(() => Promise.reject(new DOMException("The operation either timed out or was not allowed.", "NotAllowedError")));
    const el = await mount(<SignIn options={ON} next="/" />);
    await click(el.querySelector("[data-testid='passkey-sign-in']"));
    expect(get).toHaveBeenCalledTimes(1);
    expect(el.querySelector("[data-testid='no-passkey-here-line']")?.textContent).toBe("No passkey here? Use your password instead.");
    expect(el.querySelector("#password")).not.toBeNull();
    expect(el.querySelector("[role='alert']")).toBeNull();
  });

  it("a passkey saved in this browser is offered in the email autofill, with no modal", async () => {
    browserWithPasskeys(true);
    routes({ "POST /v1/auth/webauthn/options": { body: { publicKey: { challenge: "AAAA" } } } });
    const el = await mount(<SignIn options={ON} next="/" />);
    expect(get).toHaveBeenCalledTimes(1);
    expect(get.mock.calls[0][0]).toMatchObject({ mediation: "conditional" });
    expect(el.querySelector("#email")?.getAttribute("autocomplete")).toBe("username webauthn");
    expect(el.textContent).not.toContain("Waiting for your device");
    expect(el.querySelector("h1")?.textContent).toBe("Welcome back");
  });
});

describe("email + password, then the second step", () => {
  it("a passkey-only second factor is a button, pressed by the person, with another way", async () => {
    routes({ "POST /v1/auth/login": { body: { next: "mfa", methods: ["passkey", "recovery"], who: null } },
             "POST /v1/auth/webauthn/options": { body: { publicKey: { challenge: "AAAA" } } } });
    const el = await mount(<SignIn options={ON} next="/" />);
    await signInWithPassword(el);
    expect(el.querySelector("h1")?.textContent).toBe("Use your passkey");
    expect(get).not.toHaveBeenCalled();
    expect(el.querySelector("[data-testid='try-another-way']")).not.toBeNull();
    expect(button(el, "Use a recovery code")).not.toBeNull();
    await click(el.querySelector("[data-testid='mfa-passkey']"));
    expect(get).toHaveBeenCalledTimes(1);
  });

  it("an authenticator code is the default second step, with the passkey instead", async () => {
    routes({ "POST /v1/auth/login": { body: { next: "mfa", methods: ["totp", "passkey", "recovery"], who: null } } });
    const el = await mount(<SignIn options={ON} next="/" />);
    await signInWithPassword(el);
    expect(el.querySelector("h1")?.textContent).toBe("Enter your code");
    expect(button(el, "Use a passkey instead")).not.toBeNull();
    expect(get).not.toHaveBeenCalled();
  });

  it("a wrong password reveals nothing about the account's factors", async () => {
    routes({ "POST /v1/auth/login": { status: 401, body: { error: "invalid_credentials" } } });
    const el = await mount(<SignIn options={ON} next="/" />);
    await signInWithPassword(el);
    expect(el.querySelector("[role='alert']")?.textContent).toBe("Wrong email or password");
    expect(el.querySelector("[data-testid='try-another-way']")).toBeNull();
  });

  it("a sign-in the server accepts starts the session (BOSSAPP-671)", async () => {
    const assign = vi.fn();
    vi.spyOn(window.location, "assign").mockImplementation(assign);
    routes({ "POST /v1/auth/login": { body: { next: "done" } } });
    const el = await mount(<SignIn options={ON} next="/?area=setup" />);
    await signInWithPassword(el);
    expect(assign).toHaveBeenCalledWith("/?area=setup");
    expect(el.querySelector("[role='alert']")).toBeNull();
  });

  it("csrf_failed shows the cause the server named, not Reload and retry (BOSSAPP-671)", async () => {
    routes({ "POST /v1/auth/login": { status: 403, body: { error: "csrf_failed", detail: "Origin does not match this BOSS." } } });
    const el = await mount(<SignIn options={ON} next="/" />);
    await signInWithPassword(el);
    const said = el.querySelector("[role='alert']")?.textContent ?? "";
    expect(said).toBe("Origin does not match this BOSS.");
    expect(said).not.toContain("Reload and retry");
  });

  it("csrf_failed with only the code still names that refusal, not Reload and retry (BOSSAPP-671)", async () => {
    routes({ "POST /v1/auth/login": { status: 403, body: { error: "csrf_failed" } } });
    const el = await mount(<SignIn options={ON} next="/" />);
    await signInWithPassword(el);
    const said = el.querySelector("[role='alert']")?.textContent ?? "";
    expect(said).not.toContain("Reload and retry");
    expect(said).toMatch(/csrf_failed/);
  });
});

describe("claim: a password first, then protect your account", () => {
  it("the authenticator path: claim with the password, then the code, never a passkey ceremony", async () => {
    routes({
      "POST /v1/auth/setup/claim": { body: { next: "enrol", methods: [], who: null } },
      "GET /v1/identity/me": { body: { csrf_token: "c" } },
      "POST /v1/identity/me/mfa/totp": { body: { secret: "JBSWY3DPEHPK3PXP", otpauth_uri: "otpauth://totp/x", qr_svg: null } },
    });
    window.history.replaceState(null, "", "/claim#t=setup_abc");
    const el = await mount(<ClaimFlow options={{ ...ON, claim: { path: "setup_token", origin: null, expires_at: null, seats: [] } }} />);
    type(el.querySelector("#claim-name"), "Param");
    type(el.querySelector("#claim-email"), "param@example.com");
    type(el.querySelector("#claim-pw"), PW);
    await click(el.querySelector("[data-testid='claim-submit']"));
    expect(calls.find((c) => c.url === "/v1/auth/setup/claim")?.body).toMatchObject({ setup_token: "setup_abc", password: PW });
    expect(el.querySelector("h1")?.textContent).toBe("Protect your account");
    await click(el.querySelector("[data-testid='claim-factor-totp']"));
    expect(el.querySelector("[data-testid='totp-step']")).not.toBeNull();
    expect(create).not.toHaveBeenCalled();
  });

  it("Claim and add an authenticator enables once the form is filled on a secure context (BOSSAPP-672)", async () => {
    vi.stubGlobal("PublicKeyCredential", undefined);
    Object.defineProperty(navigator, "credentials", { value: undefined, configurable: true });
    window.history.replaceState(null, "", "/claim#t=setup_abc");
    const el = await mount(<ClaimFlow options={{ ...ON, passkey: false, passkey_reason: "passkey_needs_domain", claim: { path: "setup_token", origin: null, expires_at: null, seats: [] } }} />);
    const submit = el.querySelector("[data-testid='claim-submit']") as HTMLButtonElement;
    expect(submit.textContent).toContain("Claim and add an authenticator");
    expect(submit.getAttribute("aria-disabled")).toBe("true");
    type(el.querySelector("#claim-name"), "Param");
    type(el.querySelector("#claim-email"), "param@example.com");
    type(el.querySelector("#claim-pw"), PW);
    expect(submit.disabled).toBe(false);
    expect(submit.getAttribute("aria-disabled")).toBeNull();
  });

  it("an HTTPS page does not say passkeys are impossible because of plain HTTP, and a password claim still posts (BOSSAPP-662)", async () => {
    const dom = (window as unknown as { happyDOM?: { setURL: (u: string) => void } }).happyDOM;
    if (dom?.setURL) dom.setURL("https://boss.example.com/claim#t=setup_abc");
    else window.history.replaceState(null, "", "https://boss.example.com/claim#t=setup_abc");
    routes({
      "POST /v1/auth/setup/claim": { body: { next: "enrol", methods: [], who: null } },
      "GET /v1/identity/me": { body: { csrf_token: "c" } },
      "POST /v1/identity/me/passkeys/options": { status: 409, body: { error: "passkey_needs_https" } },
    });
    const el = await mount(<ClaimFlow options={{ ...ON, passkey_reason: "passkey_needs_https", claim: { path: "setup_token", origin: null, expires_at: null, seats: [] } }} />);
    expect(el.textContent ?? "").not.toMatch(/plain HTTP/);
    expect(el.querySelector("#claim-pw")).not.toBeNull();
    type(el.querySelector("#claim-name"), "Param");
    type(el.querySelector("#claim-email"), "param@example.com");
    type(el.querySelector("#claim-pw"), PW);
    await click(el.querySelector("[data-testid='claim-submit']"));
    expect(calls.find((c) => c.url === "/v1/auth/setup/claim")?.body).toMatchObject({ setup_token: "setup_abc", password: PW, name: "Param" });
    // The password claim stands. A passkey the server still refuses leaves an authenticator.
    const passkey = el.querySelector("[data-testid='claim-factor-passkey']");
    if (passkey) {
      await click(passkey);
      expect(el.textContent ?? "").not.toMatch(/plain HTTP/);
      const alt = [...el.querySelectorAll("button")].find((b) => b.textContent?.includes("Use an authenticator app") || b.textContent?.includes("Authenticator app"));
      expect(alt).toBeTruthy();
      await click(alt ?? null);
      expect(el.querySelector("[data-testid='totp-step']")).not.toBeNull();
    } else {
      expect(el.querySelector("[data-testid='totp-step']")).not.toBeNull();
      expect(el.textContent ?? "").not.toMatch(/plain HTTP/);
    }
  });
});

describe("a passkey-only account is never locked out, and is asked for a password", () => {
  const me = (password: boolean): MeResponse => ({
    principal_id: "usr_1", kind: "user", display_name: "Owner", email: "owner@example.com", org: { id: "o", name: "Acme" },
    status: "active", access_roles: ["owner"], sign_in: { password, totp: false, passkeys: 1, recovery_codes_remaining: 10, sso_links: [] },
    mfa_required: true, mfa_at: null, session_id: "s", session_expires_at: null, csrf_token: "c", epoch: 1, network_bound: false,
    banners: [], avatar_seed: null,
  });

  it("Account › Security says Set password and needs no current one", async () => {
    routes({});
    const el = await mount(<AccountSecurity me={me(false)} factors={[]} identities={[]} host="boss.example.com" />);
    expect(el.querySelector("[data-testid='pw-submit']")?.textContent).toBe("Set password");
    expect(el.querySelector("#pw-current")).toBeNull();
  });

  it("with a password it is Change password", async () => {
    routes({});
    const el = await mount(<AccountSecurity me={me(true)} factors={[]} identities={[]} host="boss.example.com" />);
    expect(el.querySelector("[data-testid='pw-submit']")?.textContent).toBe("Change password");
    expect(el.querySelector("#pw-current")).not.toBeNull();
  });

  it("the banner links to the password card", async () => {
    routes({});
    const withBanner = { ...me(false), banners: [{ id: "no_password", kind: "no_password" as const, policy: "dismissible" as const, message: "Add a password so you can sign in from any browser.", until: null }] };
    const perms = { epoch: 1, scope: "org", permissions: [], step_up_fresh_until: null, catalogue_fingerprint: "x" };
    const el = await mount(<IdentityProvider me={withBanner} org={perms}><IdentityBanners /></IdentityProvider>);
    expect(el.textContent).toContain("Add a password so you can sign in from any browser.");
    expect(el.querySelector("[data-testid='banner-set-password']")?.getAttribute("href")).toBe("/account#acc-pw-h");
  });
});

describe("an enrolment without a password chooses one after the factor", () => {
  it("passkey, codes, then Choose a password", async () => {
    routes({
      "GET /v1/identity/me": { body: { csrf_token: "c", sign_in: { password: false } } },
      "POST /v1/identity/me/passkeys/options": { body: { stub: true, challenge: "x", rp: { name: "BOSS" }, user: { id: "dQ", name: "p", displayName: "P" }, pubKeyCredParams: [] } },
      "POST /v1/identity/me/passkeys": { status: 201, body: { recovery_codes: CODES } },
      "POST /v1/identity/me/password": { body: { changed: true } },
    });
    const assign = vi.fn();
    vi.stubGlobal("location", { ...window.location, assign, hostname: "boss.example.com", protocol: "https:", origin: "https://boss.example.com", pathname: "/enrol" });
    const el = await mount(<Enrol host="boss.example.com" next="/" who={null} />);
    expect(el.querySelector("h1")?.textContent).toBe("Protect your account");
    await click(el.querySelector("[data-testid='enrol-passkey']"));
    expect(el.textContent).toContain(CODES[0]);
    await click(el.querySelector("[data-testid='codes-stored']"));
    await click(el.querySelector("[data-testid='codes-continue']"));
    expect(el.querySelector("h1")?.textContent).toBe("Choose a password");
    type(el.querySelector("#enrol-pw"), PW);
    await click(el.querySelector("[data-testid='enrol-password']"));
    expect(calls.find((c) => c.url === "/v1/identity/me/password")?.body).toEqual({ new: PW });
    expect(assign).toHaveBeenCalledWith("/");
  });
});
