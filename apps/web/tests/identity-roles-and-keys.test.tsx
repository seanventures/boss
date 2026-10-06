// @vitest-environment happy-dom
/**
 * Access roles, the role editor, service accounts and keys, rendered against the identity stub
 * API in process (lib/identity/stub — the same decisions the PDP will make):
 *   - the editor draws the matrix by category; a toggle marks the diff and asks for the impact
 *     preview; keys the viewer does not hold are locked; a critical addition needs the role's
 *     name typed back; a Viewer gets the matrix read-only, with the reason;
 *   - a built-in role is read-only with Clone;
 *   - a new key is shown ONCE: the dialog will not close until "stored" is ticked, and after it
 *     closes no part of the secret is anywhere in the page;
 *   - every screen keeps the identity copy budget.
 */
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { IdentityProvider } from "../components/identity/identity-provider";
import { NowProvider } from "../components/identity/kit";
import { RoleEditor } from "../app/identity/roles/[id]/role-editor";
import { RoleList } from "../app/identity/roles/role-list";
import { ServiceAccounts, presetScopes } from "../app/identity/service-accounts/service-accounts";
import { MATRIX, ROLE_PERMS } from "../app/identity/roles/catalogue";
import type { MePermissionsResponse, MeResponse } from "../lib/identity/contract";
import { PERMISSION_META, ROLE_PERMISSIONS, type Permission } from "../lib/identity/permissions.gen";
import * as F from "../lib/identity/stub/fixtures";
import { resetStub, stubHandle, stubState } from "../lib/identity/stub/server";
import { identityCopyAudit } from "./_helpers/identity-copy";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }), useSearchParams: () => new URLSearchParams(), usePathname: () => "/" }));

const NOW = Date.now();
let root: Root | null = null;
let host: HTMLElement | null = null;
const calls: { method: string; path: string; body: unknown }[] = [];

function signInAs(principal: string, fresh = true) {
  stubState().live.set("tok", { principal, csrf: "c", freshUntil: fresh ? Date.now() + 600_000 : 0, methods: ["passkey"], created: Date.now() });
}

beforeEach(() => {
  resetStub(NOW);
  calls.length = 0;
  vi.stubGlobal("fetch", vi.fn(async (input: string, init?: RequestInit) => {
    const u = new URL(input, "http://console.test");
    const body = typeof init?.body === "string" ? JSON.parse(init.body) : null;
    calls.push({ method: init?.method ?? "GET", path: u.pathname, body });
    const out = stubHandle({ method: init?.method ?? "GET", path: u.pathname, query: u.searchParams, body,
      cookies: { "__Host-boss_session": "tok" }, headers: new Headers(init?.headers) });
    return new Response(JSON.stringify(out.body), { status: out.status });
  }));
  vi.stubGlobal("navigator", { ...navigator, clipboard: { writeText: vi.fn(async () => undefined) } });
});

afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  root = null;
  vi.unstubAllGlobals();
});

function perms(list: readonly Permission[]): MePermissionsResponse {
  return { epoch: 1, scope: "org", permissions: [...list], step_up_fresh_until: null, catalogue_fingerprint: "x" };
}
function me(name: string, roles: string[]): MeResponse {
  return { principal_id: "usr_x", kind: "user", display_name: name, email: "x@acme.dev", org: F.ORG, status: "active", access_roles: roles,
    sign_in: { password: true, totp: true, passkeys: 1, recovery_codes_remaining: 10, sso_links: [] }, mfa_required: true, mfa_at: null,
    session_id: "ses_1", session_expires_at: null, csrf_token: "c", epoch: 1, network_bound: false, banners: [] };
}

async function settle(n = 4) {
  for (let i = 0; i < n; i += 1) await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
}
async function wait(ms: number) { await act(async () => { await new Promise((r) => setTimeout(r, ms)); }); }

