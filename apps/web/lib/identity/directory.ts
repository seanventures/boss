/**
 * THE DIRECTORY, READ ON THE SERVER (people, roles, groups and every binding), joined into the
 * console's view by lib/identity/adapt-people.ts. The real API keeps them on separate routes;
 * a page needs them together (a person's chips, their groups, a group's inherited roles).
 * Server-only: pages import it, client components never do.
 */
import { adaptServiceAccount, assembleDirectory, nameIndex, type Directory } from "./adapt-people";
import { serverGet } from "./server";
import type { ApiKey, ServiceAccount } from "./types";
import type { DirectoryFailures } from "./types-people";

type List<K extends string> = Record<K, unknown[]>;

/**
 * Read and join. A part the viewer may not read (403) or the API does not serve is left out
 * — bindings then fall back to each person's `access_roles` — rather than failing the page.
 */
export async function loadDirectory(opts: { groupMembers?: boolean } = {}): Promise<Directory & {
  ok: boolean; invited: number | null; readable: { roles: boolean; groups: boolean };
  /** Each part that did not load, with why: a 403 is "you can't see this", anything else an error. */
  failures: DirectoryFailures;
}> {
  const [users, roles, groups, bindings, invitations] = await Promise.all([
    serverGet<List<"users">>("/v1/identity/users"),
    serverGet<List<"roles">>("/v1/identity/access-roles"),
    serverGet<List<"groups">>("/v1/identity/groups"),
    serverGet<List<"bindings">>("/v1/identity/role-bindings"),
    serverGet<List<"invitations">>("/v1/identity/invitations"),
  ]);
  const groupRows = groups.ok ? groups.data.groups ?? [] : [];
  // Membership lives on each group's own page (/groups/{id}); a list row carries only a count.
  const needDetails = opts.groupMembers !== false && groupRows.some((g) => !Array.isArray((g as { members?: unknown }).members));
  const details = needDetails
    ? (await Promise.all(groupRows.map((g) => serverGet<Record<string, unknown>>(`/v1/identity/groups/${encodeURIComponent(String((g as { id?: unknown }).id ?? ""))}`))))
      .flatMap((r) => (r.ok ? [r.data] : []))
    : [];
  const dir = assembleDirectory({
    users: users.ok ? users.data.users : [],
    roles: roles.ok ? roles.data.roles : [],
    groups: groupRows,
    groupDetails: details,
    bindings: bindings.ok ? bindings.data.bindings : null,
  });
  const failures: DirectoryFailures = {};
  for (const [k, r] of [["users", users], ["roles", roles], ["groups", groups], ["bindings", bindings]] as const) {
    if (!r.ok) failures[k] = { status: r.status, code: r.code, permission: r.permission };
  }
  return { ...dir, ok: users.ok, invited: invitations.ok ? (invitations.data.invitations ?? []).length : null,
    readable: { roles: roles.ok, groups: groups.ok }, failures };
}

/**
 * Service accounts with their keys (GET /service-accounts/{id}/keys) and bindings, owners named
 * from the directory. A key list the viewer cannot read is left empty.
 */
export async function loadServiceAccounts(dir: Pick<Directory, "people" | "roles" | "bindings">): Promise<ServiceAccount[] | null> {
  const list = await serverGet<List<"service_accounts">>("/v1/identity/service-accounts");
  if (!list.ok) return null;
  const rows = list.data.service_accounts ?? [];
  const names = nameIndex(dir.people);
  const keys = await Promise.all(rows.map((raw) => {
    const r = raw as { id?: unknown; keys?: unknown };
    return Array.isArray(r.keys) ? Promise.resolve(null)
      : serverGet<{ keys: ApiKey[] }>(`/v1/identity/service-accounts/${encodeURIComponent(String(r.id ?? ""))}/keys`);
  }));
  return rows.map((raw, i) => {
    const id = String((raw as { id?: unknown }).id ?? "");
    const k = keys[i];
    return adaptServiceAccount(raw, {
      keys: k && k.ok ? k.data.keys : undefined,
      bindings: dir.bindings.filter((b) => b.subject_id === id),
      names, roles: dir.roles,
    });
  });
}
