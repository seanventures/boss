// @vitest-environment happy-dom
/**
 * EVERY ROLE PICKER APPLIES THE GRANT LIMIT (round 2, R2-WEB), AND PEOPLE › REQUESTS READS THE
 * QUEUE THE VIEWER MAY READ.
 *
 * A role may be given only by someone who holds every permission in it; the API refuses the rest.
 * The new-service-account sheet offered an Owner-only custom role to an Admin, while Invite greyed
 * it. Every picker now asks one helper (lib/identity/grant-limit.ts grantReach):
 *   1. a scan: every identity screen that sends a role (`role_id:` / `role_key:` in a write)
 *      imports grantReach;
 *   2. the screens themselves, rendered as an Admin: the Owner-only role is aria-disabled with
 *      "You don't hold all of it" in New service account, Invite, and Approve an access request;
 *   3. accessRequestQueue: the org list for an org admin, the project queue for a project Admin,
 *      nothing for anyone else, and the People page reads only what it returns.
 */
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import { IdentityProvider } from "../components/identity/identity-provider";
import { InviteSheet } from "../components/identity/invite-sheet";
import { NowProvider } from "../components/identity/kit";
import { ServiceAccounts } from "../app/identity/service-accounts/service-accounts";
import { AccessRequests } from "../app/identity/people/access-requests";
import { accessRequestQueue } from "../lib/identity/access-request-queue";
import { snapshotFrom, type MePermissionsResponse, type MeResponse } from "../lib/identity/contract";
import { GRANT_LIMIT_WORDS, grantReach } from "../lib/identity/grant-limit";
import { ROLE_PERMISSIONS, type Permission } from "../lib/identity/permissions.gen";
import * as F from "../lib/identity/stub/fixtures";
import type { AccessRole } from "../lib/identity/types";
import type { AccessRequestRow } from "../lib/identity/types-people";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }), useSearchParams: () => new URLSearchParams(), usePathname: () => "/" }));

const WEB = process.cwd();
const NOW = Date.parse("2026-10-06T11:00:00Z");

describe("the scan: every screen that gives a role asks grantReach", () => {
  function walk(dir: string): string[] {
    return readdirSync(join(WEB, dir)).flatMap((name) => {
      const p = join(dir, name);
      if (statSync(join(WEB, p)).isDirectory()) return walk(p);
      return /\.tsx?$/.test(name) && !/\.test\./.test(name) ? [p] : [];
    });
  }
  // Screens that give an APP's own roles (an OIDC client's roles, F6: `role_key` of the app, which
  // carries no BOSS permission), not a BOSS role: the grant limit is about BOSS permissions.
  const APP_ROLE_SCREENS = new Set(["app/identity/app-sign-in/app-sign-in.tsx"]);
  const givers = ["app/identity", "components/identity", "app/account"].flatMap(walk)
    .filter((f) => !APP_ROLE_SCREENS.has(f))
    .filter((f) => /\b(to_)?role_(id|key):/.test(readFileSync(join(WEB, f), "utf8")));

  it("finds the pickers (invite, service accounts, groups, a person, access requests)", () => {
    expect(givers).toEqual(expect.arrayContaining([
      "components/identity/invite-sheet.tsx", "app/identity/service-accounts/service-accounts.tsx",
      "app/identity/groups/group-sheets.tsx", "app/identity/people/[id]/person.tsx", "app/identity/people/access-requests.tsx",
      // Delete a role → move its holders: that gives them the target role (to_role_id).
      "app/identity/roles/[id]/role-editor.tsx",
    ]));
  });
  it("an app-role screen sends only an app client's roles, never a BOSS role id", () => {
    for (const f of APP_ROLE_SCREENS) {
      const src = readFileSync(join(WEB, f), "utf8");
      expect(src).toMatch(/\/v1\/oidc\/clients\//);
      expect(src).not.toMatch(/\b(to_)?role_id:/);
      expect(src).not.toMatch(/\/v1\/identity\/(roles|bindings)/);
    }
  });
  it.each(givers)("%s mirrors the grant limit through grantReach", (file) => {
    const src = readFileSync(join(WEB, file), "utf8");
    expect(src).toMatch(/\bgrantReach\(/);
    expect(src, "no hand-rolled copy of the limit").not.toMatch(/permissions\.(every|filter)\(\(p\) => !?held\.has\(p\)\)/);
  });
});

describe("grantReach", () => {
  const role = { permissions: ["user:read", "identity_provider:manage"] as Permission[], description: "Reads people" };
  it("holds all of it: offered, with its description", () => {
    expect(grantReach(new Set(["user:read", "identity_provider:manage"]), role)).toMatchObject({ ok: true, reason: "Reads people" });
  });
  it("misses any of it: refused, in one set of words, naming what is missing", () => {
    expect(grantReach(new Set(["user:read"]), role)).toEqual({ ok: false, missing: ["identity_provider:manage"], reason: GRANT_LIMIT_WORDS });
  });
  it("permissions unknown: offered, the API decides", () => {
    expect(grantReach(null, role).ok).toBe(true);
  });
});

// ---- rendered, as an Admin, with an Owner-only custom role -----------------------------------
let root: Root | null = null;
let host: HTMLElement | null = null;
afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  document.querySelectorAll(".idn-sheet-wrap").forEach((n) => n.remove());
  root = null;
  vi.unstubAllGlobals();
});
const perms = (list: readonly Permission[], scope = "org"): MePermissionsResponse =>
  ({ epoch: 1, scope, permissions: [...list], step_up_fresh_until: null, catalogue_fingerprint: "x" });
