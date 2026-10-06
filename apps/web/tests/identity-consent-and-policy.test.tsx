// @vitest-environment happy-dom
/**
 * The console against the REAL API's shapes (S3 consent, S5 policy, S1 providers):
 *   - a 409 consent_required body becomes the sheet's request, and the grant it creates carries
 *     the task_run_id (task) or the days (standing), every permission as asked, and the GitHub
 *     scope as {repos, ...levels} — never the 409's own github object;
 *   - a schedule's consent (no task run id, standing only) offers only the standing choice;
 *   - a workflow start refused for consent asks, then re-sends the same start once;
 *   - the policy: network trust is never offered back on, "Require sign-in" PUTs only
 *     {network_mode:{active:false}}, SMTP is a status not a switch, the GitHub repo-access rule
 *     shows "automatic" and resets with null, and a save PUTs only what changed;
 *   - a provider renders from the real shape and PATCHes jit_mode.
 */
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ConsentSheet } from "../components/identity/consent-sheet";
import { IdentityProvider } from "../components/identity/identity-provider";
import { NowProvider } from "../components/identity/kit";
import { SignInSettings } from "../app/identity/sign-in/sign-in-settings";
import { DelegationView } from "../app/identity/agents/delegations/[id]/delegation";
import { detailFromGrant } from "../app/identity/agents/delegations/[id]/detail";
import type { Delegation } from "../lib/identity/types";
import { apiQuery, EMPTY_FILTERS } from "../app/identity/audit/audit-query";
import { connectOutcomeLine } from "../app/account/agents/agents-for-me";
import { CONSENT_EVENT, consentFromBody, consentFromError, grantBodyFor, type ConsentRequest } from "../lib/identity/consent";
import { policyChanges, providerView } from "../lib/identity/adapt-admin";
import { IdentityError } from "../lib/identity/api";
import type { MePermissionsResponse, MeResponse } from "../lib/identity/contract";
import { ROLE_PERMISSIONS } from "../lib/identity/permissions.gen";
import type { SecurityPolicy } from "../lib/identity/types";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }), useSearchParams: () => new URLSearchParams(), usePathname: () => "/" }));

const NOW = Date.parse("2026-10-06T11:00:00Z");

/** Verbatim shape of identity/consent.py require_authority's 409 body (a build). */
const BUILD_409 = {
  error: "consent_required", task: "Interview Scorecard", task_run_id: "pending:Ab12Cd34Ef56",
  agents: [{ id: "agt_forge", name: "Forge", role_key: "builder" }, { id: "agt_atlas", name: "Atlas", role_key: "planner" }],
  permissions: ["code_task:create", "code_task:read", "future:permission"],
  projects: ["boss-dogfood"],
  github: { repo: "acme/web", repos: ["acme/web"], permissions: { contents: "write", pull_requests: "write" },
    summary: "code write, pull requests write · pushes only to boss/*" },
  durations: [{ kind: "task", label: "For this build only", days: null }, { kind: "standing", label: "For 30 days", days: 30 }],
  max_days: 90,
};

/** A schedule's (consent.py _consent_body, scope "schedule"): no run id, standing only. */
const SCHEDULE_409 = {
  error: "consent_required", scope: "schedule", task: "Nightly triage", task_run_id: null,
  agents: [{ id: "agt_juno", name: "Juno", role_key: "triager" }], permissions: ["workflow_run:start"], projects: ["boss-dogfood"],
  github: null, durations: [{ kind: "standing", label: "For 14 days", days: 14 }], max_days: 90,
};

let root: Root | null = null;
let host: HTMLElement | null = null;
afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  root = null;
  vi.unstubAllGlobals();
});

function meFor(role: keyof typeof ROLE_PERMISSIONS): { me: MeResponse; org: MePermissionsResponse } {
  const me = {
    principal_id: "usr_param", kind: "user", display_name: "Param", email: "param@kaappu.org", hue: 4, org: { id: "org_1", name: "Acme" },
    roles: [role], sign_in: { passkeys: 1, totp: true, sso: null, recovery_codes_remaining: 10, password: false },
    csrf_token: "csrf_1", step_up_fresh_until: new Date(NOW + 600_000).toISOString(), banners: [],
  } as unknown as MeResponse;
  const org = { epoch: 1, scope: "org", permissions: [...ROLE_PERMISSIONS[role]], step_up_fresh_until: new Date(NOW + 600_000).toISOString(), catalogue_fingerprint: "x" } as MePermissionsResponse;
  return { me, org };
}

