/**
 * The multi-repo screens' tests share this: the mockup's sample (org acme, project Acme Shop, app
 * Storefront = contracts → api → web, deploy config infra read only) in the API's real shapes
 * (apps/api/app/routes_multirepo.py), a fetch stub at the boundary that records every call, and a
 * mount inside IdentityProvider at a chosen role (tests/gated-button.test.tsx).
 */
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { vi } from "vitest";

import { IdentityProvider } from "../../components/identity/identity-provider";
import type { MePermissionsResponse, MeResponse } from "../../lib/identity/contract";
import { ROLE_PERMISSIONS } from "../../lib/identity/permissions.gen";
import type { AppComponents, ProjectRepos, Registry, RegistryRepo } from "../../lib/multirepo";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

export type Role = keyof typeof ROLE_PERMISSIONS;

const ok = { ok: true, state: "ok" };
const bad = { ok: false, state: "denied" };
const none = { ok: null, state: "not_run" };
function repo(id: string, name: string, lang: string, extra: Partial<RegistryRepo> = {}): RegistryRepo {
  return {
    id, full_name: `acme/${name}`, description: `${name} description`, default_branch: "main", language: lang,
    private: true, archived: false, reachable: true, allowed: false, source: "app",
    checks: { read: none, write: none, pull_requests: none }, checked_at: null, state: "not_allowed", used_by: [], ...extra,
  };
}
const AT = "2026-10-10T10:00:00Z";
const working = { allowed: true, state: "working" as const, checks: { read: ok, write: ok, pull_requests: ok }, checked_at: AT,
                  used_by: [{ project_id: "p1", name: "Acme Shop" }] };

export function registryFixture(): Registry {
  const items = [
    repo("ghr_web", "web", "TypeScript", { ...working, description: "Storefront UI" }),
    repo("ghr_api", "api", "Go", { ...working, description: "Orders and carts service" }),
    repo("ghr_contracts", "contracts", "Protobuf", { ...working, used_by: [{ project_id: "p1", name: "Acme Shop" }, { project_id: "p2", name: "Payments" }] }),
    repo("ghr_infra", "infra", "HCL", { ...working, state: "read_only", checks: { read: ok, write: bad, pull_requests: bad } }),
    repo("ghr_mobile", "mobile", "Kotlin", { default_branch: "develop" }),
    repo("ghr_payments", "payments", "Go"),
    repo("ghr_docs", "docs-site", "Markdown"),
    repo("ghr_ml", "ml-experiments", "Python"),
    repo("ghr_legacy", "legacy-admin", "Ruby", { archived: true, state: "archived", default_branch: "master" }),
  ];
  return {
    org_id: "org_1", items, counts: { all: 9, allowed: 4, needs_fix: 1, not_allowed: 5 }, in_use: true,
    connection: { kind: "app", account_login: "acme", account_type: "Organization", app_slug: "boss-acme",
                  change_url: "https://github.com/organizations/acme/settings/installations/1" },
  };
}

export function projectReposFixture(): ProjectRepos {
  const pr = (id: string, name: string, lang: string, role: ProjectRepos["items"][number]["role_hint"], state: string, apps: string[]) => ({
    binding_id: id, full_name: `acme/${name}`, registry_repo_id: `ghr_${name}`, language: lang, description: null,
    role_hint: role, is_default: name === "web", base_branch: "main", state,
    used_by_apps: apps.map((a) => ({ app_key: a.toLowerCase(), app_name: a, components: [] })), checked_at: AT,
  });
  return {
    project_id: "p1", repo_mode: "multi",
    items: [
      pr("bnd_contracts", "contracts", "Protobuf", "contracts", "working", ["Storefront", "Checkout"]),
      pr("bnd_api", "api", "Go", "backend", "working", ["Storefront", "Checkout"]),
      pr("bnd_web", "web", "TypeScript", "frontend", "working", ["Storefront"]),
      pr("bnd_infra", "infra", "HCL", "deploy_config", "read_only", ["Storefront", "Checkout"]),
    ],
    addable: [{ id: "ghr_payments", full_name: "acme/payments", language: "Go", default_branch: "main", state: "working" }],
    not_allowed_count: 5, registry_in_use: true,
    apps: [
      { app_key: "storefront", app_name: "Storefront", chain: "contracts → api → web", deploys_with: "infra", components: [] },
      { app_key: "checkout", app_name: "Checkout", chain: "contracts → api", deploys_with: "infra", components: [] },
    ],
  };
}

