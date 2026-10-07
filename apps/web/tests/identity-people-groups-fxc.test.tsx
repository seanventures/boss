// @vitest-environment happy-dom
/**
 * FX-C, People and Groups: everything a community admin needs here works from the console, with
 * the API's own bodies, and never shows a raw code, id or permission key:
 *   - groups: create, rename, delete (the sheet says what members lose), bind and unbind a role
 *     at the org or one project; an empty list explains groups and offers New group; a failed read
 *     is an error, never "No groups yet";
 *   - invite: a group picker (BOSS-managed groups only) sent as group_ids;
 *   - CSV: export (spreadsheet-safe), import with a per-row check and per-row results;
 *   - a person: Mark verified for an unverified address, Add access with a project picker and an
 *     end date, parts that failed say so (a 403 says what it needs);
 *   - access requests: Approve through a role (with an end) or Deny; nothing shown when none.
 */
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { IdentityProvider } from "../components/identity/identity-provider";
import { NowProvider } from "../components/identity/kit";
import { GroupsList, groupsSub } from "../app/identity/groups/groups-list";
import { GroupDetail } from "../app/identity/groups/[id]/group";
import { groupKey } from "../app/identity/groups/group-sheets";
import { PeopleList } from "../app/identity/people/people-list";
import { PersonProfile } from "../app/identity/people/[id]/person";
import { AccessRequests } from "../app/identity/people/access-requests";
import { csvField, parseCsv, peopleCsv, readInviteCsv } from "../app/identity/people/people-csv";
import { expiresAt } from "../app/identity/people/expiry";
import { InviteSheet } from "../components/identity/invite-sheet";
import type { MePermissionsResponse, MeResponse } from "../lib/identity/contract";
import { ROLE_PERMISSIONS } from "../lib/identity/permissions.gen";
import * as F from "../lib/identity/stub/fixtures";
import { resetStub } from "../lib/identity/stub/server";
import type { AccessRequestRow } from "../lib/identity/types-people";
import { needsWords } from "../lib/identity/words";
import { identityCopyAudit } from "./_helpers/identity-copy";
import { rawLeaks } from "./_helpers/identity-raw";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const refresh = vi.fn();
const push = vi.fn();
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh, push, replace: vi.fn() }), useSearchParams: () => new URLSearchParams(), usePathname: () => "/" }));

const NOW = Date.parse("2026-10-06T11:00:00Z");
let root: Root | null = null;
let host: HTMLElement | null = null;

beforeEach(() => { resetStub(NOW); refresh.mockClear(); push.mockClear(); });
afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  for (const el of document.body.querySelectorAll(".idn-sheet-wrap")) el.remove();
  root = null;
  vi.unstubAllGlobals();
});

function perms(role: keyof typeof ROLE_PERMISSIONS): MePermissionsResponse {
  return { epoch: 1, scope: "org", permissions: [...ROLE_PERMISSIONS[role]], step_up_fresh_until: null, catalogue_fingerprint: "x" };
}
function me(roles: string[]): MeResponse {
  return { principal_id: "usr_param", kind: "user", display_name: "Param Gnanaguru", email: "param@acme.dev", org: F.ORG, status: "active", access_roles: roles,
    sign_in: { password: true, totp: true, passkeys: 1, recovery_codes_remaining: 10, sso_links: [] }, mfa_required: true, mfa_at: null,
    session_id: "ses_1", session_expires_at: null, csrf_token: "c", epoch: 1, network_bound: false, banners: [], avatar_seed: null };
}

async function mount(node: React.ReactElement, role: keyof typeof ROLE_PERMISSIONS = "owner") {
  const fetchSpy = vi.fn(async () => new Response(JSON.stringify({ ok: true, id: "grp_new" }), { status: 200 }));
  vi.stubGlobal("fetch", fetchSpy);
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => root!.render(
    <IdentityProvider me={me([role])} org={perms(role)}>
      <NowProvider now={NOW}>{node}</NowProvider>
    </IdentityProvider>,
  ));
  await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
  return { el: host, fetchSpy };
}

