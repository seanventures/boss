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
 * WHY "ASK" IS THE FACE. Enter sends — the one convention a composer cannot break — and Enter
 * fires the primary button. A face reading "Build" would mean a stray Enter starts a governed
 * pipeline that spends money.
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
    expect(CSS.slice(at, at + 260)).toContain("position: absolute");
  });
});

describe("one action, with the rest behind a caret", () => {
  it("there are no longer three peer buttons", () => {
    expect(HERO).toContain("<SplitAction");
    expect(HERO).not.toContain('className="askboss-actions-inline"');
  });

  it("Ask is the face, because Enter fires it", () => {
    const at = HERO.indexOf("<SplitAction");
    const block = HERO.slice(at, at + 900);
    expect(block).toContain('id: "ask"');
    // And the primary is a submit, so the button and the Enter key do the same thing.
    expect(SPLIT).toContain('type="submit"');
    expect(HERO).toContain("e.preventDefault(); ask(input);");
  });

  it("Research and Build are in the menu, and say what they cost", () => {
    const at = HERO.indexOf("<SplitAction");
    const block = HERO.slice(at, at + 1200);
    expect(block).toContain('id: "research"');
    expect(block).toContain('id: "build"');
    // A menu that hides the expensive option among the free ones is how somebody spends money
    // by accident.
    expect(block.match(/cost: "spends money"/g)?.length).toBe(2);
    expect(SPLIT).toContain("split-action-cost");
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
