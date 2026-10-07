// @vitest-environment happy-dom
/**
 * IDENTITY CONSOLE GAPS (integration walk, fixer F3). Each block is one gap: the console showed
 * something the API does not answer, said something untrue, or offered something it refuses.
 *
 *   G1  the delegation page reads GET /v1/identity/delegations/{id} (404 → not found), and a
 *       grantor may open their own grant's page (the API lets them);
 *   G3  the sidebar and the account menu name the HIGHEST role (an Owner who also holds Admin
 *       read "Admin · Owner"); never a guessed "Operator" when the API did not answer;
 *   G7  My account offers "Link" for a provider not linked yet: the start is probed, a fresh
 *       session is asked for, and only then does the browser go to the provider;
 *   G12 an enrolment-only session (403 mfa_enrolment_required) is taken to /enrol, and a refused
 *       enrolment (enrolment_grant_required) says to ask an admin to reset the second factor;
 *   G13 Setup shows the public URL BOSS uses, or how to set BOSS_PUBLIC_URL;
 *   G14 email sign-in (magic link, email code, forgot password) is hidden without a public URL;
 *   G15 the posture's "origin" check renders, and /identity/sign-in#public-url shows the URL;
 *   G12b a sign-in answered next:"enrol" with enrolment_grant:false says to ask an admin.
 */
import React, { act } from "react";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { allRoleLabels, primaryRoleLabel } from "../lib/identity/role-label";
import { IdentityError, idGet } from "../lib/identity/api";
import { ENROLMENT_GRANT_WORDS, enrolHref, needsEnrolment, sendToEnrolment } from "../lib/identity/enrolment";
import { probeSsoLink, ssoLinkStartPath } from "../lib/identity/sso-link";
import { sayAuth } from "../app/(auth)/auth-flow";
import { SignIn } from "../app/(auth)/login/sign-in";
import { emailSignInOffered } from "../lib/identity/email-sign-in";
import { PublicUrlRow, PUBLIC_URL_TIP } from "../app/setup/public-url-row";
import { AccountSecurity } from "../app/account/security";
import { pageFor } from "../lib/identity/pages";
import { detailOf } from "../app/identity/agents/delegations/[id]/detail";
import type { MeResponse } from "../lib/identity/contract";
import type { AuthOptions, Delegation } from "../lib/identity/types";
import * as F from "../lib/identity/stub/fixtures";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
// A server page (no React import) rendered here under the classic JSX runtime.
(globalThis as { React?: typeof React }).React = React;
vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }), useSearchParams: () => new URLSearchParams(), usePathname: () => "/",
  notFound: () => { throw new Error("NEXT_NOT_FOUND"); }, redirect: (to: string) => { throw new Error(`NEXT_REDIRECT ${to}`); },
}));

const WEB = process.cwd();
const read = (p: string) => readFileSync(join(WEB, p), "utf8");
let root: Root | null = null;
let host: HTMLElement | null = null;

async function mount(node: React.ReactElement) {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => root!.render(node));
  for (let i = 0; i < 6; i += 1) await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
  return host;
}

beforeEach(() => { window.history.replaceState(null, "", "/"); });
afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  root = null;
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

const OPTIONS: AuthOptions = { org_name: "Acme", host: "boss.acme.dev", providers: [], smtp: true, signup: false, legacy_password: false, claim: null };

