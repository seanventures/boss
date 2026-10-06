/**
 * THE IDENTITY FIXTURE ORG: the people, agents and events of the approved mockups
 * (~/Downloads/boss-identity-mockup/). One source for the stub API (stub/server.ts), the web
 * tests and the production-build walk, so a screen is checked against the same data the
 * mockup drew. Times are offsets from `now`, so the fixture never ages.
 */
import { PERMISSIONS, ROLE_PERMISSIONS, type Permission } from "../permissions.gen";
import type {
  AccessRole, ApiKey, AuditEvent, Binding, Delegation, Factor, Group, Passport, Person, Provider,
  RoleVersion, SecurityPolicy, ServiceAccount, SessionRow,
} from "../types";

const MIN = 60_000;
const HOUR = 60 * MIN;
const DAY = 24 * HOUR;
export const ago = (now: number, ms: number) => new Date(now - ms).toISOString();
export const ahead = (now: number, ms: number) => new Date(now + ms).toISOString();

export const ORG = { id: "org_acme", name: "Acme BOSS" };
export const HOST = "boss.acme.dev";

export const ROLE_HUE: Record<string, number> = { owner: 3, admin: 4, operator: 0, viewer: 7, auditor: 5, release_captain: 1 };

const OPERATOR = ROLE_PERMISSIONS.operator;
const RELEASE_V3: Permission[] = [...OPERATOR.filter((p) => p !== "deployment:rollback"), "deployment:rollback"];

export function roles(now: number): AccessRole[] {
  const builtin = (key: "owner" | "admin" | "operator" | "viewer" | "auditor", name: string, description: string, holders: number): AccessRole => ({
    id: `arl_${key}`, key, name, description, builtin: true, permissions: [...ROLE_PERMISSIONS[key]],
    bindable_scopes: key === "owner" || key === "auditor" ? ["org"] : ["org", "project"], holders, version: 1,
    cloned_from: null, hue: ROLE_HUE[key],
  });
  return [
    builtin("owner", "Owner", "Everything, including Owners and deleting the org.", 1),
    builtin("admin", "Admin", "Runs the org: people, roles, keys and settings.", 2),
    builtin("operator", "Operator", "Builds, ships and approves the everyday work.", 4),
    builtin("viewer", "Viewer", "Sees everything that is not security detail.", 1),
    builtin("auditor", "Auditor", "Reads every record, changes nothing.", 1),
    {
      id: "arl_release_captain", key: "release_captain", name: "Release Captain",
      description: "Ships what Operators built: deploys, rollbacks and merges, without owning settings.",
      builtin: false, permissions: RELEASE_V3, bindable_scopes: ["org", "project"], holders: 2, version: 3,
      cloned_from: { id: "arl_operator", name: "Operator", version: 1, at: ago(now, 22 * DAY) }, hue: ROLE_HUE.release_captain,
    },
  ];
}

export function roleVersions(now: number): RoleVersion[] {
  return [
    { version: 3, by: "Priya", at: ago(now, 6 * DAY), summary: "+github:merge" },
    { version: 2, by: "Priya", at: ago(now, 15 * DAY), summary: "−code_task:cancel" },
    { version: 1, by: "Param", at: ago(now, 22 * DAY), summary: "cloned" },
  ];
}

function bind(id: string, role: string, name: string, scope: Binding["scope"], subject: string, now: number,
  extra: Partial<Binding> = {}): Binding {
  return { id, role_id: `arl_${role}`, role_key: role, role_name: name, scope, subject_id: subject, via_group: null,
    granted_by: "Priya", granted_at: ago(now, 55 * DAY), expires_at: null, ...extra };
}

