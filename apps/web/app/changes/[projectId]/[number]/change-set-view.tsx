"use client";

/**
 * A LOCAL CHANGE SET, THE PULL REQUEST OF A LOCAL PROJECT (BOSSAPP-919). Read from
 * GET /v1/projects/{project_id}/local-changes/{number}:
 *
 *   header   title #n · state · branch → base · Local repo · checks · the published PR, if any
 *   side     the review decision and Merge (a person; a step-up when the decision asks for one),
 *            then BOSS's sandbox checks
 *   main     files (+/− bars, each expandable, the first few open) with the diff coloured per
 *            language and the review panel's inline comments under their line; commits; review
 *
 * Merge is POST …/local-changes/{n}/merge: a person's approval of the review decision, after which
 * BOSS runs the governed merge; the page re-reads until the change set says merged.
 *
 * LOCAL REVIEW (BOSSAPP-948, ./local-review.tsx): where the code lives and a working copy; the Code
 * tab (the whole file at this change or its base); comments on any line of the diff or the file,
 * in one store; Request changes, which starts the fix round with those comments as required
 * inputs (Agent actions); Merge held while a person's comment is open.
 */
import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ArrowLeft, ArrowRight, CaretDown, CaretRight, ChatCircleDots, ChatCircleText, CheckCircle, CircleNotch, FileCode,
  GitBranch, GitCommit, GitMerge, HardDrives, PencilSimpleLine, ShieldCheck, XCircle,
} from "@phosphor-icons/react";

import { BoxPop } from "../../../../components/box-pop";
import { ConsentHost } from "../../../../components/identity/consent-sheet";
import { CanButton, IdentityProvider, useGuardedCall, useIdentity } from "../../../../components/identity/identity-provider";
import type { MePermissionsResponse, MeResponse } from "../../../../lib/identity/contract";
import { idGet, idPost, refusalSentence } from "../../../../lib/identity/api";
import { changeSetPath, parseChangeSet, type ChangeSet, type ChangeSetNote } from "../../../../lib/repo-links";
import { commentsByLine, highlight, langOf, parseUnifiedDiff, type DiffFile } from "../../../../lib/diff-view";
import { Markdown } from "../../../markdown";
import { requestChanges, runReview, type Thread } from "../../../../lib/local-review";
import {
  AgentActions, CodeIcon, CodeTab, ReviewRail, ThreadView, WhereItLives, Who, useJumpTo, useLineReview, type LineReview,
} from "./local-review";
import "./change-set.css";

/** Files open on arrival: the first few, so the diff is there without a click. */
const OPEN_FIRST = 3;

export function ChangeSetView({ projectId, number, me = null, org = null, project = null }: {
  projectId: string; number: string;
  /** Read on the server (page.tsx): who is here and what they hold, at the org and this project. */
  me?: MeResponse | null; org?: MePermissionsResponse | null; project?: MePermissionsResponse | null;
}) {
  // The page sits outside the console shell: its own identity provider, so a merge that needs
  // "Confirm it's you" can ask for it here.
  return (
    <IdentityProvider me={me} org={org} project={project} projectId={projectId}>
      <ChangeSetPage projectId={projectId} number={number} />
      <ConsentHost />
    </IdentityProvider>
  );
}

type InlineComment = { path?: string; line?: number; body: string; author: string };

