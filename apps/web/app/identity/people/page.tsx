import { pageAllowed } from "../../../components/identity/route-shell";
import { loadDirectory } from "../../../lib/identity/directory";
import { serverGet } from "../../../lib/identity/server";
import type { AccessRequestRow } from "../../../lib/identity/types-people";
import { PeopleList } from "./people-list";

export const dynamic = "force-dynamic";

/** Identity › People (IDENTITY.md §9.2): everyone who signs in, filterable, with invite. */
export default async function PeoplePage() {
  if (!(await pageAllowed("/identity/people"))) return null;
  const [dir, requests] = await Promise.all([
    loadDirectory(),
    serverGet<{ requests: AccessRequestRow[] }>("/v1/identity/access-requests?status=open"),
  ]);
  return (
    <PeopleList
      people={dir.people}
      roles={dir.roles}
      groups={dir.groups}
      invited={dir.invited}
      failure={dir.failures.users ?? null}
      requests={requests.ok ? requests.data.requests ?? [] : null}
      requestsFailure={!requests.ok && requests.status !== 403 ? { status: requests.status, code: requests.code, permission: requests.permission } : null}
    />
  );
}
