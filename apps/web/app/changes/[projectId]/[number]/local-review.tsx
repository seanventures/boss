"use client";

/**
 * LOCAL REVIEW ON THE CHANGE PAGE (BOSSAPP-948; wireframe 5-local-change-review.html):
 *
 *   (a) where it lives      the bare repository, the branch and its commit, the clone URL, the
 *                           working copies — each with Copy — and "Get a working copy"
 *   (b) the Code tab        the tree (M/A status, comment counts, Go to file) and the whole file at
 *                           this change or at its base, coloured by Shiki (loaded only here)
 *   (c) line comments       on the diff and in the Code tab: one store (services/local_review.py);
 *                           threads with the agent's replies; a PERSON resolves or reopens
 *   (d) Agent actions       the fix round Request changes started: the person's comments are its
 *                           required inputs, each ticked when the agent answers
 *
 * Fonts are var(--fs-*) steps; popovers go through components/box-pop.tsx; a local project shows
 * no GitHub words.
 */
import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ArrowCounterClockwise, ChatCircleDots, Check, CheckCircle, CircleNotch, Code, Copy, Database, FileCode,
  FolderOpen, GitBranch, LinkSimple, MagnifyingGlass, Plus, Robot, Terminal, Wrench, X,
} from "@phosphor-icons/react";

import { AgentAvatar } from "../../../agent-avatar";
import { BoxPop } from "../../../../components/box-pop";
import { CanButton } from "../../../../components/identity/identity-provider";
import { refusalSentence } from "../../../../lib/identity/api";
import {
  addComment, cloneCommand, cloneUrl, colourise, getFile, getTree, replyTo, resolveThread,
  anchorWords, snippetFocus, type ChangeAt, type FileView, type Thread, type Tok, type Tree, type Where,
} from "../../../../lib/local-review";
import { fixComments } from "../../../../lib/change-review";
import type { ChangeSet } from "../../../../lib/repo-links";
import { Markdown } from "../../../markdown";
import { whenLocal } from "../../../../lib/local-time";
import { Breadcrumb, FileTree, GoToFile } from "./file-tree";
import { DockPane, FullScreenButton, useReviewLayout } from "./review-dock";

// ── people and agents ─────────────────────────────────────────────────────────────────────────

/** An agent's name or role key gets its face; a person's name gets initials. */
// ── a comment's code, shown wherever the comment is out of the diff ──────────────────────────

/** file:line (or "on the whole change"), and 3-5 lines of the code with the commented line
 *  marked — as it was when the comment was made. Outdated: says so and links that commit. A click
 *  opens Files changed at that line. */
export function CommentCode({ thread, onOpen, commitHref, small = false }: {
  thread: Pick<Thread, "path" | "line" | "outdated" | "snippet" | "made_on" | "commit_sha"> & { id: number };
  onOpen?: (t: never) => void; commitHref?: (sha: string) => string; small?: boolean;
}) {
  const s = thread.snippet;
  const focus = s ? snippetFocus(s) : -1;
  const madeOn = thread.made_on || thread.commit_sha;
  const where = anchorWords(thread);
  const open = onOpen && thread.path ? () => (onOpen as (t: unknown) => void)(thread) : undefined;
  return (
    <div className="gr-code" data-small={small || undefined} data-testid="gr-comment-code">
      <div className="gr-code-h">
        {open ? <button type="button" className="lr-link cs-mono" onClick={open} data-testid="gr-comment-anchor">{where}</button>
          : <span className={thread.path ? "cs-mono" : "cs-faint"} data-testid="gr-comment-anchor">{where}</span>}
        {thread.outdated ? <span className="lr-tag" data-tone="amber" data-testid="gr-outdated">outdated</span> : null}
        {thread.outdated && madeOn ? (commitHref
          ? <a className="cs-small" href={commitHref(madeOn)} data-testid="gr-made-on">made on <code>{madeOn.slice(0, 7)}</code></a>
          : <span className="cs-small cs-faint">made on <code>{madeOn.slice(0, 7)}</code></span>) : null}
      </div>
      {s ? (
        <table className="gr-snip" onClick={open} data-testid="gr-snippet"><tbody>
          {s.lines.map((text, i) => (
            <tr key={i} data-focus={i === focus || undefined} data-mark={s.marks?.[i] || undefined}>
              <td className="cs-no">{s.start != null ? s.start + i : ""}</td><td className="cs-code">{text || " "}</td></tr>))}
        </tbody></table>) : null}
    </div>
  );
}