export function people(now: number): Person[] {
  const p = (id: string, name: string, email: string, hue: number, rest: Partial<Person>): Person => ({
    id, display_name: name, email, status: "active", source: "local", bindings: [], groups: [],
    sign_in: { passkeys: 1, totp: false, password: true, sso: null, idp_mfa: null },
    last_seen_at: ago(now, 2 * MIN), last_seen_from: null, joined_at: ago(now, 90 * DAY), joined_by: "Param",
    seat: null, self_signed_up: false, network_bound: false, hue, ...rest,
  });
  const builders = { id: "grp_builders", name: "Builders", source: "oidc" as const, provider: "Okta" };
  const release = { id: "grp_release", name: "Release", source: "manual" as const, provider: null };
  const oncall = { id: "grp_oncall", name: "On-call", source: "manual" as const, provider: null };
  return [
    p("usr_param", "Param Gnanaguru", "param@kaappu.org", 4, {
      bindings: [bind("rb_1", "owner", "Owner", "org", "usr_param", now, { granted_by: "BOSS (claim)" })],
      sign_in: { passkeys: 2, totp: false, password: true, sso: null, idp_mfa: null }, last_seen_at: ago(now, 10_000),
      seat: "ceo", joined_by: null, last_seen_from: "Chennai, IN",
    }),
    p("usr_priya", "Priya Raman", "priya@acme.dev", 5, {
      bindings: [bind("rb_2", "admin", "Admin", "org", "usr_priya", now, { granted_by: "Param" })],
      last_seen_at: ago(now, 12 * MIN), seat: "cao", groups: [oncall],
    }),
    p("usr_sam", "Sam Okafor", "sam@acme.dev", 6, {
      bindings: [
        bind("rb_3", "operator", "Operator", "org", "usr_sam", now, { granted_at: ago(now, 55 * DAY) }),
        bind("rb_4", "admin", "Admin", "project:acme-web", "usr_sam", now, { granted_by: "Param", granted_at: ago(now, 6 * DAY), expires_at: ahead(now, 86 * DAY) }),
        bind("rb_5", "release_captain", "Release Captain", "org", "grp_release", now, { via_group: release }),
        bind("rb_6", "operator", "Operator", "org", "grp_builders", now, { via_group: builders, granted_by: "Okta" }),
      ],
      groups: [builders, release, oncall], source: "sso",
      sign_in: { passkeys: 1, totp: true, password: false, sso: "Okta", idp_mfa: "Okta MFA" },
      last_seen_at: ago(now, 3 * MIN), last_seen_from: "Lagos, NG", seat: "release_manager", joined_by: "Priya",
      joined_at: ago(now, 55 * DAY),
    }),
    p("usr_lena", "Lena Fischer", "lena@acme.dev", 3, {
      bindings: [bind("rb_7", "operator", "Operator", "org", "usr_lena", now)],
      sign_in: { passkeys: 0, totp: true, password: true, sso: null, idp_mfa: null }, last_seen_at: ago(now, 2 * HOUR),
      groups: [builders, oncall], last_seen_from: "Berlin, DE",
    }),
    p("usr_theo", "Theo Nakamura", "theo@acme.dev", 2, {
      bindings: [bind("rb_8", "operator", "Operator", "project:acme-web", "usr_theo", now)],
      sign_in: { passkeys: 0, totp: false, password: true, sso: null, idp_mfa: null }, last_seen_at: ago(now, DAY),
      groups: [builders],
    }),
    p("usr_diego", "Diego Alvarez", "diego@acme.dev", 1, {
      bindings: [bind("rb_9", "viewer", "Viewer", "org", "usr_diego", now)], source: "sso",
      sign_in: { passkeys: 0, totp: false, password: false, sso: "Okta", idp_mfa: "Okta MFA" }, last_seen_at: ago(now, 4 * DAY),
      groups: [{ id: "grp_finance", name: "Finance", source: "oidc", provider: "Okta" }, builders],
    }),
    p("usr_mei", "Mei Tanaka", "mei@acme.dev", 0, {
      bindings: [bind("rb_10", "auditor", "Auditor", "org", "usr_mei", now)], last_seen_at: ago(now, 6 * DAY),
    }),
    p("usr_ana", "Ana Costa", "ana@acme.dev", 7, {
      status: "pending", bindings: [bind("rb_11", "operator", "Operator", "project:acme-web", "usr_ana", now)],
      sign_in: { passkeys: 0, totp: false, password: false, sso: null, idp_mfa: null }, last_seen_at: null,
      joined_at: ago(now, 2 * DAY), joined_by: "Priya",
    }),
  ];
}