const ME: MeResponse = {
  principal_id: "usr_priya", kind: "user", display_name: "Priya Raman", email: "priya@acme.dev", org: F.ORG, status: "active", access_roles: ["admin"],
  sign_in: { password: true, totp: true, passkeys: 1, recovery_codes_remaining: 10, sso_links: [] }, mfa_required: true, mfa_at: null,
  session_id: "ses_1", session_expires_at: null, csrf_token: "c", epoch: 1, network_bound: false, banners: [], avatar_seed: null,
};
const ownerOnly = ROLE_PERMISSIONS.owner.find((p) => !ROLE_PERMISSIONS.admin.includes(p))!;
const OUT_OF_REACH: AccessRole = {
  id: "arl_out", key: "out_of_reach", name: "Out of reach", description: "", builtin: false, system: false,
  permissions: ["user:read", ownerOnly], bindable_scopes: ["org", "project"], holders: 0, version: 1, cloned_from: null, hue: 3,
};
const roles = () => [...F.roles(NOW), OUT_OF_REACH];

async function settle(n = 6) {
  for (let i = 0; i < n; i += 1) await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
}
async function mount(node: React.ReactElement) {
  vi.stubGlobal("fetch", vi.fn(async () => new Response("{}", { status: 200, headers: { "content-type": "application/json" } })));
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => root!.render(
    <IdentityProvider me={ME} org={perms(ROLE_PERMISSIONS.admin)}><NowProvider now={NOW}>{node}</NowProvider></IdentityProvider>,
  ));
  await settle();
}
const button = (text: string) => [...document.querySelectorAll("button")].find((b) => b.textContent?.trim() === text) ?? null;
async function click(el: Element | null) { expect(el).not.toBeNull(); await act(async () => { (el as HTMLElement).click(); }); await settle(); }
function expectRefused(scope: string, name: string) {
  const el = [...document.querySelectorAll(`${scope} [role='radio']`)].find((b) => b.textContent?.includes(name));
  expect(el, `${name} is listed`).toBeTruthy();
  expect(el!.getAttribute("aria-disabled")).toBe("true");
  expect(el!.getAttribute("title")).toBe("You don't hold all of it");
  return el as HTMLElement;
}