export function looksLikeAgent(name: string): boolean {
  return !name || /^(agent:|boss\b|[a-z]+(_[a-z]+)+$)|captain|reviewer|panel|engineer|helm|marshal|builder|forge/i.test(name);
}

/** A role key reads as words (engineering_captain → Engineering captain); a name or an id is
 *  shown exactly as stored — nothing is title-cased in CSS any more. */
export function whoLabel(name: string): string {
  const bare = name.replace(/^agent:/, "");
  if (!/^[a-z]+(_[a-z]+)+$/.test(bare)) return bare;
  const words = bare.replace(/_/g, " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
}

export function Who({ name, size = 22, agent }: { name: string; size?: number; agent?: boolean }) {
  const label = name || "BOSS";
  const isAgent = agent ?? looksLikeAgent(name);
  return (
    <span className="cs-who">
      {isAgent ? <AgentAvatar agent={name.replace(/^agent:/, "") || "engineering_captain"} size={size} aiBadge={false} />
        : <span className="cs-initials" style={{ width: size, height: size }} aria-hidden>{label.slice(0, 1).toUpperCase()}</span>}
      <b>{whoLabel(label)}</b>
    </span>
  );
}

// ── copy ──────────────────────────────────────────────────────────────────────────────────────

export function CopyButton({ value, label, testid }: { value: string; label: string; testid?: string }) {
  const [done, setDone] = useState(false);
  return (
    <button type="button" className="lr-copy" title={done ? "Copied" : label} aria-label={label} data-testid={testid}
            onClick={() => {
              try { void navigator.clipboard?.writeText(value); } catch { /* the value is on screen */ }
              setDone(true); window.setTimeout(() => setDone(false), 1400);
            }}>
      {done ? <Check size={13} weight="bold" aria-hidden /> : <Copy size={13} aria-hidden />}
    </button>
  );
}

// ── (a) where it lives ────────────────────────────────────────────────────────────────────────

export function WhereItLives({ cs, projectId }: { cs: ChangeSet; projectId: string }) {
  const w = cs.where;
  const [sheet, setSheet] = useState(false);
  if (!w) return null;
  const url = cloneUrl(w);
  const head = (w.head_sha || cs.head_sha || "").slice(0, 7);
  const copy = w.working_copies[0];
  return (
    <section className="lr-where" aria-label="Where it lives" data-testid="lr-where">
      <div className="lr-cell">
        <span className="lr-k"><Database size={13} aria-hidden />Stored in BOSS</span>
        <span className="lr-v"><code className="cs-ell" title={w.storage_path} data-testid="lr-storage">{w.storage_path}</code>
          <CopyButton value={w.storage_path} label="Copy the path" /></span>
        <small>{w.volume ? `Docker volume ${w.volume} · not a folder on your computer` : "BOSS's repository on this machine"}</small>
      </div>
      <div className="lr-cell">
        <span className="lr-k"><GitBranch size={13} aria-hidden />Branch · commit</span>
        <span className="lr-v"><code className="cs-ell" title={w.branch}>{w.branch}</code><CopyButton value={w.branch} label="Copy the branch" /></span>
        <small><code>{head}</code>{w.ahead != null ? ` · ${w.ahead} commit${w.ahead === 1 ? "" : "s"} ahead of ${cs.base_branch}` : ""}</small>
      </div>
      <div className="lr-cell">
        <span className="lr-k"><LinkSimple size={13} aria-hidden />Git remote</span>
        <span className="lr-v"><code className="cs-ell" title={url} data-testid="lr-remote">{url}</code><CopyButton value={url} label="Copy the clone URL" testid="lr-copy-remote" /></span>
        <small>{w.reach === "network" ? "your network · your BOSS token signs in" : "this machine only · your BOSS token signs in"}</small>
      </div>
      <div className="lr-cell">
        <span className="lr-k"><FolderOpen size={13} aria-hidden />Working copy</span>
        {copy ? <span className="lr-v"><code className="cs-ell" title={copy.path} data-testid="lr-working-copy">{copy.path}</code><CopyButton value={copy.path} label="Copy the folder" /></span>
          : <span className="lr-v cs-faint" data-testid="lr-working-copy">none yet</span>}
        <small>{copy ? `${copy.branch ? `on ${copy.branch} · ` : ""}${copy.machine || "this machine"}` : "a folder you can open in your editor"}</small>
      </div>
      <div className="lr-get">
        <button type="button" className="lr-btn" data-kind="primary" aria-expanded={sheet} data-testid="lr-get-copy"
                onClick={() => setSheet((v) => !v)}>
          <Terminal size={14} aria-hidden />Get a working copy</button>
        {sheet ? <WorkingCopySheet projectId={projectId} branch={cs.branch} onClose={() => setSheet(false)} /> : null}
      </div>
    </section>
  );
}

function WorkingCopySheet({ projectId, branch, onClose }: { projectId: string; branch: string; onClose: () => void }) {
  const [folder, setFolder] = useState(`~/code/${projectId}`);
  const [copied, setCopied] = useState(false);
  const command = cloneCommand(projectId, folder.trim() || `~/code/${projectId}`, branch);
  return (
    <BoxPop className="lr-sheet" role="dialog" aria-label="Get a working copy" onEscape={onClose} data-testid="lr-copy-sheet">
      <div className="lr-sheet-h"><b>Get a working copy</b><span className="cs-faint cs-ell">{projectId} · {branch}</span>
        <span className="cs-spacer" /><button type="button" className="lr-icon" aria-label="Close" onClick={onClose}><X size={14} aria-hidden /></button></div>
      <ol className="lr-steps" aria-label="Steps">
        <li data-s="done"><i aria-hidden /><b>Pick a folder</b><small className="cs-ell">{folder}</small></li>
        <li data-s={copied ? "done" : "run"}><i aria-hidden /><b>Clone</b><small>on this branch</small></li>
        <li><i aria-hidden /><b>Open</b><small>your editor, or a terminal</small></li>
      </ol>
      <label className="lr-row"><span>Folder</span>
        <input className="lr-input" value={folder} onChange={(e) => setFolder(e.target.value)} spellCheck={false} data-testid="lr-folder" /></label>
      <div className="lr-row"><span>Runs</span>
        <span className="lr-cmd"><code data-testid="lr-clone-command">{command}</code><CopyButton value={command} label="Copy the command" /></span></div>
      <p className="lr-row lr-note"><span>Sign-in</span><span>Your BOSS access token goes to the keychain through git&apos;s credential helper. It never sits in the URL or in .git/config.</span></p>
      <div className="lr-sheet-f">
        <span className="cs-faint cs-small">Run it in a terminal on this machine; the page lists the folder under Working copy once it exists.</span>
        <button type="button" className="lr-btn" data-kind="ghost" onClick={onClose}>Cancel</button>
        <button type="button" className="lr-btn" data-kind="primary" data-testid="lr-copy-command"
                onClick={() => { try { void navigator.clipboard?.writeText(`${command} --open`); } catch { /* on screen */ } setCopied(true); }}>
          {copied ? <><Check size={14} aria-hidden />Copied: paste it in a terminal</> : <><Copy size={14} aria-hidden />Copy clone and open</>}
        </button>
      </div>
    </BoxPop>
  );
}

// ── threads and the composer ──────────────────────────────────────────────────────────────────

const SEVERITIES: { key: string; label: string }[] = [
  { key: "must fix", label: "Must fix" }, { key: "should fix", label: "Should fix" }, { key: "nit", label: "Nit" }];

export function Composer({ where, onSubmit, onCancel, busy, error, reply = false }: {
  where?: string; onSubmit: (body: string, severity: string) => void; onCancel: () => void;
  busy?: boolean; error?: string | null; reply?: boolean;
}) {
  const [body, setBody] = useState("");
  const [sev, setSev] = useState("must fix");
  const send = () => { if (body.trim() && !busy) onSubmit(body.trim(), sev); };
  return (
    <BoxPop className="lr-composer" fit={false} onEscape={onCancel} data-testid={reply ? "lr-reply-composer" : "lr-composer"}>
      {where ? <div className="lr-composer-h cs-faint">{where}</div> : null}
      <textarea className="lr-input" rows={3} value={body} aria-label={reply ? "Reply" : "Comment"}
                placeholder={reply ? "Reply in this thread" : "What should change on this line?"} data-testid="lr-body"
                onChange={(e) => setBody(e.target.value)}
                onKeyDown={(e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); send(); } }} />
      <div className="lr-composer-f">
        {!reply ? (
          <span className="lr-seg" role="radiogroup" aria-label="How much it blocks">
            {SEVERITIES.map((s) => (
              <button key={s.key} type="button" role="radio" aria-checked={sev === s.key} onClick={() => setSev(s.key)}>{s.label}</button>))}
          </span>) : null}
        <span className="cs-spacer" />
        <button type="button" className="lr-btn" data-kind="ghost" onClick={onCancel}>Cancel</button>
        <button type="button" className="lr-btn" data-kind="primary" disabled={!body.trim() || busy} onClick={send} data-testid="lr-send">
          {busy ? <CircleNotch size={13} className="cs-spin" aria-hidden /> : null}{reply ? "Reply" : "Comment"} <kbd>⌘↵</kbd></button>
      </div>
      {error ? <p className="cs-error" role="alert">{error}</p> : null}
    </BoxPop>
  );
}