const settle = async () => { for (let i = 0; i < 5; i += 1) await act(async () => { await new Promise((r) => setTimeout(r, 0)); }); };
const click = async (sel: string, scope: ParentNode = document) => {
  const b = scope.querySelector(sel) as HTMLElement | null;
  if (!b) throw new Error(`no ${sel}`);
  await act(async () => { b.click(); });
  await settle();
};
function type(sel: string, value: string) {
  const el = document.querySelector(sel) as HTMLInputElement | HTMLTextAreaElement;
  const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
  act(() => {
    Object.getOwnPropertyDescriptor(proto, "value")!.set!.call(el, value);
    el.dispatchEvent(new Event("input", { bubbles: true }));
  });
}
function select(sel: string, value: string) {
  const el = document.querySelector(sel) as HTMLSelectElement;
  act(() => {
    Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value")!.set!.call(el, value);
    el.dispatchEvent(new Event("change", { bubbles: true }));
  });
}
const writes = (spy: ReturnType<typeof vi.fn>) => (spy.mock.calls as unknown as [string, RequestInit][])
  .filter(([, init]) => init?.method && init.method !== "GET")
  .map(([url, init]) => ({ url, method: init.method, body: init.body ? JSON.parse(String(init.body)) : null }));

function clean(el: Element) {
  expect(rawLeaks(el), "raw codes, ids or permission keys on screen").toEqual([]);
  const a = identityCopyAudit(el);
  expect(a.labels, "labels over 6 words").toEqual([]);
  expect(a.prose, "prose over 24 words").toEqual([]);
  expect(a.tips, "tooltips over 14 words").toEqual([]);
}

const people = () => F.people(NOW);
const groups = () => F.groups(NOW, people());

// ---- groups ---------------------------------------------------------------------------------

