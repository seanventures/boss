/**
 * Two things the screen said at once that could not both be true.
 *
 * "After approval I get this screen, I think it's deploying in the background but why it
 *  displays Deploy now" — and it was displaying both:
 *
 *     [ Deploy now ]  Ships this build to Pricing Experiment Log · staging (192.0.2.202:3017).
 *     ...
 *     Approved — starting. The gate is signed off; the step moves as soon as a worker picks
 *     it up, usually within a minute.
 *
 * Pressing it would have started a SECOND deploy of the same build while the first was being
 * picked up. An approved gate is a decision already taken; the button that takes it does not
 * belong on screen any more.
 *
 * And the rail's first node had its label pushed to the far right of its own box. That node is
 * now a chip at the head of the run lane (see below).
 */
import { describe, expect, it } from "vitest";

import { codeOnly } from "./_helpers/source";

const UI = codeOnly("app/delivery-workflows-ui.tsx");

describe("an approved gate stops offering the press that answers it", () => {
  it("Deploy now is withheld once the decision is approved", () => {
    expect(UI).toContain('decidedButNotMoved(step) !== "approved"');
    const at = UI.indexOf("<ShipNow");
    const guard = UI.slice(Math.max(0, at - 400), at);
    expect(guard).toContain("decidedButNotMoved");
  });

  it("but it is still offered before the decision is made", () => {
    // The withholding is keyed on APPROVED, not on the step being a gate at all — a deploy
    // waiting for a person still needs the button that ships it.
    expect(UI).not.toContain("isDeployStep(step, capability) && shippedTo && buildRunId\n        && !step.decision_id");
  });

  it("and the panel still says what is happening instead", () => {
    expect(UI).toContain("Approved — starting.");
  });
});

describe("who is on the run is not drawn as a step", () => {
  // REWRITTEN (Workflow Studio): this was the rail's first node, whose label the layout pushed
  // to the far edge. The run is now the shared lane, and who is on it is a chip at its head —
  // so it cannot be laid out unlike a step, because it is not drawn as one.
  it("is a chip at the head of the lane", () => {
    expect(UI).toContain('className="wf-chip wf-chip-btn" data-testid="dw-node-agents"');
  });

  it("and it carries no step glyph or number", () => {
    const at = UI.indexOf('className="wf-chip wf-chip-btn" data-testid="dw-node-agents"');
    const chip = UI.slice(at, UI.indexOf("</button>", at));
    expect(chip).not.toContain("StepIcon");
    expect(chip).not.toContain("index + 1");
  });
});
