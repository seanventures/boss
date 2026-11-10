"use client";

/**
 * THE CHANGE PAGE'S PANES (BOSSAPP-954; wireframe docs/product/wireframes/agentic-factory/6-git-review.html):
 *
 *   FilesTab         tree left (filter, Changed/All files, comment dots, +/−) · the diff (split or
 *                    unified, base…head, Since my review, Viewed per person, inline comments)
 *   CommitsTab       one click shows a commit; tick two to compare them
 *   ConversationTab  the summary in 3 lines, then a timeline of short events, then every note
 *   ChecksTab        the decision, the fix round, BOSS's sandbox and GitHub's checks
 *   AgentPane        the PR agent: its task, the comments (open / fixed in <sha>), Fix N open
 *                    comments, and the chat (steered while it runs, kept for the fix run when idle)
 *   ClonePop         where it lives: the clone URL and branch (a local change: the bare repository,
 *                    the remote, the working copies and Get a working copy)
 *
 * Every call names its change (ChangeAt), so a local change set and a GitHub PR are one page.
 */
import React, { useCallback, useEffect, useMemo, useState } from "react";
import {
  ArrowRight, CaretDown, CaretRight, ChatCircleDots, CheckCircle, CircleNotch, ClockCounterClockwise, Code,
  Columns, FolderOpen, GitBranch, GitCommit, GitMerge, PaperPlaneRight, Robot, Rows, ShieldCheck, Terminal, Wrench, X, XCircle,
} from "@phosphor-icons/react";

import { BoxPop } from "../../../../components/box-pop";
import { CanButton } from "../../../../components/identity/identity-provider";
import { refusalSentence } from "../../../../lib/identity/api";
import {
  agentStatus, changedFiles, commentCounts, fixComments, getAgent, getCommit, neighbours, rangeLabel, rangeOfTicked,
  sendToAgent, setViewed, sinceMyReview, tick, totals, treeFiles, filterFiles, viewedCount, withoutWhatChanged,
  type AgentPane as AgentData, type ChangeAt, type Commit, type CommitView, type FileStat, type Range, type Viewed,
} from "../../../../lib/change-review";
import { getTree, type Thread, type Tree } from "../../../../lib/local-review";
import { highlight, langOf, parseUnifiedDiff, type DiffFile, type DiffHunk, type DiffLine } from "../../../../lib/diff-view";
import type { ChangeSet, ChangeSetNote } from "../../../../lib/repo-links";
import { Markdown } from "../../../markdown";
import { FileTree } from "./file-tree";
import { AgentActions, CommentCode, CopyButton, ThreadView, WhereItLives, Who, useLineReview, type LineReview } from "./local-review";
import { DockPane, FullScreenButton, useReviewLayout } from "./review-dock";

type InlineNote = { path?: string; line?: number; body: string; author: string };

function readPref(key: string, fallback: string): string {
  try { return window.localStorage.getItem(key) ?? fallback; } catch { return fallback; }
}
function writePref(key: string, value: string): void {
  try { window.localStorage.setItem(key, value); } catch { /* a convenience only */ }
}

// ── the diff: unified or split ───────────────────────────────────────────────────────────────

type Pair = { left: DiffLine | null; right: DiffLine | null };

/** A hunk's lines side by side: context on both sides, a run of deletions beside the additions
 *  that replaced them. */
export function splitRows(h: DiffHunk): Pair[] {
  const out: Pair[] = [];
  let dels: DiffLine[] = [];
  let adds: DiffLine[] = [];
  const flush = () => {
    for (let i = 0; i < Math.max(dels.length, adds.length); i++) out.push({ left: dels[i] ?? null, right: adds[i] ?? null });
    dels = []; adds = [];
  };
  for (const l of h.lines) {
    if (l.kind === "del") { if (adds.length) flush(); dels.push(l); }
    else if (l.kind === "add") adds.push(l);
    else { flush(); out.push(l.kind === "note" ? { left: l, right: null } : { left: l, right: l }); }
  }
  flush();
  return out;
}

function Code1({ text, lang }: { text: string; lang: string }) {
  return <>{highlight(text, lang).map((s, si) => s.t === "plain"
    ? <React.Fragment key={si}>{s.v}</React.Fragment> : <span key={si} className={`tk-${s.t}`}>{s.v}</span>)}</>;
}

function anchorOf(path: string, l: DiffLine | null, side: "old" | "new") {
  if (!l || l.kind === "note") return null;
  if (side === "old") return l.oldNo != null && l.kind === "del" ? { path, line: l.oldNo, side: "old" as const } : null;
  return l.newNo != null ? { path, line: l.newNo, side: "new" as const } : null;
}

