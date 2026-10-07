/**
 * THE CONSOLE'S IDENTITY CONTRACT (docs/product/IDENTITY.md §6.5, §10; frozen in Phase 0).
 *
 * Mirrors engine/boss_engine/identity/contracts.py field for field; tests/identity-contract.test.ts
 * fails when the two drift. The console MIRRORS the API's answers for UX — it disables a control
 * with the reason, hides an area the viewer can never open — and never decides anything: the API
 * re-checks every call.
 */
import { isPermission, PERMISSION_META, type Permission } from "./permissions.gen";
import { permissionName } from "./words";

export type { Permission } from "./permissions.gen";

export type PrincipalKind = "user" | "service_account" | "agent" | "system";
export type PrincipalStatus = "pending" | "active" | "disabled" | "suspended" | "revoked";

export interface MeOrg {
  id: string;
  name: string;
}

export interface MeSignInMethods {
  password: boolean;
  totp: boolean;
  passkeys: number;
  recovery_codes_remaining: number;
  sso_links: string[];
}

export interface MeBanner {
  kind: "network_bridge" | "legacy_password" | "legacy_api_key" | "owner_recovered" | "mfa_enrolment_required" | "recovery_codes_low" | "api_key_expiring" | "new_device";
  message: string;
  until: string | null;
}

/** GET /v1/identity/me */
export interface MeResponse {
  principal_id: string;
  kind: PrincipalKind;
  display_name: string;
  email: string | null;
  org: MeOrg;
  status: PrincipalStatus;
  access_roles: string[];
  sign_in: MeSignInMethods;
  mfa_required: boolean;
  mfa_at: string | null;
  session_id: string | null;
  session_expires_at: string | null;
  /** Send as the X-Boss-CSRF header on every cookie-authenticated write. */
  csrf_token: string;
  /** Refetch /me/permissions whenever this changes (the console socket pushes it). */
  epoch: number;
  network_bound: boolean;
  banners: MeBanner[];
  /** The person's chosen avatar colour, "0"–"7"; null: derived from the id (lib/identity/avatar.ts). */
  avatar_seed: string | null;
}

/** GET /v1/identity/me/permissions?project_id= */
export interface MePermissionsResponse {
  epoch: number;
  /** "org" or "project:<id>" */
  scope: string;
  permissions: string[];
  step_up_fresh_until: string | null;
  catalogue_fingerprint: string;
}

export const ME_PATH = "/v1/identity/me";
export const ME_PERMISSIONS_PATH = "/v1/identity/me/permissions";
export const CSRF_HEADER = "X-Boss-CSRF";

/** Where a check applies: the org, or one project. */
export type CanScope = { kind: "org" } | { kind: "project"; projectId: string };

export interface CanResult {
  allowed: boolean;
  /** Tooltip for a disabled control: "Needs “Publish workflows” · ask an Admin" (lib/identity/words.ts). */
  reason: string;
  /** True when allowed but the action will first ask for step-up (passkey/TOTP). */
  needsStepUp: boolean;
}

/** One fetched answer of /me/permissions, at the scope it was fetched for. */
export interface PermissionSnapshot {
  scope: CanScope;
  permissions: ReadonlySet<Permission>;
  stepUpFreshUntil: number | null;
  epoch: number;
}

export function snapshotFrom(response: MePermissionsResponse): PermissionSnapshot {
  const scope: CanScope = response.scope.startsWith("project:")
    ? { kind: "project", projectId: response.scope.slice("project:".length) }
    : { kind: "org" };
  return {
    scope,
    // A key the console's catalogue does not know (an older console against a newer API) is
    // dropped: unknown means "cannot", never "can".
    permissions: new Set(response.permissions.filter(isPermission)),
    stepUpFreshUntil: response.step_up_fresh_until ? Date.parse(response.step_up_fresh_until) : null,
    epoch: response.epoch,
  };
}

function sameScope(a: CanScope, b: CanScope): boolean {
  return a.kind === b.kind && (a.kind === "org" || (b.kind === "project" && a.projectId === b.projectId));
}

/**
 * Can the viewer do `permission` at `scope`? UX only. A snapshot for a different scope answers
 * "cannot" (fetch the right one) rather than guess — a guess is how hidden UI becomes authorization.
 */
export function can(
  snapshot: PermissionSnapshot | null,
  permission: Permission,
  scope: CanScope = { kind: "org" },
  now: number = Date.now(),
): CanResult {
  const meta = PERMISSION_META[permission];
  if (!snapshot) return { allowed: false, reason: "Loading your access…", needsStepUp: false };
  if (!sameScope(snapshot.scope, scope)) {
    return { allowed: false, reason: "Access for this project is still loading", needsStepUp: false };
  }
  if (!snapshot.permissions.has(permission)) {
    return { allowed: false, reason: `Needs “${permissionName(permission)}”`, needsStepUp: false };
  }
  const fresh = snapshot.stepUpFreshUntil !== null && snapshot.stepUpFreshUntil > now;
  const needsStepUp = meta.stepUp && !fresh;
  return { allowed: true, reason: needsStepUp ? "You'll confirm it's you first" : "", needsStepUp };
}
