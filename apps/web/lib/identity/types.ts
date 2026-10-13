/**
 * THE IDENTITY CONSOLE'S VIEW OF THE ADMINISTRATION API (IDENTITY.md §10, §11).
 *
 * Only /v1/identity/me and /me/permissions are frozen (contract.ts). Everything here is the
 * console's reading of the route list in IDENTITY.md §10 — the INTEGRATION SEAM with streams
 * S1 (people, sessions, keys, providers), S2 (groups, roles, bindings, effective), S3 (agents,
 * delegations, cascade) and S5 (posture, policy, audit). When their OpenAPI lands, each shape is
 * checked against it (tests/identity-api-shapes.test.ts lists the paths) and the stub server in
 * lib/identity/stub/ is retired route by route. No secret field exists anywhere below except
 * `IssuedSecret.secret`, which is the shown-once value and is never stored by the console.
 */
import type { Permission } from "./permissions.gen";
import type { PrincipalStatus } from "./contract";

export type ScopeLabel = "org" | `project:${string}`;

export interface Ref {
  id: string;
  name: string;
}

/** A role held at a scope, directly or through a group. */
export interface Binding {
  id: string;
  role_id: string;
  role_key: string;
  role_name: string;
  scope: ScopeLabel;
  subject_id: string;
  via_group: (Ref & { source: GroupSource }) | null;
  granted_by: string | null;
  /** Who granted it, in words (the API's display name); the raw id is never shown. */
  granted_by_name?: string | null;
  granted_at: string;
  expires_at: string | null;
  /** Drawn from a person's `access_roles` when /role-bindings was unreadable: nothing to remove. */
  synthetic?: boolean;
}

export type GroupSource = "manual" | "scim" | "oidc";
export type PersonSource = "local" | "sso" | "scim";

export interface SignInSummary {
  passkeys: number;
  totp: boolean;
  password: boolean;
  sso: string | null;
  /** MFA satisfied by the IdP ("Okta MFA"), when the provider is trusted for it. */
  idp_mfa: string | null;
}

export interface Person {
  id: string;
  display_name: string;
  email: string;
  status: PrincipalStatus;
  source: PersonSource;
  bindings: Binding[];
  groups: (Ref & { source: GroupSource; provider: string | null })[];
  sign_in: SignInSummary;
  last_seen_at: string | null;
  last_seen_from: string | null;
  joined_at: string;
  joined_by: string | null;
  seat: string | null;
  self_signed_up: boolean;
  network_bound: boolean;
  /** Avatar hue, chosen by the API so every screen draws the same person the same way. */
  hue: number;
}

export interface WhyStep {
  role: string;
  role_key: string;
  scope: ScopeLabel;
  via_group: string | null;
}

export interface EffectiveEntry {
  permission: Permission;
  held: boolean;
  why: WhyStep[];
  /** For a permission not held: the built-in roles that carry it ("Admin at org"). */
  held_by: string | null;
}

export interface EffectiveAccess {
  principal_id: string;
  scope: ScopeLabel;
  held: number;
  total: number;
  /** Held somewhere at project scope only, by project. */
  by_project: Record<string, number>;
  entries: EffectiveEntry[];
}

export interface SessionRow {
  id: string;
  current: boolean;
  device: "desktop" | "phone" | "tablet";
  client: string;
  location: string | null;
  ip: string | null;
  methods: string[];
  started_at: string;
  last_seen_at: string;
}

export type FactorKind = "passkey" | "totp" | "recovery" | "sso";

export interface Factor {
  id: string;
  kind: FactorKind;
  name: string;
  detail: string;
  last_used_at: string | null;
  note: string | null;
}

export interface Group {
  id: string;
  name: string;
  description: string;
  source: GroupSource;
  provider: string | null;
  synced_at: string | null;
  /** `source`: where THIS membership comes from — a manual group can hold members an IdP
   *  sign-in synced (a mapped IdP group), and those are read-only like a synced group's. */
  members: (Ref & { email: string; hue: number; source?: GroupSource })[];
  /** The API's count (a list row carries no members). */
  member_count?: number;
  bindings: Binding[];
}

