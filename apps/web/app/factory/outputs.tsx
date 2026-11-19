"use client";

/**
 * Outputs (factory spec §4.5.5): the Brief, then eight rows, one line each, label, the server's
 * word, the face of the agent who made it, expanding in place, one at a time. The Brief is what
 * the build was asked and how it was built: the old per-build page's contract and meta rows,
 * which the redesign had dropped (parity audit). Every collapsed value is the
 * server's `word`; nothing here counts tests (only commands are real) or draws a diff (hunks are
 * not served: `View diff ↗` goes to the PR).
 */
import { changeHref, changeSubHref, isLocalChangeUrl, isLocalRepo, linkTarget, reviewChangeWords } from "../../lib/repo-links";
import { latestPerStep } from "../../lib/model-routes";
import { WhyThisModel } from "../why-this-model";
import { CaretDown, CaretRight, Check, Minus, X } from "@phosphor-icons/react";
import { plural } from "../../lib/factory-floor";
import React, { useEffect, useState } from "react";

import type { CodeTaskRun, PreviewRec } from "@boss/api-client";
import type { Cockpit, CockpitOutputs, PreviewOutput } from "../../lib/cockpit";
import { previewWebHref } from "../../lib/browser-links";
import { isGitHubUrl, repoUrl } from "../../lib/github-urls";
import { callAction } from "../../lib/call-action";
import { GatedButton } from "../../components/gated";
import { followUpCodeTaskAction, startPreviewAction } from "../actions";
import { commentsWord, fixFindingsInstructions, foldComments, type FoldedComment } from "../../lib/review-comments";
import { showToast } from "../client-forms";
import { notifyChanged } from "../changed";
import { DeploymentLine, QaBenchmarkEvidence } from "./legacy-build-parts";
import { ApprovePreview } from "../previews-ui";
import { WaiveSheet } from "./waive-sheet";
import { AgFaceStack } from "../agents/agent-face";
import { nameOf, RunFace } from "../agents/run-face";
import { fetchCalls, recheckChange, recheckPreview, resolveComment, reviewPrAgain, stopPreview, type ModelCall, type RecheckPreview } from "./cockpit-actions";
import { areaHref, costWord, kilo, money, secondsWord, whoName } from "./cockpit-util";
import { localWith } from "../../lib/local-time";
import { useIdentityVersion } from "../../lib/use-agent-identities";
import { missingWord } from "../../lib/build-detail";
import { failedTestName } from "../../lib/failed-test-name";
import { nowInput } from "./now-band";
import { WorkPreviewButton, buildPreviewApp } from "../projects/work-preview";

export type OutputKey = "brief" | "pr" | "checks" | "tests" | "changes" | "comments" | "preview" | "deploy" | "cost";

const LABELS: Record<OutputKey, string> = {
  brief: "Brief", pr: "Pull request", checks: "Checks", tests: "Tests", changes: "Changes", comments: "Comments",
  preview: "Preview", deploy: "Deploy", cost: "Cost",
};
const SEVERITY_ORDER = ["blocker", "major", "minor", "nit"] as const;
const SEVERITY_WORD: Record<string, string> = { blocker: "Blocker", major: "Major", minor: "Minor", nit: "Nit" };
const STATUS_WORD: Record<string, string> = { open: "Open", addressed: "Fixed", dismissed: "Dismissed" };

/** Which row opens by itself: failed checks, or the PR when it is waiting on you (§4.7). */
export function defaultOpen(c: Pick<Cockpit, "state" | "outputs">): OutputKey | null {
  const failed = c.outputs.checks?.gates.some((g) => !g.ok && !g.skipped && !g.followup && !g.waivedBy) && !c.outputs.checks?.advisory;
  if (c.state.state === "needs_waiver" || (failed && c.state.state !== "pr_open")) return "checks";
  if (c.state.state === "pr_open") return "pr";
  return null;
}

function Maker({ roleKey, boss }: { roleKey?: string | null; boss?: boolean }) {
  useIdentityVersion(); // names and faces re-resolve when the install's identities load
  if (roleKey) {
    return <span className="fx-maker" data-role={roleKey} title={`Made by ${nameOf(roleKey).name}`}><RunFace roleKey={roleKey} size={20} state="idle" /></span>;
  }
  if (boss) return <span className="fx-maker" data-face="boss" title="BOSS machinery. No agent at this station."><RunFace roleKey={null} variant="boss" size={20} state="idle" /></span>;
  return null;
}

/** The Brief row's word: what the build was held to, else where it was built. */
export function briefWord(c: Pick<Cockpit, "brief">): string {
  const b = c.brief;
  if (b.criteria.length) return plural(b.criteria.length, "criterion", "criteria");
  if (b.testCommands.length) return plural(b.testCommands.length, "check");
  return b.repo || b.workflow?.name || "—";
}

/** Why a row has nothing yet, when the server's word only says that it hasn't (lib/build-detail). */
function missingOf(key: OutputKey, c: Cockpit): { word: string; tip?: string } | null {
  if (key !== "pr" && key !== "tests" && key !== "changes" && key !== "preview" && key !== "cost") return null;
  return missingWord(key, { ...nowInput(c), outputs: c.outputs, local: buildIsLocal(c) });
}

/** A build whose code is on this machine: its "Pull request" row is its Change set (BOSSAPP-946). */
export function buildIsLocal(c: Pick<Cockpit, "brief" | "outputs">): boolean {
  return isLocalRepo(c.brief.repo ?? "") || isLocalChangeUrl((c.outputs.pr as { url?: string | null } | null)?.url ?? "");
}

/** The row's label, in the build's own words. */
export function outputLabel(k: OutputKey, c: Pick<Cockpit, "brief" | "outputs">): string {
  return k === "pr" && buildIsLocal(c) ? "Change set" : LABELS[k];
}

function valueOf(key: OutputKey, o: CockpitOutputs, c: Cockpit): string {
  const missing = missingOf(key, c);
  if (missing) return missing.word;
  switch (key) {
    case "brief": return briefWord(c);
    case "pr": return o.pr?.word || "Not yet";
    case "checks": return o.checks?.word || "Not yet";
    case "tests": return o.tests?.word || "Not run";
    case "changes": return o.changes?.word || "Not yet";
    // One finding once: review rounds re-raise the same one (lib/review-comments.ts).
    case "comments": return o.comments.items.length ? commentsWord(o.comments.items) : o.comments.word || "None";
    case "preview": return o.preview?.word || "Not running";
    case "deploy": return o.deploy.word || "Not set up";
    case "cost": return o.cost.word || "—";
  }
}

