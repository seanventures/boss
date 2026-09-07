/**
 * "Set target is kind of not clear for user, the user might click on Review and decide without
 *  setting the target, or he may click set target multiple times.. it is not very clear what it
 *  does and what user has to do next in the Deploy step"
 *
 * And, on the Approvals page the gate sends you to:
 *
 * "Approve and decide is taking to Approval page, but the page isn't giving me much info.. it is
 *  not relevant, we need to explain what the approver is going to do, in this case he is going
 *  to approve to be deployed in the container which is not clear"
 *
 * Both are the same defect in two places: the screen shows the CONTROLS and withholds the
 * CONSEQUENCES. The deploy step laid out a host dropdown, two boxes, "Set target" and "Review
 * and decide" as four peers with no order between them, and the approval card said, in the last
 * line before the green button:
 *
 *     Approving records your decision — this type has no automated follow-on (no project is
 *     started).
 *
 * over a gate whose approval builds a container and runs it on a host. Not unhelpful — false.
 */

import { describe, expect, it } from "vitest";

import { workflowGateEffect } from "../app/decision-inbox-ui";
import { codeOnly } from "./_helpers/source";

const UI = codeOnly("app/delivery-workflows-ui.tsx");
const CSS = codeOnly("app/globals.css");

describe("shipping is two acts, in order", () => {
  it("the deploy step numbers them instead of showing four peer controls", () => {
    expect(UI).toContain('data-testid="dw-ship-where"');
    expect(UI).toContain('data-testid="dw-ship-approve"');
    expect(UI).toContain("Where it ships");
    expect(UI).toContain("Approve the release");
    expect(CSS).toContain(".dw-ship-plan");
  });

  it("and the second one says it is blocked until the first is done", () => {
    // "the user might click on Review and decide without setting the target"
    expect(UI).toContain('data-blocked={!shippedTo ? "true" : undefined}');
    expect(UI).toContain("Approving now would fail at the deploy");
  });

  it("the approve button lives inside the step it belongs to", () => {
    // It used to sit three blocks below, past "Ask BOSS about this step", which is how it came
    // to read as a peer of "Set target" rather than the act after it.
    expect(UI).toContain("{needsYou(step) ? <ReviewAndDecide step={step} /> : null}");
    expect(UI).toContain("needsYou(step) && !isDeployStep(step, capability)");
  });

  it("a bound target is stated as a fact, not re-offered as a form", () => {
    // "he may click set target multiple times" — the reasonable response to a button that looks
    // like it did nothing.
    expect(UI).toContain('data-testid="dw-deploy-bound"');
    expect(UI).toContain("Ships to");
    expect(UI).toContain('data-testid="dw-deploy-change"');
    expect(CSS).toContain(".dw-bound");
  });

  it("and the target list is re-read after binding, which is why it looked like nothing happened", () => {
    // The target really was created — deptgt_ee5dae79, 'Onboarding Path Builder.', active — and
    // the step went on rendering the empty form, because the only evidence that a target exists
    // lives in an array fetched once per project and never again.
    expect(UI).toContain("const reloadTargets = useCallback");
    expect(UI).toContain("onBound={() => { void load(); void reloadTargets(); }}");
  });

  it("binding is resolved by identity, not by the two checks that both said 'unbound'", () => {
    // `chosen` needs a target_id on the run input (set-target does not always write one) and
    // `mine` compares names by string equality ('Lead‐Triage Inbox' carries a U+2010 hyphen).
    expect(UI).toContain("const settled = bound ?? chosen;");
    expect(UI).toContain("bound={shippedTo}");
  });
});

describe("the approval card says what approving does", () => {
  const deployGate = {
    capability: "deploy", step_id: "deploy", step_label: "Deploy",
    workflow_run_id: "wfr_42c78321", app_name: "Onboarding Path Builder",
    environment: "staging", gate_completes_step: false,
  };

  it("a deploy gate names the container, the host and the environment", () => {
    const said = workflowGateEffect(deployGate);
    expect(said).toMatch(/container/i);
    expect(said).toContain("Onboarding Path Builder");
    expect(said).toContain("staging");
    // The claim that was flatly wrong.
    expect(said).not.toMatch(/no automated follow-on/);
  });

  it("and says nothing is sent before it is approved", () => {
    expect(workflowGateEffect(deployGate)).toMatch(/before this is approved/i);
  });

  it("an unrecognised capability still never claims there is no follow-on", () => {
    // The bug was the DEFAULT branch, so the default is what has to be right. A gate whose
    // capability this build has never heard of is still a gate: answering it moves a workflow.
    const odd = workflowGateEffect({
      ...deployGate, capability: "some_new_thing", step_label: "Some new thing",
    });
    expect(odd).toContain("Some new thing");
    expect(odd).toMatch(/workflow continues/);
    expect(odd).not.toMatch(/no automated follow-on/);
  });

  it("completing the step and releasing it are different sentences", () => {
    const releases = workflowGateEffect({ ...deployGate, capability: "x", step_label: "X" });
    const completes = workflowGateEffect({
      ...deployGate, capability: "x", step_label: "X", gate_completes_step: true,
    });
    expect(releases).not.toBe(completes);
    expect(completes).toMatch(/completes/);
  });

  it("and a decision that is not a workflow gate is left alone", () => {
    // Everything keyed on `action` already had correct copy; this must not intercept it.
    expect(workflowGateEffect({ action: "review_pull_request" })).toBe("");
    expect(workflowGateEffect({})).toBe("");
  });
});

describe("where a deploy would actually land", () => {
  const SRC = codeOnly("app/decision-inbox-ui.tsx");

  it("is resolved when the card is read, not frozen when the gate opened", () => {
    // The destination is usually chosen AFTER the gate opens — bind a target, then approve —
    // so anything recorded at open time would be stale exactly when it mattered.
    expect(SRC).toContain("function DeployDestination");
    expect(SRC).toContain("/api/deployment-targets?project_id=");
  });

  it("and an unbound app is called out before the green button, not after", () => {
    expect(SRC).toContain('data-testid="decision-destination-unbound"');
    expect(SRC).toContain("Nowhere to ship yet.");
    expect(SRC).toContain("would fail at the deploy rather than release anything");
  });

  it("matched by identity, since the stored names are not the typed ones", () => {
    expect(SRC).toContain("identityKey(String(t.app_name ?? \"\")) === wanted");
  });

  it("the consequence line is the most legible thing on the card, not the faintest", () => {
    // It was 11.5px in --faint, below the evidence chips: fine-print styling for the sentence
    // an approver most needs before an irreversible press.
    const CSS2 = codeOnly("app/design-system.css");
    const at = CSS2.indexOf(".dq-effect {");
    expect(at).toBeGreaterThan(-1);
    expect(CSS2.slice(at, at + 320)).not.toContain("var(--faint)");
  });
});
