// @vitest-environment happy-dom
/**
 * Service accounts and personal keys (FX-C W4), against a recorded API:
 *   - create sends the owners you picked and binds a role you hold all of; "Create its first key
 *     now" issues the key in the same press and opens the shown-once dialog;
 *   - Edit changes owners (PATCH owner_principal_ids) and roles (POST/DELETE role-bindings);
 *   - an account with no owner offers Add owner; Delete names what stops and needs the name typed;
 *   - a personal key rotates with the chosen overlap and is created with project and network limits;
 *   - nothing on these screens shows a permission key, a raw id or an error code; the copy budget holds.
 */
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import { IdentityProvider } from "../components/identity/identity-provider";
import { NowProvider } from "../components/identity/kit";
import { ServiceAccounts } from "../app/identity/service-accounts/service-accounts";
import { MyKeys } from "../app/account/keys/my-keys";
import type { MePermissionsResponse, MeResponse } from "../lib/identity/contract";
import { ROLE_PERMISSIONS, type Permission } from "../lib/identity/permissions.gen";
import * as F from "../lib/identity/stub/fixtures";
import type { IssuedSecret, ServiceAccount } from "../lib/identity/types";
import { identityCopyAudit } from "./_helpers/identity-copy";
import { rawLeaks } from "./_helpers/identity-raw";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }), useSearchParams: () => new URLSearchParams(), usePathname: () => "/" }));

const NOW = Date.parse("2026-10-06T11:00:00Z");
let root: Root | null = null;
let host: HTMLElement | null = null;
let calls: { method: string; url: string; body: unknown }[] = [];

type Answer = { status?: number; body: unknown };
function routes(table: Record<string, Answer | ((body: unknown) => Answer)>) {
  calls = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    const method = (init?.method ?? "GET").toUpperCase();
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    calls.push({ method, url, body });
    const key = Object.keys(table).find((k) => `${method} ${url}`.startsWith(k));
    const hit = key ? table[key] : { status: 404, body: { error: "not_found" } };
    const a = typeof hit === "function" ? hit(body) : hit;
    return new Response(a.status === 204 ? null : JSON.stringify(a.body), { status: a.status ?? 200, headers: { "content-type": "application/json" } });
  }));
  vi.stubGlobal("navigator", { ...navigator, clipboard: { writeText: vi.fn(async () => undefined) } });
}

afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  document.querySelectorAll(".idn-sheet-wrap").forEach((n) => n.remove());
  root = null;
  vi.unstubAllGlobals();
});

function perms(list: readonly Permission[]): MePermissionsResponse {
  return { epoch: 1, scope: "org", permissions: [...list], step_up_fresh_until: null, catalogue_fingerprint: "x" };
}
const ME: MeResponse = {
  principal_id: "usr_param", kind: "user", display_name: "Param Gnanaguru", email: "param@acme.dev", org: F.ORG, status: "active", access_roles: ["owner"],
  sign_in: { password: true, totp: true, passkeys: 1, recovery_codes_remaining: 10, sso_links: [] }, mfa_required: true, mfa_at: null,
  session_id: "ses_1", session_expires_at: null, csrf_token: "c", epoch: 1, network_bound: false, banners: [], avatar_seed: null
};

async function settle(n = 6) {
  for (let i = 0; i < n; i += 1) await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
}
async function mount(node: React.ReactElement, held: readonly Permission[] = ROLE_PERMISSIONS.owner) {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => root!.render(
    <IdentityProvider me={ME} org={perms(held)}><NowProvider now={NOW}>{node}</NowProvider></IdentityProvider>,
  ));
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
function choose(el: Element | null, value: string) {
  const sel = el as HTMLSelectElement;
  act(() => {
    Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value")!.set!.call(sel, value);
    sel.dispatchEvent(new Event("change", { bubbles: true }));
  });
}
const button = (text: string) => [...document.querySelectorAll("button")].find((b) => b.textContent?.trim() === text) ?? null;
function noLeaks() {
  for (const el of [host!, ...document.querySelectorAll(".idn-sheet-wrap")]) {
    expect(rawLeaks(el), "raw ids, codes or jargon on screen: " + JSON.stringify(rawLeaks(el))).toEqual([]);
    const a = identityCopyAudit(el);
    expect([...a.labels, ...a.prose, ...a.tips], "copy budget: " + JSON.stringify([...a.labels, ...a.prose, ...a.tips])).toEqual([]);
  }
}

