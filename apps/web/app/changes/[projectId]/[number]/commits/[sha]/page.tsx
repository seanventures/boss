import { loadIdentity } from "../../../../../../lib/identity/server";
import { ChangeSetView } from "../../change-set-view";

export const dynamic = "force-dynamic";
export const metadata = { title: "Commit · BOSS" };

/** ONE COMMIT OF A CHANGE, a real link (BOSSAPP-954): /changes/<project>/<n>/commits/<sha> opens
 *  the change page's Commits tab on that commit's own diff — read by sha, so it still opens after
 *  the change merged and its branch is gone. */
export default async function ChangeCommitPage({ params, searchParams }: {
  params: Promise<{ projectId: string; number: string; sha: string }>;
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const { projectId, number, sha } = await params;
  const q = await searchParams;
  const repo = Array.isArray(q.repo) ? q.repo[0] : q.repo;
  const pid = decodeURIComponent(projectId);
  const boot = await loadIdentity(pid);
  return <ChangeSetView projectId={pid} number={decodeURIComponent(number)} repo={repo && /^[^/\s]+\/[^/\s]+$/.test(repo) ? repo : null}
                        tab="commits" commit={decodeURIComponent(sha)} me={boot.me} org={boot.org} project={boot.project} />;
}
