// @vitest-environment happy-dom
/**
 * Identity › People, a person's profile and Groups, rendered from the fixture org:
 *   - the list filters by search, role, status, MFA and source, and marks invited people;
 *   - the profile shows every binding (inherited ones name their group instead of Remove), the
 *     effective access WHY ("via Operator org + Release Captain", "only in acme-web", "not held"),
 *     factors, sessions, the grants Sam gave, and every subtab renders;
 *   - mirroring: an Owner's admin actions are live; a Viewer's are visible but aria-disabled,
 *     each saying "Needs <permission> · ask an Admin", and pressing one does nothing;
 *   - an IdP group is read-only, a manual one offers Add member and Remove;
 *   - every screen keeps the identity copy budget.
 */
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { IdentityProvider } from "../components/identity/identity-provider";
import { NowProvider } from "../components/identity/kit";
import { PeopleList, filterPeople } from "../app/identity/people/people-list";
import { PersonProfile, mergeEffective } from "../app/identity/people/[id]/person";
import { PERSON_TABS, type PersonTab } from "../app/identity/people/[id]/tabs";
import { GroupsList } from "../app/identity/groups/groups-list";
import { GroupDetail, impactLine } from "../app/identity/groups/[id]/group";
import type { MePermissionsResponse, MeResponse } from "../lib/identity/contract";
import { ROLE_PERMISSIONS } from "../lib/identity/permissions.gen";
import * as F from "../lib/identity/stub/fixtures";
import { resetStub, stubHandle } from "../lib/identity/stub/server";
import type { EffectiveAccess } from "../lib/identity/types";
import { identityCopyAudit } from "./_helpers/identity-copy";
import { req, signIn } from "./_helpers/identity-stub-session";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const refresh = vi.fn();
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh, push: vi.fn(), replace: vi.fn() }), useSearchParams: () => new URLSearchParams(), usePathname: () => "/" }));

const NOW = Date.parse("2026-10-06T11:00:00Z");
let root: Root | null = null;
let host: HTMLElement | null = null;

beforeEach(() => resetStub(NOW));
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
    session_id: "ses_1", session_expires_at: null, csrf_token: "c", epoch: 1, network_bound: false, banners: [] };
}

async function mount(node: React.ReactElement, role: keyof typeof ROLE_PERMISSIONS = "owner") {
  const fetchSpy = vi.fn(async () => new Response(JSON.stringify({ ok: true }), { status: 200 }));
  vi.stubGlobal("fetch", fetchSpy);
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => root!.render(
    <IdentityProvider me={me("Param Gnanaguru", [role])} org={perms(role)}>
      <NowProvider now={NOW}>{node}</NowProvider>
    </IdentityProvider>,
  ));
  await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
  return { el: host, fetchSpy };
}

function expectBudget(el: Element) {
  const a = identityCopyAudit(el);
  expect(a.labels, "labels over 6 words").toEqual([]);
  expect(a.prose, "prose over 24 words").toEqual([]);
  expect(a.tips, "tooltips over 14 words").toEqual([]);
}

function effective(scope?: string): EffectiveAccess {
  const s = signIn("param@kaappu.org");
  return stubHandle(req("GET", `/v1/identity/principals/usr_sam/effective${scope ? `?project_id=${scope}` : ""}`, { cookies: s.cookies }), NOW).body as EffectiveAccess;
}

function samProps(tab: PersonTab) {
  const people = F.people(NOW);
  return {
    person: people.find((p) => p.id === "usr_sam")!, tab,
    factors: F.factors(NOW).usr_sam, sessions: F.sessions(NOW).usr_sam,
    effective: effective(), projectEffective: [effective("acme-web")],
    roles: F.roles(NOW), delegations: F.delegations(NOW).filter((d) => d.user.id === "usr_sam"),
    keys: F.personalKeys(NOW).usr_sam, events: F.auditEvents(NOW),
  };
}

