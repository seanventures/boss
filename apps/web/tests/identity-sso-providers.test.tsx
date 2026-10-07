// @vitest-environment happy-dom
/**
 * Identity › Sign-in: sign-in providers end to end against the identity stub (the routes of
 * identity_sso.py and FX-B's GET /providers/{id}):
 *   - with none: an empty state that explains and offers Add provider (an Admin sees it greyed,
 *     naming "Manage identity providers");
 *   - Add: pick a kind, fill it, the POST body carries only allowlisted fields, the redirect URL
 *     to paste into the IdP is shown, the card appears;
 *   - Edit sends only what changed; the secret is write-only; Remove confirms, then the card is off;
 *   - Groups and sync: a mapping saves as {external: group_id}; a SCIM token is shown once and
 *     the dialog cannot close until "stored" is ticked; Revoke asks first;
 *   - a failed providers read is an error, never "no providers";
 *   - no raw permission keys, codes or ids on screen; the copy budget holds.
 */
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { IdentityProvider } from "../components/identity/identity-provider";
import { NowProvider } from "../components/identity/kit";
import { SignInSettings } from "../app/identity/sign-in/sign-in-settings";
import { createBody, emptyForm, idpUrls, missing, patchBody, testFailureWords } from "../app/identity/sign-in/provider-form";
import type { MePermissionsResponse, MeResponse } from "../lib/identity/contract";
import { resetStub, stubHandle } from "../lib/identity/stub/server";
import type { Provider, SecurityPolicy } from "../lib/identity/types";
import { scimPrincipalId } from "../lib/identity/types-sso";
import { identityCopyAudit } from "./_helpers/identity-copy";
import { rawLeaks } from "./_helpers/identity-raw";
import { req, signIn } from "./_helpers/identity-stub-session";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }), useSearchParams: () => new URLSearchParams(), usePathname: () => "/" }));

const NOW = Date.now();
const ORIGIN = "https://boss.acme.dev";
let root: Root | null = null;
let host: HTMLElement | null = null;
let calls: { method: string; path: string; body: unknown }[] = [];

function as(email: string) {
  const s = signIn(email);
  const get = <T,>(path: string) => stubHandle(req("GET", path, { cookies: s.cookies })).body as T;
  return { s, me: get<MeResponse>("/v1/identity/me"), org: get<MePermissionsResponse>("/v1/identity/me/permissions"), get };
}

function wire(s: { cookies: Record<string, string> }) {
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), "http://x");
    const method = (init?.method ?? "GET").toUpperCase();
    const body = typeof init?.body === "string" ? JSON.parse(init.body) : null;
    calls.push({ method, path: url.pathname, body });
    const out = stubHandle({ method, path: url.pathname, query: url.searchParams, body, cookies: s.cookies, headers: new Headers(init?.headers) });
    return out.status === 204 ? new Response(null, { status: 204 })
      : new Response(JSON.stringify(out.body), { status: out.status, headers: { "content-type": "application/json" } });
  }));
}

beforeEach(() => { resetStub(NOW); calls = []; });
afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  document.body.innerHTML = "";
  root = null;
  vi.unstubAllGlobals();
});

async function settle() {
  for (let i = 0; i < 8; i += 1) await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
}

async function mount(who: ReturnType<typeof as>, node: React.ReactElement) {
  wire(who.s);
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => root!.render(<IdentityProvider me={who.me} org={who.org}><NowProvider now={NOW}>{node}</NowProvider></IdentityProvider>));
  await settle();
  return host;
}

const q = (sel: string) => document.querySelector(sel) as HTMLElement | null;
async function click(el: Element | null) {
  expect(el, "element to click").not.toBeNull();
  await act(async () => { (el as HTMLElement).click(); });
  await settle();
}
async function type(el: Element | null, value: string) {
  expect(el, "field").not.toBeNull();
  const input = el as HTMLInputElement;
  const proto = input instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : input instanceof HTMLSelectElement ? HTMLSelectElement.prototype : HTMLInputElement.prototype;
  await act(async () => {
    Object.getOwnPropertyDescriptor(proto, "value")!.set!.call(input, value);
    input.dispatchEvent(new Event(input instanceof HTMLSelectElement ? "change" : "input", { bubbles: true }));
  });
  await settle();
}

