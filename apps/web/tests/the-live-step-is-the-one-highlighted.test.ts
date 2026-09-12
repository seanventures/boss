import { describe, expect, it } from "vitest";

import { codeOnly } from "./_helpers/source";

/**
 * "Why is 2 highlighted if I have not acted on 1?"
 *
 * Because `data-blocked` was set on step 2 exactly when step 1 was unfinished, and the CSS drew
 * that attribute as the highlight — with the primary blue button inside it. So the screen boxed
 * the step it was simultaneously telling you would fail ("there is nowhere to put the container
 * until step 1 is done"), marked it amber, called it "waiting on you", and left the step that
 * actually needed doing flat and unmarked. Three signals, all pointing at the wrong one.
 */
const UI = codeOnly("app/delivery-workflows-ui.tsx");
const CSS = codeOnly("app/globals.css");

describe("the highlight follows the prerequisite", () => {
  it("step one is active until a target exists", () => {
    const at = UI.indexOf('data-testid="dw-ship-where"');
    expect(UI.slice(at - 200, at)).toContain('data-active={!shippedTo ? "true" : undefined}');
  });

  it("step two only once it does", () => {
    const at = UI.indexOf('data-testid="dw-ship-approve"');
    expect(UI.slice(at, at + 240))
      .toContain('data-active={shippedTo && needsYou(step) ? "true" : undefined}');
  });

  it("and the CSS boxes the active one, not the blocked one", () => {
    expect(CSS).toContain('.dw-ship-step[data-active="true"] {');
    const at = CSS.indexOf('.dw-ship-step[data-blocked="true"] {');
    expect(CSS.slice(at, at + 120)).toContain("opacity");
    expect(CSS.slice(at, at + 120)).not.toContain("background:");
  });
});

describe("nothing else points at the step that cannot run", () => {
  it("its button is not the primary call to action", () => {
    // Still reachable — an operator may want to read the decision — but not dressed as the
    // thing to do next.
    expect(UI).toContain("premature={!shippedTo}");
    expect(UI).toContain('`cap-btn${premature ? "" : " primary"} dw-decide-btn`');
  });

  it("and it does not claim to be waiting on the operator", () => {
    expect(UI).toContain('!shippedTo ? "after step 1"');
  });

  it("the blocked label is not amber, which would compete with the live step", () => {
    const at = CSS.indexOf('.dw-ship-step[data-blocked="true"] .dw-ship-state');
    expect(CSS.slice(at, at + 120)).toContain("--faint");
  });
});

describe("a gate that has not been answered is the only way to ship", () => {
  it("Redeploy is not offered while the gate is open", () => {
    // It POSTs the deploy directly, with no decision — the one thing a manual gate exists to
    // prevent. It also read as proof the step was done: two primary buttons saying the app is
    // deployed, above a line asking for approval to deploy it.
    const at = UI.indexOf("isDeployStep(step, capability) && shippedTo && buildRunId");
    expect(UI.slice(at, at + 200)).toContain("!needsYou(step)");
  });

  it("and comes back once the decision has been taken", () => {
    // The re-ship case it was added for: a failed or wrongly-"succeeded" deploy.
    const at = UI.indexOf("isDeployStep(step, capability) && shippedTo && buildRunId");
    expect(UI.slice(at, at + 240)).toContain('decidedButNotMoved(step) !== "approved"');
  });
});

describe("an address answering does not mean this run shipped", () => {
  it("the live link says which release it is, when this step has not succeeded", () => {
    // This app had a deployment from a run five days earlier, so a step waiting for approval
    // led with "shipped and answering" — true about the app, false about the step.
    expect(UI).toContain("answering, from an earlier release");
    expect(UI).toContain("This build has not");
  });

  it("and does not take the primary button off the gate", () => {
    const at = UI.indexOf('data-testid="dw-open-app"');
    const block = UI.slice(at - 400, at);
    expect(block).toContain('isDeployStep(step, capability) && step.status !== "succeeded" ? ""');
  });

  it("a succeeded deploy still says so plainly", () => {
    expect(UI).toContain("shipped and answering");
  });
});