async function mount(node: React.ReactElement, held: readonly Permission[] = ROLE_PERMISSIONS.owner) {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => root!.render(
    <IdentityProvider me={me("Param Gnanaguru", ["owner"])} org={perms(held)}>
      <NowProvider now={NOW}>{node}</NowProvider>
    </IdentityProvider>,
  ));
  await settle();
  return host;
}

const q = <T extends Element = HTMLElement>(sel: string) => document.querySelector(sel) as T | null;
const click = async (el: Element | null) => { expect(el).not.toBeNull(); await act(async () => { (el as HTMLElement).click(); }); await settle(); };
function typeInto(el: HTMLInputElement, value: string) {
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;
  setter.call(el, value);
  el.dispatchEvent(new Event("input", { bubbles: true }));
}

function audit(el: Element) {
  const a = identityCopyAudit(el);
  const sheets = [...document.querySelectorAll(".idn-sheet-wrap")].map(identityCopyAudit);
  return {
    labels: [...a.labels, ...sheets.flatMap((s) => s.labels)],
    prose: [...a.prose, ...sheets.flatMap((s) => s.prose)],
    tips: [...a.tips, ...sheets.flatMap((s) => s.tips)],
  };
}

const releaseCaptain = () => F.roles(NOW).find((r) => r.key === "release_captain")!;

describe("the permission matrix's shape", () => {
  it("covers every role-holdable permission once, grouped by category then resource", () => {
    const all = MATRIX.flatMap((c) => c.resources.flatMap((r) => r.perms));
    expect(all.length).toBe(ROLE_PERMS.length);
    expect(new Set(all).size).toBe(all.length);
    expect(MATRIX.map((c) => c.name)).toContain("Factory and code");
  });
});

describe("the role editor", () => {
  it("draws categories and resources, and a toggle marks the diff and asks for the impact", async () => {
    signInAs("usr_param");
    const el = await mount(<RoleEditor role={releaseCaptain()} versions={F.roleVersions(NOW)} />);
    expect(el.textContent).toContain("Factory and code");
    expect(el.querySelector('[data-resource="code_task"]')).not.toBeNull();
    expect(q('[data-testid="unsaved"]')).toBeNull();
    await click(q('[data-perm="workflow:delete"]'));
    expect(q('[data-perm="workflow:delete"]')!.getAttribute("data-change")).toBe("add");
    expect(q('[data-testid="unsaved"]')!.textContent).toContain("v3 → v4");
    expect(q('[data-testid="diff"]')!.textContent).toContain("workflow:delete");
    await wait(300);
    await settle();
    expect(calls.some((c) => c.method === "POST" && c.path.endsWith("/preview"))).toBe(true);
    expect(q('[data-testid="impact"]')!.textContent).toMatch(/Affects/);
  });

  it("locks keys the viewer does not hold, and says so", async () => {
    signInAs("usr_priya");
    const adminHeld = ROLE_PERMISSIONS.admin;
    await mount(<RoleEditor role={releaseCaptain()} versions={[]} />, adminHeld);
    const unheld = ROLE_PERMS.find((p) => !adminHeld.includes(p))!;
    await act(async () => { const f = q<HTMLInputElement>('[data-testid="matrix-filter"]')!; typeInto(f, unheld); });
    await settle();
    const cell = q(`[data-perm="${unheld}"]`)!;
    expect(cell.getAttribute("data-locked")).toBe("true");
    expect(cell.getAttribute("title")).toBe("You don't hold this");
    const before = cell.getAttribute("aria-pressed");
    await click(cell);
    expect(q(`[data-perm="${unheld}"]`)!.getAttribute("aria-pressed")).toBe(before);
  });

  it("a critical addition needs the role's name typed back, then saves", async () => {
    signInAs("usr_param");
    await mount(<RoleEditor role={releaseCaptain()} versions={[]} />);
    await click(q('[data-perm="deployment:production"]'));
    await click(q('[data-testid="save"]'));
    const sheet = q('[data-testid="critical-confirm"]')!;
    expect(sheet).not.toBeNull();
    const go = q<HTMLButtonElement>('[data-testid="confirm-save"]')!;
    expect(go.disabled).toBe(true);
    await act(async () => typeInto(q<HTMLInputElement>("#confirm-name")!, "Release Captain"));
    await settle();
    expect(q<HTMLButtonElement>('[data-testid="confirm-save"]')!.disabled).toBe(false);
    await click(q('[data-testid="confirm-save"]'));
    await settle(6);
    const patch = calls.find((c) => c.method === "PATCH");
    // The real API (roles.py update_role) confirms by the list of critical keys added; the typed
    // name is the console's own gate. (The dev stub still wants `confirm`; it is not the contract.)
    expect(patch?.body).toMatchObject({ confirm_critical: ["deployment:production"] });
    expect((patch?.body as Record<string, unknown>).confirm).toBeUndefined();
  });

  it("a Viewer sees it read-only, with the reason, and no save", async () => {
    signInAs("usr_diego");
    await mount(<RoleEditor role={releaseCaptain()} versions={[]} />, ROLE_PERMISSIONS.viewer);
    const cell = q('[data-perm="code_task:create"]')!;
    expect(cell.getAttribute("aria-disabled")).toBe("true");
    expect(cell.getAttribute("title")).toMatch(/Needs access_role:manage/);
    await click(cell);
    expect(q('[data-testid="unsaved"]')).toBeNull();
    expect(q('[data-testid="save"]')!.getAttribute("aria-disabled")).toBe("true");
  });

  it("a built-in role is read-only, with Clone", async () => {
    signInAs("usr_param");
    const el = await mount(<RoleEditor role={F.roles(NOW)[2]} versions={[]} />);
    expect(el.textContent).toContain("Clone to customise");
    expect(q('[data-perm="code_task:create"]')!.getAttribute("aria-disabled")).toBe("true");
  });

  it("keeps the copy budget", async () => {
    signInAs("usr_param");
    const el = await mount(<RoleEditor role={releaseCaptain()} versions={F.roleVersions(NOW)} />);
    await click(q('[data-perm="deployment:production"]'));
    await wait(300);
    await settle();
    expect(audit(el)).toEqual({ labels: [], prose: [], tips: [] });
  });
});

