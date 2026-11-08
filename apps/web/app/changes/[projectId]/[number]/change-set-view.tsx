"use client";

import React, { useEffect, useState } from "react";
import { idGet, refusalSentence } from "../../../../lib/identity/api";
import { changeSetPath, parseChangeSet, type ChangeSet } from "../../../../lib/repo-links";

/** The change set, read from GET /v1/projects/{project_id}/local-changes/{number}. */
export function ChangeSetView({ projectId, number }: { projectId: string; number: string }) {
  const [cs, setCs] = useState<ChangeSet | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let alive = true;
    idGet<unknown>(changeSetPath(projectId, number))
      .then((v) => { if (alive) setCs(parseChangeSet(v)); })
      .catch((e: unknown) => { if (alive) setError(refusalSentence(e)); });
    return () => { alive = false; };
  }, [projectId, number]);

  const projectHref = `/?project_id=${encodeURIComponent(projectId)}&area=project&tab=work`;
  return (
    <main className="cs-page" data-testid="change-set" style={{ maxWidth: "var(--page-max)", margin: "0 auto", padding: "1.5rem 1rem" }}>
      <p style={{ margin: 0 }}><a href={projectHref} data-testid="change-set-back">← Back to the project</a></p>
      {error ? <p role="alert" data-testid="change-set-error">{error}</p> : null}
      {!cs && !error ? <p role="status">Loading change set #{number}…</p> : null}
      {cs ? (
        <>
          <h1 style={{ marginBottom: 4 }} data-testid="change-set-title">
            {cs.title || `Change set #${cs.number}`} <span style={{ opacity: 0.6 }}>#{cs.number}</span>
          </h1>
          <p style={{ marginTop: 0 }}>
            <span className="cs-state" data-state={cs.state} data-testid="change-set-state">{cs.state}</span>
            {" · "}<code>{cs.branch}</code> → <code>{cs.base_branch}</code>
            {" · "}<span title="A repository BOSS keeps on this machine">{cs.repository}</span>
            {cs.github_pr_url ? <> · <a href={cs.github_pr_url} target="_blank" rel="noreferrer">Published PR ↗</a></> : null}
          </p>
          {cs.merge ? (
            <p data-testid="change-set-merge">
              <a className="cap-btn primary" href={`/?area=decisions&decision=${encodeURIComponent(cs.merge.decision_id)}`}>
                Review and merge
              </a>{" "}
              <span style={{ opacity: 0.7 }}>Merging is approving its review decision; a person decides.</span>
            </p>
          ) : cs.state === "merged" ? (
            <p data-testid="change-set-merged">Merged{cs.merged_by ? ` by ${cs.merged_by}` : ""}{cs.merged_sha ? <> as <code>{cs.merged_sha.slice(0, 12)}</code></> : null}.</p>
          ) : null}
          {cs.body ? <section><h2>Description</h2><p style={{ whiteSpace: "pre-wrap" }} data-user-text>{cs.body}</p></section> : null}
          <section data-testid="change-set-checks">
            <h2>Checks</h2>
            {cs.checks ? (
              <p>In {cs.checks.where}: build {cs.checks.run_status ?? "—"}
                {cs.checks.tests_passed != null ? ` · tests ${cs.checks.tests_passed === "true" ? "passed" : cs.checks.tests_passed === "false" ? "failed" : cs.checks.tests_passed}` : ""}.</p>
            ) : <p>No build recorded for this change set.</p>}
          </section>
          <section data-testid="change-set-files">
            <h2>Files ({cs.files.length})</h2>
            {cs.files.length ? (
              <ul>{cs.files.map((f) => (
                <li key={f.path}><code>{f.path}</code> <span style={{ opacity: 0.7 }}>{f.status} · +{f.additions} −{f.deletions}</span></li>
              ))}</ul>
            ) : <p>{cs.diff_error ? `The diff could not be read: ${cs.diff_error}` : "No file changes."}</p>}
            {cs.diff ? (
              <details>
                <summary>Diff</summary>
                <pre style={{ overflowX: "auto", fontSize: 12 }} data-testid="change-set-diff">{cs.diff}</pre>
              </details>
            ) : null}
          </section>
          <section data-testid="change-set-commits">
            <h2>Commits ({cs.commits.length})</h2>
            <ul>{cs.commits.map((c) => <li key={c.sha}><code>{c.sha.slice(0, 12)}</code> {c.message}</li>)}</ul>
          </section>
          <section data-testid="change-set-review">
            <h2>Review</h2>
            {cs.review.length ? cs.review.map((n) => (
              <div key={String(n.id)} className="cs-note" style={{ borderLeft: "3px solid #8884", paddingLeft: 8, margin: "8px 0" }}>
                <b>{n.author || "BOSS"}</b> <span style={{ opacity: 0.7 }}>{n.event || n.kind}</span>
                {n.body ? <p style={{ whiteSpace: "pre-wrap", margin: "4px 0" }} data-user-text>{n.body}</p> : null}
                {n.comments.length ? <ul>{n.comments.map((c, i) => (
                  <li key={i}>{c.path ? <code>{c.path}{c.line ? `:${c.line}` : ""}</code> : null} <span data-user-text>{c.body}</span></li>
                ))}</ul> : null}
              </div>
            )) : <p>No review notes yet.</p>}
          </section>
        </>
      ) : null}
    </main>
  );
}
