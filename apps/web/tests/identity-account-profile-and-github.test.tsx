// @vitest-environment happy-dom
/**
 * My account (FX-C W4), against a recorded API:
 *   - you rename yourself and pick an avatar colour (PATCH /v1/identity/me, nothing else);
 *   - GitHub that needs approval again offers Reconnect, and any connection can be disconnected;
 *     a failed GitHub return is said in words, never as its code;
 *   - every "choose a password" field says and enforces the policy's minimum, not a fixed 12;
 *   - the account screens show no raw ids, codes or jargon, and keep the copy budget.
 */
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import { IdentityProvider } from "../components/identity/identity-provider";
import { NowProvider } from "../components/identity/kit";
import { AccountSecurity } from "../app/account/security";
import { AgentsForMe, connectOutcomeLine } from "../app/account/agents/agents-for-me";
import { MySessions } from "../app/account/sessions/sessions";
import { ClaimFlow } from "../app/(auth)/claim/claim";
import { Reset } from "../app/(auth)/reset/reset";
import { InviteAccept } from "../app/(auth)/invite/invite";
import type { MePermissionsResponse, MeResponse } from "../lib/identity/contract";
import { ROLE_PERMISSIONS } from "../lib/identity/permissions.gen";
import * as F from "../lib/identity/stub/fixtures";
import type { AuthOptions, Connection } from "../lib/identity/types";
import { passwordMin } from "../lib/identity/types-account";
import { SignIn } from "../app/(auth)/login/sign-in";
import { Enrol } from "../app/(auth)/enrol/enrol";
import { SignUp } from "../app/(auth)/signup/signup";
import { MagicLinkLanding } from "../app/(auth)/signin/link/link";
import { StepUpSheet } from "../components/identity/step-up-sheet";
import { ConsentSheet, consentFromBody } from "../components/identity/consent-sheet";
import { identityCopyAudit } from "./_helpers/identity-copy";
import { rawLeaks } from "./_helpers/identity-raw";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }), useSearchParams: () => new URLSearchParams(), usePathname: () => "/" }));

const NOW = Date.parse("2026-10-06T11:00:00Z");
let root: Root | null = null;
let host: HTMLElement | null = null;
let calls: { method: string; url: string; body: unknown }[] = [];

type Answer = { status?: number; body: unknown };
function routes(table: Record<string, Answer>) {
  calls = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    const method = (init?.method ?? "GET").toUpperCase();
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    calls.push({ method, url, body });
    const key = Object.keys(table).find((k) => `${method} ${url}`.startsWith(k));
    const a = key ? table[key] : { status: 404, body: { error: "not_found" } };
    return new Response(JSON.stringify(a.body), { status: a.status ?? 200, headers: { "content-type": "application/json" } });
  }));
}

afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  document.querySelectorAll(".idn-sheet-wrap").forEach((n) => n.remove());
  root = null;
  vi.unstubAllGlobals();
});

const ME: MeResponse = {
  principal_id: "usr_sam", kind: "user", display_name: "Sam Okafor", email: "sam@acme.dev", org: F.ORG, status: "active", access_roles: ["operator"],
  sign_in: { password: true, totp: true, passkeys: 1, recovery_codes_remaining: 8, sso_links: [] }, mfa_required: true, mfa_at: null,
  session_id: "ses_s1", session_expires_at: null, csrf_token: "c", epoch: 1, network_bound: false, banners: [], avatar_seed: null
};
const PERMS: MePermissionsResponse = { epoch: 1, scope: "org", permissions: [...ROLE_PERMISSIONS.operator], step_up_fresh_until: null, catalogue_fingerprint: "x" };

async function settle(n = 6) {
  for (let i = 0; i < n; i += 1) await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
}
async function mount(node: React.ReactElement, provider = true) {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => root!.render(provider
    ? <IdentityProvider me={ME} org={PERMS}><NowProvider now={NOW}>{node}</NowProvider></IdentityProvider>
    : node));
  await settle();
  return host;
}
const q = <T extends Element = HTMLElement>(sel: string) => document.querySelector(sel) as T | null;
async function click(el: Element | null) { expect(el).not.toBeNull(); await act(async () => { (el as HTMLElement).click(); }); await settle(); }
function type(el: Element | null, value: string) {
  const input = el as HTMLInputElement;
  act(() => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, value);
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
}
function noLeaks() {
  for (const el of [host!, ...document.querySelectorAll(".idn-sheet-wrap")]) {
    expect(rawLeaks(el), "raw ids, codes or jargon on screen").toEqual([]);
    const a = identityCopyAudit(el);
    expect([...a.labels, ...a.prose, ...a.tips], "copy budget").toEqual([]);
  }
}

