import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { codeOnly } from "./_helpers/source";
import { SplitAction } from "../app/split-action";

/**
 * "I pressed Build and it is still doing the Discussion."
 *
 * The split button's primary was a `type="submit"`, so pressing it never called the action it
 * carried — it submitted the enclosing form, whose onSubmit was still wired to Ask from when
 * Ask was the primary. Flipping the face to Build changed the label and nothing else. The
 * operator pressed Build on a finished brief and got a forty-second chat turn, a reply full of
 * buttons, and no workflow run: the console ran a different action than the one it was showing.
 */
const HERO = codeOnly("app/ask-boss-hero.tsx");

describe("the primary button runs its own action", () => {
  const markup = renderToStaticMarkup(
    React.createElement(SplitAction, {
      primary: { id: "build", label: "Build →", hint: "h", onRun: () => {} },
      others: [{ id: "ask", label: "Ask", hint: "h", onRun: () => {} }],
    }),
  );

  it("is not a submit button borrowing whatever the form happens to do", () => {
    const primary = markup.slice(markup.indexOf('class="split-action-main"'));
    expect(primary.slice(0, 200)).toContain('type="button"');
    expect(primary.slice(0, 200)).not.toContain('type="submit"');
  });

  it("and the component actually calls the handler it was given", () => {
    // Declaring `onRun` and never calling it is the trap the next caller falls into too.
    expect(codeOnly("app/split-action.tsx")).toContain("onClick={primary.onRun}");
  });
});

describe("the composer's form agrees with the button inside it", () => {
  it("submitting runs the same action the face shows", () => {
    const onSubmit = HERO.slice(HERO.indexOf("onSubmit={(e)"), HERO.indexOf("onSubmit={(e)") + 120);
    expect(onSubmit).toContain("assign(input)");
    // The primary IS Build, and Build IS assign.
    const primary = HERO.slice(HERO.indexOf("id: \"build\""), HERO.indexOf("id: \"build\"") + 120);
    expect(primary).toContain("assign(input)");
  });

  it("the free conversation is reachable, but only from the menu", () => {
    const others = HERO.slice(HERO.indexOf("others={["), HERO.indexOf("others={[") + 1400);
    expect(others).toContain("discuss(input)");
  });
});

describe("the options are readable when they open", () => {
  const SRC = codeOnly("app/split-action.tsx");
  const CSS = codeOnly("app/globals.css");

  it("the menu leaves the composer box rather than fighting it with z-index", () => {
    // `.askboss-box` has `backdrop-filter: blur(6px)`, which creates a stacking context — so a
    // menu inside it ranks only against its siblings, and the reply below painted through it.
    expect(SRC).toContain("createPortal");
    expect(SRC).toContain("document.body");
    expect(CSS).toContain("position: fixed; z-index: var(--z-menu);");
  });

  it("and it is opaque, not whatever --surface happens to be", () => {
    const at = CSS.indexOf(".split-action-menu {");
    expect(CSS.slice(at, at + 900)).toContain("background: #101A30");
    expect(CSS.slice(at, at + 900)).not.toContain("var(--surface");
  });

  it("and it does not turn white because the OPERATING SYSTEM is in light mode", () => {
    // This console has no light theme — every other surface stays dark whatever the OS says.
    // A prefers-color-scheme override painted the menu white and its light ink vanished.
    const at = CSS.indexOf(".split-action-menu {");
    const block = CSS.slice(at, at + 900);
    expect(block).not.toContain("prefers-color-scheme");
    expect(block).toContain("color: var(--ink");
  });

  it("click-away asks the portalled menu too", () => {
    // Otherwise mousedown closes it and the item's click never lands.
    expect(SRC).toContain("menu.current?.contains(target)");
  });

  it("it flips above the button when there is no room below", () => {
    expect(SRC).toContain("const up = room < 260");
  });
});
