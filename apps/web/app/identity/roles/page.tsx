import { pageAllowed } from "../../../components/identity/route-shell";
import { adaptRoles } from "../../../lib/identity/adapt-people";
import { serverGet } from "../../../lib/identity/server";
import { RoleList } from "./role-list";

export const dynamic = "force-dynamic";

/** Identity › Access roles: the five built-ins (read-only, clone to customise) and custom roles. */
export default async function AccessRolesPage() {
  if (!(await pageAllowed("/identity/roles"))) return null;
  const roles = await serverGet<{ roles: unknown[] }>("/v1/identity/access-roles");
  return <RoleList roles={roles.ok ? adaptRoles(roles.data.roles) : []} />;
}
