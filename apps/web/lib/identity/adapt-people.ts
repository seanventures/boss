/**
 * THE REAL IDENTITY API, READ INTO THE CONSOLE'S VIEW TYPES (people, groups, roles, bindings,
 * effective access, sessions, service accounts, grantors).
 *
 * S4 built every screen against an in-process stub whose answers were already in the console's
 * shapes (lib/identity/types.ts). The real routers (apps/api/app/routers/identity_people.py,
 * identity_access.py; engine/boss_engine/identity/{bindings,groups,roles,service_accounts,
 * api_keys,authz}.py) answer flatter shapes, and split what a screen shows across several routes
 * (a person's bindings live at /role-bindings, a group's members at /groups/{id}, an account's
 * keys at /service-accounts/{id}/keys). Every function here is PURE and TOLERANT:
 *
 *   - a field already in the console's shape is kept (the dev-only stub still answers that way);
 *   - otherwise it is derived from the API's fields (name ← display_name, holders ← binding_count,
 *     last_seen_at ← last_login_at, why[] ← authz.Why strings, …);
 *   - a nested list the API does not send defaults to [], so no screen dereferences undefined.
 *
 * Nothing here invents access: a binding shown with a Remove button is a real binding id from
 * the API; a binding synthesised from `access_roles` (when /role-bindings is unreadable) is
 * marked `synthetic` and offers nothing to press.
 */
import { avatarTone } from "./avatar";
import { PERMISSIONS, PERMISSION_META, isPermission, type Permission } from "./permissions.gen";
import type { PrincipalStatus } from "./contract";
import type {
  AccessRole, ApiKey, Binding, EffectiveAccess, EffectiveEntry, Grantor, Group, GroupSource, Person, PersonSource,
  RolePreview, RoleVersion, ScopeLabel, ServiceAccount, SessionRow, SignInSummary, WhyStep,
} from "./types";

type Raw = Record<string, unknown>;

const obj = (v: unknown): Raw => (v && typeof v === "object" && !Array.isArray(v) ? v as Raw : {});
const arr = (v: unknown): unknown[] => (Array.isArray(v) ? v : []);
const str = (v: unknown, fallback = ""): string => (typeof v === "string" ? v : v == null ? fallback : String(v));
const strOrNull = (v: unknown): string | null => (typeof v === "string" && v ? v : null);
const num = (v: unknown, fallback = 0): number => (typeof v === "number" && Number.isFinite(v) ? v : fallback);
const bool = (v: unknown): boolean => v === true;
const has = (r: Raw, k: string): boolean => Object.prototype.hasOwnProperty.call(r, k) && r[k] !== undefined;
const perms = (v: unknown): Permission[] => arr(v).map(String).filter(isPermission);

/** A stable avatar colour from an id, for people and roles the API sends without one (lib/identity/avatar.ts). */
export function hueOf(id: string, seed?: string | null): number {
  return avatarTone(id, seed);
}

function scopeLabel(r: Raw): ScopeLabel {
  const s = r.scope;
  if (s === "org" || (typeof s === "string" && s.startsWith("project:"))) return s as ScopeLabel;
  if (r.scope_type === "project" && r.scope_id) return `project:${str(r.scope_id)}`;
  return "org";
}

function groupSource(v: unknown): GroupSource {
  return v === "scim" || v === "oidc" ? v : "manual";
}

/** Role lookups the adapters use to name a binding's role. */
export type RoleIndex = { byId: Map<string, AccessRole>; byKey: Map<string, AccessRole> };

export function roleIndex(roles: AccessRole[]): RoleIndex {
  return { byId: new Map(roles.map((r) => [r.id, r])), byKey: new Map(roles.map((r) => [r.key, r])) };
}

const titleCase = (key: string) => key.replace(/[_-]+/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());

// ---------------------------------------------------------------------------------------------
// roles

/** GET /v1/identity/access-roles[/{id}] → AccessRole (roles.py _present). */
export function adaptRole(raw: unknown, index?: RoleIndex): AccessRole {
  const r = obj(raw);
  const key = str(r.key);
  const cf = r.cloned_from;
  let cloned: AccessRole["cloned_from"] = null;
  if (cf && typeof cf === "object") cloned = cf as AccessRole["cloned_from"];
  else if (typeof cf === "string" && cf) {
    const src = index?.byId.get(cf);
    cloned = { id: cf, name: src?.name ?? cf, version: src?.version ?? 0, at: "" };
  }
  return {
    id: str(r.id), key, name: str(r.name, titleCase(key)), description: str(r.description),
    builtin: bool(r.builtin), system: bool(r.system),
    permissions: perms(r.permissions),
    bindable_scopes: arr(r.bindable_scopes).filter((s): s is "org" | "project" => s === "org" || s === "project"),
    holders: num(r.holders, num(r.binding_count)),
    version: num(r.version, 1),
    cloned_from: cloned,
    hue: num(r.hue, hueOf(key)),
  };
}

