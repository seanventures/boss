// @vitest-environment happy-dom
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { get, refresh, redirect } = vi.hoisted(() => ({
  get: vi.fn(), refresh: vi.fn(), redirect: vi.fn(),
}));
vi.mock("../lib/identity/server", () => ({ serverGet: get }));
vi.mock("../lib/client", async (original) => ({
  ...await original<typeof import("../lib/client")>(), API_URL: "http://boss-api.example.test",
}));
vi.mock("next/headers", () => ({ headers: async () => new Headers({ host: "boss.example.test" }) }));
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh }), redirect }));

import { loadAuthOptionsState } from "../app/(auth)/auth-options";
import { AuthUnavailable } from "../app/(auth)/auth-unavailable";
import ClaimPage from "../app/(auth)/claim/page";
import LoginPage from "../app/(auth)/login/page";
import SsoLandingPage from "../app/(auth)/signin/page";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const claimed = {
  org_name: "BOSS", host: "boss.example.test", providers: [], smtp: false,
  signup: false, legacy_password: false, claim: null,
};
const unclaimed = { ...claimed, claim: { path: "setup_token", origin: null, expires_at: null, seats: [] } };
const failure = { ok: false, status: 503, code: "identity_unavailable", permission: null };
const healthy = (options: unknown) => ({ ok: true, data: options });
let root: Root | null = null;
let host: HTMLDivElement | null = null;

beforeEach(() => {
  vi.clearAllMocks();
  get.mockImplementation(async (path: string) => path === "/v1/auth/options" ? healthy(claimed)
    : { ok: false, status: 401, code: "unauthenticated", permission: null });
});
afterEach(() => {
  vi.unstubAllGlobals();
  act(() => root?.unmount());
  host?.remove();
  root = null;
  host = null;
});

describe("auth options are known only after a valid successful read", () => {
  it.each(["connection refused", "service 503", "malformed JSON"])("keeps the real server read boundary unavailable after %s", async (condition) => {
    const { serverGet } = await vi.importActual<typeof import("../lib/identity/server")>("../lib/identity/server");
    const transport = vi.fn(async () => {
      if (condition === "connection refused") throw new TypeError("fetch failed: ECONNREFUSED");
      return condition === "service 503" ? Response.json({ error: "temporarily_unavailable" }, { status: 503 })
        : new Response("{broken", { status: 200, headers: { "Content-Type": "application/json" } });
    });
    vi.stubGlobal("fetch", transport);
    get.mockImplementation(serverGet);
    expect(await loadAuthOptionsState()).toEqual({ available: false, host: "boss.example.test" });
    expect(transport).toHaveBeenCalledOnce();
  });
  it.each([failure, { ...failure, status: 502 }])("keeps a transport/service failure unavailable (%j)", async (read) => {
    get.mockResolvedValue(read);
    expect(await loadAuthOptionsState()).toEqual({ available: false, host: "boss.example.test" });
  });
  it.each([null, {}, { ...claimed, claim: undefined }, { ...claimed, providers: {} },
    { ...unclaimed, claim: { ...unclaimed.claim, seats: null } },
    { ...unclaimed, claim: { ...unclaimed.claim, path: ["setup_token"] } },
    { ...unclaimed, claim: { ...unclaimed.claim, paths: ["unknown"] } },
    { ...claimed, providers: [{ id: "idp_one", name: "Company SSO", preset: "unknown" }] },
  ])("rejects malformed successful answers (%j)", async (options) => {
    get.mockResolvedValue(healthy(options));
    expect((await loadAuthOptionsState()).available).toBe(false);
  });
  it.each([claimed, unclaimed, { ...unclaimed, password_min_length: 16, public_origin: null },
    { ...unclaimed, claim: { ...unclaimed.claim, path: "network_bridge", paths: ["network", "setup_token"] } },
    { ...claimed, providers: [{ id: "idp_okta", name: "Company SSO", preset: "okta" }],
      passkey: false, passkey_reason: "public_origin_unset", public_origin: null,
      public_origin_source: null, shared_password: null, password_min_length: 12 },
  ])("preserves a legitimate claimed or unclaimed answer (%j)", async (options) => {
    get.mockResolvedValue(healthy(options));
    expect(await loadAuthOptionsState()).toEqual({ available: true, options });
  });
});

describe("claim, login and SSO landing do not infer identity state from an outage", () => {
  it.each(["claim", "login", "signin-mfa", "signin-error"])("%s shows only truthful unavailable state and Retry", async (route) => {
    get.mockResolvedValue(failure);
    const view = route === "claim" ? await ClaimPage() : route === "login"
      ? await LoginPage({ searchParams: Promise.resolve({ next: "/account" }) })
      : await SsoLandingPage({ searchParams: Promise.resolve({ next: "/account",
        ...(route === "signin-mfa" ? { step: "mfa" } : { error: "access_denied" }) }) });
    const html = renderToStaticMarkup(view);
    expect(html).toContain("Sign-in unavailable");
    expect(html).toContain("check whether this BOSS has an Owner");
    expect(html).toContain("Retry");
    expect(html).not.toContain("Already claimed");
    expect(html).not.toContain("/claim");
    expect(html).not.toContain("<form");
  });
  it("keeps confirmed already-claimed claim view", async () => {
    expect(renderToStaticMarkup(await ClaimPage())).toContain("Already claimed");
  });
  it("recovers to the confirmed unclaimed flow after a new read", async () => {
    get.mockResolvedValueOnce(failure).mockResolvedValueOnce(healthy(unclaimed));
    expect(renderToStaticMarkup(await ClaimPage())).toContain("check whether this BOSS has an Owner");
    const recovered = renderToStaticMarkup(await ClaimPage());
    expect(recovered).toContain("Claim this BOSS");
    // The setup link is read from the fragment after mount; the server render shows the heading only.
    expect(recovered).not.toContain("Already claimed");
  });
  it("keeps the healthy login and claim door", async () => {
    get.mockImplementation(async (path: string) => path === "/v1/auth/options" ? healthy(unclaimed) : failure);
    const html = renderToStaticMarkup(await LoginPage({ searchParams: Promise.resolve({ next: "/account" }) }));
    expect(html).toContain("Welcome back");
    expect(html).toContain("This BOSS has no Owner yet");
    expect(html).not.toContain("Sign-in unavailable");
  });
  it("keeps the healthy SSO landing's second-factor step", async () => {
    const html = renderToStaticMarkup(await SsoLandingPage({ searchParams: Promise.resolve({ step: "mfa", next: "/account" }) }));
    expect(html).toContain("authenticator");
    expect(html).not.toContain("Sign-in unavailable");
  });
  it("Retry refreshes the current route without replacing next or the setup-token fragment", async () => {
    window.history.replaceState(null, "", "/claim?next=%2Faccount#t=test-proof-fragment");
    const before = window.location.href;
    host = document.createElement("div");
    document.body.appendChild(host);
    root = createRoot(host);
    await act(async () => root!.render(<AuthUnavailable host="boss.example.test" />));
    await act(async () => host!.querySelector("button")!.click());
    expect(refresh).toHaveBeenCalledOnce();
    expect(window.location.href).toBe(before);
  });
});
