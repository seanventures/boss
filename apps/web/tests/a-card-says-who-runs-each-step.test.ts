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
 */
import { describe, expect, it } from "vitest";

import { codeOnly } from "./_helpers/source";

const CARD = codeOnly("app/delivery-workflows-ui.tsx");
const STUDIO = codeOnly("app/agent-studio-ui.tsx");
const OUTLINE = codeOnly("../../engine/boss_engine/services/workflows.py");

describe("the workflow card names the agent on each step", () => {
  it("the server sends who runs each step, not just its name", () => {
    // The card can only draw what the outline carries, and it carried label/kind/mine/approval.
    expect(OUTLINE).toContain('"captain": str(step.get("captain") or "").strip()');
    expect(OUTLINE).toContain('"agent_work": bool(binding.get("agent_work"))');
  });

  it("the card draws it", () => {
    expect(CARD).toContain("dw-card-step-agent");
    expect(CARD).toContain("Runs as ${step.captain}");
  });

  it("an empty agent is a decision on an agent step, not a blank", () => {
    // On an ordinary step, empty means the anonymous runner. On an agent step it means BOSS
    // chooses from what each agent is for and holds — rendering nothing would hide a decision.
    expect(CARD).toContain("agent chosen per run");
  });

  it("and a step with no fixed skill is marked as such", () => {
    // It is the one kind whose behaviour its name does not predict.
    expect(CARD).toContain("your instructions");
  });
});

describe("the Flows graph says who runs each step, and stops stringifying bindings", () => {
  it("no longer prints a binding object into the card", () => {
    // `String(value)` over binding entries rendered `inputs: [object Object]` as the most
    // prominent line on every node, carrying no information at all.
    expect(STUDIO).not.toContain("`${compact(key)}: ${String(value)}`");
  });

  it("names the work rather than dumping the binding", () => {
    expect(STUDIO).toContain("binding.custom_step || binding.capability");
    expect(STUDIO).toContain("works from your instructions");
  });

  it("names the agent, which is what a governed graph is about", () => {
    expect(STUDIO).toContain("as-flow-agent");
    expect(STUDIO).toContain("agent chosen per run");
  });

  it("counts the wiring instead of printing it", () => {
    // The wiring belongs in the editor; the graph needs to say there IS some.
    expect(STUDIO).toContain("input{inputCount === 1 ? \"\" : \"s\"} wired");
  });
});
