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
import type {
  AppComponents, ChangeSetView, DeployPlan, Lane, ProjectRepos, Registry, RegistryRepo,
} from "../../lib/multirepo";

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

// ── A change across the three repositories (mockup 05–08: "Add saved carts") ────────────────

const steps = (states: string[]) => ["sandbox", "code", "tests", "review", "pr"].map((key, i) => ({
  key, label: ["Sandbox", "Code", "Tests", "Review", "PR"][i], state: states[i] ?? "waiting",
}));
function lane(order: number, name: string, comps: string[], extra: Partial<Lane> = {}): Lane {
  return {
    id: `lane_${name}`, order, full_name: `acme/${name}`, components: comps, wave: order, lane_state: "waiting",
    steps: steps([]), code_task_run_id: `crun_${name}`, phase: null, pr_number: null, pr_url: null,
    branch: "boss/r1482-saved-carts", base_branch: "main", head_sha: null, additions: null, deletions: null,
    changed_files: null, ci_state: null, review_state: null, findings: [], waiting_for: [], error: null,
    merged_sha: null, revert_pr_number: null, plan: [], ...extra,
  };
}
const PLAN = [
  { name: "contracts", change: "Add SavedCart and ShareLink; version 1.4.0", interface: "Cart, SavedCart, ShareLink messages", unchanged: false },
  { name: "backend", change: "PUT/GET /carts/saved, POST /carts/saved/share; owner check; migration 0042", interface: "pins @acme/contracts 1.4.0-r1482", unchanged: false },
  { name: "frontend", change: "“Save for later” button, saved cart page, share dialog", interface: null, unchanged: false },
];

/** Building: contracts has its PR, api is in Tests, web waits for api (mockup 05). */
export function changeSetFixture(extra: Partial<ChangeSetView> = {}): ChangeSetView {
  return {
    id: "rcs_1", project_id: "p1", app_key: "storefront", app_name: "Storefront", title: "Storefront: Add saved carts",
    mode: "multi", state: "building", blocked_reason: null, error: null,
    stage: { current: "build", steps: [
      { key: "plan", label: "Plan", state: "done" }, { key: "build", label: "Build · 1 of 3", state: "active" },
      { key: "review", label: "Review set", state: "waiting" }, { key: "approve", label: "You approve", state: "waiting" },
      { key: "deploy", label: "Deploy together", state: "waiting" }] },
    lanes: [
      lane(1, "contracts", ["contracts"], { lane_state: "ready", steps: steps(["done", "done", "done", "done", "done"]), pr_number: 88,
        pr_url: "https://github.com/acme/contracts/pull/88", head_sha: "aaa1111", additions: 64, deletions: 3, changed_files: 2,
        ci_state: "success", review_state: "passed", plan: [PLAN[0]],
        findings: [{ severity: "note", body: "ShareLink.expires_at has no max. Fine for now; API enforces 30 days.", path: null, line: null, author: "Sentinel", status: "open", stage: "review" }] }),
      lane(2, "api", ["backend"], { lane_state: "building", steps: steps(["done", "done", "active"]), phase: "running_tests",
        additions: 412, deletions: 58, changed_files: 11, plan: [PLAN[1]] }),
      lane(3, "web", ["frontend"], { waiting_for: ["api"], plan: [PLAN[2]] }),
    ],
    plan: { summary: "Saved carts across contracts, api and web.", components: PLAN, by: "Atlas", at: "2026-10-10T09:20:00Z" },
    cross_check: null,
    grant: { repos: [{ full_name: "acme/contracts", access: "write" }, { full_name: "acme/api", access: "write" },
                     { full_name: "acme/web", access: "write" }, { full_name: "acme/infra", access: "read" }],
             consented_by: "Param", remember: true, expires_at: null, ended_at: null, token_lifetime_minutes: 60 },
    decision: null, approved_shas: null, approved_by: null, approved_at: null, release: {},
    cost_usd: 6.4, minutes: 38, created_at: "2026-10-10T09:20:00Z", updated_at: "2026-10-10T09:58:00Z",
    ...extra,
  };
}

