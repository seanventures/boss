import React from "react";
import { readFileSync } from "node:fs";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import {
  FailedStepActions, RUN_ACTION_KEYS, RunActionsBar, STEP_ACTION_KEYS, stepActions,
  type RunAction, type RunStep,
} from "../app/delivery-workflows-ui";
import { codeOnly } from "./_helpers/source";

/**
 * A run's buttons are the server's list, rendered — never decided here.
 *
 * The incident: a cancelled Standard Product Delivery run whose Research step had FAILED still
 * offered "Build on this research →". The product owner: "Does it make sense to have Build on
 * this research? these small bugs and experience give quality." The button existed because this
 * file decided it with its own condition ("a succeeded step with a discovery id"), and an old
 * research step that had failed was recorded as exactly that. Stop, Run again, Remove, Re-check,
 * Try again and Skip were each decided the same way, from `run.status` checks that disagreed
 * with what the engine would actually do.
 *
 * The engine now sends `actions` with every run (workflow_run_actions.py) and this file renders
 * it. These tests hold the console to that: no status checks where buttons are chosen, every
 * engine key has its words here, and a rendered run shows exactly what it was sent.
 */
(globalThis as Record<string, unknown>).React = React;

const UI = codeOnly("app/delivery-workflows-ui.tsx");
const ENGINE = readFileSync("../../engine/boss_engine/services/workflow_run_actions.py", "utf8");

/** The body of a top-level function or const in the UI source, up to the next top-level one. */
function body(start: string): string {
  const at = UI.indexOf(start);
  expect(at, `${start} is missing`).toBeGreaterThan(-1);
  const next = UI.slice(at + start.length).search(/\n(export )?(function|const|type) /);
  return UI.slice(at, next === -1 ? undefined : at + start.length + next);
}

function pythonTuple(name: string): string[] {
  const match = ENGINE.match(new RegExp(`${name} = \\(([^)]*)\\)`));
  expect(match, `${name} not found in workflow_run_actions.py`).toBeTruthy();
  return [...match![1].matchAll(/"([a-z_]+)"/g)].map((m) => m[1]);
}

const noop = () => {};
const handlers = {
  recheck: noop, stop: noop, stop_and_rerun: noop, rerun: noop, remove: noop,
  build_on_research: noop,
};

function run(actions: RunAction[]) {
  return {
    id: "wfr_1", status: "cancelled", workflow_key: "standard_product_delivery",
    workflow_name: "Standard Product Delivery", version: 1, steps: [], step_count: 0,
    steps_done: 0, current_step: null, waiting_on: null, blocked_reason: null, error: null,
    updated_at: null, actions,
  };
}

const ok = (key: RunAction["key"], label: string, step_id?: string): RunAction =>
  ({ key, label, enabled: true, reason: null, ...(step_id ? { step_id } : {}) });

describe("which buttons a run has is the server's decision", () => {
  it("the console knows exactly the action keys the engine can send", () => {
    expect([...RUN_ACTION_KEYS]).toEqual(pythonTuple("RUN_ACTION_KEYS"));
    expect([...STEP_ACTION_KEYS]).toEqual(pythonTuple("STEP_ACTION_KEYS"));
  });

  it("and has the words for every run-level one — a new action cannot arrive unexplained", () => {
    const view = body("const RUN_ACTION_VIEW");
    for (const key of RUN_ACTION_KEYS) {
      expect(view, `${key} has no view`).toMatch(new RegExp(`\\n  ${key}: \\{`));
    }
    expect((view.match(/\n    explain:/g) ?? []).length).toBe(RUN_ACTION_KEYS.length);
  });

  it("the places that render run actions read no run or step status", () => {
    // Every place a workflow-management button is chosen. A `run.status` or `step.status`
    // check here is a private copy of an engine rule — the shape the incident came from.
    for (const fn of ["export function RunActionsBar(", "export function FailedStepActions(",
                      "const RUN_ACTION_VIEW"]) {
      const code = body(fn);
      expect(code, fn).not.toMatch(/\brun\.status\b/);
      expect(code, fn).not.toMatch(/\bstep\.status\b/);
      expect(code, fn).not.toContain("ACTIVE_RUN_STATES");
      expect(code, fn).not.toContain("STOPPED_STEP_STATUSES");
    }
    const path = body("function StepPath(");
    expect(path).toContain("<RunActionsBar run={run}");
    expect(path).not.toMatch(/ACTIVE_RUN_STATES\.has\(run\.status\)/);
  });

  it("the console's old private rules are gone", () => {
    // `researched` was the Build-on-this-research rule; `canSkip` was Skip's. Both now live in
    // the engine, once, shared with the endpoints that act on them.
    expect(UI).not.toContain("const researched");
    expect(UI).not.toContain("canSkip(");
    // A step's Try again/Skip render whenever its server-sent actions exist, not on a
    // hand-kept list of statuses.
    expect(UI).not.toContain('["failed", "deadlettered", "cancelled"].includes(step.status)');
  });

  it("every action button's test id exists once, in the view table, not as a literal button", () => {
    for (const id of ["dw-recheck", "dw-stop", "dw-stop-and-rerun", "dw-rerun", "dw-delete-run",
                      "dw-build-on-this"]) {
      expect(UI).not.toContain(`data-testid="${id}"`);
      expect(UI.split(`testId: "${id}"`).length - 1).toBe(1);
    }
  });
});

