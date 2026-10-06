// @vitest-environment happy-dom
/**
 * Identity › Agents, a delegation's lineage, the security audit log and Sign-in & SSO, rendered
 * against the identity stub API (the routes S3 and S5 will serve):
 *   - the chain draws every hop, narrowing visibly: what a hop dropped is struck through;
 *   - the revoke card shows the dry-run cascade, and revoking reports what stopped;
 *   - a Viewer sees Revoke disabled with the missing permission, never a silent no-op;
 *   - the audit drawer shows the redacted event as data, the why rows, and the chain; Verify
 *     now calls the verify route; a row opens by keyboard;
 *   - every screen keeps the identity copy budget.
 */
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { IdentityProvider } from "../components/identity/identity-provider";
import { NowProvider } from "../components/identity/kit";
import { DelegationView } from "../app/identity/agents/delegations/[id]/delegation";
import { AgentsScreen } from "../app/identity/agents/agents-screen";
import { PassportScreen } from "../app/identity/agents/[id]/passport";
import { AuditScreen } from "../app/identity/audit/audit-screen";
import { EMPTY_FILTERS, parseSearch } from "../app/identity/audit/audit-query";
import { SignInSettings } from "../app/identity/sign-in/sign-in-settings";
import type { MePermissionsResponse, MeResponse } from "../lib/identity/contract";
import { resetStub, stubHandle } from "../lib/identity/stub/server";
import type { AuditPage, Delegation, DelegationDetail, Passport, Posture, Provider, SecurityPolicy } from "../lib/identity/types";
import { identityCopyAudit } from "./_helpers/identity-copy";
import { req, signIn } from "./_helpers/identity-stub-session";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }), useSearchParams: () => new URLSearchParams(), usePathname: () => "/" }));

const NOW = Date.now();
let root: Root | null = null;
let host: HTMLElement | null = null;
let calls: { method: string; path: string; body: unknown }[] = [];

type Who = { cookies: Record<string, string>; csrf: string };

function as(email: string) {
  const s = signIn(email);
  const get = <T,>(path: string) => stubHandle(req("GET", path, { cookies: s.cookies })).body as T;
  return { s, me: get<MeResponse>("/v1/identity/me"), org: get<MePermissionsResponse>("/v1/identity/me/permissions"), get };
}

/** The browser's fetch, answered by the stub with this person's session (the proxy's job). */
function wire(s: Who) {
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), "http://x");
    const method = (init?.method ?? "GET").toUpperCase();
    const body = typeof init?.body === "string" ? JSON.parse(init.body) : null;
    calls.push({ method, path: url.pathname, body });
    const headers = new Headers(init?.headers);
    const out = stubHandle({ method, path: url.pathname, query: url.searchParams, body, cookies: s.cookies, headers });
    return new Response(JSON.stringify(out.body), { status: out.status, headers: { "content-type": "application/json" } });
  }));
}

beforeEach(() => { resetStub(NOW); calls = []; });
afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  root = null;
  vi.unstubAllGlobals();
});

async function settle() {
  for (let i = 0; i < 6; i += 1) await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
}

async function mount(who: ReturnType<typeof as>, node: React.ReactElement) {
  wire(who.s);
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => root!.render(
    <IdentityProvider me={who.me} org={who.org}><NowProvider now={NOW}>{node}</NowProvider></IdentityProvider>,
  ));
  await settle();
  return host;
}

async function click(el: Element | null) {
  expect(el, "element to click").not.toBeNull();
  await act(async () => { (el as HTMLElement).click(); });
  await settle();
}

