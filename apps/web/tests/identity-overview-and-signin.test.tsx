// @vitest-environment happy-dom
/**
 * Identity › Overview and the sign-in sheet, as they render from the fixture org:
 *   - the Overview mirrors access: an Owner gets every panel and an enabled Invite; a Viewer
 *     gets locked panels naming the permission and a disabled Invite saying why;
 *   - sign-in follows D33: the provider row only when configured (one provider = "Continue
 *     with X"), passkey first, the email tabs only with SMTP, the self sign-up footer only when on;
 *   - the step-up and consent sheets, and <AccessDenied>, say what they must;
 *   - every screen keeps the identity copy budget.
 */
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import { IdentityProvider } from "../components/identity/identity-provider";
import { NowProvider } from "../components/identity/kit";
import { AccessDenied } from "../components/identity/access-denied";
import { StepUpSheet } from "../components/identity/step-up-sheet";
import { ConsentSheet, consentFromBody } from "../components/identity/consent-sheet";
import { Overview } from "../app/identity/overview";
import { SignIn } from "../app/(auth)/login/sign-in";
import type { MePermissionsResponse, MeResponse } from "../lib/identity/contract";
import { PERMISSION_META, ROLE_PERMISSIONS } from "../lib/identity/permissions.gen";
import * as F from "../lib/identity/stub/fixtures";
import type { AuthOptions } from "../lib/identity/types";
import { identityCopyAudit } from "./_helpers/identity-copy";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }), useSearchParams: () => new URLSearchParams(), usePathname: () => "/" }));

const NOW = Date.parse("2026-10-06T11:00:00Z");
let root: Root | null = null;
let host: HTMLElement | null = null;

afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  root = null;
  vi.unstubAllGlobals();
});

function perms(role: keyof typeof ROLE_PERMISSIONS): MePermissionsResponse {
  return { epoch: 1, scope: "org", permissions: [...ROLE_PERMISSIONS[role]], step_up_fresh_until: null, catalogue_fingerprint: "x" };
}

function me(name: string, roles: string[]): MeResponse {
  return { principal_id: "usr_x", kind: "user", display_name: name, email: "x@acme.dev", org: F.ORG, status: "active", access_roles: roles,
    sign_in: { password: true, totp: true, passkeys: 1, recovery_codes_remaining: 10, sso_links: [] }, mfa_required: true, mfa_at: null,
    session_id: "ses_1", session_expires_at: null, csrf_token: "c", epoch: 1, network_bound: false, banners: [], avatar_seed: null };
}

async function mount(node: React.ReactElement) {
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ grantors: [{ id: "usr_priya", name: "Priya", role: "Admin", hue: 5 }] }), { status: 200 })));
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => root!.render(node));
  await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
  return host;
}

function overviewData() {
  const people = F.people(NOW);
  return {
    posture: null, people, roles: F.roles(NOW), groups: F.groups(NOW, people), serviceAccounts: F.serviceAccounts(NOW),
    agents: F.passports(NOW), events: F.auditEvents(NOW).slice(0, 6),
  };
}

describe("Identity › Overview", () => {
  it("an Owner sees every panel, and Invite is live", async () => {
    const d = overviewData();
    const el = await mount(
      <IdentityProvider me={me("Param Gnanaguru", ["owner"])} org={perms("owner")}>
        <NowProvider now={NOW}><Overview {...d} /></NowProvider>
      </IdentityProvider>,
    );
    expect(el.textContent).toContain("Who can do what,");
    expect(el.textContent).toContain("Sam Okafor");
    expect(el.textContent).toContain("Release Captain");
    expect(el.querySelector("[data-testid='overview-invite']")?.getAttribute("data-can")).toBe("yes");
    const audit = identityCopyAudit(el);
    expect(audit).toEqual({ labels: [], prose: [], tips: [] });
  });

  it("a Viewer sees what is locked and why, never an empty table", async () => {
    const el = await mount(
      <IdentityProvider me={me("Diego Alvarez", ["viewer"])} org={perms("viewer")}>
        <NowProvider now={NOW}><Overview posture={null} people={null} roles={F.roles(NOW)} groups={null} serviceAccounts={null} agents={F.passports(NOW)} events={null} /></NowProvider>
      </IdentityProvider>,
    );
    const invite = el.querySelector("[data-testid='overview-invite']")!;
    expect(invite.getAttribute("aria-disabled")).toBe("true");
    expect(invite.getAttribute("title")).toBe("Needs “Invite people” · ask an Admin");
    expect(el.querySelector("[data-testid='locked-people']")?.textContent).toContain("View people");
    expect(el.textContent).toContain(PERMISSION_META["audit:read"].title);
  });
});