describe("Groups: create, rename, delete, bind and unbind", () => {
  it("the list counts synced groups only when there are some, and New group creates with a key from the name", async () => {
    expect(groupsSub(groups())).toBe("4 groups · 2 synced from Okta");
    expect(groupsSub(groups().filter((g) => g.source === "manual"))).toBe("2 groups");
    expect(groupKey("Release Captains!")).toBe("release-captains");
    const { el, fetchSpy } = await mount(<GroupsList groups={groups()} />);
    clean(el);
    await click("[data-testid='groups-create']");
    type("[data-testid='group-name']", "Release Captains");
    type("[data-testid='group-description']", "Ship it");
    await click("[data-testid='group-save']");
    expect(writes(fetchSpy)).toEqual([{ url: "/v1/identity/groups", method: "POST", body: { key: "release-captains", name: "Release Captains", description: "Ship it" } }]);
    expect(push).toHaveBeenCalledWith("/identity/groups/grp_new");
  });

  it("no groups: an explanation and New group; a failed read: an error, never 'No groups yet'", async () => {
    const empty = await mount(<GroupsList groups={[]} />);
    expect(empty.el.querySelector("[data-testid='groups-empty']")?.textContent).toContain("same access roles");
    expect(empty.el.querySelector("[data-testid='groups-empty'] [data-testid='groups-create']")).toBeTruthy();
    clean(empty.el);
    act(() => root?.unmount()); host?.remove();
    const failed = await mount(<GroupsList groups={[]} failure={{ status: 500, code: "error" }} />);
    expect(failed.el.querySelector("[data-testid='groups-error']")?.textContent).toContain("Couldn't load groups");
    expect(failed.el.textContent).not.toContain("No groups yet");
    clean(failed.el);
  });

  it("a manual group: Edit sends a PATCH, Delete says what members lose and sends a DELETE", async () => {
    const release = groups().find((g) => g.id === "grp_release")!;
    const { el, fetchSpy } = await mount(<GroupDetail group={release} people={people()} roles={F.roles(NOW)} projects={[{ id: "acme-web", name: "acme-web" }]} />);
    clean(el);
    await click("[data-testid='group-edit']");
    type("[data-testid='group-name']", "Releasers");
    await click("[data-testid='group-save']");
    await click("[data-testid='group-delete']");
    const sheet = document.querySelector("[data-testid='group-delete-sheet']")!;
    expect(sheet.textContent).toContain("Release Captain");
    expect(sheet.textContent).toMatch(/people leave|person leaves/);
    await click("[data-testid='group-delete-go']");
    expect(writes(fetchSpy)).toEqual([
      { url: "/v1/identity/groups/grp_release", method: "PATCH", body: { name: "Releasers", description: release.description } },
      { url: "/v1/identity/groups/grp_release", method: "DELETE", body: null },
    ]);
    expect(push).toHaveBeenCalledWith("/identity/groups");
  });

  it("binds a role at the org and in one project (the project's own route), and unbinds", async () => {
    const oncall = groups().find((g) => g.id === "grp_oncall")!;
    const release = groups().find((g) => g.id === "grp_release")!;
    const viewer = F.roles(NOW).find((r) => r.key === "viewer")!;
    const { el, fetchSpy } = await mount(<GroupDetail group={oncall} people={people()} roles={F.roles(NOW)} projects={[{ id: "acme-web", name: "Acme web" }]} />);
    expect(el.querySelector("[data-testid='group-no-roles']")).toBeTruthy();
    await click("[data-testid='group-bind']");
    await click("[data-testid='gb-role-viewer']");
    await click("[data-testid='group-bind-save']");
    await click("[data-testid='group-bind']");
    await click("[data-testid='gb-role-viewer']");
    const project = [...document.querySelectorAll<HTMLButtonElement>("[data-testid='group-bind-sheet'] .idn-segs button")].find((b) => b.textContent === "One project")!;
    await act(async () => { project.click(); });
    expect(document.querySelector("[data-testid='gb-project-select']")?.textContent).toContain("Acme web");
    await click("[data-testid='group-bind-save']");
    expect(writes(fetchSpy)).toEqual([
      { url: "/v1/identity/role-bindings", method: "POST", body: { subject_type: "group", subject_id: "grp_oncall", role_id: viewer.id } },
      { url: "/v1/identity/projects/acme-web/role-bindings", method: "POST", body: { subject_type: "group", subject_id: "grp_oncall", role_id: viewer.id } },
    ]);
    act(() => root?.unmount()); host?.remove();

    const second = await mount(<GroupDetail group={release} people={people()} roles={F.roles(NOW)} />);
    await click("[data-testid='group-unbind-rb_5']");
    expect(document.querySelector("[data-testid='group-unbind-sheet']")?.textContent).toContain("Release Captain");
    await click("[data-testid='group-unbind-go']");
    expect(writes(second.fetchSpy)).toEqual([{ url: "/v1/identity/role-bindings/rb_5", method: "DELETE", body: null }]);
  });

  it("an IdP group: members are read-only, roles can still be bound; a Viewer's controls say what they need", async () => {
    const builders = groups().find((g) => g.id === "grp_builders")!;
    const owner = await mount(<GroupDetail group={builders} people={people()} roles={F.roles(NOW)} />);
    expect(owner.el.querySelector("[data-testid='group-edit']")).toBeNull();
    expect(owner.el.querySelector("[data-testid='group-bind']")?.getAttribute("aria-disabled")).toBeNull();
    clean(owner.el);
    act(() => root?.unmount()); host?.remove();
    const release = groups().find((g) => g.id === "grp_release")!;
    const viewer = await mount(<GroupDetail group={release} people={people()} roles={F.roles(NOW)} />, "viewer");
    for (const [id, p] of [["group-edit", "group:manage"], ["group-delete", "group:manage"], ["group-bind", "role_binding:assign"], ["group-unbind-rb_5", "role_binding:assign"]] as const) {
      const b = viewer.el.querySelector(`[data-testid='${id}']`)!;
      expect(b.getAttribute("aria-disabled"), id).toBe("true");
      expect(b.getAttribute("title")).toBe(needsWords(p));
    }
    clean(viewer.el);
  });
});