function ChangeSetPage({ projectId, number }: { projectId: string; number: string }) {
  const [cs, setCs] = useState<ChangeSet | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Who is here (and the write token Merge sends), read once on arrival.
  const { refresh } = useIdentity();
  useEffect(() => { void refresh(); }, [refresh]);
  const read = useCallback(async () => {
    const v = await idGet<unknown>(changeSetPath(projectId, number));
    const parsed = parseChangeSet(v);
    setCs(parsed);
    return parsed;
  }, [projectId, number]);
  useEffect(() => {
    let alive = true;
    read().catch((e: unknown) => { if (alive) setError(refusalSentence(e)); });
    return () => { alive = false; };
  }, [read]);

  const projectHref = `/?project_id=${encodeURIComponent(projectId)}&area=project&tab=work`;
  const projectHome = `/?project_id=${encodeURIComponent(projectId)}&area=project`;
  const files = useMemo(() => parseUnifiedDiff(cs?.diff), [cs?.diff]);
  const inline = useMemo(() => commentsByLine<InlineComment>((cs?.review ?? []).flatMap((n) =>
    n.comments.map((c) => ({ ...c, author: n.author })))), [cs?.review]);
  const commentCount = (cs?.review ?? []).reduce((a, n) => a + n.comments.length + (n.body ? 1 : 0), 0)
    + (cs?.comments.length ?? 0);
  // Files (the diff) or Code (the whole file): the Code tab opens at a file named in the diff.
  const [view, setView] = useState<"files" | "code">("files");
  const [codePath, setCodePath] = useState<string | null>(null);
  const reread = useCallback(() => { void read().catch(() => null); }, [read]);
  const jump = useJumpTo();
  const lineReview = useLineReview({
    projectId, number, threads: cs?.comments ?? [], onChanged: reread, open: cs?.state === "open",
    commitFor: (side) => (side === "new" ? cs?.where?.head_sha || cs?.head_sha || null : null),
  });
  const openCode = (path: string | null) => { setCodePath(path); setView("code"); };
  const onJump = (t: Thread) => {
    if (t.path && !files.some((f) => f.path === t.path) && !t.outdated) { openCode(t.path); jump(t.id); return; }
    setView("files"); jump(t.id);
  };

  return (
    <main className="cs" data-testid="change-set">
      <nav className="cs-crumb" aria-label="Breadcrumb">
        <a href={projectHref} data-testid="change-set-back"><ArrowLeft size={13} aria-hidden /> Back</a>
        <span className="cs-faint" aria-hidden>·</span>
        <a href={projectHome}>Project</a><span className="cs-faint" aria-hidden>›</span>
        <a href={projectHref}>Work</a><span className="cs-faint" aria-hidden>›</span>
        <span aria-current="page">Change #{number}</span>
      </nav>
      {error ? <p className="cs-error" role="alert" data-testid="change-set-error">{error}</p> : null}
      {!cs && !error ? (
        <div className="cs-loading" role="status" aria-label={`Loading change #${number}`}>
          <i /><i /><i />
        </div>
      ) : null}
      {cs ? (
        <>
          <Header cs={cs} />
          <WhereItLives cs={cs} projectId={projectId} />
          <nav className="cs-tabs" aria-label="Sections">
            <a href="#cs-files" aria-current={view === "files" ? "true" : undefined} data-testid="change-set-tab-files"
               onClick={() => setView("files")}><FileCode size={13} aria-hidden />Files <b>{cs.files.length || files.length}</b></a>
            <a href="#lr-code" aria-current={view === "code" ? "true" : undefined} data-testid="change-set-tab-code"
               onClick={(e) => { e.preventDefault(); openCode(codePath); }}><CodeIcon size={13} aria-hidden />Code</a>
            <a href="#cs-commits" onClick={() => setView("files")}><GitCommit size={13} aria-hidden />Commits <b>{cs.commits.length}</b></a>
            <a href="#cs-review" onClick={() => setView("files")}><ChatCircleText size={13} aria-hidden />Review <b>{commentCount}</b></a>
            <a href="#cs-checks"><ShieldCheck size={13} aria-hidden />Checks</a>
          </nav>
          <div className="cs-grid">
            <div className="cs-main">
              <AgentActions cs={cs} />
              {view === "code" ? (
                <div id="lr-code"><CodeTab cs={cs} projectId={projectId} number={number} startPath={codePath} onChanged={reread} /></div>
              ) : (
                <>
                  {cs.body ? <Body text={cs.body} /> : null}
                  <Files cs={cs} files={files} inline={inline} review={lineReview} onViewFile={openCode} />
                  <Commits cs={cs} />
                  <Review notes={cs.review} threads={cs.comments} projectId={projectId} number={number} onChanged={reread} />
                </>
              )}
            </div>
            <aside className="cs-side">
              <Decide cs={cs} projectId={projectId} number={number} reread={read} />
              <ReviewRail cs={cs} onJump={onJump} />
              <Checks cs={cs} />
            </aside>
          </div>
        </>
      ) : null}
    </main>
  );
}