describe("Identity › People", () => {
  it("lists everyone, marks the invited, and filters", async () => {
    const people = F.people(NOW);
    const { el } = await mount(<PeopleList people={people} roles={F.roles(NOW)} />);
    expect(el.textContent).toContain("Sam Okafor");
    expect(el.textContent).toContain("Ana Costa");
    expect(el.textContent).toContain("invited");
    expect(filterPeople(people, { q: "lena", role: "", group: "", status: "", mfa: "", source: "" }).map((p) => p.id)).toEqual(["usr_lena"]);
    expect(filterPeople(people, { q: "", role: "", group: "", status: "", mfa: "off", source: "" }).map((p) => p.id)).toContain("usr_theo");
    expect(filterPeople(people, { q: "", role: "", group: "", status: "", mfa: "", source: "sso" }).map((p) => p.id).sort()).toEqual(["usr_diego", "usr_sam"]);
    expect(filterPeople(people, { q: "", role: "", group: "grp_release", status: "", mfa: "", source: "" }).map((p) => p.id)).toEqual(["usr_sam"]);
    expectBudget(el);
  });

  it("a Viewer sees Invite disabled with the reason", async () => {
    const { el } = await mount(<PeopleList people={F.people(NOW)} roles={F.roles(NOW)} />, "viewer");
    const invite = el.querySelector("[data-testid='people-invite']")!;
    expect(invite.getAttribute("aria-disabled")).toBe("true");
    expect(invite.getAttribute("title")).toBe("Needs user:invite · ask an Admin");
  });
});

describe("a person's profile", () => {
  it("explains every permission: via which role, at which scope, or why not", async () => {
    const { el } = await mount(<PersonProfile {...samProps("access")} />);
    expect(el.querySelector("h1")?.textContent).toContain("Sam Okafor");
    expect(el.textContent).toContain("Seat: release_manager");
    // inherited bindings name their group, direct ones can be removed
    expect(el.querySelector("[data-testid='binding-rb_6']")?.textContent).toContain("Via group Builders");
    expect(el.querySelector("[data-testid='remove-rb_6']")).toBeNull();
    expect(el.querySelector("[data-testid='remove-rb_3']")).not.toBeNull();
    const merge = el.querySelector("[data-testid='eff-github:merge']")!;
    expect(merge.getAttribute("data-held")).toBe("true");
    expect(merge.textContent).toContain("Operator");
    expect(merge.textContent).toContain("Release Captain");
    const prod = mergeEffective(samProps("access").effective, samProps("access").projectEffective).find((r) => r.permission === "deployment:production")!;
    expect(prod.held).toBe(true);
    expect(prod.onlyIn).toEqual(["acme-web"]);
    expect(el.textContent).toContain("Effective access, and why");
    expect(el.textContent).toContain("MacBook Pro · Touch ID");
    expect(el.textContent).toContain("Builders on acme-web");
    expectBudget(el);
  });

  it("finds a permission across every category", async () => {
    const { el } = await mount(<PersonProfile {...samProps("access")} />);
    const find = el.querySelector("[data-testid='eff-find']") as HTMLInputElement;
    await act(async () => {
      const set = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;
      set.call(find, "org:delete");
      find.dispatchEvent(new Event("input", { bubbles: true }));
    });
    const row = el.querySelector("[data-testid='eff-org:delete']")!;
    expect(row.getAttribute("data-held")).toBe("false");
    expect(row.textContent).toContain("not held");
  });

  it.each(PERSON_TABS.map((t) => t.id))("renders the %s tab within the copy budget", async (tab) => {
    const { el } = await mount(<PersonProfile {...samProps(tab)} />);
    expect(el.querySelector(`[data-tab='${tab}']`)?.getAttribute("aria-current")).toBe("page");
    expectBudget(el);
  });

  it("an Owner's admin actions are live; a Viewer's say what they need and do nothing", async () => {
    const owner = await mount(<PersonProfile {...samProps("access")} />);
    for (const id of ["person-add-access", "person-sign-out", "person-reset-mfa", "person-disable"]) {
      expect(owner.el.querySelector(`[data-testid='${id}']`)?.getAttribute("aria-disabled"), id).toBeNull();
    }
    await act(async () => { (owner.el.querySelector("[data-testid='person-disable']") as HTMLButtonElement).click(); });
    expect(document.querySelector("[data-testid='confirm-disable']")?.textContent).toContain("Service accounts they alone own");
    act(() => root?.unmount());
    host?.remove();

    const viewer = await mount(<PersonProfile {...samProps("access")} />, "viewer");
    const want: Record<string, string> = {
      "person-add-access": "role_binding:assign", "person-sign-out": "user:revoke_sessions",
      "person-reset-mfa": "user:reset_mfa", "person-disable": "user:disable",
    };
    for (const [id, p] of Object.entries(want)) {
      const b = viewer.el.querySelector(`[data-testid='${id}']`)!;
      expect(b.getAttribute("aria-disabled"), id).toBe("true");
      expect(b.getAttribute("title")).toBe(`Needs ${p} · ask an Admin`);
    }
    await act(async () => { (viewer.el.querySelector("[data-testid='person-disable']") as HTMLButtonElement).click(); });
    expect(document.querySelector("[data-testid='confirm-disable']")).toBeNull();
    expect(viewer.fetchSpy).not.toHaveBeenCalled();
  });

  it("Add access locks the roles you don't fully hold", async () => {
    const { el } = await mount(<PersonProfile {...samProps("access")} />, "admin");
    await act(async () => { (el.querySelector("[data-testid='person-add-access']") as HTMLButtonElement).click(); });
    const sheet = document.querySelector("[data-testid='add-access-sheet']")!;
    const rows = [...sheet.querySelectorAll("[role='radio'].idn-pick-row")];
    // an Admin cannot assign Owner (not listed) and holds all of Operator
    expect(rows.some((r) => r.textContent?.includes("Owner"))).toBe(false);
    const op = rows.find((r) => r.textContent?.startsWith("Operator"))!;
    expect(op.getAttribute("aria-disabled")).toBeNull();
  });
});