const FULL: AuthOptions = {
  org_name: "Acme BOSS", host: "boss.acme.dev", smtp: true, signup: false, legacy_password: false, claim: null,
  providers: [{ id: "g", name: "Google", preset: "google" }, { id: "gh", name: "GitHub", preset: "github" }, { id: "okta", name: "Acme SSO", preset: "okta" }],
};

describe("sign-in (D33)", () => {
  it("shows the configured providers live, the passkey first and three email tabs with SMTP", async () => {
    vi.stubGlobal("PublicKeyCredential", function PublicKeyCredential() {});
    if (!navigator.credentials) Object.defineProperty(navigator, "credentials", { value: {}, configurable: true });
    const el = await mount(<SignIn options={FULL} next="/" />);
    // Google, GitHub, Microsoft, Apple always; then the configured OIDC/SAML provider.
    expect(el.querySelectorAll(".idn-idp").length).toBe(5);
    expect(el.querySelectorAll("a.idn-idp").length).toBe(3);
    expect(el.querySelector("a[aria-label='Continue with Google']")).not.toBeNull();
    expect(el.querySelector("a[aria-label='Continue with Acme SSO']")).not.toBeNull();
    expect(el.querySelector("[data-testid='sso-off-microsoft']")?.getAttribute("aria-disabled")).toBe("true");
    expect([...el.querySelectorAll("[role='tab']")].map((t) => t.textContent)).toEqual(["Password", "Magic link", "Email code"]);
    expect(el.querySelectorAll("[role='tab'][aria-disabled='true']").length).toBe(0);
    expect(el.querySelector("[data-testid='passkey-sign-in']")?.getAttribute("aria-disabled")).toBeNull();
    expect(el.querySelector("a[data-testid='forgot']")?.getAttribute("href")).toBe("/reset");
    expect(el.querySelector("[data-testid='create-account']")?.textContent).toBe("Create account");
    expect(el.querySelectorAll("h1").length).toBe(1);
    for (const input of el.querySelectorAll("input")) expect(el.querySelector(`label[for='${input.id}']`), input.id).not.toBeNull();
    expect(identityCopyAudit(el)).toEqual({ labels: [], prose: [], tips: [] });
    expect(el.textContent).not.toMatch(/phone|SMS/i);
  });

  it("with nothing configured: the same shape, each option disabled with what turns it on", async () => {
    const el = await mount(<SignIn options={{ ...FULL, providers: [], smtp: false }} next="/" />);
    expect(el.querySelectorAll("button.idn-idp[aria-disabled='true']").length).toBe(4);
    expect(el.querySelector("a.idn-idp")).toBeNull();
    expect(el.querySelector("[role='tablist']")).not.toBeNull();
    expect([...el.querySelectorAll("[role='tab'][aria-disabled='true']")].map((t) => t.textContent)).toEqual(["Magic link", "Email code"]);
    expect(el.querySelector("button[data-testid='forgot']")).not.toBeNull();
    expect(el.querySelector("a[href='/reset']")).toBeNull();
  });

  it("self sign-up makes Create account a link to /signup", async () => {
    const el = await mount(<SignIn options={{ ...FULL, signup: true }} next="/" />);
    expect(el.textContent).toContain("New here?");
    expect(el.querySelector("a[href='/signup']")?.textContent).toBe("Create account");
  });

  it("the tabs are a keyboard tablist", async () => {
    const el = await mount(<SignIn options={FULL} next="/" />);
    const first = el.querySelector<HTMLButtonElement>("[role='tab']")!;
    await act(async () => { first.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true })); });
    expect(el.querySelector("[role='tab'][aria-selected='true']")?.textContent).toBe("Magic link");
    expect(el.querySelectorAll("[role='tab'][tabindex='0']").length).toBe(1);
  });

  it("the email link answers the same words whoever asks (uniform response)", async () => {
    const el = await mount(<SignIn options={FULL} next="/" />);
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ sent: true }), { status: 202 })));
    await act(async () => { el.querySelectorAll<HTMLButtonElement>("[role='tab']")[1].click(); });
    const email = el.querySelector<HTMLInputElement>("#email")!;
    await act(async () => {
      const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;
      setter.call(email, "nobody@nowhere.dev");
      email.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await act(async () => { el.querySelector<HTMLButtonElement>("[data-testid='email-submit']")!.click(); });
    await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
    expect(el.textContent).toContain("If an account exists for that address, we sent a link.");
    expect(el.textContent).not.toContain("nobody@nowhere.dev");
  });
});

