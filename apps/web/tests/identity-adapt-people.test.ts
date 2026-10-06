/**
 * The real API's people/access answers, read into the console's view types (lib/identity/
 * adapt-people.ts). The JSON below is shaped exactly as the routers build it:
 * identity_people.py _person_view / list_sessions / service_accounts.public, identity_access.py
 * (bindings._present, groups.list_groups / get_group, roles._present / preview / versions,
 * principal_effective_access, grantors). Every nested list a screen dereferences must exist.
 */
import { describe, expect, it } from "vitest";

import {
  adaptEffective, adaptGrantor, adaptRole, adaptRolePreview, adaptRoleVersions, adaptServiceAccount, adaptSession,
  assembleDirectory, describeAgent, nameIndex, parseWhy, roleIndex,
} from "../lib/identity/adapt-people";
import { PERMISSIONS, PERMISSION_META } from "../lib/identity/permissions.gen";

const ROLES = [
  { id: "role_owner", key: "owner", name: "Owner", description: "Everything", builtin: true, system: false,
    bindable_scopes: ["org"], mfa_required: true, version: 1, cloned_from: null, permissions: ["user:read", "user:invite"], binding_count: 1 },
  { id: "role_operator", key: "operator", name: "Operator", description: "Builds", builtin: true, system: false,
    bindable_scopes: ["org", "project"], mfa_required: false, version: 1, cloned_from: null, permissions: ["code_task:read"], binding_count: 2 },
  { id: "role_rc", key: "release_captain", name: "Release Captain", description: "", builtin: false, system: false,
    bindable_scopes: ["org", "project"], mfa_required: false, version: 3, cloned_from: "role_operator", permissions: ["deployment:read"], binding_count: 0 },
  { id: "role_sys_worker", key: "system_worker", name: "Worker", description: "", builtin: true, system: true,
    bindable_scopes: ["org"], mfa_required: false, version: 1, cloned_from: null, permissions: [], binding_count: 1 },
];
const USERS = [
  { id: "usr_ana", display_name: "Ana", email: "ana@acme.dev", status: "active", access_roles: ["owner", "operator"], mfa: true,
    mfa_required: true, password: true, self_signed_up: false, network_bound: false, last_login_at: "2026-10-05T10:00:00+00:00" },
  { id: "usr_li", display_name: "Li", email: "li@acme.dev", status: "disabled", access_roles: [], mfa: false,
    mfa_required: false, password: false, self_signed_up: true, network_bound: false, last_login_at: null },
];
const BINDINGS = [
  { id: "rb_1", subject_type: "principal", subject_id: "usr_ana", via_group: null, role_id: "role_owner", role_key: "owner",
    scope: "org", scope_type: "org", scope_id: null, granted_by: "usr_ana", granted_at: "2026-09-01T00:00:00+00:00", expires_at: null, reason: null },
  { id: "rb_2", subject_type: "group", subject_id: "grp_build", via_group: { id: "grp_build", name: "Builders", provider: "scim" },
    role_id: "role_operator", role_key: "operator", scope: "project:acme-web", scope_type: "project", scope_id: "acme-web",
    granted_by: "usr_ana", granted_at: "2026-09-02T00:00:00+00:00", expires_at: null, reason: null },
  { id: "rb_3", subject_type: "principal", subject_id: "sa_ci", via_group: null, role_id: "role_operator", role_key: "operator",
    scope: "org", scope_type: "org", scope_id: null, granted_by: "usr_ana", granted_at: "2026-09-03T00:00:00+00:00", expires_at: null, reason: null },
];
const GROUPS = [{ id: "grp_build", key: "builders", name: "Builders", description: "", source: "scim", created_at: "2026-09-01", member_count: 1 }];
const GROUP_DETAIL = [{ ...GROUPS[0], members: [
  { principal_id: "usr_ana", display_name: "Ana", kind: "user", status: "active", source: "scim", added_by: null, added_at: "2026-09-01" },
] }];