export type OutputsProps = {
  projectId: string;
  actor: string;
  cockpit: Cockpit;
  open: OutputKey | null;
  onOpen: (key: OutputKey | null) => void;
  onLogs: (tab: "build" | "preview" | "deploy") => void;
  onRefresh: () => void;
};

export function Outputs({ projectId, actor, cockpit: c, open, onOpen, onLogs, onRefresh }: OutputsProps) {
  const o = c.outputs;
  const keys: OutputKey[] = ["brief", "pr", "checks", "tests", "changes", "comments", "preview", "deploy", "cost"];
  const makers: Partial<Record<OutputKey, React.ReactNode>> = {
    pr: <Maker roleKey={o.pr?.roleKey} />,
    tests: <Maker roleKey={o.tests?.roleKey} />,
    changes: <Maker roleKey={o.changes?.codeRoleKey ?? o.changes?.testsRoleKey} />,
    comments: (() => {
      const authors = [...new Set(o.comments.items.map((x) => x.authorRoleKey).filter(Boolean) as string[])];
      return authors.length ? <AgFaceStack roleKeys={authors} size={20} max={3} /> : null;
    })(),
    preview: o.preview ? <Maker boss /> : null,
    deploy: o.deploy.items.length ? <Maker boss /> : null,
    cost: o.cost.byAgent.length ? <AgFaceStack roleKeys={o.cost.byAgent.map((a) => a.roleKey).filter(Boolean) as string[]} size={20} max={3} /> : null,
  };
  return (
    <section className="fx-outputs" aria-labelledby="fx-outputs-h" data-testid="fx-outputs" tabIndex={-1}>
      <header className="fx-sec-head">
        <h2 id="fx-outputs-h" className="fx-sec-title">OUTPUTS</h2>
      </header>
      <span className="fx-ready" data-ready={o.mergeReady.ready || undefined} data-testid="fx-merge-ready"
            title="From checks, tests and review comments">
        {o.mergeReady.ready ? <Check size={12} weight="bold" aria-hidden /> : null}
        {o.mergeReady.word || "Building"}
      </span>
      <ul className="fx-out-list">
        {keys.map((k) => {
          const isOpen = open === k;
          return (
            <li key={k} className="fx-out" data-key={k} data-open={isOpen || undefined} data-testid={`fx-out-${k}`}>
              <div className="fx-out-row">
              <button type="button" className="fx-out-head" aria-expanded={isOpen} data-testid={`fx-out-head-${k}`}
                      title={k === "changes" ? "Files only. Open the PR for the diff." : undefined}
                      onClick={() => onOpen(isOpen ? null : k)}>
                <span className="fx-out-label">{outputLabel(k, c)}</span>
                <span className="fx-out-value" data-testid={`fx-out-value-${k}`}
                      title={missingOf(k, c)?.tip ?? (k === "cost" && o.cost.word === "Included in plan" ? "Ran on a subscription. No metered spend."
                        : k === "cost" ? "Metered model spend so far"
                        : k === "checks" && c.outputs.checks?.advisory ? "First pass: checks advise, they don't block" : undefined)}>
                  {valueOf(k, o, c)}
                </span>
                <span className="fx-out-maker">{makers[k]}</span>
                {isOpen ? <CaretDown size={10} weight="bold" aria-hidden /> : <CaretRight size={10} weight="bold" aria-hidden />}
              </button>
              {/* THE PREVIEW, ONE PRESS FROM THE ROW (operator, 2026-11-19): Start preview (never
                  had one), Recreate preview (stopped or expired), Open preview (running) — the
                  Your app card's one open. Only once there is a branch to preview. */}
              {k === "preview" && previewCanRun(c) ? (
                <span className="fx-out-inline">
                  <WorkPreviewButton app={buildPreviewApp(c.run.id, o.preview)} projectId={projectId} actor={actor}
                                     testid="fx-out-preview-act" onStarted={onRefresh} className="ag-btn" />
                </span>
              ) : null}
              </div>
              {isOpen ? (
                <div className="fx-out-body" data-testid={`fx-out-body-${k}`}>
                  {k === "brief" ? <BriefBody c={c} /> : null}
                  {k === "pr" ? <PrBody c={c} actor={actor} onRefresh={onRefresh} /> : null}
                  {k === "checks" ? <ChecksBody c={c} projectId={projectId} actor={actor} onRefresh={onRefresh} /> : null}
                  {k === "tests" ? <TestsBody c={c} /> : null}
                  {k === "changes" ? <ChangesBody c={c} /> : null}
                  {k === "comments" ? <CommentsBody c={c} actor={actor} onRefresh={onRefresh} /> : null}
                  {k === "preview" ? <PreviewBody c={c} actor={actor} onLogs={() => onLogs("preview")} onRefresh={onRefresh} /> : null}
                  {k === "deploy" ? <DeployBody c={c} projectId={projectId} actor={actor} onLogs={() => onLogs("deploy")} /> : null}
                  {k === "cost" ? <CostBody c={c} projectId={projectId} /> : null}
                </div>
              ) : null}
            </li>
          );
        })}
      </ul>
    </section>
  );
}

/** The review on the pull request, and "Review this PR again": the adversarial review on the
 *  PR's real diff, posted on the PR as a new round (services/pr_review_again.py). */
