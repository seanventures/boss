/**
 * Extra stub routes for the access-role and service-account screens. Called by stub/server.ts
 * after its core table, with its `on(method, pattern, needs, handler)`. Helpers are imported
 * from ./server — safe, because they are only called when a request is handled.
 *
 * SEAMS (S2, S1): GET …/access-roles/{id}/versions/{n} (a version's permission set, for revert),
 * PATCH /v1/identity/service-accounts/{id} (rename, describe) and GET …/service-accounts/{id}/scopes
 * (what a new key may hold) are not in IDENTITY.md §10.
 */
import { ROLE_PERMISSIONS, isPermission, type Permission } from "../permissions.gen";
import type { StubOn } from "./server";
import { stubAudit, stubErr, stubNotFound, stubOk, stubState } from "./server";

export function registerRoleRoutes(on: StubOn): void {
  on("GET", "/v1/identity/access-roles/([^/]+)/versions/(\\d+)", "access_role:read", ({ m }) => {
    const s = stubState();
    const role = s.roles.find((r) => r.id === m[1] || r.key === m[1]);
    const v = Number(m[2]);
    if (!role || v < 1 || v > role.version) return stubNotFound();
    if (role.builtin || v === role.version) return stubOk({ version: v, permissions: role.permissions });
    // The fixture keeps no history: approximate each earlier version from its summary line.
    let perms: Permission[] = role.permissions.filter((p) => p !== "github:merge");
    if (v === 1) perms = [...ROLE_PERMISSIONS.operator];
    return stubOk({ version: v, permissions: perms });
  });

  on("PATCH", "/v1/identity/service-accounts/([^/]+)", "service_account:manage", ({ m, req }) => {
    const sa = stubState().sas.find((x) => x.id === m[1]);
    if (!sa) return stubNotFound();
    const b = (req.body ?? {}) as { name?: unknown; description?: unknown };
    if (b.name !== undefined && (typeof b.name !== "string" || !b.name.trim())) return stubErr(422, "invalid_fields");
    if (typeof b.name === "string") sa.name = b.name.trim();
    if (typeof b.description === "string") sa.description = b.description;
    // Owners (FX-C W4): ids of active people; an account always keeps at least one.
    const owners = (req.body as { owner_principal_ids?: unknown } | null)?.owner_principal_ids;
    if (owners !== undefined) {
      const people = Array.isArray(owners) ? owners.map((id) => stubState().people.find((p) => p.id === id && p.status === "active")) : [];
      if (!people.length || people.some((p) => !p)) return stubErr(422, "invalid_owners");
      sa.owners = people.map((p) => ({ id: p!.id, name: p!.display_name.split(" ")[0], hue: p!.hue }));
      sa.orphaned = false;
    }
    return stubOk(sa);
  });

  on("GET", "/v1/identity/service-accounts/([^/]+)/scopes", "service_account:read", ({ m }) => {
    const s = stubState();
    const sa = s.sas.find((x) => x.id === m[1]);
    if (!sa) return stubNotFound();
    const set = new Set<Permission>();
    for (const b of sa.bindings) {
      const builtin = (ROLE_PERMISSIONS as Record<string, readonly Permission[]>)[b.role_key];
      for (const p of builtin ?? s.roles.find((r) => r.key === b.role_key)?.permissions ?? []) if (isPermission(p)) set.add(p);
    }
    return stubOk({ permissions: [...set].sort() });
  });

  // Delete a custom role: refused while anyone holds it (409 role_in_use, as roles.py), or moved
  // first with POST …/reassign (FX-B contract §6).
  const holdersOf = (key: string) => stubState().people.flatMap((p) => p.bindings.filter((b) => b.role_key === key).map((b) => ({ p, b })));
  on("DELETE", "/v1/identity/access-roles/([^/]+)", "access_role:manage", ({ m, who, now }) => {
    const s = stubState();
    const role = s.roles.find((r) => r.id === m[1]);
    if (!role) return stubNotFound();
    if (role.builtin) return stubErr(409, "builtin_read_only");
    const held = new Set(holdersOf(role.key).map((x) => x.b.id)).size;
    if (held) return stubErr(409, "role_in_use", { bindings: held, message: "Reassign its bindings first." });
    s.roles = s.roles.filter((r) => r.id !== role.id);
    stubAudit(s, now, { event_type: "access_role.deleted", what: role.name, headline: "An access role was deleted" }, who);
    return { status: 204, body: null };
  });
  on("POST", "/v1/identity/access-roles/([^/]+)/reassign", "access_role:manage", ({ m, req, who, now }) => {
    const s = stubState();
    const role = s.roles.find((r) => r.id === m[1]);
    const b = (req.body ?? {}) as { to_role_id?: unknown; delete?: unknown };
    const to = s.roles.find((r) => r.id === b.to_role_id);
    if (!role || !to) return stubNotFound();
    if (role.id === to.id) return stubErr(422, "same_role");
    const moving = holdersOf(role.key);
    for (const { b: bind } of moving) { bind.role_id = to.id; bind.role_key = to.key; bind.role_name = to.name; }
    if (b.delete) s.roles = s.roles.filter((r) => r.id !== role.id);
    stubAudit(s, now, { event_type: "access_role.deleted", what: `${role.name} → ${to.name}`, headline: "An access role's holders were moved" }, who);
    return stubOk({ moved: new Set(moving.map((x) => x.b.id)).size, bindings: moving.map((x) => x.b), deleted: Boolean(b.delete) });
  });
}
