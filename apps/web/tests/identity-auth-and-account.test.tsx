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
import { InviteAccept, InviteLanding } from "../app/(auth)/invite/invite";
import { SignIn } from "../app/(auth)/login/sign-in";
import { ssoErrorWords } from "../app/(auth)/auth-flow";
import { idPost, setCsrfToken } from "../lib/identity/api";
import { toSessionRow } from "../lib/identity/adapt-session";
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
  it("setup link: reads the token from the fragment and asks for who you are and a password (812)", async () => {
    window.history.replaceState(null, "", "/claim#t=setup_abc");
    const el = await mount(<ClaimFlow options={options({ path: "setup_token", origin: null, expires_at: new Date(Date.now() + 52 * 60_000).toISOString(), seats: SEATS })} />);
    expect(el.querySelector("[data-testid='claim-no-token']")).toBeNull();
    expect(el.textContent).toMatch(/setup link · expires in 5\d min/);
    // BOSSAPP-812: a password first, always (the way in from any browser); no passkey yet.
    expect(el.textContent).toContain("Claim and continue");
    expect(el.textContent).not.toContain("Create a passkey");
    const pw = el.querySelector("#claim-pw") as HTMLInputElement;
    expect(pw.type).toBe("password");
    await click(el.querySelector("[data-testid='claim-pw-eye']"));
    expect((el.querySelector("#claim-pw") as HTMLInputElement).type).toBe("text");
    type(el.querySelector("#claim-name"), "Param");
    type(el.querySelector("#claim-email"), "param@example.com");
    // Not ready: the button says why on click instead of sitting silently disabled (2026-11-05).
    type(el.querySelector("#claim-pw"), "short-pw");
    expect(el.querySelector("[data-testid='claim-pw-hint']")?.className).toContain("idn-err");
    expect(el.querySelector("[data-testid='claim-pw-hint']")?.textContent).toMatch(/\d+ more characters needed/);
    expect(el.querySelector("[data-testid='claim-submit']")?.getAttribute("aria-disabled")).toBe("true");
    await click(el.querySelector("[data-testid='claim-submit']"));
    expect(el.querySelector("[data-testid='claim-missing']")?.textContent).toMatch(/Your password needs \d+ more characters/);
    type(el.querySelector("#claim-pw"), "violet-otter-harbour-1987");
    expect(el.querySelector("[data-testid='claim-submit']")?.getAttribute("aria-disabled")).toBeNull();
    expect(navigator.credentials.create).not.toHaveBeenCalled();
    expect(el.querySelector("#claim-seat")?.textContent).toContain("ceo. Approvals and history will show your name");
    accessible(el);
  });

  it("a fresh install never asks for the shared console password: no link → get the link (2026-11-05)", async () => {
    window.history.replaceState(null, "", "/");
    const el = await mount(<ClaimFlow options={options({ path: "legacy_password", paths: ["setup_token", "legacy_password"], origin: null, expires_at: null, seats: SEATS })} />);
    expect(el.querySelector("[data-testid='claim-legacy-password']")).toBeNull();
    expect(el.querySelector("[data-testid='claim-no-token']")).not.toBeNull();
    expect(el.querySelector("form")).toBeNull();
    expect(el.textContent).toContain("boss identity setup-link");
  });

  it("setup link with no token says how to print a new one", async () => {
    window.history.replaceState(null, "", "/claim");
    const el = await mount(<ClaimFlow options={options({ path: "setup_token", origin: null, expires_at: null, seats: [] })} />);
    expect(el.querySelector("[data-testid='claim-no-token']")).not.toBeNull();
    expect(el.textContent).toContain("boss identity setup-link");
  });

  it("setup link with no token, where the admin key can issue one over the API, says so too", async () => {
    window.history.replaceState(null, "", "/claim");
    const el = await mount(<ClaimFlow options={options({ path: "setup_token", origin: null, expires_at: null, seats: [], link_over_api: true })} />);
    const note = el.querySelector("[data-testid='claim-no-token']")?.textContent ?? "";
    expect(note).toContain("whoever holds this install's admin key");
    expect(note).toContain("server where BOSS is installed");
    expect(el.textContent).toContain("boss identity setup-link");
    accessible(el);
  });

  it("legacy password: the shared password is the proof, with where to find it", async () => {
    const el = await mount(<ClaimFlow options={options({ path: "legacy_password", origin: null, expires_at: null, seats: SEATS })} />);
    expect(el.querySelector("[data-testid='claim-legacy-password']")).not.toBeNull();
    expect(el.querySelector("label[for='claim-legacy']")?.textContent).toBe("Shared console password");
    expect(el.querySelector("[data-testid='console-password-hint']")?.textContent).toContain("boss password");
    accessible(el);
  });

  it("a setup link on a shared-password install is the proof: no shared password asked, the token is sent (J2)", async () => {
    routes({ "POST /v1/auth/setup/claim": { body: { next: "enrol", methods: ["passkey"], who: null } } });
    window.history.replaceState(null, "", "/claim#t=setup_abc");
    const el = await mount(<ClaimFlow options={options({ path: "legacy_password", paths: ["setup_token", "legacy_password"], origin: null, expires_at: null, seats: [] })} />);
    expect(el.querySelector("[data-testid='claim-legacy-password']")).toBeNull();
    expect(el.querySelector("[data-testid='console-password-hint']")).toBeNull();
    type(el.querySelector("#claim-name"), "Param Gnanaguru");
    type(el.querySelector("#claim-email"), "param@example.com");
    type(el.querySelector("#claim-pw"), "violet-otter-harbour-1987");
    await click(el.querySelector("[data-testid='claim-submit']"));
    const claim = calls.find((c) => c.url === "/v1/auth/setup/claim");
    expect(claim?.body).toMatchObject({ setup_token: "setup_abc", password: "violet-otter-harbour-1987" });
    expect(claim?.body).not.toHaveProperty("legacy_password");
    window.history.replaceState(null, "", "/claim");
  });

  it("network bridge with no origin from the API binds the page's own (J14)", async () => {
    window.history.replaceState(null, "", "/claim");
    const el = await mount(<ClaimFlow options={options({ path: "network_bridge", paths: ["setup_token", "network"], origin: null, expires_at: null, seats: [] })} />);
    expect(el.querySelector("[data-testid='claim-origin'] code")?.textContent).toBe(window.location.origin);
  });

  it("network bridge: shows the origin it binds to and will not go on until it is confirmed", async () => {
    routes({
      "POST /v1/auth/setup/claim": { body: { next: "enrol", methods: ["passkey"], who: null } },
      "GET /v1/identity/me": { body: { csrf_token: "csrf_1" } },
      "POST /v1/identity/me/passkeys/options": { body: { stub: true, challenge: "x", rp: { name: "BOSS" }, user: { id: "dQ", name: "p", displayName: "P" }, pubKeyCredParams: [] } },
      "POST /v1/identity/me/passkeys": { status: 201, body: { ok: true } },
      "POST /v1/identity/me/recovery-codes": { body: { codes: CODES } },
    });
    const origin = "https://console.example.test";
    const el = await mount(<ClaimFlow options={options({ path: "network_bridge", origin, expires_at: null, seats: SEATS })} />);
    expect(el.querySelector("[data-testid='claim-origin']")?.textContent).toContain(origin);
    type(el.querySelector("#claim-name"), "Param Gnanaguru");
    type(el.querySelector("#claim-email"), "param@example.com");
    type(el.querySelector("#claim-pw"), "violet-otter-harbour-1987");
    const submit = el.querySelector("[data-testid='claim-submit']") as HTMLButtonElement;
    expect(submit.getAttribute("aria-disabled")).toBe("true");
    await click(el.querySelector("[data-testid='claim-origin-confirm']"));
    expect(submit.getAttribute("aria-disabled")).toBeNull();
    await click(submit);
    const claim = calls.find((c) => c.url === "/v1/auth/setup/claim");
    expect(claim?.body).toMatchObject({ confirm_origin: origin, name: "Param Gnanaguru", email: "param@example.com", seat: "ceo", password: "violet-otter-harbour-1987" });
    // Step 2 "Protect your account": the passkey ceremony starts only on its button (812).
    expect(el.querySelector("h1")?.textContent).toBe("Protect your account");
    expect(calls.some((c) => c.url === "/v1/identity/me/passkeys/options")).toBe(false);
    await click(el.querySelector("[data-testid='claim-factor-passkey']"));
    expect(calls.some((c) => c.url === "/v1/identity/me/passkeys" && c.method === "POST")).toBe(true);
    // A passkey lives in one browser: an authenticator is offered too; skipping goes to the codes.
    expect(el.querySelector("h1")?.textContent).toBe("Add an authenticator too?");
    await click(el.querySelector("[data-testid='claim-skip-totp']"));
    expect(el.querySelector("[data-testid='recovery-codes']")?.textContent).toContain(CODES[0]);
  });

  it("private network (BOSSAPP-813): the claim form is the page — no link, no terminal, no shared password", async () => {
    routes({
      "POST /v1/auth/setup/claim": { body: { next: "enrol", methods: ["passkey"], who: null } },
      "GET /v1/identity/me": { body: { csrf_token: "csrf_1" } },
    });
    window.history.replaceState(null, "", "/claim");
    const origin = "https://boss-1.tail18604a.ts.net";
    const el = await mount(<ClaimFlow options={options({ path: "network_bridge", paths: ["setup_token", "legacy_password", "network"], network: "private_network", origin, expires_at: null, seats: SEATS })} />);
    expect(el.querySelector("[data-testid='claim-no-token']")).toBeNull();
    expect(el.querySelector("[data-testid='claim-legacy-password']")).toBeNull();
    expect(el.textContent).not.toContain("boss identity setup-link");
    expect(el.textContent).toContain("private network");
    expect(el.querySelector("[data-testid='claim-origin'] code")?.textContent).toBe(origin);
    type(el.querySelector("#claim-name"), "Param");
    type(el.querySelector("#claim-email"), "param@example.com");
    type(el.querySelector("#claim-pw"), "violet-otter-harbour-1987");
    await click(el.querySelector("[data-testid='claim-origin-confirm']"));
    await click(el.querySelector("[data-testid='claim-submit']"));
    const claim = calls.find((c) => c.url === "/v1/auth/setup/claim");
    expect(claim?.body).toMatchObject({ confirm_origin: origin, password: "violet-otter-harbour-1987" });
    expect(claim?.body).not.toHaveProperty("setup_token");
    expect(claim?.body).not.toHaveProperty("legacy_password");
    // Then "Protect your account" (812), with both factors offered.
    expect(el.querySelector("h1")?.textContent).toBe("Protect your account");
    expect(el.querySelector("[data-testid='claim-factor-passkey']")).not.toBeNull();
    expect(el.querySelector("[data-testid='claim-factor-totp']")).not.toBeNull();
  });

  it("a setup link the page holds is still the proof on a private network", async () => {
    window.history.replaceState(null, "", "/claim#t=setup_abc");
    const el = await mount(<ClaimFlow options={options({ path: "network_bridge", paths: ["setup_token", "network"], network: "private_network", origin: "https://boss.acme.dev", expires_at: null, seats: [] })} />);
    expect(el.querySelector("[data-testid='claim-origin']")).toBeNull();
    expect(el.querySelector("[data-testid='claim-no-token']")).toBeNull();
  });

  it("public address (BOSSAPP-813): the setup-link step says it is a terminal command for the server, not an address", async () => {
    window.history.replaceState(null, "", "/claim");
    const el = await mount(<ClaimFlow options={options({ path: "setup_token", paths: ["setup_token"], network: null, origin: null, expires_at: null, seats: [] })} />);
    const step = el.querySelector("[data-testid='claim-no-token']")?.textContent ?? "";
    expect(el.querySelector("form")).toBeNull();
    expect(step).toContain("in a terminal on the server where BOSS is installed");
    expect(step).toContain("over SSH");
    expect(step).toContain("not a web address");
    expect(step).toContain("It prints a link");
    expect(el.textContent).toContain("boss identity setup-link");
    accessible(el);
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

describe("An authenticator key that expired is replaced, never “That code didn't work” forever (hotfix, boss-1)", () => {
  it("confirm answered 409 enrolment_expired: a NEW key is shown with the reason, and its code enrols", async () => {
    let begins = 0;
    routes({
      "GET /v1/identity/me": { body: { csrf_token: "c" } },
      "POST /v1/identity/me/mfa/totp/confirm": (body) => (body as { code: string }).code === "222222"
        ? { body: { enrolled: true, recovery_codes: CODES } }
        : { status: 409, body: { error: "enrolment_expired" } },
      "POST /v1/identity/me/mfa/totp": () => { begins += 1; return { body: { secret: `KEYNUMBER${begins}`, otpauth_uri: "otpauth://totp/x", qr_svg: null } }; },
    });
    const el = await mount(<Enrol host="100.64.0.20" next="/identity" who={null} passkeyReason="passkey_needs_domain" />);
    expect(el.textContent).toContain("KEYNUMBER1");
    type(el.querySelector("input[aria-label='Digit 1 of 6']"), "111111");
    await settle();
    expect(begins).toBe(2);
    expect(el.textContent).toContain("KEYNUMBER2");
    expect(el.textContent).not.toContain("KEYNUMBER1");
    expect(el.querySelector("[role='alert']")?.textContent).toContain("That key expired");
    type(el.querySelector("input[aria-label='Digit 1 of 6']"), "222222");
    await settle();
    expect(el.textContent).toContain(CODES[9]);
  });
});

describe("The real API's shapes (S1)", () => {
  it("a first passkey shows the recovery codes the API issued with it, and asks for no new set", async () => {
    routes({
      "GET /v1/identity/me": { body: { csrf_token: "csrf_1" } },
      // S1 wraps the options as navigator.credentials takes them.
      "POST /v1/identity/me/passkeys/options": { body: { publicKey: { stub: true, challenge: "x", rp: { name: "BOSS" }, user: { id: "dQ", name: "p", displayName: "P" }, pubKeyCredParams: [] } } },
      "POST /v1/identity/me/passkeys": { status: 201, body: { id: "pk_1", recovery_codes: CODES } },
      "POST /v1/identity/me/recovery-codes": { body: { recovery_codes: ["SHOULD-NOT-BE-ASKED"] } },
    });
    const el = await mount(<Enrol host="boss.acme.dev" next="/identity" who={null} />);
    await click(el.querySelector("[data-testid='enrol-passkey']"));
    expect(el.textContent).toContain(CODES[9]);
    expect(calls.some((c) => c.url === "/v1/identity/me/recovery-codes")).toBe(false);
  });

  it("a write refused for a rotated session's CSRF token re-reads the token once and retries", async () => {
    setCsrfToken("csrf_old");
    let tries = 0;
    routes({
      "POST /v1/identity/me/recovery-codes": () => (++tries === 1 ? { status: 403, body: { error: "csrf_failed" } } : { body: { recovery_codes: CODES } }),
      "GET /v1/identity/me": { body: { csrf_token: "csrf_new" } },
    });
    const sent: (string | null)[] = [];
    const inner = globalThis.fetch as unknown as (u: string, i?: RequestInit) => Promise<Response>;
    vi.stubGlobal("fetch", (u: string, i?: RequestInit) => { if (i?.method === "POST") sent.push(new Headers(i.headers).get("x-boss-csrf")); return inner(u, i); });
    const r = await idPost<{ recovery_codes: string[] }>("/v1/identity/me/recovery-codes", {});
    expect(r.recovery_codes).toEqual(CODES);
    expect(sent).toEqual(["csrf_old", "csrf_new"]);
  });

  it("single sign-on coming back with ?step=mfa opens the code screen; ?error= says why in words", async () => {
    const el = await mount(<SignIn options={options(null)} next="/" initialStep={{ next: "mfa", methods: ["totp", "passkey", "recovery"], who: null }} />);
    expect(el.querySelector("h1")?.textContent).toBe("Enter your code");
    act(() => root?.unmount());
    const el2 = await mount(<SignIn options={options(null)} next="/" notice={ssoErrorWords("not_invited")} />);
    expect(el2.querySelector("[role='alert'], .idn-err")?.textContent).toContain("Ask an admin to invite you");
    expect(ssoErrorWords("saml_signature_invalid")).toContain("SAML");
    expect(ssoErrorWords("weird_code")).toContain("didn't finish");
    expect(ssoErrorWords(null)).toBeNull();
  });

  it("an API session row reads as a device, a client and when it started", () => {
    const row = toSessionRow({ id: "ses_1", current: true, methods: ["pwd", "sso:idp_8f2"], created_at: "2026-10-06T10:00:00Z", last_seen_at: "2026-10-06T10:30:00Z",
      ip: "10.0.0.2", user_agent: "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1" });
    expect(row).toMatchObject({ device: "phone", client: "Safari on iPhone", started_at: "2026-10-06T10:00:00Z", location: null });
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
    // Disabled, and SAYS WHY — before anything is typed, the name is the first thing missing.
    expect(go.disabled).toBe(true);
    expect(go.title).toBe("Your name is needed first");
    type(el.querySelector("#inv-name"), "Ana Costa");
    type(el.querySelector("#inv-pw"), "short");
    expect(go.disabled).toBe(true);
    // THE HINT COUNTS DOWN LIVE: "short" is 5 of 12, so 7 are still needed — never a static
    // "12 characters or more" that never answers how much further there is to go.
    expect(el.querySelector("[data-testid='invite-pw-hint']")?.textContent).toBe("7 more characters needed");
    expect(go.title).toBe("7 more characters needed");
    type(el.querySelector("#inv-pw"), "a long enough phrase");
    expect(go.disabled).toBe(false);
    expect(el.querySelector("[data-testid='invite-pw-hint']")?.textContent).toBe("Long enough");
    expect(go.title ?? "").toBe("");
    accessible(el);
  });

  it("the invitation token is read from the fragment and only ever sent in a POST body", async () => {
    routes({
      "POST /v1/auth/invitations/preview": (b) => ((b as { token?: string }).token === "tok_secret"
        ? { body: { org: "Acme BOSS", inviter: "Priya Raman", email: "ana@acme.dev", roles: ["Operator"], expires_at: null, sso: [] } }
        : { status: 404, body: { error: "not_found" } }),
      "POST /v1/auth/invitations/accept": { body: { next: "enrol", methods: ["passkey", "totp"], who: null } },
    });
    window.history.replaceState(null, "", "/invite#t=tok_secret");
    const el = await mount(<InviteLanding options={options(null)} />);
    expect(el.querySelector("h1")?.textContent).toBe("Join Acme BOSS");
    // The fragment is gone from the address bar, and no URL the console fetched carries it.
    expect(window.location.hash).toBe("");
    type(el.querySelector("#inv-name"), "Ana Costa");
    type(el.querySelector("#inv-pw"), "a long enough phrase");
    await click(el.querySelector("[data-testid='invite-accept']"));
    expect(calls.map((c) => c.url)).toEqual(["/v1/auth/invitations/preview", "/v1/auth/invitations/accept"]);
    for (const c of calls) expect(c.url).not.toContain("tok_secret");
    expect(calls[1].body).toMatchObject({ token: "tok_secret", name: "Ana Costa" });
  });

  it("an invitation link without a token says it no longer works, without calling the API", async () => {
    routes({});
    window.history.replaceState(null, "", "/invite");
    const el = await mount(<InviteLanding options={options(null)} />);
    expect(el.querySelector("[data-testid='invite-expired']")).not.toBeNull();
    expect(calls).toEqual([]);
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
  session_id: "ses_1", session_expires_at: null, csrf_token: "c", epoch: 1, network_bound: false, banners: [], avatar_seed: null
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

  it("a passkey-only person sets a password; one who has one changes it", async () => {
    const none = { ...ME, sign_in: { ...ME.sign_in, password: false } };
    const el = await mount(asOperator(<AccountSecurity me={none} factors={F.factors(NOW).usr_sam} identities={[]} host="boss.acme.dev" />));
    expect(el.querySelector("[data-testid='pw-submit']")?.textContent).toBe("Set password");
    expect(el.querySelector("[data-testid='pw-change-note']")).toBeNull();
    expect(el.querySelector("#pw-current")).toBeNull();
    const el2 = await mount(asOperator(<AccountSecurity me={ME} factors={F.factors(NOW).usr_sam} identities={[]} host="boss.acme.dev" />));
    expect(el2.querySelector("[data-testid='pw-submit']")?.textContent).toBe("Change password");
    expect(el2.querySelector("[data-testid='pw-change-note']")?.textContent).toBe("Every other session ends when it changes.");
  });

  it("revoking a grant shows the cascade first", async () => {
    routes({ "POST /v1/identity/me/delegations/dlg_01JB7Q/revoke": { body: { halts: 3, items: [{ kind: "runs", label: "Halts running agents", detail: "Atlas, Forge, Vera", count: 3 }] } } });
    const el = await mount(asOperator(<AgentsForMe delegations={F.delegations(NOW).filter((d) => d.user.id === "usr_sam")} agents={null} connections={[]} untilRevokedAllowed={false} openNew={false} />));
    await click(el.querySelector("[data-testid='grant-dlg_01JB7Q'] button.danger"));
    expect(document.querySelector("[data-testid='cascade']")?.textContent).toContain("Halts running agents");
    expect(document.querySelector("[data-testid='revoke-confirm']")?.textContent).toContain("Revoke and halt 3 runs");
    // (the page also asks whether "Sign in with GitHub" is offered: the revoke is the first write)
    expect(calls.find((c) => String(c.url).endsWith("/revoke"))!.body).toEqual({ dry_run: true });
  });
});

describe("Passkeys need a domain, and every passkey failure says why (round 3, boss-1)", () => {
  const ON_IP = "passkey_needs_domain";
  const LINE = "Passkeys need a domain name. This BOSS is reached by an IP address.";

  it("claim on an IP address: no passkey offered, the authenticator app is the way in, one plain line", async () => {
    window.history.replaceState(null, "", "/claim#t=setup_abc");
    const el = await mount(<ClaimFlow options={options({ path: "setup_token", origin: null, expires_at: null, seats: [] }, { passkey: false, passkey_reason: ON_IP })} />);
    expect(el.querySelector("[data-testid='no-passkey-here']")?.textContent).toBe(LINE);
    expect(el.querySelector("[data-testid='claim-submit']")?.textContent).toContain("Claim and add an authenticator");
    expect(el.querySelector("#claim-pw")).not.toBeNull();
    expect(el.textContent).not.toContain("Create a passkey");
    window.history.replaceState(null, "", "/");
  });

  it("enrolment on an IP address opens on the authenticator app with the line, and offers no passkey", async () => {
    routes({ "POST /v1/identity/me/mfa/totp": { body: { secret: "JBSWY3DPEHPK3PXP", otpauth_uri: "otpauth://totp/x", qr_svg: null } },
             "GET /v1/identity/me": { body: { csrf_token: "c" } } });
    const el = await mount(<Enrol host="100.64.0.20" next="/identity" who={null} passkeyReason={ON_IP} />);
    expect(el.querySelector("[data-testid='totp-step']")).not.toBeNull();
    expect(el.querySelector("[data-testid='no-passkey-here']")?.textContent).toBe(LINE);
    expect(el.querySelector("[data-testid='enrol-passkey']")).toBeNull();
    expect(el.textContent).not.toContain("Use a passkey instead");
    // No QR from the API: no image box at all, only the key.
    expect(el.querySelector("[data-testid='totp-step'] img")).toBeNull();
    expect(el.textContent).toContain("JBSWY3DPEHPK3PXP");
  });

  it("My account on an IP address: no “Add a passkey”, the line instead", async () => {
    const el = await mount(asOperator(<AccountSecurity me={ME} factors={F.factors(NOW).usr_sam} identities={[]} host="100.64.0.20" passkeyReason={ON_IP} />));
    expect(el.querySelector("[data-testid='add-passkey']")).toBeNull();
    expect(el.querySelector("[data-testid='no-passkey-here']")?.textContent).toBe(LINE);
  });

  it("the QR is drawn as a namespaced SVG image, even when the API's SVG lacks xmlns", async () => {
    routes({ "POST /v1/identity/me/mfa/totp": { body: { secret: "K", otpauth_uri: "otpauth://totp/x", qr_svg: '<svg width="8" height="8"><path d="M0 0h8v8H0z"/></svg>' } },
             "GET /v1/identity/me": { body: { csrf_token: "c" } } });
    const el = await mount(<Enrol host="boss.acme.dev" next="/identity" who={null} passkeyReason={ON_IP} />);
    const src = el.querySelector("[data-testid='totp-qr']")?.getAttribute("src") ?? "";
    expect(src.startsWith("data:image/svg+xml;base64,")).toBe(true);
    expect(atob(src.split(",")[1])).toMatch(/^<svg xmlns="http:\/\/www\.w3\.org\/2000\/svg"/);
  });

  for (const [name, words] of [
    ["NotAllowedError", "Passkey cancelled or timed out"],
    ["SecurityError", "This address can't hold a passkey"],
    ["InvalidStateError", "This device already has a passkey here"],
    ["NotSupportedError", "This device can't make this kind of passkey"],
    ["ConstraintError", "This device can't verify it's you"],
    ["UnknownError", "The device's passkey store failed"],
  ] as const) {
    it(`a ${name} from the device says why: “${words}…”`, async () => {
      routes({
        "GET /v1/identity/me": { body: { csrf_token: "c" } },
        "POST /v1/identity/me/passkeys/options": { body: { publicKey: { challenge: "eA", rp: { name: "BOSS" }, user: { id: "dQ", name: "p", displayName: "P" }, pubKeyCredParams: [] } } },
      });
      Object.defineProperty(navigator, "credentials", { value: { get: vi.fn(), create: vi.fn(async () => { throw new DOMException("x", name); }) }, configurable: true });
      const el = await mount(<Enrol host="boss.acme.dev" next="/identity" who={null} />);
      await click(el.querySelector("[data-testid='enrol-passkey']"));
      const said = el.querySelector("[role='alert']")?.textContent ?? "";
      expect(said).toContain(words);
      expect(said).not.toBe("That didn't work");
    });
  }

  it("the API's refusal of passkey options is said in the same plain words", async () => {
    routes({
      "GET /v1/identity/me": { body: { csrf_token: "c" } },
      "POST /v1/identity/me/passkeys/options": { status: 409, body: { error: ON_IP } },
    });
    const el = await mount(<Enrol host="boss.acme.dev" next="/identity" who={null} />);
    await click(el.querySelector("[data-testid='enrol-passkey']"));
    expect(el.querySelector("[role='alert']")?.textContent).toBe(LINE);
  });
});
