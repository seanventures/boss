import { pageAllowed } from "../../../components/identity/route-shell";
import { PROJECT_ID } from "../../../lib/client";
import { accessRequestQueue } from "../../../lib/identity/access-request-queue";
import { snapshotFrom } from "../../../lib/identity/contract";
import { loadDirectory } from "../../../lib/identity/directory";
import { serverGet } from "../../../lib/identity/server";
import { loadIdentityCached } from "../../../lib/identity/server-cache";
import type { AccessRequestRow } from "../../../lib/identity/types-people";
import { PeopleList } from "./people-list";

export const dynamic = "force-dynamic";

/** Identity › People (IDENTITY.md §9.2): everyone who signs in, filterable, with invite. */
export default async function PeoplePage() {
  if (!(await pageAllowed("/identity/people"))) return null;
  // The org's requests for an org admin, the project's queue for a project Admin, none otherwise.
  const boot = await loadIdentityCached(PROJECT_ID);
  const queue = accessRequestQueue(boot.org ? snapshotFrom(boot.org) : null,
    boot.project ? snapshotFrom(boot.project) : null, PROJECT_ID);
  const [dir, requests] = await Promise.all([
    loadDirectory(),
    queue ? serverGet<{ requests: AccessRequestRow[] }>(queue.path) : Promise.resolve(null),
  ]);
  return (
    <PeopleList
      people={dir.people}
      roles={dir.roles}
      groups={dir.groups}
      invited={dir.invited}
      failure={dir.failures.users ?? null}
      requests={requests?.ok ? requests.data.requests ?? [] : null}
      requestsProject={queue?.projectId ?? null}
      requestsFailure={requests && !requests.ok && requests.status !== 403 ? { status: requests.status, code: requests.code, permission: requests.permission } : null}
    />
  );
}
