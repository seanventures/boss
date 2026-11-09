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
 */
import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ArrowLeft, ArrowRight, CaretDown, CaretRight, ChatCircleText, CheckCircle, CircleNotch, FileCode,
  GitBranch, GitCommit, GitMerge, HardDrives, ShieldCheck, XCircle,
} from "@phosphor-icons/react";

import { AgentAvatar } from "../../../agent-avatar";
import { IdentityProvider, useGuardedCall, useIdentity } from "../../../../components/identity/identity-provider";
import type { MePermissionsResponse, MeResponse } from "../../../../lib/identity/contract";
import { idGet, idPost, refusalSentence } from "../../../../lib/identity/api";
import { changeSetPath, parseChangeSet, type ChangeSet, type ChangeSetNote } from "../../../../lib/repo-links";
import { commentsByLine, highlight, langOf, parseUnifiedDiff, type DiffFile } from "../../../../lib/diff-view";
import { Markdown } from "../../../markdown";
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
  const commentCount = (cs?.review ?? []).reduce((a, n) => a + n.comments.length + (n.body ? 1 : 0), 0);

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
          <nav className="cs-tabs" aria-label="Sections">
            <a href="#cs-files"><FileCode size={13} aria-hidden />Files <b>{cs.files.length || files.length}</b></a>
            <a href="#cs-commits"><GitCommit size={13} aria-hidden />Commits <b>{cs.commits.length}</b></a>
            <a href="#cs-review"><ChatCircleText size={13} aria-hidden />Review <b>{commentCount}</b></a>
            <a href="#cs-checks"><ShieldCheck size={13} aria-hidden />Checks</a>
          </nav>
          <div className="cs-grid">
            <div className="cs-main">
              {cs.body ? <Body text={cs.body} /> : null}
              <Files cs={cs} files={files} inline={inline} />
              <Commits cs={cs} />
              <Review notes={cs.review} />
            </div>
            <aside className="cs-side">
              <Decide cs={cs} projectId={projectId} number={number} reread={read} />
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
        {cs.pending_push ? <span className="cs-pill" data-tone="amber">Waiting for GitHub</span> : null}
        {cs.github_pr_url ? <a className="cs-link" href={cs.github_pr_url} target="_blank" rel="noreferrer">Published PR ↗</a> : null}
      </div>
    </header>
  );
}

// ── files and the diff ──────────────────────────────────────────────────────────────────────

function Files({ cs, files, inline }: { cs: ChangeSet; files: DiffFile[]; inline: Map<string, InlineComment[]> }) {
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
              {isOpen ? (r.diff && r.diff.hunks.length ? <DiffTable file={r.diff} inline={inline} />
                : <p className="cs-empty">{r.diff?.status === "binary" ? "A binary file." : "No lines to show."}</p>) : null}
            </div>
          );
        })}
      </div>
    </section>
  );
}

function DiffTable({ file, inline }: { file: DiffFile; inline: Map<string, InlineComment[]> }) {
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
                return (
                  <React.Fragment key={li}>
                    <tr className="cs-ln" data-k={l.kind}>
                      <td className="cs-no">{l.oldNo ?? ""}</td>
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

/** An agent's name or role key gets its face; a person's name gets initials. */
function looksLikeAgent(name: string): boolean {
  return !name || /^(agent:|boss\b|[a-z]+(_[a-z]+)+$)|captain|reviewer|panel|engineer|helm|marshal/i.test(name);
}

function Who({ name, size = 22 }: { name: string; size?: number }) {
  const label = name || "BOSS";
  return (
    <span className="cs-who">
      {looksLikeAgent(name) ? <AgentAvatar agent={name.replace(/^agent:/, "") || "engineering_captain"} size={size} aiBadge={false} />
        : <span className="cs-initials" style={{ width: size, height: size }} aria-hidden>{label.slice(0, 1).toUpperCase()}</span>}
      <b>{label.replace(/^agent:/, "").replace(/_/g, " ")}</b>
    </span>
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

export function Review({ notes }: { notes: ChangeSetNote[] }) {
  return (
    <section className="cs-card" id="cs-review" data-testid="change-set-review" aria-label="Review">
      <div className="cs-card-h"><ChatCircleText size={15} aria-hidden /><b>Review</b><span className="cs-faint">{notes.length}</span></div>
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
      )) : <p className="cs-empty">No review yet: the review panel comments here.</p>}
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
      {said ? <p className="cs-error" role="alert">{said}</p> : null}
      {d || cs.merge ? <a className="cs-link cs-small" href={`/?area=decisions&decision=${encodeURIComponent(cs.merge?.decision_id ?? d!.id)}`}>Open the decision ›</a> : null}
    </section>
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