type Call = { method: string; path: string; body: unknown };
function wire(answer: (c: Call) => { status: number; body: unknown }): Call[] {
  const calls: Call[] = [];
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), "http://x");
    const c = { method: (init?.method ?? "GET").toUpperCase(), path: url.pathname + url.search, body: typeof init?.body === "string" ? JSON.parse(init.body) : null };
    calls.push(c);
    const out = answer(c);
    return new Response(JSON.stringify(out.body), { status: out.status, headers: { "content-type": "application/json" } });
  }));
  return calls;
}

async function settle() {
  for (let i = 0; i < 6; i += 1) await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
}

async function mount(node: React.ReactElement, role: keyof typeof ROLE_PERMISSIONS = "owner") {
  const { me, org } = meFor(role);
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => root!.render(<IdentityProvider me={me} org={org}><NowProvider now={NOW}>{node}</NowProvider></IdentityProvider>));
  await settle();
  return host;
}

async function click(el: Element | null) {
  expect(el, "element to click").not.toBeNull();
  await act(async () => { (el as HTMLElement).click(); });
  await settle();
}

describe("consent, from the API's 409", () => {
  it("a task grant carries the run id, every permission and the GitHub scope as {repos, ...levels}", () => {
    const req = consentFromBody(BUILD_409)!;
    expect(req.agents.map((a) => a.role_key)).toEqual(["builder", "planner"]);
    expect(grantBodyFor(req, req.durations[0])).toEqual({
      agents: ["agt_forge", "agt_atlas"], permissions: ["code_task:create", "code_task:read", "future:permission"],
      projects: ["boss-dogfood"], kind: "task", task_run_id: "pending:Ab12Cd34Ef56",
      github: { repos: ["acme/web"], contents: "write", pull_requests: "write" }, purpose: "Interview Scorecard",
    });
  });

  it("a standing grant carries the policy's days and no run id", () => {
    const req = consentFromBody(BUILD_409)!;
    const body = grantBodyFor(req, req.durations[1]);
    expect(body.kind).toBe("standing");
    expect(body.days).toBe(30);
    expect(body).not.toHaveProperty("task_run_id");
    // Never the 409's own github object: `repo`, `summary` and `permissions` are refused (422).
    expect(Object.keys(body.github ?? {})).not.toContain("summary");
    expect(Object.keys(body.github ?? {})).not.toContain("repo");
  });

  it("reads the refusal off an IdentityError, and ignores every other refusal", () => {
    const e = new IdentityError(409, "consent_required", { body: BUILD_409 });
    expect(consentFromError(e)?.task_run_id).toBe("pending:Ab12Cd34Ef56");
    expect(consentFromError(new IdentityError(409, "revoked_is_terminal", { body: { error: "revoked_is_terminal" } }))).toBeNull();
    // The workflow proxy wraps it under `consent`.
    expect(consentFromBody({ error: "consent_required needs you", consent: SCHEDULE_409 })?.scope).toBe("schedule");
  });

  it("a schedule offers only the standing grant, and creates exactly that", async () => {
    const calls = wire(() => ({ status: 201, body: { id: "dlg_new" } }));
    const decided: (string | null)[] = [];
    const req: ConsentRequest = { ...consentFromBody(SCHEDULE_409)!, onDecided: (g) => decided.push(g) };
    expect(req.durations.map((d) => d.kind)).toEqual(["standing"]);
    const el = await mount(<ConsentSheet request={req} onClose={() => undefined} />, "operator");
    expect(el.querySelector("[data-testid='consent-task']")).toBeNull();
    const standing = el.querySelector("[data-testid='consent-standing']")!;
    expect(standing.textContent).toBe("For 14 days");
    await click(standing);
    const post = calls.find((c) => c.method === "POST" && c.path === "/v1/identity/me/delegations")!;
    expect(post.body).toEqual({ agents: ["agt_juno"], permissions: ["workflow_run:start"], projects: ["boss-dogfood"], kind: "standing", days: 14, purpose: "Nightly triage" });
    expect(decided).toEqual(["dlg_new"]);
  });

  it("a task duration without a run id is never offered", () => {
    const req = consentFromBody({ ...BUILD_409, task_run_id: null })!;
    expect(req.durations.map((d) => d.kind)).toEqual(["standing"]);
  });

  it("a workflow start refused for consent asks, then re-sends the same start once", async () => {
    let n = 0;
    const calls = wire(() => {
      n += 1;
      return n === 1 ? { status: 409, body: { error: "consent_required", consent: BUILD_409 } } : { status: 200, body: { run_id: "wfr_1" } };
    });
    const on = (e: Event) => (e as CustomEvent<ConsentRequest>).detail.onDecided?.("dlg_new");
    window.addEventListener(CONSENT_EVENT, on);
    try {
      const { post } = await import("../app/workflows/delivery-api");
      const out = await post("start", { project_id: "boss-dogfood", workflow_key: "minor_feature" }, "press-1");
      expect(out).toEqual({ ok: true, data: { run_id: "wfr_1" } });
      expect(calls).toHaveLength(2);
      expect(calls[1].body).toEqual(calls[0].body);
    } finally {
      window.removeEventListener(CONSENT_EVENT, on);
    }
  });
});