const PEOPLE = [
  { id: "usr_param", name: "Param Gnanaguru", hue: 1, email: "param@acme.dev" },
  { id: "usr_priya", name: "Priya Raman", hue: 5, email: "priya@acme.dev" },
  { id: "usr_sam", name: "Sam Okafor", hue: 6, email: "sam@acme.dev" },
];
const sas = () => F.serviceAccounts(NOW);
const roles = () => F.roles(NOW);
const ISSUED = (name: string): IssuedSecret => ({
  key: { id: "key_new123", name, owner: { id: "sva_new", name: "nightly", kind: "service_account" }, scopes: ["deployment:read"], projects: null, networks: null,
    created_at: new Date(NOW).toISOString(), last_used_at: null, last_used_ip: null, last_used_client: null, expires_at: new Date(NOW + 90 * 86_400_000).toISOString(),
    grace_until: null, rotated_from: null, status: "active" },
  secret: "boss_sak_example_only_not_a_secret", replaces: null,
});

describe("service accounts", () => {
  it("creates with the owners picked, a role you hold, and its first key shown once", async () => {
    const viewer = roles().find((r) => r.key === "viewer")!;
    routes({
      "POST /v1/identity/service-accounts/sva_new/keys": { status: 201, body: ISSUED("Nightly read") },
      "POST /v1/identity/service-accounts": { status: 201, body: { id: "sva_new", name: "nightly" } },
      "POST /v1/identity/role-bindings": { status: 201, body: { id: "rb_1" } },
      "GET /v1/identity/service-accounts": { body: { service_accounts: [] } },
      "GET /v1/identity/role-bindings": { body: { bindings: [] } },
    });
    await mount(<ServiceAccounts initial={sas()} roles={roles()} people={PEOPLE} noExpiryAllowed={false} maxDays={365} selected="sva_ci_deployer" />);
    await click(button("New"));
    type(q("#sa-name"), "nightly");
    choose(q("[data-testid='sa-owners-add']"), "usr_priya");
    await settle();
    expect(q("[data-testid='sa-owners']")!.textContent).toContain("Priya Raman");
    await click(q("[data-testid='sa-first-key']"));
    type(q("#sa-key-name"), "Nightly read");
    await click([...document.querySelectorAll("[aria-pressed]")].find((b) => b.textContent === "Read-only")!);
    noLeaks();
    await click(q("[data-testid='create-sa']"));
    await settle(10);
    const made = calls.find((c) => c.method === "POST" && c.url === "/v1/identity/service-accounts");
    expect(made?.body).toMatchObject({ name: "nightly", owner_principal_ids: ["usr_param", "usr_priya"] });
    expect(calls.find((c) => c.url === "/v1/identity/role-bindings")?.body).toMatchObject({ subject_id: "sva_new", role_id: viewer.id });
    const key = calls.find((c) => c.url === "/v1/identity/service-accounts/sva_new/keys");
    expect(key?.body).toMatchObject({ name: "Nightly read", expires_days: 90 });
    expect((key?.body as { scopes: string[] }).scopes.length).toBeGreaterThan(0);
    expect((key?.body as { scopes: string[] }).scopes.every((p) => p.endsWith(":read"))).toBe(true);
    expect(q("[data-testid='shown-once']")).not.toBeNull();
  });

  it("a role you do not hold all of cannot be picked", async () => {
    routes({});
    await mount(<ServiceAccounts initial={sas()} roles={roles()} people={PEOPLE} noExpiryAllowed={false} maxDays={365} selected={null} />,
      [...ROLE_PERMISSIONS.viewer, "service_account:manage"]);
    await click(button("New"));
    const admin = [...document.querySelectorAll("[data-testid='new-sa'] [role='radio']")].find((b) => b.textContent === "Admin");
    expect(admin?.getAttribute("aria-disabled")).toBe("true");
    expect(admin?.getAttribute("title")).toBe("You don't hold all of it");
  });

  it("edits owners and roles: one PATCH for owners, a binding per role added or removed", async () => {
    const ci = sas().find((s) => s.id === "sva_ci_deployer")!;
    const admin = roles().find((r) => r.key === "admin")!;
    routes({
      "PATCH /v1/identity/service-accounts/sva_ci_deployer": { body: ci },
      "POST /v1/identity/role-bindings": { status: 201, body: { id: "rb_9" } },
      "DELETE /v1/identity/role-bindings/": { status: 204, body: null },
      "GET /v1/identity/service-accounts": { body: { service_accounts: [ci] } },
      "GET /v1/identity/role-bindings": { body: { bindings: [] } },
      "GET /v1/identity/service-accounts/sva_ci_deployer/keys": { body: { keys: ci.keys } },
    });
    await mount(<ServiceAccounts initial={sas()} roles={roles()} people={PEOPLE} noExpiryAllowed={false} maxDays={365} selected="sva_ci_deployer" />);
    await click(q("[data-testid='edit-sa-open']"));
    // Remove Sam as an owner, add the Admin role, drop the account's current role.
    const sam = [...document.querySelectorAll("[data-testid='edit-owners'] button")].find((b) => b.getAttribute("aria-label") === "Remove Sam Okafor");
    await click(sam!);
    await click(q(`[data-testid='edit-roles'] [data-role='admin']`));
    for (const b of ci.bindings) await click(q(`[data-testid='edit-roles'] [data-role='${b.role_key}']`));
    noLeaks();
    await click(q("[data-testid='save-sa']"));
    await settle(10);
    expect(calls.find((c) => c.method === "PATCH")?.body).toMatchObject({ owner_principal_ids: ["usr_priya"] });
    expect(calls.find((c) => c.method === "POST" && c.url === "/v1/identity/role-bindings")?.body).toMatchObject({ subject_id: "sva_ci_deployer", role_id: admin.id });
    expect(calls.filter((c) => c.method === "DELETE").map((c) => c.url)).toEqual(ci.bindings.map((b) => `/v1/identity/role-bindings/${b.id}`));
  });

  it("an account with no owner offers Add owner", async () => {
    routes({});
    const orphan: ServiceAccount = { ...sas()[0], id: "sva_orphan", name: "orphan", owners: [], orphaned: true };
    await mount(<ServiceAccounts initial={[orphan]} roles={roles()} people={PEOPLE} noExpiryAllowed={false} maxDays={365} selected="sva_orphan" />);
    expect(host!.textContent).toContain("No owner");
    await click(q("[data-testid='add-owner']"));
    expect(q("[data-testid='edit-sa']")).not.toBeNull();
  });

  it("delete says the keys stop and needs the name typed back", async () => {
    routes({
      "DELETE /v1/identity/service-accounts/sva_ci_deployer": { body: { status: "revoked" } },
      "GET /v1/identity/service-accounts": { body: { service_accounts: [] } },
      "GET /v1/identity/role-bindings": { body: { bindings: [] } },
    });
    await mount(<ServiceAccounts initial={sas()} roles={roles()} people={PEOPLE} noExpiryAllowed={false} maxDays={365} selected="sva_ci_deployer" />);
    await click(q("[data-testid='delete-sa-open']"));
    expect(q("[data-testid='delete-sa']")!.textContent).toMatch(/keys? stops? working immediately/);
    expect((q("[data-testid='delete-sa-confirm']") as HTMLButtonElement).disabled).toBe(true);
    type(q("#delete-sa-name"), "ci-deployer");
    await click(q("[data-testid='delete-sa-confirm']"));
    await settle(8);
    expect(calls.some((c) => c.method === "DELETE" && c.url === "/v1/identity/service-accounts/sva_ci_deployer")).toBe(true);
  });

  it("a reader without manage sees every write greyed with the permission's name", async () => {
    routes({});
    await mount(<ServiceAccounts initial={sas()} roles={roles()} people={PEOPLE} noExpiryAllowed={false} maxDays={365} selected="sva_ci_deployer" />, ROLE_PERMISSIONS.auditor);
    for (const id of ["edit-sa-open", "delete-sa-open"]) {
      const b = q(`[data-testid='${id}']`)!;
      expect(b.getAttribute("aria-disabled"), id).toBe("true");
      expect(b.getAttribute("title")).toMatch(/^Needs “Manage service accounts” · ask an Admin$/);
    }
    noLeaks();
  });

  it("the list and an account read without raw ids, codes or jargon", async () => {
    routes({});
    for (const sa of sas()) {
      await mount(<ServiceAccounts initial={sas()} roles={roles()} people={PEOPLE} noExpiryAllowed={false} maxDays={365} selected={sa.id} />);
      noLeaks();
      act(() => root?.unmount());
      host?.remove();
    }
    await mount(<ServiceAccounts initial={[]} roles={roles()} people={PEOPLE} noExpiryAllowed={false} maxDays={365} selected={null} />);
    expect(q("[data-testid='sa-empty']")).not.toBeNull();
    expect(q("[data-testid='sa-empty-create']")).not.toBeNull();
    noLeaks();
  });
});