/** Roles a person can see and bind: never the fixed system roles (worker, collector, SCIM). */
export function adaptRoles(raw: unknown): AccessRole[] {
  const first = arr(raw).map((x) => adaptRole(x));
  const index = roleIndex(first);
  return arr(raw).map((x) => adaptRole(x, index)).filter((r) => !r.system);
}

/** GET …/versions → RoleVersion[] (newest first), each summarised against the one before it. */
export function adaptRoleVersions(raw: unknown): RoleVersion[] {
  const rows = arr(raw).map(obj).sort((a, b) => num(b.version) - num(a.version));
  return rows.map((r, i) => {
    let summary = str(r.summary);
    if (!summary) {
      const now = new Set(arr(r.permissions).map(String));
      const before = rows[i + 1] ? new Set(arr(rows[i + 1].permissions).map(String)) : null;
      if (!before) summary = `${now.size} permissions`;
      else {
        const plus = [...now].filter((p) => !before.has(p)).length;
        const minus = [...before].filter((p) => !now.has(p)).length;
        summary = plus || minus ? `+${plus} −${minus}` : "renamed";
      }
    }
    return { version: num(r.version), by: str(r.by ?? r.changed_by, "BOSS"), at: str(r.at ?? r.changed_at), summary };
  });
}

/** Who a principal id is, for names on previews and owner lists. */
export type NameIndex = Map<string, { name: string; hue: number; email?: string }>;

export function nameIndex(people: { id: string; display_name?: string; name?: string; hue?: number; email?: string }[]): NameIndex {
  return new Map(people.map((p) => [p.id, { name: p.display_name ?? p.name ?? p.id, hue: p.hue ?? hueOf(p.id), email: p.email }]));
}

/** POST …/{id}/preview → RolePreview (roles.py preview). Pages/routes/tools have no API source yet. */
export function adaptRolePreview(raw: unknown, names?: NameIndex): RolePreview {
  const r = obj(raw);
  if (has(r, "affected") && has(r, "diff")) return r as unknown as RolePreview;
  const via = arr(r.via).map(obj);
  const ids = arr(r.affected_principals).map(String);
  const groups = new Map<string, Set<string>>();
  const direct = new Set<string>();
  for (const v of via) {
    const pid = str(v.principal_id);
    if (!pid) continue;
    if (v.group) {
      const g = str(v.group);
      if (!groups.has(g)) groups.set(g, new Set());
      groups.get(g)!.add(pid);
    } else direct.add(pid);
  }
  const groupOf = (pid: string) => [...groups].find(([, s]) => s.has(pid))?.[0] ?? null;
  return {
    diff: { added: perms(r.added), removed: perms(r.removed) },
    affected: {
      people: ids.map((id) => ({ id, name: names?.get(id)?.name ?? id, hue: names?.get(id)?.hue ?? hueOf(id), via: groupOf(id) })),
      direct: direct.size,
      via_groups: [...groups].map(([group, s]) => ({ group, count: s.size })),
      service_accounts: 0,
      agents: [],
      grants: arr(r.affected_grants).length,
    },
    pages: { gained: [], lost: [] },
    routes: { gained: 0, lost: 0 },
    tools: { gained: 0, lost: 0 },
    warnings: perms(r.critical_added).map((p) => ({ permission: p, text: "critical" })),
  };
}

// ---------------------------------------------------------------------------------------------
// bindings, groups

/** GET /v1/identity/role-bindings → Binding (bindings.py _present). */
export function adaptBinding(raw: unknown, index?: RoleIndex): Binding & { subject_type: "principal" | "group" } {
  const r = obj(raw);
  const vg = r.via_group && typeof r.via_group === "object" ? obj(r.via_group) : null;
  const roleKey = str(r.role_key);
  const role = index?.byId.get(str(r.role_id)) ?? index?.byKey.get(roleKey);
  return {
    id: str(r.id), role_id: str(r.role_id, role?.id ?? ""), role_key: roleKey || role?.key || "",
    role_name: str(r.role_name, role?.name ?? titleCase(roleKey)),
    scope: scopeLabel(r), subject_id: str(r.subject_id),
    subject_type: r.subject_type === "group" || vg ? "group" : "principal",
    via_group: vg ? { id: str(vg.id), name: str(vg.name, str(vg.id)), source: groupSource(vg.source ?? vg.provider) } : null,
    granted_by: strOrNull(r.granted_by), granted_by_name: strOrNull(r.granted_by_name),
    granted_at: str(r.granted_at), expires_at: strOrNull(r.expires_at),
  };
}

