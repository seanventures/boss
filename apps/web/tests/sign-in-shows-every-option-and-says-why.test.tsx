// @vitest-environment happy-dom
/**
 * THE SIGN-IN PAGE HAS ITS WHOLE SHAPE, AND NO FAKE BUTTONS (operator, round 3 redesign).
 *
 * Every option remains available on the page: the four social providers, a passkey button, the three
 * email methods, "Forgot?" and "Create account". What /v1/auth/options says isn't set up is
 * disabled, and its tooltip (and, on a tap, a line of words) says exactly what turns it on.
 * Nothing disabled ever navigates or posts. No phone, no SMS.
 */
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import { SignIn } from "../app/(auth)/login/sign-in";
import type { AuthOptions } from "../lib/identity/types";
import { methodWhyNot, passkeyWhyNot, providerOffers } from "../lib/identity/sign-in-offers";
import { PASSKEY_NEEDS_DOMAIN_WORDS } from "../lib/identity/passkey-support";
import { identityCopyAudit } from "./_helpers/identity-copy";
import * as webauthn from "../lib/identity/webauthn";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }), useSearchParams: () => new URLSearchParams(), usePathname: () => "/" }));

let root: Root | null = null;
let host: HTMLElement | null = null;
const calls: string[] = [];

afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  root = null;
  calls.length = 0;
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

/** All off: an IP install, no SMTP, no providers, sign-up off. */
const OFF: AuthOptions = {
  org_name: "BOSS", host: "100.93.58.20", providers: [], smtp: false, signup: false, legacy_password: false, claim: null,
  passkey: false, passkey_reason: "passkey_needs_domain", public_origin: "https://100.93.58.20",
};
/** All on: providers, SMTP, sign-up, a passkey on a domain. */
const ON: AuthOptions = {
  org_name: "Acme", host: "boss.example.com", smtp: true, signup: true, legacy_password: false, claim: null,
  passkey: true, passkey_reason: null, public_origin: "https://boss.example.com",
  providers: [
    { id: "g", name: "Google", preset: "google" }, { id: "gh", name: "GitHub", preset: "github" },
    { id: "ms", name: "Microsoft", preset: "microsoft" }, { id: "ap", name: "Apple", preset: "apple" },
    { id: "okta", name: "Acme SSO", preset: "okta" },
  ],
};

async function mount(options: AuthOptions) {
  vi.stubGlobal("fetch", vi.fn(async (url: string) => {
    calls.push(String(url));
    return new Response(JSON.stringify({ error: "invalid_credentials" }), { status: 401 });
  }));
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => root!.render(<SignIn options={options} next="/projects" />));
  await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
  return host;
}

const tipOf = (el: Element, target: Element | null) => {
  const id = target?.getAttribute("aria-describedby");
  return id ? el.ownerDocument.getElementById(id)?.textContent ?? null : null;
};
const click = (b: Element | null) => act(async () => { (b as HTMLElement).click(); });