export function PrReviewLine({ c, actor, onRefresh }: { c: Cockpit; actor: string; onRefresh?: () => void }) {
  const pr = c.outputs.pr;
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  if (!pr?.url) return null;
  const review = pr.review;
  const inFlight = review?.again === "queued" || review?.again === "running";
  const go = async () => {
    setBusy(true); setError("");
    const r = await reviewPrAgain(c.run.id, actor);
    setBusy(false);
    if (r.ok) { showToast("success", r.data?.already ? "Already reviewing this PR" : "Reviewing the PR again"); notifyChanged("code-tasks"); onRefresh?.(); }
    else setError(r.error ?? "");
  };
  return (
    <span className="fx-out-col" data-testid="fx-pr-review-line">
      <span className="fx-out-links">
        <span className="fx-muted" data-testid="fx-pr-review-word">{review?.word || "No review on the PR yet"}</span>
        {review?.url ? <a className="ag-link" href={review.url} target="_blank" rel="noreferrer" data-testid="fx-pr-review-open">See the review ↗</a> : null}
        <GatedButton permission="code_task:steer" type="button" className="ag-btn" disabled={busy || inFlight}
                     data-testid="fx-pr-review-again" onClick={() => void go()}
                     title="Run the adversarial review on this PR's real diff and post it on the PR as a new round">
          {inFlight ? "Reviewing…" : "Review this PR again"}
        </GatedButton>
      </span>
      {review?.reason ? <span className="fx-err" role="note" data-user-text data-testid="fx-pr-review-reason">{review.reason}</span> : null}
      {error ? <span className="fx-err" role="alert" data-user-text>{error}</span> : null}
      <RecheckLine c={c} actor={actor} onRefresh={onRefresh} />
    </span>
  );
}

/** "Re-check this change": every gate again on the branch head, no code written, its
 *  estimated cost on the button (services/change_recheck.py, BOSSAPP-1326). */
export function RecheckLine({ c, actor, onRefresh }: { c: Cockpit; actor: string; onRefresh?: () => void }) {
  const [info, setInfo] = useState<RecheckPreview | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [started, setStarted] = useState("");
  useEffect(() => {
    let live = true;
    void recheckPreview(c.run.id).then((p) => { if (live) setInfo(p); });
    return () => { live = false; };
  }, [c.run.id]);
  if (!info) return null;
  const cost = `about $${info.estimate.usd.toFixed(2)}`;
  const go = async () => {
    setBusy(true); setError("");
    const r = await recheckChange(c.run.id, actor);
    setBusy(false);
    if (r.ok) { setStarted(r.data?.run_id ?? ""); showToast("success", "Re-checking the change"); notifyChanged("code-tasks"); onRefresh?.(); }
    else setError(r.error ?? "");
  };
  return (
    <span className="fx-out-links" data-testid="fx-recheck-line">
      <GatedButton permission="code_task:steer" type="button" className="ag-btn" disabled={busy || !info.can || !!started}
                   data-testid="fx-recheck" onClick={() => void go()}
                   title={info.can ? `Runs every check again (${info.gates.join(", ")}) on this change's branch, writes no code. Estimated ${cost} (${info.estimate.basis}).` : info.why}>
        {started ? "Re-check started" : busy ? "Starting…" : `Re-check this change · ${cost}`}
      </GatedButton>
      {!info.can && info.why ? <span className="fx-muted" data-testid="fx-recheck-why">{info.why}</span> : null}
      {error ? <span className="fx-err" role="alert" data-user-text>{error}</span> : null}
    </span>
  );
}

function PrBody({ c, actor, onRefresh }: { c: Cockpit; actor: string; onRefresh?: () => void }) {
  const pr = c.outputs.pr;
  if (!pr) return <span className="fx-muted">Not yet</span>;
  const card = c.attention.find((a) => a.decisionKind === "pull_request" || a.decisionId === pr.decisionId);
  // THE MERGE DECISION IS ALWAYS REACHABLE (the old page: "Merge gated by Decision <id>"), not
  // only while it sits in the attention list: an approved or merged one is still a record.
  const decisionId = card?.decisionId || pr.decisionId;
  const p = c.outputs.preview;
  return (
    <span className="fx-out-col">
      <span className="fx-out-links">
        {pr.url ? <a className="ag-link" href={changeHref(pr.url)} {...linkTarget(pr.url)} data-testid="fx-pr-open">{isLocalChangeUrl(pr.url) ? reviewChangeWords(pr.url) ?? "Open change set" : "Open PR ↗"}</a> : null}
        {pr.filesUrl || pr.url ? (
          <a className="ag-link" href={pr.filesUrl || changeSubHref(pr.url, "files") || undefined} {...linkTarget(pr.filesUrl || pr.url)}>{isLocalChangeUrl(pr.url) ? "View diff" : "View diff ↗"}</a>
        ) : null}
        {decisionId ? (
          <a className="ag-link" href={areaHref({ area: "decisions", decision: decisionId })} data-testid="fx-pr-review"
             title={pr.decisionStatus ? `Merge decision: ${pr.decisionStatus}` : "The merge decision"}>Review ↗</a>
        ) : null}
        {pr.madeBy > 0 ? <span className="fx-muted">Made by {pr.madeBy} {pr.madeBy === 1 ? "agent" : "agents"}</span> : null}
      </span>
      <PrReviewLine c={c} actor={actor} onRefresh={onRefresh} />
      {/* APPROVE & SHIP, beside the PR it merges (the old action bar): it reads the preview's
          gate, says what approving deploys, and renders nothing where there is no gate. */}
      {p && ["running", "verified", "starting"].includes(p.status) ? (
        <span className="fx-out-links" data-testid="fx-pr-approve">
          <ApprovePreview preview={{ id: p.id, run_id: c.run.id, status: p.status } as PreviewRec} compact />
        </span>
      ) : null}
    </span>
  );
}

