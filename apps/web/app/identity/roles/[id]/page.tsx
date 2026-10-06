import { notFound } from "next/navigation";

import { pageAllowed } from "../../../../components/identity/route-shell";
import { serverGet } from "../../../../lib/identity/server";
import type { AccessRole, RoleVersion } from "../../../../lib/identity/types";
import { RoleEditor } from "./role-editor";

export const dynamic = "force-dynamic";

/** Identity › Access roles › one role (mockup c): the matrix, the diff and the impact preview. */
export default async function AccessRolePage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  if (!(await pageAllowed(`/identity/roles/${id}`))) return null;
  const [role, versions] = await Promise.all([
    serverGet<AccessRole>(`/v1/identity/access-roles/${encodeURIComponent(id)}`),
    serverGet<{ versions: RoleVersion[] }>(`/v1/identity/access-roles/${encodeURIComponent(id)}/versions`),
  ]);
  if (!role.ok) notFound();
  return <RoleEditor role={role.data} versions={versions.ok ? versions.data.versions : []} />;
}
