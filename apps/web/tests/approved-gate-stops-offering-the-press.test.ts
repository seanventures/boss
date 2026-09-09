/**
 * Two things the screen said at once that could not both be true.
 *
 * "After approval I get this screen, I think it's deploying in the background but why it
 *  displays Deploy now" — and it was displaying both:
 *
 *     [ Deploy now ]  Ships this build to Pricing Experiment Log · staging (2.29.2.202:3017).
 *     ...
 *     Approved — starting. The gate is signed off; the step moves as soon as a worker picks
 *     it up, usually within a minute.
 *
 * Pressing it would have started a SECOND deploy of the same build while the first was being
 * picked up. An approved gate is a decision already taken; the button that takes it does not
 * belong on screen any more.
 *
 * And the rail's first node had its label pushed to the far right of its own box: the marker
 * sat in row 1 and "auto-confirmed" in row 2 of the SAME grid column, so the column sized to
 * the word. `.dw-dot` spans both rows, which is why every other step is laid out correctly.
 */
import { describe, expect, it } from "vitest";

import { codeOnly } from "./_helpers/source";

const UI = codeOnly("app/delivery-workflows-ui.tsx");
const CSS = codeOnly("app/globals.css");

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

describe("the rail's first node is laid out like every other one", () => {
  it("its marker spans both rows", () => {
    // Without this the marker is in (1,1) and the meta in (2,1), so the first column sizes to
    // "auto-confirmed" and the name is pushed to the right-hand edge of the node.
    expect(UI).toContain('className="dw-dot dw-node-dot"');
    const at = CSS.indexOf(".dw-dot {");
    expect(CSS.slice(at, at + 120)).toContain("grid-row: 1 / 3");
  });

  it("and it is a marker, not a numbered step", () => {
    // It does not run, so it must not carry the filled disc the runtime's own steps use.
    const at = CSS.indexOf(".dw-node-agents .dw-dot");
    expect(at).toBeGreaterThan(-1);
    expect(CSS.slice(at, at + 200)).toContain("background: transparent");
  });
});
