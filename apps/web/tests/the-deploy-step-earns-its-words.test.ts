import { describe, expect, it } from "vitest";

import { codeOnly } from "./_helpers/source";

/**
 * "Lots of text and a little cluttered … the info is good but it looks like a distraction."
 *
 * Counted on the deploy step: three controls and eight lines of prose, every line permanent.
 * Each of those sentences was added to fix a real confusion — "the user might click Review and
 * decide without setting the target", "he may click Set target multiple times" — and each is
 * correct. None of them accounted for the twentieth reading.
 *
 * So none is deleted. They are EARNED OUT: verbose until the operator has completed the thing
 * they explain, quiet after, and one press of `?` brings all of it back.
 */
const UI = codeOnly("app/delivery-workflows-ui.tsx");
const CSS = codeOnly("app/globals.css");

describe("the first run still explains itself", () => {
  it("help shows until the operator has shipped once", () => {
    expect(UI).toContain("const showHelp = helpOpen || !learnedDeploy;");
  });

  it("and the step is what records that they have", () => {
    // Binding a target is the act the copy exists to explain.
    expect(UI).toContain("onBound={() => { markDeployLearned(); onBound(); }}");
  });

  it("the flag is per browser and survives a reload", () => {
    expect(UI).toContain("boss.learned.");
    expect(UI).toContain("window.localStorage.setItem(key, \"1\")");
  });

  it("and it cannot break the page where storage is blocked", () => {
    // Private mode throws on access, and an onboarding hint is not worth a blank screen.
    const at = UI.indexOf("function useLearned");
    expect(UI.slice(at, at + 900)).toContain("catch");
  });

  it("nothing is hydrated away mid-read", () => {
    // Reading localStorage during render would make the server and first client paint disagree.
    const at = UI.indexOf("function useLearned");
    expect(UI.slice(at, at + 900)).toContain("useState(false)");
    expect(UI.slice(at, at + 900)).toContain("useEffect");
  });
});

describe("after that it is quiet, and recoverable", () => {
  it("one toggle, not one per sentence", () => {
    expect(UI.split('data-testid="dw-help-toggle"').length - 1).toBe(1);
  });

  it("offered only once there is nothing left to teach", () => {
    const at = UI.indexOf('data-testid="dw-help-toggle"');
    expect(UI.slice(at - 200, at)).toContain("isDeployStep(step, capability) && learnedDeploy");
  });

  it("and it is the quietest thing in the header", () => {
    const at = CSS.indexOf(".dw-help-toggle {");
    expect(CSS.slice(at, at + 300)).toContain("--faint");
    expect(CSS.slice(at, at + 300)).toContain("background: transparent");
  });

  it("both teaching paragraphs answer to it", () => {
    expect(UI).toContain("{!shippedTo && showHelp ? (");
    expect(UI).toContain('data-testid="dw-help-where"');
    expect(UI).toContain('data-testid="dw-help-approve"');
    // The approve hint is the else-branch of "has a target", so it is reached only via showHelp.
    const at = UI.indexOf('data-testid="dw-help-approve"');
    expect(UI.slice(at - 120, at)).toContain("showHelp ?");
  });
});

describe("a step that cannot run takes one line", () => {
  it("no paragraph, no button, no role note while it is blocked", () => {
    expect(UI).toContain("{needsYou(step) && shippedTo ? <ReviewAndDecide step={step} /> : null}");
  });

  it("its state is still said, beside the title", () => {
    expect(UI).toContain('!shippedTo ? "after step 1"');
  });
});

describe("a button that says what it does needs no sentence", () => {
  it("the ask explains itself in a tooltip", () => {
    // The label now names the step's captain, so the tooltip does too — and says the dock
    // arrives addressed to them rather than to BOSS in general.
    expect(UI).toContain("Opens the side dock addressed to the ");
    expect(UI).toContain("with this step's state already filled in");
  });

  it("and the sentence is gone from the body", () => {
    expect(UI).not.toContain("Opens the side dock with this step&rsquo;s state");
  });
});

describe("an operator who has already shipped is not lectured", () => {
  it("an existing deployment target counts as having done it", () => {
    // The flaw that made the first attempt invisible: keying only on localStorage meant an
    // operator with bound targets and weeks of releases had no flag, so they got the full
    // first-run copy and not even the `?` to close it. They looked at the build and correctly
    // reported seeing no change.
    expect(UI).toContain("const learnedDeploy = flaggedDeploy || targets.length > 0;");
  });

  it("and the browser flag still covers the genuinely first act", () => {
    expect(UI).toContain('useLearned("deploy_step")');
    expect(UI).toContain("onBound={() => { markDeployLearned(); onBound(); }}");
  });
});

describe("one fact is stated once", () => {
  it("a waiting gate is one chip, not a status and a gate saying half each", () => {
    expect(UI).toContain('const gateWaiting = approval?.mode === "manual" && step.status === "waiting";');
    expect(UI).toContain("{gateWaiting ? null : (");
  });

  it("and that chip says both halves", () => {
    expect(UI).toContain('`needs ${approval.approver_role || "a person"}`');
  });

  it("the button no longer repeats the role", () => {
    expect(UI).not.toContain("signs this off");
    // It still explains itself when there is no role to have named.
    expect(UI).toContain("Opens the approval with its recommendation, cost and risk.");
  });
});

