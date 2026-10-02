import { describe, expect, it } from "vitest";

import { canSkip, type RunStep } from "../app/delivery-workflows-ui";
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

function step(status: string, kind = "capability"): RunStep {
  return { step_id: "research", kind, status, capability: "research" } as unknown as RunStep;
}

describe("Skip and proceed", () => {
  it("is offered on a step whose work failed or was dead-lettered", () => {
    expect(canSkip(step("failed"), { is_gate: false })).toBe(true);
    expect(canSkip(step("deadlettered"), { is_gate: false })).toBe(true);
  });

  it("is never offered on an approval gate — a refused gate is somebody's no", () => {
    expect(canSkip(step("failed"), { is_gate: true })).toBe(false);
    expect(canSkip(step("failed", "decision"))).toBe(false);
  });

  it("is not offered on a live, finished or cancelled step", () => {
    for (const status of ["ready", "running", "waiting", "succeeded", "skipped", "cancelled"]) {
      expect(canSkip(step(status), { is_gate: false })).toBe(false);
    }
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
    expect(skip - retry).toBeLessThan(800);
    expect(body).toContain("Skip and proceed");
    expect(body.slice(retry, skip)).toContain("canSkip(step, capability)");
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