describe("the pickers, as an Admin", () => {
  it("New service account greys the Owner-only role and does not pick it", async () => {
    await mount(<ServiceAccounts initial={F.serviceAccounts(NOW)} roles={roles()} people={[]} noExpiryAllowed={false} maxDays={365} selected={null} />);
    await click(button("New"));
    const el = expectRefused("[data-testid='new-sa']", "Out of reach");
    await click(el);
    expect(el.getAttribute("aria-checked")).toBe("false");
    const operator = [...document.querySelectorAll("[data-testid='new-sa'] [role='radio']")].find((b) => b.textContent === "Operator");
    expect(operator?.getAttribute("aria-disabled")).toBeNull();
  });

  it("Invite greys it the same way", async () => {
    await mount(<InviteSheet roles={roles()} onClose={() => undefined} />);
    expectRefused("[data-testid='invite-sheet']", "Out of reach");
  });

  it("Approve an access request greys a candidate role the approver does not hold", async () => {
    const request: AccessRequestRow = {
      id: "acr_1", requester_principal_id: "usr_sam", requester_name: "Sam Okafor", permission_key: "user:read", project_id: null,
      reason: null, status: "open", created_at: new Date(NOW - 60_000).toISOString(),
      candidate_roles: [{ id: OUT_OF_REACH.id, key: OUT_OF_REACH.key, name: OUT_OF_REACH.name }, { id: "arl_viewer", key: "viewer", name: "Viewer" }],
    } as AccessRequestRow;
    const withViewer = roles().map((r) => (r.key === "viewer" ? { ...r, id: "arl_viewer" } : r));
    await mount(<AccessRequests requests={[request]} roles={withViewer} />);
    await click(document.querySelector("[data-testid='request-approve-acr_1']"));
    expectRefused("[data-testid='request-approve-sheet']", "Out of reach");
    const viewer = document.querySelector("[data-testid='ar-role-viewer']");
    expect(viewer?.getAttribute("aria-checked")).toBe("true");
  });
});

describe("People › Requests reads the queue the viewer may read", () => {
  const snap = (list: readonly Permission[], scope = "org") => snapshotFrom(perms(list, scope));
  it("an org admin: the org's list", () => {
    expect(accessRequestQueue(snap(ROLE_PERMISSIONS.admin), null, "acme-web"))
      .toEqual({ path: "/v1/identity/access-requests?status=open", projectId: null });
  });
  it("a project Admin who cannot read the org's bindings: that project's queue", () => {
    expect(accessRequestQueue(snap(ROLE_PERMISSIONS.operator), snap([...ROLE_PERMISSIONS.operator, "role_binding:assign"], "project:acme-web"), "acme-web"))
      .toEqual({ path: "/v1/identity/projects/acme-web/access-requests?status=open", projectId: "acme-web" });
  });
  it("neither: nothing is fetched", () => {
    expect(accessRequestQueue(snap(ROLE_PERMISSIONS.operator), snap(ROLE_PERMISSIONS.operator, "project:acme-web"), "acme-web")).toBeNull();
    expect(accessRequestQueue(snap(ROLE_PERMISSIONS.viewer), null, "acme-web")).toBeNull();
  });
  it("identity unreadable: the org list, and the API decides", () => {
    expect(accessRequestQueue(null, null, "acme-web")?.projectId).toBeNull();
  });
  it("the People page reads only the queue accessRequestQueue chose", () => {
    const src = readFileSync(join(WEB, "app/identity/people/page.tsx"), "utf8");
    expect(src).toMatch(/accessRequestQueue\(/);
    expect(src).not.toMatch(/serverGet<[^>]*>\("\/v1\/identity\/access-requests/);
  });
  it("the dev stub answers the project queue with that project's requests only", async () => {
    const { stubHandle } = await import("../lib/identity/stub/server");
    const out = stubHandle({ method: "GET", path: "/v1/identity/projects/acme-web/access-requests", query: new URLSearchParams("status=open"),
      body: null, cookies: {}, headers: new Headers() });
    if (out.status === 200) {
      const rows = (out.body as { requests: AccessRequestRow[] }).requests;
      expect(rows.length).toBeGreaterThan(0);
      expect(rows.every((r) => r.project_id === "acme-web")).toBe(true);
    } else {
      expect(out.status, "the route exists (refused for a signed-out caller, never 404)").not.toBe(404);
    }
  });
});