describe("all off: the page keeps its shape and says what turns each thing on", () => {
  it("keeps the password form outside a closed catalog of unavailable external methods", async () => {
    const el = await mount(OFF);
    const catalog = el.querySelector<HTMLDetailsElement>("[data-testid='other-signin-options']")!;
    expect(catalog.open).toBe(false);
    expect(catalog.querySelector("summary")?.textContent).toBe("Other sign-in options");
    expect(catalog.querySelectorAll(".idn-idp").length).toBe(4);
    expect(catalog.querySelector("[data-testid='passkey-sign-in']")?.getAttribute("aria-disabled")).toBe("true");
    expect(catalog.contains(el.querySelector("#email"))).toBe(false);
    expect(catalog.contains(el.querySelector("#password"))).toBe(false);
    expect(catalog.contains(el.querySelector("[data-testid='email-submit']"))).toBe(false);
    expect(el.querySelectorAll("[role='tab']").length).toBe(3);
    expect(calls).toEqual([]);
  });

  it("an unclaimed install says how to claim it without the logs: the host's setup-link command", async () => {
    await mount({ ...OFF, claim: { path: "setup_token", seats: [], expires_at: null } as unknown as AuthOptions["claim"] });
    const how = document.querySelector("[data-testid='claim-how']")!;
    expect(how.querySelector("[data-testid='claim-door']")!.getAttribute("href")).toBe("/claim");
    expect(how.textContent).toContain("boss identity setup-link");
  });

  it("every provider square is there, disabled, naming where an Owner turns it on", async () => {
    const el = await mount(OFF);
    const squares = [...el.querySelectorAll(".idn-idp")];
    expect(squares.map((s) => s.getAttribute("aria-label"))).toEqual(["Google, not set up", "GitHub, not set up", "Microsoft, not set up", "Apple, not set up"]);
    for (const s of squares) {
      expect(s.tagName).toBe("BUTTON");
      expect(s.getAttribute("aria-disabled")).toBe("true");
    }
    expect(tipOf(el, squares[0])).toBe("Google sign-in isn't set up · an Owner turns it on in Identity › Sign-in & SSO");
    await click(squares[1]);
    expect(el.querySelector("[data-testid='why-idp']")?.textContent).toBe("GitHub sign-in isn't set up · an Owner turns it on in Identity › Sign-in & SSO");
    expect(calls).toEqual([]);
  });

  it("the passkey button says an IP address can't hold one, and asks nothing of the API", async () => {
    const el = await mount(OFF);
    const pk = el.querySelector("[data-testid='passkey-sign-in']");
    expect(pk?.getAttribute("aria-disabled")).toBe("true");
    expect(tipOf(el, pk)).toBe(PASSKEY_NEEDS_DOMAIN_WORDS);
    await click(pk);
    expect(el.querySelector("[data-testid='why-passkey']")?.textContent).toBe(PASSKEY_NEEDS_DOMAIN_WORDS);
    expect(calls).toEqual([]);
  });

  it("magic link and email code stay visible, disabled, and need SMTP; password stays chosen", async () => {
    const el = await mount(OFF);
    const tabs = [...el.querySelectorAll("[role='tab']")];
    expect(tabs.map((t) => t.textContent)).toEqual(["Password", "Magic link", "Email code"]);
    expect(tipOf(el, tabs[1])).toBe("Magic links need email (SMTP) set up");
    expect(tipOf(el, tabs[2])).toBe("Email codes need email (SMTP) set up");
    await click(tabs[1]);
    expect(el.querySelector("[role='tab'][aria-selected='true']")?.textContent).toBe("Password");
    expect(el.querySelector("[data-testid='why-tabs']")?.textContent).toBe("Magic links need email (SMTP) set up");
    // The arrows reach a method that's off (so its reason shows) but never choose it.
    await act(async () => { tabs[0].dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true })); });
    expect(el.querySelector("[role='tab'][aria-selected='true']")?.textContent).toBe("Password");
  });

  it("Forgot? opens a sheet: ask an admin, or the host's recover-owner command", async () => {
    const el = await mount(OFF);
    const forgot = el.querySelector("[data-testid='forgot']");
    expect(forgot?.tagName).toBe("BUTTON");
    expect(forgot?.textContent).toBe("Forgot?");
    await click(forgot);
    const sheet = document.querySelector("[data-testid='forgot-sheet']")!;
    expect(sheet.getAttribute("role")).toBe("dialog");
    expect(sheet.textContent).toContain("Ask an admin to send you a reset link.");
    expect(sheet.textContent).toContain("boss identity recover-owner --email you@example.com");
    expect(identityCopyAudit(sheet)).toEqual({ labels: [], prose: [], tips: [] });
  });

  it("Create account stays, and says this BOSS is invite-only (no admin names: the API doesn't publish them)", async () => {
    const el = await mount(OFF);
    const create = el.querySelector("[data-testid='create-account']");
    expect(create?.tagName).toBe("BUTTON");
    await click(create);
    const sheet = document.querySelector("[data-testid='invite-only-sheet']")!;
    expect(sheet.textContent).toContain("This BOSS is invite-only");
    expect(sheet.textContent).toContain("Ask an admin to invite you.");
    expect(calls).toEqual([]);
  });

  it("the host is at the foot; nothing says phone or SMS; the copy fits its budget", async () => {
    const el = await mount(OFF);
    expect(el.querySelector(".idn-hostline")?.textContent).toContain("100.93.58.20");
    expect(el.textContent).not.toMatch(/phone|SMS/i);
    expect(identityCopyAudit(el)).toEqual({ labels: [], prose: [], tips: [] });
  });
});

