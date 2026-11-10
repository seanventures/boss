import { loadIdentity } from "../../../../lib/identity/server";
import { ChangeSetView } from "./change-set-view";

export const dynamic = "force-dynamic";
export const metadata = { title: "Change · BOSS" };

/**
 * THE CHANGE PAGE (BOSSAPP-919/948/954): a local project's change set, or — with ?repo=owner/name —
 * a GitHub pull request of the project, reviewed the same way. Every console link to a
 * `http://boss-local/...` PR URL lands here, and a github.com PR of a BOSS project too, through
 * /changes/github/<owner>/<repo>/<n> (lib/repo-links.ts changeHref). ?tab= and ?commit= keep the
 * tab and the commit open, so links and Back work.
 */
export default async function ChangeSetPage({ params, searchParams }: {
  params: Promise<{ projectId: string; number: string }>;
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const { projectId, number } = await params;
  const q = await searchParams;
  const one = (v: string | string[] | undefined) => (Array.isArray(v) ? v[0] : v) ?? null;
  const pid = decodeURIComponent(projectId);
  const repo = one(q.repo);
  // Who is signed in and what they hold here, read on the server like the console's own page:
  // the browser's read gate judges the change set's read with it from the first paint (a page
  // that waited for the browser's own read showed "Locked" on arrival).
  const boot = await loadIdentity(pid);
  return <ChangeSetView projectId={pid} number={decodeURIComponent(number)} repo={repo && /^[^/\s]+\/[^/\s]+$/.test(repo) ? repo : null}
                        tab={one(q.tab)} commit={one(q.commit)} me={boot.me} org={boot.org} project={boot.project} />;
}
