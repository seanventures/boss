import { describe, expect, it } from "vitest";

import { codeOnly } from "./_helpers/source";

/**
 * The app action bar reads as one bar, and every control carries an icon.
 *
 * It had grown into five unrelated chips at four different heights — a bare link, a filled
 * button, a badge, a ghost button — with meaning carried by typed glyphs (↗ ▶ ↻) that render
 * differently on every platform and mean nothing to a screen reader.
 */
const SRC = codeOnly("app/code-tasks-ui.tsx");
const CSS = codeOnly("app/globals.css");

describe("every control on the bar has an icon", () => {
  it.each([
    ["the PR link", "GitPullRequest"],
    ["Open preview", "ArrowSquareOut"],
    ["Startup logs", "TerminalWindow"],
    ["Rebuild preview", "ArrowsClockwise"],
    ["Run preview", "PlayCircle"],
    ["Ask BOSS", "ChatCircleDots"],
  ])("%s uses <%s>", (_label, icon) => {
    expect(SRC).toContain(`<${icon} `);
  });

  it("imports them all from the icon set already in use", () => {
    const imports = SRC.slice(SRC.indexOf("@phosphor-icons/react") - 400,
                              SRC.indexOf("@phosphor-icons/react"));
    for (const icon of ["GitPullRequest", "TerminalWindow", "ArrowSquareOut", "ChatCircleDots"]) {
      expect(imports).toContain(icon);
    }
  });

  it("drops the typed glyphs the icons replaced", () => {
    // "▶ Run preview" and "↻ Rebuild preview" rendered as tofu on some systems and were read
    // aloud as "black right-pointing triangle".
    expect(SRC).not.toContain("▶ Run preview");
    expect(SRC).not.toContain("↻ Rebuild preview");
    expect(SRC).not.toContain("Open preview ↗");
  });

  it("hides the icons from assistive tech, since each sits beside its own label", () => {
    const withIcons = SRC.match(/<(GitPullRequest|TerminalWindow|ChatCircleDots|PlayCircle)[^>]*>/g) ?? [];
    expect(withIcons.length).toBeGreaterThan(0);
    for (const tag of withIcons) expect(tag).toContain("aria-hidden");
  });
});

describe("the bar is one row, not five leftovers", () => {
  const rule = CSS.slice(CSS.indexOf(".ct-detail-actions .cap-btn,"),
                         CSS.indexOf(".ct-live-dot {"));

  it("gives every control the same height and shape", () => {
    expect(rule).toContain("min-height: 32px");
    expect(rule).toContain("border-radius: 9px");
    expect(rule).toContain("align-items: center");
  });

  it("keeps the status pill from looking pressable", () => {
    // A badge that looks like a button gets clicked, and clicking it does nothing.
    expect(rule).toContain("cursor: default");
  });
});

describe("the live pill", () => {
  it("pulses, because 'running' is a claim about right now", () => {
    expect(CSS).toContain("@keyframes ct-live-pulse");
    expect(SRC).toContain('className="ct-live-dot"');
  });

  it("only appears while the preview is actually live", () => {
    // `starting` counts: a preview now exists from the moment the button is pressed and is
    // genuinely doing something, so a still dot beside "building…" would understate it. What
    // must NOT get a dot is stopped, failed, expired, or verified-but-not-running.
    const dot = SRC.slice(SRC.indexOf("ct-live-pill"), SRC.indexOf("preview: {p.status"));
    expect(dot).toContain('p.status === "running" || p.status === "starting"');
    expect(dot).toContain('className="ct-live-dot"');
    for (const dead of ["stopped", "failed", "expired"]) {
      expect(dot).not.toContain(`"${dead}"`);
    }
  });

  it("still reads as live with motion turned off", () => {
    // Removing the animation must not remove the signal.
    const reduced = CSS.slice(CSS.indexOf("@media (prefers-reduced-motion: reduce) {\n  .ct-detail-actions"));
    expect(reduced).toContain(".ct-live-dot, .spin { animation: none; }");
    expect(reduced).toContain("box-shadow: 0 0 0 3px");
  });
});
