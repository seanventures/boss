import { describe, expect, it } from "vitest";

import { codeOnly } from "./_helpers/source";

/**
 * "Not just deploy — review all the workflow steps and follow this consistent pattern."
 *
 * One rule, applied to every step kind:
 *
 *   an EXPLANATION goes on the dot beside the thing it explains;
 *   PROGRESS stays visible, because a minute of silence after a click reads as a click that
 *     did nothing;
 *   a WARNING stays visible, because it is news and not elaboration.
 *
 * That third distinction is the one worth guarding. "Everything grey becomes a tooltip" would
 * have hidden "starting a preview, this takes up to a minute" and "this link opens a LATER
 * build", which are the two sentences on this screen an operator cannot afford to miss.
 */
const UI = codeOnly("app/delivery-workflows-ui.tsx");
const CSS = codeOnly("app/globals.css");

describe("every step follows the same rule", () => {
  it("retrying a failed step explains itself on the dot", () => {
    const at = UI.indexOf('data-testid="dw-step-retry"');
    expect(UI.slice(at, at + 400)).toContain("<Explain");
    expect(UI).toContain("Runs this step only; everything before it stays done.");
  });

  it("a decided gate keeps its headline and moves the elaboration", () => {
    // "Approved — starting." is news. The paragraph after it is not.
    expect(UI).toContain("<strong>Approved — starting.</strong>");
    const at = UI.indexOf("<strong>Approved — starting.</strong>");
    expect(UI.slice(at, at + 300)).toContain("<Explain");
  });

  it("an expired gate does too, and keeps its way out", () => {
    const at = UI.indexOf("The gate expired before anyone answered it.");
    const block = UI.slice(at, at + 700);
    expect(block).toContain("<Explain");
    expect(block).toContain('data-testid="dw-step-reopen"');
  });

  it("and the deploy step it started from is unchanged", () => {
    expect(UI).toContain('label="How setting a target works"');
    expect(UI).toContain('label="What approving does"');
  });
});

describe("progress and warnings are not explanations", () => {
  it("a preview that is starting says so in the layout, not behind a click", () => {
    const at = UI.indexOf("{starting ? (");
    const block = UI.slice(at, at + 400);
    expect(block).toContain('data-testid="dw-preview-hint"');
    expect(block).toContain("this takes up to a minute");
    expect(block).not.toContain("<Explain");
  });

  it("a preview of the WRONG build is a warning and stays visible", () => {
    expect(UI).toContain("the link opens the same app from a LATER");
    const at = UI.indexOf("the link opens the same app from a LATER");
    expect(UI.slice(at - 300, at)).toContain('data-tone="warn"');
  });

  it("but what a preview IS goes on the dot", () => {
    expect(UI).toContain('label="What this preview is"');
  });
});

describe("an open run is a different object, not a thicker stripe", () => {
  it("the card says whether it is open", () => {
    expect(UI).toContain('data-open={open ? "true" : undefined}');
  });

  it("and open changes border, surface and elevation — not just one of them", () => {
    const at = CSS.indexOf('.dw-run[data-open="true"] {');
    const block = CSS.slice(at, at + 400);
    expect(block).toContain("border-color");
    expect(block).toContain("background");
    expect(block).toContain("box-shadow");
  });

  it("its header is separated from the body it opened", () => {
    expect(CSS).toContain('.dw-run[data-open="true"] > .dw-run-head {');
  });

  it("status still reads down the left edge, independently of open", () => {
    // Two different questions — "which run is this" and "this is the one I opened" — so two
    // different signals.
    expect(CSS).toContain('.dw-runs .dw-run[data-status="waiting"]      { border-left-color:');
    expect(CSS).toContain(".dw-runs .dw-run { border-left-width: 4px; }");
  });

  it("and there is real air between cards", () => {
    expect(CSS).toContain(".dw-runs { display: grid; gap: 16px; }");
  });
});