function ChecksBody({ c, projectId, actor, onRefresh }: { c: Cockpit; projectId: string; actor: string; onRefresh: () => void }) {
  const checks = c.outputs.checks;
  const [waiving, setWaiving] = useState(false);
  const bench = c.brief.qaBenchmark;
  const evidence = bench ? <QaBenchmarkEvidence runId={c.run.id} benchmarkName={bench.name} /> : null;
  if (!checks || !checks.gates.length) {
    return <span className="fx-out-col"><span className="fx-muted">{checks?.word || "After review"}</span>{evidence}</span>;
  }
  // WAIVE FROM WHERE THE FAILURES ARE LISTED (the old Iterate block's override), for any
  // stopped build with a failing check, not only one whose card is on the Floor.
  const failing = checks.gates.filter((g) => !g.ok && !g.skipped && !g.followup && !g.waivedBy);
  const canWaive = failing.some((g) => g.waivable) && ["needs_waiver", "failed", "blocked"].includes(c.state.state);
  return (
    <span className="fx-out-col">
      <ul className="fx-gates">
        {checks.gates.map((g) => (
          <li key={g.name} className="fx-gate" data-ok={g.skipped ? "skipped" : g.followup ? "followup" : g.ok} data-testid={`fx-gate-${g.name}`} title={g.evidence || undefined}>
            {g.skipped || g.followup ? <Minus size={12} weight="bold" aria-hidden /> : g.ok ? <Check size={12} weight="bold" aria-hidden /> : <X size={12} weight="bold" aria-hidden />}
            <span className="ag-sr">{g.skipped ? "skipped" : g.followup ? "follow-up" : g.ok ? "passed" : "failed"}</span>
            <span>{g.label}</span>
            {g.skipped ? <span className="fx-muted">Skipped</span> : null}
            {g.followup ? <span className="fx-muted">Follow-up</span> : null}
            {g.evidence ? <span className="fx-muted fx-trunc" data-user-text data-testid={`fx-gate-why-${g.name}`}>{g.evidence}</span> : null}
            {g.waivedBy ? <span className="fx-muted">Waived by {g.waivedBy}</span> : null}
            <Maker roleKey={g.roleKey} boss={!g.roleKey} />
          </li>
        ))}
      </ul>
      {canWaive ? (
        <span className="fx-out-links">
          <button type="button" className="ag-btn" data-testid="fx-checks-waive" title="Open the PR past these checks. The merge still needs you."
                  onClick={() => setWaiving(true)}>Waive…</button>
        </span>
      ) : null}
      {evidence}
      {waiving ? (
        <WaiveSheet runId={c.run.id} app={c.app} projectId={projectId} actor={actor}
                    failing={failing.map((g) => ({ name: g.name, label: g.label, waivable: g.waivable, reason: g.reason }))}
                    onClose={() => setWaiving(false)} onDone={() => { setWaiving(false); onRefresh(); }} />
      ) : null}
    </span>
  );
}

/** Visible database reasons. Trimmed non-empty strings joined with "; ". Nothing when none. */
export function databaseWhySentence(why: readonly string[] | null | undefined): string | null {
  const kept = (why ?? []).map((part) => part.trim()).filter(Boolean);
  return kept.length ? kept.join("; ") : null;
}

/** Postgres for this build, with why beside it. Blank is nothing. More than six words is a paragraph. */
export function DatabaseWhyLine({ why, sentence }: { why: readonly string[]; sentence: string | null }) {
  const text = !sentence ? null : sentence.split(/\s+/).filter(Boolean).length > 6
    ? <p data-testid="fx-brief-database-why" data-user-text>{sentence}</p>
    : <span data-testid="fx-brief-database-why" data-user-text>{sentence}</span>;
  return (
    <span className="fx-kv" data-testid="fx-brief-database" title={why.join("; ")} data-tip={why.join("; ")}>
      <span className="fx-out-sub">Database</span><span>Postgres for this build ✓</span>
      {text}
    </span>
  );
}

/** What the build was asked to do and how it was built: the old page's contract and meta rows. */
function BriefBody({ c }: { c: Cockpit }) {
  const b = c.brief;
  const yes = (v?: boolean) => (v === undefined ? "—" : v ? "yes" : "no");
  const github = b.repo && isGitHubUrl(c.outputs.pr?.url) ? repoUrl(b.repo) : null;
  return (
    <span className="fx-out-col" data-testid="fx-brief">
      {b.criteria.length ? (
        <span className="fx-out-col" data-group="criteria">
          <span className="fx-out-sub">Done when</span>
          {b.criteria.map((x, i) => <span key={i} className="fx-clamp" title={x} data-user-text>{x}</span>)}
        </span>
      ) : null}
      {b.testCommands.length ? (
        <span className="fx-out-col" data-group="verify">
          <span className="fx-out-sub" title="The commands the build must pass">Verified by</span>
          {b.testCommands.map((x, i) => <span key={i} className="fx-mono fx-trunc" title={x} data-user-text>{x}</span>)}
        </span>
      ) : null}
      {b.database?.postgres ? (
        <DatabaseWhyLine why={b.database.why} sentence={databaseWhySentence(b.database.why)} />
      ) : null}
      {b.repo ? (
        <span className="fx-kv"><span className="fx-out-sub">Repo</span>
          {github && b.branch ? (
            <a className="ag-link fx-mono" href={`${github}/tree/${encodeURIComponent(b.branch)}`} target="_blank" rel="noreferrer"
               data-user-text title={b.branch}>{b.repo}, {b.branch} </a>
          ) : <span className="fx-mono" data-user-text>{[b.repo, b.branch].filter(Boolean).join(", ")}</span>}
        </span>
      ) : null}
      {b.workflow ? (
        <span className="fx-kv" data-testid="fx-brief-workflow"><span className="fx-out-sub">Workflow</span>
          <span data-user-text>{b.workflow.name}{typeof b.workflow.version === "number" ? ` v${b.workflow.version}` : ""}</span>
          {b.workflow.default ? <span className="fx-muted" title="Nothing chose a workflow, so the standard one ran">default</span> : null}
        </span>
      ) : null}
      {b.engine || b.model ? (
        <span className="fx-kv"><span className="fx-out-sub">Built with</span>
          <span data-user-text>{[b.engine, b.model].filter(Boolean).join(", ")}</span>
        </span>
      ) : null}
      {b.reviewEngine || b.reviewModel ? (
        <span className="fx-kv"><span className="fx-out-sub">Reviewed with</span>
          <span data-user-text>{[b.reviewEngine, b.reviewModel].filter(Boolean).join(", ")}</span>
          <span className="fx-muted" title={b.crossModel ? "A different model reviewed the code: no self-grading" : "The model that built it also reviewed it"}>
            {b.crossModel ? "cross-model" : "same model"}
          </span>
        </span>
      ) : null}
      {c.routes.length ? (
        <span className="fx-out-col" data-group="models" data-testid="fx-brief-models">
          <span className="fx-out-sub" title="Which model ran each stage, and why">Models</span>
          <span className="wm-list">
            {latestPerStep(c.routes).map((r) => (
              <WhyThisModel key={r.id} route={r} title={r.step_label ?? r.step} />
            ))}
          </span>
        </span>
      ) : null}
      {b.profile || typeof b.riskTier === "number" ? (
        <span className="fx-kv"><span className="fx-out-sub" title="The profile and risk tier set which checks run">Setup</span>
          <span data-user-text>{[b.profile, typeof b.riskTier === "number" ? `risk tier ${b.riskTier}` : ""].filter(Boolean).join(", ")}</span>
        </span>
      ) : null}
      {b.reviewerIndependent !== undefined || b.testAuthorIndependent !== undefined ? (
        <span className="fx-kv"><span className="fx-out-sub" title="Whether a different agent reviewed the code and wrote the tests">Independent</span>
          <span>reviewer {yes(b.reviewerIndependent)}, tests {yes(b.testAuthorIndependent)}</span>
        </span>
      ) : null}
      {b.builtOn || typeof b.turns === "number" ? (
        <span className="fx-kv"><span className="fx-out-sub">Ran on</span>
          <span data-user-text>{[b.builtOn, typeof b.turns === "number" ? plural(b.turns, "turn") : ""].filter(Boolean).join(", ")}</span>
        </span>
      ) : null}
      {b.deferredFindings ? (
        <span className="fx-kv" title="Findings below the blocking bar: they shipped with the change and become their own build">
          <span className="fx-out-sub">Deferred</span><span>{plural(b.deferredFindings, "finding")}</span>
        </span>
      ) : null}
      {b.redelivered ? (
        <span className="fx-kv" title="A worker lost this build and another resumed it; work in flight was redone">
          <span className="fx-out-sub">Restarted</span><span>{plural(b.redelivered, "time")}</span>
        </span>
      ) : null}
      {b.error ? (
        <span className="fx-kv" data-testid="fx-brief-error"><span className="fx-out-sub">Error</span>
          <span className="fx-clamp fx-err" title={b.error} data-user-text>{b.error}</span>
        </span>
      ) : null}
    </span>
  );
}

