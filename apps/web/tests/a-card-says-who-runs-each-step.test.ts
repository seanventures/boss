/**
 * A workflow that lists its steps and not its agents invites exactly one question.
 *
 * The operator looked at a card reading "1 Fetch the video / 2 edit / 3 Publish to YouTube" and
 * asked "did you assign the agents to the steps or not?" — about a workflow where all three
 * were assigned correctly. The binding was right and nothing on screen said so.
 *
 * That matters more here than on most cards: the product's claim is that AGENTS do the work and
 * steps track it. A surface that shows only the steps is showing the tracking and hiding the
 * work.
 *
 * REWRITTEN for Workflow Studio. The Yours card and Agent Studio's Flows graph are gone. Who runs
 * each step is now ONE rule, `stepView().agent` (named / Auto / You / A person), drawn by the
 * same StepNode on the lane, the run and the Library's mini-lane — pinned in
 * tests/step-view.test.ts and tests/flow-lane.test.tsx. Kept here: the server still sends it,
 * and the Library card still says which agents a workflow uses.
 */
import { describe, expect, it } from "vitest";

import { codeOnly } from "./_helpers/source";

const LIBRARY = codeOnly("app/workflows/library.tsx");
const OUTLINE = codeOnly("../../engine/boss_engine/services/workflows.py");

describe("the workflow card names the agents on its steps", () => {
  it("the server sends who runs each step, not just its name", () => {
    // The card can only draw what the outline carries, and it carried label/kind/mine/approval.
    expect(OUTLINE).toContain('"captain": str(step.get("captain") or "").strip()');
    expect(OUTLINE).toContain('"agent_work": bool(binding.get("agent_work"))');
  });

  it("the Library card shows each agent, named in its tooltip", () => {
    expect(LIBRARY).toContain("w.steps.map((s) => s.captain)");
    expect(LIBRARY).toContain('agents.map((a) => a.replace(/_/g, " ")).join(", ")');
    expect(LIBRARY).toContain("<MiniLane steps={w.steps} />");
  });
});