describe("a delegation's lineage", () => {
  it("draws every hop, narrowing left to right, with what a hop dropped struck through", async () => {
    const owner = as("param@kaappu.org");
    const d = owner.get<DelegationDetail>("/v1/identity/delegations/dlg_01JB7Q");
    const el = await mount(owner, <DelegationView initial={d} />);
    const hops = el.querySelectorAll(".idn-node");
    expect(hops.length).toBe(d.chain.length);
    expect(hops[1].getAttribute("data-kind")).toBe("grant");
    const counts = d.chain.map((h) => h.count);
    expect(counts[counts.length - 1]).toBeLessThan(counts[0]);
    // Something is dropped along the way, and it is shown struck, not silently missing.
    expect(el.querySelectorAll(".idn-node .idn-perm.off").length).toBeGreaterThan(0);
    expect(el.querySelector(".idn-funnel")?.getAttribute("aria-label")).toMatch(/narrow from \d+ to \d+/);
    // Token claims are shown as data, redacted.
    await click(el.querySelector("[data-testid='toggle-claims']"));
    expect(el.querySelector("[data-testid='json-view']")?.textContent).toContain("[redacted]");
    expect(identityCopyAudit(el)).toEqual({ labels: [], prose: [], tips: [] });
  });

  it("shows the cascade first, then revokes and says what stopped", async () => {
    const owner = as("param@kaappu.org");
    const d = owner.get<DelegationDetail>("/v1/identity/delegations/dlg_01JB7Q");
    const el = await mount(owner, <DelegationView initial={d} />);
    expect(calls.some((c) => c.method === "POST" && c.path.endsWith("/revoke") && (c.body as { dry_run?: boolean }).dry_run)).toBe(true);
    for (const kind of ["runs", "tokens", "github", "grants", "schedules"]) {
      expect(el.querySelector(`[data-testid='cascade-${kind}']`), kind).not.toBeNull();
    }
    const confirm = el.querySelector("[data-testid='revoke-confirm']")!;
    expect(confirm.textContent).toMatch(/Revoke and halt 3 runs/);
    await click(confirm);
    // Revoking needs no step-up for the stub's owner session (fresh from passkey) — it lands.
    expect(el.querySelector("[data-testid='revoked']")).not.toBeNull();
    expect(el.textContent).toContain("revoked");
    expect(calls.some((c) => c.method === "POST" && c.path.endsWith("/revoke") && !(c.body as { dry_run?: boolean }).dry_run)).toBe(true);
  });

  it("a Viewer sees Revoke disabled with the permission it needs", async () => {
    const owner = as("param@kaappu.org");
    const d = owner.get<DelegationDetail>("/v1/identity/delegations/dlg_01JB7Q");
    const viewer = as("diego@acme.dev");
    const el = await mount(viewer, <DelegationView initial={d} />);
    const revoke = el.querySelector("[data-testid='revoke-grant']")!;
    expect(revoke.getAttribute("aria-disabled")).toBe("true");
    expect(revoke.getAttribute("title")).toContain("Needs delegation:revoke_any");
    await click(revoke);
    // Nothing was sent: no dry run (it cannot), no revoke.
    expect(calls.filter((c) => c.path.endsWith("/revoke"))).toEqual([]);
  });
});

describe("agents and Passports", () => {
  it("lists Passports, flags the agent with no sponsor, and links every grant", async () => {
    const owner = as("param@kaappu.org");
    const el = await mount(owner, <AgentsScreen agents={owner.get<{ agents: Passport[] }>("/v1/identity/agents").agents}
      delegations={owner.get<{ delegations: Delegation[] }>("/v1/identity/delegations").delegations} />);
    expect(el.querySelector("[data-testid='passport-herald']")?.textContent).toContain("No sponsor");
    expect(el.querySelectorAll("[data-testid='delegations'] tbody tr").length).toBeGreaterThan(3);
    expect(identityCopyAudit(el)).toEqual({ labels: [], prose: [], tips: [] });
  });

  it("locks the never-delegable permissions in the ceiling editor", async () => {
    const owner = as("param@kaappu.org");
    const p = owner.get<Passport>("/v1/identity/agents/agt_forge/passport");
    const el = await mount(owner, <PassportScreen initial={p} grants={[]} />);
    await click(el.querySelector(".idn-mx-top .idn-fchip"));
    const prod = el.querySelector("[data-testid='ceil-deployment:production']")!;
    expect(prod.getAttribute("aria-disabled")).toBe("true");
    expect(prod.getAttribute("title")).toBe("Never delegable");
    await click(el.querySelector(".idn-tg[aria-pressed='false']:not([aria-disabled='true'])"));
    expect(el.querySelector("[data-testid='save-ceiling']")).not.toBeNull();
  });
});