function TestsBody({ c }: { c: Cockpit }) {
  const t = c.outputs.tests;
  const [show, setShow] = useState(false);
  if (!t || !t.commands.length) return <span className="fx-muted">Not run</span>;
  const failed = failedTestName(t.outputTail);
  return (
    <span className="fx-out-col">
      {t.commands.map((cmd, i) => (
        <span key={i} className="fx-cmd" data-ok={cmd.ok}>
          <span className="fx-mono" data-user-text>{cmd.command}</span>
          <span>, exit {cmd.exitCode ?? "?"}</span>
          <span>, {cmd.timedOut ? "timed out" : cmd.ok ? "ok" : "failed"}</span>
          {typeof cmd.durationMs === "number" ? <span className="fx-muted">, {secondsWord(Math.round(cmd.durationMs / 1000))}</span> : null}
        </span>
      ))}
      {failed ? <span className="fx-mono fx-cmd" data-ok="false" data-testid="fx-tests-failed" data-user-text>{failed}</span> : null}
      {t.outputTail ? (
        <>
          <button type="button" className="ag-link" aria-expanded={show} data-testid="fx-tests-output" onClick={() => setShow((v) => !v)}>
            Show output {show ? "▴" : "▾"}
          </button>
          {show ? <pre className="fx-log fx-log-small" data-user-text>{t.outputTail}</pre> : null}
        </>
      ) : null}
    </span>
  );
}

function ChangesBody({ c }: { c: Cockpit }) {
  const ch = c.outputs.changes;
  if (!ch || (!ch.tests.length && !ch.code.length && !ch.filesChanged)) return <span className="fx-muted">Not yet</span>;
  const diff = ch.diffUrl || c.outputs.pr?.filesUrl || (c.outputs.pr?.url ? changeSubHref(c.outputs.pr.url, "files") ?? undefined : undefined);
  const group = (name: string, files: { path: string; bytes?: number }[], roleKey: string | null) => files.length ? (
    <span className="fx-out-col" data-group={name}>
      <span className="fx-out-sub">{name} <Maker roleKey={roleKey} /></span>
      {files.map((f) => (
        <span key={f.path} className="fx-file">
          <span className="fx-mono fx-trunc" title={f.path} data-user-text>{f.path}</span>
          {typeof f.bytes === "number" ? <span className="fx-muted">, {f.bytes} bytes</span> : null}
        </span>
      ))}
    </span>
  ) : null;
  return (
    <span className="fx-out-col">
      {typeof ch.additions === "number" || typeof ch.deletions === "number" ? (
        <span className="fx-diffstat" data-testid="fx-diffstat">
          <span>{plural(ch.filesChanged ?? ch.tests.length + ch.code.length, "file")}</span>
          <span className="fx-add"> +{ch.additions ?? 0}</span>
          <span className="fx-del"> −{ch.deletions ?? 0}</span>
        </span>
      ) : null}
      {group("Tests", ch.tests, ch.testsRoleKey)}
      {group("Code", ch.code, ch.codeRoleKey)}
      {diff ? <a className="ag-link" href={diff} target="_blank" rel="noreferrer">View diff ↗</a> : null}
    </span>
  );
}

/** The follow-up "Let BOSS fix these" started, per build, for the findings it was given. Kept
 *  across a refresh or remount of the page in this tab. */
const fixStarted = new Map<string, { instructions: string; runId: string }>();