describe("personal keys", () => {
  const keys = () => F.personalKeys(NOW).usr_sam ?? Object.values(F.personalKeys(NOW))[0];

  it("rotates with the chosen overlap and shows the new key once", async () => {
    const k = keys().find((x) => x.status === "active")!;
    routes({
      [`POST /v1/identity/me/api-keys/${k.id}/rotate`]: { status: 201, body: { ...ISSUED(k.name), replaces: { id: k.id, stops_at: null } } },
      "GET /v1/identity/me/api-keys": { body: { keys: keys() } },
    });
    await mount(<MyKeys keys={keys()} />, ROLE_PERMISSIONS.operator);
    await click(q(`[data-testid='rotate-${k.name}']`));
    expect(q("[data-testid='rotate-key']")!.textContent).toContain(`Rotate “${k.name}”`);
    await click([...document.querySelectorAll("[data-testid='rotate-key'] [role='radio']")].find((b) => b.textContent === "24 h")!);
    noLeaks();
    await click(q("[data-testid='rotate-confirm']"));
    await settle(8);
    expect(calls.find((c) => c.url.endsWith("/rotate"))?.body).toEqual({ grace: "24h" });
    expect(q("[data-testid='shown-once']")).not.toBeNull();
  });

  it("a new key carries project and network limits", async () => {
    routes({
      "POST /v1/identity/me/api-keys": { status: 201, body: ISSUED("CLI") },
      "GET /v1/identity/me/api-keys": { body: { keys: [] } },
    });
    await mount(<MyKeys keys={[]} />, ROLE_PERMISSIONS.operator);
    expect(q("[data-testid='keys-empty']")).not.toBeNull();
    await click(q("[data-testid='new-key']"));
    type(q("#key-name"), "CLI");
    type(q("[data-testid='key-projects']"), "acme-web, boss");
    type(q("[data-testid='key-networks']"), "10.0.0.0/8");
    noLeaks();
    await click(q("[data-testid='create-key']"));
    await settle(8);
    expect(calls.find((c) => c.method === "POST")?.body).toMatchObject({ name: "CLI", projects: ["acme-web", "boss"], networks: "10.0.0.0/8", expires_days: 90 });
  });

  it("the key list reads by name, never by id", async () => {
    routes({});
    await mount(<MyKeys keys={keys()} />, ROLE_PERMISSIONS.operator);
    noLeaks();
  });
});
