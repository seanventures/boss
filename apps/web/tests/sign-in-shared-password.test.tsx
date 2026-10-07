// @vitest-environment happy-dom
/**
 * The upgrade promise on the sign-in page (docs/community/IDENTITY_UPGRADE.md): after the claim,
 * while the shared console password still works, "Use the shared password" signs in with no
 * email, says it is Operator and until when, and is never offered once it has stopped. The
 * console-wide banners link where each one says to go.
 */
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import { SignIn } from "../app/(auth)/login/sign-in";
import type { AuthOptions } from "../lib/identity/types";
import { identityCopyAudit } from "./_helpers/identity-copy";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }), useSearchParams: () => new URLSearchParams(), usePathname: () => "/" }));

let root: Root | null = null;
let host: HTMLElement | null = null;
const posts: { url: string; body: unknown }[] = [];

afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  root = null;
  posts.length = 0;
  vi.unstubAllGlobals();
});

async function mount(options: AuthOptions) {
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    posts.push({ url: String(url), body: init?.body ? JSON.parse(String(init.body)) : null });
    return new Response(JSON.stringify({ error: "invalid_credentials" }), { status: 401 });
  }));
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => root!.render(<SignIn options={options} next="/" />));
  return host;
}

const BASE: AuthOptions = { org_name: "Acme BOSS", host: "boss.acme.dev", providers: [], smtp: false, signup: false, legacy_password: true, claim: null };

describe("the shared console password after the claim", () => {
  it("is offered only while it works", async () => {
    const off = await mount(BASE);
    expect(off.querySelector("[data-testid='shared-password-toggle']")).toBeNull();
    act(() => root?.unmount());
    const on = await mount({ ...BASE, shared_password: { until: "2026-11-05T12:00:00Z" } });
    expect(on.querySelector("[data-testid='shared-password-toggle']")?.textContent).toBe("Use the shared password");
  });

  it("asks for no email, says Operator and until when, and posts no email", async () => {
    const el = await mount({ ...BASE, shared_password: { until: "2026-11-05T12:00:00Z" } });
    await act(async () => el.querySelector<HTMLButtonElement>("[data-testid='shared-password-toggle']")!.click());
    expect(el.querySelector("#email")).toBeNull();
    expect(el.querySelector("label[for='password']")?.textContent).toBe("Shared password");
    const note = el.querySelector("[data-testid='shared-password-note']")?.textContent ?? "";
    expect(note).toMatch(/Operator until/);
    expect(note).toContain("ask an Owner to invite you");
    const input = el.querySelector<HTMLInputElement>("#password")!;
    await act(async () => {
      const set = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;
      set.call(input, "the-shared-one");
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await act(async () => { el.querySelector("form")!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })); });
    await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
    const login = posts.find((p) => p.url.endsWith("/v1/auth/login"));
    expect(login?.body).toEqual({ email: "", password: "the-shared-one" });
    expect(identityCopyAudit(el)).toEqual({ labels: [], prose: [], tips: [] });
  });
});
