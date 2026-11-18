// @vitest-environment happy-dom
/**
 * THE CLAIM LINK WORKS IN A TAB THAT IS ALREADY ON /claim (stranger test, rc10.27).
 *
 *   - Opening the console first lands on /claim ("run boss identity setup-link"). Pasting the
 *     printed `/claim#t=…` link into that same tab changes only the fragment: no load, no remount.
 *     The page must notice the hashchange and show the claim form.
 *   - Once the claim succeeds, the spent token leaves the address bar (history.replaceState).
 *   - A fresh install sends no seats, so "Which seat were you?" is not asked; an upgrade with real
 *     seats still asks.
 */
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ClaimFlow } from "../app/(auth)/claim/claim";
import type { AuthOptions } from "../lib/identity/types";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }), useSearchParams: () => new URLSearchParams(), usePathname: () => "/" }));

let root: Root | null = null;
let host: HTMLElement | null = null;
let calls: { method: string; url: string; body: unknown }[] = [];

beforeEach(() => {
  vi.stubGlobal("PublicKeyCredential", function PublicKeyCredential() { /* present */ });
  Object.defineProperty(navigator, "credentials", { value: { get: vi.fn(), create: vi.fn() }, configurable: true });
  calls = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    const method = (init?.method ?? "GET").toUpperCase();
    calls.push({ method, url, body: init?.body ? JSON.parse(String(init.body)) : undefined });
    const body = url === "/v1/identity/me" ? { csrf_token: "csrf" } : { next: "enrol", methods: ["passkey"], who: null };
    return new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json" } });
  }));
  window.history.replaceState(null, "", "/claim");
});

afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  root = null;
  vi.unstubAllGlobals();
  window.history.replaceState(null, "", "/");
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

/** What pasting `/claim#t=…` into a tab already on /claim does: the fragment changes, nothing loads. */
async function pasteLink(fragment: string) {
  await act(async () => {
    window.history.replaceState(null, "", `/claim${fragment}`);
    window.dispatchEvent(new HashChangeEvent("hashchange"));
  });
  await settle();
}

function options(seats: string[]): AuthOptions {
  return {
    org_name: "BOSS", host: "localhost:3000", providers: [], smtp: false, signup: false, legacy_password: false,
    claim: { path: "setup_token", paths: ["setup_token"], origin: null, expires_at: new Date(Date.now() + 50 * 60_000).toISOString(), seats },
  } as AuthOptions;
}

describe("the setup link pasted into an open /claim tab", () => {
  it("shows the claim form on hashchange, with no reload", async () => {
    const el = await mount(<ClaimFlow options={options([])} />);
    expect(el.querySelector("[data-testid='claim-no-token']")).not.toBeNull();
    await pasteLink("#t=setup_abc");
    expect(el.querySelector("[data-testid='claim-no-token']")).toBeNull();
    expect(el.querySelector("#claim-name")).not.toBeNull();
    expect(el.textContent).toMatch(/setup link, expires in \d+ min/);
  });

  it("sends the pasted token, then removes it from the address bar", async () => {
    const el = await mount(<ClaimFlow options={options([])} />);
    await pasteLink("#t=setup_abc");
    type(el.querySelector("#claim-name"), "Param");
    type(el.querySelector("#claim-email"), "param@example.com");
    type(el.querySelector("#claim-pw"), "violet-otter-harbour-1987");
    await act(async () => { (el.querySelector("[data-testid='claim-submit']") as HTMLElement).click(); });
    await settle();
    const claim = calls.find((c) => c.url === "/v1/auth/setup/claim");
    expect(claim?.body).toMatchObject({ setup_token: "setup_abc" });
    expect(window.location.hash).toBe("");
    expect(window.location.pathname).toBe("/claim");
  });

  it("a failed claim keeps the link, so the person can fix the form and try again", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ error: "invalid_request" }), { status: 400 })));
    const el = await mount(<ClaimFlow options={options([])} />);
    await pasteLink("#t=setup_abc");
    type(el.querySelector("#claim-name"), "Param");
    type(el.querySelector("#claim-email"), "param@example.com");
    type(el.querySelector("#claim-pw"), "violet-otter-harbour-1987");
    await act(async () => { (el.querySelector("[data-testid='claim-submit']") as HTMLElement).click(); });
    await settle();
    expect(window.location.hash).toBe("#t=setup_abc");
  });
});

describe("Which seat were you?", () => {
  it("is not asked on a fresh install (no seats), and the claim sends none", async () => {
    window.history.replaceState(null, "", "/claim#t=setup_abc");
    const el = await mount(<ClaimFlow options={options([])} />);
    expect(el.querySelector("#claim-seat")).toBeNull();
    expect(el.textContent).not.toContain("Which seat were you?");
    type(el.querySelector("#claim-name"), "Param");
    type(el.querySelector("#claim-email"), "param@example.com");
    type(el.querySelector("#claim-pw"), "violet-otter-harbour-1987");
    await act(async () => { (el.querySelector("[data-testid='claim-submit']") as HTMLElement).click(); });
    await settle();
    expect(calls.find((c) => c.url === "/v1/auth/setup/claim")?.body).toMatchObject({ seat: null });
  });

  it("is asked on an upgrade whose seats have history", async () => {
    window.history.replaceState(null, "", "/claim#t=setup_abc");
    const el = await mount(<ClaimFlow options={options(["ceo"])} />);
    expect(el.querySelector("#claim-seat")).not.toBeNull();
  });
});