/** "granted by Ada" — a name, or plain words; never a principal id (FX-C P1-11). */
export function grantedByWords(b: Pick<Binding, "granted_by" | "granted_by_name">): string {
  if (b.granted_by_name) return b.granted_by_name;
  if (!b.granted_by) return "BOSS";
  return /^(sys_|sso:|scim)/.test(b.granted_by) ? "BOSS" : "an admin";
}

/**
 * GET /v1/identity/groups (list rows carry member_count) or /groups/{id} (members with
 * principal_id, display_name) → Group. Bindings come from /role-bindings?subject_id={id}.
 */
export function adaptGroup(raw: unknown, ctx: { bindings?: Binding[]; names?: NameIndex } = {}): Group {
  const r = obj(raw);
  const source = groupSource(r.source);
  const members = arr(r.members).map(obj).map((m) => {
    const id = str(m.id ?? m.principal_id);
    const known = ctx.names?.get(id);
    return { id, name: str(m.name ?? m.display_name, known?.name ?? id), email: str(m.email, known?.email ?? ""),
      hue: num(m.hue, known?.hue ?? hueOf(id)), source: groupSource(m.source) };
  });
  return {
    id: str(r.id), name: str(r.name), description: str(r.description), source,
    provider: has(r, "provider") ? strOrNull(r.provider) : source === "manual" ? null : source.toUpperCase(),
    synced_at: strOrNull(r.synced_at),
    members,
    member_count: has(r, "member_count") ? num(r.member_count) : members.length,
    bindings: has(r, "bindings") ? arr(r.bindings).map((b) => adaptBinding(b)) : ctx.bindings ?? [],
  };
}

// ---------------------------------------------------------------------------------------------
// people

function signInOf(r: Raw): SignInSummary {
  if (has(r, "sign_in")) return r.sign_in as SignInSummary;
  const f = obj(r.factors);
  const sso = arr(r.sso_links).map(String);
  if (has(r, "factors")) {
    return { passkeys: num(f.passkeys), totp: bool(f.totp), password: bool(r.password), sso: sso[0] ?? null, idp_mfa: null };
  }
  // The list view says only whether a second factor is on.
  return { passkeys: 0, totp: false, password: bool(r.password), sso: sso[0] ?? null, idp_mfa: bool(r.mfa) ? "MFA on" : null };
}

function status(v: unknown): PrincipalStatus {
  return v === "pending" || v === "active" || v === "disabled" || v === "suspended" || v === "revoked" ? v : "active";
}

/**
 * GET /v1/identity/users[/{id}] → Person (identity_people.py _person_view). `ctx.bindings` are
 * the person's own plus their groups' (from /role-bindings); without them, `access_roles` is
 * shown as org-scope chips marked synthetic.
 */
export function adaptPerson(raw: unknown, ctx: { bindings?: Binding[]; groups?: Person["groups"]; index?: RoleIndex } = {}): Person {
  const r = obj(raw);
  const id = str(r.id);
  const bindings: Binding[] = has(r, "bindings") ? arr(r.bindings).map((b) => adaptBinding(b, ctx.index))
    : ctx.bindings ?? arr(r.access_roles).map(String).map((key) => {
      const role = ctx.index?.byKey.get(key);
      return { id: `synthetic:${key}`, role_id: role?.id ?? "", role_key: key, role_name: role?.name ?? titleCase(key),
        scope: "org" as ScopeLabel, subject_id: id, via_group: null, granted_by: null, granted_at: "", expires_at: null,
        synthetic: true } as Binding;
    });
  const sso = arr(r.sso_links);
  const source: PersonSource = r.source === "sso" || r.source === "scim" || r.source === "local" ? r.source : sso.length ? "sso" : "local";
  return {
    id, display_name: str(r.display_name, str(r.name, id)), email: str(r.email), status: status(r.status), source,
    bindings,
    groups: has(r, "groups") ? arr(r.groups) as Person["groups"] : ctx.groups ?? [],
    sign_in: signInOf(r),
    last_seen_at: strOrNull(r.last_seen_at ?? r.last_login_at),
    last_seen_from: strOrNull(r.last_seen_from),
    joined_at: str(r.joined_at ?? r.created_at),
    joined_by: strOrNull(r.joined_by),
    seat: strOrNull(r.seat),
    self_signed_up: bool(r.self_signed_up),
    network_bound: bool(r.network_bound),
    hue: num(r.hue, hueOf(id, strOrNull(r.avatar_seed))),
  };
}