describe("the directory, joined from the real API", () => {
  const dir = assembleDirectory({ users: USERS, roles: ROLES, groups: GROUPS, groupDetails: GROUP_DETAIL, bindings: BINDINGS });

  it("never lists a system role", () => {
    expect(dir.roles.map((r) => r.key)).toEqual(["owner", "operator", "release_captain"]);
    expect(dir.roles.find((r) => r.key === "operator")!.holders).toBe(2);
    expect(dir.roles.find((r) => r.key === "release_captain")!.cloned_from).toMatchObject({ id: "role_operator", name: "Operator" });
  });

  it("gives each person their own and their groups' bindings, groups and sign-in", () => {
    const ana = dir.people.find((p) => p.id === "usr_ana")!;
    expect(ana.bindings.map((b) => b.id).sort()).toEqual(["rb_1", "rb_2"]);
    const viaGroup = ana.bindings.find((b) => b.id === "rb_2")!;
    expect(viaGroup.via_group).toEqual({ id: "grp_build", name: "Builders", source: "scim" });
    expect(viaGroup.role_name).toBe("Operator");
    expect(viaGroup.scope).toBe("project:acme-web");
    expect(ana.groups).toEqual([{ id: "grp_build", name: "Builders", source: "scim", provider: "SCIM" }]);
    expect(ana.last_seen_at).toBe("2026-10-05T10:00:00+00:00");
    expect(ana.sign_in.idp_mfa).toBe("MFA on");
    const li = dir.people.find((p) => p.id === "usr_li")!;
    expect(li.bindings).toEqual([]);
    expect(li.groups).toEqual([]);
    expect(li.sign_in.idp_mfa).toBeNull();
    expect(li.status).toBe("disabled");
  });

  it("gives each group its members (named) and the roles bound to it", () => {
    const g = dir.groups[0];
    expect(g.members).toEqual([{ id: "usr_ana", name: "Ana", email: "ana@acme.dev", hue: expect.any(Number) }]);
    expect(g.member_count).toBe(1);
    expect(g.bindings.map((b) => b.id)).toEqual(["rb_2"]);
    expect(g.provider).toBe("SCIM");
  });

  it("without /role-bindings, shows access_roles as chips that offer nothing to remove", () => {
    const thin = assembleDirectory({ users: USERS, roles: ROLES, bindings: null });
    const ana = thin.people[0];
    expect(ana.bindings.map((b) => b.role_key)).toEqual(["owner", "operator"]);
    expect(ana.bindings.every((b) => b.synthetic)).toBe(true);
    expect(ana.groups).toEqual([]);
  });

  it("reads a person's detail view (factors, SSO links)", () => {
    const detail = { ...USERS[0], factors: { totp: true, passkeys: 2, recovery_codes_remaining: 8 }, sessions: 1, api_keys: 0, sso_links: ["Okta"] };
    const one = assembleDirectory({ users: [detail], roles: ROLES, bindings: BINDINGS }).people[0];
    expect(one.sign_in).toMatchObject({ passkeys: 2, totp: true, sso: "Okta" });
    expect(one.source).toBe("sso");
  });
});

describe("roles", () => {
  it("summarises versions against the one before", () => {
    const v = adaptRoleVersions([
      { version: 1, permissions: ["a:read"], changed_by: "usr_ana", changed_at: "2026-09-01" },
      { version: 2, permissions: ["a:read", "b:read", "c:read"], changed_by: "usr_ana", changed_at: "2026-09-02" },
    ]);
    expect(v.map((x) => x.version)).toEqual([2, 1]);
    expect(v[0]).toMatchObject({ by: "usr_ana", at: "2026-09-02", summary: "+2 −0" });
    expect(v[1].summary).toBe("1 permissions");
  });

  it("reads the impact preview (who, through which group, which grants)", () => {
    const names = nameIndex([{ id: "usr_ana", display_name: "Ana" }]);
    const p = adaptRolePreview({
      added: ["deployment:production"], removed: [], critical_added: ["deployment:production"],
      affected_principals: ["usr_ana", "usr_li"],
      via: [{ principal_id: "usr_ana", group: null, scope: "org" }, { principal_id: "usr_li", group: "Builders", scope: "org" }],
      affected_grants: ["grt_1"],
    }, names);
    expect(p.affected.people.map((x) => x.name)).toEqual(["Ana", "usr_li"]);
    expect(p.affected.direct).toBe(1);
    expect(p.affected.via_groups).toEqual([{ group: "Builders", count: 1 }]);
    expect(p.affected.grants).toBe(1);
    expect(p.affected.agents).toEqual([]);
    expect(p.warnings).toEqual([{ permission: "deployment:production", text: "critical" }]);
    expect(p.pages).toEqual({ gained: [], lost: [] });
  });

  it("an unknown role key in a role still renders", () => {
    expect(adaptRole({ id: "r", key: "custom_x" }).name).toBe("Custom X");
  });
});