export function DiffTable({ file, inline, review, layout = "unified" }: {
  file: DiffFile; inline: Map<string, InlineNote[]>; review?: LineReview; layout?: "unified" | "split";
}) {
  const lang = langOf(file.path);
  if (layout === "split") {
    return (
      <div className="cs-diff" role="region" aria-label={`Diff of ${file.path}, side by side`} tabIndex={0} data-layout="split">
        <table className="gr-split"><tbody>
          {file.hunks.map((h, hi) => (
            <React.Fragment key={hi}>
              <tr className="cs-hunk"><td colSpan={4}><span className="cs-mono">{h.header.replace(/ @@.*$/, " @@")}</span>{h.section ? <span className="cs-faint"> {h.section}</span> : null}</td></tr>
              {splitRows(h).map((p, pi) => {
                const left = p.left?.kind === "del" ? anchorOf(file.path, p.left, "old") : null;
                const right = anchorOf(file.path, p.right, "new");
                const notes = p.right?.newNo != null ? inline.get(`${file.path}:${p.right.newNo}`) : undefined;
                const under = review ? [left, right].filter((a): a is NonNullable<typeof a> => !!a && review.has(a)) : [];
                return (
                  <React.Fragment key={pi}>
                    <tr className="cs-ln gr-pair" data-c={under.length ? true : undefined}>
                      <td className="cs-no lr-gut" data-k={p.left?.kind}>{review && left ? review.gutter(left) : null}{p.left && p.left.kind !== "note" ? p.left.oldNo ?? "" : ""}</td>
                      <td className="cs-code" data-k={p.left?.kind}>{p.left ? (p.left.kind === "note" ? <span className="cs-faint">{p.left.text}</span> : <Code1 text={p.left.text} lang={lang} />) : null}</td>
                      <td className="cs-no lr-gut" data-k={p.right?.kind}>{review && right ? review.gutter(right) : null}{p.right?.newNo ?? ""}</td>
                      <td className="cs-code" data-k={p.right?.kind}>{p.right ? <Code1 text={p.right.text} lang={lang} /> : null}</td>
                    </tr>
                    {notes ? <tr className="cs-inline"><td colSpan={4}>{notes.map((n, ni) => <InlineNote key={ni} author={n.author} body={n.body} />)}</td></tr> : null}
                    {under.map((a) => <tr key={`${a.side}${a.line}`} className="cs-inline"><td colSpan={4}>{review!.under(a)}</td></tr>)}
                  </React.Fragment>
                );
              })}
            </React.Fragment>
          ))}
        </tbody></table>
      </div>
    );
  }
  return (
    <div className="cs-diff" role="region" aria-label={`Diff of ${file.path}`} tabIndex={0} data-layout="unified">
      <table>
        <tbody>
          {file.hunks.map((h, hi) => (
            <React.Fragment key={hi}>
              <tr className="cs-hunk"><td colSpan={3}><span className="cs-mono">{h.header.replace(/ @@.*$/, " @@")}</span>{h.section ? <span className="cs-faint"> {h.section}</span> : null}</td></tr>
              {h.lines.map((l, li) => {
                const notes = l.newNo != null ? inline.get(`${file.path}:${l.newNo}`) : undefined;
                // A comment sits on the new side's line, or on a deleted line's old one.
                const anchor = l.kind === "note" ? null : l.kind === "del" ? anchorOf(file.path, l, "old") : anchorOf(file.path, l, "new");
                const threaded = !!(review && anchor && review.has(anchor));
                return (
                  <React.Fragment key={li}>
                    <tr className="cs-ln" data-k={l.kind} data-c={threaded || undefined}>
                      <td className="cs-no lr-gut">{review && anchor ? review.gutter(anchor) : null}{l.oldNo ?? ""}</td>
                      <td className="cs-no">{l.newNo ?? ""}</td>
                      <td className="cs-code"><span className="cs-sign" aria-hidden>{l.kind === "add" ? "+" : l.kind === "del" ? "−" : " "}</span>
                        {l.kind === "note" ? <span className="cs-faint">{l.text}</span> : <Code1 text={l.text} lang={lang} />}
                      </td>
                    </tr>
                    {notes ? (
                      <tr className="cs-inline"><td colSpan={3}>
                        {notes.map((n, ni) => <InlineNote key={ni} author={n.author} body={n.body} />)}
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

/** A review note on a line of the diff: its author beside it (never on top of its words), the
 *  note as Markdown (BOSSAPP-946: "Boss" overlapped "MINOR** —" with the asterisks raw). */
export function InlineNote({ author, body }: { author: string; body: string }) {
  return (
    <div className="cs-inline-c" data-testid="change-set-inline-note"><Who name={author} size={18} />
      <div className="cs-inline-b cs-md" data-user-text><Markdown text={body} /></div></div>
  );
}

// ── the tree and the diff: Files changed, and one commit ─────────────────────────────────────

const OPEN_FIRST = 3;

export function useLayoutPref(): ["unified" | "split", (v: "unified" | "split") => void] {
  const [layout, setLayoutState] = useState<"unified" | "split">("unified");
  useEffect(() => { setLayoutState(readPref("boss.review.layout", "unified") === "split" ? "split" : "unified"); }, []);
  return [layout, (v) => { setLayoutState(v); writePref("boss.review.layout", v); }];
}

export function LayoutToggle({ layout, setLayout }: { layout: "unified" | "split"; setLayout: (v: "unified" | "split") => void }) {
  return (
    <span className="lr-seg" role="radiogroup" aria-label="Layout">
      <button type="button" role="radio" aria-checked={layout === "split"} onClick={() => setLayout("split")} data-testid="gr-split">
        <Columns size={13} aria-hidden />Split</button>
      <button type="button" role="radio" aria-checked={layout === "unified"} onClick={() => setLayout("unified")} data-testid="gr-unified">
        <Rows size={13} aria-hidden />Unified</button>
    </span>
  );
}

/** The tree on the left (filter, Changed/All files, comment dots, +/−) and the diff beside it.
 *  `files` is the ONE list every count reads (lib/change-review.ts changedFiles). */
export function DiffBrowser({ cs, at, files, diff, review, inline, layout, toolbar, onViewFile, viewed = null, head = null,
                              onViewed, allowAll = true, emptyWords, focus = null }: {
  cs: ChangeSet; at: ChangeAt; files: FileStat[]; diff: string; review?: LineReview; inline: Map<string, InlineNote[]>;
  layout: "unified" | "split"; toolbar: React.ReactNode; onViewFile: (path: string) => void;
  viewed?: Viewed | null; head?: string | null; onViewed?: (v: Viewed) => void; allowAll?: boolean; emptyWords?: string;
  /** Open this file and scroll to it (a comment clicked out of the diff). */
  focus?: { path: string; n: number } | null;
}) {
  const dock = useReviewLayout();
  const parsed = useMemo(() => parseUnifiedDiff(diff), [diff]);
  const [mode, setMode] = useState<"changed" | "all">("changed");
  const [filter, setFilter] = useState("");
  const [all, setAll] = useState<Tree | null>(null);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => {
    if (mode !== "all" || all) return undefined;
    let alive = true;
    getTree(at, "head").then((t) => { if (alive) setAll(t); }).catch(() => null);
    return () => { alive = false; };
  }, [mode, all, at]);
  const rows = filterFiles(treeFiles(files, all?.items ?? null, cs.comments, mode), filter);
  const shown = filterFiles(files.map((f) => ({ ...f, diff: parsed.find((d) => d.path === f.path) ?? null })), filter);
  const [open, setOpen] = useState<Set<string>>(() => new Set(files.slice(0, OPEN_FIRST).map((r) => r.path)));
  useEffect(() => { setOpen(new Set(files.slice(0, OPEN_FIRST).map((r) => r.path))); }, [diff]); // eslint-disable-line react-hooks/exhaustive-deps
  const [current, setCurrent] = useState<string | null>(null);
  useEffect(() => {
    if (!focus) return;
    setCurrent(focus.path);
    setOpen((s) => new Set(s).add(focus.path));
  }, [focus]);
  const isViewed = (p: string) => !!(viewed && head && viewed.paths[p] === head);
  const pick = (p: string) => {
    if (dock.layout.tree === "overlay") dock.dispatch({ type: "close", pane: "tree" });
    if (!files.some((f) => f.path === p)) { onViewFile(p); return; }
    setCurrent(p);
    setOpen((s) => new Set(s).add(p));
    window.setTimeout(() => document.getElementById(fileAnchor(p))?.scrollIntoView({ block: "start", behavior: "smooth" }), 40);
  };
  const toggleViewed = async (p: string) => {
    if (!onViewed) return;
    const was = isViewed(p);
    try {
      const v = await setViewed(at, p, head, !was);
      onViewed(v);
      setOpen((s) => { const n = new Set(s); if (!was) n.delete(p); else n.add(p); return n; });
    } catch (e) { setErr(refusalSentence(e)); }
  };
  const max = Math.max(1, ...files.map((r) => r.additions + r.deletions));
  return (
    <div className="gr-files" data-testid="change-set-files">
      <DockPane pane="tree" label="Files" icon={<FolderOpen size={15} aria-hidden />} testid="gr-dock-tree">
      <nav className="gr-tree" aria-label="Changed files">
        <label className="lr-goto"><input value={filter} onChange={(e) => setFilter(e.target.value)} placeholder="Filter files…"
                                          aria-label="Filter files" data-testid="gr-filter" /></label>
        {allowAll ? (
          <div className="lr-seg gr-mode" role="radiogroup" aria-label="Which files">
            <button type="button" role="radio" aria-checked={mode === "changed"} onClick={() => setMode("changed")} data-testid="gr-mode-changed">
              Changed {files.length}</button>
            <button type="button" role="radio" aria-checked={mode === "all"} onClick={() => setMode("all")} data-testid="gr-mode-all">All files</button>
          </div>) : null}
        <FileTree files={rows} current={current} onPick={pick} stats width={dock.layout.treeW} />
        {!rows.length ? <p className="cs-empty">{filter ? "No file matches." : "No files."}</p> : null}
      </nav>
      </DockPane>
      <div className="gr-main">
        {toolbar}
        {err ? <p className="cs-error" role="alert">{err}</p> : null}
        {!files.length ? <p className="cs-empty">{emptyWords ?? "No file changes."}</p> : null}
        <div data-testid="change-set-diff">
          {shown.map((r, i) => {
            const isOpen = open.has(r.path);
            const v = isViewed(r.path);
            return (
              <div className="cs-file" key={r.path} id={fileAnchor(r.path)} data-testid={`change-set-file-${i}`} data-open={isOpen || undefined}
                   data-viewed={v || undefined}>
                <div className="gr-fh">
                  <button type="button" className="cs-file-h" aria-expanded={isOpen}
                          onClick={() => setOpen((s) => { const n = new Set(s); if (n.has(r.path)) n.delete(r.path); else n.add(r.path); return n; })}>
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
                  {r.status !== "removed" && r.status !== "deleted" ? (
                    <button type="button" className="lr-link" data-testid="change-set-view-file" onClick={() => onViewFile(r.path)}
                            title="Open the whole file in the Code tab"><Code size={12} aria-hidden />View file</button>) : null}
                  {onViewed ? <label className="gr-viewed" title="Viewed: folds the file until it changes again">
                    <input type="checkbox" checked={v} onChange={() => void toggleViewed(r.path)} data-testid="gr-viewed" />Viewed</label> : null}
                </div>
                {isOpen ? (r.diff && r.diff.hunks.length
                  ? <DiffTable file={r.diff} inline={inline} review={review} layout={layout} />
                  : <p className="cs-empty">{r.diff?.status === "binary" ? "A binary file." : "No lines to show."}</p>) : null}
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}

/** FILES CHANGED: the change (or any base…head) in the tree and the diff. The page reads the
 *  range's diff once and passes `files` — the one source of every count. */
export function FilesTab({ cs, at, commits, files, diff, loading, error, review, inline, viewed, onViewed, onViewFile, range, setRange, focus = null }: {
  focus?: { path: string; n: number } | null;
  cs: ChangeSet; at: ChangeAt; commits: Commit[]; files: FileStat[]; diff: string; loading: boolean; error: string | null;
  review: LineReview; inline: Map<string, InlineNote[]>;
  viewed: Viewed | null; onViewed: (v: Viewed) => void; onViewFile: (path: string) => void;
  range: Range; setRange: (r: Range) => void;
}) {
  const [layout, setLayout] = useLayoutPref();
  const head = cs.where?.head_sha || cs.head_sha || commits[0]?.sha || null;
  const latest = !range.head || range.head === commits[0]?.sha || range.head === head;
  const since = sinceMyReview(viewed, head);
  const sum = totals(files);
  const toolbar = (
    <>
      <div className="gr-cmp" role="toolbar" aria-label="What the diff shows">
        <span className="cs-faint">Showing</span>
        <RangePicker cs={cs} commits={commits} range={range} setRange={setRange} />
        <button type="button" className="lr-btn" data-kind="ghost" disabled={!since} aria-pressed={!!since && range.base === since.base}
                title={since ? `What changed since you last reviewed (${since.base!.slice(0, 7)})` : "You have not reviewed an earlier head"}
                onClick={() => since && setRange(since)} data-testid="gr-since-review">
          <ClockCounterClockwise size={13} aria-hidden />Since my review</button>
        <span className="cs-spacer" />
        <LayoutToggle layout={layout} setLayout={setLayout} />
        <FullScreenButton />
        <span className="gr-viewed-n" data-testid="gr-viewed-count">{viewedCount(files, viewed, head)} of {sum.files} viewed</span>
        <span data-testid="gr-totals"><span className="cs-add">+{sum.additions}</span> <span className="cs-del">−{sum.deletions}</span></span>
      </div>
      {error ? <p className="cs-error" role="alert">{error}</p> : null}
      {loading ? <div className="cs-loading" role="status" aria-label="Loading the comparison"><i /><i /></div> : null}
    </>
  );
  return (
    <DiffBrowser cs={cs} at={at} files={loading ? [] : files} diff={loading ? "" : diff} review={latest ? review : undefined}
                 inline={inline} layout={layout} toolbar={toolbar} onViewFile={onViewFile} viewed={viewed} head={head}
                 onViewed={onViewed} focus={focus} emptyWords={cs.diff_error ? `The diff could not be read: ${cs.diff_error}` : undefined} />
  );
}

export const fileAnchor = (path: string) => `gr-f-${path.replace(/[^A-Za-z0-9_-]/g, "_")}`;

export function RangePicker({ cs, commits, range, setRange }: { cs: ChangeSet; commits: Commit[]; range: Range; setRange: (r: Range) => void }) {
  const [open, setOpen] = useState(false);
  return (
    <span className="gr-range">
      <button type="button" className="gr-sel" aria-expanded={open} onClick={() => setOpen((v) => !v)} data-testid="gr-range">
        <GitCommit size={13} aria-hidden />{rangeLabel(range, cs.base_branch, commits)}<CaretDown size={11} aria-hidden /></button>
      {open ? (
        <BoxPop className="gr-pop" role="dialog" aria-label="Compare" onEscape={() => setOpen(false)} data-testid="gr-range-pop">
          <label className="lr-row"><span>Base</span>
            <select className="gr-select" value={range.base ?? ""} data-testid="gr-base"
                    onChange={(e) => setRange({ ...range, base: e.target.value || null })}>
              <option value="">{cs.base_branch} (the whole change)</option>
              {commits.map((c) => <option key={c.sha} value={c.sha}>{c.sha.slice(0, 7)} · {c.message.slice(0, 40)}</option>)}
            </select></label>
          <label className="lr-row"><span>Head</span>
            <select className="gr-select" value={range.head ?? ""} data-testid="gr-head"
                    onChange={(e) => setRange({ ...range, head: e.target.value || null })}>
              <option value="">latest</option>
              {commits.map((c) => <option key={c.sha} value={c.sha}>{c.sha.slice(0, 7)} · {c.message.slice(0, 40)}</option>)}
            </select></label>
          <div className="lr-composer-f"><button type="button" className="lr-btn" data-kind="ghost" onClick={() => { setRange({ base: null, head: null }); setOpen(false); }}
                                                 data-testid="gr-range-reset">The whole change</button>
            <span className="cs-spacer" /><button type="button" className="lr-btn" data-kind="primary" onClick={() => setOpen(false)}>Done</button></div>
        </BoxPop>) : null}
    </span>
  );
}

// ── Commits: All commits (the default), one commit by its own link, two compared ─────────────

/** A real link that switches the view in place (a click), and opens in a new tab, is copied and
 *  survives Back like any link. */
function ViewLink({ href, onGo, children, className, testid, current }: {
  href: string; onGo: () => void; children: React.ReactNode; className?: string; testid?: string; current?: boolean;
}) {
  return (
    <a href={href} className={className} data-testid={testid} aria-current={current ? "page" : undefined}
       onClick={(e) => { if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return; e.preventDefault(); onGo(); }}>{children}</a>
  );
}

export function CommitsTab({ cs, at, commits, shown, onShow, hrefFor, wholeFiles, wholeDiff, review, inline, onCompare, onViewFile }: {
  cs: ChangeSet; at: ChangeAt; commits: Commit[];
  /** The commit open (the URL's /commits/<sha>), or null for All commits. */
  shown: string | null; onShow: (sha: string | null) => void; hrefFor: (sha: string | null) => string;
  /** All commits: the whole change so far (base…head), its comments inline. */
  wholeFiles: FileStat[]; wholeDiff: string; review: LineReview; inline: Map<string, InlineNote[]>;
  onCompare: (r: Range) => void; onViewFile: (path: string) => void;
}) {
  const [ticked, setTicked] = useState<string[]>([]);
  const [view, setView] = useState<CommitView | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [layout, setLayout] = useLayoutPref();
  useEffect(() => {
    if (!shown) { setView(null); return undefined; }
    let alive = true;
    setView(null); setErr(null);
    getCommit(at, shown).then((v) => { if (alive) setView(v); }).catch((e: unknown) => { if (alive) setErr(refusalSentence(e)); });
    return () => { alive = false; };
  }, [at, shown]);
  const range = rangeOfTicked(commits, ticked);
  const commitFiles = useMemo(() => (view ? changedFiles(view.files, parseUnifiedDiff(view.diff)) : []), [view]);
  // A commit's own view: the comments made on that commit, under their line (read-only here).
  const madeHere = useMemo(() => cs.comments.filter((t) => !!view && !!t.path && (t.made_on ?? t.commit_sha ?? "") === view.sha), [cs.comments, view]);
  const commitReview = useLineReviewReadOnly(at, madeHere);
  const nb = neighbours(commits, shown);
  const sumAll = totals(wholeFiles);
  const list = (
    <ol className="gr-cl" aria-label="Commits" data-testid="change-set-commits">
      <li data-on={!shown || undefined} data-testid="gr-commit-all">
        <span className="gr-c-box" aria-hidden />
        <ViewLink href={hrefFor(null)} onGo={() => onShow(null)} className="gr-c-msg" testid="gr-commit-all-open" current={!shown}>
          <b>All commits</b><span className="cs-faint cs-small">the whole change so far · {cs.base_branch} … head</span></ViewLink>
        <span className="gr-c-stat"><span className="cs-add">+{sumAll.additions}</span> <span className="cs-del">−{sumAll.deletions}</span>
          <span className="cs-faint"> · {sumAll.files} file{sumAll.files === 1 ? "" : "s"}</span></span>
        <span /><span />
      </li>
      {commits.map((c) => (
        <li key={c.sha} data-on={(shown && c.sha.startsWith(shown)) || undefined} data-testid="gr-commit">
          <input type="checkbox" checked={ticked.includes(c.sha)} aria-label={`Compare ${c.sha.slice(0, 7)}`}
                 onChange={() => setTicked((t) => tick(t, c.sha))} data-testid="gr-commit-tick" />
          <ViewLink href={hrefFor(c.sha)} onGo={() => onShow(c.sha)} className="gr-c-msg" testid="gr-commit-open"
                    current={!!shown && c.sha.startsWith(shown)}>
            <span className="cs-ell" title={c.message}>{c.message.split("\n")[0]}</span>
            {c.fixes.length ? <span className="lr-tag" data-tone="green" data-testid="gr-commit-fixes">fixes {c.fixes.length} comment{c.fixes.length === 1 ? "" : "s"}</span> : null}
          </ViewLink>
          <span className="gr-c-stat" data-testid="gr-commit-stat">{c.additions != null
            ? <><span className="cs-add">+{c.additions}</span> <span className="cs-del">−{c.deletions}</span> <span className="cs-faint">· {c.files} file{c.files === 1 ? "" : "s"}</span></>
            : <span className="cs-faint">—</span>}</span>
          <code className="cs-chip" title={c.sha}>{c.sha.slice(0, 7)}</code>
          <span className="cs-faint cs-small gr-c-who">{c.author}{c.date ? ` · ${shortWhen(c.date)}` : ""}</span>
        </li>))}
    </ol>
  );
  const compare = ticked.length ? (
    <div className="gr-cmpbar" data-testid="gr-compare-bar">
      {range ? <>Compare <code>{range.base!.slice(0, 7)}</code> … <code>{range.head!.slice(0, 7)}</code></> : <span className="cs-faint">Tick one more to compare</span>}
      <span className="cs-spacer" />
      <button type="button" className="lr-btn" data-kind="ghost" onClick={() => setTicked([])} data-testid="gr-compare-clear">Clear</button>
      <button type="button" className="lr-btn" data-kind="primary" disabled={!range} onClick={() => range && onCompare(range)} data-testid="gr-compare">
        Compare</button>
    </div>) : null;
  const head = shown ? (
    <div className="gr-cmp gr-commit-h" data-testid="gr-commit-head">
      <GitCommit size={14} aria-hidden />
      <b className="cs-ell" data-testid="gr-commit-message">{view ? view.message.split("\n")[0] : shown.slice(0, 7)}</b>
      <code className="cs-chip" data-testid="gr-commit-sha">{(view?.sha ?? shown).slice(0, 7)}</code>
      {view ? <span className="cs-faint cs-small">{view.author}{view.date ? ` · ${shortWhen(view.date)}` : ""}</span> : null}
      <span data-testid="gr-totals"><span className="cs-add">+{totals(commitFiles).additions}</span> <span className="cs-del">−{totals(commitFiles).deletions}</span></span>
      <span className="cs-spacer" />
      <LayoutToggle layout={layout} setLayout={setLayout} />
      <FullScreenButton />
      {nb.prev ? <ViewLink href={hrefFor(nb.prev)} onGo={() => onShow(nb.prev)} className="lr-btn" testid="gr-commit-prev">‹ Previous</ViewLink>
        : <button type="button" className="lr-btn" data-kind="ghost" disabled data-testid="gr-commit-prev">‹ Previous</button>}
      {nb.next ? <ViewLink href={hrefFor(nb.next)} onGo={() => onShow(nb.next)} className="lr-btn" testid="gr-commit-next">Next ›</ViewLink>
        : <button type="button" className="lr-btn" data-kind="ghost" disabled data-testid="gr-commit-next">Next ›</button>}
    </div>
  ) : (
    <div className="gr-cmp" data-testid="gr-commit-head">
      <b>All commits</b><span className="cs-faint cs-small">{commits.length} commit{commits.length === 1 ? "" : "s"}</span>
      <span className="cs-spacer" /><LayoutToggle layout={layout} setLayout={setLayout} /><FullScreenButton />
    </div>
  );
  return (
    <section className="gr-commits" aria-label="Commits">
      {list}
      {compare}
      <div className="gr-commit-view" data-testid={shown ? "gr-commit-view" : "gr-commit-all-view"}>
        {!shown ? (
          <DiffBrowser cs={cs} at={at} files={wholeFiles} diff={wholeDiff} review={review} inline={inline} layout={layout}
                       toolbar={head} onViewFile={onViewFile} allowAll={false} />
        ) : err ? <>{head}<p className="cs-error" role="alert">{err}</p></> : !view
          ? <>{head}<div className="cs-loading" role="status" aria-label="Loading the commit"><i /><i /></div></>
          : <DiffBrowser cs={cs} at={at} files={commitFiles} diff={view.diff} review={commitReview} inline={new Map()} layout={layout}
                         allowAll={false} toolbar={head} onViewFile={onViewFile} />}
      </div>
    </section>
  );
}

/** Comments under their line in a commit's own view: shown, not written (a new comment goes on
 *  the change's head, in Files changed). */
function useLineReviewReadOnly(at: ChangeAt, threads: Thread[]): LineReview {
  return useLineReview({ at, threads, onChanged: () => undefined, open: false, anyLine: true, commitFor: () => null });
}

function shortWhen(iso: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return d.toDateString() === new Date().toDateString()
    ? d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : d.toLocaleDateString([], { day: "numeric", month: "short" });
}

// ── Conversation ─────────────────────────────────────────────────────────────────────────────

type Event = { at: string | null; icon: React.ReactNode; text: React.ReactNode; key: string; thread?: number };

export function timeline(cs: ChangeSet): Omit<Event, "icon">[] {
  const out: Omit<Event, "icon">[] = [];
  const commits = cs.commits ?? [];
  if (commits.length) {
    const who = commits[commits.length - 1].author || "Someone";
    out.push({ key: "commits", at: commits[commits.length - 1].date ?? cs.created_at,
               text: `${who} pushed ${commits.length} commit${commits.length === 1 ? "" : "s"}` });
  }
  for (const n of cs.review) out.push({ key: `n${n.id}`, at: null, text: `${n.author} ${noteVerb(n)}` });
  if (cs.checks) {
    const t = cs.checks.tests_passed;
    if (t === "true" || t === "false") out.push({ key: "tests", at: null, text: t === "true" ? "Tests passed" : "Tests failed" });
  }
  for (const t of cs.comments.filter((x) => x.author_kind === "human")) {
    out.push({ key: `c${t.id}`, at: t.created_at, thread: t.id,
               text: `${t.author} commented${t.path ? ` on ${t.path.split("/").pop()}${t.line ? `:${t.line}` : ""}` : ""}${t.state === "resolved" ? " · resolved" : t.fixed_in ? ` · fixed in ${t.fixed_in.slice(0, 7)}` : ""}` });
  }
  if (cs.state === "merged") out.push({ key: "merged", at: cs.merged_at, text: `${cs.merged_by ?? "Someone"} merged it` });
  return out.sort((a, b) => String(a.at ?? "").localeCompare(String(b.at ?? "")));
}

function noteVerb(n: ChangeSetNote): string {
  const e = (n.event || n.kind || "").toUpperCase();
  return e.includes("APPROVE") ? "approved" : e.includes("CHANGES") ? "asked for changes" : "reviewed";
}

export function ConversationTab({ cs, at, files, onChanged, onJump, onFiles, commitHref }: {
  cs: ChangeSet; at: ChangeAt; files: FileStat[]; onChanged: () => void; onJump: (t: Thread) => void; onFiles: () => void;
  commitHref?: (sha: string) => string;
}) {
  const [more, setMore] = useState(false);
  const events = timeline(cs);
  const body = withoutWhatChanged(cs.body);
  const sum = totals(files);
  return (
    <section className="gr-conv" aria-label="Conversation" data-testid="gr-conversation">
      <p className="gr-what" data-testid="gr-what-changed">
        <button type="button" className="lr-link" onClick={onFiles}>{sum.files} file{sum.files === 1 ? "" : "s"} changed</button>
        <span className="cs-add">+{sum.additions}</span> <span className="cs-del">−{sum.deletions}</span>
        <span className="cs-faint"> · {cs.commits.length} commit{cs.commits.length === 1 ? "" : "s"}</span></p>
      {body ? (
        <div className="cs-body" data-testid="change-set-body" data-clamp3={!more || undefined}>
          <div className="cs-body-md" data-user-text><Markdown text={body} /></div>
          <button type="button" className="cs-more" data-testid="change-set-body-more" onClick={() => setMore((v) => !v)}>{more ? "Less" : "More"}</button>
        </div>) : null}
      <ol className="gr-timeline" aria-label="Timeline" data-testid="gr-timeline">
        {events.map((e) => (
          <li key={e.key} data-testid={e.thread != null ? "gr-timeline-comment" : undefined}>
            <span>{e.text}</span>
            {e.at ? <span className="cs-faint cs-small"> · {shortWhen(e.at)}</span> : null}
            {e.thread != null ? (() => { const t = cs.comments.find((x) => x.id === e.thread);
              return t ? <CommentCode thread={t} onOpen={onJump as (t: never) => void} commitHref={commitHref} small /> : null; })() : null}
          </li>))}
        {!events.length ? <li className="cs-empty">Nothing yet.</li> : null}
      </ol>
      <Review notes={cs.review} threads={cs.comments} at={at} onChanged={onChanged} onOpen={onJump} commitHref={commitHref} />
    </section>
  );
}

function eventTone(e: string): string {
  const v = e.toUpperCase();
  return v.includes("APPROVE") ? "green" : v.includes("CHANGES") || v.includes("REQUEST") ? "amber" : "grey";
}

export function Review({ notes, threads = [], at, onChanged, onOpen, commitHref }: {
  notes: ChangeSetNote[]; threads?: Thread[]; at?: ChangeAt; onChanged?: () => void;
  /** Every thread here is out of the diff: it shows its code, and a click opens it there. */
  onOpen?: (t: Thread) => void; commitHref?: (sha: string) => string;
}) {
  // People's threads first (open before resolved), then the review panel's notes.
  const people = threads.filter((t) => t.author_kind === "human");
  const agents = threads.filter((t) => t.author_kind !== "human");
  const where = at ?? { projectId: "", number: "" };
  return (
    <section className="cs-card" id="cs-review" data-testid="change-set-review" aria-label="Review">
      <div className="cs-card-h"><ChatCircleDots size={15} aria-hidden /><b>Review</b><span className="cs-faint">{notes.length + threads.length}</span></div>
      {[...people, ...agents].length ? (
        <div className="lr-threads" data-testid="lr-review-threads">
          {[...people, ...agents].map((t) => <ThreadView key={t.id} thread={t} at={where} withCode onOpen={onOpen} commitHref={commitHref}
                                                         onChanged={onChanged ?? (() => undefined)} compact />)}
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

// ── Checks ───────────────────────────────────────────────────────────────────────────────────

export function checksWords(cs: ChangeSet): { tone: "green" | "red" | "amber" | "grey"; label: string } {
  const c = cs.checks;
  if (!c) return { tone: "grey", label: "No checks" };
  const tests = c.tests_passed;
  if (tests === "false") return { tone: "red", label: "Tests failed" };
  if (tests === "true") return { tone: "green", label: "Tests passed" };
  const failing = (c.items ?? []).some((i) => /fail|error|cancel|timed/.test(String(i.conclusion)));
  if (failing) return { tone: "red", label: "Checks failed" };
  const s = (c.run_status ?? "").toLowerCase();
  if (/fail|error|cancel/.test(s)) return { tone: "red", label: `Build ${s.replace(/_/g, " ")}` };
  if (/complete|succeed|done|merged/.test(s)) return { tone: "green", label: "Build done" };
  if (!s && (c.items ?? []).length) return (c.items ?? []).every((i) => i.conclusion === "success" || i.conclusion === "skipped" || i.conclusion === "neutral")
    ? { tone: "green", label: "Checks passed" } : { tone: "amber", label: "Checks running" };
  return { tone: "amber", label: s ? `Build ${s.replace(/_/g, " ")}` : "Checks —" };
}

export function ChecksTab({ cs, decision }: { cs: ChangeSet; decision: React.ReactNode }) {
  const c = cs.checks;
  const tests = c?.tests_passed;
  const ck = checksWords(cs);
  return (
    <div className="gr-checks">
      {decision}
      <AgentActions cs={cs} />
      <section className="cs-card" id="cs-checks" data-testid="change-set-checks" aria-label="Checks">
        <div className="cs-card-h"><ShieldCheck size={15} aria-hidden /><b>Checks</b><span className="cs-spacer" /><span className="cs-pill" data-tone={ck.tone}>{ck.label}</span></div>
        {c ? (
          <ul className="cs-checks">
            {c.run_status != null ? <>
              <li><span className="cs-dot" data-tone={/fail|error/i.test(c.run_status ?? "") ? "red" : c.run_status ? "green" : "grey"} aria-hidden />Build<span className="cs-spacer" /><span className="cs-faint">{(c.run_status ?? "—").replace(/_/g, " ")}</span></li>
              <li><span className="cs-dot" data-tone={tests === "true" ? "green" : tests === "false" ? "red" : "grey"} aria-hidden />Tests<span className="cs-spacer" /><span className="cs-faint">{tests === "true" ? "passed" : tests === "false" ? "failed" : tests ?? "—"}</span></li>
            </> : null}
            {(c.items ?? []).map((i) => (
              <li key={`${i.app}${i.name}`} data-testid="gr-check-run"><span className="cs-dot" data-tone={i.conclusion === "success" ? "green" : /fail|error|cancel|timed/.test(String(i.conclusion)) ? "red" : "amber"} aria-hidden />
                {i.name}<span className="cs-spacer" /><span className="cs-faint">{i.conclusion ?? i.status ?? "—"}</span></li>))}
            <li className="cs-faint cs-small">In {c.where}</li>
          </ul>
        ) : <p className="cs-empty">No build recorded for this change.</p>}
      </section>
    </div>
  );
}

// ── the PR agent ─────────────────────────────────────────────────────────────────────────────

export function AgentPane({ cs, at, onJump, onChanged, onClose, refreshKey, commitHref }: {
  cs: ChangeSet; at: ChangeAt; onJump: (id: number) => void; onChanged: () => void; onClose?: () => void; refreshKey?: unknown;
  commitHref?: (sha: string) => string;
}) {
  const [data, setData] = useState<AgentData | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [said, setSaid] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState<"send" | "fix" | null>(null);
  const read = useCallback(() => getAgent(at).then(setData).catch((e: unknown) => setErr(refusalSentence(e))), [at]);
  useEffect(() => { void read(); }, [read, refreshKey]);
  useEffect(() => {
    if (!data?.run?.running) return undefined;
    const t = window.setInterval(() => { void read(); }, 8000);
    return () => window.clearInterval(t);
  }, [data?.run?.running, read]);
  const status = agentStatus(data);
  const counts = commentCounts(data?.comments ?? []);
  const fixable = data?.fixable ?? 0;
  const name = data?.agent || data?.builder?.builder || "builder";
  const send = async () => {
    const body = draft.trim();
    if (!body || busy) return;
    setBusy("send"); setErr(null);
    try { const r = await sendToAgent(at, body); setSaid(r.message); setDraft(""); await read(); }
    catch (e) { setErr(refusalSentence(e)); } finally { setBusy(null); }
  };
  const fix = async () => {
    if (busy || !fixable) return;
    setBusy("fix"); setErr(null);
    try { await fixComments(at, ""); setSaid(`The fix run started on ${cs.branch}.`); onChanged(); await read(); }
    catch (e) { setErr(refusalSentence(e)); } finally { setBusy(null); }
  };
  return (
    <aside className="gr-agent" aria-label="The PR agent" data-testid="gr-agent">
      <div className="gr-ah">
        <Robot size={20} aria-hidden />
        <div className="gr-ah-t"><b data-testid="gr-agent-name">{name.replace(/_/g, " ")}</b>
          <small className="cs-faint">builder of this change · <span data-tone={status.tone} data-testid="gr-agent-status">{status.label}</span>
            {data?.run?.cost_usd ? ` · $${data.run.cost_usd.toFixed(2)}` : ""}</small></div>
        {onClose ? <button type="button" className="lr-icon" aria-label="Close the agent" onClick={onClose}><X size={14} aria-hidden /></button> : null}
      </div>
      <div className="gr-oc" data-testid="gr-agent-comments">
        <div className="gr-oc-h"><b>Comments</b><span className="cs-pill" data-tone={counts.open ? "amber" : "grey"} data-testid="lr-open-count">{counts.open} open</span>
          <span className="cs-pill" data-tone="green">{counts.fixed} fixed</span></div>
        {(data?.comments ?? []).map((c) => {
          const t = cs.comments.find((x) => x.id === c.id);
          return (
            <div key={c.id} className="gr-oc-item" data-s={c.state} data-testid="gr-agent-comment">
              <button type="button" className="gr-oc-row" onClick={() => onJump(c.id)}>
                <span className="cs-ell">{c.body}</span>
                <span className="gr-oc-st">{c.queued ? "queued" : c.state === "resolved" ? "resolved" : c.fixed_in ? <>fixed in <code>{c.fixed_in.slice(0, 7)}</code></>
                  : c.state === "disputed" ? "disputed" : c.state === "addressed" ? "answered" : "open"}</span>
              </button>
              {t ? <CommentCode thread={t} onOpen={(() => onJump(c.id)) as (t: never) => void} commitHref={commitHref} small /> : null}
            </div>);
        })}
        {data && !data.comments.length ? <p className="cs-empty">No comments from people yet. Press + on a line.</p> : null}
        <CanButton permission="decision:approve" projectId={at.projectId} className="lr-btn gr-fix" data-kind="primary"
                   disabled={!fixable || !!busy || cs.state !== "open"} onClick={() => void fix()} data-testid="gr-fix-open"
                   tip="Start a fix run on this branch with every open comment">
          {busy === "fix" ? <CircleNotch size={13} className="cs-spin" aria-hidden /> : <Wrench size={13} aria-hidden />}
          Fix {fixable} open comment{fixable === 1 ? "" : "s"}</CanButton>
      </div>
      <div className="gr-chat" aria-label="Chat with the agent" data-testid="gr-chat">
        {(data?.messages ?? []).map((m) => (
          <div key={m.id} className="gr-msg" data-me={m.author_kind === "human" || undefined} data-testid="gr-msg">
            <small className="cs-faint">{m.author_kind === "human" ? m.author : `${m.author} · ${m.about?.path ? `${m.about.path.split("/").pop()}${m.about.line ? `:${m.about.line}` : ""}` : "reply"}`}
              {m.delivery === "queued" ? " · for the next fix run" : m.delivery === "steered" ? " · sent to the running task" : ""}</small>
            <div className="cs-md" data-user-text><Markdown text={m.body} /></div>
          </div>))}
        {data && !data.messages.length ? <p className="cs-empty">{data.run ? `Ask ${name.replace(/_/g, " ")} about this change.` : "No task built this change, so there is no agent to ask."}</p> : null}
      </div>
      {said ? <p className="cs-faint cs-small gr-said" role="status">{said}</p> : null}
      {err ? <p className="cs-error" role="alert">{err}</p> : null}
      <div className="gr-compose">
        <input value={draft} onChange={(e) => setDraft(e.target.value)} placeholder={data?.run?.running ? "Steer the running task…" : "Message the agent…"}
               aria-label="Message the agent" data-testid="gr-chat-input" disabled={!data?.run}
               onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); void send(); } }} />
        <CanButton permission="code_task:steer" projectId={at.projectId} className="lr-btn" data-kind="primary"
                   disabled={!draft.trim() || !!busy || !data?.run} onClick={() => void send()} data-testid="gr-chat-send" aria-label="Send">
          {busy === "send" ? <CircleNotch size={13} className="cs-spin" aria-hidden /> : <PaperPlaneRight size={13} aria-hidden />}</CanButton>
      </div>
    </aside>
  );
}

// ── Clone (where it lives) ───────────────────────────────────────────────────────────────────

export function ClonePop({ cs, projectId, onClose }: { cs: ChangeSet; projectId: string; onClose: () => void }) {
  return (
    <BoxPop className="gr-pop gr-clone" role="dialog" aria-label="Clone" onEscape={onClose} data-testid="gr-clone-pop">
      <div className="lr-sheet-h"><Terminal size={14} aria-hidden /><b>Where it lives</b><span className="cs-spacer" />
        <button type="button" className="lr-icon" aria-label="Close" onClick={onClose}><X size={14} aria-hidden /></button></div>
      {cs.source === "github" && cs.clone ? (
        <div className="gr-clone-g">
          <div className="lr-row"><span>Clone</span><span className="lr-cmd"><code data-testid="gr-clone-url">{cs.clone.url}</code><CopyButton value={cs.clone.url} label="Copy the clone URL" /></span></div>
          <div className="lr-row"><span>Branch</span><span className="lr-cmd"><code>{cs.branch}</code><CopyButton value={cs.branch} label="Copy the branch" /></span></div>
          <div className="lr-row"><span>Fetch</span><span className="lr-cmd"><code>{cs.clone.fetch}</code><CopyButton value={cs.clone.fetch} label="Copy the command" /></span></div>
        </div>
      ) : <WhereItLives cs={cs} projectId={projectId} />}
    </BoxPop>
  );
}

// ── the header's state and source ────────────────────────────────────────────────────────────

export function StatePill({ cs }: { cs: ChangeSet }) {
  return (
    <span className="cs-pill" data-state={cs.state} data-testid="change-set-state">
      {cs.state === "merged" ? <GitMerge size={12} weight="bold" aria-hidden /> : cs.state === "closed" ? <XCircle size={12} aria-hidden /> : <GitBranch size={12} weight="bold" aria-hidden />}
      {cs.state === "published" ? "On GitHub" : cs.state.charAt(0).toUpperCase() + cs.state.slice(1)}{cs.draft ? " · draft" : ""}
    </span>
  );
}

export function BranchChips({ cs }: { cs: ChangeSet }) {
  return (
    <span className="cs-branch" data-testid="change-set-branch" title={`${cs.branch} into ${cs.base_branch}`}>
      <code className="cs-chip">{cs.branch}</code><ArrowRight size={12} aria-hidden className="cs-faint" /><code className="cs-chip">{cs.base_branch}</code>
    </span>
  );
}

export function ChecksPill({ cs }: { cs: ChangeSet }) {
  const ck = checksWords(cs);
  return (
    <span className="cs-pill" data-tone={ck.tone} title={cs.checks ? `In ${cs.checks.where}` : "No build recorded"}>
      {ck.tone === "green" ? <CheckCircle size={12} weight="fill" aria-hidden /> : ck.tone === "red" ? <XCircle size={12} weight="fill" aria-hidden /> : <ShieldCheck size={12} aria-hidden />}{ck.label}
    </span>
  );
}
