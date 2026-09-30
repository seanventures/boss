/**
 * An action BOSS offers must reach the screen.
 *
 * The agent replied "I've offered a button to delete all containers on hetzner-ci-01", the API
 * really did return `proposed_actions`, and the console rendered nothing. Asked where the
 * button was, the agent could only repeat that it had offered one — which was true.
 *
 * The cause: `send` handled `pending_confirmation` from the reply and never called
 * `setProposed`, while both thread-LOADING paths did. So an offer survived reopening a thread
 * and did not survive the turn that produced it — the one case that matters.
 *
 * This is the console's whole confirm-before-acting story. `run_on_host` deliberately refuses a
 * model's direct call so a person sees the exact command first; if the button never arrives,
 * that design does not become safer, it becomes unreachable.
 */

import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const source = readFileSync(join(process.cwd(), "app/agent-ui.tsx"), "utf8");

/** Every place the component absorbs a server payload sets the pending plan. */
const PLAN_WRITES = source.split("setPendingPlan(").length - 1;
const PROPOSAL_WRITES = source.split("setProposed(").length - 1;

describe("a proposed action survives the turn that produced it", () => {
  it("every path that reads a reply also reads its proposals", () => {
    // Not a count for its own sake: the bug WAS one path handling the plan and forgetting the
    // proposals. Proposals may legitimately be reset in more places (opening a session clears
    // them), so this is a floor, not an equality.
    expect(PROPOSAL_WRITES).toBeGreaterThanOrEqual(PLAN_WRITES);
  });

  it("the send handler stores proposals from the response", () => {
    // The specific regression: locate the success branch of `send` and require it there.
    const marker = "if (typeof d.thread_id === \"string\") setThreadId(d.thread_id);";
    const at = source.indexOf(marker);
    expect(at).toBeGreaterThan(-1);
    const branch = source.slice(at, at + 2600);
    expect(branch).toContain("setPendingPlan(");
    expect(branch).toContain("setProposed(");
  });

  it("proposals are read from the field the API actually returns", () => {
    expect(source).toContain("d.proposed_actions");
  });

  it("the confirm-before-acting UI is still what renders them", () => {
    // If this text goes, the offer has stopped being a confirmation and become something else.
    expect(source).toContain("nothing runs until you choose");
  });
});
