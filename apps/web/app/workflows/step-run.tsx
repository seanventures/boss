"use client";

/**
 * A run step's panel, led by the step's RUN rather than by the agent (operator, 2026-10-04:
 * "too much info about the agent … more information about that workflow step's run").
 *
 * - `StepDoneBy`: one line, "Done by <face> <name> · $cost · model", with the full agent card
 *   (StepSignedBy: handed by, how, why, scope, what it said) one press away.
 * - `StepActivity`: the step's own latest log rows and "Step log", a deep link straight to the
 *   activity log scoped to this step (`?area=activity&wfr=…&step=…`, see /v1/activity step_id).
 * - `useStepActivity`: one read of that scope, shared by both, so the cost and model on the
 *   Done-by line come from the same rows the Activity list shows.
 */
import React, { useEffect, useState } from "react";

import type { Act } from "../../lib/agent-world-types";
import { fetchActivityLog } from "../../lib/activity-log-client";
import type { ActivityLogItem } from "../../lib/activity-log";
import { consoleHref } from "../url-intent";
import { laneAgentOf, costLabel } from "../agents/chain-model";
import { nameOf, RunFace } from "../agents/run-face";
import { StepSignedBy } from "../agents/step-signed-by";
import { RanAs } from "../step-agents-ui";
import { useIdentityVersion } from "../../lib/use-agent-identities";

/** Rows shown in the panel; the rest are one click away in the step log. */
export const STEP_ACTIVITY_ROWS = 5;

/** The activity log, scoped to one step of one workflow run. Keeps the org and project. */
export function stepLogHref(search: string, workflowRunId: string, stepId: string): string {
  return consoleHref(search, { area: "activity", wfr: workflowRunId, step: stepId });
}

export type StepActivityState = {
  rows: ActivityLogItem[];
  loaded: boolean;
  /** The model the step's work ran on, from its model calls. */
  model: string;
  /** What the step's model calls cost, in dollars. */
  cost: number;
};

const EMPTY: StepActivityState = { rows: [], loaded: false, model: "", cost: 0 };

/** Summarise the step's log: the rows worth a glance (verbose turns are the log's job), the
 *  model it ran on and what its model calls cost. Pure, so it is tested without a fetch. */
export function summariseStepActivity(items: ActivityLogItem[]): Omit<StepActivityState, "loaded"> {
  const calls = items.filter((i) => i.source === "llm_call");
  const model = String(calls.find((c) => typeof c.data?.model === "string" && c.data.model)?.data?.model ?? "");
  const cost = calls.reduce((sum, c) => sum + (Number(c.data?.cost_usd) || 0), 0);
  return { rows: items.filter((i) => i.level !== "verbose"), model, cost };
}

/** One read of the step's log; re-read while the step is live, never in a hidden tab. */
export function useStepActivity(projectId: string, workflowRunId: string | undefined, stepId: string,
                                live: boolean, version: string): StepActivityState {
  const [state, setState] = useState<StepActivityState>(EMPTY);
  useEffect(() => {
    if (!workflowRunId) { setState({ ...EMPTY, loaded: true }); return; }
    let alive = true;
    let timer: number | undefined;
    const load = async () => {
      const log = await fetchActivityLog(projectId, {
        workflow_run_id: workflowRunId, step_id: stepId, limit: 100,
      });
      if (alive) setState({ ...summariseStepActivity(log.items ?? []), loaded: true });
    };
    const tick = () => {
      timer = window.setTimeout(async () => {
        if (!alive) return;
        if (typeof document === "undefined" || document.visibilityState !== "hidden") await load();
        if (alive) tick();
      }, 5000);
    };
    void load();
    if (live) tick();
    return () => { alive = false; if (timer) window.clearTimeout(timer); };
  }, [projectId, workflowRunId, stepId, live, version]);
  return state;
}