// ── header ──────────────────────────────────────────────────────────────────────────────────

function checksWords(cs: ChangeSet): { tone: "green" | "red" | "amber" | "grey"; label: string } {
  const c = cs.checks;
  if (!c) return { tone: "grey", label: "No checks" };
  const tests = c.tests_passed;
  if (tests === "false") return { tone: "red", label: "Tests failed" };
  if (tests === "true") return { tone: "green", label: "Tests passed" };
  const s = (c.run_status ?? "").toLowerCase();
  if (/fail|error|cancel/.test(s)) return { tone: "red", label: `Build ${s.replace(/_/g, " ")}` };
  if (/complete|succeed|done|merged/.test(s)) return { tone: "green", label: "Build done" };
  return { tone: "amber", label: s ? `Build ${s.replace(/_/g, " ")}` : "Checks —" };
}

function Header({ cs }: { cs: ChangeSet }) {
  const ck = checksWords(cs);
  return (
    <header className="cs-head">
      <h1 data-testid="change-set-title">{cs.title || `Change #${cs.number}`} <span className="cs-faint">#{cs.number}</span></h1>
      <div className="cs-meta">
        <span className="cs-pill" data-state={cs.state} data-testid="change-set-state">
          {cs.state === "merged" ? <GitMerge size={12} weight="bold" aria-hidden /> : cs.state === "closed" ? <XCircle size={12} aria-hidden /> : <GitBranch size={12} weight="bold" aria-hidden />}
          {cs.state === "published" ? "On GitHub" : cs.state.charAt(0).toUpperCase() + cs.state.slice(1)}{cs.draft ? " · draft" : ""}
        </span>
        <span className="cs-branch" data-testid="change-set-branch" title={`${cs.branch} into ${cs.base_branch}`}>
          <code className="cs-chip">{cs.branch}</code><ArrowRight size={12} aria-hidden className="cs-faint" /><code className="cs-chip">{cs.base_branch}</code>
        </span>
        <span className="cs-repo" title="A repository BOSS keeps on this machine">
          <HardDrives size={12} aria-hidden /><span className="cs-mono cs-ell">{cs.repository.split("/").pop()}</span>
          <span className="cs-tag">Local repo</span>
        </span>
        <span className="cs-pill" data-tone={ck.tone} title={cs.checks ? `In ${cs.checks.where}` : "No build recorded"}>
          {ck.tone === "green" ? <CheckCircle size={12} weight="fill" aria-hidden /> : ck.tone === "red" ? <XCircle size={12} weight="fill" aria-hidden /> : <ShieldCheck size={12} aria-hidden />}{ck.label}
        </span>
        {cs.comment_summary.human_open ? (
          <span className="cs-pill" data-tone="amber" title="Open line comments from people" data-testid="change-set-open-comments">
            <ChatCircleDots size={12} aria-hidden />{cs.comment_summary.human_open} open comment{cs.comment_summary.human_open === 1 ? "" : "s"}</span>) : null}
        {cs.labels.includes("pushed") ? <span className="cs-tag" title="A person pushed this branch to BOSS">pushed</span> : null}
        {cs.pending_push ? <span className="cs-pill" data-tone="amber">Waiting for GitHub</span> : null}
        {cs.github_pr_url ? <a className="cs-link" href={cs.github_pr_url} target="_blank" rel="noreferrer">Published PR ↗</a> : null}
      </div>
    </header>
  );
}

// ── files and the diff ──────────────────────────────────────────────────────────────────────

