/**
 * Stub routes for Identity › Groups and People extras: group create/rename/delete, group role
 * bindings (org and project), access requests (list, approve, deny — FX-B's contract), and an
 * admin marking an email verified. Dev-only double; registered by stub/server.ts.
 *
 * The core table answers GET /groups and GET /groups/{id} from fixtures; groupsOf(s) is the
 * state-aware list (created, renamed, deleted, with bound roles) that those two routes and the
 * core POST/DELETE /role-bindings should call for a group subject (see stub/server.ts).
 */
import * as F from "./fixtures";
import type { StubOn } from "./server";
import { stubAudit, stubErr, stubNotFound, stubOk } from "./server";
import type { Binding, Group, Person, ScopeLabel } from "../types";
import type { AccessRequestRow } from "../types-people";
import { PERMISSION_META, isPermission } from "../permissions.gen";

type Overlay = {
  created: Group[];
  edited: Record<string, { name?: string; description?: string }>;
  deleted: Set<string>;
  added: Binding[];
  removed: Set<string>;
  requests: AccessRequestRow[];
};

const OVERLAYS = new WeakMap<object, Overlay>();

type S = { t0: number; people: Person[]; roles: { id: string; key: string; name: string; permissions: string[]; bindable_scopes: string[]; system?: boolean }[] };

function overlay(s: S): Overlay {
  let o = OVERLAYS.get(s);
  if (!o) {
    const iso = (ms: number) => new Date(s.t0 - ms).toISOString();
    o = {
      created: [], edited: {}, deleted: new Set(), added: [], removed: new Set(),
      requests: [
        { id: "acr_demo1", requester_principal_id: "usr_lena", requester_name: "Lena Fischer", permission_key: "deployment:production",
          project_id: "acme-web", reason: "Ship the hotfix tonight", status: "open", created_at: iso(40 * 60_000) },
        { id: "acr_demo2", requester_principal_id: "usr_theo", requester_name: "Theo Nakamura", permission_key: "audit:read",
          project_id: null, reason: null, status: "open", created_at: iso(5 * 3_600_000) },
      ],
    };
    // One person who has not proved their address yet, so the profile can show "Mark verified".
    const lena = s.people.find((p) => p.id === "usr_lena") as (Person & { email_verified?: boolean }) | undefined;
    if (lena && lena.email_verified === undefined) lena.email_verified = false;
    OVERLAYS.set(s, o);
  }
  return o;
}

function members(s: S, gid: string): Group["members"] {
  return s.people.filter((x) => x.groups.some((g) => g.id === gid)).map((x) => ({ id: x.id, name: x.display_name, email: x.email, hue: x.hue }));
}

/** Every group as the API would list it now. */
export function groupsOf(s: S): Group[] {
  const o = overlay(s);
  return [...F.groups(s.t0, s.people), ...o.created.map((g) => ({ ...g, members: members(s, g.id) }))]
    .filter((g) => !o.deleted.has(g.id))
    .map((g) => ({ ...g, ...o.edited[g.id],
      bindings: [...g.bindings.filter((b) => !o.removed.has(b.id)), ...o.added.filter((b) => b.subject_id === g.id)] }));
}

/** A role bound to a group: every member inherits it. Null when the group or role is unknown. */
export function bindGroup(s: S, groupId: string, roleId: string, scope: ScopeLabel, by: string, now: number, expiresAt: string | null): Binding | null {
  const g = groupsOf(s).find((x) => x.id === groupId);
  const role = s.roles.find((r) => r.id === roleId || r.key === roleId);
  if (!g || !role) return null;
  const b: Binding = { id: `rb_g${Math.random().toString(36).slice(2, 8)}`, role_id: role.id, role_key: role.key, role_name: role.name, scope,
    subject_id: g.id, via_group: null, granted_by: by, granted_at: new Date(now).toISOString(), expires_at: expiresAt };
  overlay(s).added.push(b);
  for (const p of s.people) {
    if (p.groups.some((x) => x.id === g.id)) p.bindings.push({ ...b, id: `${b.id}_${p.id}`, subject_id: p.id, via_group: { id: g.id, name: g.name, source: g.source } });
  }
  return b;
}