export function groups(now: number, all: Person[]): Group[] {
  const members = (gid: string) => all.filter((x) => x.groups.some((g) => g.id === gid))
    .map((x) => ({ id: x.id, name: x.display_name, email: x.email, hue: x.hue }));
  return [
    { id: "grp_builders", name: "Builders", description: "Everyone who builds on acme-web.", source: "oidc", provider: "Okta",
      synced_at: ago(now, 4 * MIN), members: members("grp_builders"),
      bindings: [bind("rb_6", "operator", "Operator", "org", "grp_builders", now, { granted_by: "Okta" })] },
    { id: "grp_release", name: "Release", description: "Ships to production.", source: "manual", provider: null, synced_at: null,
      members: [...members("grp_release"), { id: "usr_priya", name: "Priya Raman", email: "priya@acme.dev", hue: 5 }],
      bindings: [bind("rb_5", "release_captain", "Release Captain", "org", "grp_release", now)] },
    { id: "grp_finance", name: "Finance", description: "Spend and budgets, read only.", source: "oidc", provider: "Okta",
      synced_at: ago(now, 4 * MIN), members: members("grp_finance"), bindings: [] },
    { id: "grp_oncall", name: "On-call", description: "Paged when production breaks.", source: "manual", provider: null,
      synced_at: null, members: members("grp_oncall"), bindings: [] },
  ];
}

export function sessions(now: number): Record<string, SessionRow[]> {
  return {
    usr_sam: [
      { id: "ses_s1", current: true, device: "desktop", client: "Chrome · macOS", location: "Lagos, NG", ip: "102.89.4.17", methods: ["passkey"], started_at: ago(now, 3 * MIN), last_seen_at: ago(now, MIN) },
      { id: "ses_s2", current: false, device: "phone", client: "Safari · iOS", location: "Lagos, NG", ip: "102.89.4.18", methods: ["sso:okta"], started_at: ago(now, 2 * DAY), last_seen_at: ago(now, 5 * HOUR) },
      { id: "ses_s3", current: false, device: "desktop", client: "Firefox · Ubuntu", location: "Berlin, DE", ip: "88.130.9.2", methods: ["pwd", "totp"], started_at: ago(now, 6 * DAY), last_seen_at: ago(now, 3 * DAY) },
    ],
    usr_param: [
      { id: "ses_p1", current: true, device: "desktop", client: "Chrome · macOS", location: "Chennai, IN", ip: "49.37.1.9", methods: ["passkey"], started_at: ago(now, HOUR), last_seen_at: ago(now, 10_000) },
      { id: "ses_p2", current: false, device: "phone", client: "Safari · iOS", location: "Chennai, IN", ip: "49.37.1.10", methods: ["passkey"], started_at: ago(now, 3 * DAY), last_seen_at: ago(now, 7 * HOUR) },
    ],
  };
}

export function factors(now: number): Record<string, Factor[]> {
  return {
    usr_sam: [
      { id: "pk_1", kind: "passkey", name: "MacBook Pro · Touch ID", detail: "Passkey", last_used_at: ago(now, 3 * MIN), note: "synced" },
      { id: "pk_2", kind: "passkey", name: "YubiKey 5C", detail: "Passkey", last_used_at: ago(now, 9 * DAY), note: "device-bound" },
      { id: "totp_1", kind: "totp", name: "Authenticator app", detail: "TOTP", last_used_at: ago(now, 52 * DAY), note: null },
      { id: "rc", kind: "recovery", name: "Recovery codes", detail: "8 of 10 left", last_used_at: ago(now, 34 * DAY), note: null },
      { id: "sso_okta", kind: "sso", name: "Okta SSO", detail: "acme.okta.com · Okta MFA trusted", last_used_at: ago(now, 2 * DAY), note: null },
    ],
    usr_param: [
      { id: "pk_p1", kind: "passkey", name: "MacBook Air · Touch ID", detail: "Passkey", last_used_at: ago(now, HOUR), note: "synced" },
      { id: "pk_p2", kind: "passkey", name: "iPhone", detail: "Passkey", last_used_at: ago(now, 3 * DAY), note: "synced" },
      { id: "totp_p", kind: "totp", name: "Authenticator app", detail: "TOTP", last_used_at: null, note: null },
      { id: "rc", kind: "recovery", name: "Recovery codes", detail: "10 of 10 left", last_used_at: null, note: null },
    ],
  };
}

