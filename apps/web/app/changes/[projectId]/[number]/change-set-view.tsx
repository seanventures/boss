"use client";

/**
 * THE CHANGE PAGE, A GIT-STYLE REVIEW (BOSSAPP-954; wireframe
 * docs/product/wireframes/agentic-factory/6-git-review.html). One page for a LOCAL change set
 * (BOSSAPP-919/948) and a GITHUB pull request of a BOSS project: only the source line differs.
 *
 *   header   two lines: title #n · state · branch → base · checks · open comments · Clone ·
 *            (Open on GitHub) · Request changes · Merge — then where it is (repository, sync)
 *   tabs     Conversation · Commits · Files changed · Code · Checks
 *   panes    full width: the tab (Files changed: tree | diff) and the PR agent on the right;
 *            under 1100px the agent is a button; on a phone the diff alone
 *
 * Data: GET …/local-changes/{n} or …/github-changes/{owner}/{repo}/{n} (lib/change-review.ts). A
 * local change later published to GitHub answers from its PR, and the page follows it.
 */
import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ArrowLeft, ArrowSquareOut, ChatCircleDots, ChatCircleText, CircleNotch, Code, FileCode, GitCommit, GitMerge,
  FolderOpen, GithubLogo, HardDrives, Info, PencilSimpleLine, Robot, ShieldCheck, Terminal,
} from "@phosphor-icons/react";

import { BoxPop } from "../../../../components/box-pop";
import { ConsentHost } from "../../../../components/identity/consent-sheet";
import { CanButton, IdentityProvider, useGuardedCall, useIdentity } from "../../../../components/identity/identity-provider";
import type { MePermissionsResponse, MeResponse } from "../../../../lib/identity/contract";
import { idGet, idPost, refusalSentence } from "../../../../lib/identity/api";
import { parseChangeSet, type ChangeSet } from "../../../../lib/repo-links";
import { commentsByLine, parseUnifiedDiff } from "../../../../lib/diff-view";
import {
  agoWords, changeBase, changedFiles, changeUrl, getCommits, getFiles, getViewed, readChangeUrl, WHOLE_CHANGE,
  type ChangeAt, type Commit, type FilesView, type Loc, type Range, type Viewed,
} from "../../../../lib/change-review";
import { OverviewTab } from "./overview-tab";
import { SyncPanel } from "./sync-panel";
import { fileAnchor, lineAnchor } from "./review-panes";
import { requestChanges, runReview, type Thread } from "../../../../lib/local-review";
import { CodeTab, useJumpTo, useLineReview } from "./local-review";
import {
  AgentPane, BranchChips, ChecksPill, ChecksTab, ClonePop, CommitsTab, ConversationTab, FilesTab, InlineNote, Review,
  StatePill,
} from "./review-panes";
import { DockPane, ReviewLayoutProvider, useReviewLayout } from "./review-dock";
import "./change-set.css";

export { InlineNote, Review };

export type Tab = "overview" | "conversation" | "commits" | "files" | "code" | "checks";
const TABS: Tab[] = ["overview", "conversation", "commits", "files", "code", "checks"];

export function ChangeSetView({ projectId, number, repo = null, tab = null, commit = null, loc = null, me = null, org = null, project = null }: {
  projectId: string; number: string;
  /** A GitHub PR of the project (`owner/repo`); none for a local change set. */
  repo?: string | null; tab?: string | null; commit?: string | null;
  /** ?file=…&line=…: the exact row of the diff this link names (BOSSAPP-995). */
  loc?: Loc | null;
  /** Read on the server (page.tsx): who is here and what they hold, at the org and this project. */
  me?: MeResponse | null; org?: MePermissionsResponse | null; project?: MePermissionsResponse | null;
}) {
  // The page sits outside the console shell: its own identity provider, so a merge that needs
  // "Confirm it's you" can ask for it here.
  return (
    <IdentityProvider me={me} org={org} project={project} projectId={projectId}>
      <ReviewLayoutProvider page="change">
        <ChangeSetPage projectId={projectId} number={number} repo={repo} startTab={TABS.includes(tab as Tab) ? (tab as Tab) : null}
                       startCommit={commit} startLoc={loc} />
      </ReviewLayoutProvider>
      <ConsentHost />
    </IdentityProvider>
  );
}

