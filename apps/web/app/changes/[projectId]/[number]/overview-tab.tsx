"use client";

/**
 * THE OVERVIEW TAB (BOSSAPP-995). The operator: "introduce the overview tab, where explaining
 * about the PR description (introduce good formatting) and other active and resolved comments
 * for people to directly work on it, we should also provide link to jump to specific code piece."
 *
 * One reading surface for a change, local or GitHub alike:
 *   - the change's description, rendered whole as Markdown (headings, lists, code, tables) —
 *     never the Conversation tab's three-line clamp;
 *   - the review comments to WORK ON, grouped Active (open · addressed · disputed) first and
 *     Resolved behind an expander, each thread with its code and its replies;
 *   - every file:line reference — a thread's anchor, and the review panel's inline notes — is a
 *     link that lands on that exact row of the diff (review-panes.lineAnchor).
 *
 * The Conversation tab keeps the timeline (who said what, when); this tab is the work queue.
 */
import React from "react";
import { ChatCircleDots, CheckCircle, FileText } from "@phosphor-icons/react";

import { Markdown } from "../../../markdown";
import type { ChangeSet, ChangeSetNote } from "../../../../lib/repo-links";
import { totals, type ChangeAt, type FileStat } from "../../../../lib/change-review";
import type { Thread } from "../../../../lib/local-review-data";
import { ThreadView, Who } from "./local-review";

/** A `path:line` reference as a link into the diff: the one way any comment names code. */
export function FileLineLink({ path, line, onJumpLoc, testid }: {
  path: string; line?: number | null; onJumpLoc: (path: string, line?: number | null) => void; testid?: string;
}) {
  return (
    <button type="button" className="cs-chip gr-loc" data-testid={testid ?? "gr-loc"}
            title={`Open the diff at ${path}${line ? `:${line}` : ""}`}
            onClick={() => onJumpLoc(path, line)}>
      <code>{path}{line ? `:${line}` : ""}</code>
    </button>
  );
}

const ACTIVE = new Set(["open", "addressed", "disputed"]);

export function OverviewTab({ cs, at, files, onChanged, onJump, onJumpLoc, onFiles, commitHref }: {
  cs: ChangeSet; at: ChangeAt; files: FileStat[]; onChanged: () => void;
  onJump: (t: Thread) => void; onJumpLoc: (path: string, line?: number | null) => void;
  onFiles: () => void; commitHref?: (sha: string) => string;
}) {
  const active = cs.comments.filter((t) => ACTIVE.has(t.state));
  const resolved = cs.comments.filter((t) => !ACTIVE.has(t.state));
  // The review panel's notes that carry inline comments: each file:line a link into the diff.
  const notes = cs.review.filter((n) => n.body || n.comments.length);
  const sum = totals(files);
  return (
    <section className="gr-overview" aria-label="Overview" data-testid="gr-overview">
      <p className="gr-what" data-testid="gr-ov-what">
        <button type="button" className="lr-link" onClick={onFiles}>{sum.files} file{sum.files === 1 ? "" : "s"} changed</button>
        <span className="cs-add">+{sum.additions}</span> <span className="cs-del">−{sum.deletions}</span>
        <span className="cs-faint"> · {cs.commits.length} commit{cs.commits.length === 1 ? "" : "s"}</span></p>

      <section className="cs-card" aria-label="Description" data-testid="gr-ov-description">
        <div className="cs-card-h"><FileText size={15} aria-hidden /><b>Description</b></div>
        {cs.body.trim()
          ? <div className="cs-body-md gr-ov-body" data-user-text><Markdown text={cs.body} /></div>
          : <p className="cs-empty">No description yet: the build writes one when it proposes the change.</p>}
      </section>

      <section className="cs-card" aria-label="Active comments" data-testid="gr-ov-active">
        <div className="cs-card-h"><ChatCircleDots size={15} aria-hidden /><b>Active</b><span className="cs-faint">{active.length}</span></div>
        {active.length ? (
          <div className="lr-threads">
            {active.map((t) => <ThreadView key={t.id} thread={t} at={at} withCode onOpen={onJump} commitHref={commitHref}
                                           onChanged={onChanged} compact />)}
          </div>
        ) : <p className="cs-empty">Nothing waits on anyone: every comment is resolved.</p>}
        {notes.map((n) => <PanelNote key={String(n.id)} note={n} onJumpLoc={onJumpLoc} />)}
      </section>

      {resolved.length ? (
        <details className="cs-card gr-ov-resolved" data-testid="gr-ov-resolved">
          <summary className="cs-card-h"><CheckCircle size={15} aria-hidden /><b>Resolved</b><span className="cs-faint">{resolved.length}</span></summary>
          <div className="lr-threads">
            {resolved.map((t) => <ThreadView key={t.id} thread={t} at={at} withCode onOpen={onJump} commitHref={commitHref}
                                             onChanged={onChanged} compact />)}
          </div>
        </details>
      ) : null}
    </section>
  );
}

/** One of the review panel's notes: its words, then each inline comment with its file:line as a
 *  link into the diff (the note's words sit under that row there, review-panes `inline`). */
function PanelNote({ note, onJumpLoc }: { note: ChangeSetNote; onJumpLoc: (path: string, line?: number | null) => void }) {
  return (
    <article className="cs-note" data-testid="gr-ov-note">
      <div className="cs-note-h"><Who name={note.author} />
        {note.event || note.kind ? <span className="cs-pill" data-tone="grey">{(note.event || note.kind).replace(/_/g, " ").toLowerCase()}</span> : null}</div>
      {note.body ? <div className="cs-note-b cs-md" data-user-text><Markdown text={note.body} /></div> : null}
      {note.comments.length ? (
        <ul className="cs-note-c">{note.comments.map((c, i) => (
          <li key={i}>
            {c.path ? <FileLineLink path={c.path} line={c.line} onJumpLoc={onJumpLoc} testid="gr-ov-note-loc" /> : null}
            <div className="cs-note-cb cs-md" data-user-text><Markdown text={c.body} /></div>
          </li>
        ))}</ul>
      ) : null}
    </article>
  );
}
