"use client";

/**
 * Outputs (factory spec §4.5.5): the Brief, then eight rows, one line each — label · the server's
 * word · the face of the agent who made it — expanding in place, one at a time. The Brief is what
 * the build was asked and how it was built: the old per-build page's contract and meta rows,
 * which the redesign had dropped (parity audit). Every collapsed value is the
 * server's `word`; nothing here counts tests (only commands are real) or draws a diff (hunks are
 * not served: `View diff ↗` goes to the PR).
 */
import { latestPerStep } from "../../lib/model-routes";
import { WhyThisModel } from "../why-this-model";
import { CaretDown, CaretRight, Check, X } from "@phosphor-icons/react";
import { plural } from "../../lib/factory-floor";
import React, { useEffect, useState } from "react";

import type { CodeTaskRun, PreviewRec } from "@boss/api-client";
import type { Cockpit, CockpitOutputs, PreviewOutput, ReviewComment } from "../../lib/cockpit";
import { previewWebHref } from "../../lib/browser-links";
import { callAction } from "../../lib/call-action";
import { GatedButton } from "../../components/gated";
import { startPreviewAction } from "../actions";
import { showToast } from "../client-forms";
import { notifyChanged } from "../changed";
import { DeploymentLine, QaBenchmarkEvidence } from "./legacy-build-parts";
import { ApprovePreview } from "../previews-ui";
import { WaiveSheet } from "./waive-sheet";
import { AgFaceStack } from "../agents/agent-face";
import { nameOf, RunFace } from "../agents/run-face";
import { fetchCalls, resolveComment, stopPreview, type ModelCall } from "./cockpit-actions";
import { areaHref, costWord, kilo, money, secondsWord, whoName } from "./cockpit-util";
import { localWith } from "../../lib/local-time";
import { useIdentityVersion } from "../../lib/use-agent-identities";

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
  const failed = c.outputs.checks?.gates.some((g) => !g.ok && !g.waivedBy) && !c.outputs.checks?.advisory;
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