describe("the access role list", () => {
  it("shows built-ins with a lock and Clone, and keeps the copy budget", async () => {
    const el = await mount(<RoleList roles={F.roles(NOW)} />);
    expect(el.querySelector('[data-testid="role-row-owner"]')).not.toBeNull();
    expect(el.querySelector('[data-testid="clone-operator"]')).not.toBeNull();
    expect(el.textContent).toContain("Release Captain");
    expect(audit(el)).toEqual({ labels: [], prose: [], tips: [] });
  });

  it("clones by creating a role FROM the source (the API has no /clone route)", async () => {
    signInAs("usr_param");
    await mount(<RoleList roles={F.roles(NOW)} />);
    await click(q('[data-testid="clone-admin"]'));
    await settle(6);
    const post = calls.find((c) => c.method === "POST" && c.path.startsWith("/v1/identity/access-roles"));
    expect(post?.path).toBe("/v1/identity/access-roles");
    const body = post?.body as { key: string; cloned_from: string; confirm_critical: string[] };
    expect(body.key).toBe("admin_copy");
    expect(body.key).toMatch(/^[a-z][a-z0-9_]{1,39}$/);
    expect(body.cloned_from).toBe(F.roles(NOW).find((r) => r.key === "admin")!.id);
    // Cloning Admin copies critical permissions: each is confirmed explicitly.
    expect(body.confirm_critical.length).toBeGreaterThan(0);
    expect(body.confirm_critical.every((p) => PERMISSION_META[p as Permission].critical)).toBe(true);
  });
});

