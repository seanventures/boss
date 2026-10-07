import { notFound } from "next/navigation";

import { pageAllowed } from "../../../../components/identity/route-shell";
import { ErrorState } from "../../../../components/identity/states";
import { loadDirectory } from "../../../../lib/identity/directory";
import { loadProjectChoices } from "../../people/projects";
import { GroupDetail } from "./group";

export const dynamic = "force-dynamic";

type Props = { params: Promise<{ id: string }> };

/** Identity › Groups › a group: members, where they come from, and what they inherit. */
export default async function GroupPage({ params }: Props) {
  const { id } = await params;
  if (!(await pageAllowed(`/identity/groups/${id}`))) return null;
  const [dir, projects] = await Promise.all([loadDirectory(), loadProjectChoices()]);
  if (dir.failures.groups) return <ErrorState what="this group" failure={dir.failures.groups} testId="group-error" />;
  const group = dir.groups.find((g) => g.id === id);
  if (!group) notFound();
  return <GroupDetail group={group} people={dir.ok ? dir.people : null} roles={dir.roles} projects={projects} />;
}
