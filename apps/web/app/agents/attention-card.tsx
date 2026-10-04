"use client";

/**
 * One thing that needs you, as a card you can act on (agents-home spec §4.2). Shared by
 * Agents › Live (density "card") and the Command Center (density "row"): one design, one source
 * (command_center.assemble().needs_you, extended by the live board).
 *
 * EVERY BUTTON IS THE SERVER'S. An approval draws exactly its `actions` (labels and effects as
 * sent), a failed step exactly the run's `answers`, readiness a `Fix ↗`. Approving in place is
 * allowed only when the server says `decide_inline` (a gate at risk tier ≤ 2 with nothing to
 * type and its preview's hash in hand); anything else is `Review ↗`, into Approvals. The press
 * goes through the same server action the Approvals inbox posts, with the same content hash,
 * so the engine refuses here exactly what it refuses there — and its sentence is shown.
 *
 * NEVER A GUESSED FACE. The attributed agent's face, or the BOSS glyph (setup, refusals) or
 * `You` (a gate on a run you started) when the server attributes none.
 */
import { CaretDown, CaretRight, SpinnerGap } from "@phosphor-icons/react";
import React, { useEffect, useId, useRef, useState } from "react";

import { resolveDecisionAction } from "../actions";
import { callAction } from "../../lib/call-action";
import { retryBuildAction } from "../factory/floor-actions";
import { WaiveSheet } from "../factory/waive-sheet";
import { showToast } from "../client-forms";
import { notifyChanged } from "../changed";
import { sendRunAnswer, type RunAction } from "../delivery-workflows-ui";
import type { AttentionAction, AttentionAnswer, AttentionItem } from "../../lib/agents-live-board";
import { useIdentityVersion } from "../../lib/use-agent-identities";
import { AgFace } from "./agent-face";
import { RunFace, nameOf } from "./run-face";
import {
  ago, decisionPatch, linkTo, localTime, navigate, plainClick, runPatch, useNow,
} from "./live-util";

const KIND_WORD: Record<string, string> = {
  workflow_gate: "Gate", general: "Gate", deploy_gate: "Deploy", pull_request: "Pull request",
  refusal: "Refusal",
};

/** The word for what kind of thing this is: `Gate`, `Deploy`, `Pull request`, `Refusal`, `Setup`. */
export function kindWord(item: Pick<AttentionItem, "kind" | "decisionKind">): string {
  if (item.kind === "readiness") return "Setup";
  if (item.kind === "failed_step") return "Failed";
  if (item.kind === "gate_override") return "Checks";
  if (item.kind === "budget_refused") return "Budget";
  return KIND_WORD[item.decisionKind ?? ""] ?? "Gate";
}

/** Who a card is about: the attributed agent, else the BOSS glyph or You — never a guess. */
export function cardWho(item: Pick<AttentionItem, "roleKey" | "kind" | "decisionKind" | "id">):
    { roleKey: string | null; variant: "agent" | "boss" | "you" } {
  if (item.roleKey) return { roleKey: item.roleKey, variant: "agent" };
  if (item.kind === "readiness" || item.kind === "budget_refused" || item.kind === "gate_override"
      || item.decisionKind === "refusal" || item.id.startsWith("ready:")) {
    return { roleKey: null, variant: "boss" };
  }
  return { roleKey: null, variant: "you" };
}

export type AttentionCardProps = {
  item: AttentionItem;
  projectId: string;
  actor: string;
  density?: "card" | "row";
  /** Pressed the `You` hop, or arrived on `?focus=`: scroll here, focus, pulse once. */
  focusKey?: number;
  /** Arrived while the page was open: rises in once. Present at first paint: does not. */
  enter?: boolean;
  /** Its face may move (the ring budget, spec §5.2). */
  still?: boolean;
  /** Decided (or answered) here: it leaves; re-read now. */
  onDone?: () => void;
  /** Refused, or decided elsewhere: it stays with the server's sentence; re-read now. */
  onRefresh?: () => void;
  /** For links that open in a new tab (presses navigate in place). */
  orgId?: string | null;
};

type Busy = null | string;

/** The cockpit of a build, as a console link (`?area=code_tasks&build=`). */
export const buildPatch = (runId: string) => ({ area: "code_tasks", build: runId, run: null, view: null, role: null });
const STUCK_KINDS = new Set(["failed_step", "gate_override", "budget_refused"]);