function expectClean(el: Element) {
  expect(rawLeaks(el), "raw keys, codes or ids on screen").toEqual([]);
  const a = identityCopyAudit(el);
  expect(a.labels, "labels over 6 words").toEqual([]);
  expect(a.prose, "prose over 24 words").toEqual([]);
  expect(a.tips, "tooltips over 14 words").toEqual([]);
}

const settings = (who: ReturnType<typeof as>, providers?: Provider[]) => (
  <SignInSettings providers={providers ?? who.get<{ providers: Provider[] }>("/v1/identity/providers").providers}
    policy={who.get<SecurityPolicy>("/v1/identity/policy")} legacy={[]} publicUrl={{ public_origin: ORIGIN, public_origin_source: "env" }} />
);

describe("the provider form's model", () => {
  it("sends only allowlisted fields, per kind", () => {
    const okta = { ...emptyForm("okta"), issuer: "https://acme.okta.com", client_id: "abc", client_secret: "s3", domains: "@Acme.dev, acme.io" };
    expect(missing(okta)).toBe("");
    expect(createBody(okta)).toEqual({ protocol: "oidc", preset: "okta", name: "Okta", issuer: "https://acme.okta.com", client_id: "abc", client_secret: "s3",
      scopes: ["openid", "email", "profile"], domains: ["acme.dev", "acme.io"], jit_mode: "off", trust_idp_mfa: false, enforce_sso: false });
    const ms = { ...emptyForm("microsoft"), client_id: "c", client_secret: "s" };
    expect(missing(ms)).toBe("Add your tenant id");
    expect(createBody({ ...ms, tenant: "contoso" })).toMatchObject({ preset: "microsoft", tenant: "contoso" });
    expect(createBody({ ...ms, tenant: "contoso" })).not.toHaveProperty("issuer");
    const saml = { ...emptyForm("saml"), issuer: "urn:idp", saml_sso_url: "https://idp/sso", saml_certificate: "PEM" };
    expect(createBody(saml)).toMatchObject({ protocol: "saml", preset: "generic", saml_certificates: ["PEM"] });
    expect(createBody(saml)).not.toHaveProperty("client_id");
    expect(missing({ ...emptyForm("generic"), client_id: "x", client_secret: "y", issuer: "https://i", enforce_sso: true })).toBe("Requiring SSO needs a domain");
  });

  it("an edit sends only what changed, and never an empty secret", () => {
    const p = stubHandle(req("GET", "/v1/identity/providers", { cookies: signIn("param@example.com").cookies })).body as { providers: Provider[] };
    const okta = p.providers.find((x) => x.id === "idp_okta")!;
    const f = { ...emptyForm("okta"), name: okta.name, issuer: okta.issuer!, client_id: okta.client_id!, scopes: okta.scopes.join(" "), domains: okta.domains.join(", "),
      jit: true, trust_mfa: true, groups_claim: "groups" };
    expect(patchBody(okta, f)).toEqual({});
    expect(patchBody(okta, { ...f, name: "Okta", client_secret: "new" })).toEqual({ name: "Okta", client_secret: "new" });
  });

  it("names the URLs for the IdP, and test failures in words", () => {
    expect(idpUrls(ORIGIN, { id: "idp_1", protocol: "oidc" })).toEqual([expect.objectContaining({ value: `${ORIGIN}/v1/auth/sso/idp_1/callback` })]);
    expect(idpUrls(ORIGIN, { id: null, protocol: "saml" })!.map((u) => u.value)).toEqual([`${ORIGIN}/saml/v2/acs`, `${ORIGIN}/saml/v2/metadata`]);
    expect(idpUrls(null, { id: "idp_1", protocol: "oidc" })).toBeNull();
    expect(testFailureWords("jwks_refused")).toBe("Can't read its signing keys");
    expect(testFailureWords("anything_else")).not.toMatch(/_/);
    expect(scimPrincipalId("idp_abc_def")).toBe("sys_scim_abc_def");
  });
});

