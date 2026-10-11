/**
 * A `.wf-tag` on a <button> is a dark chip, not the browser's button face.
 *
 * The run card's Agents and Credits are `<button class="wf-tag">`. The tag was written for a
 * span and set no background, so the browser's own white button face showed through: grey text
 * on a near-white pill on the dark card (operator report, 2026-10-11). This holds every button
 * that wears the tag to a fill, an ink and a selected state from the console's tokens, and the
 * text on each to WCAG AA (4.5:1) against what is really behind it.
 */
import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

const css = readFileSync("app/workflows/library.css", "utf8");
const globals = readFileSync("app/globals.css", "utf8");

/** A dark-theme token's value from the first :root block that defines it. */
function token(name: string): string {
  const m = new RegExp(`--${name}:\\s*([^;]+);`).exec(globals);
  if (!m) throw new Error(`no --${name}`);
  return m[1].trim();
}
type RGBA = [number, number, number, number];
function color(value: string): RGBA {
  const v = value.trim();
  const ref = /^var\(--([\w-]+)\)$/.exec(v);
  if (ref) return color(token(ref[1]));
  const hex = /^#([0-9a-f]{6})$/i.exec(v);
  if (hex) return [0, 2, 4].map((i) => Number.parseInt(hex[1].slice(i, i + 2), 16)).concat(1) as RGBA;
  const rgba = /^rgba\(([^)]+)\)$/.exec(v);
  if (rgba) {
    const [r, g, b, a] = rgba[1].split(",").map((n) => Number(n.trim()));
    return [r, g, b, a];
  }
  throw new Error(`cannot read the colour ${v}`);
}
function over(top: RGBA, under: RGBA): RGBA {
  const a = top[3];
  return [0, 1, 2].map((i) => top[i] * a + under[i] * (1 - a)).concat(1) as RGBA;
}
function luminance(c: RGBA): number {
  return c.slice(0, 3).map((n) => {
    const s = n / 255;
    return s <= 0.04045 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
  }).reduce((sum, n, i) => sum + n * [0.2126, 0.7152, 0.0722][i], 0);
}
function contrast(a: RGBA, b: RGBA): number {
  const [x, y] = [luminance(a), luminance(b)];
  return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05);
}
function rule(selector: string): string {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const m = new RegExp(`(?:^|\\n|,\\s*)${escaped}\\s*(?:,[^{]*)?\\{([^}]+)\\}`).exec(css);
  if (!m) throw new Error(`no rule for ${selector}`);
  return m[1];
}
function prop(body: string, name: string): string | undefined {
  return new RegExp(`(?:^|;|\\s)${name}:\\s*([^;]+);`).exec(body)?.[1].trim();
}

describe("a tag that is a button", () => {
  const card = color(token("surface"));
  const base = rule("button.wf-tag");

  it("paints its own fill, so the browser's button face never shows", () => {
    expect(prop(base, "background")).toBeTruthy();
    expect(prop(base, "color")).toBeTruthy();
    expect(prop(base, "cursor")).toBe("pointer");
  });

  it("reads at AA at rest, on hover and when on", () => {
    const rest = over(color(prop(base, "background")!), card);
    expect(contrast(color(prop(base, "color")!), rest)).toBeGreaterThanOrEqual(4.5);
    const hover = rule("button.wf-tag:hover");
    expect(contrast(color(prop(hover, "color")!), rest)).toBeGreaterThanOrEqual(4.5);
    const on = rule('button.wf-tag[aria-pressed="true"]');
    const onFill = over(color(prop(on, "background")!), card);
    expect(contrast(color(prop(on, "color")!), onFill)).toBeGreaterThanOrEqual(4.5);
  });

  it("says when it is on, for a toggle and for a dialog it opened", () => {
    expect(css).toMatch(/button\.wf-tag\[aria-pressed="true"\],\s*button\.wf-tag\[aria-expanded="true"\]/);
    const on = rule('button.wf-tag[aria-pressed="true"]');
    expect(prop(on, "background")).not.toBe(prop(base, "background"));
  });
});