function CommentsBody({ c, actor, onRefresh }: { c: Cockpit; actor: string; onRefresh: () => void }) {
  const items: FoldedComment[] = foldComments(c.outputs.comments.items);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState("");
  if (!items.length) return <span className="fx-muted">None</span>;
  const act = async (cm: FoldedComment, status: "addressed" | "dismissed") => {
    setBusy(cm.id);
    setError("");
    // A folded row settles every repeat it stands for.
    let failed = "";
    for (const id of cm.ids) {
      const r = await resolveComment(c.run.id, id, actor, status);
      if (!r.ok) { failed = r.error ?? ""; break; }
    }
    setBusy(null);
    if (!failed) { showToast("success", "Comment resolved"); notifyChanged("code-tasks"); onRefresh(); }
    else setError(failed);
  };
  // LET BOSS FIX THESE: a follow-up build on this app with every open finding as its brief
  // (the same follow-up a person asks for in Add a feature; code task followUp).
  const instructions = fixFindingsInstructions(c.outputs.comments.items);
  // ONCE PER SET OF FINDINGS (review #224): after it started, the findings stay open until the
  // follow-up fixes them, so the button would offer the same paid build again. It waits until
  // the findings change; the API also joins an identical follow-up already in flight.
  const started = fixStarted.get(c.run.id);
  const already = started && started.instructions === instructions ? started : null;
  const fixAll = async () => {
    setBusy("fix-all");
    setError("");
    const form = new FormData();
    form.set("run_id", c.run.id);
    form.set("instructions", instructions);
    form.set("actor", actor);
    const res = await callAction(followUpCodeTaskAction, undefined, "code_task:steer")(form);
    setBusy(null);
    if (res?.error) { setError(res.error); return; }
    if (res?.run_id) fixStarted.set(c.run.id, { instructions, runId: res.run_id });
    showToast("success", res?.existing ? "BOSS is already fixing them" : "BOSS started a follow-up to fix them");
    notifyChanged("code-tasks"); onRefresh();
  };
  return (
    <span className="fx-out-col">
      {error ? <span className="fx-err" role="alert" data-user-text>{error}</span> : null}
      {instructions && already ? (
        <span className="fx-out-links" data-testid="fx-fix-comments-started">
          <span className="fx-muted">BOSS is fixing these</span>
          <a href={areaHref({ area: "code_tasks", build: already.runId })} data-testid="fx-fix-comments-open">Open the follow-up</a>
        </span>
      ) : instructions ? (
        <span className="fx-out-links">
          <GatedButton permission="code_task:steer" type="button" className="ag-btn" disabled={busy !== null}
                       data-testid="fx-fix-comments" title="Start a follow-up build that fixes every open comment"
                       onClick={() => void fixAll()}>Let BOSS fix these</GatedButton>
        </span>
      ) : null}
      {SEVERITY_ORDER.map((sev) => {
        const group = items.filter((x) => x.severity === sev);
        if (!group.length) return null;
        return (
          <span key={sev} className="fx-out-col" data-severity={sev}>
            <span className="fx-out-sub">{SEVERITY_WORD[sev]}</span>
            {group.map((cm) => (
              <span key={cm.id} className="fx-cmt" data-status={cm.status} data-testid={`fx-comment-${cm.id}`}>
                <span className="fx-cmt-head">
                  <Maker roleKey={cm.authorRoleKey} />
                  {cm.path ? <span className="fx-mono fx-trunc" title={cm.path} data-user-text>{cm.path}{cm.line ? `:${cm.line}` : ""}</span> : null}
                  <span className="fx-muted">{STATUS_WORD[cm.status] ?? cm.status}</span>
                  {cm.repeats ? <span className="fx-muted" title="Raised again in later review rounds">· raised {cm.repeats + 1}×</span> : null}
                </span>
                <span className="fx-clamp" title={cm.body} data-user-text>{cm.body}</span>
                {cm.status === "open" ? (
                  <span className="fx-out-links">
                    <GatedButton permission="code_task:steer" type="button" className="ag-btn" disabled={busy !== null} data-testid={`fx-resolve-${cm.id}`}
                            onClick={() => void act(cm, "addressed")}>Resolve</GatedButton>
                    <GatedButton permission="code_task:steer" type="button" className="ag-link" disabled={busy !== null} data-testid={`fx-dismiss-${cm.id}`}
                            onClick={() => void act(cm, "dismissed")}>Dismiss</GatedButton>
                  </span>
                ) : null}
              </span>
            ))}
          </span>
        );
      })}
    </span>
  );
}

/**
 * A FALLBACK THAT WAS PASSED OVER IS NOT A FAILURE (tester P1-4). A preview tries its ways of
 * running in order (retained rootfs, a container, a host process); one that cannot serve here is
 * recorded `failed` before the next one serves. Once a later step is done, or the preview runs,
 * that step is "passed over", not a red ✗ beside a working preview.
 */
export function isPassedOver(p: Pick<PreviewOutput, "status" | "steps">, i: number): boolean {
  const x = p.steps[i];
  if (!x || x.status !== "failed") return false;
  return p.status === "running" || p.steps.slice(i + 1).some((y) => y.status === "ok");
}

/** The preview's error, visible. Blank is nothing. */
export function previewErrorSentence(error: string | null | undefined): string | null {
  const trimmed = error?.trim() ?? "";
  // Why BOSS stopped it, said plainly: "Stopped to free memory (nobody opened it for 20 min)."
  return trimmed ? trimmed.replace(/^Stopped by BOSS: to /, "Stopped to ") : null;
}

/** Stopped or Failed, with the error beside it. More than six words is a paragraph. */
export function PreviewErrorLine({ status, error, sentence }: {
  status: string;
  error: string | null | undefined;
  sentence: string | null;
}) {
  const text = !sentence ? null : sentence.split(/\s+/).filter(Boolean).length > 6
    ? <p data-testid="fx-preview-error" data-user-text>{sentence}</p>
    : <span data-testid="fx-preview-error" data-user-text>{sentence}</span>;
  return status === "stopped" || status === "expired" ? (
    <>
      <span className="fx-muted" title={error ?? undefined} data-testid="fx-preview-stopped">Stopped</span>
      {text}
    </>
  ) : (
    <>
      <span className="fx-err" title={error ?? undefined}>Failed</span>
      {text}
    </>
  );
}

/** The steps toggle's words: how it is running once it runs; how far it got while it boots. */
export function stepsWord(p: Pick<PreviewOutput, "status" | "steps" | "stepsDone" | "stepsTotal">): string {
  const serving = [...p.steps].reverse().find((x) => x.status === "ok");
  if (p.status === "running") return serving ? `Running, ${serving.label}` : "How it started";
  const finished = p.steps.filter((x) => x.status === "ok" || x.status === "skipped" || x.status === "failed").length;
  const total = p.stepsTotal ?? p.steps.length;
  return `${Math.min(finished || p.stepsDone || 0, total)} of ${plural(total, "step")}`;
}

/** What happens to an app's data in its previews (preview_data.py), said where it matters. */
export const PREVIEW_DATA_TIP = "What this app saves (its SQLite files and data folder) is kept when the preview stops, idles out or restarts, for every build of this app. Fresh data deletes it and starts the app empty.";
export const FRESH_DATA_CONFIRM = "Delete the data this app saved in its previews and start it empty? This cannot be undone.";

