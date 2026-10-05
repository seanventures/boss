/**
 * Faint and muted text meet WCAG AA (4.5:1) on every panel the console draws.
 *
 * The independent walk of Infra and Observability measured `--faint` (#64769a) at 3.49–3.79:1 on
 * the panels — every count, age, column head and "Find" placeholder failed AA — and the model
 * drawer faded the other models' names with `opacity: 0.5`, taking them to 3.7:1. Computed here
 * from the token values themselves, so a later palette change cannot quietly undo it.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const css = (f: string) => readFileSync(join(process.cwd(), "app", f), "utf8");
const ROOT = (() => {
  const src = css("design-system.css");
  const block = src.slice(src.indexOf(":root {"), src.indexOf("}", src.indexOf(":root {")));
  return Object.fromEntries([...block.matchAll(/--([a-z0-9-]+):\s*(#[0-9a-f]{6})/gi)].map((m) => [m[1], m[2]]));
})();

function lum(hex: string): number {
  const c = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255)
    .map((v) => (v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4));
  return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2];
}
const ratio = (a: string, b: string) => {
  const [hi, lo] = [lum(a), lum(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
};

const PANELS = ["void", "base", "panel", "panel-alt", "card", "raised"];

describe("faint and muted text on the console's panels", () => {
  for (const token of ["faint", "muted", "ink-soft"]) {
    for (const panel of PANELS) {
      it(`--${token} on --${panel} is at least 4.5:1`, () => {
        expect(ROOT[token], `--${token} is not a hex token`).toMatch(/^#/);
        expect(ROOT[panel], `--${panel} is not a hex token`).toMatch(/^#/);
        expect(ratio(ROOT[token], ROOT[panel])).toBeGreaterThanOrEqual(4.5);
      });
    }
  }

  it("a dimmed legend name is not faded with opacity (the model drawer's other models)", () => {
    const rule = css("observability/obs.css").match(/\.obs-legend-item\[data-dim="1"\]\s*\{([^}]*)\}/);
    expect(rule, "the dimmed legend rule is gone").not.toBeNull();
    expect(rule![1]).not.toMatch(/opacity/);
  });
});
