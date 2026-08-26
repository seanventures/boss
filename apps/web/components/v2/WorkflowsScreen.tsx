"use client";

// esbuild uses the classic JSX runtime here (no vitest config, tsconfig jsx=preserve),
// so a component rendered in a test must have React in scope -- see app/previews-ui.tsx.
import React from "react";

import type { V2Workflow, WorkflowsView } from "../../lib/v2/platform";

/**
 * Workflows — the processes this company runs, and where each one stops for a human.
 *
 * NO VERSION CONTROL, ON PURPOSE
 * ------------------------------
 * The reference offers "Deploy version" and a version number. BOSS keeps one active spec per
 * process, so that button would do nothing. The screen says so instead — "not versioned" is
 * more useful than a fabricated 1.3 beside a control that cannot act.
 */

export function WorkflowsScreen({ view }: { view: WorkflowsView }) {
  if (!view.apiReachable) {
    return (
      <section className="v2-screen" data-testid="v2-workflows-unreachable">
        <h1 className="v2-screen-title">Workflows</h1>
        <p className="v2-empty">
          Could not reach the control plane
          {view.blockedReason ? ` (${view.blockedReason})` : ""}. An empty list would read as a
          company with no processes.
        </p>
      </section>
    );
  }

  // Only countable when the spec records gating at all. Counting `=== true` over a spec that
  // never mentions gates yields 0, which reads as "nothing stops for you" — an assertion the
  // data does not support.
  const gates = view.gates_recorded
    ? view.workflows.reduce((n, w) => n + w.stages.filter((s) => s.gated === true).length, 0)
    : null;

  return (
    <section className="v2-screen" data-testid="v2-workflows">
      <header className="v2-screen-head">
        <h1 className="v2-screen-title">Workflows</h1>
        <p className="v2-screen-sub">
          {view.workflows.length} process{view.workflows.length === 1 ? "" : "es"}
          {gates !== null && gates > 0
            ? ` · ${gates} stage${gates === 1 ? " stops" : "s stop"} for you`
            : ""}
        </p>
      </header>

      {/* Said once, rather than drawing a version control that cannot act. */}
      <p className="v2-org-isolated-note" data-testid="v2-workflows-versioning">
        {view.version_note}
      </p>

      {/* Said when the spec has no gating vocabulary, so the screen never implies that nothing
          stops for a human when it simply does not know. */}
      {!view.gates_recorded && view.workflows.length > 0 ? (
        <p className="v2-org-isolated-note" data-testid="v2-workflows-gates-unknown">
          {view.gates_note}
        </p>
      ) : null}

      {view.workflows.length === 0 ? (
        <p className="v2-empty" data-testid="v2-workflows-none">
          No processes are defined for this company yet.
        </p>
      ) : (
        <ul className="v2-cards" data-testid="v2-workflows-list">
          {view.workflows.map((w) => (
            <WorkflowCard key={w.id} w={w} />
          ))}
        </ul>
      )}
    </section>
  );
}

function WorkflowCard({ w }: { w: V2Workflow }) {
  const status = w.running_now > 0 ? "RUNNING" : w.runs_30d > 0 ? "DONE" : "IDLE";
  return (
    <li className="v2-card" data-testid={`v2-workflow-${w.key}`} data-status={status}>
      <div className="v2-card-head">
        <span className="v2-card-name">{w.name}</span>
        <span className="v2-card-role">{w.key}</span>
      </div>

      {w.description ? <p className="v2-card-task">{w.description}</p> : null}

      {/* The stages, in order, with the gated ones marked — that is what an operator needs
          from this screen: where the process will stop and wait for them. */}
      {w.stages.length === 0 ? (
        <p className="v2-card-task" data-testid={`v2-workflow-nostages-${w.key}`}>
          This process has no stages recorded in its spec.
        </p>
      ) : (
        <ol className="v2-stages" data-testid={`v2-workflow-stages-${w.key}`}>
          {w.stages.map((s) => (
            <li
              key={s.key}
              className="v2-stage"
              data-gated={s.gated}
              data-testid={`v2-stage-${w.key}-${s.key}`}
            >
              <span className="v2-stage-name">{s.action || s.key}</span>
              {s.role ? <span className="v2-stage-role">{s.role}</span> : null}
              {s.gated === true ? (
                <span className="v2-stage-gate" data-testid={`v2-stage-gated-${w.key}-${s.key}`}>
                  waits for you
                </span>
              ) : null}
            </li>
          ))}
        </ol>
      )}

      <div className="v2-card-meta">
        <span>
          <b>{w.runs_30d}</b> runs / 30d
        </span>
        {w.running_now > 0 ? (
          <span data-testid={`v2-workflow-running-${w.key}`}>
            <b>{w.running_now}</b> running now
          </span>
        ) : null}
        {w.last_run_at ? (
          <span>last {w.last_run_at.slice(0, 10)}</span>
        ) : (
          <span data-testid={`v2-workflow-neverrun-${w.key}`}>never run</span>
        )}
      </div>
    </li>
  );
}
