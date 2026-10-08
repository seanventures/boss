// @vitest-environment happy-dom
/**
 * "We should be able to close this flash message — it does not automatically go away."
 *
 * The console-wide identity banners close by their policy (engine/boss_engine/identity/
 * banner_policy.py): × ("Hide this") on a dismissible one, "Got it" on a security notice, nothing
 * on a persistent one. A close hides the banner at once and tells the API, which keeps it closed
 * for that person on every device. Two or more banners stack into one "2 notices" card.
 */
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import { IdentityBanners } from "../components/identity/account-menu";
import { IdentityProvider } from "../components/identity/identity-provider";
import type { MeBanner, MePermissionsResponse, MeResponse } from "../lib/identity/contract";
import * as F from "../lib/identity/stub/fixtures";
import { identityCopyAudit } from "./_helpers/identity-copy";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }), useSearchParams: () => new URLSearchParams(), usePathname: () => "/" }));

const NEW_DEVICE: MeBanner = { id: "new_device.41", kind: "new_device", policy: "dismissible", until: null, message: "New sign-in from a new device on 2026-10-07 09:12 UTC. Not you? Sign out everywhere in Sessions and change your password." };
const RECOVERED: MeBanner = { id: "owner_recovered.40", kind: "owner_recovered", policy: "acknowledge", until: "2026-10-14T09:00:00Z", message: "An Owner account was recovered from the host shell. Check Identity › Audit." };
const MFA: MeBanner = { id: "mfa_enrolment_required", kind: "mfa_enrolment_required", policy: "persistent", until: null, message: "Your access needs a second factor. Add a passkey or an authenticator app." };

const me = (banners: MeBanner[]): MeResponse => ({
  principal_id: "usr_param", kind: "user", display_name: "Param", email: "param@acme.dev", org: F.ORG, status: "active", access_roles: ["owner"],
  sign_in: { password: true, totp: true, passkeys: 1, recovery_codes_remaining: 10, sso_links: [] }, mfa_required: true, mfa_at: null,
  session_id: "ses_1", session_expires_at: null, csrf_token: "c", epoch: 1, network_bound: false, banners, avatar_seed: null,
});
const PERMS: MePermissionsResponse = { epoch: 1, scope: "org", permissions: [], step_up_fresh_until: null, catalogue_fingerprint: "x" };

let root: Root | null = null;
let host: HTMLElement | null = null;
let calls: { method: string; url: string }[] = [];
afterEach(() => { act(() => root?.unmount()); host?.remove(); root = null; vi.unstubAllGlobals(); });

async function settle() { for (let i = 0; i < 6; i += 1) await act(async () => { await new Promise((r) => setTimeout(r, 0)); }); }
async function mount(banners: MeBanner[], status = 200) {
  calls = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    calls.push({ method: (init?.method ?? "GET").toUpperCase(), url: String(url) });
    return new Response(JSON.stringify(status === 200 ? {} : { error: "unavailable" }), { status, headers: { "content-type": "application/json" } });
  }));
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => root!.render(<IdentityProvider me={me(banners)} org={PERMS}><IdentityBanners /></IdentityProvider>));
  await settle();
  return host;
}
const shown = () => [...host!.querySelectorAll<HTMLElement>(".idn-banner[data-banner-id]")].map((el) => el.dataset.bannerId);
const posts = () => calls.filter((c) => c.method === "POST").map((c) => c.url.replace(/^https?:\/\/[^/]+/, ""));
async function click(el: Element | null) { expect(el).not.toBeNull(); await act(async () => { (el as HTMLElement).click(); }); await settle(); }

describe("closing a banner", () => {
  it("× hides a dismissible banner at once and closes it server-side", async () => {
    const el = await mount([NEW_DEVICE]);
    const x = el.querySelector<HTMLButtonElement>("[data-testid='banner-close']")!;
    expect(x.tagName).toBe("BUTTON");                              // keyboard reachable, Enter/Space
    expect(x.getAttribute("title")).toBe("Hide this");
    expect(x.getAttribute("aria-label")).toBe("Hide this");
    await click(x);
    expect(shown()).toEqual([]);
    expect(posts()).toEqual(["/v1/identity/me/banners/new_device.41/dismiss"]);
  });

  it("a security notice reads Got it", async () => {
    const el = await mount([RECOVERED]);
    const ack = el.querySelector<HTMLButtonElement>("[data-testid='banner-close']")!;
    expect(ack.textContent).toBe("Got it");
    expect(ack.getAttribute("title")).toBe("Got it");
    await click(ack);
    expect(shown()).toEqual([]);
    expect(posts()).toEqual(["/v1/identity/me/banners/owner_recovered.40/dismiss"]);
  });

  it("a persistent banner offers no close", async () => {
    const el = await mount([MFA]);
    expect(shown()).toEqual(["mfa_enrolment_required"]);
    expect(el.querySelector("[data-testid='banner-close']")).toBeNull();
  });

  it("comes back when the API could not close it", async () => {
    await mount([NEW_DEVICE], 503);
    await click(host!.querySelector("[data-testid='banner-close']"));
    expect(shown()).toEqual(["new_device.41"]);
  });
});

describe("two or more banners", () => {
  it("stack into one card that says how many, and open on demand", async () => {
    const el = await mount([MFA, RECOVERED, NEW_DEVICE]);
    const toggle = el.querySelector<HTMLButtonElement>("[data-testid='banner-stack-toggle']")!;
    expect(toggle.textContent).toBe("3 notices");
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    expect(shown()).toEqual(["mfa_enrolment_required"]);                 // the binding one, alone
    await click(toggle);
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
    expect(shown()).toEqual(["mfa_enrolment_required", "owner_recovered.40", "new_device.41"]);
    await click(el.querySelector("[data-banner-id='new_device.41'] [data-testid='banner-close']"));
    expect(shown()).toEqual(["mfa_enrolment_required", "owner_recovered.40"]);
    expect(identityCopyAudit(el)).toEqual({ labels: [], prose: [], tips: [] });
  });

  it("one banner is never a stack", async () => {
    const el = await mount([NEW_DEVICE]);
    expect(el.querySelector("[data-testid='banner-stack']")).toBeNull();
  });
});