describe("the sheets and the denied page", () => {
  it("step-up offers the passkey first, then the code, and names why", async () => {
    vi.stubGlobal("PublicKeyCredential", function PublicKeyCredential() {});
    if (!navigator.credentials) Object.defineProperty(navigator, "credentials", { value: {}, configurable: true });
    const el = await mount(<StepUpSheet reason="Publishing Release Captain v4 changes access for 4 people." me={me("Sam", ["operator"])} onDone={() => undefined} />);
    const sheet = document.querySelector("[data-testid='step-up-sheet']")!;
    expect(sheet.getAttribute("role")).toBe("dialog");
    expect(sheet.getAttribute("aria-modal")).toBe("true");
    expect(sheet.textContent).toContain("Confirm with passkey");
    expect(sheet.querySelectorAll("input[aria-label^='Digit']").length).toBe(6);
    expect(identityCopyAudit(sheet)).toEqual({ labels: [], prose: [], tips: [] });
    void el;
  });

  it("consent names the agents, the abilities and the API's own duration choices", async () => {
    // The real 409 consent_required body (identity/consent.py): only delegable permissions.
    const request = consentFromBody({
      error: "consent_required", task: "Interview Scorecard", task_run_id: "pending:abc",
      agents: [{ id: "agt_forge", name: "Forge", role_key: "builder" }, { id: "agt_vera", name: "Vera", role_key: "reviewer" }],
      permissions: ["code_task:steer", "github:sync", "preview:manage"], projects: ["acme-web"],
      github: { repo: "acme/web", repos: ["acme/web"], permissions: { contents: "write", pull_requests: "write" }, summary: "push to boss/* · open PRs" },
      durations: [{ kind: "task", label: "For this build only", days: null }, { kind: "standing", label: "For 30 days", days: 30 }],
      max_days: 90,
    })!;
    await mount(<ConsentSheet onClose={() => undefined} request={request} />);
    const sheet = document.querySelector("[data-testid='consent-sheet']")!;
    expect(sheet.textContent).toContain("Let Forge and Vera act for you?");
    expect(sheet.textContent).toContain("For this build only");
    expect(sheet.textContent).toContain("For 30 days");
    expect(sheet.textContent).toContain("acme/web, push to boss/* · open PRs");
  });

  it("AccessDenied names the permission and who can grant it", async () => {
    const el = await mount(<AccessDenied permission="user:read" page="People" />);
    expect(el.textContent).toContain("You can't open People");
    expect(el.textContent).toContain("View people");
    expect(el.querySelector("[data-testid='access-denied-grantors']")?.textContent).toBe("Admins: Priya");
    expect(el.querySelector("[data-testid='request-access']")).not.toBeNull();
  });
});
