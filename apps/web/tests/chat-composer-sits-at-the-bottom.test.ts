import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { join } from "node:path";

/**
 * The composer floated in the middle of the panel with an empty band beneath it.
 *
 * The transcript was capped at `max-height: 52vh` and the input simply followed it, so in any
 * box taller than half the viewport — the dock, a full page — everything stopped halfway and
 * the rest was dead space. The layout also lived in INLINE styles on the JSX, which is why the
 * dock could only argue with it using `!important` and lost on the one property that mattered.
 *
 * The fix is a shape, not a measurement: the shell has a definite height, the transcript takes
 * whatever is left, and the input is the last thing in the column. That holds at any panel
 * width, any zoom, and on a phone — which is what "by form factor, not by pixel" means.
 *
 * These assertions read the declarations that PRODUCE the behaviour rather than a screenshot,
 * because the failure was never visible to a test that only checks that the elements exist.
 */
const CSS = readFileSync(join(process.cwd(), "app/globals.css"), "utf8");
const UI = readFileSync(join(process.cwd(), "app/agent-ui.tsx"), "utf8");

/** The declarations inside one selector's block. */
function rule(selector: string): string {
  const at = CSS.indexOf(`\n${selector} {`);
  expect(at, `no rule for ${selector}`).toBeGreaterThan(-1);
  return CSS.slice(at, CSS.indexOf("}", at));
}

describe("the transcript takes the slack", () => {
  it("it grows and shrinks rather than claiming a fixed slice of the viewport", () => {
    const log = rule(".agent-chat-log");
    expect(log).toContain("flex: 1 1 auto");
    // The exact cause of the dead band. A cap in vh is a guess about the window that is wrong
    // in every panel narrower or shorter than the guess.
    expect(log).not.toMatch(/max-height:\s*\d+vh/);
  });

  it("it can shrink below its content, or the column pushes the input off the bottom", () => {
    // min-height:0 is the whole trick. Without it a flex child refuses to shrink past its
    // content and the overflow goes to the parent instead of scrolling here.
    expect(rule(".agent-chat-log")).toContain("min-height: 0");
    expect(rule(".agent-chat-main")).toContain("min-height: 0");
  });

  it("and it is the thing that scrolls", () => {
    expect(rule(".agent-chat-log")).toContain("overflow-y: auto");
  });
});

describe("the composer is the last thing in the column", () => {
  it("it is never compressed to make room", () => {
    expect(rule(".agent-composer")).toContain("flex: 0 0 auto");
  });

  it("the panel itself does not scroll, so the input cannot scroll away", () => {
    // With a scrolling dock body the composer travelled with the conversation — the one
    // element that has to be reachable without scrolling back.
    expect(rule(".bx-dock-body")).toContain("overflow: hidden");
  });

  it("the shell has a definite height, which is what makes 'the bottom' mean anything", () => {
    const shell = rule(".agent-chat-shell");
    expect(shell).toMatch(/height:\s*min\(/);
    // In the dock it is the PANEL's height, not a fraction of the window: a viewport fraction
    // stops short of the panel and leaves exactly the band this test is named after.
    expect(rule(".bx-dock .agent-chat-shell")).toContain("height: 100%");
  });
});

describe("the layout is in the stylesheet", () => {
  it("the shell no longer carries its grid as an inline style", () => {
    // Inline styles beat every class rule, so the dock could not restyle what it contains
    // without `!important` — and could not override the one property that broke it at all.
    expect(UI).toContain('<div className="agent-chat-shell"');
    expect(UI).not.toContain('style={{ display: "grid", gridTemplateColumns:');
  });

  it("nor does the transcript carry its own height", () => {
    expect(UI).not.toContain('style={{ flex: 1, overflowY: "auto", minHeight: 260 }}');
  });

  it("and the dock's overrides no longer need to shout", () => {
    expect(rule(".bx-dock .agent-chat-shell")).not.toContain("!important");
  });
});

describe("it holds on a small screen", () => {
  it("the dock becomes the full width rather than a sliver", () => {
    expect(CSS).toContain(".bx-dock { width: 100vw !important; }");
  });

  it("the shell keeps a floor so the transcript cannot vanish entirely", () => {
    expect(rule(".agent-chat-shell")).toMatch(/min-height:\s*\d+px/);
  });
});