/** A group's binding removed; true when the id was one. */
export function unbindGroup(s: S, bindingId: string): boolean {
  const g = groupsOf(s).find((x) => x.bindings.some((b) => b.id === bindingId));
  if (!g) return false;
  const o = overlay(s);
  o.removed.add(bindingId);
  o.added = o.added.filter((b) => b.id !== bindingId);
  for (const p of s.people) p.bindings = p.bindings.filter((b) => b.id !== bindingId && !b.id.startsWith(`${bindingId}_`));
  return true;
}

function body(req: { body: unknown }): Record<string, unknown> {
  return req.body && typeof req.body === "object" ? req.body as Record<string, unknown> : {};
}

export function registerGroupRoutes(on: StubOn): void {
  on("POST", "/v1/identity/groups", "group:manage", ({ s, req, who, now }) => {
    const b = body(req);
    const key = String(b.key ?? "");
    const name = String(b.name ?? "").trim();
    if (!/^[a-z0-9][a-z0-9_.-]{0,62}$/.test(key) || !name) return stubErr(422, "invalid_group");
    if (groupsOf(s).some((g) => g.name.toLowerCase() === name.toLowerCase() || g.id === `grp_${key}`)) return stubErr(409, "group_exists");
    const g: Group = { id: `grp_${key.replace(/[^a-z0-9]/g, "")}`, name, description: String(b.description ?? ""), source: "manual",
      provider: null, synced_at: null, members: [], member_count: 0, bindings: [] };
    overlay(s).created.push(g);
    stubAudit(s, now, { event_type: "group.created", what: name, headline: `Group ${name} was created` }, who);
    return stubOk(g, 201);
  });
  on("PATCH", "/v1/identity/groups/([^/]+)", "group:manage", ({ s, m, req, who, now }) => {
    const g = groupsOf(s).find((x) => x.id === m[1]);
    if (!g) return stubNotFound();
    if (g.source !== "manual") return stubErr(409, "idp_group_read_only");
    const b = body(req);
    const o = overlay(s);
    o.edited[g.id] = { ...o.edited[g.id], ...(b.name ? { name: String(b.name).trim() } : {}), ...(typeof b.description === "string" ? { description: b.description } : {}) };
    stubAudit(s, now, { event_type: "group.updated", what: g.name, headline: `Group ${g.name} was edited` }, who);
    return stubOk(groupsOf(s).find((x) => x.id === g.id));
  });
  on("DELETE", "/v1/identity/groups/([^/]+)", "group:manage", ({ s, m, who, now }) => {
    const g = groupsOf(s).find((x) => x.id === m[1]);
    if (!g) return stubNotFound();
    if (g.source !== "manual") return stubErr(409, "idp_group_read_only");
    overlay(s).deleted.add(g.id);
    for (const p of s.people) {
      p.groups = p.groups.filter((x) => x.id !== g.id);
      p.bindings = p.bindings.filter((b) => b.via_group?.id !== g.id);
    }
    stubAudit(s, now, { event_type: "group.deleted", what: g.name, headline: `Group ${g.name} was deleted` }, who);
    return { status: 204, body: null };
  });

  // A project's own binding routes (the core table has the org ones).
  on("POST", "/v1/identity/projects/([^/]+)/role-bindings", "role_binding:assign", ({ s, m, req, who, now }) => {
    const b = body(req);
    const scope = `project:${m[1]}` as ScopeLabel;
    const by = s.people.find((x) => x.id === who)?.display_name.split(" ")[0] ?? "BOSS";
    const expires = typeof b.expires_at === "string" ? b.expires_at : null;
    if (b.subject_type === "group") {
      const out = bindGroup(s, String(b.subject_id ?? ""), String(b.role_id ?? ""), scope, by, now, expires);
      return out ? stubOk(out, 201) : stubNotFound();
    }
    const p = s.people.find((x) => x.id === b.subject_id);
    const role = s.roles.find((r) => r.id === b.role_id || r.key === b.role_id);
    if (!p || !role) return stubNotFound();
    if (!role.bindable_scopes.includes("project")) return stubErr(422, "scope_not_bindable");
    const nb: Binding = { id: `rb_p${Math.random().toString(36).slice(2, 8)}`, role_id: role.id, role_key: role.key, role_name: role.name, scope,
      subject_id: p.id, via_group: null, granted_by: by, granted_at: new Date(now).toISOString(), expires_at: expires };
    p.bindings.push(nb);
    return stubOk(nb, 201);
  });
  on("DELETE", "/v1/identity/projects/([^/]+)/role-bindings/([^/]+)", "role_binding:assign", ({ s, m }) => {
    if (unbindGroup(s, m[2])) return { status: 204, body: null };
    for (const p of s.people) {
      if (p.bindings.some((b) => b.id === m[2])) { p.bindings = p.bindings.filter((b) => b.id !== m[2]); return { status: 204, body: null }; }
    }
    return stubNotFound();
  });

  // Access requests (A13), FX-B's approve/deny contract.
  on("GET", "/v1/identity/access-requests", "role_binding:read", ({ s, req }) => {
    const status = req.query.get("status") ?? "open";
    const rows = overlay(s).requests.filter((r) => r.status === status).map((r) => ({
      ...r,
      candidate_roles: s.roles.filter((x) => !x.system && (x.permissions as string[]).includes(r.permission_key) && (!r.project_id || x.bindable_scopes.includes("project")))
        .map((x) => ({ id: x.id, key: x.key, name: x.name })),
    }));
    return stubOk({ requests: rows });
  });
  on("POST", "/v1/identity/access-requests/([^/]+)/approve", "role_binding:assign", ({ s, m, req, who, now }) => {
    const r = overlay(s).requests.find((x) => x.id === m[1]);
    if (!r) return stubNotFound();
    if (r.status !== "open") return stubErr(409, "already_resolved", { status: r.status });
    if (r.requester_principal_id === who) return stubErr(403, "forbidden", { reason: "self_binding" });
    const b = body(req);
    const role = s.roles.find((x) => x.id === b.role_id);
    if (!role) return stubNotFound();
    if (!isPermission(r.permission_key) || !(role.permissions as string[]).includes(r.permission_key)) return stubErr(422, "role_lacks_permission");
    const p = s.people.find((x) => x.id === r.requester_principal_id);
    const scope = (r.project_id ? `project:${r.project_id}` : "org") as ScopeLabel;
    const binding: Binding = { id: `rb_r${Math.random().toString(36).slice(2, 8)}`, role_id: role.id, role_key: role.key, role_name: role.name, scope,
      subject_id: r.requester_principal_id, via_group: null, granted_by: s.people.find((x) => x.id === who)?.display_name.split(" ")[0] ?? null,
      granted_at: new Date(now).toISOString(), expires_at: typeof b.expires_at === "string" ? b.expires_at : null };
    p?.bindings.push(binding);
    r.status = "granted";
    stubAudit(s, now, { event_type: "access.request_granted", what: `${PERMISSION_META[r.permission_key].title} → ${r.requester_name}`, headline: "An access request was granted" }, who);
    return stubOk({ request: r, binding });
  });
  on("POST", "/v1/identity/access-requests/([^/]+)/deny", "role_binding:assign", ({ s, m, who, now }) => {
    const r = overlay(s).requests.find((x) => x.id === m[1]);
    if (!r) return stubNotFound();
    if (r.status !== "open") return stubErr(409, "already_resolved", { status: r.status });
    r.status = "declined";
    stubAudit(s, now, { event_type: "access.request_declined", what: r.requester_name, headline: "An access request was declined" }, who);
    return stubOk({ request: r });
  });

  // An admin vouches for a person's address.
  on("POST", "/v1/identity/users/([^/]+)/verify-email", "user:update", ({ s, m, who, now }) => {
    overlay(s);
    const p = s.people.find((x) => x.id === m[1]) as (Person & { email_verified?: boolean }) | undefined;
    if (!p) return stubNotFound();
    if (p.email_verified === false) {
      p.email_verified = true;
      stubAudit(s, now, { event_type: "user.email_verified", what: p.display_name, headline: `${p.display_name}'s email was verified` }, who);
    }
    return stubOk({ ...p, email_verified: true });
  });
}
