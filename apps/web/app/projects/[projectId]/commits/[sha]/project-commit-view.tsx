"use client";

/**
 * A PROJECT COMMIT (BOSSAPP-954): /projects/<project>/commits/<sha> — a commit of the default
 * branch (main/master) in the change page's tree-plus-diff layout. A merge commit shows what the
 * merge brought in (against its first parent) and links the change or PR that merged it.
 */
import React, { useEffect, useMemo, useState } from "react";
import { ArrowLeft, GitCommit, GitMerge } from "@phosphor-icons/react";

import { IdentityProvider } from "../../../../../components/identity/identity-provider";
import type { MePermissionsResponse, MeResponse } from "../../../../../lib/identity/contract";
import { refusalSentence } from "../../../../../lib/identity/api";
import { changedFiles, getHistoryCommit, parseFiles, projectCommitUrl, totals } from "../../../../../lib/change-review";
import { parseUnifiedDiff } from "../../../../../lib/diff-view";
import { parseChangeSet } from "../../../../../lib/repo-links";
import { DiffBrowser, LayoutToggle, useLayoutPref } from "../../../../changes/[projectId]/[number]/review-panes";
import { FullScreenButton, ReviewLayoutProvider } from "../../../../changes/[projectId]/[number]/review-dock";
import "../../../../changes/[projectId]/[number]/change-set.css";

type View = { sha: string; parents: string[]; merge: boolean; author: string; date: string | null; message: string;
              repository: string; change: { source: string; number: number; href: string } | null; files: ReturnType<typeof parseFiles>["files"]; diff: string };

export function ProjectCommitView({ projectId, sha, repo, me = null, org = null, project = null }: {
  projectId: string; sha: string; repo: string | null;
  me?: MeResponse | null; org?: MePermissionsResponse | null; project?: MePermissionsResponse | null;
}) {
  return (
    <IdentityProvider me={me} org={org} project={project} projectId={projectId}>
      <ReviewLayoutProvider page="project-commit"><Inner projectId={projectId} sha={sha} repo={repo} /></ReviewLayoutProvider>
    </IdentityProvider>
  );
}

function Inner({ projectId, sha, repo }: { projectId: string; sha: string; repo: string | null }) {
  const [view, setView] = useState<View | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [layout, setLayout] = useLayoutPref();
  useEffect(() => {
    let alive = true;
    getHistoryCommit(projectId, sha, repo).then((r) => {
      if (!alive) return;
      const f = parseFiles({ ...r, base: r.parent, head: r.sha });
      const c = r.change && typeof r.change === "object" ? r.change as View["change"] : null;
      setView({ sha: String(r.sha ?? sha), parents: Array.isArray(r.parents) ? r.parents.map(String) : [], merge: r.merge === true,
                author: String(r.author ?? ""), date: typeof r.date === "string" ? r.date : null, message: String(r.message ?? ""),
                repository: String(r.repository ?? ""), change: c, files: f.files, diff: f.diff });
    }).catch((e: unknown) => { if (alive) setErr(refusalSentence(e)); });
    return () => { alive = false; };
  }, [projectId, sha, repo]);
  const files = useMemo(() => (view ? changedFiles(view.files, parseUnifiedDiff(view.diff)) : []), [view]);
  const cs = useMemo(() => parseChangeSet({ repository: view?.repository ?? "" }), [view?.repository]);
  const at = useMemo(() => ({ projectId, number: 0 }), [projectId]);
  const sum = totals(files);
  const back = `/?project_id=${encodeURIComponent(projectId)}&area=project&tab=knowledge`;
  const head = view ? (
    <div className="gr-cmp gr-commit-h" data-testid="gr-project-commit-head">
      {view.merge ? <GitMerge size={14} aria-hidden /> : <GitCommit size={14} aria-hidden />}
      <b className="cs-ell" data-testid="gr-commit-message">{view.message.split("\n")[0]}</b>
      <code className="cs-chip" data-testid="gr-commit-sha">{view.sha.slice(0, 7)}</code>
      <span className="cs-faint cs-small">{view.author}</span>
      <span><span className="cs-add">+{sum.additions}</span> <span className="cs-del">−{sum.deletions}</span>
        <span className="cs-faint cs-small"> · {sum.files} file{sum.files === 1 ? "" : "s"}</span></span>
      {view.merge ? <span className="lr-tag" data-tone="green">what the merge brought in</span> : null}
      {view.change ? <a className="lr-btn" data-kind="ghost" href={view.change.href} data-testid="gr-merged-change">
        {view.change.source === "github" ? "PR" : "Change"} #{view.change.number} ›</a> : null}
      {view.parents[0] ? <a className="cs-small" href={projectCommitUrl(projectId, view.parents[0], repo)} data-testid="gr-parent">parent <code>{view.parents[0].slice(0, 7)}</code></a> : null}
      <span className="cs-spacer" />
      <LayoutToggle layout={layout} setLayout={setLayout} /><FullScreenButton />
    </div>) : null;
  return (
    <main className="cs gr" data-testid="project-commit">
      <nav className="cs-crumb" aria-label="Breadcrumb">
        <a href={back} data-testid="project-commit-back"><ArrowLeft size={13} aria-hidden /> History</a>
        <span className="cs-faint" aria-hidden>·</span><span aria-current="page">{view?.repository || "Commit"} · {sha.slice(0, 7)}</span>
      </nav>
      {err ? <p className="cs-error" role="alert">{err}</p> : null}
      {!view && !err ? <div className="cs-loading" role="status" aria-label="Loading the commit"><i /><i /></div> : null}
      {view ? <DiffBrowser cs={cs} at={at} files={files} diff={view.diff} inline={new Map()} layout={layout} toolbar={head}
                           allowAll={false} onViewFile={() => undefined} /> : null}
    </main>
  );
}