describe("all on: every option is live", () => {
  it("keeps even one configured provider prominent without hiding the unavailable setup reasons", async () => {
    const el = await mount({ ...OFF, providers: [ON.providers[0]] });
    expect(el.querySelector("[data-testid='other-signin-options']")).toBeNull();
    expect(el.querySelector("a.idn-idp")?.getAttribute("href")).toBe("/v1/auth/sso/g/start?next=%2Fprojects");
    expect(el.querySelectorAll("button.idn-idp[aria-disabled='true']").length).toBe(3);
  });

  it("keeps a usable passkey prominent when no social provider is configured", async () => {
    vi.spyOn(webauthn, "webauthnAvailable").mockReturnValue(true);
    const el = await mount({ ...ON, providers: [] });
    expect(el.querySelector("[data-testid='other-signin-options']")).toBeNull();
    expect(el.querySelector("[data-testid='passkey-sign-in']")?.getAttribute("aria-disabled")).toBeNull();
    expect(calls).toEqual([]);
  });

  it("providers link to their single sign-on start, carrying where you were going", async () => {
    const el = await mount(ON);
    const links = [...el.querySelectorAll<HTMLAnchorElement>("a.idn-idp")];
    expect(links.map((a) => a.getAttribute("aria-label"))).toEqual([
      "Continue with Google", "Continue with GitHub", "Continue with Microsoft", "Continue with Apple", "Continue with Acme SSO"]);
    expect(links[0].getAttribute("href")).toBe("/v1/auth/sso/g/start?next=%2Fprojects");
    expect(el.querySelector("button.idn-idp")).toBeNull();
  });

  it("the email methods are live, Forgot? is the email reset, Create account is the sign-up page", async () => {
    const el = await mount(ON);
    expect(el.querySelectorAll("[role='tab'][aria-disabled='true']").length).toBe(0);
    expect(el.querySelector("a[data-testid='forgot']")?.getAttribute("href")).toBe("/reset");
    expect(el.querySelector("a[data-testid='create-account']")?.getAttribute("href")).toBe("/signup");
    await click(el.querySelectorAll("[role='tab']")[2]);
    expect(el.querySelector("[data-testid='email-submit']")?.textContent).toBe("Email me a code");
    expect(el.querySelector("#password")).toBeNull();
  });

  it("the eye shows and hides the password", async () => {
    const el = await mount(ON);
    const input = el.querySelector<HTMLInputElement>("#password")!;
    const eye = el.querySelector<HTMLButtonElement>(".idn-eye")!;
    expect(input.type).toBe("password");
    expect(eye.getAttribute("aria-label")).toBe("Show password");
    await click(eye);
    expect(input.type).toBe("text");
    expect(eye.getAttribute("aria-pressed")).toBe("true");
    expect(eye.getAttribute("aria-label")).toBe("Hide password");
  });

  it("the shared password link shows only while it still works", async () => {
    const live = await mount({ ...ON, shared_password: { until: new Date(Date.now() + 86_400_000).toISOString() } });
    expect(live.querySelector("[data-testid='shared-password-toggle']")?.textContent).toBe("Use the shared password");
    act(() => root?.unmount());
    host?.remove();
    const gone = await mount({ ...ON, shared_password: { until: new Date(Date.now() - 1000).toISOString() } });
    expect(gone.querySelector("[data-testid='shared-password-toggle']")).toBeNull();
  });
});

describe("the offers, as rules", () => {
  it("a second provider of a social preset, and every enterprise one, follow the four squares", () => {
    const offers = providerOffers([{ id: "s", name: "Corp SAML", preset: "generic" }, { id: "g1", name: "Google", preset: "google" }, { id: "g2", name: "Google (partners)", preset: "google" }], "/");
    expect(offers.map((o) => o.key)).toEqual(["g1", "off-github", "off-microsoft", "off-apple", "s", "g2"]);
  });

  it("passkeys: the API's reason first, then the page's own address, then the browser", () => {
    expect(passkeyWhyNot({ passkey_reason: "passkey_needs_https" }, null, true)).toMatch(/HTTPS/);
    expect(passkeyWhyNot({}, { hostname: "10.0.0.5", protocol: "https:" }, true)).toBe(PASSKEY_NEEDS_DOMAIN_WORDS);
    expect(passkeyWhyNot({ passkey_reason: "public_origin_unset" }, null, true)).toMatch(/public address/);
    expect(passkeyWhyNot({}, { hostname: "boss.example.com", protocol: "https:" }, false)).toBe("This browser has no passkeys");
    expect(passkeyWhyNot({ passkey: true }, { hostname: "boss.example.com", protocol: "https:" }, true)).toBeNull();
  });

  it("email methods need SMTP, and a public address to link to", () => {
    expect(methodWhyNot("password", { smtp: false })).toBeNull();
    expect(methodWhyNot("magic", { smtp: true, public_origin: "https://b.example.com" })).toBeNull();
    expect(methodWhyNot("code", { smtp: true, public_origin: null })).toBe("Email codes need BOSS's public address set");
  });
});
