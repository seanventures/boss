import { notFound } from "next/navigation";

import { pageAllowed } from "../../../../components/identity/route-shell";
import { serverGet } from "../../../../lib/identity/server";
import type { Group, Person } from "../../../../lib/identity/types";
import { GroupDetail } from "./group";

export const dynamic = "force-dynamic";

type Props = { params: Promise<{ id: string }> };

/** Identity › Groups › a group: members, where they come from, and what they inherit. */
export default async function GroupPage({ params }: Props) {
  const { id } = await params;
  if (!(await pageAllowed(`/identity/groups/${id}`))) return null;
  const [group, people] = await Promise.all([
    serverGet<Group>(`/v1/identity/groups/${encodeURIComponent(id)}`),
    serverGet<{ users: Person[] }>("/v1/identity/users"),
  ]);
  if (!group.ok && group.status === 404) notFound();
  if (!group.ok) return null;
  return <GroupDetail group={group.data} people={people.ok ? people.data.users : null} />;
}