// ---- invite ---------------------------------------------------------------------------------

describe("Invite: a group picker", () => {
  it("offers only BOSS-managed groups and sends the picked ones as group_ids", async () => {
    const { fetchSpy } = await mount(<InviteSheet roles={F.roles(NOW)} groups={groups()} onClose={() => undefined} />);
    const offered = [...document.querySelectorAll("[data-testid='invite-groups'] button")].map((b) => b.textContent?.trim());
    expect(offered).toEqual(["Release", "On-call"]);
    type("#invite-emails", "ana@acme.dev");
    await click("[data-testid='invite-group-grp_release']");
    await click("[data-testid='invite-send']");
    expect(writes(fetchSpy)).toEqual([{ url: "/v1/identity/users", method: "POST",
      body: { email: "ana@acme.dev", bindings: [{ role_key: "operator", scope_type: "org" }], group_ids: ["grp_release"] } }]);
  });
});

// ---- CSV ------------------------------------------------------------------------------------

describe("People as CSV", () => {
  it("exports a spreadsheet-safe file with roles, groups, MFA and last seen", () => {
    expect(csvField("=HYPERLINK(1)")).toBe("'=HYPERLINK(1)");
    expect(csvField('a "b", c')).toBe('"a ""b"", c"');
    const text = peopleCsv(people());
    const rows = parseCsv(text);
    expect(rows[0]).toEqual(["Name", "Email", "Status", "Access roles", "Groups", "MFA", "Last seen"]);
    const sam = rows.find((r) => r[1] === "sam@acme.dev")!;
    expect(sam[0]).toBe("Sam Okafor");
    expect(sam[5]).toMatch(/on|off/);
    expect(rows.length).toBe(people().length + 1);
  });

  it("checks every imported row before anything is sent", () => {
    const rows = readInviteCsv("email,role,groups\nzoe@acme.dev,viewer,Release\nnot-an-email\nsam@acme.dev\nzoe@acme.dev\nbo@acme.dev,wizard\ncy@acme.dev,viewer,Builders\ndi@acme.dev,,Nope\n", {
      roles: F.roles(NOW), groups: groups(), people: people(), held: null, defaultRole: "operator",
    });
    expect(rows.map((r) => [r.line, r.email, r.problem])).toEqual([
      [2, "zoe@acme.dev", null],
      [3, "not-an-email", "Not an email address"],
      [4, "sam@acme.dev", "Already here"],
      [5, "zoe@acme.dev", "Twice in the file"],
      [6, "bo@acme.dev", "No role called “wizard”"],
      [7, "cy@acme.dev", "Synced groups are set by your identity provider"],
      [8, "di@acme.dev", "No group called “Nope”"],
    ]);
    expect(rows[0]).toMatchObject({ role: "viewer", groups: ["grp_release"] });
    const bare = readInviteCsv("eve@acme.dev\r\n", { roles: F.roles(NOW), groups: [], people: [], held: null, defaultRole: "operator" });
    expect(bare).toEqual([{ line: 1, email: "eve@acme.dev", role: "operator", groups: [], problem: null }]);
  });

  it("Import previews the rows, invites only the ready ones and shows each result", async () => {
    const { el, fetchSpy } = await mount(<PeopleList people={people()} roles={F.roles(NOW)} groups={groups()} />);
    expect(el.querySelector("[data-testid='people-export']")).toBeTruthy();
    await click("[data-testid='people-import']");
    type("[data-testid='import-text']", "zoe@acme.dev,viewer,Release\nsam@acme.dev\n");
    await settle();
    const preview = document.querySelector("[data-testid='import-preview']")!;
    expect(preview.textContent).toContain("Ready");
    expect(preview.textContent).toContain("Already here");
    clean(document.querySelector("[data-testid='import-sheet']")!);
    await click("[data-testid='import-send']");
    expect(writes(fetchSpy)).toEqual([{ url: "/v1/identity/users", method: "POST",
      body: { email: "zoe@acme.dev", bindings: [{ role_key: "viewer", scope_type: "org" }], group_ids: ["grp_release"] } }]);
    expect(document.querySelector("[data-testid='import-results-table']")?.textContent).toContain("zoe@acme.dev");
  });

  it("a Viewer cannot import, and is told why", async () => {
    const { el } = await mount(<PeopleList people={people()} roles={F.roles(NOW)} />, "viewer");
    const b = el.querySelector("[data-testid='people-import']")!;
    expect(b.getAttribute("aria-disabled")).toBe("true");
    expect(b.getAttribute("title")).toBe(needsWords("user:invite"));
  });
});