export function componentsFixture(): AppComponents {
  const c = (name: string, binding: string, repoName: string, depends: string[], order: number, extra = {}) => ({
    id: `cmp_${name}`, name, binding_id: binding, repository: `acme/${repoName}`, path: "/", access: "write" as const,
    kind: "code" as const, depends_on: depends, ship_target: { kind: "none" as const }, base_branch: "main",
    repository_active: true, order, ...extra,
  });
  return {
    project_id: "p1", app_key: "storefront", app_name: "Storefront", repo_mode: "multi",
    components: [
      // Listed out of order on purpose: the screen draws them in build order.
      c("frontend", "bnd_web", "web", ["contracts", "backend"], 3, { path: "/apps/storefront", ship_target: { kind: "host", label: "shop.acme.com" } }),
      c("contracts", "bnd_contracts", "contracts", [], 1, { ship_target: { kind: "package", label: "npm", package: "@acme/contracts" } }),
      c("backend", "bnd_api", "api", ["contracts"], 2, { ship_target: { kind: "host", label: "api.acme.shop" } }),
    ],
    deploy_config: [{ id: "cmp_deploy", name: "deploy", binding_id: "bnd_infra", repository: "acme/infra", path: "/envs/storefront",
                      access: "read", kind: "deploy_config", depends_on: [], ship_target: { kind: "none" }, order: null }],
    waves: [["contracts"], ["backend"], ["frontend"]],
    change_sets: [],
  };
}

// ── Me, at a role ─────────────────────────────────────────────────────────────────────────────

export function identityFor(role: Role): { me: MeResponse; org: MePermissionsResponse } {
  const me = {
    principal_id: "usr_x", kind: "user", display_name: "Param", email: "p@acme.dev", org: { id: "org_1", name: "Acme" },
    status: "active", access_roles: [role], sign_in: { password: true, totp: false, passkeys: 0, recovery_codes_remaining: 0, sso_links: [] },
    mfa_required: false, mfa_at: null, session_id: "s", session_expires_at: null, csrf_token: "c", epoch: 1, network_bound: false,
    banners: [], avatar_seed: null,
  } as unknown as MeResponse;
  const org = { epoch: 1, scope: "org", permissions: [...ROLE_PERMISSIONS[role]], step_up_fresh_until: null,
                catalogue_fingerprint: "x" } as MePermissionsResponse;
  return { me, org };
}

// ── The API, at the fetch boundary ────────────────────────────────────────────────────────────

export type Call = { method: string; path: string; search: string; body: unknown };
export type Answer = { status?: number; body: unknown } | undefined;

/** Stub fetch: `answer` gets every call and returns {status, body}, or undefined for a 404. */
export function stubFetch(answer: (c: Call) => Answer): Call[] {
  const calls: Call[] = [];
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input instanceof Request ? input.url : input), "http://console.test");
    const method = (init?.method ?? (input instanceof Request ? input.method : "GET")).toUpperCase();
    let body: unknown = undefined;
    if (init?.body) { try { body = JSON.parse(String(init.body)); } catch { body = init.body; } }
    const call = { method, path: url.pathname, search: url.search, body };
    calls.push(call);
    const a = answer(call) ?? { status: 404, body: { error: "not_found", message: "Not here" } };
    return new Response(JSON.stringify(a.body), { status: a.status ?? 200, headers: { "Content-Type": "application/json" } });
  }));
  return calls;
}

// ── Mounting ──────────────────────────────────────────────────────────────────────────────────

let root: Root | null = null;
let host: HTMLElement | null = null;

export async function mountAs(node: React.ReactElement, role: Role): Promise<HTMLElement> {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  const { me, org } = identityFor(role);
  await act(async () => root!.render(<IdentityProvider me={me} org={org}>{node}</IdentityProvider>));
  await settle();
  return host;
}

export function unmount(): void {
  act(() => root?.unmount());
  host?.remove();
  root = null; host = null;
  document.body.innerHTML = "";
  vi.unstubAllGlobals();
}

export const settle = () => act(async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); await new Promise((r) => setTimeout(r, 0)); });
export const $ = (sel: string) => document.querySelector(sel) as HTMLElement | null;
export const $$ = (sel: string) => [...document.querySelectorAll(sel)] as HTMLElement[];
export const tid = (id: string) => $(`[data-testid="${id}"]`);

export async function click(el: HTMLElement | null): Promise<void> {
  if (!el) throw new Error("nothing to click");
  await act(async () => { el.click(); });
  await settle();
}

export async function choose(el: HTMLElement | null, value: string): Promise<void> {
  if (!el) throw new Error("no select");
  const select = el as HTMLSelectElement;
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value")!.set!.call(select, value);
    select.dispatchEvent(new Event("change", { bubbles: true }));
  });
  await settle();
}

export async function type(el: HTMLElement | null, value: string): Promise<void> {
  if (!el) throw new Error("no field");
  const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
  await act(async () => {
    Object.getOwnPropertyDescriptor(proto, "value")!.set!.call(el, value);
    el.dispatchEvent(new Event("input", { bubbles: true }));
  });
  await settle();
}