describe("G1 · one delegation, read by id", () => {
  it("the page asks the API for that grant and keeps its shape; 404 is not found", async () => {
    const grant = F.delegations(Date.now())[0] as Delegation;
    const serverGet = vi.fn(async (_path: string) => ({ ok: true as const, status: 200, data: grant }));
    vi.doMock("../lib/identity/server", () => ({ serverGet }));
    vi.doMock("../components/identity/route-shell", () => ({ pageAllowed: async () => true }));
    const { default: Page } = await import("../app/identity/agents/delegations/[id]/page");
    const el = await Page({ params: Promise.resolve({ id: grant.id }) }) as React.ReactElement<{ initial: { id: string; chain: unknown[] } }>;
    expect(serverGet).toHaveBeenCalledWith(`/v1/identity/delegations/${grant.id}`);
    expect(serverGet).toHaveBeenCalledTimes(1);          // no 500-row list scan
    expect(el.props.initial.id).toBe(grant.id);
    expect(el.props.initial.chain).toEqual([]);
    serverGet.mockResolvedValueOnce({ ok: false, status: 404, code: "not_found", permission: null } as never);
    await expect(Page({ params: Promise.resolve({ id: "dlg_other_org" }) })).rejects.toThrow("NEXT_NOT_FOUND");
    vi.doUnmock("../lib/identity/server");
    vi.doUnmock("../components/identity/route-shell");
  });

  it("a full detail (the dev stub's) is kept as it is", () => {
    const grant = F.delegations(Date.now())[0] as Delegation;
    const detail = { ...grant, run: null, chain: [{ kind: "person", id: "x", name: "X", count: 1, count_label: "", effective: [] }], tokens: [], egress: [], checked: null };
    expect(detailOf(detail as never)).toBe(detail);
  });

  it("the grantor may open the page; the API decides who else (grantor or delegation:read_all)", () => {
    expect(pageFor("/identity/agents/delegations/dlg_1")?.access).toEqual({ authenticated: true });
  });
});

describe("G3 · the role the sidebar names", () => {
  it("is the highest role held, whatever order the API lists them in", () => {
    expect(primaryRoleLabel(["admin", "owner"])).toBe("Owner");
    expect(primaryRoleLabel(["operator", "owner"])).toBe("Owner");
    expect(primaryRoleLabel(["auditor", "viewer"])).toBe("Auditor");
    expect(primaryRoleLabel(["release_manager"])).toBe("Release manager");
    expect(primaryRoleLabel([])).toBe("No access role");
    expect(allRoleLabels(["admin", "release_manager", "owner"])).toBe("Owner · Admin · Release manager");
  });

  it("the sidebar and the account menu both use it, and never guess Operator", () => {
    const shell = read("app/ConsoleShell.tsx");
    expect(shell).toContain("primaryRoleLabel(me.access_roles)");
    expect(shell).not.toContain("Operator · in control");
    expect(read("components/identity/account-menu.tsx")).toContain("primaryRoleLabel(me.access_roles)");
  });
});

describe("G7 · link a single sign-on account from My account", () => {
  it("probes the start without following it: a redirect means go, a refusal is thrown", async () => {
    expect(ssoLinkStartPath("idp 1")).toBe("/v1/auth/sso/idp%201/start?purpose=link");
    const redirect = vi.fn(async () => ({ type: "opaqueredirect", status: 0, ok: false, json: async () => ({}) }) as unknown as Response);
    await expect(probeSsoLink("idp_g", redirect as unknown as typeof fetch)).resolves.toBeUndefined();
    expect(redirect).toHaveBeenCalledWith("/v1/auth/sso/idp_g/start?purpose=link", expect.objectContaining({ redirect: "manual" }));
    const stale = vi.fn(async () => new Response(JSON.stringify({ error: "step_up_required" }), { status: 401 }));
    await expect(probeSsoLink("idp_g", stale as unknown as typeof fetch)).rejects.toMatchObject({ code: "step_up_required", status: 401 });
  });

  it("offers Link for each provider not linked yet, and goes to the provider once the start allows it", async () => {
    const assign = vi.fn();
    vi.spyOn(window, "location", "get").mockReturnValue({ ...window.location, assign, pathname: "/account", search: "" } as Location);
    vi.stubGlobal("fetch", vi.fn(async (url: string) => (String(url).includes("/start?purpose=link")
      ? { type: "opaqueredirect", status: 0, ok: false, json: async () => ({}) } as unknown as Response
      : new Response("{}", { status: 200 }))));
    const me = { display_name: "Sam", email: "sam@acme.dev", org: { id: "org_boss", name: "Acme" }, session_expires_at: null,
      sign_in: { password: true, totp: false, passkeys: 0, recovery_codes_remaining: 0, sso_links: [] } } as unknown as MeResponse;
    const el = await mount(<AccountSecurity me={me} factors={[]} host="boss.acme.dev"
      identities={[{ id: "lid_1", provider: "GitHub", preset: "github", label: "sam", linked_at: new Date().toISOString() }]}
      providers={[{ id: "idp_gh", name: "GitHub", preset: "github" }, { id: "idp_google", name: "Google", preset: "google" }]} />);
    expect(el.querySelector("[data-testid='sso-link-idp_gh']")).toBeNull();      // already linked
    const row = el.querySelector("[data-testid='sso-link-idp_google']");
    expect(row).not.toBeNull();
    await act(async () => { (row!.querySelector("button") as HTMLButtonElement).click(); });
    for (let i = 0; i < 4; i += 1) await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
    expect(assign).toHaveBeenCalledWith("/v1/auth/sso/idp_google/start?purpose=link");
  });

  it("the callback's landing pages exist: /signin?error=, /signin?step=mfa and /account?linked=1", () => {
    const signin = read("app/(auth)/signin/page.tsx");
    expect(signin).toMatch(/params\.step === "mfa"/);
    expect(signin).toMatch(/ssoErrorWords\(params\.error\)/);
    expect(read("app/account/page.tsx")).toMatch(/linked=\{linked === "1"\}/);
  });
});

