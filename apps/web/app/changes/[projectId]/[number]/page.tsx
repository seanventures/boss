import { ChangeSetView } from "./change-set-view";

export const dynamic = "force-dynamic";
export const metadata = { title: "Change set · BOSS" };

/**
 * A LOCAL CHANGE SET (BOSSAPP-919): a local project's "pull request", kept by BOSS on this machine.
 * Every console link to a `http://boss-local/...` PR URL lands here (lib/repo-links.ts changeHref):
 * its branch, checks, files and diff, commits and review notes, and — while it is open — the
 * review decision whose approval merges it.
 */
export default async function ChangeSetPage({ params }: { params: Promise<{ projectId: string; number: string }> }) {
  const { projectId, number } = await params;
  return <ChangeSetView projectId={decodeURIComponent(projectId)} number={decodeURIComponent(number)} />;
}