function clock(iso: string): string {
  const t = Date.parse(iso);
  if (!Number.isFinite(t)) return "";
  const d = new Date(t);
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

/** "Done by <face> <name> · $0.03 · model", and the agent's whole card behind one press. */
export function StepDoneBy({ act, approval, output, phase, projectId, viewer, failure, question,
                             cost, model, assigned, onAsk, stepId }: {
  act: Act | null; approval?: Act | null; output: unknown;
  /** phaseOf(step): `ahead` reads Assigned to, `now` Doing, anything after it Done by. */
  phase: string; projectId: string; viewer: string; failure?: string | null; question: string;
  /** Cost from the step's own model calls, when the work record has none. */
  cost: number; model: string;
  /** Who the step will go to, before anyone has run it. */
  assigned?: string | null;
  onAsk(): void; stepId: string;
}) {
  useIdentityVersion();
  const [open, setOpen] = useState(false);
  const agent = laneAgentOf(output, act, viewer);
  const roleKey = agent?.roleKey ?? (agent ? null : assigned || null);
  const variant = agent?.variant ?? "agent";
  if (!agent && (!roleKey || phase === "skipped")) return null;
  const named = nameOf(roleKey, variant);
  const verb = phase === "ahead" ? "Assigned to" : phase === "now" ? "Doing" : "Done by";
  const spent = act?.costUsd || cost;
  const hasCard = !!act || !!(output && typeof output === "object" && (output as { ran_as?: unknown }).ran_as);
  return (
    <div className="wf-doneby" data-testid="dw-step-doneby">
      <div className="wf-doneby-line">
        <span className="wf-doneby-verb">{verb}</span>
        <RunFace roleKey={roleKey} variant={variant} size={20} state={agent?.state ?? "idle"} />
        <strong className="wf-doneby-name" data-testid="dw-step-doneby-name"
                data-tip={named.role !== named.name ? named.role : undefined}>{named.name}</strong>
        {spent ? <span className="wf-doneby-fact" data-testid="dw-step-cost">{costLabel(spent)}</span> : null}
        {model ? <span className="wf-doneby-fact" data-testid="dw-step-model" data-user-text>{model}</span> : null}
        <span className="wf-doneby-acts">
          {roleKey ? (
            <button type="button" className="wf-link" data-testid="dw-step-ask"
                    data-tip={`Ask ${named.name} about this step`} onClick={onAsk}>
              Ask
            </button>
          ) : null}
          {hasCard ? (
            <button type="button" className="wf-link" aria-expanded={open} data-testid="dw-step-agent-toggle"
                    data-tip="Who did it and how it reached them" onClick={() => setOpen((o) => !o)}>
              {open ? "Hide agent" : "Agent"}
            </button>
          ) : null}
        </span>
      </div>
      {open ? (
        <div className="wf-doneby-card" data-testid="dw-step-agent-card">
          {act ? (
            <StepSignedBy act={act} approval={approval} projectId={projectId} viewer={viewer}
                          failure={failure} question={question} />
          ) : (
            <RanAs output={output} stepId={stepId} />
          )}
        </div>
      ) : null}
    </div>
  );
}

/** The step's latest log rows and the way into its whole log. */
export function StepActivity({ state, href, live }: { state: StepActivityState; href: string; live: boolean }) {
  const rows = state.rows.slice(0, STEP_ACTIVITY_ROWS);
  return (
    <section className="wf-step-sec" data-testid="dw-step-activity" aria-label="Activity">
      <header className="wf-step-sec-head">
        <span className="eyebrow">Activity</span>
        {state.rows.length > rows.length ? (
          <span className="wf-step-sec-count">{state.rows.length}</span>
        ) : null}
        <a className="wf-link wf-step-sec-link" href={href} data-testid="dw-step-log">Step log →</a>
      </header>
      {!state.loaded ? (
        <span className="wf-step-empty">Reading…</span>
      ) : rows.length ? (
        <ol className="wf-step-log">
          {rows.map((r) => (
            <li key={r.id} data-level={r.level} data-testid="dw-step-log-row">
              <time dateTime={r.createdAt}>{clock(r.createdAt)}</time>
              <span className="wf-step-log-title" data-tip={r.body || r.title} data-user-text>{r.title || r.kind}</span>
            </li>
          ))}
        </ol>
      ) : (
        <span className="wf-step-empty">{live ? "Nothing logged yet" : "Nothing logged"}</span>
      )}
    </section>
  );
}

/** A titled block of the step panel (Output). */
export function StepSection({ title, testId, children, empty }: {
  title: string; testId: string; children?: React.ReactNode; empty?: string;
}) {
  return (
    <section className="wf-step-sec" data-testid={testId} aria-label={title}>
      <header className="wf-step-sec-head"><span className="eyebrow">{title}</span></header>
      {children ?? <span className="wf-step-empty">{empty}</span>}
    </section>
  );
}

/** "Started 18:01 · Attempt 2": when it began and whether it is a retry. */
export function stepFacts(step: { started_at?: string | null; attempt?: number }): string[] {
  const out: string[] = [];
  if (step.started_at && clock(step.started_at)) out.push(`Started ${clock(step.started_at)}`);
  if ((step.attempt ?? 0) > 1) out.push(`Attempt ${step.attempt}`);
  return out;
}