describe("the sub-step is not a third nested box", () => {
  it("the active one is marked with a rule, not a container", () => {
    const at = CSS.indexOf('.dw-ship-step[data-active="true"] {');
    const block = CSS.slice(at, at + 320);
    expect(block).toContain("border-left: 2px solid");
    expect(block).toContain("background: transparent");
    expect(block).toContain("border-color: transparent");
  });
});

describe("an explanation sits on the thing it explains", () => {
  it("there is a reusable dot, click-driven so touch and keyboard reach it", () => {
    // Hover has no touch or keyboard equivalent, and these are the sentences a new operator
    // most needs.
    expect(UI).toContain("function Explain(");
    expect(UI).toContain('data-testid="dw-explain"');
    expect(UI).toContain("onClick={() => setOpen((o) => !o)}");
  });

  it("it closes on Escape and on click-away", () => {
    const at = UI.indexOf("function Explain(");
    const block = UI.slice(at, at + 1200);
    expect(block).toContain('e.key === "Escape"');
    expect(block).toContain("wrap.current?.contains");
  });

  it("and it is labelled, so it is not a bare 'i' to a screen reader", () => {
    expect(UI).toContain('label="How setting a target works"');
    // The run-level actions carry theirs in the view table the action row renders from.
    expect(UI).toContain('explainLabel: "What stopping does"');
    expect(UI).toContain('explainLabel: "What restarting does"');
    expect(UI).toContain('explainLabel: "What removing does"');
  });
});

describe("the footer is a row of actions, not a column of essays", () => {
  it("each button carries its own explanation", () => {
    // REWRITTEN for server-decided actions (a cancelled run whose research failed offered
    // "Build on this research"): the row no longer hand-writes six buttons; it renders the
    // run's `actions`, one `dw-run-action` per action, each with its `Explain` beside it.
    // Six keys, each with its own explanation in the view table — re-check, stop,
    // stop-and-run-again, run again, remove, build-on-this-research.
    const at = UI.indexOf("export function RunActionsBar(");
    const bar = UI.slice(at, UI.indexOf("\n}\n", at));
    expect(UI).toContain('data-testid="dw-run-actions"');
    expect(bar.split('className="dw-run-action"').length - 1).toBe(1);
    expect(bar).toContain("<Explain label={view.explainLabel}>");
    const table = UI.slice(UI.indexOf("const RUN_ACTION_VIEW"), at);
    expect((table.match(/\n    explainLabel: "/g) ?? []).length
      + (table.match(/, explainLabel: "/g) ?? []).length).toBe(6);
  });

  it("and none of them is followed by a permanent paragraph", () => {
    const at = UI.indexOf("export function RunActionsBar(");
    const bar = UI.slice(at, UI.indexOf("\n}\n", at));
    expect(bar).not.toContain('<span className="detail">');
  });

  it("there is no dead button left to explain", () => {
    // There used to be one — Run again, disabled mid-flight, explaining that you should
    // "cancel this run first" for a control that did not exist. The explanation was good and
    // the button was the problem: it now stops the run and starts a new one. A button the
    // server sends DISABLED now says why, on itself and in its dot.
    expect(UI).not.toContain('data-testid="dw-rerun-blocked"');
    const at = UI.indexOf('testId: "dw-stop-and-rerun"');
    expect(UI.slice(at, at + 900)).toContain("explain:");
    expect(UI).toContain("`Not available: ${action.reason}.`");
  });

  it("and the destructive one says what it destroys", () => {
    const at = UI.indexOf('testId: "dw-stop"');
    expect(UI.slice(at, at + 700)).toContain("Stops the work, not just the record");
  });

  it("the action and its dot cannot be orphaned by wrapping", () => {
    expect(CSS).toContain(".dw-run-action { display: inline-flex;");
  });
});

describe("the card wears the rail's number", () => {
  it("so the two do not have to be matched by name", () => {
    expect(UI).toContain('data-testid="dw-detail-n"');
    expect(UI).toContain("stepIndex + 1");
  });

  it("and the index comes from the run, not a guess", () => {
    expect(UI).toContain("stepIndex={run.steps.findIndex((s) => s.step_id === current.step_id)}");
  });
});

describe("scaffolding comes down once the building is up", () => {
  it("no numbers once a target is bound — there is one thing to do", () => {
    expect(UI).toContain('data-testid="dw-ship-single"');
    const at = UI.indexOf('data-testid="dw-ship-single"');
    const block = UI.slice(at, at + 700);
    expect(block).not.toContain('className="dw-ship-n"');
  });

  it("but the numbered plan stays until it has done its job", () => {
    // The reported bug was pressing Review and decide without setting a target. While no target
    // exists, the order IS the point.
    expect(UI).toContain('data-testid="dw-step-target"');
    expect(UI).toContain('<span className="dw-ship-n">1</span>');
    expect(UI).toContain('<span className="dw-ship-n">2</span>');
  });

  it("and the footer group says what it is, so its buttons need less", () => {
    expect(UI).toContain("Workflow management");
    expect(CSS).toContain(".dw-run-actions-label");
  });
});
