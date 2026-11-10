"use client";

/**
 * A DEPLOY YOU CAN WATCH (BOSSAPP-932): on a Deployments environment card, while its deploy runs,
 * the seven phases (prepare → upload → environment → switch → install → health → smoke) live —
 * each with its state, elapsed time and its own log tail behind a press. On failure: the reason
 * in words, Fix (hands it to the project's agent) and Roll back (only when the engine offers it).
 *
 * One stream, reused: Infra's deploy detail (/v1/infra/deploys/stack:<id>, useDeployDetail),
 * polled every 3 s while the deploy moves and never after — the same read Infra's drawer and the
 * workflow's deploy step use. The phases themselves are the engine's (deploy_truth.progress).
 */
import React, { useState } from "react";
import { ArrowClockwise, ArrowCounterClockwise, CaretDown, CaretRight, Check, CircleNotch, Minus, Wrench, X } from "@phosphor-icons/react";

import { GatedButton } from "../../components/gated";
import { useDeployDetail } from "../../lib/infra-view";
import { parseDeployProgress, pxHref, type DeployPhaseRow, type DeployProgress, type Environment } from "../../lib/project-experience";
import { RUN_ANSWER_PERMISSION, sendRunAnswer } from "../delivery-workflows-ui";
import { rollbackDeploy } from "../infra/infra-actions";
import { Said } from "./kit";

/** "42s", "3m 05s": a phase's elapsed time in one short token. */
export function elapsedWords(s: number | null | undefined): string {
  if (s === null || s === undefined || !Number.isFinite(s)) return "";
  const t = Math.max(0, Math.round(s));
  return t < 60 ? `${t}s` : `${Math.floor(t / 60)}m ${String(t % 60).padStart(2, "0")}s`;
}

const PHASE_WORD: Record<DeployPhaseRow["status"], string> = {
  done: "Done", running: "Running", failed: "Failed", pending: "Next", skipped: "Not in this deploy",
};

function PhaseIcon({ status }: { status: DeployPhaseRow["status"] }) {
  if (status === "done") return <Check size={11} weight="bold" aria-hidden />;
  if (status === "failed") return <X size={11} weight="bold" aria-hidden />;
  if (status === "running") return <CircleNotch size={11} weight="bold" className="px-spin" aria-hidden />;
  return <Minus size={11} aria-hidden />;
}

/** The deploy this card follows: its stack deployment while it runs or after it failed. */
export function followedDeploy(e: Environment): string | null {
  const d = e.deploy;
  if (!d || !d.attempt || d.attempt.kind !== "stack") return null;
  return d.state === "deploying" || d.state === "failed" ? `stack:${d.attempt.id}` : null;
}

export function DeployLive({ e, projectId, actor, onFix, onChanged }: {
  e: Environment; projectId: string; actor: string;
  /** Hand the failure to the project's agent (the captain drawer, with the question). */
  onFix: (question: string) => void;
  onChanged?: () => void;
}) {
  const ref = followedDeploy(e);
  const read = useDeployDetail(projectId, ref);
  const [open, setOpen] = useState<string | null>(null);
  const [said, setSaid] = useState<{ text: string; ok: boolean }>({ text: "", ok: true });
  const [busy, setBusy] = useState(false);
  if (!ref || !e.deploy) return null;
  const fresh: DeployProgress | null = parseDeployProgress(read.data?.progress);
  const prog = fresh ?? e.deploy.progress;
  const failed = read.data ? read.data.row.state === "failed" || read.data.failure !== null
    : e.deploy.state === "failed";
  const reason = (read.data?.failure?.error || e.deploy.reason || "").trim();
  const canRollback = !!read.data?.row.can.rollback;
  const where = `${e.app || "the app"} on ${e.label}`;
  const fix = () => onFix(`The deploy of ${where} failed${prog?.failed_phase ? ` at ${prog.failed_phase}` : ""}: ${reason || "no reason recorded"}. Find the cause and fix it, then deploy again.`);
  const rollBack = async () => {
    setBusy(true);
    const r = await rollbackDeploy(projectId, ref, actor);
    setBusy(false);
    setSaid(r.ok ? { text: "Roll back planned · approve it in Needs you.", ok: true } : { text: `Not rolled back: ${r.error}`, ok: false });
    if (r.ok) { read.refresh(); onChanged?.(); }
  };
  const k = e.key;
  return (
    <div className="px-deploy-live" data-state={failed ? "failed" : "running"} data-testid={`px-env-live-${k}`}>
      {prog && prog.total ? (
        <div className="px-row px-wrap" style={{ gap: 6 }}>
          <b data-testid={`px-env-live-count-${k}`}>{failed ? `Failed at ${prog.failed_phase || "a step"}` : `Step ${prog.index} of ${prog.total}`}</b>
          <span className="px-faint">{failed ? "" : prog.phases.find((p) => p.key === prog.current)?.label ?? ""}</span>
        </div>
      ) : null}
      <ol className="px-deploy-phases" aria-label={`Deploy steps of ${where}`}>
        {(prog?.phases ?? []).filter((p) => p.status !== "skipped").map((p) => {
          const shown = open === p.key;
          return (
            <li key={p.key} data-s={p.status} data-testid={`px-env-phase-${k}-${p.key}`}>
              <button type="button" className="px-deploy-phase" aria-expanded={shown} disabled={!p.log}
                      title={p.log ? `${p.label}: ${PHASE_WORD[p.status]} — show its log` : `${p.label}: ${PHASE_WORD[p.status]}`}
                      onClick={() => setOpen(shown ? null : p.key)}>
                <span className="px-ck-b" aria-hidden><PhaseIcon status={p.status} /></span>
                <span>{p.label}</span>
                <span className="px-faint">{PHASE_WORD[p.status]}</span>
                <span className="px-spacer" />
                <span className="px-mono px-faint" data-testid={`px-env-phase-time-${k}-${p.key}`}>{elapsedWords(p.elapsed_s)}</span>
                {p.log ? (shown ? <CaretDown size={10} aria-hidden /> : <CaretRight size={10} aria-hidden />) : null}
              </button>
              {shown && p.log ? <pre className="px-deploy-log" data-testid={`px-env-log-${k}-${p.key}`}>{p.log}</pre> : null}
            </li>
          );
        })}
      </ol>
      {failed ? (
        <div className="px-deploy-fail" role="alert" data-testid={`px-env-fail-${k}`}>
          <span><b>Last deploy failed.</b> {reason}</span>
          <div className="px-row px-wrap" style={{ gap: 6 }}>
            <button type="button" className="px-btn" data-size="s" data-kind="primary" onClick={fix} data-testid={`px-env-fix-${k}`}
                    title="Hand this failure to the project's agent: it finds the cause, fixes it and deploys again">
              <Wrench size={12} aria-hidden /> Fix</button>
            {canRollback ? (
              <GatedButton permission="deployment:rollback" projectId={projectId} className="px-btn" data-size="s" disabled={busy}
                           data-testid={`px-env-rollback-${k}`} title="Plan the last good release again (asks for approval like any deploy)" onClick={rollBack}>
                <ArrowCounterClockwise size={12} aria-hidden /> Roll back</GatedButton>
            ) : null}
          </div>
          <Said text={said.text} tone={said.ok ? "green" : "red"} />
        </div>
      ) : null}
    </div>
  );
}