const STATE_WORDS: Record<string, string> = { open: "Open", addressed: "Answered", disputed: "Disputed", resolved: "Resolved" };

function when(iso: string | null): string {
  return iso ? whenLocal(iso) : "";
}

const ORIGIN_WORDS: Record<string, string> = { git_notes: "from git", cli: "from the terminal", mcp: "from MCP" };

export function ThreadView({ thread, at, onChanged, compact = false, withCode, onOpen, commitHref }: {
  thread: Thread; at: ChangeAt; onChanged: () => void; compact?: boolean;
  /** Out of the diff (the Conversation tab): show the code it is about, a click opens it. */
  withCode?: boolean; onOpen?: (t: Thread) => void; commitHref?: (sha: string) => string;
}) {
  const projectId = at.projectId;
  const [replying, setReplying] = useState(false);
  const [busy, setBusy] = useState(false);
  const [said, setSaid] = useState<string | null>(null);
  const act = async (fn: () => Promise<unknown>) => {
    setBusy(true); setSaid(null);
    try { await fn(); setReplying(false); onChanged(); } catch (e) { setSaid(refusalSentence(e)); } finally { setBusy(false); }
  };
  const resolved = thread.state === "resolved";
  return (
    <div className="lr-thread" data-s={thread.state} data-testid="lr-thread" data-thread={thread.id} tabIndex={-1}>
      {withCode ? <CommentCode thread={thread} onOpen={onOpen} commitHref={commitHref} /> : null}
      {[thread, ...thread.replies].map((c) => (
        <div className="lr-c" key={c.id}>
          <div className="lr-c-b">
            <div className="lr-c-meta cs-faint">
              <Who name={c.author} size={20} agent={c.author_kind === "agent"} />
              {c.id === thread.id && thread.path ? <><code>{thread.path}{thread.line ? `:${thread.line}` : ""}</code> · </> : null}
              {c.id === thread.id ? `${thread.severity_words} · ` : ""}
              {ORIGIN_WORDS[c.origin] ? <span className="lr-tag">{ORIGIN_WORDS[c.origin]}</span> : null}
              {when(c.created_at)}
              {c.id === thread.id && thread.outdated ? <span className="lr-tag" data-tone="amber">outdated</span> : null}
              {c.id === thread.id && thread.kept ? <span className="lr-tag" data-testid="lr-kept"
                title={thread.in_review_body ? "GitHub could not take it on a line: it is in the pull request's review body"
                  : "Not on the pull request"}>kept from before publish</span> : null}
              {c.queued ? <span className="lr-tag" data-tone="amber" title="GitHub did not answer: BOSS posts it when GitHub is back"
                                data-testid="lr-queued">queued</span> : null}
            </div>
            {c.id === thread.id && thread.outdated && thread.line_text && !compact
              ? <pre className="lr-quote" aria-label="The line it was about">{thread.line_text}</pre> : null}
            <div className="cs-md lr-c-text" data-user-text><Markdown text={c.body} /></div>
          </div>
        </div>
      ))}
      <div className="lr-thread-f">
        {resolved ? <CheckCircle size={14} weight="fill" className="lr-ok" aria-hidden /> : <ChatCircleDots size={14} aria-hidden />}
        <b data-testid="lr-thread-state">{STATE_WORDS[thread.state]}</b>
        {resolved && thread.resolved_by ? <span className="cs-faint">by {thread.resolved_by} · {when(thread.resolved_at)}</span> : null}
        {thread.fixed_in ? <span className="lr-tag" data-tone="green" data-testid="lr-fixed-in">fixed in <code>{thread.fixed_in.slice(0, 7)}</code></span> : null}
        <span className="cs-spacer" />
        {!resolved && !thread.queued && !thread.kept ? <CanButton permission="code_task:steer" projectId={projectId} className="lr-link" onClick={() => setReplying(true)}
                                data-testid="lr-reply">Reply</CanButton> : null}
        {!resolved && !thread.queued && !thread.kept && thread.author_kind === "human" && thread.state === "open" ? (
          <CanButton permission="decision:approve" projectId={projectId} className="lr-link" disabled={busy}
                     tip="Start a fix run on this branch with this comment (and the other open ones)"
                     onClick={() => void act(() => fixComments(at, `Fix this one first: ${thread.path ? `${thread.path}${thread.line ? `:${thread.line}` : ""} — ` : ""}${thread.body.split("\n")[0]}`))}
                     data-testid="lr-ask-fix"><Wrench size={12} aria-hidden />Ask agent to fix</CanButton>) : null}
        {thread.queued || thread.kept || thread.kind === "conversation" ? null : resolved
          ? <CanButton permission="code_task:steer" projectId={projectId} className="lr-link" disabled={busy}
                       onClick={() => void act(() => resolveThread(at, thread.id, true))} data-testid="lr-reopen">
              <ArrowCounterClockwise size={12} aria-hidden />Reopen</CanButton>
          : <CanButton permission="code_task:steer" projectId={projectId} className="lr-link" disabled={busy}
                       onClick={() => void act(() => resolveThread(at, thread.id))} data-testid="lr-resolve">
              <Check size={12} aria-hidden />Resolve</CanButton>}
      </div>
      {replying ? <Composer reply busy={busy} error={said} onCancel={() => setReplying(false)}
                            onSubmit={(body) => void act(() => replyTo(at, thread.id, body))} /> : null}
      {said && !replying ? <p className="cs-error" role="alert">{said}</p> : null}
    </div>
  );
}