function Files({ cs, files, inline, review, onViewFile }: { cs: ChangeSet; files: DiffFile[]; inline: Map<string, InlineComment[]>;
  review?: LineReview; onViewFile?: (path: string) => void }) {
  // The API's file list is the truth for counts; the diff supplies the lines.
  const rows = cs.files.length
    ? cs.files.map((f) => ({ ...f, diff: files.find((d) => d.path === f.path) ?? null }))
    : files.map((d) => ({ path: d.path, status: d.status, additions: d.additions, deletions: d.deletions, diff: d }));
  const [open, setOpen] = useState<Set<string>>(() => new Set(rows.slice(0, OPEN_FIRST).map((r) => r.path)));
  const max = Math.max(1, ...rows.map((r) => r.additions + r.deletions));
  const adds = rows.reduce((a, r) => a + r.additions, 0);
  const dels = rows.reduce((a, r) => a + r.deletions, 0);
  const toggle = (p: string) => setOpen((s) => { const n = new Set(s); if (n.has(p)) n.delete(p); else n.add(p); return n; });
  return (
    <section className="cs-card" id="cs-files" data-testid="change-set-files" aria-label="Files">
      <div className="cs-card-h"><FileCode size={15} aria-hidden /><b>Files</b><span className="cs-faint">{rows.length}</span>
        <span className="cs-spacer" /><span className="cs-add">+{adds}</span><span className="cs-del">−{dels}</span></div>
      {!rows.length ? <p className="cs-empty">{cs.diff_error ? `The diff could not be read: ${cs.diff_error}` : "No file changes."}</p> : null}
      <div data-testid="change-set-diff">
        {rows.map((r, i) => {
          const isOpen = open.has(r.path);
          return (
            <div className="cs-file" key={r.path} data-testid={`change-set-file-${i}`} data-open={isOpen || undefined}>
              <button type="button" className="cs-file-h" aria-expanded={isOpen} onClick={() => toggle(r.path)}>
                {isOpen ? <CaretDown size={12} aria-hidden /> : <CaretRight size={12} aria-hidden />}
                <span className="cs-mono cs-ell cs-path" title={r.path}>{r.path}</span>
                <span className="cs-st" data-st={r.status}>{r.status}</span>
                <span className="cs-spacer" />
                <span className="cs-add">+{r.additions}</span><span className="cs-del">−{r.deletions}</span>
                <span className="cs-bar" aria-hidden>
                  <i data-k="add" style={{ width: `${(r.additions / max) * 100}%` }} />
                  <i data-k="del" style={{ width: `${(r.deletions / max) * 100}%` }} />
                </span>
              </button>
              {isOpen && onViewFile && r.status !== "removed" ? (
                <div className="lr-file-bar"><button type="button" className="lr-link" data-testid="change-set-view-file"
                  onClick={() => onViewFile(r.path)} title="Open the whole file in the Code tab"><CodeIcon size={12} aria-hidden />View file</button></div>) : null}
              {isOpen ? (r.diff && r.diff.hunks.length ? <DiffTable file={r.diff} inline={inline} review={review} />
                : <p className="cs-empty">{r.diff?.status === "binary" ? "A binary file." : "No lines to show."}</p>) : null}
            </div>
          );
        })}
      </div>
    </section>
  );
}