describe("a run renders exactly the actions it was sent", () => {
  it("THE INCIDENT: a cancelled run whose research failed shows no Build on this research", () => {
    // What the engine sends for it (see the engine table's INCIDENT rows).
    const html = renderToStaticMarkup(
      <RunActionsBar run={run([ok("rerun", "Run this workflow again"),
                               ok("remove", "Remove this run")])}
                     busy={null} handlers={handlers} />);
    expect(html).toContain("Run this workflow again");
    expect(html).toContain("Remove this run");
    expect(html).not.toContain("dw-build-on-this");
    expect(html).not.toContain('data-testid="dw-stop"');
  });

  it("a live run shows Stop and Stop and run again, and not Remove", () => {
    const html = renderToStaticMarkup(
      <RunActionsBar run={run([ok("stop", "Stop this run"), ok("stop_and_rerun", "Stop and run again")])}
                     busy={null} handlers={handlers} />);
    expect(html).toContain('data-testid="dw-stop"');
    expect(html).toContain('data-testid="dw-stop-and-rerun"');
    expect(html).not.toContain("dw-delete-run");
    expect(html).not.toContain("dw-rerun\"");
  });

  it("a disabled action is shown disabled with its reason as the button's help", () => {
    const html = renderToStaticMarkup(
      <RunActionsBar run={run([{ key: "build_on_research", label: "Build on this research →",
                                 enabled: false,
                                 reason: "the research is still being written up" }])}
                     busy={null} handlers={handlers} />);
    expect(html).toMatch(/<button[^>]*disabled=""[^>]*title="the research is still being written up"/);
    expect(html).toContain('data-enabled="false"');
  });

  it("a run with no actions renders no management row at all", () => {
    expect(renderToStaticMarkup(
      <RunActionsBar run={run([])} busy={null} handlers={handlers} />)).toBe("");
  });

  it("a step's Try again and Skip come from the run's list for THAT step", () => {
    const step = { step_id: "research", kind: "capability", status: "failed",
                   capability: "research" } as unknown as RunStep;
    const sent = run([
      { key: "retry_step", label: "Try Research again", enabled: false, step_id: "research",
        reason: "this run was stopped, and a stopped run is not resumed" },
      { key: "skip_step", label: "Skip and proceed", enabled: false, step_id: "research",
        reason: "this run was stopped, and a stopped run is not resumed" },
      ok("retry_step", "Try Panel again", "panel"),
    ]);
    const html = renderToStaticMarkup(
      <FailedStepActions step={step} actions={stepActions(sent, "research")} busy={null}
                         onRetry={noop} onSkip={noop} />);
    expect(html).toContain("Try Research again");
    expect(html).not.toContain("Try Panel again");
    expect(html).toMatch(/data-testid="dw-step-retry"/);
    expect(html).toMatch(/disabled=""[^>]*title="this run was stopped/);
    // Nothing sent for a step, nothing rendered — whatever its status says.
    expect(renderToStaticMarkup(
      <FailedStepActions step={step} actions={stepActions(sent, "deploy")} busy={null}
                         onRetry={noop} onSkip={noop} />)).toBe("");
  });

  it("Skip is absent when the server did not send it — an approval gate", () => {
    const gate = { step_id: "approve", kind: "capability", status: "failed",
                   capability: "spec_review" } as unknown as RunStep;
    const html = renderToStaticMarkup(
      <FailedStepActions step={gate} busy={null} onRetry={noop} onSkip={noop}
                         actions={stepActions(run([ok("retry_step", "Try Spec review again", "approve")]),
                                              "approve")} />);
    expect(html).toContain("Try Spec review again");
    expect(html).not.toContain("Skip and proceed");
  });
});