export function AttentionCard({ item, projectId, actor, density = "card", focusKey, enter, still, onDone, onRefresh, orgId }: AttentionCardProps) {
  useIdentityVersion();
  const titleId = useId();
  const hintId = useId();
  const box = useRef<HTMLDivElement>(null);
  const now = useNow(30_000);
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState<Busy>(null);
  const [error, setError] = useState("");
  const [confirming, setConfirming] = useState<AttentionAction | null>(null);
  const [leaving, setLeaving] = useState(false);
  const [pulse, setPulse] = useState(false);
  const [waiving, setWaiving] = useState(false);

  useEffect(() => {
    if (!focusKey || !box.current) return undefined;
    box.current.scrollIntoView?.({ block: "center", behavior: "smooth" });
    box.current.focus({ preventScroll: true });
    setPulse(true);
    const t = window.setTimeout(() => setPulse(false), 600);
    return () => window.clearTimeout(t);
  }, [focusKey]);

  const who = cardWho(item);
  const inline = item.kind === "approval" && item.decideInline
    && item.actions.some((a) => a.action === "approve" || a.action === "reject");
  const direct = inline ? item.actions.filter((a) => !a.needs.length && (a.action === "approve" || a.action === "reject")) : [];
  const decisionId = item.decisionId ?? (item.kind === "approval" ? item.id : undefined);
  const run = item.run;
  const k = run?.stepIndex;
  const subject = run ? (run.subject || "Run") : "";

  function finish(message: string) {
    showToast("success", message);
    setLeaving(true);
    notifyChanged("decisions");
    window.setTimeout(() => onDone?.(), 200);
  }

  async function decide(action: AttentionAction) {
    if (!decisionId || busy) return;
    setConfirming(null);
    setBusy(action.action);
    setError("");
    const form = new FormData();
    form.set("decision_id", decisionId);
    form.set("project_id", projectId);
    form.set("actor", actor);
    form.set("action", action.action);
    if (item.contentHash) form.set("content_hash", item.contentHash);
    if (run?.id) form.set("workflow_run_id", run.id);
    try {
      const result = await callAction(resolveDecisionAction)(form);
      if (result.ok) {
        finish(action.action === "approve"
          ? (run ? `Approved · ${subject} continues` : "Approved")
          : (run ? "Rejected · run stopped" : "Rejected"));
      } else {
        setError(result.error ?? "");
        onRefresh?.();
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }

  async function answer(a: AttentionAnswer) {
    if (!run?.id || busy || !a.enabled) return;
    setBusy(a.key);
    setError("");
    try {
      const r = await sendRunAnswer(projectId, actor, run.id,
        { key: a.key, label: a.label, enabled: a.enabled, reason: a.reason ?? null,
          step_id: a.stepId ?? run.stepId } as RunAction);
      if (r.ok) {
        finish(a.key === "retry_step" ? `Retrying step ${k ?? ""}`.trim()
          : a.key === "skip_step" ? `Skipped step ${k ?? ""}`.trim() : a.label);
      } else {
        setError(r.error ?? "");
        onRefresh?.();
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }

  async function retryBuild() {
    const b = item.build;
    if (!b || busy || !b.retryable) return;
    setBusy("retry");
    setError("");
    const form = new FormData();
    form.set("run_id", b.runId);
    form.set("project_id", projectId);
    form.set("actor", actor);
    try {
      const r = await callAction(retryBuildAction)(form);
      if (r.ok) finish(r.attempt ? `Attempt ${r.attempt} started` : "Retrying");
      else { setError(r.error ?? ""); onRefresh?.(); }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }

  function press(a: AttentionAction) {
    if (a.action === "reject") setConfirming(a); else void decide(a);
  }

  function onKey(e: React.KeyboardEvent) {
    if (!inline || e.altKey || e.metaKey || e.ctrlKey) return;
    const t = e.target as HTMLElement;
    if (t.closest("input, textarea, select")) return;
    const key = e.key.toLowerCase();
    const approve = direct.find((a) => a.action === "approve");
    const reject = direct.find((a) => a.action === "reject");
    if (key === "a" && approve) { e.preventDefault(); press(approve); }
    if (key === "r" && reject) { e.preventDefault(); press(reject); }
  }

  const approveFx = item.actions.find((a) => a.action === "approve")?.effect;
  const rejectFx = item.actions.find((a) => a.action === "reject")?.effect;
  const named = who.roleKey ? nameOf(who.roleKey).name : who.variant === "boss" ? "BOSS" : "You";
  const age = ago(item.at, now);
  const runHref = run?.id ? linkTo(runPatch(run.id), orgId) : "";
  const build = item.build;
  const buildHref = build ? linkTo(buildPatch(build.runId), orgId) : "";
  const stuck = STUCK_KINDS.has(item.kind);
  const planHref = build?.planUrl && /^https?:/i.test(build.planUrl) ? build.planUrl : buildHref;

  return (
    <div ref={box} className="ag-att" role="group" aria-labelledby={titleId} tabIndex={-1}
         aria-describedby={inline ? hintId : undefined}
         data-testid={`ag-att-${item.id}`} data-att-card data-att-id={item.id} data-kind={item.kind}
         data-density={density} data-enter={enter || undefined} data-leaving={leaving || undefined}
         data-pulse={pulse || undefined} data-still={still || undefined} onKeyDown={onKey}>
      <div className="ag-att-head">
        <span className="ag-att-face" title={named}>
          {who.roleKey
            ? <AgFace roleKey={who.roleKey} size={density === "row" ? 24 : 32}
                      state={stuck ? "stuck" : "needs_you"} />
            : <RunFace roleKey={null} variant={who.variant} size={density === "row" ? 24 : 32}
                       state={stuck ? "stuck" : "needs_you"} standalone />}
        </span>
        <span id={titleId} className="ag-att-title ag-trunc" title={item.title} data-user-text>{item.title}</span>
        {age ? <span className="ag-att-age" title={`Waiting since ${localTime(item.at)}`}>{age}</span> : null}
      </div>

      <div className="ag-att-context">
        {build && !run?.id ? (
          <a className="ag-att-run" href={buildHref} data-testid="ag-att-build"
             onClick={(e) => { if (plainClick(e)) { e.preventDefault(); navigate(buildPatch(build.runId)); } }}>
            <span data-user-text>{build.app}</span>
            {build.where ? <span className="ag-att-step"> · {build.where}</span> : null}
          </a>
        ) : run?.id ? (
          <a className="ag-att-run" href={runHref}
             onClick={(e) => { if (plainClick(e)) { e.preventDefault(); navigate(runPatch(run.id)); } }}>
            <span data-user-text>{subject}</span>
            {k && run.stepCount ? <span className="ag-att-step"> · step {k} of {run.stepCount}</span> : null}
          </a>
        ) : <span className="ag-att-kind">{kindWord(item)}</span>}
        {run?.id && item.kind === "failed_step" ? <span className="ag-att-failed"> · Failed</span> : null}
        {run?.id && item.kind === "approval" ? <span className="ag-att-kind"> · {kindWord(item)}</span> : null}
      </div>

      {density === "row" && item.detail ? (
        <span className="ag-att-detail ag-trunc" title={item.detail} data-user-text>{item.detail}</span>
      ) : null}

      {error ? <span className="ag-att-error" role="alert" data-testid="ag-att-error" data-user-text>{error}</span> : null}

      <div className="ag-att-actions">
        {direct.map((a) => (
          <button key={a.action} type="button" className="ag-btn" data-tone={a.tone || undefined}
                  data-testid={`ag-att-${a.action}`} title={a.effect || undefined}
                  disabled={busy !== null} aria-busy={busy === a.action || undefined}
                  onClick={() => press(a)}>
            {busy === a.action ? <SpinnerGap size={12} className="ag-spin" aria-hidden /> : null}
            <span data-user-text>{a.label}</span>
          </button>
        ))}
        {item.kind === "approval" && !inline && decisionId ? (
          <a className="ag-btn" href={linkTo(decisionPatch(decisionId), orgId)} data-testid="ag-att-review"
             title="Needs a closer look in Approvals"
             onClick={(e) => { if (plainClick(e)) { e.preventDefault(); navigate(decisionPatch(decisionId)); } }}>
            Review ↗
          </a>
        ) : null}
        {item.kind === "approval" && build?.prUrl ? (
          <a className="ag-btn" href={build.prUrl} target="_blank" rel="noreferrer" data-testid="ag-att-open-pr">Open PR ↗</a>
        ) : null}
        {item.kind === "approval" && build?.previewUrl ? (
          <a className="ag-btn" href={build.previewUrl} target="_blank" rel="noreferrer" data-testid="ag-att-open-preview">Open preview ↗</a>
        ) : null}
        {(item.kind === "gate_override" || item.kind === "budget_refused") && build ? (
          <button type="button" className="ag-btn" data-testid="ag-att-retry"
                  disabled={!build.retryable || busy !== null} title={!build.retryable ? build.retryReason : undefined}
                  aria-busy={busy === "retry" || undefined} onClick={() => void retryBuild()}>
            {busy === "retry" ? <SpinnerGap size={12} className="ag-spin" aria-hidden /> : null}
            Retry
          </button>
        ) : null}
        {item.kind === "gate_override" && build ? (
          <button type="button" className="ag-btn" data-testid="ag-att-waive" disabled={busy !== null}
                  title="Open the PR anyway. The merge still needs you." onClick={() => setWaiving(true)}>
            Waive…
          </button>
        ) : null}
        {item.kind === "budget_refused" ? (
          <a className="ag-btn" data-testid="ag-att-raise-budget" href={linkTo({ area: "budget", view: null, role: null }, orgId)}
             title="Raise the build budget in Cost"
             onClick={(e) => { if (plainClick(e)) { e.preventDefault(); navigate({ area: "budget", view: null, role: null }); } }}>
            Raise budget ↗
          </a>
        ) : null}
        {item.kind === "failed_step" ? item.answers.map((a) => (
          <button key={a.key} type="button" className="ag-btn" data-testid={`ag-att-answer-${a.key}`}
                  disabled={!a.enabled || busy !== null} title={!a.enabled ? a.reason : undefined}
                  aria-busy={busy === a.key || undefined} onClick={() => void answer(a)}>
            {busy === a.key ? <SpinnerGap size={12} className="ag-spin" aria-hidden /> : null}
            <span data-user-text>{a.label}</span>
          </button>
        )) : null}
        {item.kind === "readiness" ? (
          <a className="ag-btn" data-testid="ag-att-fix" href={linkTo({ area: item.fix?.area || "setup", view: null, role: null }, orgId)}
             title={item.whatToDo || undefined}
             onClick={(e) => { if (plainClick(e)) { e.preventDefault(); navigate({ area: item.fix?.area || "setup", view: null, role: null }); } }}>
            Fix ↗
          </a>
        ) : null}
        <button type="button" className="ag-link ag-att-more" aria-expanded={open} data-testid="ag-att-details"
                onClick={() => setOpen((v) => !v)}>
          Details {open ? <CaretDown size={10} weight="bold" aria-hidden /> : <CaretRight size={10} weight="bold" aria-hidden />}
        </button>
        {/* Read by aria-describedby, never shown: hidden text can still describe. */}
        {inline ? <span id={hintId} hidden>Press A to approve or R to reject.</span> : null}
      </div>

      {confirming ? (
        <div className="ag-att-confirm" role="alertdialog" aria-label="Reject this?" data-testid="ag-att-confirm">
          <span>Reject this?</span>
          <button type="button" className="ag-btn" data-tone="reject" data-testid="ag-att-confirm-reject"
                  title={confirming.effect || undefined} onClick={() => void decide(confirming)}>Reject</button>
          <button type="button" className="ag-link" onClick={() => setConfirming(null)}>Cancel</button>
        </div>
      ) : null}

      {open ? (
        <dl className="ag-att-details" data-testid="ag-att-details-body">
          {item.detail ? <><dt>Why</dt><dd data-user-text>{item.detail}</dd></> : null}
          {approveFx ? <><dt>If approved</dt><dd data-user-text>{approveFx}</dd></> : null}
          {rejectFx ? <><dt>If rejected</dt><dd data-user-text>{rejectFx}</dd></> : null}
          {run?.id ? (
            <><dt>Blocks</dt><dd>
              <a href={runHref} onClick={(e) => { if (plainClick(e)) { e.preventDefault(); navigate(runPatch(run.id)); } }}>
                <span data-user-text>{subject}</span>
              </a>
              {k && run.stepCount ? <span> · step {k} of {run.stepCount}</span> : null}
            </dd></>
          ) : null}
          {item.preview ? <><dt>Preview</dt><dd data-user-text>{item.preview}</dd></> : null}
          {build && build.failing.length ? (
            <><dt>Failed</dt><dd>{build.failing.map((g) => g.label).join(" · ")}</dd></>
          ) : null}
          {build && item.kind === "approval" && item.decisionKind !== "pull_request" ? (
            <><dt className="ag-sr">Plan</dt><dd>
              <a className="ag-link" href={planHref} data-testid="ag-att-open-plan"
                 onClick={(e) => { if (planHref === buildHref && plainClick(e)) { e.preventDefault(); navigate(buildPatch(build.runId)); } }}>
                Open plan ↗
              </a>
            </dd></>
          ) : null}
          {typeof item.riskTier === "number" ? (
            <><dt>Risk</dt><dd><span title="Higher tiers need a closer look before approving">Tier {item.riskTier}</span></dd></>
          ) : null}
          {decisionId ? (
            <><dt className="ag-sr">Approvals</dt><dd>
              <a className="ag-link" href={linkTo(decisionPatch(decisionId), orgId)}
                 onClick={(e) => { if (plainClick(e)) { e.preventDefault(); navigate(decisionPatch(decisionId)); } }}>
                Open in Approvals ↗
              </a>
            </dd></>
          ) : null}
        </dl>
      ) : null}

      {waiving && build ? (
        <WaiveSheet runId={build.runId} app={build.app} failing={build.failing} projectId={projectId} actor={actor}
                    onClose={() => setWaiving(false)} onDone={() => { setLeaving(true); window.setTimeout(() => onDone?.(), 200); }} />
      ) : null}
    </div>
  );
}