function valueOf(key: OutputKey, o: CockpitOutputs, c: Cockpit): string {
  switch (key) {
    case "brief": return briefWord(c);
    case "pr": return o.pr?.word || "Not yet";
    case "checks": return o.checks?.word || "Not yet";
    case "tests": return o.tests?.word || "Not run";
    case "changes": return o.changes?.word || "Not yet";
    case "comments": return o.comments.word || "None";
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
              <button type="button" className="fx-out-head" aria-expanded={isOpen} data-testid={`fx-out-head-${k}`}
                      title={k === "changes" ? "Files only. Open the PR for the diff." : undefined}
                      onClick={() => onOpen(isOpen ? null : k)}>
                <span className="fx-out-label">{LABELS[k]}</span>
                <span className="fx-out-value" data-testid={`fx-out-value-${k}`}
                      title={k === "cost" && o.cost.word === "Included in plan" ? "Ran on a subscription. No metered spend."
                        : k === "cost" ? "Metered model spend so far"
                        : k === "checks" && c.outputs.checks?.advisory ? "First pass: checks advise, they don't block" : undefined}>
                  {valueOf(k, o, c)}
                </span>
                <span className="fx-out-maker">{makers[k]}</span>
                {isOpen ? <CaretDown size={10} weight="bold" aria-hidden /> : <CaretRight size={10} weight="bold" aria-hidden />}
              </button>
              {isOpen ? (
                <div className="fx-out-body" data-testid={`fx-out-body-${k}`}>
                  {k === "brief" ? <BriefBody c={c} /> : null}
                  {k === "pr" ? <PrBody c={c} /> : null}
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

function PrBody({ c }: { c: Cockpit }) {
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
        {pr.url ? <a className="ag-link" href={pr.url} target="_blank" rel="noreferrer" data-testid="fx-pr-open">Open PR ↗</a> : null}
        {pr.filesUrl || pr.url ? (
          <a className="ag-link" href={pr.filesUrl || `${pr.url}/files`} target="_blank" rel="noreferrer">View diff ↗</a>
        ) : null}
        {decisionId ? (
          <a className="ag-link" href={areaHref({ area: "decisions", decision: decisionId })} data-testid="fx-pr-review"
             title={pr.decisionStatus ? `Merge decision: ${pr.decisionStatus}` : "The merge decision"}>Review ↗</a>
        ) : null}
        {pr.madeBy > 0 ? <span className="fx-muted">Made by {pr.madeBy} {pr.madeBy === 1 ? "agent" : "agents"}</span> : null}
      </span>
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
  // stopped build with a failing check — not only one whose card is on the Floor.
  const failing = checks.gates.filter((g) => !g.ok && !g.waivedBy);
  const canWaive = failing.some((g) => g.waivable) && ["needs_waiver", "failed", "blocked"].includes(c.state.state);
  return (
    <span className="fx-out-col">
      <ul className="fx-gates">
        {checks.gates.map((g) => (
          <li key={g.name} className="fx-gate" data-ok={g.ok} data-testid={`fx-gate-${g.name}`} title={g.evidence || undefined}>
            {g.ok ? <Check size={12} weight="bold" aria-hidden /> : <X size={12} weight="bold" aria-hidden />}
            <span className="ag-sr">{g.ok ? "passed" : "failed"}</span>
            <span>{g.label}</span>
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

/** What the build was asked to do and how it was built: the old page's contract and meta rows. */
function BriefBody({ c }: { c: Cockpit }) {
  const b = c.brief;
  const yes = (v?: boolean) => (v === undefined ? "—" : v ? "yes" : "no");
  const github = b.repo && (c.outputs.pr?.url ?? "").startsWith("https://github.com/") ? `https://github.com/${b.repo}` : null;
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
      {b.repo ? (
        <span className="fx-kv"><span className="fx-out-sub">Repo</span>
          {github && b.branch ? (
            <a className="ag-link fx-mono" href={`${github}/tree/${encodeURIComponent(b.branch)}`} target="_blank" rel="noreferrer"
               data-user-text title={b.branch}>{b.repo} · {b.branch} ↗</a>
          ) : <span className="fx-mono" data-user-text>{[b.repo, b.branch].filter(Boolean).join(" · ")}</span>}
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
          <span data-user-text>{[b.engine, b.model].filter(Boolean).join(" · ")}</span>
        </span>
      ) : null}
      {b.reviewEngine || b.reviewModel ? (
        <span className="fx-kv"><span className="fx-out-sub">Reviewed with</span>
          <span data-user-text>{[b.reviewEngine, b.reviewModel].filter(Boolean).join(" · ")}</span>
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
          <span data-user-text>{[b.profile, typeof b.riskTier === "number" ? `risk tier ${b.riskTier}` : ""].filter(Boolean).join(" · ")}</span>
        </span>
      ) : null}
      {b.reviewerIndependent !== undefined || b.testAuthorIndependent !== undefined ? (
        <span className="fx-kv"><span className="fx-out-sub" title="Whether a different agent reviewed the code and wrote the tests">Independent</span>
          <span>reviewer {yes(b.reviewerIndependent)} · tests {yes(b.testAuthorIndependent)}</span>
        </span>
      ) : null}
      {b.builtOn || typeof b.turns === "number" ? (
        <span className="fx-kv"><span className="fx-out-sub">Ran on</span>
          <span data-user-text>{[b.builtOn, typeof b.turns === "number" ? plural(b.turns, "turn") : ""].filter(Boolean).join(" · ")}</span>
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
  return (
    <span className="fx-out-col">
      {t.commands.map((cmd, i) => (
        <span key={i} className="fx-cmd" data-ok={cmd.ok}>
          <span className="fx-mono" data-user-text>{cmd.command}</span>
          <span> · exit {cmd.exitCode ?? "?"}</span>
          <span> · {cmd.timedOut ? "timed out" : cmd.ok ? "ok" : "failed"}</span>
          {typeof cmd.durationMs === "number" ? <span className="fx-muted"> · {secondsWord(Math.round(cmd.durationMs / 1000))}</span> : null}
        </span>
      ))}
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
  const diff = ch.diffUrl || c.outputs.pr?.filesUrl || (c.outputs.pr?.url ? `${c.outputs.pr.url}/files` : undefined);
  const group = (name: string, files: { path: string; bytes?: number }[], roleKey: string | null) => files.length ? (
    <span className="fx-out-col" data-group={name}>
      <span className="fx-out-sub">{name} <Maker roleKey={roleKey} /></span>
      {files.map((f) => (
        <span key={f.path} className="fx-file">
          <span className="fx-mono fx-trunc" title={f.path} data-user-text>{f.path}</span>
          {typeof f.bytes === "number" ? <span className="fx-muted"> · {f.bytes} bytes</span> : null}
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

function CommentsBody({ c, actor, onRefresh }: { c: Cockpit; actor: string; onRefresh: () => void }) {
  const items = c.outputs.comments.items;
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState("");
  if (!items.length) return <span className="fx-muted">None</span>;
  const act = async (cm: ReviewComment, status: "addressed" | "dismissed") => {
    setBusy(cm.id);
    setError("");
    const r = await resolveComment(c.run.id, cm.id, actor, status);
    setBusy(null);
    if (r.ok) { showToast("success", "Comment resolved"); notifyChanged("code-tasks"); onRefresh(); }
    else setError(r.error ?? "");
  };
  return (
    <span className="fx-out-col">
      {error ? <span className="fx-err" role="alert" data-user-text>{error}</span> : null}
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

/** The steps toggle's words: how it is running once it runs; how far it got while it boots. */
export function stepsWord(p: Pick<PreviewOutput, "status" | "steps" | "stepsDone" | "stepsTotal">): string {
  const serving = [...p.steps].reverse().find((x) => x.status === "ok");
  if (p.status === "running") return serving ? `Running · ${serving.label}` : "How it started";
  const finished = p.steps.filter((x) => x.status === "ok" || x.status === "skipped" || x.status === "failed").length;
  const total = p.stepsTotal ?? p.steps.length;
  return `${Math.min(finished || p.stepsDone || 0, total)} of ${plural(total, "step")}`;
}

function PreviewBody({ c, actor, onLogs, onRefresh }: { c: Cockpit; actor: string; onLogs: () => void; onRefresh: () => void }) {
  const p = c.outputs.preview;
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  // Pressed and not yet read back: it says so at once (walk D9, D10), not after the boot.
  const [starting, setStarting] = useState(false);
  // A library build has no live app: the press ran its tests instead (the old "Ran tests —
  // passed (library build)"), which is an answer, not an error.
  const [note, setNote] = useState("");
  const [showNote, setShowNote] = useState(false);
  const [showSteps, setShowSteps] = useState(false);
  const start = async () => {
    setBusy(true);
    setError("");
    setNote("");
    setStarting(true);
    showToast("success", "Preview starting");
    const form = new FormData();
    form.set("run_id", c.run.id);
    form.set("actor", actor);
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
  const canRun = ["pr_open", "merged", "deployed"].includes(c.state.state);
  const cannotRun = canRun ? undefined : c.state.state === "done_no_pr"
    ? "This build opened no PR, so there is no branch to preview"
    : "Runs once the build has opened its PR";
  const err = error ? <span className="fx-err" role="alert" title={error}>Couldn't start</span> : null;
  const passed = note ? (
    <span className="fx-out-col" data-testid="fx-preview-note">
      <button type="button" className="ag-link" aria-expanded={showNote} onClick={() => setShowNote((v) => !v)}
              title="A library or CLI has no live app: its tests ran instead">Tests passed · no live app {showNote ? "▴" : "▾"}</button>
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
      <GatedButton permission="preview:manage" type="button" className="ag-btn" disabled={busy} data-testid="fx-preview-restart" onClick={() => void start()}>Restart</GatedButton>
      {running ? <GatedButton permission="preview:manage" type="button" className="ag-link" disabled={busy} data-testid="fx-preview-stop" onClick={() => void stop()}>Stop</GatedButton> : null}
      <button type="button" className="ag-link" onClick={onLogs} data-testid="fx-preview-logs">Logs ▾</button>
      {p.expiresAt ? <span className="fx-muted" title={p.expiresAt}>Expires {localWith(p.expiresAt, { hour: "numeric", minute: "2-digit" })}</span> : null}
      {p.substrate ? <span className="fx-muted" title="Where the preview runs">{p.substrate}</span> : null}
      {typeof p.stepsDone === "number" && p.stepsTotal ? (
        <button type="button" className="ag-link" aria-expanded={showSteps} data-testid="fx-preview-steps"
                disabled={!p.steps.length} onClick={() => setShowSteps((v) => !v)}>
          {stepsWord(p)} {p.steps.length ? (showSteps ? "▴" : "▾") : ""}
        </button>
      ) : null}
      {p.error && !running ? <span className="fx-err" title={p.error}>Failed</span> : null}
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
              {passedOver ? <span className="fx-muted"> · passed over</span> : null}
              {x.durationMs ? <span className="fx-muted"> · {secondsWord(Math.round(x.durationMs / 1000))}</span> : null}
              {x.log ? <span className="fx-muted fx-trunc" title={x.log} data-user-text> · {x.log}</span> : null}
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
                  <span> · {whoName(x.role)}</span>
                  <span className="fx-muted" data-user-text> · {x.model || x.engine}</span>
                  {x.turns ? <span className="fx-muted"> · {plural(x.turns, "turn")}</span> : null}
                  {x.latencyMs >= 1000 ? <span className="fx-muted"> · {secondsWord(Math.round(x.latencyMs / 1000))}</span> : null}
                  {x.retries ? <span className="fx-muted"> · {plural(x.retries, "retry", "retries")}</span> : null}
                  {metered && x.usd ? <span> · {x.usd < 0.01 ? `$${x.usd.toFixed(3)}` : money(x.usd)}</span> : null}
                  {!x.ok ? <span className="fx-err fx-trunc" title={x.error ?? undefined} data-user-text> · {x.error ?? "failed"}</span> : null}
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
