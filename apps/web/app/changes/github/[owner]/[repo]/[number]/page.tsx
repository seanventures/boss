import { redirect } from "next/navigation";

import { serverGet } from "../../../../../../lib/identity/server";

export const dynamic = "force-dynamic";

/**
 * A GITHUB PULL REQUEST OPENS IN BOSS (BOSSAPP-954). lib/repo-links.ts changeHref sends every
 * github.com PR link here; the repository's BOSS project (one the person can read) gets its change
 * page, with "Open on GitHub" one click away. A repository in none of their projects goes to
 * GitHub itself.
 */
export default async function GithubChange({ params, searchParams }: {
  params: Promise<{ owner: string; repo: string; number: string }>;
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const { owner, repo, number } = await params;
  const q = await searchParams;
  const o = decodeURIComponent(owner);
  const r = decodeURIComponent(repo);
  const n = decodeURIComponent(number);
  const found = await serverGet<{ projects: string[] }>(
    `/v1/github-changes/resolve?owner=${encodeURIComponent(o)}&repo=${encodeURIComponent(r)}`);
  const project = found.ok ? found.data.projects[0] : undefined;
  if (!project || !/^\d+$/.test(n)) redirect(`https://github.com/${encodeURIComponent(o)}/${encodeURIComponent(r)}/pull/${encodeURIComponent(n)}`);
  const keep = new URLSearchParams({ repo: `${o}/${r}` });
  const commit = typeof q.commit === "string" && /^[0-9a-f]{7,40}$/i.test(q.commit) ? q.commit : null;
  if (commit) redirect(`/changes/${encodeURIComponent(project)}/${n}/commits/${commit}?${keep.toString()}`);
  if (typeof q.tab === "string" && q.tab) keep.set("tab", q.tab);
  redirect(`/changes/${encodeURIComponent(project)}/${n}?${keep.toString()}`);
}
