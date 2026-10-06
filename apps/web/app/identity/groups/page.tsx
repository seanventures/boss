import { pageAllowed } from "../../../components/identity/route-shell";
import { serverGet } from "../../../lib/identity/server";
import type { Group } from "../../../lib/identity/types";
import { GroupsList } from "./groups-list";

export const dynamic = "force-dynamic";

/** Identity › Groups (IDENTITY.md §9.3): sets of people who share access roles. */
export default async function GroupsPage() {
  if (!(await pageAllowed("/identity/groups"))) return null;
  const groups = await serverGet<{ groups: Group[] }>("/v1/identity/groups");
  return <GroupsList groups={groups.ok ? groups.data.groups : []} />;
}