// ---- people list states ---------------------------------------------------------------------

describe("People: real states", () => {
  it("a failed read is an error with Try again, never 'No people yet'", async () => {
    const { el } = await mount(<PeopleList people={[]} roles={[]} failure={{ status: 503, code: "identity_unavailable" }} />);
    expect(el.querySelector("[data-testid='people-error']")?.textContent).toContain("Identity is unreachable");
    expect(el.textContent).not.toContain("No people yet");
    clean(el);
  });
  it("nobody yet: says what to do and offers Invite", async () => {
    const { el } = await mount(<PeopleList people={[]} roles={F.roles(NOW)} />);
    expect(el.querySelector("[data-testid='people-empty']")?.textContent).toContain("Invite your team");
    clean(el);
  });
});

// ---- a person ------------------------------------------------------------------------------

function samProps(tab: "access" | "overview" | "security" | "audit" | "keys" = "access") {
  return {
    person: people().find((p) => p.id === "usr_sam")!, tab,
    factors: F.factors(NOW).usr_sam, sessions: F.sessions(NOW).usr_sam,
    effective: null, projectEffective: [], roles: F.roles(NOW),
    delegations: F.delegations(NOW).filter((d) => d.user.id === "usr_sam"),
    keys: F.personalKeys(NOW).usr_sam, events: F.auditEvents(NOW),
  };
}

describe("A person: verify, Add access with a project and an end, honest gaps", () => {
  it("an unverified address shows the pill and Mark verified sends the API's call", async () => {
    const { el, fetchSpy } = await mount(<PersonProfile {...samProps()} emailVerified={false} />);
    expect(el.textContent).toContain("unverified");
    await click("[data-testid='person-verify-email']");
    expect(writes(fetchSpy)).toEqual([{ url: "/v1/identity/users/usr_sam/verify-email", method: "POST", body: {} }]);
    act(() => root?.unmount()); host?.remove();
    const verified = await mount(<PersonProfile {...samProps()} emailVerified />);
    expect(verified.el.querySelector("[data-testid='person-verify-email']")).toBeNull();
    act(() => root?.unmount()); host?.remove();
    const viewer = await mount(<PersonProfile {...samProps()} emailVerified={false} />, "viewer");
    expect(viewer.el.querySelector("[data-testid='person-verify-email']")?.getAttribute("title")).toBe(needsWords("user:update"));
  });

  it("Add access: a project from the list and an end date go in the body", async () => {
    expect(expiresAt("7d", "", NOW)).toBe(new Date(NOW + 7 * 86_400_000).toISOString());
    expect(expiresAt("none", "", NOW)).toBeNull();
    expect(expiresAt("date", "2026-12-31", NOW)).toBe("2026-12-31T23:59:59Z");
    const { fetchSpy } = await mount(<PersonProfile {...samProps()} projects={[{ id: "proj_1", name: "Acme web" }, { id: "proj_2", name: "Billing" }]} />, "owner");
    await click("[data-testid='person-add-access']");
    const sheet = document.querySelector("[data-testid='add-access-sheet']")!;
    const viewerRole = [...sheet.querySelectorAll<HTMLButtonElement>(".idn-pick-row")].find((r) => r.textContent?.startsWith("Viewer"))!;
    await act(async () => { viewerRole.click(); });
    const project = [...sheet.querySelectorAll<HTMLButtonElement>(".idn-segs button")].find((b) => b.textContent === "One project")!;
    await act(async () => { project.click(); });
    select("[data-testid='aa-project-select']", "proj_2");
    await click("[data-testid='aa-exp-7d']");
    await click("[data-testid='add-access-save']");
    const viewer = F.roles(NOW).find((r) => r.key === "viewer")!;
    expect(writes(fetchSpy)).toEqual([{ url: "/v1/identity/projects/proj_2/role-bindings", method: "POST",
      body: { subject_type: "principal", subject_id: "usr_sam", role_id: viewer.id, expires_at: new Date(NOW + 7 * 86_400_000).toISOString() } }]);
  });

  it("a part that failed is an error; one refused says what it needs; no raw ids or event names", async () => {
    const { el } = await mount(<PersonProfile {...samProps()} failures={{ effective: { status: 500, code: "error" } }} sessions={null}
      delegations={null} />);
    expect(el.textContent).toContain("Couldn't load effective access");
    expect(el.textContent).not.toContain("Needs “View people”");
    expect(el.querySelector("[data-testid='locked-sessions']")?.textContent).toContain("View sessions");
    clean(el);
    for (const tab of ["overview", "audit", "keys"] as const) {
      act(() => root?.unmount()); host?.remove();
      const t = await mount(<PersonProfile {...samProps(tab)} />);
      expect(rawLeaks(t.el), tab).toEqual([]);
    }
  });
});