const OPTIONS: AuthOptions = { org_name: "Acme BOSS", host: "boss.acme.dev", providers: [], smtp: false, signup: false, legacy_password: false, claim: null };

describe("your name and avatar", () => {
  it("saves only your name and avatar colour, and shows them", async () => {
    routes({ "PATCH /v1/identity/me": { body: { display_name: "Sam O.", email: "sam@acme.dev", avatar_seed: "3" } }, "GET /v1/identity/me": { body: ME } });
    await mount(<AccountSecurity me={ME} factors={F.factors(NOW).usr_sam} identities={[]} host="boss.acme.dev"
      profile={{ display_name: "Sam Okafor", email: "sam@acme.dev", avatar_seed: null }} />);
    await click(q("[data-testid='edit-profile']"));
    type(q("#me-name"), "Sam O.");
    await click(q("[data-testid='tone-3']"));
    expect(q("[data-testid='tone-3']")!.getAttribute("aria-checked")).toBe("true");
    noLeaks();
    await click(q("[data-testid='profile-save']"));
    await settle(8);
    expect(calls.find((c) => c.method === "PATCH")).toMatchObject({ url: "/v1/identity/me", body: { display_name: "Sam O.", avatar_seed: "3" } });
    expect(host!.querySelector(".idn-me-row")?.textContent).toContain("Sam O.");
    expect(host!.querySelector(".idn-me-row .idn-face")?.getAttribute("data-tone")).toBe("3");
  });
});

describe("GitHub in Agents acting for me", () => {
  const base = { delegations: [], agents: null, untilRevokedAllowed: false, openNew: false };
  const gh = (status: Connection["status"]): Connection => ({ id: "con_gh1", provider: "GitHub", account: "sam-okafor", scopes: ["repo"], status, linked_at: new Date(NOW - 86_400_000).toISOString() });

  it("needs approval again: Reconnect starts the approval", async () => {
    routes({ "POST /v1/identity/me/connections": { body: { redirect: "https://github.com/login/oauth/authorize?x=1" } } });
    const assign = vi.fn();
    vi.stubGlobal("location", { ...window.location, assign });
    await mount(<AgentsForMe {...base} connections={[gh("needs_reauth")]} />);
    expect(host!.textContent).toContain("GitHub needs your approval again");
    await click(q("[data-testid='reconnect-github']"));
    await settle(6);
    expect(calls.find((c) => c.method === "POST")).toMatchObject({ url: "/v1/identity/me/connections", body: { provider: "github" } });
    noLeaks();
  });

  it("Disconnect removes the connection", async () => {
    routes({ "DELETE /v1/identity/me/connections/con_gh1": { body: { revoked: "con_gh1" } } });
    await mount(<AgentsForMe {...base} connections={[gh("active")]} />);
    await click(q("[data-testid='disconnect-github']"));
    await settle(6);
    expect(calls.some((c) => c.method === "DELETE" && c.url === "/v1/identity/me/connections/con_gh1")).toBe(true);
    expect(q("[data-testid='connect-github']")).not.toBeNull();
  });

  it("a failed return is said in words, never as its code", () => {
    expect(connectOutcomeLine({ connected: null, error: "some_internal_code" })?.text).toBe("GitHub did not connect. Try again.");
    expect(connectOutcomeLine({ connected: null, error: "access_denied" })?.text).toBe("GitHub approval was cancelled.");
  });

  it("an unread connection list is said, not drawn as not connected", async () => {
    routes({});
    await mount(<AgentsForMe {...base} connections={[]} connectionsFailed />);
    expect(q("[data-testid='connect-github']")).toBeNull();
    expect(host!.textContent).toContain("could not be read");
    noLeaks();
  });

  it("no grants yet: says what a grant is for", async () => {
    routes({});
    await mount(<AgentsForMe {...base} connections={[]} />);
    expect(q("[data-testid='grants-empty']")).not.toBeNull();
    noLeaks();
  });
});