// ── comments on lines: one hook for the diff and the Code tab ─────────────────────────────────

type Anchor = { path: string; line: number; side: "new" | "old" };
const keyOf = (a: Anchor) => `${a.side}:${a.path}:${a.line}`;

export type LineReview = {
  /** The "+" in a line's gutter (a person may comment). */
  gutter: (a: Anchor) => React.ReactNode;
  /** The threads and the open composer under a line. */
  under: (a: Anchor) => React.ReactNode;
  has: (a: Anchor) => boolean;
};

export function useLineReview({ at, threads, commitFor, onChanged, open = true, anyLine = false }: {
  at: ChangeAt; threads: Thread[];
  /** A commit's own view: its comments sit on the line they were made on, outdated or not. */
  anyLine?: boolean;
  /** The commit a new comment on this side is anchored to (the head, or the base). */
  commitFor: (side: "new" | "old") => string | null; onChanged: () => void; open?: boolean;
}): LineReview {
  const [composing, setComposing] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [said, setSaid] = useState<string | null>(null);
  const byLine = useMemo(() => {
    const m = new Map<string, Thread[]>();
    for (const t of threads) {
      if (!t.path || t.line == null || (t.outdated && !anyLine)) continue;
      const k = keyOf({ path: t.path, line: t.line, side: t.side });
      m.set(k, [...(m.get(k) ?? []), t]);
    }
    return m;
  }, [threads, anyLine]);
  const submit = useCallback(async (a: Anchor, body: string, severity: string) => {
    setBusy(true); setSaid(null);
    try {
      await addComment(at, { body, severity, path: a.path, line: a.line, side: a.side, commit_sha: commitFor(a.side) });
      setComposing(null); onChanged();
    } catch (e) { setSaid(refusalSentence(e)); } finally { setBusy(false); }
  }, [at, commitFor, onChanged]);
  return {
    has: (a) => byLine.has(keyOf(a)) || composing === keyOf(a),
    gutter: (a) => !open ? null : (
      <CanButton permission="code_task:steer" projectId={at.projectId} className="lr-plus" aria-label={`Comment on ${a.path} line ${a.line}`}
                 tip="Comment on this line" onClick={() => { setComposing(keyOf(a)); setSaid(null); }} data-testid="lr-plus">
        <Plus size={11} weight="bold" aria-hidden /></CanButton>),
    under: (a) => {
      const list = byLine.get(keyOf(a)) ?? [];
      const writing = composing === keyOf(a);
      if (!list.length && !writing) return null;
      return (
        <div className="lr-under">
          {list.map((t) => <ThreadView key={t.id} thread={t} at={at} onChanged={onChanged} />)}
          {writing ? <Composer where={`${a.path}:${a.line} · ${a.side === "old" ? "old side" : "new side"}`} busy={busy} error={said}
                               onCancel={() => setComposing(null)} onSubmit={(b, s) => void submit(a, b, s)} /> : null}
        </div>
      );
    },
  };
}