type Note = { path?: string; line?: number; body: string; author: string };

function ChangeSetPage({ projectId, number, repo, startTab, startCommit, startLoc = null }: {
  projectId: string; number: string; repo: string | null; startTab: Tab | null; startCommit: string | null; startLoc?: Loc | null;
}) {
  const asked: ChangeAt = useMemo(() => ({ projectId, number, repo }), [projectId, number, repo]);
  const [cs, setCs] = useState<ChangeSet | null>(null);
  const [error, setError] = useState<string | null>(null);
  // A local change published to GitHub answers from its PR: every later call goes there.
  const followed = cs && cs.source === "github" && !repo ? `${cs.repository}#${cs.number}` : "";
  const at: ChangeAt = useMemo(() => (followed
    ? { projectId, number: followed.split("#")[1], repo: followed.split("#")[0] } : asked), [followed, projectId, asked]);
  const { refresh } = useIdentity();
  useEffect(() => { void refresh(); }, [refresh]);
  const read = useCallback(async () => {
    const v = await idGet<unknown>(changeBase(asked));
    const parsed = parseChangeSet(v);
    setCs(parsed);
    return parsed;
  }, [asked]);
  useEffect(() => {
    let alive = true;
    read().catch((e: unknown) => { if (alive) setError(refusalSentence(e)); });
    return () => { alive = false; };
  }, [read]);
  // Refresh on view: a GitHub PR is read again when the person comes back to the tab.
  useEffect(() => {
    const again = () => { if (document.visibilityState === "visible") void read().catch(() => null); };
    document.addEventListener("visibilitychange", again);
    return () => document.removeEventListener("visibilitychange", again);
  }, [read]);

  const [commits, setCommits] = useState<Commit[]>([]);
  const loaded = !!cs;
  const headSha = cs?.head_sha ?? null;
  useEffect(() => {
    if (!loaded) return undefined;
    let alive = true;
    getCommits(at).then((c) => { if (alive) setCommits(c); }).catch(() => {
      if (alive && cs) setCommits(cs.commits.map((c) => ({ sha: c.sha, message: c.message, author: c.author ?? "", date: c.date ?? null, fixes: c.fixes ?? [],
                                                 additions: null, deletions: null, files: null })));
    });
    return () => { alive = false; };
  }, [at, loaded, headSha]); // eslint-disable-line react-hooks/exhaustive-deps
  const [viewed, setViewedState] = useState<Viewed | null>(null);
  useEffect(() => {
    if (!loaded) return undefined;
    let alive = true;
    getViewed(at).then((v) => { if (alive) setViewedState(v); }).catch(() => null);
    return () => { alive = false; };
  }, [at, loaded]);

  // EVERY TAB SWITCHES THE MAIN VIEW, AND THE URL KEEPS IT (?tab=…&commit=…): links and Back work.
  const [tab, setTabState] = useState<Tab>(startTab ?? (startCommit ? "commits" : "files"));
  const [shownCommit, setShownCommit] = useState<string | null>(startCommit);
  const go = useCallback((t: Tab, commit: string | null = null) => {
    setTabState(t); setShownCommit(t === "commits" ? commit : null);
    try { window.history.pushState({ tab: t, commit }, "", changeUrl(asked, t, t === "commits" ? commit : null)); } catch { /* the view still switches */ }
    window.scrollTo({ top: 0 });
  }, [asked]);
  const setTab = useCallback((t: Tab) => go(t, null), [go]);
  useEffect(() => {
    const back = () => {
      const r = readChangeUrl(window.location.pathname, window.location.search);
      setTabState(TABS.includes(r.tab as Tab) ? (r.tab as Tab) : "files");
      setShownCommit(r.commit);
      if (r.loc) setFocus({ path: r.loc.path, n: Date.now() });
    };
    window.addEventListener("popstate", back);
    return () => window.removeEventListener("popstate", back);
  }, []);
  const [range, setRange] = useState<Range>(WHOLE_CHANGE);
  // THE ONE SOURCE OF EVERY COUNT: the diff of the current base…head (the whole change, or the
  // range the person picked), read once here; the tab badge, tree, toolbar and summary read it.
  const whole = !range.base && !range.head;
  const [ranged, setRanged] = useState<FilesView | null>(null);
  const [rangeErr, setRangeErr] = useState<string | null>(null);
  useEffect(() => {
    if (whole) { setRanged(null); setRangeErr(null); return undefined; }
    let alive = true;
    setRanged(null); setRangeErr(null);
    getFiles(at, range.base, range.head).then((f) => { if (alive) setRanged(f); })
      .catch((e: unknown) => { if (alive) setRangeErr(refusalSentence(e)); });
    return () => { alive = false; };
  }, [at, whole, range.base, range.head]);
  const diffNow = whole ? cs?.diff ?? "" : ranged?.diff ?? "";
  const files = useMemo(() => changedFiles(whole ? cs?.files ?? [] : ranged?.files ?? [], parseUnifiedDiff(diffNow)),
                        [whole, cs?.files, ranged?.files, diffNow]);
  const wholeFiles = useMemo(() => changedFiles(cs?.files ?? [], parseUnifiedDiff(cs?.diff)), [cs?.files, cs?.diff]);
  const [codePath, setCodePath] = useState<string | null>(null);
  /** A comment clicked out of the diff: Files changed opens that file, scrolled to its line. */
  const [focus, setFocus] = useState<{ path: string; n: number } | null>(startLoc ? { path: startLoc.path, n: Date.now() } : null);
  const dock = useReviewLayout();
  const [clone, setClone] = useState(false);
  const [agentKey, setAgentKey] = useState(0);
  // ONE ORDER FOR EVERY UPDATE: the change first, then the agent pane. Both at once let the pane's
  // (faster) read say "resolved" while the thread and the header still said open.
  const reread = useCallback(() => { void read().catch(() => null).finally(() => setAgentKey((k) => k + 1)); }, [read]);
  const jump = useJumpTo();
  const inline = useMemo(() => commentsByLine<Note>((cs?.review ?? []).flatMap((n) =>
    n.comments.map((c) => ({ ...c, author: n.author })))), [cs?.review]);
  const lineReview = useLineReview({
    at, threads: cs?.comments ?? [], onChanged: reread, open: cs?.state === "open",
    commitFor: (side) => (side === "new" ? cs?.where?.head_sha || cs?.head_sha || null : cs?.base_sha ?? null),
  });
  const openCode = (path: string | null) => { setCodePath(path); go("code"); };
  /** JUMP TO A CODE PIECE (BOSSAPP-995): Files changed, that file open, scrolled to that row —
   *  and the URL carries ?file=&line=, so the place can be handed to someone as a link. */
  const scrollLoc = useCallback((path: string, line?: number | null, side: "new" | "old" = "new") => {
    window.setTimeout(() => {
      const row = line ? document.getElementById(lineAnchor(path, line, side)) : null;
      (row ?? document.getElementById(fileAnchor(path)))?.scrollIntoView({ block: "center", behavior: "smooth" });
      row?.setAttribute("data-hit", "");
      if (row) window.setTimeout(() => row.removeAttribute("data-hit"), 2400);
    }, 160);
  }, []);
  const onJumpLoc = useCallback((path: string, line?: number | null, side: "new" | "old" = "new") => {
    setRange(WHOLE_CHANGE); setFocus({ path, n: Date.now() }); setTabState("files"); setShownCommit(null);
    try { window.history.pushState({ tab: "files" }, "", changeUrl(asked, "files", null, { path, line, side })); } catch { /* the view still moves */ }
    scrollLoc(path, line, side);
  }, [asked, scrollLoc]);
  // A pasted ?file=&line= link: the page lands on that row once the diff is there.
  const landed = useRef(false);
  useEffect(() => {
    if (!startLoc || landed.current || !cs || tab !== "files" || !diffNow) return;
    landed.current = true;
    scrollLoc(startLoc.path, startLoc.line, startLoc.side ?? "new");
  }, [startLoc, cs, tab, diffNow, scrollLoc]);
  const onJump = (t: Thread) => {
    if (!t.path || t.outdated || t.kept) { setTab("conversation"); jump(t.id); return; }
    if (!(cs?.files ?? []).some((f) => f.path === t.path)) { openCode(t.path); jump(t.id); return; }
    setRange(WHOLE_CHANGE); setFocus({ path: t.path, n: Date.now() }); setTab("files"); window.setTimeout(() => jump(t.id), 120);
  };
  const jumpId = (id: number) => {
    const t = cs?.comments.find((x) => x.id === id);
    if (t) { onJump(t); if (dock.layout.agent === "overlay") dock.dispatch({ type: "close", pane: "agent" }); }
  };

  const projectHref = `/?project_id=${encodeURIComponent(projectId)}&area=project&tab=work`;
  const projectHome = `/?project_id=${encodeURIComponent(projectId)}&area=project`;
  const commentCount = (cs?.review ?? []).reduce((a, n) => a + n.comments.length + (n.body ? 1 : 0), 0) + (cs?.comments.length ?? 0);
  // The Overview badge: the threads someone still has to act on (open · addressed · disputed).
  const activeCount = (cs?.comments ?? []).filter((t) => t.state !== "resolved").length;
  const word = cs?.source === "github" ? "PR" : "Change";
  const checkCount = (cs?.checks?.items?.length ?? 0) + (cs?.checks?.run_status ? 2 : 0);

  return (
    <main className="cs gr" data-testid="change-set" data-source={cs?.source ?? (repo ? "github" : "local")}>
      <nav className="cs-crumb" aria-label="Breadcrumb">
        <a href={projectHref} data-testid="change-set-back"><ArrowLeft size={13} aria-hidden /> Back</a>
        <span className="cs-faint" aria-hidden>·</span>
        <a href={projectHome}>Project</a><span className="cs-faint" aria-hidden>›</span>
        <a href={projectHref}>Work</a><span className="cs-faint" aria-hidden>›</span>
        <span aria-current="page">{word} #{cs?.number || number}</span>
      </nav>
      {error ? <p className="cs-error" role="alert" data-testid="change-set-error">{error}</p> : null}
      {!cs && !error ? <div className="cs-loading" role="status" aria-label={`Loading change #${number}`}><i /><i /><i /></div> : null}
      {cs ? (
        <>
          <header className="cs-head gr-hd">
            <div className="gr-hd-1">
              <h1 data-testid="change-set-title">{cs.title || `${word} #${cs.number}`} <span className="cs-faint">#{cs.number}</span></h1>
              <StatePill cs={cs} />
              <BranchChips cs={cs} />
              <ChecksPill cs={cs} />
              {cs.comment_summary.human_open ? (
                <span className="cs-pill" data-tone="amber" title="Open line comments from people" data-testid="change-set-open-comments">
                  <ChatCircleDots size={12} aria-hidden />{cs.comment_summary.human_open} open</span>) : null}
              <span className="cs-spacer" />
              <span className="gr-hd-acts">
                <span className="gr-anchor">
                  <button type="button" className="lr-btn" data-kind="ghost" aria-expanded={clone} onClick={() => setClone((v) => !v)} data-testid="gr-clone">
                    <Terminal size={14} aria-hidden />Clone</button>
                  {clone ? <ClonePop cs={cs} projectId={projectId} onClose={() => setClone(false)} /> : null}
                </span>
                {cs.github_pr_url ? <a className="lr-btn" data-kind="ghost" href={cs.github_pr_url} target="_blank" rel="noreferrer" data-testid="gr-open-github">
                  <GithubLogo size={14} aria-hidden />Open on GitHub<ArrowSquareOut size={11} aria-hidden /></a> : null}
                {/* A tablet or a phone: the panes are drawers/sheets, opened from here. */}
                {dock.eff.tree !== "docked" && (tab === "files" || tab === "code" || tab === "commits") ? (
                  <button type="button" className="lr-btn gr-sheet-btn" data-kind="ghost" aria-expanded={dock.layout.tree === "overlay"}
                          onClick={() => dock.dispatch(dock.layout.tree === "overlay" ? { type: "close", pane: "tree" } : { type: "open", pane: "tree" })}
                          data-testid="gr-tree-toggle"><FolderOpen size={14} aria-hidden />Files</button>) : null}
                {dock.eff.agent !== "docked" ? (
                  <button type="button" className="lr-btn gr-sheet-btn" data-kind="ghost" aria-expanded={dock.layout.agent === "overlay"}
                          onClick={() => dock.dispatch(dock.layout.agent === "overlay" ? { type: "close", pane: "agent" } : { type: "open", pane: "agent" })}
                          data-testid="gr-agent-toggle"><Robot size={14} aria-hidden />Agent</button>) : null}
                <Decide cs={cs} projectId={projectId} number={String(cs.number)} at={at} reread={read} compact />
              </span>
            </div>
            <SourceLine cs={cs} />
            {/* Pull/sync, restack, the conflicting files, History (BOSSAPP-996): the git the
                page can run itself, each action a recorded decision. */}
            <SyncPanel cs={cs} at={at} reread={read} />
          </header>
          <nav className="cs-tabs gr-tabs" aria-label="Sections" role="tablist">
            <TabLink tab="overview" on={tab} set={setTab} icon={<Info size={13} aria-hidden />} label="Overview" n={activeCount || undefined} />
            <TabLink tab="conversation" on={tab} set={setTab} icon={<ChatCircleText size={13} aria-hidden />} label="Conversation" n={commentCount} />
            <TabLink tab="commits" on={tab} set={setTab} icon={<GitCommit size={13} aria-hidden />} label="Commits" n={commits.length || cs.commits.length} />
            <TabLink tab="files" on={tab} set={setTab} icon={<FileCode size={13} aria-hidden />} label="Files changed" n={files.length} />
            <TabLink tab="code" on={tab} set={setTab} icon={<Code size={13} aria-hidden />} label="Code" />
            <TabLink tab="checks" on={tab} set={setTab} icon={<ShieldCheck size={13} aria-hidden />} label="Checks" n={checkCount || undefined} />
          </nav>
          <div className="gr-body">
            <div className="gr-tab" data-tab={tab}>
              {tab === "files" ? (
                <FilesTab cs={cs} at={at} commits={commits} files={files} diff={diffNow} loading={!whole && !ranged && !rangeErr} focus={focus}
                          error={rangeErr} review={lineReview} inline={inline} viewed={viewed}
                          onViewed={setViewedState} onViewFile={openCode} range={range} setRange={setRange} />
              ) : tab === "code" ? (
                <div id="lr-code"><CodeTab cs={cs} at={at} startPath={codePath} onChanged={reread} commits={commits} /></div>
              ) : tab === "commits" ? (
                <CommitsTab cs={cs} at={at} commits={commits} shown={shownCommit} onShow={(sha) => go("commits", sha)}
                            hrefFor={(sha) => changeUrl(asked, "commits", sha)} wholeFiles={wholeFiles} wholeDiff={cs.diff}
                            review={lineReview} inline={inline} onCompare={(r) => { setRange(r); setTab("files"); }} onViewFile={openCode} />
              ) : tab === "overview" ? (
                <OverviewTab cs={cs} at={at} files={wholeFiles} commitHref={(sha) => changeUrl(asked, "commits", sha)} onChanged={reread}
                             onJump={onJump} onJumpLoc={onJumpLoc} onFiles={() => { setRange(WHOLE_CHANGE); setTab("files"); }} />
              ) : tab === "conversation" ? (
                <ConversationTab cs={cs} at={at} files={wholeFiles} commitHref={(sha) => changeUrl(asked, "commits", sha)} onChanged={reread} onJump={onJump} onFiles={() => { setRange(WHOLE_CHANGE); setTab("files"); }} />
              ) : (
                <ChecksTab cs={cs} decision={<DecisionCard cs={cs} at={at} reread={read} />} />
              )}
            </div>
            <DockPane pane="agent" label="Agent" icon={<Robot size={15} aria-hidden />} testid="gr-dock-agent">
              <AgentPane cs={cs} at={at} onJump={jumpId} commitHref={(sha) => changeUrl(asked, "commits", sha)} onChanged={reread} refreshKey={agentKey} />
            </DockPane>
          </div>
        </>
      ) : null}
    </main>
  );
}