/** A preview runs a branch: only once the build opened its PR (or GitHub waits on it). */
export function previewCanRun(c: Pick<Cockpit, "state">): boolean {
  return ["pr_open", "merged", "deployed", "github_wait"].includes(c.state.state);
}

function PreviewBody({ c, actor, onLogs, onRefresh }: { c: Cockpit; actor: string; onLogs: () => void; onRefresh: () => void }) {
  const p = c.outputs.preview;
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  // Pressed and not yet read back: it says so at once (walk D9, D10), not after the boot.
  const [starting, setStarting] = useState(false);
  // A library build has no live app: the press ran its tests instead (the old "Ran tests , 
  // passed (library build)"), which is an answer, not an error.
  const [note, setNote] = useState("");
  const [showNote, setShowNote] = useState(false);
  const [showSteps, setShowSteps] = useState(false);
  const start = async (freshData = false) => {
    setBusy(true);
    setError("");
    setNote("");
    setStarting(true);
    showToast("success", freshData ? "Preview starting with fresh data" : "Preview starting");
    const form = new FormData();
    form.set("run_id", c.run.id);
    form.set("actor", actor);
    if (freshData) form.set("fresh_data", "true");
    const res = await callAction(startPreviewAction, undefined, "preview:manage")(null, form);
    setBusy(false);
    // A CLIENT TIMEOUT IS NOT A VERDICT: the preview builds on after the console stops waiting.
    if (res?.error && /did not answer within/i.test(res.error)) { notifyChanged("previews"); onRefresh(); return; }
    if (res?.error) { setError(res.error); setStarting(false); }
    else {
      if (res?.note) { setNote(String(res.note)); setStarting(false); }
      notifyChanged("previews"); onRefresh();
    }
  };
  const booting = p?.status === "starting" || (starting && (!p || p.status !== "running"));
  const stop = async () => {
    if (!p) return;
    setBusy(true);
    const r = await stopPreview(p.id, actor);
    setBusy(false);
    if (!r.ok) setError(r.error ?? ""); else { notifyChanged("previews"); onRefresh(); }
  };
  // A PREVIEW RUNS A BRANCH: a build that opened no PR has none, and the press answered with an
  // internal error (tester P2). Disabled, and it says why.
  // A build waiting for GitHub previews from this machine's copy (BOSSAPP-922).
  const canRun = previewCanRun(c);
  const cannotRun = canRun ? undefined : c.state.state === "done_no_pr"
    ? "This build opened no PR, so there is no branch to preview"
    : "Runs once the build has opened its PR";
  const err = error ? <span className="fx-err" role="alert" title={error}>Couldn't start</span> : null;
  const passed = note ? (
    <span className="fx-out-col" data-testid="fx-preview-note">
      <button type="button" className="ag-link" aria-expanded={showNote} onClick={() => setShowNote((v) => !v)}
              title="A library or CLI has no live app: its tests ran instead">Tests passed, no live app {showNote ? "▴" : "▾"}</button>
      {showNote ? <pre className="fx-log fx-log-small" data-user-text>{note}</pre> : null}
    </span>
  ) : null;
  if (!p) {
    return (
      <span className="fx-out-links">
        {booting ? <span className="fx-muted" data-testid="fx-preview-starting">Starting…</span> : null}
        {cannotRun ? <span className="fx-muted" data-testid="fx-preview-why">{c.state.state === "done_no_pr" ? "No PR to preview" : "After the PR"}</span> : null}
        <GatedButton permission="preview:manage" type="button" className="ag-btn" disabled={busy || !canRun} data-testid="fx-preview-run"
                title={cannotRun} onClick={() => void start()}>
          Run preview
        </GatedButton>
        {err}
        {passed}
      </span>
    );
  }
  const running = p.status === "running";
  return (
    <span className="fx-out-links">
      {booting ? <span className="fx-muted" data-testid="fx-preview-starting">Starting…</span> : null}
      {running ? <a className="ag-link" href={previewWebHref(p.id)} target="_blank" rel="noreferrer" data-testid="fx-preview-open">Open ↗</a> : null}
      {/* RECREATE PREVIEW (operator, 2026-11-19): a preview that is not running starts again
          from the same build, its kept data restored. A running one restarts. */}
      <GatedButton permission="preview:manage" type="button" className="ag-btn" disabled={busy} data-testid="fx-preview-restart"
                   title={running || p.status === "starting" ? "Start it again" : "Start it again from the same build. What the app saved is kept."}
                   onClick={() => void start()}>{running || p.status === "starting" ? "Restart" : "Recreate preview"}</GatedButton>
      {running ? <GatedButton permission="preview:manage" type="button" className="ag-link" disabled={busy} data-testid="fx-preview-stop" onClick={() => void stop()}>Stop</GatedButton> : null}
      <button type="button" className="ag-link" onClick={onLogs} data-testid="fx-preview-logs">Logs ▾</button>
      {/* A stopped preview expires at nothing: its time limit is only news while it runs. */}
      {running && p.expiresAt ? <span className="fx-muted" title={p.expiresAt} data-testid="fx-preview-expires">Expires {localWith(p.expiresAt, { hour: "numeric", minute: "2-digit" })}</span> : null}
      <span className="fx-muted" data-testid="fx-preview-data" title={PREVIEW_DATA_TIP}>Data kept</span>
      <GatedButton permission="preview:manage" type="button" className="ag-link" disabled={busy} data-testid="fx-preview-fresh"
                   title="Delete the data this app saved in its previews, then start it empty"
                   onClick={() => { if (window.confirm(FRESH_DATA_CONFIRM)) void start(true); }}>Fresh data</GatedButton>
      {p.substrate ? <span className="fx-muted" title="Where the preview runs">{p.substrate}</span> : null}
      {typeof p.stepsDone === "number" && p.stepsTotal ? (
        <button type="button" className="ag-link" aria-expanded={showSteps} data-testid="fx-preview-steps"
                disabled={!p.steps.length} onClick={() => setShowSteps((v) => !v)}>
          {stepsWord(p)} {p.steps.length ? (showSteps ? "▴" : "▾") : ""}
        </button>
      ) : null}
      {p.error && !running ? (
        // Idle or expired is Stopped, not a failure. The reason stays on the title and is shown beside it.
        <PreviewErrorLine status={p.status} error={p.error} sentence={previewErrorSentence(p.error)} />
      ) : null}
      {err}
      {passed}
      {showSteps && p.steps.length ? (
        <span className="fx-out-col" data-testid="fx-preview-ledger">
          {p.steps.map((x, i) => {
            const passedOver = isPassedOver(p, i);
            return (
            <span key={i} className="fx-cmd" data-ok={x.status === "failed" && !passedOver ? "false" : undefined}
                  title={passedOver ? "Tried first; it could not serve here, so the next way was used" : undefined}>
              <span aria-hidden>{x.status === "ok" ? "✓" : passedOver || x.status === "skipped" ? "–" : x.status === "failed" ? "✗" : "…"} </span>
              <span className="ag-sr">{passedOver ? "passed over" : x.status}</span>
              <span data-user-text>{x.label}</span>
              {passedOver ? <span className="fx-muted">, passed over</span> : null}
              {x.durationMs ? <span className="fx-muted">, {secondsWord(Math.round(x.durationMs / 1000))}</span> : null}
              {x.log ? <span className="fx-muted fx-trunc" title={x.log} data-user-text>, {x.log}</span> : null}
            </span>
            );
          })}
        </span>
      ) : null}
    </span>
  );
}