describe("service accounts and keys", () => {
  it("presets never exceed what the account holds", () => {
    const held: Permission[] = ["deployment:read", "code_task:read", "workflow:read"];
    expect(presetScopes("ci", held)).toEqual(["deployment:read", "code_task:read"]);
    expect(presetScopes("read", held)).toEqual(held);
  });

  it("shows a new key once, will not close until stored, and leaves no part of it behind", async () => {
    signInAs("usr_param");
    const el = await mount(<ServiceAccounts initial={F.serviceAccounts(NOW)} roles={F.roles(NOW)} noExpiryAllowed={false} maxDays={365} selected="sva_ci_deployer" />);
    expect(el.textContent).toContain("key_7f3k2q");
    await click(q('[data-testid="new-key"]'));
    await act(async () => typeInto(q<HTMLInputElement>("#key-name")!, "Deploy bot"));
    await settle();
    await click(q('[data-testid="create-key"]'));
    await settle(6);
    const secret = q('[data-testid="secret"]')!.textContent!;
    expect(secret).toMatch(/^boss_sak_/);
    // Escape and the backdrop do nothing until "stored" is ticked; Done is disabled.
    await act(async () => { document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" })); });
    await act(async () => { q(".idn-sheet-wrap")!.dispatchEvent(new MouseEvent("mousedown", { bubbles: true })); });
    await settle();
    expect(q('[data-testid="shown-once"]')).not.toBeNull();
    expect(q<HTMLButtonElement>('[data-testid="done"]')!.disabled).toBe(true);
    await click(q('[data-testid="stored"]'));
    await click(q('[data-testid="done"]'));
    expect(q('[data-testid="shown-once"]')).toBeNull();
    const body = secret.slice("boss_sak_".length);
    const page = document.body.innerHTML;
    for (const fragment of [secret, body.slice(0, 8), body.slice(-8)]) expect(page).not.toContain(fragment);
  });

  it("rotation names the old key and its overlap", async () => {
    signInAs("usr_param");
    await mount(<ServiceAccounts initial={F.serviceAccounts(NOW)} roles={F.roles(NOW)} noExpiryAllowed={false} maxDays={365} selected="sva_ci_deployer" />);
    await click([...document.querySelectorAll("button")].find((b) => b.textContent?.trim() === "Rotate")!);
    expect(q('[data-testid="key-form"]')!.textContent).toContain("Replaces key_7f3k2q");
    await click(q('[data-testid="create-key"]'));
    await settle(6);
    const post = calls.find((c) => c.method === "POST" && c.path.endsWith("/keys"));
    expect(post?.body).toMatchObject({ replaces: "key_7f3k2q", overlap_hours: 24 });
    expect(q('[data-testid="shown-once"]')!.textContent).toContain("old key");
  });

  it("a Viewer-like reader cannot create keys and is told why; copy budget holds", async () => {
    signInAs("usr_mei");
    const el = await mount(<ServiceAccounts initial={F.serviceAccounts(NOW)} roles={F.roles(NOW)} noExpiryAllowed={false} maxDays={365} selected={null} />, ROLE_PERMISSIONS.auditor);
    const btn = q('[data-testid="new-key"]')!;
    expect(btn.getAttribute("aria-disabled")).toBe("true");
    expect(btn.getAttribute("title")).toMatch(/Needs service_account_key:manage/);
    expect(audit(el)).toEqual({ labels: [], prose: [], tips: [] });
  });

  it("the key form and the shown-once dialog keep the copy budget", async () => {
    signInAs("usr_param");
    const el = await mount(<ServiceAccounts initial={F.serviceAccounts(NOW)} roles={F.roles(NOW)} noExpiryAllowed={false} maxDays={365} selected="sva_ci_deployer" />);
    await click(q('[data-testid="new-key"]'));
    expect(audit(el)).toEqual({ labels: [], prose: [], tips: [] });
    await act(async () => typeInto(q<HTMLInputElement>("#key-name")!, "Deploy bot"));
    await click(q('[data-testid="create-key"]'));
    await settle(6);
    expect(audit(el)).toEqual({ labels: [], prose: [], tips: [] });
  });
});
