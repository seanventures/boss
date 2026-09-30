import { describe, expect, it } from "vitest";

import { codeOnly } from "./_helpers/source";

/**
 * The banner that says the machine is struggling carries a warning icon.
 *
 * It fired for real on a Mac whose swap was 96.5% used:
 *
 *   This computer is struggling to keep up
 *   The computer stopped running BOSS for about 2 minutes in total, in stretches...
 *
 * — as a block of amber text with nothing to mark it as a warning at a glance. It is the one
 * thing on the page that has to be noticed while somebody is reading step timings past it.
 */
const SRC = codeOnly("app/host-advice.tsx");
const CSS = codeOnly("app/globals.css");

describe("the host-advice banner", () => {
  it("shows a warning icon", () => {
    expect(SRC).toContain("<Warning ");
    expect(SRC).toContain('className="host-advice-icon"');
  });

  it("takes the icon from the set the console already uses", () => {
    // code-tasks-ui.tsx renders this banner and imports its own icons from here. A
    // hand-drawn triangle would be a second warning glyph to keep in step for no gain.
    expect(SRC).toContain('from "@phosphor-icons/react"');
    expect(SRC).toContain("import { Warning }");
  });

  it("hides the icon from screen readers, which the words already serve", () => {
    const tag = SRC.slice(SRC.indexOf("<Warning "), SRC.indexOf("<Warning ") + 220);
    expect(tag).toContain("aria-hidden");
    expect(SRC).toContain('role="status"');
  });

  it("colours the icon with the severity, from one rule rather than two icons", () => {
    expect(CSS).toContain(".host-advice-icon");
    expect(CSS).toMatch(/\.host-advice\[data-severity="critical"\] \.host-advice-icon/);
  });

  it("keeps the icon beside the text rather than letting it wrap above it", () => {
    // The detail line is long. With the old single wrapping row, an icon added as a plain
    // sibling would drop onto a line of its own exactly when the message is longest.
    expect(SRC).toContain("host-advice-body");
    expect(CSS).toContain(".host-advice-body");
    expect(CSS).toMatch(/\.host-advice-icon \{[^}]*flex: none/);
  });

  it("does not let a long message push the icon off the edge", () => {
    // A flex child will not shrink below its content width without this, so the body pushes
    // the icon out of the box instead of wrapping.
    expect(CSS).toMatch(/\.host-advice-body \{[^}]*min-width: 0/);
  });

  it("still renders nothing on a healthy machine", () => {
    // The whole point of the banner is being believed the one time it appears.
    expect(SRC).toContain("if (!advice?.show) return null;");
  });
});