describe("Sign-in providers", () => {
  it("with none: explains, and offers Add provider; an Admin sees it greyed with the permission's name", async () => {
    const owner = as("param@example.com");
    const el = await mount(owner, settings(owner, []));
    expect(q("[data-testid='providers-empty']")?.textContent).toContain("No single sign-on yet");
    expect(q("[data-testid='provider-add']")?.getAttribute("aria-disabled")).toBeNull();
    expectClean(el);
    act(() => root?.unmount());
    host?.remove();

    const admin = as("priya@acme.dev");
    await mount(admin, settings(admin, []));
    const add = q("[data-testid='provider-add']")!;
    expect(add.getAttribute("aria-disabled")).toBe("true");
    expect(add.getAttribute("title")).toBe("Needs “Manage identity providers” · ask an Owner");
  });

  it("a failed read is an error with Try again, never 'no providers'", async () => {
    const owner = as("param@example.com");
    const el = await mount(owner, <SignInSettings providers={[]} policy={null} legacy={null}
      failures={{ providers: { status: 503, code: "identity_unavailable" }, policy: { status: 500, code: "error" } }} />);
    expect(q("[data-testid='providers-error']")?.textContent).toContain("Identity is unreachable");
    expect(q("[data-testid='providers-empty']")).toBeNull();
    expect(el.textContent).toContain("Couldn't load the security policy");
    expectClean(el);
  });

  it("adds an Okta provider: only allowlisted fields go, the redirect URL is shown, the card appears", async () => {
    const owner = as("param@example.com");
    await mount(owner, settings(owner, []));
    await click(q("[data-testid='provider-add']"));
    await click(q("[data-testid='kind-okta']"));
    // The redirect URL comes FIRST, before the client id exists: the id is reserved on open.
    const first = q("[data-testid='provider-first']")!.textContent ?? "";
    const redirect = new RegExp(`${ORIGIN}/v1/auth/sso/(idp_[a-z2-7]{26})/callback`).exec(first);
    expect(redirect, first).not.toBeNull();
    expect(q("[data-testid='provider-first'] button")?.textContent).toContain("Copy");
    expect(q("[data-testid='provider-save']")?.hasAttribute("disabled")).toBe(true);
    await type(q("#pv-issuer"), "https://acme2.okta.com");
    await type(q("#pv-cid"), "0oa2");
    await type(q("#pv-secret"), "secret-value");
    await type(q("#pv-domains"), "acme.dev");
    expectClean(q("[data-testid='provider-sheet']")!);
    await click(q("[data-testid='provider-save']"));
    const post = calls.find((c) => c.method === "POST" && c.path === "/v1/identity/providers")!;
    expect(Object.keys(post.body as object).sort()).toEqual(["client_id", "client_secret", "domains", "enforce_sso", "id", "issuer", "jit_mode", "name", "preset", "protocol", "scopes", "trust_idp_mfa"]);
    expect((post.body as { id: string }).id).toBe(redirect![1]);
    const added = q("[data-testid='provider-added']")!;
    // The URL pasted into the IdP before saving is the one the saved provider answers on.
    expect(added.textContent).toContain(`${ORIGIN}/v1/auth/sso/${redirect![1]}/callback`);
    await click(q("[data-testid='provider-done']"));
    expect(document.querySelector("[data-testid^='provider-idp_']")?.textContent).toContain("Okta");
  });

  it("Google says to add yourself as a Test user; SAML shows its ACS and entity ID first", async () => {
    const owner = as("param@example.com");
    await mount(owner, settings(owner, []));
    await click(q("[data-testid='provider-add']"));
    await click(q("[data-testid='kind-google']"));
    expect(q("[data-testid='google-hint']")?.textContent).toContain("Test user under Audience");
    expect(q("[data-testid='provider-first']")?.textContent).toMatch(/\/v1\/auth\/sso\/idp_[a-z2-7]{26}\/callback/);
    act(() => root?.unmount()); host?.remove();
    await mount(owner, settings(owner, []));
    await click(q("[data-testid='provider-add']"));
    await click(q("[data-testid='kind-saml']"));
    const first = q("[data-testid='provider-first']")!.textContent ?? "";
    expect(first).toContain(`${ORIGIN}/saml/v2/acs`);
    expect(first).toContain(`${ORIGIN}/saml/v2/metadata`);
    expect(first).not.toContain("provider_id="); // the metadata URL exists only once saved
    expect(q("[data-testid='google-hint']")).toBeNull();
  });

  it("edits only what changed; Remove confirms and turns it off", async () => {
    const owner = as("param@example.com");
    const el = await mount(owner, settings(owner));
    expectClean(el);
    await click(q("[data-testid='provider-edit-idp_okta']"));
    expect((q("#pv-secret") as HTMLInputElement).value).toBe("");
    expect((q("#pv-secret") as HTMLInputElement).placeholder).toBe("Stored · type to replace");
    await type(q("#pv-name"), "Acme Okta");
    await click(q("[data-testid='provider-save']"));
    expect(calls.find((c) => c.method === "PATCH")?.body).toEqual({ name: "Acme Okta" });
    expect(q("[data-testid='provider-idp_okta']")?.textContent).toContain("Acme Okta");

    await click(q("[data-testid='provider-remove-idp_okta']"));
    expect(q("[data-testid='remove-provider']")?.textContent).toContain("directory-sync tokens stop working");
    await click(q("[data-testid='remove-provider-confirm']"));
    expect(calls.some((c) => c.method === "DELETE" && c.path === "/v1/identity/providers/idp_okta")).toBe(true);
    expect(q("[data-testid='provider-status-idp_okta']")?.getAttribute("aria-pressed")).toBe("false");
  });

  it("maps an IdP group to a BOSS group, and shows a SCIM token once", async () => {
    const owner = as("param@example.com");
    await mount(owner, settings(owner));
    await click(q("[data-testid='provider-sync-idp_okta']"));
    const sheet = q("[data-testid='provider-sync']")!;
    expect(sheet.textContent).toContain(`${ORIGIN}/scim/v2`);
    expect(q("[data-testid='scim-empty']")).not.toBeNull();
    await click(q("[data-testid='mapping-add']"));
    await type(sheet.querySelector("[aria-label='IdP group']"), "eng");
    await type(sheet.querySelector("[aria-label='BOSS group']"), "grp_builders");
    await click(q("[data-testid='mapping-save']"));
    expect(calls.find((c) => c.method === "PATCH")?.body).toEqual({ groups_claim: "groups", group_mappings: { eng: "grp_builders" } });
    expectClean(q("[data-testid='provider-sync']")!);

    await click(q("[data-testid='scim-create']"));
    const once = q("[data-testid='scim-shown-once']")!;
    expect(q("[data-testid='scim-secret']")?.textContent).toMatch(/^boss_scim_/);
    expect(q("[data-testid='scim-done']")?.hasAttribute("disabled")).toBe(true);
    await act(async () => { document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" })); });
    expect(q("[data-testid='scim-shown-once']")).not.toBeNull();
    expectClean(once);
    await click(q("[data-testid='scim-stored']"));
    await click(q("[data-testid='scim-done']"));
    expect(q("[data-testid='scim-secret']")).toBeNull();
    const row = q("[data-testid='scim-keys']")!;
    expect(row.textContent).toContain("SCIM");
    await click(row.querySelector("[data-testid^='scim-revoke-key_']"));
    await click(row.querySelector("[data-testid^='scim-revoke-confirm-']"));
    expect(calls.some((c) => c.method === "DELETE" && /\/scim-keys\/key_/.test(c.path))).toBe(true);
  });
});