function TabLink({ tab, on, set, icon, label, n }: { tab: Tab; on: Tab; set: (t: Tab) => void; icon: React.ReactNode; label: string; n?: number }) {
  return (
    <a href={`#${tab}`} role="tab" aria-selected={on === tab} aria-current={on === tab ? "true" : undefined}
       data-testid={`change-set-tab-${tab}`} onClick={(e) => { e.preventDefault(); set(tab); }}>
      {icon}{label}{n != null ? <b>{n}</b> : null}</a>
  );
}

/** The header's second line: where the change is, and — for GitHub — how fresh the copy is. */
function SourceLine({ cs }: { cs: ChangeSet }) {
  const [, setTick] = useState(0);
  useEffect(() => { const t = window.setInterval(() => setTick((n) => n + 1), 15000); return () => window.clearInterval(t); }, []);
  if (cs.source === "github") {
    const at = cs.fetched_at ? new Date(cs.fetched_at).getTime() : NaN;
    const ago = Number.isFinite(at) ? agoWords(Date.now() - at) : "";
    return (
      <div className="gr-where" data-testid="gr-source">
        <span><GithubLogo size={12} aria-hidden /><code>{cs.repository}</code></span>
        {cs.stale ? <span className="gr-stale" data-testid="gr-stale" title="GitHub did not answer: this is the last copy BOSS has">
          Stale · GitHub did not answer{ago ? ` · copy from ${ago}` : ""}</span>
          : <span data-testid="gr-sync">In sync{ago ? ` · ${ago}` : ""}</span>}
        {cs.queued ? <span className="gr-stale">{cs.queued} comment{cs.queued === 1 ? "" : "s"} waiting for GitHub</span> : null}
        {cs.labels.includes("pushed") ? <span className="cs-tag">pushed</span> : null}
      </div>
    );
  }
  const ahead = cs.where?.ahead;
  return (
    <div className="gr-where" data-testid="gr-source">
      <span title="A repository BOSS keeps on this machine"><HardDrives size={12} aria-hidden /><code>{cs.repository.split("/").pop()}</code>
        <span className="cs-tag">Local repo</span></span>
      {ahead != null ? <span>{ahead} commit{ahead === 1 ? "" : "s"} ahead of {cs.base_branch}</span> : null}
      {cs.labels.includes("pushed") ? <span className="cs-tag" title="A person pushed this branch to BOSS">pushed</span> : null}
      {cs.pending_push ? <span className="cs-pill" data-tone="amber">Waiting for GitHub</span> : null}
      {cs.stale ? <span className="gr-stale" data-testid="gr-stale">Published to GitHub · GitHub did not answer, showing the local record</span> : null}
    </div>
  );
}

