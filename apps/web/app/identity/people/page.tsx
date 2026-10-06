import { pageAllowed } from "../../../components/identity/route-shell";
import { serverGet } from "../../../lib/identity/server";
import type { AccessRole, Person } from "../../../lib/identity/types";
import { PeopleList } from "./people-list";

export const dynamic = "force-dynamic";

/** Identity › People (IDENTITY.md §9.2): everyone who signs in, filterable, with invite. */
export default async function PeoplePage() {
  if (!(await pageAllowed("/identity/people"))) return null;
  const [people, roles] = await Promise.all([
    serverGet<{ users: Person[] }>("/v1/identity/users"),
    serverGet<{ roles: AccessRole[] }>("/v1/identity/access-roles"),
  ]);
  return <PeopleList people={people.ok ? people.data.users : []} roles={roles.ok ? roles.data.roles : []} />;
}
