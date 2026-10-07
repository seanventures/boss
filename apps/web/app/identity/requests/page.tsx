import { pageAllowed } from "../../../components/identity/route-shell";
import { checkWith } from "../../../lib/identity/check";
import { snapshotFrom } from "../../../lib/identity/contract";
import { heldAnywhere } from "../../../lib/identity/held-anywhere";
import { requestQueues } from "../../../lib/identity/request-queues";
import { serverGet } from "../../../lib/identity/server";
import { loadIdentityCached } from "../../../lib/identity/server-cache";
import type { AccessRole } from "../../../lib/identity/types";
import type { AccessRequestRow } from "../../../lib/identity/types-people";
import { RequestsScreen, type QueueView } from "./requests-screen";

export const dynamic = "force-dynamic";

/**
 * Identity › Access requests: every request waiting on the viewer. Opens for anyone holding
 * role_binding:assign at the org or at any project (pages.ts `anyScope`), so a project Admin, who
 * cannot open People, answers their project's requests here.
 */
export default async function RequestsPage() {
  if (!(await pageAllowed("/identity/requests"))) return null;
  const boot = await loadIdentityCached(null);
  const org = boot.org ? snapshotFrom(boot.org) : null;
  const held = await heldAnywhere("role_binding:assign");
  const queues = requestQueues(org, held);
  const mayReadRoles = !org || checkWith(org, null, null, "access_role:read").allowed;
  const [roles, ...answers] = await Promise.all([
    mayReadRoles ? serverGet<{ roles: AccessRole[] }>("/v1/identity/access-roles") : Promise.resolve(null),
    ...queues.map((q) => serverGet<{ requests: AccessRequestRow[] }>(q.path)),
  ]);
  const views: QueueView[] = queues.map((q, i) => {
    const a = answers[i];
    return {
      project: q.projectId,
      requests: a.ok ? a.data.requests ?? [] : null,
      failure: a.ok ? null : { status: a.status, code: a.code, permission: a.permission },
    };
  });
  return <RequestsScreen queues={views} roles={roles?.ok ? roles.data.roles ?? [] : []} />;
}