export interface AccessRole {
  id: string;
  key: string;
  name: string;
  description: string;
  builtin: boolean;
  /** A fixed system role (worker, collector, SCIM): never listed, bound or cloned. */
  system?: boolean;
  permissions: Permission[];
  bindable_scopes: ("org" | "project")[];
  holders: number;
  version: number;
  cloned_from: (Ref & { version: number; at: string }) | null;
  /** Display hue for its chip. */
  hue: number;
}

export interface RoleVersion {
  version: number;
  by: string;
  at: string;
  summary: string;
}

/** POST /v1/identity/access-roles/{id}/preview */
export interface RolePreview {
  diff: { added: Permission[]; removed: Permission[] };
  affected: {
    people: (Ref & { hue: number; via: string | null })[];
    direct: number;
    via_groups: { group: string; count: number }[];
    service_accounts: number;
    agents: { id: string; name: string; grants: number; change: string | null }[];
    grants: number;
  };
  pages: { gained: string[]; lost: string[] };
  routes: { gained: number; lost: number };
  tools: { gained: number; lost: number };
  warnings: { permission: Permission; text: string }[];
}

export type KeyStatus = "active" | "grace" | "revoked" | "expired";

export interface ApiKey {
  /** The random public id (`key_7f3k2q`). BOSS shows no part of the secret, ever (D20). */
  id: string;
  name: string;
  owner: Ref & { kind: "user" | "service_account" };
  scopes: Permission[];
  projects: string[] | null;
  networks: string | null;
  created_at: string;
  last_used_at: string | null;
  last_used_ip: string | null;
  last_used_client: string | null;
  expires_at: string | null;
  grace_until: string | null;
  rotated_from: string | null;
  status: KeyStatus;
}

/** The ONE response that carries a secret. Rendered once, never stored or logged. */
export interface IssuedSecret {
  key: ApiKey;
  secret: string;
  replaces: { id: string; stops_at: string | null } | null;
}

export interface ServiceAccount {
  id: string;
  name: string;
  description: string;
  owners: (Ref & { hue: number })[];
  bindings: Binding[];
  keys: ApiKey[];
  status: PrincipalStatus;
  kind: "service" | "scim" | "legacy";
  created_by: string | null;
  created_at: string;
  last_used: { at: string; client: string } | null;
  effective: number;
  orphaned: boolean;
}

export type AgentStatus = "active" | "suspended" | "revoked";

export interface Passport {
  id: string;
  name: string;
  role_key: string;
  title: string;
  sponsor: (Ref & { hue: number }) | null;
  ceiling_preset: string | null;
  ceiling: Permission[];
  external_ceiling: Record<string, Record<string, string>>;
  can_spawn: boolean;
  allowed_children: string[];
  max_depth: number;
  autonomous: boolean;
  status: AgentStatus;
  sealed: boolean;
  active_grants: number;
  last_run_at: string | null;
  lineage: { at: string; event: string; detail: string }[];
}

export type GrantKind = "standing" | "task" | "schedule";
export type GrantStatus = "active" | "paused" | "revoked" | "expired";

export interface GithubResource {
  repos: string[];
  contents: "read" | "write" | null;
  pull_requests: "read" | "write" | null;
  ref_rule: string | null;
}

export interface Delegation {
  id: string;
  kind: GrantKind;
  user: Ref & { hue: number };
  agents: Ref[];
  label: string;
  permissions: Permission[];
  projects: string[];
  github: GithubResource | null;
  subdelegation: { allowed: boolean; to: string[] };
  max_depth: number;
  created_at: string;
  expires_at: string | null;
  purpose: string | null;
  status: GrantStatus;
  last_used_at: string | null;
  last_used_run: string | null;
}

export type HopKind = "person" | "grant" | "agent";

