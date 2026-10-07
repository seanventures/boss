/** One read of who is signed in per server request, shared by the layout and its page. */
import { cache } from "react";

import { ME_PERMISSIONS_PATH, type MePermissionsResponse } from "./contract";
import { loadIdentity, serverGet, type IdentityBoot } from "./server";

/** Who, and what they hold at the org: read once per request, whichever project is asked after. */
const loadOrgIdentity = cache(() => loadIdentity(null));

export const loadIdentityCached = cache(async (projectId?: string | null): Promise<IdentityBoot> => {
  if (!projectId) return loadOrgIdentity();
  const [base, project] = await Promise.all([
    loadOrgIdentity(),
    serverGet<MePermissionsResponse>(`${ME_PERMISSIONS_PATH}?project_id=${encodeURIComponent(projectId)}`),
  ]);
  return { ...base, project: project.ok ? project.data : null };
});
