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
  it("Ask BOSS explains itself in a tooltip", () => {
    expect(UI).toContain('title="Opens the side dock with this step\'s state and message already filled in"');
  });

  it("and the sentence is gone from the body", () => {
    expect(UI).not.toContain("Opens the side dock with this step&rsquo;s state");
  });
});
