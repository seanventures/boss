import { describe, expect, it } from "vitest";

import { readFileSync } from "node:fs";

import { stepActions, type RunAction } from "../app/delivery-workflows-ui";
import { codeOnly } from "./_helpers/source";

/**
 * boss-1, 2026-10-02: a delivery workflow's research step failed, the runner recorded it as
 * succeeded ("failed_continued"), the console showed it green, and the run walked on into a
 * panel, an auto-approved spec and a build — all without research.
 *
 * A failed step now stops its run, for every kind of step. The product owner's words for the
 * other half: "if a step fails it should stop, that's the expectation; there should be a button
 * to skip and proceed." This is that button, beside the existing "Try … again".
 */
const UI = codeOnly("app/delivery-workflows-ui.tsx");
const ROUTE = codeOnly("app/api/delivery-workflows/route.ts");
/** WHETHER Skip is offered is the engine's decision now (workflow_run_actions.py), sent with the
 *  run as one of its `actions`. The console's `canSkip` was a copy that ignored the run: it
 *  offered Skip on a CANCELLED run's failed step, which `skip_failed_step` refuses. The full
 *  table is engine/tests/test_a_runs_actions_come_from_its_state.py. */
const ENGINE = readFileSync("../../engine/boss_engine/services/workflow_run_actions.py", "utf8");

const sent = (key: RunAction["key"], step_id: string): RunAction =>
  ({ key, step_id, label: key, enabled: true, reason: null });

describe("Skip and proceed", () => {
  it("is offered on a step whose work failed or was dead-lettered", () => {
    expect(ENGINE).toContain('SKIPPABLE_STEP_STATES = frozenset({"failed", "deadlettered"})');
    // REWRITTEN to the verdict form: Skip is absent (with the refusal's sentence) for a step
    // that did not fail and for an approval, and the SAME verdict is what skip_failed_step
    // enforces through `require`.
    expect(ENGINE).toContain("if step_status not in SKIPPABLE_STEP_STATES:");
    // ...and a refused approval of any kind: a gate, or a stack deployment the operator
    // rejected (is_refused_approval) — skipping either would turn a "no" into a yes.
    expect(ENGINE).toContain("elif (is_gate(_step_kind(step), step_capability(step))");
    expect(ENGINE).toContain("or is_refused_approval(step)):");
  });

  it("is never offered on an approval gate — a refused gate is somebody's no", () => {
    // ONE gate test, `is_gate`, read by the action list and by workflows._is_gate_step alike.
    const at = ENGINE.indexOf("def is_gate(");
    const block = ENGINE.slice(at, ENGINE.indexOf("\ndef ", at + 1));
    expect(block).toContain('== "decision"');
    expect(block).toContain(".is_gate");
  });

  it("the console shows exactly the step's Skip it was sent, and nothing for other steps", () => {
    const run = { actions: [sent("retry_step", "research"), sent("skip_step", "research"),
                            sent("retry_step", "panel")] };
    expect(stepActions(run, "research").skip?.key).toBe("skip_step");
    expect(stepActions(run, "panel").skip).toBeUndefined();
    expect(stepActions(run, "panel").retry?.key).toBe("retry_step");
    expect(stepActions({ actions: [] }, "research")).toEqual({ retry: undefined, skip: undefined });
  });

  it("sits beside Try again, in the same failed-step block", () => {
    // One component carries both answers, so the step's detail and the run card cannot drift.
    const at = UI.indexOf("function FailedStepActions(");
    expect(at).toBeGreaterThan(-1);
    const body = UI.slice(at, UI.indexOf("\nfunction StepDetail(", at));
    const retry = body.indexOf("data-testid={`${idPrefix}-retry`}");
    const skip = body.indexOf("data-testid={`${idPrefix}-skip`}");
    expect(retry).toBeGreaterThan(-1);
    expect(skip).toBeGreaterThan(retry);
    expect(skip - retry).toBeLessThan(900);
    expect(ENGINE).toContain('"Skip and proceed"');
    expect(body).toContain("{skip ? (");
    // Rendered on the failed step's own detail, keyed so a half-typed reason never follows the
    // operator to a different step.
    expect(UI).toContain("<FailedStepActions key={step.step_id} step={step}");
  });

  it("asks why, inline, and sends nothing without a reason", () => {
    // Inline rather than window.prompt: the console does not use browser prompts.
    const form = UI.indexOf("data-testid={`${idPrefix}-skip-form`}");
    expect(form).toBeGreaterThan(-1);
    const body = UI.slice(form, form + 1200);
    expect(body).toContain("if (!skipWhy.trim()) return;");
    expect(body).toContain("onSkip(skipWhy.trim());");
    expect(body).toContain("data-testid={`${idPrefix}-skip-reason`}");
    expect(body).toContain("disabled={!!busy || !skipWhy.trim()}");
    expect(UI).not.toContain("window.prompt");
  });

  it("sends the reason to the skip action", () => {
    expect(UI).toContain('act("Skip", "skip_step", { run_id: runId, step_id: stepId, reason })');
  });

  it("reaches the skip endpoint, addressed to the run", () => {
    expect(ROUTE).toContain("skip_step:");
    expect(ROUTE).toContain("/v1/delivery-workflows/runs/${encodeURIComponent(id)}/skip-step");
  });
});
