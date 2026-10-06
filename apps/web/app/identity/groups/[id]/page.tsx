import { notFound } from "next/navigation";

import { pageAllowed } from "../../../../components/identity/route-shell";
import { loadDirectory } from "../../../../lib/identity/directory";
import { GroupDetail } from "./group";

export const dynamic = "force-dynamic";

type Props = { params: Promise<{ id: string }> };

/** Identity › Groups › a group: members, where they come from, and what they inherit. */
export default async function GroupPage({ params }: Props) {
  const { id } = await params;
  if (!(await pageAllowed(`/identity/groups/${id}`))) return null;
  const dir = await loadDirectory();
  const group = dir.groups.find((g) => g.id === id);
  if (!group) notFound();
  return <GroupDetail group={group} people={dir.ok ? dir.people : null} />;
}