function key(id: string, name: string, owner: ApiKey["owner"], scopes: Permission[], now: number, rest: Partial<ApiKey>): ApiKey {
  return { id, name, owner, scopes, projects: null, networks: null, created_at: ago(now, 80 * DAY), last_used_at: ago(now, 2 * HOUR),
    last_used_ip: "140.82.112.4", last_used_client: "GitHub Actions", expires_at: ahead(now, 84 * DAY), grace_until: null,
    rotated_from: null, status: "active", ...rest };
}

export function serviceAccounts(now: number): ServiceAccount[] {
  const ci = { id: "sva_ci_deployer", name: "ci-deployer", kind: "service_account" as const };
  return [
    {
      id: ci.id, name: ci.name, description: "Deploys acme-web from GitHub Actions", kind: "service",
      owners: [{ id: "usr_priya", name: "Priya", hue: 5 }, { id: "usr_sam", name: "Sam", hue: 6 }],
      bindings: [bind("rb_sa1", "release_captain", "Release Captain", "org", ci.id, now)],
      keys: [
        key("key_7f3k2q", "GitHub Actions · deploy", ci, ["deployment:create", "deployment:read", "code_task:read"], now,
          { projects: ["acme-web"], expires_at: ahead(now, 6 * DAY), networks: "GitHub Actions ranges" }),
        key("key_m4w8re", "Nightly smoke", ci, ["code_task:read", "preview:read", "observability:read"], now,
          { last_used_at: ago(now, 9 * HOUR), last_used_ip: "10.0.4.12", last_used_client: "cron", expires_at: ahead(now, 84 * DAY) }),
        key("key_x9q2ab", "Old Jenkins (rotated)", ci, ["deployment:create", "deployment:read"], now,
          { projects: ["acme-web"], last_used_at: ago(now, 41 * DAY), status: "grace", grace_until: ahead(now, 23 * HOUR) }),
      ],
      status: "active", created_by: "Priya", created_at: ago(now, 95 * DAY), last_used: { at: ago(now, 2 * HOUR), client: "GitHub Actions" },
      effective: RELEASE_V3.length, orphaned: false,
    },
    {
      id: "sva_github_actions", name: "github-actions", description: "Builds and tests pull requests", kind: "service",
      owners: [{ id: "usr_sam", name: "Sam", hue: 6 }],
      bindings: [bind("rb_sa2", "operator", "Operator", "project:acme-web", "sva_github_actions", now)],
      keys: [key("key_r2d8vm", "PR checks", { id: "sva_github_actions", name: "github-actions", kind: "service_account" }, ["code_task:read", "code_task:create"], now, {})],
      status: "active", created_by: "Sam", created_at: ago(now, 60 * DAY), last_used: { at: ago(now, 20 * MIN), client: "GitHub Actions" },
      effective: OPERATOR.length, orphaned: false,
    },
    {
      id: "sys_scim_okta", name: "scim-okta", description: "SCIM provisioning only", kind: "scim", owners: [{ id: "usr_priya", name: "Priya", hue: 5 }],
      bindings: [], keys: [], status: "active", created_by: "Priya", created_at: ago(now, 120 * DAY),
      last_used: { at: ago(now, 4 * MIN), client: "Okta SCIM" }, effective: 1, orphaned: false,
    },
    {
      id: "sys_legacy_console", name: "legacy-api-key", description: "BOSS_API_KEY · acting as Admin", kind: "legacy", owners: [],
      bindings: [bind("rb_sa3", "admin", "Admin", "org", "sys_legacy_console", now, { granted_by: "migration" })], keys: [],
      status: "active", created_by: null, created_at: ago(now, 30 * DAY), last_used: { at: ago(now, 5 * HOUR), client: "1 MCP client" },
      effective: ROLE_PERMISSIONS.admin.length, orphaned: false,
    },
  ];
}

