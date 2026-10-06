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
import { stubErr, stubNotFound, stubOk, stubState } from "./server";

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
}