function DeployBody({ c, projectId, actor, onLogs }: { c: Cockpit; projectId: string; actor: string; onLogs: () => void }) {
  const deployable = ["pr_open", "merged", "deployed", "done_no_pr"].includes(c.state.state);
  return (
    <span className="fx-out-col">
      {/* Today's deployment logic, moved as it is (spec §4.5.5); restyled with the Factory later. */}
      {/* THE BUILD, NOT A STUB: the line looks the deployment up by the app's name, and a bare
          {id} named every app "Untitled app" (lookup and Set target default). */}
      <DeploymentLine run={{ id: c.run.id, title: c.run.title,
                             finalize_ctx: c.brief.qaBenchmark ? { qa_benchmark: { name: c.brief.qaBenchmark.name } } : {} } as unknown as CodeTaskRun}
                      projectId={projectId} actor={actor} deployable={deployable} />
      {c.outputs.deploy.items.length ? <button type="button" className="ag-link" onClick={onLogs}>Logs ▾</button> : null}
    </span>
  );
}

function CallsFold({ projectId, runId, metered }: { projectId: string; runId: string; metered: boolean }) {
  useIdentityVersion(); // names and faces re-resolve when the install's identities load
  const [open, setOpen] = useState(false);
  const [calls, setCalls] = useState<ModelCall[] | null>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    if (!open || calls) return undefined;
    let alive = true;
    void fetchCalls(projectId, runId).then((got) => { if (!alive) return; if (got) setCalls(got); else setFailed(true); });
    return () => { alive = false; };
  }, [open, calls, projectId, runId]);
  return (
    <span className="fx-out-col">
      <button type="button" className="ag-link fx-fold-btn" aria-expanded={open} data-testid="fx-cost-calls" onClick={() => setOpen((v) => !v)}>
        Model calls {open ? "▴" : "▾"}
      </button>
      {open ? (
        calls ? (
          calls.length ? (
            <span className="fx-out-col" data-testid="fx-calls">
              {calls.map((x, i) => (
                <span key={i} className="fx-call" data-ok={x.ok} data-testid={`fx-call-${x.stage}`}>
                  <span className="fx-mono" data-user-text>{x.stage}</span>
                  <span>, {whoName(x.role)}</span>
                  <span className="fx-muted" data-user-text>, {x.model || x.engine}</span>
                  {x.turns ? <span className="fx-muted">, {plural(x.turns, "turn")}</span> : null}
                  {x.latencyMs >= 1000 ? <span className="fx-muted">, {secondsWord(Math.round(x.latencyMs / 1000))}</span> : null}
                  {x.retries ? <span className="fx-muted">, {plural(x.retries, "retry", "retries")}</span> : null}
                  {metered && x.usd ? <span>, {x.usd < 0.01 ? `$${x.usd.toFixed(3)}` : money(x.usd)}</span> : null}
                  {!x.ok ? <span className="fx-err fx-trunc" title={x.error ?? undefined} data-user-text>, {x.error ?? "failed"}</span> : null}
                </span>
              ))}
            </span>
          ) : <span className="fx-muted">None yet</span>
        ) : <span className="fx-muted">{failed ? "Can't reach BOSS" : "Loading"}</span>
      ) : null}
    </span>
  );
}

function CostBody({ c, projectId }: { c: Cockpit; projectId: string }) {
  useIdentityVersion(); // names and faces re-resolve when the install's identities load
  const cost = c.outputs.cost;
  const stations = Object.entries(cost.byStation).filter(([, v]) => (v ?? 0) > 0);
  return (
    <span className="fx-out-col">
      {cost.byAgent.map((a, i) => (
        <span key={a.roleKey ?? i} className="fx-cost-row" data-role={a.roleKey ?? undefined}>
          <Maker roleKey={a.roleKey} boss={!a.roleKey} />
          <span>{whoName(a.roleKey)}</span>
          <span className="fx-muted">{plural(a.turns, "turn")}</span>
          <span className="fx-muted" title="Model input and output, in thousands">{kilo(a.tokens)}</span>
          {c.metered && costWord(a.usd) ? <span title="Metered model spend so far">{costWord(a.usd)}</span> : null}
        </span>
      ))}
      {c.metered && stations.length ? (
        <span className="fx-out-links">
          {stations.map(([k, v]) => (
            <span key={k} className="fx-muted">{c.stations.find((s) => s.key === k)?.label ?? k} {money(v ?? 0)}</span>
          ))}
        </span>
      ) : null}
      <CallsFold projectId={projectId} runId={c.run.id} metered={c.metered} />
      <a className="ag-link" href={`/v1/code-tasks/${encodeURIComponent(c.run.id)}/retrospective`} target="_blank" rel="noreferrer">
        Retrospective ↗
      </a>
    </span>
  );
}