/** The directory as screens need it: people with their bindings and groups, groups with theirs. */
export type Directory = { people: Person[]; roles: AccessRole[]; groups: Group[]; bindings: Binding[] };

/**
 * Join what the API serves on separate routes. `bindingsRaw` is GET /role-bindings (every binding
 * in the org) or null when unreadable; `groupDetails` are GET /groups/{id} answers (members).
 */
export function assembleDirectory(input: {
  users: unknown; roles: unknown; groups?: unknown; groupDetails?: unknown[]; bindings?: unknown | null;
}): Directory {
  const roles = adaptRoles(input.roles);
  const index = roleIndex(roles);
  const rawUsers = arr(input.users).map(obj);
  const names = nameIndex(rawUsers.map((u) => ({ id: str(u.id), display_name: str(u.display_name, str(u.name)), email: str(u.email) })));
  const all = input.bindings == null ? null : arr(input.bindings).map((b) => adaptBinding(b, index));
  const details = new Map(arr(input.groupDetails).map(obj).map((g) => [str(g.id), g]));
  const groups = arr(input.groups).map(obj).map((g) => {
    const detail = details.get(str(g.id));
    return adaptGroup({ ...g, ...(detail ?? {}) }, { names, bindings: all?.filter((b) => b.subject_type === "group" && b.subject_id === str(g.id)) });
  });
  const people = rawUsers.map((u) => {
    const id = str(u.id);
    const mine = groups.filter((g) => g.members.some((m) => m.id === id));
    const bindings = all === null ? undefined : [
      ...all.filter((b) => b.subject_type === "principal" && b.subject_id === id),
      ...mine.flatMap((g) => g.bindings.map((b) => ({ ...b, via_group: b.via_group ?? { id: g.id, name: g.name, source: g.source } }))),
    ];
    return adaptPerson(u, { index, bindings, groups: mine.map((g) => ({ id: g.id, name: g.name, source: g.source, provider: g.provider })) });
  });
  return { people, roles, groups, bindings: all ?? [] };
}

// ---------------------------------------------------------------------------------------------
// effective access

const WHY = /^role (\S+)(?: ← group (.+?)(?: \(from \w+\))?)? ← binding at (\S+)$/;

/** One authz.Why string ("role operator ← group Builders ← binding at project:x") as a WhyStep. */
export function parseWhy(text: string, index?: RoleIndex): WhyStep {
  const m = WHY.exec(text.trim());
  if (!m) return { role: text, role_key: "", scope: "org", via_group: null };
  const role = index?.byKey.get(m[1]);
  return { role: role?.name ?? titleCase(m[1]), role_key: m[1], scope: m[3] as ScopeLabel, via_group: m[2] ?? null };
}

function heldBy(p: Permission): string | null {
  const roles = PERMISSION_META[p].roles;
  if (roles.includes("admin")) return "Admin at org";
  if (roles.includes("owner")) return "Owner only";
  return null;
}

/**
 * GET /v1/identity/principals/{id}/effective → EffectiveAccess. The API lists only what is held,
 * each with its `via` chain; the console's matrix shows every grantable permission, held or not.
 */
export function adaptEffective(raw: unknown, index?: RoleIndex): EffectiveAccess {
  const r = obj(raw);
  if (has(r, "entries")) return r as unknown as EffectiveAccess;
  const held = new Map<string, string[]>();
  for (const e of arr(r.permissions)) {
    if (typeof e === "string") held.set(e, []);
    else { const o = obj(e); held.set(str(o.key), arr(o.via).map(String)); }
  }
  const entries: EffectiveEntry[] = PERMISSIONS.filter((p) => !PERMISSION_META[p].systemOnly).map((p) => {
    const via = held.get(p);
    return { permission: p, held: via !== undefined, why: (via ?? []).map((w) => parseWhy(w, index)), held_by: via ? null : heldBy(p) };
  });
  return {
    principal_id: str(r.principal_id), scope: scopeLabel(r),
    held: entries.filter((e) => e.held).length, total: entries.length,
    by_project: has(r, "by_project") ? r.by_project as Record<string, number> : {},
    entries,
  };
}