describe("the security audit log", () => {
  it("opens the denied event with its chain, why rows and the redacted event as data", async () => {
    const owner = as("param@kaappu.org");
    const page = owner.get<AuditPage>("/v1/identity/audit");
    const el = await mount(owner, <AuditScreen initial={page} initialFilters={EMPTY_FILTERS} initialEvent={null} />);
    const drawer = el.querySelector("[data-testid='audit-drawer']")!;
    expect(drawer.textContent).toContain("authz.denied");
    expect(el.querySelector("[data-testid='drawer-why']")?.textContent).toContain("not delegable");
    expect(el.querySelector("[data-testid='drawer-chain']")?.textContent).toMatch(/Sam.*Atlas.*Forge/);
    const json = drawer.querySelector("[data-testid='json-view']")!;
    expect(json.textContent).toContain("\"authorization\": \"[redacted]\"");
    expect(json.querySelector(".r")?.textContent).toBe("\"[redacted]\"");
    expect(json.innerHTML).not.toMatch(/<script/i);
    expect(identityCopyAudit(el)).toEqual({ labels: [], prose: [], tips: [] });
  });

  it("selects a row by keyboard and verifies the chain through the route", async () => {
    const owner = as("param@kaappu.org");
    const page = owner.get<AuditPage>("/v1/identity/audit");
    const el = await mount(owner, <AuditScreen initial={page} initialFilters={EMPTY_FILTERS} initialEvent={page.events[0].id} />);
    const second = el.querySelector(`[data-testid='event-${page.events[1].id}']`) as HTMLElement;
    await act(async () => { second.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true })); });
    expect(second.getAttribute("aria-selected")).toBe("true");
    await click(el.querySelector("[data-testid='verify-chain']"));
    expect(calls.some((c) => c.method === "POST" && c.path === "/v1/identity/audit/verify")).toBe(true);
    expect(el.querySelector("[data-testid='chain-status']")?.textContent).toContain("Chain verified");
  });

  it("reads search tokens into the API's filters", () => {
    expect(parseSearch("chain:Sam outcome:denied,success rt_7R", (n) => (n === "Sam" ? "usr_sam" : null)))
      .toEqual({ q: "rt_7R", outcome: "denied,ok", actor: "usr_sam", grant: "" });
  });

  it("a Viewer cannot verify or export, and is told why", async () => {
    const viewer = as("diego@acme.dev");
    const el = await mount(viewer, <AuditScreen initial={null} initialFilters={EMPTY_FILTERS} initialEvent={null} />);
    for (const id of ["verify-chain", "export-audit"]) {
      const b = el.querySelector(`[data-testid='${id}']`)!;
      expect(b.getAttribute("aria-disabled")).toBe("true");
      expect(b.getAttribute("title")).toContain("Needs audit:export");
    }
  });
});

describe("Sign-in & SSO settings", () => {
  it("shows providers, the policy and the legacy countdown; an Admin cannot save the policy", async () => {
    const admin = as("priya@acme.dev");
    const owner = as("param@kaappu.org");
    const providers = owner.get<{ providers: Provider[] }>("/v1/identity/providers").providers;
    const policy = owner.get<SecurityPolicy>("/v1/identity/policy");
    const posture = owner.get<Posture>("/v1/identity/posture");
    const el = await mount(admin, <SignInSettings providers={providers} policy={policy} legacy={posture.legacy} />);
    expect(el.querySelector("[data-testid='provider-idp_okta']")?.textContent).toContain("JWKS ok");
    expect(el.querySelector("[data-testid='legacy-legacy_password']")?.textContent).toMatch(/\d+ days left/);
    const save = el.querySelector("[data-testid='save-policy']")!;
    expect(save.getAttribute("title")).toContain("Needs security_policy:manage");
    expect(identityCopyAudit(el)).toEqual({ labels: [], prose: [], tips: [] });
  });
});
