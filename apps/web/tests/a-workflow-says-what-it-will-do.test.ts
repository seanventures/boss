import { describe, expect, it } from "vitest";

import { codeOnly } from "./_helpers/source";

/**
 * You could not find out what a workflow would do before running it.
 *
 *     "I want to see the workflow and it's definition what it will do then trigger the run…
 *      we need to know what kind of results it will bring, any agents are associated or any
 *      task is associated right now those info are missing and I also dont know how to
 *      customize a specific workflow step."
 *
 * The builder header said "4 steps · written from scratch" and each row said "Edit the video
 * / auto". That was the whole of it: not what a step does, not what it produces, not which
 * agent answers for it — and "auto" on a tier-3 step was actively misleading, because the
 * risk floor was going to stop and wait for a person regardless.
 *
 * None of this needed new data. The capability palette already held the summary, the
 * produces, the captain and the gate rule; the stored bindings already held where every input
 * comes from. Nothing joined them. `workflow_explain` is that join, and it is shared with the
 * agent's `describe_workflow` so the console and the agent cannot describe one workflow two
 * different ways.
 */
const UI = codeOnly("app/delivery-workflows-ui.tsx");
const ROUTE = codeOnly("app/api/delivery-workflows/route.ts");

describe("the plan is readable before the run", () => {
  it("the plan is fetched with the spec, not behind a click", () => {
    // "What will this do" is the question you have WHILE looking at it. A second interaction
    // is how it ends up never being asked.
    expect(UI).toContain('view: "plan"');
    expect(ROUTE).toContain("planPath");
    expect(ROUTE).toContain('view === "spec" || view === "plan"');
  });

  it("it renders above the editor, because reading comes before changing", () => {
    expect(UI).toContain('data-testid="dw-plan"');
    expect(UI.indexOf("<WorkflowPlanPanel")).toBeLessThan(UI.indexOf('<ol className="dw-draft">'));
  });

  it("it answers the three things that were missing", () => {
    // Named in the report: what results it brings, which agents, and what the operator owes.
    expect(UI).toContain('data-testid="dw-plan-results"');
    expect(UI).toContain('data-testid="dw-plan-agents"');
    expect(UI).toContain('data-testid="dw-plan-supply"');
  });

  it("each step says what it does, who runs it, and what it produces", () => {
    const panel = UI.slice(UI.indexOf("function WorkflowPlanPanel"));
    const body = panel.slice(0, panel.indexOf("\n/* ── Catalogue"));
    expect(body).toContain("step.does");
    expect(body).toContain("step.agent");
    expect(body).toContain("step.produces");
    expect(body).toContain("step.runs");
  });

  it("a forced gate reads as forced rather than as a setting", () => {
    const panel = UI.slice(UI.indexOf("function WorkflowPlanPanel"));
    // The spec may ask for `auto`; the risk floor overrides it. Showing the REQUESTED mode
    // would be a plan that lies about what happens.
    expect(panel).toContain("step.approval_is_forced");
    expect(panel).toContain("step.approval_note");
    expect(panel).toContain("stops for you");
  });

  it("it says up front when the workflow cannot run at all", () => {
    // Rather than 41 seconds into a run, as "needs 'media_artifact_id', which is not set".
    expect(UI).toContain('data-testid="dw-plan-blocked"');
    expect(UI).toContain("plan.can_start === false");
  });

  it("customizing one step lands on that step", () => {
    // "I dont know how to customize a specific workflow step" — there was no route from a
    // step to the row that edits it.
    expect(UI).toContain("dw-plan-customize-${step.id}");
    expect(UI).toContain("Customize this step");
    // Anchored by STEP ID. An index would break the first time a row is dragged.
    expect(UI).toContain("data-step-row={step.id}");
    expect(UI).toContain('document.querySelector(`[data-step-row="${stepId}"]`)');
  });

  it("a failed plan fetch never breaks the editor", () => {
    // The plan is an explanation. Losing it must not cost the operator their steps.
    const effect = UI.slice(UI.indexOf('view: "plan"'));
    expect(effect.slice(0, 600)).toContain("catch");
  });
});