// ---------------------------------------------------------------------------------------------
// sessions, service accounts, grantors

/** A browser name and device from a User-Agent ("Chrome on macOS"). */
export function describeAgent(ua: string): { client: string; device: SessionRow["device"] } {
  const u = ua || "";
  const browser = /Edg\//.test(u) ? "Edge" : /Firefox\//.test(u) ? "Firefox" : /Chrome\//.test(u) ? "Chrome"
    : /Safari\//.test(u) ? "Safari" : /curl|python|node|Go-http/i.test(u) ? "API client" : u ? "Browser" : "Unknown client";
  const os = /iPhone|iPad/.test(u) ? "iOS" : /Android/.test(u) ? "Android" : /Mac OS X|Macintosh/.test(u) ? "macOS"
    : /Windows/.test(u) ? "Windows" : /Linux/.test(u) ? "Linux" : "";
  const device = /iPad|Tablet/.test(u) ? "tablet" : /iPhone|Android.*Mobile|Mobile/.test(u) ? "phone" : "desktop";
  return { client: os ? `${browser} on ${os}` : browser, device };
}

/** GET /v1/identity/sessions?principal_id= → SessionRow (identity_people.py list_sessions). */
export function adaptSession(raw: unknown): SessionRow {
  const r = obj(raw);
  const ua = describeAgent(str(r.user_agent));
  return {
    id: str(r.id), current: bool(r.current),
    device: r.device === "phone" || r.device === "tablet" || r.device === "desktop" ? r.device : ua.device,
    client: str(r.client, ua.client), location: strOrNull(r.location), ip: strOrNull(r.ip),
    methods: arr(r.methods).map(String), started_at: str(r.started_at ?? r.created_at), last_seen_at: str(r.last_seen_at),
  };
}

/**
 * GET /v1/identity/service-accounts → ServiceAccount (service_accounts.public: display_name,
 * owners as ids). Keys come from /service-accounts/{id}/keys, bindings from /role-bindings.
 */
export function adaptServiceAccount(raw: unknown, ctx: { keys?: ApiKey[]; bindings?: Binding[]; names?: NameIndex; roles?: AccessRole[] } = {}): ServiceAccount {
  const r = obj(raw);
  const id = str(r.id);
  const owners = arr(r.owners).map((o) => {
    if (o && typeof o === "object") { const x = obj(o); return { id: str(x.id), name: str(x.name, str(x.id)), hue: num(x.hue, hueOf(str(x.id))) }; }
    const oid = String(o);
    const known = ctx.names?.get(oid);
    return { id: oid, name: known?.name ?? oid, hue: known?.hue ?? hueOf(oid) };
  });
  const keys = has(r, "keys") ? arr(r.keys) as ApiKey[] : ctx.keys ?? [];
  const bindings = has(r, "bindings") ? arr(r.bindings).map((b) => adaptBinding(b)) : ctx.bindings ?? [];
  const used = keys.filter((k) => k.last_used_at).sort((a, b) => str(b.last_used_at).localeCompare(str(a.last_used_at)))[0];
  const roleByKey = new Map((ctx.roles ?? []).map((x) => [x.key, x]));
  const effective = has(r, "effective") ? num(r.effective)
    : new Set(bindings.flatMap((b) => roleByKey.get(b.role_key)?.permissions ?? [])).size;
  const kind = r.kind === "scim" || r.kind === "legacy" || r.kind === "service" ? r.kind : id.startsWith("sys_scim") ? "scim" : "service";
  return {
    id, name: str(r.name ?? r.display_name, id), description: str(r.description), owners, bindings, keys,
    status: status(r.status), kind, created_by: strOrNull(r.created_by), created_at: str(r.created_at),
    last_used: has(r, "last_used") ? (r.last_used as ServiceAccount["last_used"])
      : used ? { at: str(used.last_used_at), client: str(used.last_used_client, used.name) } : null,
    effective, orphaned: bool(r.orphaned),
  };
}

/** GET /v1/identity/grantors → Grantor (bindings.grantors: principal_id, display_name). */
export function adaptGrantor(raw: unknown): Grantor {
  const r = obj(raw);
  const id = str(r.id ?? r.principal_id);
  return { id, name: str(r.name ?? r.display_name, id), role: str(r.role), hue: num(r.hue, hueOf(id, strOrNull(r.avatar_seed))) };
}