describe("the password minimum comes from the policy", () => {
  it("passwordMin reads the policy, and falls back to 12 only when unknown", () => {
    expect(passwordMin({ password_min_length: 16 })).toBe(16);
    expect(passwordMin({})).toBe(12);
    expect(passwordMin(null)).toBe(12);
  });

  it("My account says and enforces it", async () => {
    routes({});
    await mount(<AccountSecurity me={ME} factors={F.factors(NOW).usr_sam} identities={[]} host="boss.acme.dev" passwordMin={16} />);
    expect(q("#pw-new-hint")!.textContent).toBe("16 characters or more");
    type(q("#pw-current"), "old-password-here");
    type(q("#pw-new"), "fifteen-chars-x");
    expect((q("form.idn-form button[type='submit']") as HTMLButtonElement).disabled).toBe(true);
    type(q("#pw-new"), "sixteen-chars-xy");
    expect((q("form.idn-form button[type='submit']") as HTMLButtonElement).disabled).toBe(false);
    noLeaks();
  });

  it("claim, reset and invitation say it too", async () => {
    routes({});
    await mount(<ClaimFlow options={{ ...OPTIONS, password_min_length: 14, claim: { path: "legacy_password", origin: null, expires_at: null, seats: [] } } as AuthOptions} />, false);
    const claimHint = q("#claim-pw-hint");
    if (claimHint) expect(claimHint.textContent).toBe("14 characters or more");
    act(() => root?.unmount()); host?.remove();
    window.history.replaceState(null, "", "/reset#t=rst_example");
    await mount(<Reset host="boss.acme.dev" smtp={false} email="" minPassword={10} />, false);
    expect(host!.textContent).toContain("10 characters or more");
    act(() => root?.unmount()); host?.remove();
    await mount(<InviteAccept token="inv_1" options={{ ...OPTIONS, password_min_length: 15 } as AuthOptions}
      invitation={{ email: "new@acme.dev", org: "Acme BOSS", inviter: "Priya", roles: ["Operator"], sso: [], expires_at: new Date(NOW + 86_400_000 * 5).toISOString() }} />, false);
    expect(q("#inv-pw-hint")?.textContent).toBe("15 characters or more");
  });
});

describe("sessions", () => {
  it("read without raw ids or method codes", async () => {
    routes({});
    const sessions = F.sessions(NOW).usr_sam ?? [];
    await mount(<MySessions sessions={sessions} />);
    noLeaks();
  });
});

describe("the sign-in screens", () => {
  it("read without raw ids, codes or jargon", async () => {
    routes({});
    const screens: React.ReactElement[] = [
      <SignIn key="a" options={{ ...OPTIONS, providers: [{ id: "idp_okta", name: "Okta", preset: "okta" }], smtp: true }} next="/" />,
      <SignIn key="b" options={OPTIONS} next="/" initialStep={{ next: "mfa", methods: ["totp", "passkey", "recovery"], who: null }} />,
      <ClaimFlow key="c" options={{ ...OPTIONS, claim: { path: "setup_token", origin: null, expires_at: new Date(NOW + 3_000_000).toISOString(), seats: [] } }} />,
      <Enrol key="d" host="boss.acme.dev" next="/identity" who={{ name: "Sam Okafor", email: "sam@acme.dev", hue: 6 }} />,
      <SignUp key="e" host="boss.acme.dev" org="Acme BOSS" enabled />,
      <MagicLinkLanding key="f" host="boss.acme.dev" next="/" />,
      <StepUpSheet key="g" reason="Publishing changes access for 4 people." me={ME} onDone={() => undefined} />,
      <ConsentSheet key="h" onClose={() => undefined} request={consentFromBody({
        error: "consent_required", task: "Interview Scorecard", task_run_id: "pending:abc",
        agents: [{ id: "agt_forge", name: "Forge", role_key: "builder" }], permissions: ["code_task:steer", "github:sync"], projects: ["acme-web"],
        durations: [{ kind: "task", label: "For this build only", days: null }], max_days: 90,
      })!} />,
    ];
    for (const node of screens) {
      await mount(node);
      expect(rawLeaks(document.body), node.key ?? "").toEqual([]);
      act(() => root?.unmount());
      host?.remove();
      document.querySelectorAll(".idn-sheet-wrap").forEach((n) => n.remove());
    }
  });
});