export function personalKeys(now: number): Record<string, ApiKey[]> {
  const sam = { id: "usr_sam", name: "Sam Okafor", kind: "user" as const };
  const param = { id: "usr_param", name: "Param Gnanaguru", kind: "user" as const };
  return {
    usr_sam: [key("key_c1aude", "Claude Code", sam, ["code_task:read", "code_task:create", "code_task:steer", "workflow:read"], now,
      { last_used_client: "Claude Code (MCP)", last_used_ip: "102.89.4.17", last_used_at: ago(now, 40 * MIN) })],
    usr_param: [key("key_p4ram1", "Laptop CLI", param, ["code_task:read", "workflow:read", "deployment:read"], now,
      { last_used_client: "boss CLI", last_used_ip: "49.37.1.9", last_used_at: ago(now, DAY) })],
  };
}

const CEILING_BUILD: Permission[] = ["code_task:read", "code_task:create", "code_task:steer", "code_task:retry", "github:sync",
  "preview:manage", "repository:read", "workflow_run:read", "workflow_run:start"];

export function passports(now: number): Passport[] {
  const a = (id: string, name: string, title: string, rest: Partial<Passport>): Passport => ({
    id: `agt_${id}`, name, role_key: id, title, sponsor: { id: "usr_param", name: "Param", hue: 4 }, ceiling_preset: "Builder",
    ceiling: CEILING_BUILD, external_ceiling: { github: { contents: "write", pull_requests: "write", issues: "read" } },
    can_spawn: false, allowed_children: [], max_depth: 3, autonomous: false, status: "active", sealed: true,
    active_grants: 1, last_run_at: ago(now, 2 * HOUR),
    lineage: [
      { at: ago(now, 40 * DAY), event: "born", detail: "by Param" },
      { at: ago(now, 40 * DAY), event: "sealed", detail: "ceiling: Builder" },
    ], ...rest,
  });
  return [
    a("atlas", "Atlas", "Chief Architect", { can_spawn: true, allowed_children: ["forge", "vera", "rook"], active_grants: 3, last_run_at: ago(now, 11 * MIN), ceiling_preset: "Planner" }),
    a("forge", "Forge", "Software Engineer", { active_grants: 3, last_run_at: ago(now, 9 * MIN), can_spawn: true, allowed_children: ["vera", "rook"],
      lineage: [{ at: ago(now, 40 * DAY), event: "born", detail: "by Param" }, { at: ago(now, 40 * DAY), event: "sealed", detail: "ceiling: Builder" },
        { at: ago(now, 3 * DAY), event: "ceiling_changed", detail: "−deployment:create" }] }),
    a("vera", "Vera", "QA Lead", { ceiling_preset: "Reviewer", ceiling: ["code_task:read", "code_task:steer", "repository:read", "code_task:retry", "github:sync", "preview:manage"], last_run_at: ago(now, 8 * MIN) }),
    a("juno", "Juno", "Product Manager", { ceiling_preset: "Planner", active_grants: 1 }),
    a("rook", "Rook", "Release Engineer", { active_grants: 1 }),
    a("sentry", "Sentry", "Security Reviewer", { ceiling_preset: "Reviewer", active_grants: 1 }),
    a("harbor", "Harbor", "Ops Engineer", { autonomous: true, ceiling_preset: "Ops", active_grants: 2 }),
    a("beacon", "Beacon", "On-call SRE", { autonomous: true, ceiling_preset: "Ops", active_grants: 0 }),
    a("quill", "Quill", "Technical Writer", { ceiling_preset: "Writer", active_grants: 0, sponsor: { id: "usr_lena", name: "Lena", hue: 3 } }),
    a("prism", "Prism", "Designer", { ceiling_preset: "Writer", active_grants: 0 }),
    a("ledger", "Ledger", "Finance Analyst", { status: "suspended", active_grants: 0, ceiling_preset: "Analyst",
      sponsor: { id: "usr_diego", name: "Diego", hue: 1 } }),
    a("herald", "Herald", "Release Notes", { status: "suspended", active_grants: 0, sponsor: null }),
  ];
}