describe("effective access", () => {
  const index = roleIndex(assembleDirectory({ users: [], roles: ROLES }).roles);

  it("parses authz.Why strings", () => {
    expect(parseWhy("role operator ← binding at org", index)).toEqual({ role: "Operator", role_key: "operator", scope: "org", via_group: null });
    expect(parseWhy("role operator ← group Builders (from scim) ← binding at project:acme-web", index))
      .toEqual({ role: "Operator", role_key: "operator", scope: "project:acme-web", via_group: "Builders" });
  });

  it("lists every grantable permission, held or not, with the why", () => {
    const eff = adaptEffective({ principal_id: "usr_ana", scope: "org", epoch: 4,
      permissions: [{ key: "user:read", via: ["role owner ← binding at org"] }] }, index);
    const grantable = PERMISSIONS.filter((p) => !PERMISSION_META[p].systemOnly).length;
    expect(eff.total).toBe(grantable);
    expect(eff.held).toBe(1);
    expect(eff.entries.find((e) => e.permission === "user:read")).toMatchObject({ held: true, why: [{ role_key: "owner", role: "Owner" }] });
    const notHeld = eff.entries.find((e) => e.permission === "user:invite")!;
    expect(notHeld.held).toBe(false);
    expect(notHeld.held_by).toBe("Admin at org");
    expect(eff.by_project).toEqual({});
  });
});

describe("sessions, service accounts, grantors", () => {
  it("names a session's client from its User-Agent", () => {
    const s = adaptSession({ id: "ses_1", principal_id: "usr_ana", methods: ["passkey"], created_at: "2026-10-01",
      last_seen_at: "2026-10-05", ip: "10.0.0.1", user_agent: "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) Safari/604.1", mfa_at: null });
    expect(s).toMatchObject({ client: "Safari on iOS", device: "phone", started_at: "2026-10-01", current: false, location: null });
    expect(describeAgent("")).toEqual({ client: "Unknown client", device: "desktop" });
  });

  it("reads a service account (display_name, owner ids) with its keys and bindings", () => {
    const dir = assembleDirectory({ users: USERS, roles: ROLES, bindings: BINDINGS });
    const sa = adaptServiceAccount({ id: "sa_ci", display_name: "ci-deployer", description: "CI", status: "active", owners: ["usr_ana"], orphaned: false }, {
      names: nameIndex(dir.people), roles: dir.roles, bindings: dir.bindings.filter((b) => b.subject_id === "sa_ci"),
      keys: [{ id: "key_1", name: "gha", owner: { id: "sa_ci", name: "ci-deployer", kind: "service_account" }, scopes: [], projects: null,
        networks: null, created_at: "2026-09-01", last_used_at: "2026-10-01", last_used_ip: null, last_used_client: "gh-actions",
        expires_at: null, grace_until: null, rotated_from: null, status: "active" }],
    });
    expect(sa.name).toBe("ci-deployer");
    expect(sa.owners).toEqual([{ id: "usr_ana", name: "Ana", hue: expect.any(Number) }]);
    expect(sa.kind).toBe("service");
    expect(sa.bindings.map((b) => b.role_key)).toEqual(["operator"]);
    expect(sa.keys).toHaveLength(1);
    expect(sa.last_used).toEqual({ at: "2026-10-01", client: "gh-actions" });
    expect(sa.effective).toBe(1);
    // Without keys or bindings, nothing a screen reads is undefined.
    const bare = adaptServiceAccount({ id: "sa_x", display_name: "x", status: "active", owners: [], orphaned: true });
    expect(bare.keys).toEqual([]);
    expect(bare.bindings).toEqual([]);
    expect(bare.name.includes("x")).toBe(true);
  });

  it("reads a grantor (principal_id, display_name)", () => {
    expect(adaptGrantor({ principal_id: "usr_ana", display_name: "Ana" })).toMatchObject({ id: "usr_ana", name: "Ana", role: "" });
  });
});