describe("G12 · an enrolment-only session", () => {
  it("is recognised from the me banner or the API's refusal", () => {
    expect(needsEnrolment({ banners: [{ kind: "mfa_enrolment_required", message: "", until: null }] })).toBe(true);
    expect(needsEnrolment({ banners: [] })).toBe(false);
    expect(needsEnrolment(null, "mfa_enrolment_required")).toBe(true);
    expect(enrolHref("/identity/people")).toBe("/enrol?next=%2Fidentity%2Fpeople");
    expect(enrolHref("//evil.example")).toBe("/enrol?next=%2F");
  });

  it("any identity call refused mfa_enrolment_required takes the person to /enrol (never from /enrol)", async () => {
    const assign = vi.fn();
    expect(sendToEnrolment("mfa_enrolment_required", { pathname: "/identity", search: "?x=1", assign })).toBe(true);
    expect(assign).toHaveBeenCalledWith("/enrol?next=%2Fidentity%3Fx%3D1");
    expect(sendToEnrolment("mfa_enrolment_required", { pathname: "/enrol", search: "", assign: vi.fn() })).toBe(false);
    expect(sendToEnrolment("forbidden", { pathname: "/identity", search: "", assign: vi.fn() })).toBe(false);

    const go = vi.fn();
    vi.spyOn(window, "location", "get").mockReturnValue({ ...window.location, assign: go, pathname: "/identity/people", search: "" } as Location);
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ error: "mfa_enrolment_required" }), { status: 403 })));
    await expect(idGet("/v1/identity/users")).rejects.toBeInstanceOf(IdentityError);
    expect(go).toHaveBeenCalledWith("/enrol?next=%2Fidentity%2Fpeople");
  });

  it("a refused enrolment says to ask an admin to reset the second factor", () => {
    expect(sayAuth(new IdentityError(403, "enrolment_grant_required"))).toBe(ENROLMENT_GRANT_WORDS);
    expect(ENROLMENT_GRANT_WORDS).toMatch(/ask an admin to reset your second factor/i);
  });

  it("the console and the identity pages send such a session to enrol before rendering", () => {
    expect(read("app/page.tsx")).toMatch(/needsEnrolment\(enrolment\.me, enrolment\.problem\)[\s\S]{0,200}<EnrolmentGate/);
    expect(read("components/identity/route-shell.tsx")).toMatch(/needsEnrolment\(boot\.me, boot\.problem\)\) redirect\(enrolHref\(path\)\)/);
  });
});

