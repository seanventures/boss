"use client";

/**
 * THE PROJECT'S HISTORY (BOSSAPP-954, grown for BOSSAPP-993 — the operator: "introduce the git
 * logs in the ui"), on Repo & knowledge: the default branch's commits (main/master), newest
 * first, merged work included. One list for a LOCAL repository and a GitHub one — the API's
 * /history answers from whichever host holds the code. Each row: subject, author, when, the
 * change or PR that produced it, the short sha; the subject opens the commit's own page
 * (/projects/<project>/commits/<sha>) with its files and diff.
 */
import React, { useEffect, useState } from "react";
import { GitCommit, GitMerge } from "@phosphor-icons/react";

import { refusalSentence } from "../../lib/identity/api";
import { whenLocal } from "../../lib/local-time";
import { getHistory, projectCommitUrl, type History } from "../../lib/change-review";

export function RepoHistory({ projectId, limit = 12 }: { projectId: string; limit?: number }) {
  const [h, setH] = useState<History | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [more, setMore] = useState(false);
  useEffect(() => {
    let alive = true;
    getHistory(projectId).then((v) => { if (alive) setH(v); }).catch((e: unknown) => { if (alive) setErr(refusalSentence(e)); });
    return () => { alive = false; };
  }, [projectId]);
  const items = (h?.items ?? []).slice(0, more ? 200 : limit);
  return (
    <section className="px-card" aria-label="History" data-testid="px-history">
      <div className="px-card-h"><GitCommit size={15} aria-hidden /><b>History</b>
        {h ? <span className="px-faint"> · {h.branch} · {h.repository}</span> : null}</div>
      {err ? <p className="px-faint">{err}</p> : null}
      {!h && !err ? <p className="px-faint">Reading the history…</p> : null}
      {h ? (
        <ol className="px-history" style={{ listStyle: "none", margin: 0, padding: 0 }}>
          {items.map((c) => (
            <li key={c.sha} data-merge={c.merge || undefined} data-testid="px-history-commit"
                style={{ display: "flex", gap: 8, alignItems: "baseline", padding: "4px 0", minWidth: 0, fontSize: "var(--fs-meta)" }}>
              {c.merge ? <GitMerge size={13} aria-hidden /> : <GitCommit size={13} aria-hidden />}
              <a href={projectCommitUrl(projectId, c.sha, h.source === "github" ? h.repository : null)} data-testid="px-history-link"
                 style={{ minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", flex: 1 }}
                 title={`${c.message} — files and diff`}>{c.message}</a>
              {c.author ? <span className="px-faint px-ell" style={{ maxWidth: "9rem" }} data-testid="px-history-author">{c.author}</span> : null}
              {c.date ? <span className="px-faint px-hide-phone" data-testid="px-history-when">{whenLocal(c.date)}</span> : null}
              {c.change ? <a href={c.change.href} data-testid="px-history-change"
                             title={c.change.source === "github" ? "The pull request that produced it" : "The change set that produced it"}>
                {c.change.source === "github" ? "PR" : "Change"} #{c.change.number}</a> : null}
              <code style={{ fontSize: "var(--fs-tag)" }}>{c.sha.slice(0, 7)}</code>
            </li>))}
          {!h.items.length ? <li className="px-faint">No commits yet.</li> : null}
        </ol>) : null}
      {h && h.items.length > limit ? <button type="button" className="px-chip" onClick={() => setMore((v) => !v)} data-testid="px-history-more">
        {more ? "Fewer" : `All ${h.items.length}`}</button> : null}
    </section>
  );
}