// ── the decision, Merge and Request changes ─────────────────────────────────────────────────

/** Merge and Request changes. `compact`: the header's buttons; the Checks tab shows the
 *  decision's card (DecisionCard). */
export function Decide({ cs, projectId, number, reread, at, compact = false }: {
  cs: ChangeSet; projectId: string; number: string; reread: () => Promise<ChangeSet>; at?: ChangeAt; compact?: boolean;
}) {
  const where: ChangeAt = at ?? { projectId, number };
  const guarded = useGuardedCall();
  const [busy, setBusy] = useState<"merge" | "merging" | null>(null);
  const [said, setSaid] = useState<string | null>(null);
  const alive = useRef(true);
  useEffect(() => () => { alive.current = false; }, []);
  const merge = async () => {
    if (busy) return;
    setBusy("merge"); setSaid(null);
    try {
      await guarded(`Merge ${cs.source === "github" ? "PR" : "change"} #${cs.number || number} into ${cs.base_branch}.`, () =>
        idPost(`${changeBase(where)}/merge`, {}));
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
  const merging = busy === "merging" || (cs.state === "open" && d?.status === "approved");
  useEffect(() => {
    if (!merging || cs.state !== "open") return undefined;
    const t = window.setInterval(() => { void reread().catch(() => null); }, 3000);
    return () => window.clearInterval(t);
  }, [merging, cs.state, reread]);
  useEffect(() => { if (cs.state !== "open") setBusy(null); }, [cs.state]);
  const button = cs.state === "merged" ? (
    <span className="cs-pill" data-state="merged" data-testid="change-set-merged"><GitMerge size={12} weight="bold" aria-hidden />
      Merged{cs.merged_by ? ` by ${cs.merged_by}` : ""}{cs.merged_sha ? ` as ${cs.merged_sha.slice(0, 7)}` : ""}</span>
  ) : merging ? (
    <span className="cs-merging" role="status" data-testid="change-set-merging"><CircleNotch size={14} className="cs-spin" aria-hidden /> BOSS is merging…</span>
  ) : cs.merge ? (
    <button type="button" className="cs-primary" data-testid="change-set-merge" disabled={!!busy || !!cs.merge.held} aria-busy={!!busy || undefined}
            title={cs.merge.held?.reason || "A person merges · BOSS runs the governed merge"} data-held={cs.merge.held?.state || undefined}
            onClick={() => void merge()}>
      {busy ? <CircleNotch size={15} className="cs-spin" aria-hidden /> : <GitMerge size={15} weight="bold" aria-hidden />}
      {cs.merge.held ? cs.merge.held.label : "Merge"}
    </button>
  ) : null;
  if (compact) {
    return (
      <span className="gr-decide">
        {cs.state === "open" && !merging ? <RequestChanges cs={cs} at={where} reread={reread} compact /> : null}
        {button}
        {said ? <span className="cs-error" role="alert">{said}</span> : null}
      </span>
    );
  }
  return (
    <section className="cs-card cs-decide" data-testid="change-set-decision" aria-label="Decision">
      <div className="cs-card-h"><GitMerge size={15} aria-hidden /><b>Decision</b><span className="cs-spacer" />
        {d ? <span className="cs-pill" data-tone={d.status === "approved" ? "green" : d.status === "pending" ? "amber" : "grey"}>{d.status}</span> : null}</div>
      {d?.question ? <p className="cs-q">{d.question}</p> : null}
      {button}
      {cs.merge ? <p className="cs-faint cs-small" data-testid={cs.merge.held ? "change-set-held" : undefined}>
        {cs.merge.held ? cs.merge.held.reason : "A person merges · BOSS runs the governed merge"}</p>
        : cs.state === "open" && !merging ? <p className="cs-faint cs-small">Merge opens once the review asks for a decision.</p> : null}
      {cs.state === "open" && !merging ? <RequestChanges cs={cs} at={where} reread={reread} /> : null}
      {said ? <p className="cs-error" role="alert">{said}</p> : null}
      {d || cs.merge ? <a className="cs-link cs-small" href={`/?area=decisions&decision=${encodeURIComponent(cs.merge?.decision_id ?? d!.id)}`}>Open the decision ›</a> : null}
    </section>
  );
}

/** The Checks tab's decision card: what it asks, why Merge waits, Run the review — no second
 *  Merge button (the header has it). */
function DecisionCard({ cs, at, reread }: { cs: ChangeSet; at: ChangeAt; reread: () => Promise<ChangeSet> }) {
  const d = cs.decision;
  return (
    <section className="cs-card cs-decide" data-testid="change-set-decision-card" aria-label="Decision">
      <div className="cs-card-h"><GitMerge size={15} aria-hidden /><b>Decision</b><span className="cs-spacer" />
        {d ? <span className="cs-pill" data-tone={d.status === "approved" ? "green" : d.status === "pending" ? "amber" : "grey"}>{d.status}</span> : null}</div>
      {d?.question ? <p className="cs-q">{d.question}</p> : null}
      {cs.state === "merged" ? <p className="cs-merged"><GitMerge size={15} aria-hidden />
        <span>Merged{cs.merged_by ? <> by <b>{cs.merged_by}</b></> : null}{cs.merged_sha ? <> as <code className="cs-chip">{cs.merged_sha.slice(0, 7)}</code></> : null}</span></p>
        : cs.merge ? <p className="cs-faint cs-small" data-testid={cs.merge.held ? "change-set-held" : undefined}>
          {cs.merge.held ? cs.merge.held.reason : "A person merges (the Merge button above) · BOSS runs the governed merge"}</p>
        : cs.state === "open" ? <p className="cs-faint cs-small">Merge opens once the review asks for a decision.</p> : null}
      {cs.state === "open" && !d && cs.labels.includes("pushed") && cs.source === "local" ? <RunReview cs={cs} at={at} reread={reread} /> : null}
      {d || cs.merge ? <a className="cs-link cs-small" href={`/?area=decisions&decision=${encodeURIComponent(cs.merge?.decision_id ?? d!.id)}`}>Open the decision ›</a> : null}
    </section>
  );
}

/** REQUEST CHANGES (BOSSAPP-948): a person sends the review back; the fix run starts on this
 *  branch with their open line comments as required inputs, a note optional. */
function RequestChanges({ cs, at, reread, compact = false }: { cs: ChangeSet; at: ChangeAt; reread: () => Promise<ChangeSet>; compact?: boolean }) {
  const [open, setOpen] = useState(false);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [said, setSaid] = useState<string | null>(null);
  const [done, setDone] = useState<string | null>(null);
  const n = cs.comments.filter((t) => t.author_kind === "human" && t.state !== "resolved" && !t.queued && !t.kept).length;
  const send = async () => {
    if (busy || (!note.trim() && !n)) return;
    setBusy(true); setSaid(null);
    try {
      await requestChanges(at, note.trim());
      setDone("The fix run started on this branch, your comments first.");
      setOpen(false); setNote("");
      await reread().catch(() => null);
    } catch (e) { setSaid(refusalSentence(e)); } finally { setBusy(false); }
  };
  return (
    <span className={compact ? "gr-anchor" : "lr-rc"}>
      <CanButton permission="decision:approve" projectId={at.projectId} className="lr-btn" data-kind="amber" aria-expanded={open}
                 data-testid="change-set-request-changes" tip={done ?? "Start a fix run on this branch"} onClick={() => setOpen((v) => !v)}>
        <PencilSimpleLine size={14} aria-hidden />Request changes{n ? ` · ${n}` : ""}</CanButton>
      {!compact ? <p className="cs-faint cs-small">{done ?? "The builder starts a fix run on this branch, your comments first"}</p> : null}
      {open ? (
        <BoxPop className="lr-composer gr-pop" fit={false} onEscape={() => setOpen(false)} data-testid="lr-request-changes">
          <textarea className="lr-input" rows={3} value={note} onChange={(e) => setNote(e.target.value)} aria-label="What to change"
                    placeholder={n ? "Anything to add to your line comments (optional)" : "Say what to change"} data-testid="lr-rc-note"
                    onKeyDown={(e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); void send(); } }} />
          <div className="lr-composer-f"><span className="cs-faint cs-small">{n ? `${n} line comment${n === 1 ? "" : "s"} go${n === 1 ? "es" : ""} as required inputs` : "No line comments yet"}</span>
            <span className="cs-spacer" />
            <button type="button" className="lr-btn" data-kind="ghost" onClick={() => setOpen(false)}>Cancel</button>
            <button type="button" className="lr-btn" data-kind="primary" disabled={busy || (!note.trim() && !n)} onClick={() => void send()} data-testid="lr-rc-send">
              {busy ? <CircleNotch size={13} className="cs-spin" aria-hidden /> : null}Start the fix run</button></div>
          {said ? <p className="cs-error" role="alert">{said}</p> : null}
        </BoxPop>) : null}
      {said && !open ? <span className="cs-error" role="alert">{said}</span> : null}
    </span>
  );
}

/** A pushed change whose review did not start with the push (consent, budget): start it now. */
function RunReview({ cs, at, reread }: { cs: ChangeSet; at: ChangeAt; reread: () => Promise<ChangeSet> }) {
  const [busy, setBusy] = useState(false);
  const [said, setSaid] = useState<string | null>(null);
  const running = !!cs.checks && /run|queue|lease|progress|pend/i.test(cs.checks.run_status ?? "");
  if (running) return <p className="cs-faint cs-small" data-testid="change-set-reviewing"><CircleNotch size={12} className="cs-spin" aria-hidden /> The review panel is checking this change</p>;
  return (
    <div className="lr-rc">
      <CanButton permission="code_task:create" projectId={at.projectId} className="lr-btn" data-kind="ghost" disabled={busy} data-testid="change-set-run-review"
                 tip="Run the sandbox checks and the review panel on this branch"
                 onClick={() => { setBusy(true); setSaid(null); void runReview(at).then(() => reread()).catch((e: unknown) => setSaid(refusalSentence(e))).finally(() => setBusy(false)); }}>
        <ShieldCheck size={14} aria-hidden />Run the review</CanButton>
      {said ? <p className="cs-error" role="alert">{said}</p> : null}
    </div>
  );
}
