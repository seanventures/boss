"use client";

// esbuild uses the classic JSX runtime here (no vitest config, tsconfig jsx=preserve),
// so a component rendered in a test must have React in scope -- see app/previews-ui.tsx.
import React, { useState } from "react";

import type { V2Stage, V2Workflow, WorkflowsView } from "../../lib/v2/platform";

/**
 * Workflows — the processes this company runs, and how their stages fit together.
 *
 * LIST ON THE LEFT, THE SELECTED PROCESS ON THE RIGHT
 * ---------------------------------------------------
 * The reference's layout, and it is the right one: a process is a thing you pick and then
 * study. An earlier version rendered every stage as a line of `step_key role_name` in one
 * column — a data dump, not a screen. It showed eleven identical rows and lost the fact that
 * three of them run at once.
 *
 * THE BADGES ARE DERIVED, NOT DECORATIVE
 * --------------------------------------
 * PANEL / PARALLEL / JOIN / SEQUENTIAL come from the spec's own `kind`, `next` and `wait_for`.
 * The spec is a DAG and was being read as a list. So a fan-out really is parallel, and a stage
 * that waits for three others really is where the branches rejoin.
 *
 * WHAT IS DELIBERATELY ABSENT
 * ---------------------------
 * The reference has "+ Add stage", "Deploy version" and an × on every stage. BOSS has no
 * template-editing API and no versioning, so those would be three controls that cannot act.
 * The screen says why instead of drawing them.
 */

const KIND_ORDER = ["PANEL", "PARALLEL", "JOIN", "SEQUENTIAL"] as const;

export function WorkflowsScreen({ view }: { view: WorkflowsView }) {
  const [selected, setSelected] = useState(0);

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

  const current = view.workflows[Math.min(selected, view.workflows.length - 1)];

  return (
    <section className="v2-screen v2-wf" data-testid="v2-workflows">
      <header className="v2-screen-head">
        <h1 className="v2-screen-title">Workflows</h1>
        <p className="v2-screen-sub">stages, owners and panels your agents execute</p>
      </header>

      {view.workflows.length === 0 ? (
        <p className="v2-empty" data-testid="v2-workflows-none">
          No processes are defined for this company yet.
        </p>
      ) : (
        <div className="v2-wf-split">
          {/* The list. Selected state is a mint border, as the reference does it. */}
          <ul className="v2-wf-list" data-testid="v2-workflows-list">
            {view.workflows.map((w, i) => (
              <li key={w.id}>
                <button
                  type="button"
                  className="v2-wf-card"
                  data-selected={i === selected}
                  onClick={() => setSelected(i)}
                  data-testid={`v2-workflow-${w.key}`}
                >
                  <span className="v2-wf-card-head">
                    <span className="v2-card-name">{w.name}</span>
                    <span className="v2-wf-status" data-status={w.status}>
                      {w.status}
                    </span>
                  </span>
                  {w.description ? (
                    <span className="v2-wf-card-desc">{w.description}</span>
                  ) : null}
                  {/* Counted, never estimated. Human gates are absent from the line entirely
                      when the spec does not record them — see the note below the header. */}
                  <span className="v2-wf-card-meta v2-mono">
                    {w.stages.length} stage{w.stages.length === 1 ? "" : "s"}
                    {w.panels > 0 ? ` · ${w.panels} panel${w.panels === 1 ? "" : "s"}` : ""}
                    {w.parallel_points > 0 ? ` · ${w.parallel_points} parallel` : ""}
                    {w.runs_30d > 0 ? ` · ${w.runs_30d} runs / 30d` : " · never run"}
                  </span>
                </button>
              </li>
            ))}
          </ul>

          {current ? <WorkflowDetail w={current} view={view} /> : null}
        </div>
      )}
    </section>
  );
}

function WorkflowDetail({ w, view }: { w: V2Workflow; view: WorkflowsView }) {
  return (
    <div className="v2-wf-detail" data-testid={`v2-workflow-detail-${w.key}`}>
      <header className="v2-wf-detail-head">
        <h2 className="v2-wf-detail-title">{w.name}</h2>
        <span className="v2-wf-key v2-mono">{w.key}</span>
        {w.running_now > 0 ? (
          <span className="v2-wf-running v2-mono" data-testid={`v2-workflow-running-${w.key}`}>
            {w.running_now} running now
          </span>
        ) : null}
      </header>

      {w.description ? <p className="v2-wf-detail-desc">{w.description}</p> : null}

      {/* The two things this deployment cannot do, said once, where the reference puts its
          controls. Better than three buttons that do nothing. */}
      <p className="v2-wf-note v2-mono" data-testid="v2-workflows-versioning">
        {view.version_note}
        {view.gates_recorded ? "" : ` ${view.gates_note}`}
      </p>

      {w.stages.length === 0 ? (
        <p className="v2-empty" data-testid={`v2-workflow-nostages-${w.key}`}>
          This process has no stages recorded in its spec.
        </p>
      ) : (
        <ol className="v2-wf-stages" data-testid={`v2-workflow-stages-${w.key}`}>
          {w.stages.map((s, i) => (
            <Stage key={s.key || i} s={s} index={i} wf={w.key} />
          ))}
        </ol>
      )}
    </div>
  );
}

/** One stage: number outside, card inside, left border coloured by what it does. */
function Stage({ s, index, wf }: { s: V2Stage; index: number; wf: string }) {
  const n = String(index + 1).padStart(2, "0");
  return (
    <li className="v2-wf-stage-row" data-testid={`v2-stage-${wf}-${s.key}`}>
      <span className="v2-wf-stage-n v2-mono">{n}</span>

      <div className="v2-wf-stage" data-kind={s.kind} data-unreachable={s.unreachable}>
        <div className="v2-wf-stage-head">
          <span className="v2-wf-stage-name">{s.action || s.key}</span>
          <span className="v2-wf-kind v2-mono" data-kind={s.kind}>
            {s.kind}
          </span>
          {/* Only when the spec actually records gating — otherwise the screen would be
              asserting that nothing stops for a human, which it cannot know. */}
          {s.gated === true ? (
            <span className="v2-wf-kind v2-mono" data-kind="GATE" data-testid={`v2-stage-gated-${wf}-${s.key}`}>
              HUMAN GATE
            </span>
          ) : null}
          {/* A stage the entry point cannot reach will never run. */}
          {s.unreachable ? (
            <span className="v2-wf-kind v2-mono" data-kind="UNREACHABLE" data-testid={`v2-stage-unreachable-${wf}-${s.key}`}>
              UNREACHABLE
            </span>
          ) : null}
        </div>

        {/* What this stage waits on and where it goes — the DAG, in words. */}
        <p className="v2-wf-stage-flow v2-mono">
          {s.waits_for.length > 1
            ? `waits for ${s.waits_for.length} branches, then `
            : ""}
          {s.next.length > 1
            ? `starts ${s.next.length} branches in parallel`
            : s.next.length === 1
              ? `then ${s.next[0]}`
              : "ends the process"}
        </p>

        {/* The owner, as a chip with a dot — the reference's own treatment. */}
        {s.role ? (
          <span className="v2-wf-owner v2-mono" data-testid={`v2-stage-owner-${wf}-${s.key}`}>
            <span className="v2-wf-owner-dot" data-kind={s.kind} aria-hidden />
            {s.role}
          </span>
        ) : null}
      </div>
    </li>
  );
}