// ── (b) the Code tab: the whole tree and a file at this change, its base, or any commit ──────

export function CodeTab({ cs, at, startPath, onChanged, commits = [] }: {
  cs: ChangeSet; at: ChangeAt; startPath: string | null; onChanged: () => void;
  /** The change's commits (newest first): the file at any of them. */
  commits?: { sha: string; message: string }[];
}) {
  const [ref, setRef] = useState<string>("head");
  const [tree, setTree] = useState<Tree | null>(null);
  const [path, setPath] = useState<string | null>(startPath);
  const [file, setFile] = useState<FileView | null>(null);
  const [toks, setToks] = useState<Tok[][] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => { if (startPath) setPath(startPath); }, [startPath]);
  useEffect(() => {
    let alive = true;
    getTree(at, ref).then((t) => {
      if (!alive) return;
      setTree(t);
      setPath((p) => p ?? (t.items.find((i) => i.status && i.status !== "D")?.path ?? t.items[0]?.path ?? null));
    }).catch((e: unknown) => { if (alive) setErr(refusalSentence(e)); });
    return () => { alive = false; };
  }, [at, ref, cs.comment_summary.threads, cs.head_sha]);
  useEffect(() => {
    if (!path) return undefined;
    let alive = true;
    setFile(null); setToks(null); setErr(null);
    getFile(at, path, ref).then(async (f) => {
      if (!alive) return;
      setFile(f);
      if (f.text) { const t = await colourise(f.text, f.language); if (alive) setToks(t); }
    }).catch((e: unknown) => { if (alive) setErr(refusalSentence(e)); });
    return () => { alive = false; };
  }, [at, path, ref]);
  const items = tree?.items ?? [];
  const { layout, picked } = useReviewLayout();
  const openFile = (p: string, reveal = false) => { setPath(p); if (reveal) setReveal({ path: p, n: Date.now() }); picked("tree"); };
  const [reveal, setReveal] = useState<{ path: string; n: number } | null>(null);
  // Comments sit on the head (new side) or the base (old side); at another commit, the ones made
  // on that commit show (read-only), under their line.
  const side: "new" | "old" = ref === "base" ? "old" : "new";
  const threads = useMemo(() => (ref === "head" || ref === "base")
    ? cs.comments.filter((t) => t.path === path && t.side === side && !t.outdated)
    : cs.comments.filter((t) => t.path === path && (t.made_on ?? t.commit_sha ?? "").startsWith(ref)), [cs.comments, path, ref, side]);
  const review = useLineReview({ at, threads, onChanged, open: cs.state === "open" && (ref === "head" || ref === "base"),
                                 anyLine: ref !== "head" && ref !== "base",
                                 commitFor: () => (ref === "base" ? tree?.base_sha : tree?.head_sha) ?? null });
  const lines = useMemo(() => (file?.text ?? "").replace(/\n$/, "").split("\n"), [file?.text]);
  const label = ref === "head" ? "this change" : ref === "base" ? cs.base_branch : ref.slice(0, 7);
  return (
    <section className="lr-code" aria-label="Code" data-testid="lr-code">
      <DockPane pane="tree" label="Files" icon={<FolderOpen size={15} aria-hidden />} testid="gr-dock-tree">
        <nav className="lr-tree gr-tree" aria-label="Files">
          <GoToFile paths={items.map((i) => i.path)} onPick={(p) => openFile(p, true)} />
          <FileTree files={items.map((i) => ({ path: i.path, name: i.path.split("/").pop() ?? i.path, status: i.status,
                                               additions: 0, deletions: 0, comments: i.comments, open: i.open_comments, changed: !!i.status }))}
                    current={path} onPick={(p) => openFile(p)} testid="lr-tree-item" reveal={reveal} width={layout.treeW} />
          {tree && !items.length ? <p className="cs-empty">No files.</p> : null}
        </nav>
      </DockPane>
      <div className="lr-file">
        <div className="lr-file-h">
          <FileCode size={14} aria-hidden /><span className="gr-crumb-w" data-testid="lr-file-path" data-path={path ?? ""}>
            {path ? <Breadcrumb path={path} onFolder={(f) => setReveal({ path: f, n: Date.now() })} /> : "—"}</span>
          <span className="lr-seg" role="radiogroup" aria-label="Which version of the file">
            <button type="button" role="radio" aria-checked={ref === "head"} onClick={() => setRef("head")} data-testid="lr-ref-head">
              This change · {(tree?.head_sha ?? cs.head_sha ?? "").slice(0, 7)}</button>
            <button type="button" role="radio" aria-checked={ref === "base"} onClick={() => setRef("base")} data-testid="lr-ref-base">
              {cs.base_branch} · {(tree?.base_sha ?? "").slice(0, 7)}</button>
          </span>
          {commits.length ? (
            <select className="gr-select" aria-label="The file at a commit" data-testid="lr-ref-commit"
                    value={ref !== "head" && ref !== "base" ? ref : ""} onChange={(e) => setRef(e.target.value || "head")}>
              <option value="">At a commit…</option>
              {commits.map((c) => <option key={c.sha} value={c.sha}>{c.sha.slice(0, 7)} · {c.message.slice(0, 48)}</option>)}
            </select>) : null}
          <span className="cs-spacer" />
          {file?.text != null ? <span className="cs-faint cs-small">{file.language} · {file.lines} lines</span> : null}
          {path ? <CopyButton value={path} label="Copy the path" /> : null}
          <FullScreenButton />
        </div>
        {err ? <p className="cs-error" role="alert">{err}</p> : null}
        {!file && !err && path ? <div className="cs-loading" role="status" aria-label="Loading the file"><i /><i /></div> : null}
        {file && file.text == null ? <p className="cs-empty" data-testid="lr-file-note">{file.message}</p> : null}
        {file && file.text != null ? (
          <div className="cs-diff lr-filebody" role="region" aria-label={`${path} at ${label}`} tabIndex={0}>
            <table><tbody>
              {lines.map((text, i) => {
                const a = { path: path!, line: i + 1, side };
                const line = toks?.[i];
                return (
                  <React.Fragment key={i}>
                    <tr className="cs-ln" data-c={review.has(a) || undefined}>
                      <td className="cs-no lr-gut">{review.gutter(a)}{i + 1}</td>
                      <td className="cs-code">{line ? line.map((t, ti) => (
                        <span key={ti} className="lr-tok" style={{ ["--tk-light" as string]: t.light, ["--tk-dark" as string]: t.dark ?? t.light } as React.CSSProperties}>{t.content}</span>))
                        : text}</td>
                    </tr>
                    {review.has(a) ? <tr className="cs-inline"><td colSpan={2}>{review.under(a)}</td></tr> : null}
                  </React.Fragment>
                );
              })}
            </tbody></table>
          </div>) : null}
      </div>
    </section>
  );
}