export interface ChainHop {
  kind: HopKind;
  id: string;
  name: string;
  subtitle: string;
  /** The small caps line on the card ("ACT · DEPTH 1 · PLANS"). */
  stage: string;
  count: number;
  /** "SAM HOLDS", "GRANTED", "ATLAS MAY". */
  count_label: string;
  effective: Permission[];
  dropped: Permission[];
  external: { label: string; dropped: boolean }[];
  /** What narrowed at this hop ("∩ ceiling(Atlas) · token rt_7P"). */
  rule: string;
  live: boolean;
  hue: number;
}

export interface TokenEvent {
  at: string;
  text: string;
  agent: string;
  jti: string | null;
  outcome: "live" | "refused" | "expired";
}

export interface EgressRow {
  method: string;
  path: string;
  status: number;
  note: string | null;
}

export interface DelegationDetail extends Delegation {
  run: { id: string; title: string } | null;
  chain: ChainHop[];
  tokens: TokenEvent[];
  egress: EgressRow[];
  checked: string | null;
}

/** GET /v1/identity/delegations/{id}/cascade-preview, and POST …/revoke {dry_run:true} (S3 cascade). */
export interface CascadePreview {
  items: { kind: "runs" | "tokens" | "github" | "grants" | "schedules"; label: string; detail: string; count: number }[];
  halts: number;
}

export type PostureStatus = "ok" | "warn" | "bad" | "critical";

export interface PostureCheck {
  /** `origin`: is a public URL bound (H2)? "bad" while none is: emailed links are off. */
  id: "mfa" | "keys" | "delegations" | "legacy" | "owners" | "audit" | "enforcement" | "origin" | "github";
  label: string;
  status: PostureStatus;
  value: string;
  unit: string | null;
  detail: string;
  fix: { label: string; href: string } | null;
}

export interface Posture {
  score: number;
  headline: string;
  summary: string[];
  checks: PostureCheck[];
  counts: {
    people: number; active: number; invited: number; disabled: number;
    agents: number; suspended_agents: number; groups: number; roles_builtin: number; roles_custom: number;
    service_accounts: number; active_grants: number; runs_acting: number; tokens_out: number;
  };
  acting_now: { user: Ref & { hue: number }; agents: Ref[]; project: string; label: string }[];
  audit: { verified_at: string | null; seq: number; ok: boolean };
  legacy: { kind: string; label: string; until: string | null }[];
}

export type AuditOutcome = "ok" | "denied" | "failed" | "notice";

export interface AuditEvent {
  id: string;
  seq: number;
  at: string;
  event_type: string;
  actor: { id: string; kind: "user" | "agent" | "service_account" | "system" | "unknown"; name: string; hue: number };
  on_behalf_of: (Ref & { hue: number }) | null;
  chain: (Ref & { run: string | null; kind: "user" | "agent" })[];
  grant_id: string | null;
  token_jti: string | null;
  run_id: string | null;
  what: string;
  outcome: AuditOutcome;
  source: string;
  headline: string;
  why: { held: boolean | null; text: string }[];
  /** Already redacted by the API (S5's one filter); rendered as data, never re-parsed. */
  raw: Record<string, unknown>;
  prev_hash: string;
  hash: string;
}

export interface AuditPage {
  events: AuditEvent[];
  cursor: string | null;
  chain: { verified_at: string | null; seq: number; ok: boolean; first_break: number | null };
}

/**
 * GET /v1/identity/providers → {providers: Provider[]} (routers/identity_sso.py _public_provider).
 * No secret, ever: `has_client_secret` says whether one is stored. PATCH takes the writable keys
 * (name, domains, jit_mode, trust_idp_mfa, enforce_sso, status, …) and answers the same shape.
 */
export interface Provider {
  id: string;
  name: string;
  protocol: "oidc" | "saml" | "oauth2";
  preset: "google" | "github" | "microsoft" | "apple" | "okta" | "entra" | "keycloak" | "generic";
  issuer: string | null;
  client_id: string | null;
  saml_sso_url: string | null;
  has_client_secret: boolean;
  scopes: string[];
  allowed_algs: string[];
  saml_sp_entity_id: string | null;
  saml_certificate_count: number;
  domains: string[];
  jit_mode: "off" | "domain";
  trust_idp_mfa: boolean;
  enforce_sso: boolean;
  groups_claim: string | null;
  status: "active" | "disabled";
}