/**
 * A DEPLOY THAT FAILED BEFORE IT HAD A RECORD (engine deploy_truth.step_failures): the workflow's
 * deploy step stopped while it resolved the source or planned, so there is no deploy to follow —
 * and the card used to say "Not deployed yet". It says "Last deploy failed: <reason>" with Fix
 * (the project's agent) and Retry (the run's own retry-step, the run page's Retry), and links the run.
 */
export function DeployStepFailed({ projectId, actor, where, reason, runId, stepId, runHref, onFix, onChanged, testid }: {
  projectId: string; actor: string; where: string; reason: string;
  runId: string; stepId: string; runHref: string;
  onFix: (question: string) => void; onChanged?: () => void; testid: string;
}) {
  const [said, setSaid] = useState<{ text: string; ok: boolean }>({ text: "", ok: true });
  const [busy, setBusy] = useState(false);
  const fix = () => onFix(`The deploy of ${where} failed before it started: ${reason || "no reason recorded"}. Find the cause and fix it, then deploy again.`);
  const retry = async () => {
    setBusy(true);
    try {
      const r = await sendRunAnswer(projectId, actor, runId, { key: "retry_step", label: "Retry", enabled: true, reason: null, step_id: stepId });
      setSaid(r.ok ? { text: "Retrying the deploy step.", ok: true } : { text: `Not retried: ${r.error || "the run refused it"}`, ok: false });
      if (r.ok) onChanged?.();
    } catch (err) {
      setSaid({ text: `Not retried: ${err instanceof Error ? err.message : String(err)}`, ok: false });
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="px-deploy-live" data-state="failed" data-testid={testid}>
      <div className="px-deploy-fail" role="alert">
        <span><b>Last deploy failed:</b> {reason}</span>
        <div className="px-row px-wrap" style={{ gap: 6 }}>
          <button type="button" className="px-btn" data-size="s" data-kind="primary" onClick={fix} data-testid={`${testid}-fix`}
                  title="Hand this failure to the project's agent: it finds the cause, fixes it and deploys again">
            <Wrench size={12} aria-hidden /> Fix</button>
          {runId && stepId ? (
            <GatedButton permission={RUN_ANSWER_PERMISSION} projectId={projectId} className="px-btn" data-size="s" disabled={busy}
                         data-testid={`${testid}-retry`} title="Run the workflow's deploy step again" onClick={retry}>
              <ArrowClockwise size={12} aria-hidden /> Retry</GatedButton>
          ) : null}
          {runHref ? <a className="px-more" href={pxHref(runHref)} data-testid={`${testid}-run`}>Open the run</a> : null}
        </div>
        <Said text={said.text} tone={said.ok ? "green" : "red"} />
      </div>
    </div>
  );
}
