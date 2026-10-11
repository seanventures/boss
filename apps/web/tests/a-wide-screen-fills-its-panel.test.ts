/**
 * On a wide screen the page fills the area beside the sidebar, what is inside a panel fills the
 * panel, and the type grows evenly.
 *
 * The first large-screen scale widened the outer containers by up to 520px while inner layouts
 * (the Factory floor, Agents › Live, the Command Center) kept their own caps: at 2000-2560px the
 * operator saw a wide panel holding a narrow column off to one side, and the root grew only 15%.
 * Enforced here:
 *  - wider than 1440px, every page and inner container token is 100% and the shell has no cap;
 *    at ≤1440px each token is still the width it replaced (laptops unchanged);
 *  - the root size is 19px to 1440px, +20% at 1920, +30% from 2560, so body, labels and headings
 *    (all rem) grow by one factor;
 *  - a flex cell never centres a column of text it must truncate (the Factory row overflow: a
 *    column flexbox with align-items:center lets a long title spill out both sides of its cell).
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

import { declarations } from "./a-page-width-is-a-layout-token.test";

const ROOT = join(__dirname, "..");
const scale = readFileSync(join(ROOT, "app/layout-scale.css"), "utf8");

function block(css: string, prelude: string): string {
  const at = css.indexOf(prelude);
  expect(at, prelude).toBeGreaterThanOrEqual(0);
  let depth = 0;
  for (let i = css.indexOf("{", at); i < css.length; i++) {
    if (css[i] === "{") depth++;
    if (css[i] === "}" && --depth === 0) return css.slice(at, i + 1);
  }
  throw new Error(`unclosed ${prelude}`);
}

/** The html font-size, evaluated at a viewport width (px): clamp(lo, min(line1, line2), hi). */
function rootPx(vw: number): number {
  const d = declarations(scale).find((x) => x.selector.trim() === "html" && x.prop === "font-size");
  const line = String.raw`calc\(([\d.]+)px \+ \(100vw - ([\d.]+)px\) \* ([\d.]+)\)`;
  const m = d?.value.match(new RegExp(String.raw`^clamp\(([\d.]+)px, min\(${line}, ${line}\), ([\d.]+)px\)$`));
  expect(m, d?.value).toBeTruthy();
  const n = m!.slice(1).map(Number);
  const [lo, a0, a1, as, b0, b1, bs, hi] = n;
  return Math.min(Math.max(lo, Math.min(a0 + (vw - a1) * as, b0 + (vw - b1) * bs)), hi);
}

describe("a wide screen fills its panel", () => {
  it("at ≤1440px every page token is the width it replaced", () => {
    const root = block(scale, ":root {");
    for (const [token, px] of [["--page-max", 1240], ["--page-max-wide", 1280], ["--page-max-compact", 1120], ["--page-max-mid", 960]] as const) {
      expect(root, token).toMatch(new RegExp(`${token}: ${px}px;`));
    }
  });

  it("wider than 1440px, no page or inner container keeps a cap", () => {
    const wide = block(scale, "@media (min-width: 1441px)");
    expect(wide).toMatch(/--shell-max: none;/);
    for (const token of ["--page-max", "--page-max-wide", "--page-max-compact", "--page-max-mid"]) {
      expect(wide, token).toMatch(new RegExp(`${token}: 100%;`));
    }
    // ~94% of the area beside the sidebar: a 3% gutter each side from 1600px.
    expect(block(scale, "@media (min-width: 1600px)")).toMatch(/--page-gutter: 3%;/);
  });

  it("the type grows by one factor: 19px to 1440, +20% at 1920, +30% from 2560", () => {
    expect(rootPx(390)).toBeCloseTo(19, 1);
    expect(rootPx(1440)).toBeCloseTo(19, 1);
    expect(rootPx(1920)).toBeCloseTo(19 * 1.2, 1);
    expect(rootPx(2560)).toBeCloseTo(19 * 1.3, 1);
    expect(rootPx(3840)).toBeCloseTo(19 * 1.3, 1);
  });

  it("a Factory row's name cell is a row that truncates, never a centred column", () => {
    const css = readFileSync(join(ROOT, "app/factory/factory.css"), "utf8");
    const name = declarations(css).filter((d) => d.selector.trim() === ".fx-row-name");
    const dir = name.filter((d) => d.prop === "flex-direction").map((d) => d.value);
    expect(dir).toEqual(["row"]);
    expect(name.some((d) => d.prop === "min-width" && d.value === "0")).toBe(true);
  });
});