export interface SecurityPolicy {
  mfa_required_roles: string[];
  session_idle_minutes: number;
  session_absolute_hours: number;
  password_min_length: number;
  breach_check: boolean;
  key_max_days: number;
  key_no_expiry_for_service_accounts: boolean;
  delegation_max_days: number;
  delegation_max_depth: number;
  delegation_until_revoked: boolean;
  /** The EFFECTIVE value; PUT null to make it automatic again (D28). */
  github_require_user_repo_access: boolean;
  /** Read only: true while the setting follows the install (on once more than one person). */
  github_require_user_repo_access_auto?: boolean;
  /** S5's network bridge. Only {active:false} may be PUT ("Require sign-in"); turning it back on is 409. */
  network_mode: { active: boolean; ends_at: string | null };
  signup: { enabled: boolean; domains: string[] };
  /** Read only: is email (SMTP) configured. A status, never a switch. */
  smtp: boolean;
  public_origin: string | null;
  /** Other policy keys the API serves (step_up_minutes, key_default_days, …), kept as read. */
  [extra: string]: unknown;
}

/**
 * GET /v1/auth/options — what the sign-in page may show. SEAM (S1): IDENTITY.md §9.7 needs it
 * (icon row only for configured providers, email tabs only with SMTP, self sign-up footer only
 * when on, the claim door) but §10 does not list it. Public, cacheable for 60 s, no secrets.
 */
export interface AuthOptions {
  org_name: string;
  host: string;
  providers: { id: string; name: string; preset: Provider["preset"] }[];
  smtp: boolean;
  signup: boolean;
  legacy_password: boolean;
  /** After the claim, while the shared console password still signs in (as Operator): when it
   *  stops. Null or absent: not offered. */
  shared_password?: { until: string | null } | null;
  /** S1: passkeys work only once a public origin is bound; false hides "Sign in with a passkey". */
  passkey?: boolean;
  /** Why passkeys can't be offered: passkey_needs_domain (an IP address), passkey_needs_https, or
   *  public_origin_unset (nothing bound yet — a claim binds it). Null/absent: they can. */
  passkey_reason?: string | null;
  /** The public URL BOSS binds cookies, passkeys and emailed links to (H2). Null: none bound —
   *  emailed sign-in cannot work, so it is not offered. Absent: an API that does not say. */
  public_origin?: string | null;
  /** Where it came from: BOSS_PUBLIC_URL ("env") or bound when the install was claimed ("claim"). */
  public_origin_source?: "env" | "claim" | null;
  claim: null | {
    path: "setup_token" | "legacy_password" | "network_bridge";
    /** Every proof this browser may claim with (the API's `available_paths`): a page holding a
     *  setup link (`#t=`) uses it even where `path` names the shared password (lib/identity/claim-path). */
    paths?: ("setup_token" | "legacy_password" | "network")[];
    origin: string | null;
    expires_at: string | null;
    seats: string[];
    /** The install has an admin key that can issue a claim link over the API
     *  (POST /v1/identity/setup-link), for a host with no shell. */
    link_over_api?: boolean;
  };
}

/** POST /v1/auth/login and every next-step answer of the sign-in flow. */
export interface SignInStep {
  next: "mfa" | "done" | "enrol";
  /** With next "enrol" (H1): false when this sign-in may NOT enrol a factor (only an admin
   *  resetting the second factor helps); true or absent: go and enrol. */
  enrolment_grant?: boolean;
  /** After a recovery-code sign-in that left fewer than 3 codes: make new ones now (§4.7). */
  must_regenerate_recovery_codes?: boolean;
  methods: ("totp" | "passkey" | "recovery")[];
  who: { display_name: string; email: string; hue: number } | null;
}

export interface Grantor extends Ref {
  role: string;
  hue: number;
}

export interface Connection {
  id: string;
  provider: string;
  account: string;
  scopes: string[];
  status: "active" | "needs_reauth" | "revoked";
  linked_at: string;
}