export function delegations(now: number): Delegation[] {
  const g = (rest: Partial<Delegation> & Pick<Delegation, "id" | "user" | "agents" | "label">): Delegation => ({
    kind: "standing", permissions: CEILING_BUILD, projects: ["acme-web"],
    github: { repos: ["acme/web"], contents: "write", pull_requests: "write", ref_rule: "boss/*" },
    subdelegation: { allowed: false, to: [] }, max_depth: 3, created_at: ago(now, 6 * DAY), expires_at: ahead(now, 24 * DAY),
    purpose: null, status: "active", last_used_at: ago(now, 3 * MIN), last_used_run: "run_8812", ...rest,
  });
  return [
    g({ id: "dlg_01JB7Q", user: { id: "usr_sam", name: "Sam", hue: 6 }, agents: [{ id: "agt_atlas", name: "Atlas" }, { id: "agt_forge", name: "Forge" }, { id: "agt_vera", name: "Vera" }],
      label: "Builders on acme-web", subdelegation: { allowed: true, to: ["Vera", "Rook"] }, purpose: "Build and review acme-web" }),
    g({ id: "dlg_01JC4L", user: { id: "usr_lena", name: "Lena", hue: 3 }, agents: [{ id: "agt_quill", name: "Quill" }, { id: "agt_prism", name: "Prism" }],
      label: "Planner on docs-site", projects: ["docs-site"], github: null, expires_at: ahead(now, 7 * DAY), last_used_at: ago(now, 2 * DAY), last_used_run: "run_8790" }),
    g({ id: "dlg_01JD9T", user: { id: "usr_theo", name: "Theo", hue: 2 }, agents: [{ id: "agt_harbor", name: "Harbor" }, { id: "agt_beacon", name: "Beacon" }],
      label: "Ops on acme-web", expires_at: ahead(now, 14 * DAY), github: null, last_used_at: ago(now, 16 * DAY), last_used_run: null }),
    g({ id: "dlg_01JE2P", user: { id: "usr_param", name: "Param", hue: 4 }, agents: [{ id: "agt_forge", name: "Forge" }, { id: "agt_rook", name: "Rook" }, { id: "agt_sentry", name: "Sentry" }],
      label: "this build only", kind: "task", projects: ["boss"], expires_at: null, github: { repos: ["acme/web", "acme/infra", "acme/docs"], contents: "write", pull_requests: "write", ref_rule: null } }),
    g({ id: "dlg_01JA2C", user: { id: "usr_lena", name: "Lena", hue: 3 }, agents: [{ id: "agt_harbor", name: "Harbor" }],
      label: "deploy previews", kind: "task", status: "expired", expires_at: ago(now, DAY), last_used_run: "run_8790" }),
  ];
}

/** As S1's `_public_provider` answers them (apps/api/app/routers/identity_sso.py). */
export function providers(_now: number): Provider[] {
  const base = { client_id: null, saml_sso_url: null, has_client_secret: true, scopes: ["openid", "email", "profile"], allowed_algs: ["RS256"],
    saml_sp_entity_id: null, saml_certificate_count: 0, trust_idp_mfa: false, enforce_sso: false, groups_claim: null, status: "active" as const };
  return [
    { ...base, id: "idp_okta", name: "Acme SSO", protocol: "oidc", preset: "okta", issuer: "https://acme.okta.com", client_id: "0oa1acme",
      domains: ["acme.dev"], jit_mode: "domain", trust_idp_mfa: true, groups_claim: "groups" },
    { ...base, id: "idp_google", name: "Google", protocol: "oidc", preset: "google", issuer: "https://accounts.google.com", domains: [], jit_mode: "off" },
    { ...base, id: "idp_github", name: "GitHub", protocol: "oauth2", preset: "github", issuer: "https://github.com", domains: [], jit_mode: "off" },
    { ...base, id: "idp_microsoft", name: "Microsoft", protocol: "oidc", preset: "microsoft", issuer: "https://login.microsoftonline.com/common/v2.0", domains: [], jit_mode: "off" },
    { ...base, id: "idp_apple", name: "Apple", protocol: "oidc", preset: "apple", issuer: "https://appleid.apple.com", domains: [], jit_mode: "off" },
  ];
}