// ---- access requests ------------------------------------------------------------------------

const REQ: AccessRequestRow[] = [
  { id: "acr_1", requester_principal_id: "usr_lena", requester_name: "Lena Fischer", permission_key: "audit:read", project_id: null,
    reason: "Need to check a deploy", status: "open", created_at: new Date(NOW - 3_600_000).toISOString(),
    candidate_roles: [{ id: "arl_auditor", key: "auditor", name: "Auditor" }] },
];

describe("Access requests: approve or deny", () => {
  it("lists who asked for what by name; Approve grants through a role with an end; Deny closes it", async () => {
    const { el, fetchSpy } = await mount(<AccessRequests requests={REQ} roles={F.roles(NOW)} />);
    const card = el.querySelector("[data-testid='access-requests']")!;
    expect(card.textContent).toContain("Lena Fischer");
    expect(card.textContent).toContain("View the security audit log");
    clean(el);
    await click("[data-testid='request-approve-acr_1']");
    expect(document.querySelector("[data-testid='request-approve-sheet']")?.textContent).toContain("Auditor");
    await click("[data-testid='request-approve-go']");
    await click("[data-testid='request-deny-acr_1']");
    expect(writes(fetchSpy)).toEqual([
      { url: "/v1/identity/access-requests/acr_1/approve", method: "POST", body: { role_id: "arl_auditor", expires_at: new Date(NOW + 30 * 86_400_000).toISOString(), reason: null } },
      { url: "/v1/identity/access-requests/acr_1/deny", method: "POST", body: { reason: null } },
    ]);
  });

  it("nothing waiting: nothing shown; a failed read: an error; a Viewer's buttons say what they need", async () => {
    const none = await mount(<AccessRequests requests={[]} roles={[]} />);
    expect(none.el.textContent).toBe("");
    act(() => root?.unmount()); host?.remove();
    const failed = await mount(<AccessRequests requests={null} roles={[]} failure={{ status: 500, code: "error" }} />);
    expect(failed.el.querySelector("[data-testid='requests-error']")).toBeTruthy();
    act(() => root?.unmount()); host?.remove();
    const viewer = await mount(<AccessRequests requests={REQ} roles={[]} />, "viewer");
    expect(viewer.el.querySelector("[data-testid='request-approve-acr_1']")?.getAttribute("title")).toBe(needsWords("role_binding:assign"));
  });
});
