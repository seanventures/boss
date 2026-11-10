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
  FolderOpen, GitBranch, LinkSimple, MagnifyingGlass, Plus, Robot, Terminal, X,
} from "@phosphor-icons/react";

import { AgentAvatar } from "../../../agent-avatar";
import { BoxPop } from "../../../../components/box-pop";
import { CanButton } from "../../../../components/identity/identity-provider";
import { refusalSentence } from "../../../../lib/identity/api";
import {
  addComment, cloneCommand, cloneUrl, colourise, getFile, getTree, replyTo, resolveThread,
  type FileView, type Thread, type Tok, type Tree, type Where,
} from "../../../../lib/local-review";
import type { ChangeSet } from "../../../../lib/repo-links";
import { Markdown } from "../../../markdown";

// ── people and agents ─────────────────────────────────────────────────────────────────────────

/** An agent's name or role key gets its face; a person's name gets initials. */
export function looksLikeAgent(name: string): boolean {
  return !name || /^(agent:|boss\b|[a-z]+(_[a-z]+)+$)|captain|reviewer|panel|engineer|helm|marshal|builder|forge/i.test(name);
}

export function Who({ name, size = 22, agent }: { name: string; size?: number; agent?: boolean }) {
  const label = name || "BOSS";
  const isAgent = agent ?? looksLikeAgent(name);
  return (
    <span className="cs-who">
      {isAgent ? <AgentAvatar agent={name.replace(/^agent:/, "") || "engineering_captain"} size={size} aiBadge={false} />
        : <span className="cs-initials" style={{ width: size, height: size }} aria-hidden>{label.slice(0, 1).toUpperCase()}</span>}
      <b>{label.replace(/^agent:/, "").replace(/_/g, " ")}</b>
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
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const today = new Date();
  return d.toDateString() === today.toDateString()
    ? d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
    : d.toLocaleDateString([], { day: "numeric", month: "short" });
}

const ORIGIN_WORDS: Record<string, string> = { git_notes: "from git", cli: "from the terminal", mcp: "from MCP" };

export function ThreadView({ thread, projectId, number, onChanged, compact = false }: {
  thread: Thread; projectId: string; number: string | number; onChanged: () => void; compact?: boolean;
}) {
  const [replying, setReplying] = useState(false);
  const [busy, setBusy] = useState(false);
  const [said, setSaid] = useState<string | null>(null);
  const act = async (fn: () => Promise<unknown>) => {
    setBusy(true); setSaid(null);
    try { await fn(); setReplying(false); onChanged(); } catch (e) { setSaid(refusalSentence(e)); } finally { setBusy(false); }
  };
  const resolved = thread.state === "resolved";
  return (
    <div className="lr-thread" data-s={thread.state} data-testid="lr-thread" data-thread={thread.id}>
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
        <span className="cs-spacer" />
        {!resolved ? <CanButton permission="code_task:steer" projectId={projectId} className="lr-link" onClick={() => setReplying(true)}
                                data-testid="lr-reply">Reply</CanButton> : null}
        {resolved
          ? <CanButton permission="code_task:steer" projectId={projectId} className="lr-link" disabled={busy}
                       onClick={() => void act(() => resolveThread(projectId, number, thread.id, true))} data-testid="lr-reopen">
              <ArrowCounterClockwise size={12} aria-hidden />Reopen</CanButton>
          : <CanButton permission="code_task:steer" projectId={projectId} className="lr-link" disabled={busy}
                       onClick={() => void act(() => resolveThread(projectId, number, thread.id))} data-testid="lr-resolve">
              <Check size={12} aria-hidden />Resolve</CanButton>}
      </div>
      {replying ? <Composer reply busy={busy} error={said} onCancel={() => setReplying(false)}
                            onSubmit={(body) => void act(() => replyTo(projectId, number, thread.id, body))} /> : null}
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

export function useLineReview({ projectId, number, threads, commitFor, onChanged, open = true }: {
  projectId: string; number: string | number; threads: Thread[];
  /** The commit a new comment on this side is anchored to (the head, or the base). */
  commitFor: (side: "new" | "old") => string | null; onChanged: () => void; open?: boolean;
}): LineReview {
  const [composing, setComposing] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [said, setSaid] = useState<string | null>(null);
  const byLine = useMemo(() => {
    const m = new Map<string, Thread[]>();
    for (const t of threads) {
      if (!t.path || t.line == null || t.outdated) continue;
      const k = keyOf({ path: t.path, line: t.line, side: t.side });
      m.set(k, [...(m.get(k) ?? []), t]);
    }
    return m;
  }, [threads]);
  const submit = useCallback(async (a: Anchor, body: string, severity: string) => {
    setBusy(true); setSaid(null);
    try {
      await addComment(projectId, number, { body, severity, path: a.path, line: a.line, side: a.side, commit_sha: commitFor(a.side) });
      setComposing(null); onChanged();
    } catch (e) { setSaid(refusalSentence(e)); } finally { setBusy(false); }
  }, [projectId, number, commitFor, onChanged]);
  return {
    has: (a) => byLine.has(keyOf(a)) || composing === keyOf(a),
    gutter: (a) => !open ? null : (
      <CanButton permission="code_task:steer" projectId={projectId} className="lr-plus" aria-label={`Comment on ${a.path} line ${a.line}`}
                 tip="Comment on this line" onClick={() => { setComposing(keyOf(a)); setSaid(null); }} data-testid="lr-plus">
        <Plus size={11} weight="bold" aria-hidden /></CanButton>),
    under: (a) => {
      const list = byLine.get(keyOf(a)) ?? [];
      const writing = composing === keyOf(a);
      if (!list.length && !writing) return null;
      return (
        <div className="lr-under">
          {list.map((t) => <ThreadView key={t.id} thread={t} projectId={projectId} number={number} onChanged={onChanged} />)}
          {writing ? <Composer where={`${a.path}:${a.line} · ${a.side === "old" ? "old side" : "new side"}`} busy={busy} error={said}
                               onCancel={() => setComposing(null)} onSubmit={(b, s) => void submit(a, b, s)} /> : null}
        </div>
      );
    },
  };
}

// ── (b) the Code tab ──────────────────────────────────────────────────────────────────────────

export function CodeTab({ cs, projectId, number, startPath, onChanged }: {
  cs: ChangeSet; projectId: string; number: string; startPath: string | null; onChanged: () => void;
}) {
  const [ref, setRef] = useState<"head" | "base">("head");
  const [tree, setTree] = useState<Tree | null>(null);
  const [path, setPath] = useState<string | null>(startPath);
  const [file, setFile] = useState<FileView | null>(null);
  const [toks, setToks] = useState<Tok[][] | null>(null);
  const [q, setQ] = useState("");
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => { if (startPath) setPath(startPath); }, [startPath]);
  useEffect(() => {
    let alive = true;
    getTree(projectId, number, ref).then((t) => {
      if (!alive) return;
      setTree(t);
      setPath((p) => p ?? (t.items.find((i) => i.status && i.status !== "D")?.path ?? t.items[0]?.path ?? null));
    }).catch((e: unknown) => { if (alive) setErr(refusalSentence(e)); });
    return () => { alive = false; };
  }, [projectId, number, ref, cs.comment_summary.threads, cs.head_sha]);
  useEffect(() => {
    if (!path) return undefined;
    let alive = true;
    setFile(null); setToks(null); setErr(null);
    getFile(projectId, number, path, ref).then(async (f) => {
      if (!alive) return;
      setFile(f);
      if (f.text) { const t = await colourise(f.text, f.language); if (alive) setToks(t); }
    }).catch((e: unknown) => { if (alive) setErr(refusalSentence(e)); });
    return () => { alive = false; };
  }, [projectId, number, path, ref]);
  const items = useMemo(() => (tree?.items ?? []).filter((i) => !q || i.path.toLowerCase().includes(q.toLowerCase())), [tree, q]);
  const threads = useMemo(() => cs.comments.filter((t) => t.path === path && (ref === "head" ? t.side === "new" : t.side === "old")), [cs.comments, path, ref]);
  const review = useLineReview({ projectId, number, threads, onChanged, open: cs.state === "open",
                                 commitFor: () => (ref === "head" ? tree?.head_sha : tree?.base_sha) ?? null });
  const side: "new" | "old" = ref === "head" ? "new" : "old";
  const lines = useMemo(() => (file?.text ?? "").replace(/\n$/, "").split("\n"), [file?.text]);
  return (
    <section className="cs-card lr-code" aria-label="Code" data-testid="lr-code">
      <nav className="lr-tree" aria-label="Files">
        <label className="lr-goto"><MagnifyingGlass size={13} aria-hidden />
          <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Go to file" aria-label="Go to file" data-testid="lr-goto" /></label>
        <ul>
          {items.map((i) => (
            <li key={i.path}>
              <button type="button" aria-current={i.path === path ? "true" : undefined} title={i.path}
                      onClick={() => setPath(i.path)} data-testid="lr-tree-item" data-path={i.path}>
                <span className="cs-ell">{i.path}</span>
                {i.comments ? <span className="lr-count" title={`${i.comments} comment thread${i.comments === 1 ? "" : "s"}`}>{i.comments}</span> : null}
                {i.status ? <span className="lr-st" data-s={i.status}>{i.status}</span> : null}
              </button>
            </li>))}
          {tree && !items.length ? <li className="cs-empty">No file matches.</li> : null}
        </ul>
      </nav>
      <div className="lr-file">
        <div className="lr-file-h">
          <FileCode size={14} aria-hidden /><b className="cs-ell cs-mono" data-testid="lr-file-path">{path ?? "—"}</b>
          <span className="lr-seg" role="radiogroup" aria-label="Which version of the file">
            <button type="button" role="radio" aria-checked={ref === "head"} onClick={() => setRef("head")} data-testid="lr-ref-head">
              This change · {(tree?.head_sha ?? cs.head_sha ?? "").slice(0, 7)}</button>
            <button type="button" role="radio" aria-checked={ref === "base"} onClick={() => setRef("base")} data-testid="lr-ref-base">
              {cs.base_branch} · {(tree?.base_sha ?? "").slice(0, 7)}</button>
          </span>
          <span className="cs-spacer" />
          {file?.text != null ? <span className="cs-faint cs-small">{file.language} · {file.lines} lines</span> : null}
          {path ? <CopyButton value={path} label="Copy the path" /> : null}
        </div>
        {err ? <p className="cs-error" role="alert">{err}</p> : null}
        {!file && !err && path ? <div className="cs-loading" role="status" aria-label="Loading the file"><i /><i /></div> : null}
        {file && file.text == null ? <p className="cs-empty" data-testid="lr-file-note">{file.message}</p> : null}
        {file && file.text != null ? (
          <div className="cs-diff lr-filebody" role="region" aria-label={`${path} at ${ref === "head" ? "this change" : cs.base_branch}`} tabIndex={0}>
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

// ── the review rail and (d) Agent actions ─────────────────────────────────────────────────────

export function ReviewRail({ cs, onJump }: { cs: ChangeSet; onJump: (t: Thread) => void }) {
  const open = cs.comment_summary.human_open;
  const people = cs.comments.filter((t) => t.author_kind === "human");
  const panel = cs.review.reduce((a, n) => a + n.comments.length, 0);
  return (
    <section className="cs-card" aria-label="Review" data-testid="lr-rail">
      <div className="cs-card-h"><ChatCircleDots size={15} aria-hidden /><b>Review</b><span className="cs-spacer" />
        {open ? <span className="cs-pill" data-tone="amber" data-testid="lr-open-count">{open} open</span>
          : people.length ? <span className="cs-pill" data-tone="green">all resolved</span> : null}</div>
      {people.length ? (
        <ul className="lr-rail">
          {people.map((t) => (
            <li key={t.id} data-s={t.state}>
              <button type="button" onClick={() => onJump(t)}>
                {t.state === "resolved" ? <CheckCircle size={13} weight="fill" className="lr-ok" aria-hidden />
                  : <ChatCircleDots size={13} className="lr-warn" aria-hidden />}
                <span className="lr-rail-t"><span className="cs-ell">{t.body.split("\n")[0]}</span>
                  <small className="cs-faint">{t.path ? `${t.path}${t.line ? `:${t.line}` : ""} · ` : ""}{t.author} · {t.state === "resolved" ? "resolved" : t.severity_words}</small></span>
              </button>
            </li>))}
        </ul>
      ) : <p className="cs-empty">No comments from people yet. Press + on a line to leave one.</p>}
      {panel ? <p className="cs-faint cs-small">{panel} review-panel note{panel === 1 ? "" : "s"} · after yours</p> : null}
    </section>
  );
}

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
