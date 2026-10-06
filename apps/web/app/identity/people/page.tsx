import { pageAllowed } from "../../../components/identity/route-shell";
import { loadDirectory } from "../../../lib/identity/directory";
import { PeopleList } from "./people-list";

export const dynamic = "force-dynamic";

/** Identity › People (IDENTITY.md §9.2): everyone who signs in, filterable, with invite. */
export default async function PeoplePage() {
  if (!(await pageAllowed("/identity/people"))) return null;
  const dir = await loadDirectory();
  return <PeopleList people={dir.people} roles={dir.roles} invited={dir.invited} />;
}
