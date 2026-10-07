import { notFound } from "next/navigation";

import { pageAllowed } from "../../../../components/identity/route-shell";
import { adaptRole, adaptRoleVersions, adaptRoles, nameIndex, roleIndex } from "../../../../lib/identity/adapt-people";
import { serverGet } from "../../../../lib/identity/server";
import { ErrorState } from "../../../../components/identity/states";
import { RoleEditor } from "./role-editor";

export const dynamic = "force-dynamic";

/** Identity › Access roles › one role (mockup c): the matrix, the diff and the impact preview. */
export default async function AccessRolePage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  if (!(await pageAllowed(`/identity/roles/${id}`))) return null;
  const enc = encodeURIComponent(id);
  const [role, versions, roles, users] = await Promise.all([
    serverGet<unknown>(`/v1/identity/access-roles/${enc}`),
    serverGet<{ versions: unknown[] }>(`/v1/identity/access-roles/${enc}/versions`),
    serverGet<{ roles: unknown[] }>("/v1/identity/access-roles"),
    serverGet<{ users: { id: string; display_name?: string; email?: string }[] }>("/v1/identity/users"),
  ]);
  if (!role.ok && role.status === 404) notFound();
  if (!role.ok) return <ErrorState what="this access role" failure={role} />;
  const all = roles.ok ? adaptRoles(roles.data.roles) : [];
  const index = roleIndex(all);
  // Names for the impact preview's people (the API answers principal ids).
  const names = Object.fromEntries(nameIndex(users.ok ? users.data.users : []));
  return <RoleEditor role={adaptRole(role.data, index)} versions={versions.ok ? adaptRoleVersions(versions.data.versions) : []} names={names} roles={all} />;
}
