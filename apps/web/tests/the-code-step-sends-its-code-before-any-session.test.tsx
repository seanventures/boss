// @vitest-environment happy-dom
/**
 * THE CODE STEP SENDS ITS CODE, WITH NO SESSION YET (2026-11-19: sign-in stood at "Checking…").
 *
 * Between the password and the code there is no session: no /me, no intent, no consent, no
 * step-up mirror to wait for. A full six-digit code goes straight out as POST /v1/auth/mfa/totp,
 * the first and only request, and the step leaves "Checking…" as soon as it is answered (here:
 * a wrong code, so the error shows and the button is Verify again).
 */
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import { MfaStep } from "../app/(auth)/mfa-step";
import type { SignInStep } from "../lib/identity/types";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }), useSearchParams: () => new URLSearchParams(), usePathname: () => "/" }));

let root: Root | null = null;
let host: HTMLElement | null = null;
afterEach(() => { act(() => root?.unmount()); host?.remove(); root = null; vi.unstubAllGlobals(); });

const STEP: SignInStep = { next: "mfa", methods: ["totp"], who: { display_name: "Pat", email: "pat@example.test", hue: 200 } };

function typeInto(input: HTMLInputElement, value: string) {
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;
  setter.call(input, value);
  input.dispatchEvent(new Event("input", { bubbles: true }));
}

describe("the sign-in code step", () => {
  it("posts the code as its first request, with no session, and stops checking when answered", async () => {
    const calls: string[] = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      calls.push(`${(init?.method ?? "GET").toUpperCase()} ${url}`);
      return new Response(JSON.stringify({ error: "invalid_code", attempts_left: 4 }),
        { status: 401, headers: { "content-type": "application/json" } });
    }));
    const done = vi.fn();
    host = document.createElement("div");
    document.body.appendChild(host);
    root = createRoot(host);
    await act(async () => { root!.render(<MfaStep step={STEP} onDone={done} />); });
    const first = host.querySelector<HTMLInputElement>("input[autocomplete='one-time-code']")!;
    await act(async () => { typeInto(first, "123456"); });
    for (let i = 0; i < 10 && !host.querySelector("[role=alert]"); i += 1) {
      await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
    }
    expect(calls[0]).toMatch(/^POST .*\/v1\/auth\/mfa\/totp$/);
    expect(calls.every((c) => c.includes("/v1/auth/mfa/totp"))).toBe(true);
    expect(host.querySelector("[role=alert]")).not.toBeNull();
    expect(host.textContent).not.toContain("Checking…");
    expect(done).not.toHaveBeenCalled();
  });
});
