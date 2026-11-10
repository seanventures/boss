import { loadIdentity } from "../../../../../lib/identity/server";
import { ProjectCommitView } from "./project-commit-view";

export const dynamic = "force-dynamic";
export const metadata = { title: "Commit · BOSS" };

/** ONE COMMIT OF THE PROJECT'S DEFAULT BRANCH (BOSSAPP-954): its own changes in the tree and the
 *  diff; a merge shows what it brought in and links the change or PR it merged. ?repo= picks a
 *  repository other than the project's primary. */
export default async function ProjectCommitPage({ params, searchParams }: {
  params: Promise<{ projectId: string; sha: string }>;
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const { projectId, sha } = await params;
  const q = await searchParams;
  const repo = Array.isArray(q.repo) ? q.repo[0] : q.repo;
  const pid = decodeURIComponent(projectId);
  const boot = await loadIdentity(pid);
  return <ProjectCommitView projectId={pid} sha={decodeURIComponent(sha)} repo={repo ?? null} me={boot.me} org={boot.org} project={boot.project} />;
}