const POLICY: SecurityPolicy = {
  mfa_required_roles: ["owner", "admin"], session_idle_minutes: 720, session_absolute_hours: 168, password_min_length: 12,
  breach_check: true, key_max_days: 365, key_no_expiry_for_service_accounts: false, delegation_max_days: 90,
  delegation_max_depth: 3, delegation_until_revoked: false, github_require_user_repo_access: true,
  github_require_user_repo_access_auto: true, network_mode: { active: false, ends_at: null },
  signup: { enabled: false, domains: [] }, smtp: false, public_origin: "https://boss.acme.dev", step_up_minutes: 10,
};

describe("the security policy (S5's rules)", () => {
  it("a save sends only what changed, never smtp, *_auto or network_mode", () => {
    const edited = { ...POLICY, session_idle_minutes: 60, smtp: true, github_require_user_repo_access_auto: false, network_mode: { active: true, ends_at: null } };
    expect(policyChanges(POLICY, edited)).toEqual({ session_idle_minutes: 60 });
  });

  it("never offers network trust back on; SMTP is a status; the repo-access rule says automatic", async () => {
    wire(() => ({ status: 200, body: POLICY }));
    const el = await mount(<SignInSettings providers={[]} policy={POLICY} legacy={[]} />);
    expect(el.querySelector("[data-testid='require-sign-in']")).toBeNull();
    expect(el.querySelector("#network .idn-tg")).toBeNull();
    expect(el.querySelector("[data-testid='smtp-status']")?.textContent).toContain("No email (SMTP) configured");
    expect(el.querySelector("[data-testid='smtp-status']")?.tagName).not.toBe("BUTTON");
    expect(el.querySelector("[data-testid='github-access-auto']")?.textContent).toBe("automatic");
    expect(el.querySelector("[data-testid='github-access-reset']")).toBeNull();
  });

  it("Require sign-in PUTs only {network_mode:{active:false}}, and a 409 shows the API's sentence", async () => {
    const toasts: string[] = [];
    const onToast = (e: Event) => toasts.push((e as CustomEvent<{ message: string }>).detail.message);
    window.addEventListener("boss:toast", onToast);
    const calls = wire(() => ({ status: 409, body: { error: "policy_refused", detail: "claim the first Owner before requiring sign-in" } }));
    const active = { ...POLICY, network_mode: { active: true, ends_at: null } };
    const el = await mount(<SignInSettings providers={[]} policy={active} legacy={[]} />);
    await click(el.querySelector("[data-testid='require-sign-in']"));
    window.removeEventListener("boss:toast", onToast);
    const put = calls.find((c) => c.method === "PUT")!;
    expect(put.body).toEqual({ network_mode: { active: false, ends_at: null } });
    expect(toasts).toContain("claim the first Owner before requiring sign-in");
  });

  it("a pinned repo-access rule resets to automatic with null", async () => {
    const pinned = { ...POLICY, github_require_user_repo_access: false, github_require_user_repo_access_auto: false };
    const calls = wire(() => ({ status: 200, body: POLICY }));
    const el = await mount(<SignInSettings providers={[]} policy={pinned} legacy={[]} />);
    await click(el.querySelector("[data-testid='github-access-reset']"));
    expect(calls.find((c) => c.method === "PUT")?.body).toEqual({ github_require_user_repo_access: null });
    expect(el.querySelector("[data-testid='github-access-auto']")).not.toBeNull();
  });

  it("Save policy PUTs only the edited key", async () => {
    const calls = wire(() => ({ status: 200, body: { ...POLICY, breach_check: false } }));
    const el = await mount(<SignInSettings providers={[]} policy={POLICY} legacy={[]} />);
    const breach = [...el.querySelectorAll(".idn-tg")].find((b) => b.textContent?.includes("Check breached passwords"))!;
    await click(breach);
    await click(el.querySelector("[data-testid='save-policy']"));
    expect(calls.find((c) => c.method === "PUT")?.body).toEqual({ breach_check: false });
  });
});