function DiffTable({ file, inline, review }: { file: DiffFile; inline: Map<string, InlineComment[]>; review?: LineReview }) {
  const lang = langOf(file.path);
  return (
    <div className="cs-diff" role="region" aria-label={`Diff of ${file.path}`} tabIndex={0}>
      <table>
        <tbody>
          {file.hunks.map((h, hi) => (
            <React.Fragment key={hi}>
              <tr className="cs-hunk"><td colSpan={3}><span className="cs-mono">{h.header.replace(/ @@.*$/, " @@")}</span>{h.section ? <span className="cs-faint"> {h.section}</span> : null}</td></tr>
              {h.lines.map((l, li) => {
                const notes = l.newNo != null ? inline.get(`${file.path}:${l.newNo}`) : undefined;
                // A comment sits on the new side's line, or on a deleted line's old one.
                const anchor = l.kind === "note" ? null : l.kind === "del"
                  ? (l.oldNo != null ? { path: file.path, line: l.oldNo, side: "old" as const } : null)
                  : (l.newNo != null ? { path: file.path, line: l.newNo, side: "new" as const } : null);
                const threaded = !!(review && anchor && review.has(anchor));
                return (
                  <React.Fragment key={li}>
                    <tr className="cs-ln" data-k={l.kind} data-c={threaded || undefined}>
                      <td className="cs-no lr-gut">{review && anchor ? review.gutter(anchor) : null}{l.oldNo ?? ""}</td>
                      <td className="cs-no">{l.newNo ?? ""}</td>
                      <td className="cs-code"><span className="cs-sign" aria-hidden>{l.kind === "add" ? "+" : l.kind === "del" ? "−" : " "}</span>
                        {l.kind === "note" ? <span className="cs-faint">{l.text}</span>
                          : highlight(l.text, lang).map((s, si) => s.t === "plain" ? <React.Fragment key={si}>{s.v}</React.Fragment> : <span key={si} className={`tk-${s.t}`}>{s.v}</span>)}
                      </td>
                    </tr>
                    {notes ? (
                      <tr className="cs-inline"><td colSpan={3}>
                        {notes.map((n, ni) => (
                          <InlineNote key={ni} author={n.author} body={n.body} />
                        ))}
                      </td></tr>
                    ) : null}
                    {threaded && review && anchor ? <tr className="cs-inline"><td colSpan={3}>{review.under(anchor)}</td></tr> : null}
                  </React.Fragment>
                );
              })}
            </React.Fragment>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ── commits and review ──────────────────────────────────────────────────────────────────────

function Commits({ cs }: { cs: ChangeSet }) {
  return (
    <section className="cs-card" id="cs-commits" data-testid="change-set-commits" aria-label="Commits">
      <div className="cs-card-h"><GitCommit size={15} aria-hidden /><b>Commits</b><span className="cs-faint">{cs.commits.length}</span></div>
      {cs.commits.length ? (
        <ol className="cs-commits">
          {cs.commits.map((c) => (
            <li key={c.sha}><i aria-hidden /><code className="cs-chip" title={c.sha}>{c.sha.slice(0, 7)}</code><span className="cs-ell" title={c.message}>{c.message.split("\n")[0]}</span></li>
          ))}
        </ol>
      ) : <p className="cs-empty">No commits read.</p>}
    </section>
  );
}

function eventTone(e: string): string {
  const v = e.toUpperCase();
  return v.includes("APPROVE") ? "green" : v.includes("CHANGES") || v.includes("REQUEST") ? "amber" : "grey";
}

/** A review note on a line of the diff: its author beside it (never on top of its words), the
 *  note as Markdown (BOSSAPP-946: "Boss" overlapped "MINOR** —" with the asterisks raw). */
export function InlineNote({ author, body }: { author: string; body: string }) {
  return (
    <div className="cs-inline-c" data-testid="change-set-inline-note"><Who name={author} size={18} />
      <div className="cs-inline-b cs-md" data-user-text><Markdown text={body} /></div></div>
  );
}

export function Review({ notes, threads = [], projectId = "", number = "", onChanged }: {
  notes: ChangeSetNote[]; threads?: Thread[]; projectId?: string; number?: string; onChanged?: () => void;
}) {
  // People's threads first (open before resolved), then the review panel's notes.
  const people = threads.filter((t) => t.author_kind === "human");
  const agents = threads.filter((t) => t.author_kind !== "human");
  return (
    <section className="cs-card" id="cs-review" data-testid="change-set-review" aria-label="Review">
      <div className="cs-card-h"><ChatCircleText size={15} aria-hidden /><b>Review</b><span className="cs-faint">{notes.length + threads.length}</span></div>
      {[...people, ...agents].length ? (
        <div className="lr-threads" data-testid="lr-review-threads">
          {[...people, ...agents].map((t) => <ThreadView key={t.id} thread={t} projectId={projectId} number={number}
                                                         onChanged={onChanged ?? (() => undefined)} compact={!t.outdated} />)}
        </div>) : null}
      {notes.length ? notes.map((n) => (
        <article key={String(n.id)} className="cs-note">
          <div className="cs-note-h"><Who name={n.author} />
            {n.event || n.kind ? <span className="cs-pill" data-tone={eventTone(n.event || n.kind)}>{(n.event || n.kind).replace(/_/g, " ").toLowerCase()}</span> : null}</div>
          {n.body ? <div className="cs-note-b cs-md" data-user-text><Markdown text={n.body} /></div> : null}
          {n.comments.length ? (
            <ul className="cs-note-c">{n.comments.map((c, i) => (
              <li key={i}>{c.path ? <code className="cs-chip">{c.path}{c.line ? `:${c.line}` : ""}</code> : null}<div className="cs-note-cb cs-md" data-user-text><Markdown text={c.body} /></div></li>
            ))}</ul>
          ) : null}
        </article>
      )) : !threads.length ? <p className="cs-empty">No review yet: the review panel comments here.</p> : null}
    </section>
  );
}

/** The change's description, as written (Markdown), folded past a few lines with Show all. */
function Body({ text }: { text: string }) {
  const [open, setOpen] = useState(false);
  const long = text.split("\n").length > 8 || text.length > 600;
  return (
    <section className="cs-card cs-body" data-testid="change-set-body" data-clamp={long && !open ? "" : undefined}>
      <div className="cs-body-md" data-user-text><Markdown text={text} /></div>
      {long ? <button type="button" className="cs-more" data-testid="change-set-body-more" onClick={() => setOpen((v) => !v)}>{open ? "Show less" : "Show all"}</button> : null}
    </section>
  );
}

// ── the decision, Merge and the checks ──────────────────────────────────────────────────────

export function Decide({ cs, projectId, number, reread }: { cs: ChangeSet; projectId: string; number: string; reread: () => Promise<ChangeSet> }) {
  const guarded = useGuardedCall();
  const [busy, setBusy] = useState<"merge" | "merging" | null>(null);
  const [said, setSaid] = useState<string | null>(null);
  const alive = useRef(true);
  useEffect(() => () => { alive.current = false; }, []);
  const merge = async () => {
    if (busy) return;
    setBusy("merge"); setSaid(null);
    try {
      await guarded(`Merge change #${number} into ${cs.base_branch}.`, () =>
        idPost(`${changeSetPath(projectId, number)}/merge`, {}));
      setBusy("merging");
      // BOSS runs the governed merge on its own (a worker job): the page reads back while the
      // decision is approved and the change still open (the effect below), however long it takes.
      await reread().catch(() => null);
    } catch (e) {
      if (!alive.current) return;
      setSaid(refusalSentence(e));
      setBusy(null);
    }
  };
  const d = cs.decision;
  // Approved and still open: the governed merge is on its way. Read back every few seconds while
  // the page is open; a merge that lands while nobody watches shows on the next visit.
  const merging = busy === "merging" || (cs.state === "open" && d?.status === "approved");
  useEffect(() => {
    if (!merging || cs.state !== "open") return undefined;
    const t = window.setInterval(() => { void reread().catch(() => null); }, 3000);
    return () => window.clearInterval(t);
  }, [merging, cs.state, reread]);
  useEffect(() => { if (cs.state !== "open") setBusy(null); }, [cs.state]);
  return (
    <section className="cs-card cs-decide" data-testid="change-set-decision" aria-label="Decision">
      <div className="cs-card-h"><GitMerge size={15} aria-hidden /><b>Decision</b><span className="cs-spacer" />
        {d ? <span className="cs-pill" data-tone={d.status === "approved" ? "green" : d.status === "pending" ? "amber" : "grey"}>{d.status}</span> : null}</div>
      {d?.question ? <p className="cs-q">{d.question}</p> : null}
      {cs.state === "merged" ? (
        <p className="cs-merged" data-testid="change-set-merged"><CheckCircle size={15} weight="fill" aria-hidden />
          <span>Merged{cs.merged_by ? <> by <b>{cs.merged_by}</b></> : null}{cs.merged_sha ? <> as <code className="cs-chip">{cs.merged_sha.slice(0, 7)}</code></> : null}</span></p>
      ) : merging ? (
        <p className="cs-merging" role="status" data-testid="change-set-merging"><CircleNotch size={14} className="cs-spin" aria-hidden /> Approved · BOSS is merging…</p>
      ) : cs.merge ? (
        <>
          <button type="button" className="cs-primary" data-testid="change-set-merge" disabled={!!busy || !!cs.merge.held} aria-busy={!!busy || undefined}
                  title={cs.merge.held?.reason || undefined} data-held={cs.merge.held?.state || undefined}
                  onClick={() => void merge()}>
            {busy ? <CircleNotch size={15} className="cs-spin" aria-hidden /> : <GitMerge size={15} weight="bold" aria-hidden />}
            {cs.merge.held ? cs.merge.held.label : `Merge into ${cs.base_branch}`}
          </button>
          <p className="cs-faint cs-small" data-testid={cs.merge.held ? "change-set-held" : undefined}>
            {cs.merge.held ? cs.merge.held.reason : "A person merges · BOSS runs the governed merge"}</p>
        </>
      ) : cs.state === "open" ? <p className="cs-faint cs-small">Merge opens once the review asks for a decision.</p> : null}
      {cs.state === "open" && !merging ? <RequestChanges cs={cs} projectId={projectId} number={number} reread={reread} /> : null}
      {cs.state === "open" && !d && cs.labels.includes("pushed") ? <RunReview cs={cs} projectId={projectId} number={number} reread={reread} /> : null}
      {said ? <p className="cs-error" role="alert">{said}</p> : null}
      {d || cs.merge ? <a className="cs-link cs-small" href={`/?area=decisions&decision=${encodeURIComponent(cs.merge?.decision_id ?? d!.id)}`}>Open the decision ›</a> : null}
    </section>
  );
}

/** REQUEST CHANGES (BOSSAPP-948): a person sends the review back; the fix round starts on this
 *  branch with their open line comments as required inputs, a note optional. */
function RequestChanges({ cs, projectId, number, reread }: { cs: ChangeSet; projectId: string; number: string; reread: () => Promise<ChangeSet> }) {
  const [open, setOpen] = useState(false);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [said, setSaid] = useState<string | null>(null);
  const [done, setDone] = useState<string | null>(null);
  const n = cs.comments.filter((t) => t.author_kind === "human" && t.state !== "resolved").length;
  const send = async () => {
    if (busy || (!note.trim() && !n)) return;
    setBusy(true); setSaid(null);
    try {
      await requestChanges(projectId, number, note.trim());
      setDone("The fix round started on this branch, your comments first.");
      setOpen(false); setNote("");
      await reread().catch(() => null);
    } catch (e) { setSaid(refusalSentence(e)); } finally { setBusy(false); }
  };
  return (
    <div className="lr-rc">
      <CanButton permission="decision:approve" projectId={projectId} className="lr-btn" data-kind="amber" aria-expanded={open}
                 data-testid="change-set-request-changes" tip="Start a fix round on this branch" onClick={() => setOpen((v) => !v)}>
        <PencilSimpleLine size={14} aria-hidden />Request changes{n ? ` · ${n} comment${n === 1 ? "" : "s"}` : ""}</CanButton>
      <p className="cs-faint cs-small">{done ?? "The builder starts a fix round on this branch, your comments first"}</p>
      {open ? (
        <BoxPop className="lr-composer" fit={false} onEscape={() => setOpen(false)} data-testid="lr-request-changes">
          <textarea className="lr-input" rows={3} value={note} onChange={(e) => setNote(e.target.value)} aria-label="What to change"
                    placeholder={n ? "Anything to add to your line comments (optional)" : "Say what to change"} data-testid="lr-rc-note"
                    onKeyDown={(e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); void send(); } }} />
          <div className="lr-composer-f"><span className="cs-faint cs-small">{n ? `${n} line comment${n === 1 ? "" : "s"} go${n === 1 ? "es" : ""} as required inputs` : "No line comments yet"}</span>
            <span className="cs-spacer" />
            <button type="button" className="lr-btn" data-kind="ghost" onClick={() => setOpen(false)}>Cancel</button>
            <button type="button" className="lr-btn" data-kind="primary" disabled={busy || (!note.trim() && !n)} onClick={() => void send()} data-testid="lr-rc-send">
              {busy ? <CircleNotch size={13} className="cs-spin" aria-hidden /> : null}Start the fix round</button></div>
          {said ? <p className="cs-error" role="alert">{said}</p> : null}
        </BoxPop>) : null}
      {said && !open ? <p className="cs-error" role="alert">{said}</p> : null}
    </div>
  );
}

/** A pushed change whose review did not start with the push (consent, budget): start it now. */
function RunReview({ cs, projectId, number, reread }: { cs: ChangeSet; projectId: string; number: string; reread: () => Promise<ChangeSet> }) {
  const [busy, setBusy] = useState(false);
  const [said, setSaid] = useState<string | null>(null);
  const running = !!cs.checks && /run|queue|lease|progress|pend/i.test(cs.checks.run_status ?? "");
  if (running) return <p className="cs-faint cs-small" data-testid="change-set-reviewing"><CircleNotch size={12} className="cs-spin" aria-hidden /> The review panel is checking this change</p>;
  return (
    <div className="lr-rc">
      <CanButton permission="code_task:create" projectId={projectId} className="lr-btn" data-kind="ghost" disabled={busy} data-testid="change-set-run-review"
                 tip="Run the sandbox checks and the review panel on this branch"
                 onClick={() => { setBusy(true); setSaid(null); void runReview(projectId, number).then(() => reread()).catch((e: unknown) => setSaid(refusalSentence(e))).finally(() => setBusy(false)); }}>
        <ShieldCheck size={14} aria-hidden />Run the review</CanButton>
      {said ? <p className="cs-error" role="alert">{said}</p> : null}
    </div>
  );
}

function Checks({ cs }: { cs: ChangeSet }) {
  const c = cs.checks;
  const tests = c?.tests_passed;
  const ck = checksWords(cs);
  return (
    <section className="cs-card" id="cs-checks" data-testid="change-set-checks" aria-label="Checks">
      <div className="cs-card-h"><ShieldCheck size={15} aria-hidden /><b>Checks</b><span className="cs-spacer" /><span className="cs-pill" data-tone={ck.tone}>{ck.label}</span></div>
      {c ? (
        <ul className="cs-checks">
          <li><span className="cs-dot" data-tone={/fail|error/i.test(c.run_status ?? "") ? "red" : c.run_status ? "green" : "grey"} aria-hidden />Build<span className="cs-spacer" /><span className="cs-faint">{(c.run_status ?? "—").replace(/_/g, " ")}</span></li>
          <li><span className="cs-dot" data-tone={tests === "true" ? "green" : tests === "false" ? "red" : "grey"} aria-hidden />Tests<span className="cs-spacer" /><span className="cs-faint">{tests === "true" ? "passed" : tests === "false" ? "failed" : tests ?? "—"}</span></li>
          <li className="cs-faint cs-small">In {c.where}</li>
        </ul>
      ) : <p className="cs-empty">No build recorded for this change.</p>}
    </section>
  );
}