// ── Agent actions: the fix round's required inputs (the Checks tab) ──────────────────────────

export function AgentActions({ cs }: { cs: ChangeSet }) {
  const round = cs.run_id;
  const required = cs.comments.filter((t) => t.author_kind === "human" && t.run_round && t.run_round === round);
  if (!round || !required.length) return null;
  const answered = (t: Thread) => t.replies.find((r) => r.author_kind === "agent" && (r.run_round ?? "").startsWith(round));
  const done = required.filter(answered).length;
  const status = (cs.checks?.run_status ?? "").toLowerCase();
  const running = /run|queue|lease|progress|pend/.test(status);
  const stage = (k: string): string | undefined => {
    if (k === "read") return "done";
    if (k === "reply") return done === required.length ? "done" : running ? "run" : undefined;
    if (k === "fix" || k === "test") return done ? "done" : running ? "run" : undefined;
    return done === required.length ? "you" : undefined;
  };
  return (
    <section className="cs-card lr-agent" aria-label="Agent actions" data-testid="lr-agent-actions">
      <div className="lr-agent-h"><Robot size={18} aria-hidden />
        <div><b>Agent actions</b><small className="cs-faint">Fix round on {cs.branch} · started by Request changes</small></div>
        <span className="cs-spacer" />
        <span className="cs-pill" data-tone={running ? "amber" : "green"}>{running ? "working" : status || "done"}</span></div>
      <ol className="lr-steps lr-steps-5" aria-label="Steps">
        {[["read", "Read comments", `${required.length} from people`], ["fix", "Fix", "on the branch"], ["test", "Test", "in the sandbox"],
          ["reply", "Reply on each", "Fixed or Disputed"], ["you", "You resolve", "then merge"]].map(([k, label, sub]) => (
          <li key={k} data-s={stage(k)}><i aria-hidden /><b>{label}</b><small>{sub}</small></li>))}
      </ol>
      <ul className="lr-required" aria-label="Required inputs">
        {required.map((t) => {
          const a = answered(t);
          return (
            <li key={t.id}>
              <span className="cs-dot" data-tone={a ? "green" : running ? "amber" : undefined} aria-hidden />
              <span className="lr-rail-t"><span><b className="cs-mono">{t.path}{t.line ? `:${t.line}` : ""}</b> {t.body.split("\n")[0]}</span>
                <small className="cs-faint">{a ? a.body.split("\n")[0] : running ? "working on it" : "no reply yet"}</small></span>
              <span className="cs-pill" data-tone={a ? (t.state === "disputed" ? "amber" : "green") : undefined}>
                {a ? (t.state === "disputed" ? "disputed" : "fixed") : running ? "working" : "waiting"}</span>
            </li>);
        })}
      </ul>
      <p className="cs-faint cs-small">The round cannot ask you to merge until every required comment has a reply: Fixed with a commit, or Disputed with a reason.</p>
    </section>
  );
}

/** The Code tab's icon in the tab row. */
export const CodeIcon = Code;

/** Scroll to a thread on the page (the diff or the Code tab), when it is rendered. */
export function useJumpTo(): (id: number) => void {
  const last = useRef(0);
  return useCallback((id: number) => {
    last.current = id;
    window.setTimeout(() => {
      const el = document.querySelector(`[data-thread="${id}"]`);
      if (el) { el.scrollIntoView({ block: "center", behavior: "smooth" }); (el as HTMLElement).focus?.(); }
    }, 60);
  }, []);
}

export type { Where };
