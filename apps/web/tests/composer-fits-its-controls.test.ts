/**
 * The composer stopped competing with its own controls.
 *
 * "the buttons are going right and textbox is shrinking" — six things shared one flex row:
 * the textarea, three peer buttons (Ask, Research, Build →), the workflow picker and the agent
 * picker. The textarea, being the only flexible one, gave up its width to all of them and
 * collapsed to roughly half the panel.
 *
 * The three peers were also a claim about likelihood that was not true: asking is free and
 * constant, building spends money and happens once. One split action says that; three buttons
 * of equal weight say the opposite.
 *
 * BUILD IS THE FACE, on the operator's call after I argued for Ask: "most of the time we hit
 * Build to trigger the complete workflow." Their product, their decision.
 *
 * The hazard I raised is real and is solved a different way rather than by argument. Enter used
 * to send, which is right when the primary is free; the primary now convenes a panel, writes a
 * PRD and starts code. A box whose main content is a multi-line brief is a box where Shift is
 * pressed dozens of times, and one slip would start a governed pipeline. So Enter is a newline
 * and Cmd/Ctrl+Enter submits — the convention every composer uses when submitting is expensive.
 */
import { describe, expect, it } from "vitest";

import { codeOnly } from "./_helpers/source";

const HERO = codeOnly("app/ask-boss-hero.tsx");
const SPLIT = codeOnly("app/split-action.tsx");
const CSS = codeOnly("app/globals.css");

describe("the box is for writing; the controls sit beneath it", () => {
  it("the composer stacks instead of sharing one row", () => {
    const at = CSS.indexOf(".askboss-box {");
    expect(CSS.slice(at, at + 260)).toContain("flex-direction: column");
  });

  it("and the settings are on a bar, not inside the textarea's row", () => {
    expect(HERO).toContain('className="askboss-bar"');
    const bar = HERO.indexOf('className="askboss-bar"');
    const input = HERO.indexOf('className="askboss-input"');
    expect(bar).toBeGreaterThan(input);
  });

  it("the bar wraps rather than squeezing the box", () => {
    const at = CSS.indexOf(".askboss-bar {");
    expect(CSS.slice(at, at + 220)).toContain("flex-wrap: wrap");
  });

  it("and the agents panel opens over the page, not inside the row", () => {
    // A panel that expands inside a flex row shoves everything beside it sideways, which is
    // how this row got crowded in the first place.
    const at = CSS.indexOf(".askboss-agents-panel {");
    const rule = CSS.slice(at, at + 700);
    expect(rule).toContain("position: absolute");
    // AND IT NEEDS ITS OWN WIDTH. `right: 0` stretched it to its inline-flex parent — about
    // 150px — so every word wrapped onto its own line and the text spilled over the cards
    // underneath. An opaque background, because it now overlaps them.
    expect(rule).toContain("min-width:");
    expect(rule).not.toContain("right: 0");
    expect(rule).toContain("background: var(--surface");
  });
});

describe("one action, with the rest behind a caret", () => {
  it("there are no longer three peer buttons", () => {
    expect(HERO).toContain("<SplitAction");
    expect(HERO).not.toContain('className="askboss-actions-inline"');
  });

  it("Build is the face — the action taken most of the time", () => {
    const at = HERO.indexOf("primary={{");
    expect(HERO.slice(at, at + 200)).toContain('id: "build"');
  });

  it("and a bare Enter cannot start it", () => {
    // The primary convenes a panel, writes a PRD and starts code. A multi-line brief means
    // Shift is pressed dozens of times; one slip must not spend money.
    expect(HERO).toContain("e.metaKey || e.ctrlKey");
    expect(HERO).not.toContain('if (e.key === "Enter" && !e.shiftKey)');
  });

  it("the alternatives are in the menu, each saying what it costs", () => {
    const at = HERO.indexOf("others={[");
    const block = HERO.slice(at, at + 1400);
    // Two entries, not three: talking to BOSS about an idea and asking about the running
    // system were merged into one conversation that decides which the question is.
    expect(block).toContain('id: "discuss"');
    expect(block).toContain('id: "research"');
    expect(block).not.toContain('id: "ask"');
    // Free and expensive are labelled as such: a menu that does not distinguish them is how
    // somebody spends money reaching for an answer.
    expect(block).toContain('cost: "free"');
    expect(block).toContain('cost: "spends money"');
    expect(SPLIT).toContain("split-action-cost");
  });

  it("the placeholder leads with what the button now does", () => {
    // It said "Ask a question, or describe something to build" while the primary was Build.
    expect(HERO).toContain("Describe what to build");
  });

  it("each item says what it does, not just its name", () => {
    expect(SPLIT).toContain("split-action-item-hint");
  });

  it("and the menu can be dismissed without choosing", () => {
    // A menu that only closes by picking something traps the pointer.
    expect(SPLIT).toContain('document.addEventListener("mousedown", onDown)');
    expect(SPLIT).toContain('if (e.key === "Escape") setOpen(false)');
  });
});
