import { pageAllowed } from "../../../components/identity/route-shell";
import { loadDirectory } from "../../../lib/identity/directory";
import { GroupsList } from "./groups-list";

export const dynamic = "force-dynamic";

/** Identity › Groups (IDENTITY.md §9.3): sets of people who share access roles. */
export default async function GroupsPage() {
  if (!(await pageAllowed("/identity/groups"))) return null;
  const dir = await loadDirectory();
  return <GroupsList groups={dir.groups} failure={dir.failures.groups ?? null} />;
}