/** Every PR open; a high finding on api and a failed cross-repo check hold the set (mockup 06). */
export function blockedSetFixture(): ChangeSetView {
  const base = changeSetFixture();
  const ready = (l: Lane, n: number, a: number, d: number, f: number, findings: Lane["findings"] = []) => ({
    ...l, lane_state: "ready", steps: steps(["done", "done", "done", "done", "done"]), pr_number: n,
    pr_url: `https://github.com/${l.full_name}/pull/${n}`, head_sha: `sha${n}`, additions: a, deletions: d, changed_files: f,
    ci_state: "success", review_state: "passed", waiting_for: [], findings,
  });
  return {
    ...base, state: "blocked",
    blocked_reason: "Cross-repo check failed: web calls DELETE /carts/saved/{id}; the contract has no such endpoint. The whole set waits.",
    stage: { current: "review", steps: base.stage.steps.map((s) => (s.key === "plan" || s.key === "build" ? { ...s, state: "done", label: s.key === "build" ? "Build · 3 of 3" : s.label }
      : s.key === "review" ? { ...s, state: "failed" } : s)) },
    lanes: [
      ready(base.lanes[0], 88, 64, 3, 2, [{ severity: "pass", body: "Additive only. No field renumbered; old clients still decode.", author: "Vera", status: "open" }]),
      ready(base.lanes[1], 412, 412, 58, 11, [
        { severity: "high", body: "GET /carts/saved/{id} loads by id without checking the owner.", path: "handlers/saved.go", line: 48, author: "Sentinel", status: "open" },
        { severity: "medium", body: "Migration 0042 locks carts; add CONCURRENTLY to the index.", author: "Vera", status: "fixed" }]),
      ready(base.lanes[2], 977, 538, 21, 14, [{ severity: "medium", body: "Share dialog copies the link without saying it is public.", author: "Sentinel", status: "fixed" }]),
    ],
    cross_check: { verdict: "fail", passed: 4, total: 5, at: "2026-10-10T10:00:00Z", results: [
      { name: "api implements every new contract endpoint", detail: "3 of 3", pass: true, kind: "contract" },
      { name: "Versions pinned to the same contract build", detail: "api and web both use @acme/contracts 1.4.0-r1482", pass: true, kind: "pins" },
      { name: "web calls only what the contract defines", detail: "web calls DELETE /carts/saved/{id}; the contract has no such endpoint", pass: false, kind: "callers" },
      { name: "Deploy order is safe", detail: "contracts additive → api migration reversible → web behind flag", pass: true, kind: "order" },
      { name: "Every PR links the others", detail: "3 of 3 descriptions list the set", pass: true, kind: "links" },
    ] },
  };
}

/** In review: everything green, one decision asked. */
export function inReviewFixture(): ChangeSetView {
  const b = blockedSetFixture();
  return {
    ...b, state: "in_review", blocked_reason: null, cost_usd: 11.8,
    lanes: b.lanes.map((l) => ({ ...l, findings: l.findings.map((f) => ({ ...f, status: f.severity === "high" ? "fixed" : f.status })) })),
    cross_check: b.cross_check ? { ...b.cross_check, verdict: "pass", passed: 5, results: b.cross_check.results.map((r) => ({ ...r, pass: true })) } : null,
    decision: { id: "dec_cs", status: "pending", created_at: "2026-10-10T10:05:00Z" },
  };
}