describe("G13/G14 · the public URL", () => {
  it("Setup shows the public URL in use, and where it came from", async () => {
    const el = await mount(<PublicUrlRow initial={{ public_origin: "https://boss.acme.dev", public_origin_source: "env" }} />);
    expect(el.querySelector("[data-testid='public-url']")?.textContent).toContain("https://boss.acme.dev");
    expect(el.querySelector("[data-testid='public-url']")?.getAttribute("data-tip")).toContain("BOSS_PUBLIC_URL");
  });

  it("Setup says how to set BOSS_PUBLIC_URL when none is bound", async () => {
    const el = await mount(<PublicUrlRow initial={{ public_origin: null, public_origin_source: null }} />);
    expect(el.querySelector("[data-testid='public-url-unset']")?.getAttribute("data-tip")).toBe(PUBLIC_URL_TIP);
    expect(PUBLIC_URL_TIP).toContain("BOSS_PUBLIC_URL");
    expect(read("app/setup/setup-board.tsx")).toContain("<PublicUrlRow />");
  });

  it("email sign-in is offered only with SMTP and a public URL (otherwise shown disabled, saying why)", async () => {
    expect(emailSignInOffered({ smtp: true, public_origin: "https://boss.acme.dev" })).toBe(true);
    expect(emailSignInOffered({ smtp: true, public_origin: undefined })).toBe(true);   // an API that does not say
    expect(emailSignInOffered({ smtp: true, public_origin: null })).toBe(false);
    expect(emailSignInOffered({ smtp: false, public_origin: "https://x" })).toBe(false);
    const el = await mount(<SignIn options={{ ...OPTIONS, public_origin: null }} next="/" />);
    // The methods keep their place but are off, and their tooltip names the missing public address.
    const off = [...el.querySelectorAll("[role='tab'][aria-disabled='true']")];
    expect(off.map((t) => t.textContent)).toEqual(["Magic link", "Email code"]);
    expect(document.getElementById(off[0].getAttribute("aria-describedby") ?? "")?.textContent).toBe("Magic links need BOSS's public address set");
    expect(el.querySelector("a[href='/reset']")).toBeNull();
  });
});

describe("G15 · the posture's public URL check, and where its fix lands", () => {
  it("the \"origin\" check is a known check; its fix links to /identity/sign-in#public-url", () => {
    const overview = read("app/identity/overview.tsx");
    expect(overview).toMatch(/origin: <Globe/);
    expect(overview).toContain("data-status={tone(c.status)}");
    expect(read("lib/identity/types.ts")).toMatch(/"audit" \| (?:"enforcement" \| )?"origin"/);
  });

  it("the sign-in settings show the bound URL under #public-url, or how to set it", async () => {
    const { SignInSettings } = await import("../app/identity/sign-in/sign-in-settings");
    const bound = await mount(<SignInSettings providers={[]} policy={null} legacy={null}
      publicUrl={{ public_origin: "https://boss.acme.dev", public_origin_source: "env" }} />);
    expect(bound.querySelector("#public-url [data-testid='public-url-value']")?.textContent).toBe("https://boss.acme.dev");
    act(() => root?.unmount());
    host?.remove();
    const unset = await mount(<SignInSettings providers={[]} policy={null} legacy={null}
      publicUrl={{ public_origin: null, public_origin_source: null }} />);
    expect(unset.querySelector("#public-url")?.textContent).toContain("BOSS_PUBLIC_URL");
    expect(read("app/identity/sign-in/page.tsx")).toContain("public_origin: options.public_origin");
  });
});

describe("G12b · a sign-in that may not enrol", () => {
  it("says to ask an admin instead of opening a refused enrol screen", async () => {
    const { afterEnrolStep } = await import("../lib/identity/enrolment");
    expect(afterEnrolStep({ enrolment_grant: false }, "/")).toBe("/signin?error=enrolment_grant_required");
    expect(afterEnrolStep({ enrolment_grant: true }, "/x")).toBe("/enrol?next=%2Fx");
    expect(afterEnrolStep({}, "/x")).toBe("/enrol?next=%2Fx");
    const { ssoErrorWords } = await import("../app/(auth)/auth-flow");
    expect(ssoErrorWords("enrolment_grant_required")).toBe(ENROLMENT_GRANT_WORDS);

    const assign = vi.fn();
    vi.spyOn(window, "location", "get").mockReturnValue({ ...window.location, assign, pathname: "/login", search: "" } as Location);
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ next: "enrol", enrolment_grant: false, methods: [], who: null }), { status: 200 })));
    const el = await mount(<SignIn options={{ ...OPTIONS, smtp: false }} next="/" />);
    const email = el.querySelector("#email") as HTMLInputElement;
    const password = el.querySelector("#password") as HTMLInputElement;
    for (const [input, value] of [[email, "sam@acme.dev"], [password, "correct horse battery"]] as const) {
      act(() => {
        Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, value);
        input.dispatchEvent(new Event("input", { bubbles: true }));
      });
    }
    await act(async () => { (el.querySelector("[data-testid='email-submit']") as HTMLButtonElement).click(); });
    for (let i = 0; i < 6; i += 1) await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
    expect(assign).not.toHaveBeenCalled();
    expect(el.textContent).toContain(ENROLMENT_GRANT_WORDS);
  });
});
