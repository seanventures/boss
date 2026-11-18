// @vitest-environment happy-dom
/**
 * REGENERATING RECOVERY CODES CONFIRMS, THEN RE-AUTHENTICATES (stranger test r2, 2026-11-17).
 *
 * One click on "Regenerate" ended the old codes. Now: the press opens a confirmation; Cancel
 * changes nothing; "Make new codes" asks for a step-up (the existing step-up sheet) even on a
 * session that signed in a minute ago, and only a confirmed step-up posts. A step-up not
 * confirmed posts nothing.
 *
 * Breaks if the button posts directly, Cancel posts, or the post happens without a step-up.
 */
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const stepUp = vi.fn<(reason: string) => Promise<boolean>>();
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }), useSearchParams: () => new URLSearchParams(), usePathname: () => "/" }));
vi.mock("../components/identity/identity-provider", () => ({
  useIdentity: () => ({ refresh: vi.fn(), stepUp }),
  useGuardedCall: () => async <T,>(_reason: string, call: () => Promise<T>) => call(),
}));

import { AccountSecurity } from "../app/account/security";
import { NowProvider } from "../components/identity/kit";
import type { MeResponse } from "../lib/identity/contract";
import * as F from "../lib/identity/stub/fixtures";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const NOW = Date.parse("2026-10-06T11:00:00Z");
const CODES = Array.from({ length: 10 }, (_, i) => `AAAA-BBBB-CC${String(i).padStart(2, "0")}`);
const ME = {
  principal_id: "usr_sam", kind: "user", display_name: "Sam Okafor", email: "sam@acme.dev", org: F.ORG, status: "active", access_roles: ["owner"],
  sign_in: { password: true, totp: true, passkeys: 0, recovery_codes_remaining: 10, sso_links: [] }, mfa_required: true, mfa_at: null,
  session_id: "ses_1", session_expires_at: null, csrf_token: "c", epoch: 1, network_bound: false, banners: [], avatar_seed: null,
} as unknown as MeResponse;

let root: Root | null = null;
let host: HTMLElement | null = null;
let posts: string[] = [];

beforeEach(() => {
  posts = [];
  stepUp.mockReset();
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    const method = (init?.method ?? "GET").toUpperCase();
    if (method === "POST") posts.push(url);
    const body = url.includes("recovery-codes") ? { recovery_codes: CODES } : url.includes("factors") ? { factors: [] } : { csrf_token: "c" };
    return new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json" } });
  }));
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
async function mount() {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => root!.render(<NowProvider now={NOW}><AccountSecurity me={ME} factors={F.factors(NOW).usr_sam} identities={[]} host="boss.acme.dev" /></NowProvider>));
  await settle();
  return host;
}
const q = (sel: string) => document.querySelector<HTMLElement>(sel);
async function click(el: HTMLElement | null) {
  if (!el) throw new Error("nothing to click");
  await act(async () => { el.click(); });
  await settle();
}

describe("Regenerate recovery codes", () => {
  it("opens a confirmation and Cancel changes nothing", async () => {
    await mount();
    await click(q("[data-testid='regen-codes']"));
    expect(q("[data-testid='regen-confirm']")?.textContent).toContain("stop working");
    expect(posts).toEqual([]);
    expect(stepUp).not.toHaveBeenCalled();
    await click(q("[data-testid='regen-cancel']"));
    expect(q("[data-testid='regen-confirm']")).toBeNull();
    expect(posts).toEqual([]);
  });

  it("confirming asks for a step-up first; only a confirmed step-up makes new codes", async () => {
    await mount();
    stepUp.mockResolvedValueOnce(false);
    await click(q("[data-testid='regen-codes']"));
    await click(q("[data-testid='regen-confirm-go']"));
    expect(stepUp).toHaveBeenCalledTimes(1);
    expect(posts.filter((u) => u.endsWith("/v1/identity/me/recovery-codes"))).toEqual([]);

    stepUp.mockResolvedValueOnce(true);
    await click(q("[data-testid='regen-confirm-go']"));
    expect(stepUp).toHaveBeenCalledTimes(2);
    expect(posts.filter((u) => u.endsWith("/v1/identity/me/recovery-codes")).length).toBe(1);
    expect(q("[data-testid='codes-sheet']")?.textContent).toContain(CODES[9]);
  });
});