export function deployPlanFixture(extra: Partial<DeployPlan> = {}): DeployPlan {
  return {
    change_set_id: "rcs_1", title: "Storefront: Add saved carts", app_name: "Storefront", app_key: "storefront",
    state: "merged", status: "planned", environment: "production", host: "eu-west-1", ready: true, problems: [],
    waves: [
      { order: 1, lane_id: "lane_contracts", components: ["contracts"], repository: "acme/contracts", kind: "merge",
        ship: { kind: "package", label: "npm", package: "@acme/contracts" }, from: "1.3.2", to: "1.4.0", status: "planned",
        steps: [{ icon: "check", text: "Publish @acme/contracts 1.4.0" }, { icon: "check", text: "Additive: old api and web still work" }],
        rollback: "Unpublish → 1.3.2 stays latest" },
      { order: 2, lane_id: "lane_api", components: ["backend"], repository: "acme/api", kind: "host", from: "v2.31.0", to: "v2.32.0",
        status: "planned", host: "eu-west-1", target_id: "tgt_api",
        steps: [{ icon: "deploy", text: "Deploy v2.32.0 to api.acme.shop" }, { icon: "health", text: "Health check must pass, else this wave rolls back" }],
        rollback: "Previous release v2.31.0" },
      { order: 3, lane_id: "lane_web", components: ["frontend"], repository: "acme/web", kind: "host", from: "2026.10.08-3", to: "2026.10.10-1",
        status: "planned", host: "eu-west-1", target_id: "tgt_web",
        steps: [{ icon: "deploy", text: "Deploy shop.acme.com" }, { icon: "health", text: "Smoke: 6 Playwright checks" }],
        rollback: "Previous build" },
    ],
    rollback: [
      { order: 3, repository: "acme/web", components: ["frontend"], text: "frontend → Previous build", status: "ready" },
      { order: 2, repository: "acme/api", components: ["backend"], text: "backend → Previous release v2.31.0", status: "ready" },
      { order: 1, repository: "acme/contracts", components: ["contracts"], text: "contracts → Unpublish → 1.3.2 stays latest", status: "ready" },
    ],
    auto_rollback: "If a deploy fails or its health check fails, every component already deployed in this release goes back to its previous release, newest first.",
    approved_by: "Param", started_at: null, finished_at: null, ...extra,
  };
}

/** The Approvals row for a set (engine change_sets._ask). */
export function changeSetDecision(extra: Record<string, unknown> = {}) {
  const prs = [
    { repository: "acme/contracts", pr_number: 88, pr_url: "https://github.com/acme/contracts/pull/88", head_sha: "sha88", additions: 64, deletions: 3, components: ["contracts"] },
    { repository: "acme/api", pr_number: 412, pr_url: "https://github.com/acme/api/pull/412", head_sha: "sha412", additions: 431, deletions: 58, components: ["backend"] },
    { repository: "acme/web", pr_number: 977, pr_url: "https://github.com/acme/web/pull/977", head_sha: "sha977", additions: 541, deletions: 21, components: ["frontend"] },
  ];
  return {
    id: "dec_cs", status: "pending", question: "Ship a change · Storefront: Storefront: Add saved carts", riskTier: 2,
    recommendation: {
      action: "approve_change_set", change_set_id: "rcs_1", app_key: "storefront", app_name: "Storefront",
      title: "Storefront: Add saved carts", pull_requests: prs, head_shas: Object.fromEntries(prs.map((p) => [p.repository, p.head_sha])),
      summary: "One decision for all 3 pull requests.",
      action_preview: { kind: "fields", content_hash: "hash_heads_v1", fields: prs.map((p) => ({ label: p.repository, value: `#${p.pr_number}` })) },
    },
    options: [], costSummary: {}, createdAt: "2026-10-10T10:05:00Z", updatedAt: "2026-10-10T10:05:00Z",
    allowedActions: [
      { action: "approve", label: "Approve and ship all 3", tone: "approve", effect: "Merges every pull request of the set.", needs: null },
      { action: "revise", label: "Send back", tone: "neutral", effect: "Every pull request gets a fix build from your note.", needs: "note" },
      { action: "reject", label: "Reject", tone: "reject", effect: "Nothing is merged or deployed.", needs: null },
    ],
    ...extra,
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

export async function mountAs(node: React.ReactElement, role: Role = "owner"): Promise<HTMLElement> {
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
