/**
 * The segment between two steps in a run gets a small cap at each end, so the row reads as
 * joined nodes rather than boxes with lines lying between them.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const css = readFileSync(join(__dirname, "../app/globals.css"), "utf8");
const link = css.slice(css.indexOf(".dw-link {"), css.indexOf("@keyframes dw-flow"));

describe("the run connector", () => {
  it("has a cap at both ends, not one", () => {
    expect(link).toContain(".dw-link::before");
    expect(link).toContain(".dw-link::after");
    expect(link).toContain("left: -1px");
    expect(link).toContain("right: -1px");
  });

  it("positions them against a positioned bar", () => {
    // Absolute children of a static parent would escape to the nearest positioned ancestor and
    // land somewhere else entirely.
    expect(link).toMatch(/\.dw-link \{[^}]*position: relative/);
  });

  it("colours the caps from the same state as the bar", () => {
    // The bar is drawn with `background` and the caps with `currentColor`, so each state has to
    // set both — otherwise a completed green segment ends in two grey dots.
    expect(link).toContain("background: currentColor");
    for (const state of ["past", "future", "moving"]) {
      const from = link.indexOf(`[data-flow="${state}"]`);
      // To the END of that rule, not a fixed window: the `moving` rule carries a multi-line
      // gradient and a comment before it reaches the colour, and a short slice reported a
      // missing declaration that was there.
      const rule = link.slice(from, link.indexOf("}", from));
      expect(rule, `[data-flow="${state}"] must set color for the caps`).toContain("color:");
    }
  });
});