export function policy(): SecurityPolicy {
  return {
    mfa_required_roles: ["owner", "admin", "auditor"], session_idle_minutes: 720, session_absolute_hours: 168,
    password_min_length: 12, breach_check: true, key_max_days: 365, key_no_expiry_for_service_accounts: false,
    delegation_max_days: 90, delegation_max_depth: 3, delegation_until_revoked: false, github_require_user_repo_access: true,
    network_mode: { active: false, ends_at: null }, signup: { enabled: false, domains: ["acme.dev"] }, smtp: true,
    public_origin: `https://${HOST}`,
  };
}

type Ev = Partial<AuditEvent> & Pick<AuditEvent, "event_type" | "actor" | "what" | "outcome" | "source" | "headline">;

export function auditEvents(now: number): AuditEvent[] {
  const sam = { id: "usr_sam", name: "Sam", hue: 6 };
  const chainSam = [{ id: "usr_sam", name: "Sam", run: null, kind: "user" as const }, { id: "agt_atlas", name: "Atlas", run: "8811", kind: "agent" as const }, { id: "agt_forge", name: "Forge", run: "8812", kind: "agent" as const }];
  const today = (h: number, m: number, s: number) => {
    const d = new Date(now); d.setUTCHours(h, m, s, 0);
    return d.getTime() > now ? d.getTime() - DAY : d.getTime();
  };
  const rows: (Ev & { t: number })[] = [
    { t: today(10, 52, 30), event_type: "token.denied", actor: { id: "agt_vera", kind: "agent", name: "Vera", hue: 0 }, on_behalf_of: sam, what: "spawn Rook · depth 4 › max 3", outcome: "denied", source: "sandbox", headline: "Vera, acting for Sam, was refused a fourth hop", chain: chainSam, grant_id: "dlg_01JB7Q", token_jti: "rt_7T" },
    { t: today(10, 49, 7), event_type: "authz.denied", actor: { id: "agt_forge", kind: "agent", name: "Forge", hue: 0 }, on_behalf_of: sam, what: "deployment:production · acme-web", outcome: "denied", source: "sandbox", headline: "Forge, acting for Sam, was refused a production deploy",
      chain: chainSam, grant_id: "dlg_01JB7Q", token_jti: "rt_7R", run_id: "run_8812",
      why: [{ held: false, text: "deployment:production is not delegable. No agent may hold it, whatever the grant says." },
        { held: true, text: "Sam holds it (Admin in acme-web), so Sam can deploy it himself." },
        { held: null, text: "Grant dlg_01JB7Q · token rt_7R · depth 2 of 3" }],
      raw: { event_type: "authz.denied", actor_principal_id: "agt_forge", on_behalf_of: "usr_01J9SAM", chain: ["agt_atlas#run_8811", "agt_forge#run_8812"],
        grant_id: "dlg_01JB7Q", token_jti: "rt_7R", permission: "deployment:production", scope: "project:prj_acme_web", details: { reason: "not_delegable", authorization: "[redacted]" } } },
    { t: today(10, 48, 51), event_type: "egress.denied", actor: { id: "agt_forge", kind: "agent", name: "Forge", hue: 0 }, on_behalf_of: sam, what: "PUT /repos/acme/web/pulls/212/merge", outcome: "denied", source: "egress", headline: "Forge was refused a merge through egress", chain: chainSam, grant_id: "dlg_01JB7Q" },
    { t: today(10, 41, 40), event_type: "token.exchanged", actor: { id: "agt_forge", kind: "agent", name: "Forge", hue: 0 }, on_behalf_of: sam, what: "rt_7R ← rt_7P · 6 scopes · aud boss:api", outcome: "ok", source: "internal", headline: "Forge exchanged Atlas's token", chain: chainSam, grant_id: "dlg_01JB7Q", token_jti: "rt_7R" },
    { t: today(10, 41, 3), event_type: "token.issued", actor: { id: "agt_atlas", kind: "agent", name: "Atlas", hue: 0 }, on_behalf_of: sam, what: "rt_7P · grant dlg_01JB7Q", outcome: "ok", source: "internal", headline: "Atlas was minted a run token", chain: chainSam.slice(0, 2), grant_id: "dlg_01JB7Q", token_jti: "rt_7P" },
    { t: today(10, 41, 1), event_type: "delegation.created", actor: { id: "usr_sam", kind: "user", name: "Sam Okafor", hue: 6 }, what: "dlg_01JB7Q · Atlas, Forge, Vera · 30 days", outcome: "ok", source: "102.89.4.17", headline: "Sam let Atlas, Forge and Vera act for him", grant_id: "dlg_01JB7Q" },
    { t: today(10, 40, 58), event_type: "auth.step_up", actor: { id: "usr_sam", kind: "user", name: "Sam Okafor", hue: 6 }, what: "passkey · MacBook Pro", outcome: "ok", source: "102.89.4.17", headline: "Sam confirmed it was him" },
    { t: today(10, 38, 12), event_type: "access_role.updated", actor: { id: "usr_priya", kind: "user", name: "Priya Raman", hue: 5 }, what: "Release Captain v3→v4 · +3 −1", outcome: "ok", source: "81.2.69.160", headline: "Priya changed Release Captain" },
    { t: today(10, 21, 44), event_type: "api_key.used_from_new_ip", actor: { id: "sva_ci_deployer", kind: "service_account", name: "ci-deployer", hue: 1 }, what: "key_7f3k2q · 140.82.112.4 · GitHub", outcome: "notice", source: "140.82.112.4", headline: "ci-deployer's key was used from a new address" },
    { t: today(9, 55, 2), event_type: "auth.login.succeeded", actor: { id: "usr_sam", kind: "user", name: "Sam Okafor", hue: 6 }, what: "passkey · Chrome macOS · familiar", outcome: "ok", source: "102.89.4.17", headline: "Sam signed in" },
    { t: today(9, 12, 37), event_type: "auth.login.failed", actor: { id: "unknown", kind: "unknown", name: "unknown", hue: 7 }, what: "invalid_credentials · 6th from IP", outcome: "failed", source: "45.155.205.9", headline: "A sign-in failed" },
    { t: today(8, 0, 0), event_type: "legacy.password_used", actor: { id: "usr_legacy_shared", kind: "user", name: "Shared login", hue: 3 }, what: "shared console password · 23 days left", outcome: "notice", source: "10.0.4.31", headline: "The shared password was used" },
    { t: now - DAY + 3 * HOUR, event_type: "delegation.revoked", actor: { id: "usr_lena", kind: "user", name: "Lena Fischer", hue: 3 }, what: "dlg_01JA2C · halted 1 run · revoked 3 tokens", outcome: "ok", source: "88.130.9.2", headline: "Lena revoked a grant", grant_id: "dlg_01JA2C" },
    { t: now - DAY - 2 * HOUR, event_type: "user.mfa_reset", actor: { id: "usr_priya", kind: "user", name: "Priya Raman", hue: 5 }, what: "Theo Nakamura · recovery link issued", outcome: "ok", source: "81.2.69.160", headline: "Priya reset Theo's MFA" },
  ];
  const seq = 48_211;
  return rows.map((r, i) => {
    const { t, ...rest } = r;
    const s = seq - (i < 3 ? i * 2 : i + 2);
    return {
      id: `sev_${s}`, seq: s, at: new Date(t).toISOString(), on_behalf_of: null, chain: [], grant_id: null, token_jti: null, run_id: null,
      why: [], raw: { event_type: rest.event_type, actor_principal_id: rest.actor.id, outcome: rest.outcome },
      prev_hash: hashLike(s - 1), hash: hashLike(s), ...rest,
    } as AuditEvent;
  });
}

export function hashLike(n: number): string {
  let h = (n * 2654435761) >>> 0;
  const out: string[] = [];
  for (let i = 0; i < 8; i += 1) { h = (h * 1103515245 + 12345) >>> 0; out.push((h >>> 16).toString(16).padStart(4, "0")); }
  return out.join("");
}

/** Every permission a set of role keys carries, from the generated catalogue and the fixture's custom role. */
export function permissionsOfRole(roleKey: string, custom: AccessRole[]): Permission[] {
  const builtin = (ROLE_PERMISSIONS as Record<string, readonly Permission[]>)[roleKey];
  if (builtin) return [...builtin];
  return custom.find((r) => r.key === roleKey)?.permissions ?? [];
}

export const ALL_PERMISSIONS: readonly Permission[] = PERMISSIONS;
