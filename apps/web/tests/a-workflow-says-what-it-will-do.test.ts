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
 *
 * REWRITTEN for Workflow Studio. The plan panel above the builder is gone with the builder: the
 * editor answers the same questions as summary chips (steps, agents, approvals, typical time,
 * start-form inputs) computed from the draft and the server's /check summary, so they describe
 * what is on screen rather than what was last published. Those are pinned with the editor
 * (tests/workflow-editor.test.tsx, tests/save-flow.test.tsx). What stays here is the join
 * itself, which the chat agent's describe_workflow still reads.
 */
const ROUTE = codeOnly("app/api/delivery-workflows/route.ts");

describe("the plan is still a join the console can read", () => {
  it("the proxy serves it beside the spec", () => {
    expect(ROUTE).toContain("planPath");
    expect(ROUTE).toContain('view === "spec" || view === "plan"');
  });
});