describe("providers, as the API serves them", () => {
  const REAL = {
    id: "idp_okta", protocol: "oidc", preset: "okta", name: "Acme SSO", issuer: "https://acme.okta.com", client_id: "0oa1",
    saml_sso_url: null, has_client_secret: true, scopes: ["openid"], allowed_algs: ["RS256"], saml_sp_entity_id: null,
    saml_certificate_count: 0, domains: ["acme.dev"], jit_mode: "off", trust_idp_mfa: false, enforce_sso: false,
    groups_claim: "groups", status: "active",
  };

  it("renders the real shape and PATCHes jit_mode", async () => {
    const calls = wire(() => ({ status: 200, body: { ...REAL, jit_mode: "domain" } }));
    const el = await mount(<SignInSettings providers={[providerView(REAL)]} policy={POLICY} legacy={[]} />);
    const card = el.querySelector("[data-testid='provider-idp_okta']")!;
    expect(card.textContent).toContain("OIDC");
    const jit = [...card.querySelectorAll(".idn-tg")].find((b) => b.textContent?.includes("Create people on first sign-in"))!;
    await click(jit);
    expect(calls.find((c) => c.method === "PATCH")?.body).toEqual({ jit_mode: "domain" });
  });

  it("maps the old stub shape too (dev only)", () => {
    const p = providerView({ id: "x", name: "X", kind: "saml", preset: "custom", status: "draft", jit: "domains", domains: [] });
    expect([p.protocol, p.preset, p.status, p.jit_mode]).toEqual(["saml", "generic", "disabled", "domain"]);
  });
});

describe("the audit query and the connect outcome", () => {
  it("asks the API for the slice the dropdowns pick", () => {
    const q = new URLSearchParams(apiQuery(EMPTY_FILTERS, { group: "signin", kind: "agents", rangeMs: 3_600_000 }, NOW, "4211"));
    expect(q.get("event")).toBe("auth.*");
    expect(q.get("kind")).toBe("agent");
    expect(q.get("since")).toBe(new Date(NOW - 3_600_000).toISOString());
    expect(q.get("cursor")).toBe("4211");
    const all = new URLSearchParams(apiQuery(EMPTY_FILTERS, { group: "credentials", kind: "keys", rangeMs: Infinity }, NOW));
    expect([...all.keys()]).toEqual([]);
  });

  it("says what GitHub's redirect back reported", () => {
    expect(connectOutcomeLine({ connected: "github", error: null })).toEqual({ tone: "ok", text: "GitHub connected." });
    expect(connectOutcomeLine({ connected: null, error: "access_denied" })?.tone).toBe("bad");
    expect(connectOutcomeLine({ connected: null, error: null })).toBeNull();
  });
});

describe("a grant's detail, from the list the API serves", () => {
  const GRANT: Delegation = {
    id: "dlg_1", kind: "standing", user: { id: "usr_param", name: "Param", hue: 4 }, agents: [{ id: "agt_forge", name: "Forge" }],
    label: "acting for me", permissions: ["code_task:read"], projects: ["boss-dogfood"], github: null,
    subdelegation: { allowed: false, to: [] }, max_depth: 0, created_at: new Date(NOW - 86_400_000).toISOString(),
    expires_at: new Date(NOW + 10 * 86_400_000).toISOString(), purpose: null, status: "active", last_used_at: null, last_used_run: null,
  };
  const PREVIEW = { halts: 1, items: [{ kind: "grants", label: "Child grants", detail: "revoked", count: 1 }, { kind: "grants", label: "Paused", detail: "paused", count: 2 }] };

  it("the grantor (a Viewer) may extend, narrow and revoke their own; the dry run is the read-only preview", async () => {
    const calls = wire((c) => ({ status: 200, body: c.path.endsWith("/cascade-preview") || c.path.endsWith("/revoke") ? PREVIEW : GRANT }));
    const el = await mount(<DelegationView initial={detailFromGrant(GRANT)} />, "viewer");
    expect(el.textContent).toContain("Extend");
    expect(calls.some((c) => c.method === "GET" && c.path === "/v1/identity/delegations/dlg_1/cascade-preview")).toBe(true);
    expect(el.querySelector("[data-testid='chain-unreported']")).not.toBeNull();
    await click(el.querySelector("[data-testid='revoke-confirm']"));
    expect(calls.some((c) => c.method === "POST" && c.path === "/v1/identity/me/delegations/dlg_1/revoke")).toBe(true);
  });

  it("a task grant has no Extend: it ends with its run", async () => {
    wire(() => ({ status: 200, body: PREVIEW }));
    const el = await mount(<DelegationView initial={detailFromGrant({ ...GRANT, kind: "task", expires_at: null })} />, "viewer");
    expect(el.textContent).not.toContain("Extend");
  });
});