describe("Identity › Groups", () => {
  it("lists groups with their source and inherited roles", async () => {
    const people = F.people(NOW);
    const { el } = await mount(<GroupsList groups={F.groups(NOW, people)} />);
    expect(el.querySelector("[data-testid='group-grp_builders']")?.textContent).toContain("Okta");
    expect(el.querySelector("[data-testid='group-grp_release']")?.textContent).toContain("Release Captain");
    expectBudget(el);
  });

  it("an IdP group is read-only; a manual one can add and remove members", async () => {
    const people = F.people(NOW);
    const groups = F.groups(NOW, people);
    const idp = await mount(<GroupDetail group={groups.find((g) => g.id === "grp_builders")!} people={people} />);
    expect(idp.el.querySelector("[data-testid='idp-read-only']")).not.toBeNull();
    expect(idp.el.querySelector("[data-testid='group-add-member']")).toBeNull();
    expect(idp.el.querySelector("[data-testid^='remove-member-']")).toBeNull();
    expectBudget(idp.el);
    act(() => root?.unmount());
    host?.remove();

    const release = groups.find((g) => g.id === "grp_release")!;
    expect(impactLine(release)).toBe("2 people inherit Release Captain (org)");
    const manual = await mount(<GroupDetail group={release} people={people} />);
    expect(manual.el.querySelector("[data-testid='group-add-member']")?.getAttribute("aria-disabled")).toBeNull();
    expect(manual.el.querySelector("[data-testid='remove-member-usr_sam']")).not.toBeNull();
    expectBudget(manual.el);
  });

  it("the stub refuses adding to an IdP group and revokes a key by id", () => {
    const s = signIn("param@kaappu.org");
    const up = stubHandle(req("POST", "/v1/auth/step-up", { body: { method: "totp", code: "123456" }, cookies: s.cookies, csrf: s.csrf }), NOW);
    expect(up.status).toBe(200);
    const add = stubHandle(req("POST", "/v1/identity/groups/grp_builders/members", { body: { principal_id: "usr_mei" }, cookies: s.cookies, csrf: s.csrf }), NOW);
    expect(add.status).toBe(409);
    const revoke = stubHandle(req("POST", "/v1/identity/api-keys/key_c1aude/revoke", { cookies: s.cookies, csrf: s.csrf }), NOW);
    expect(revoke.status).toBe(200);
  });
});
