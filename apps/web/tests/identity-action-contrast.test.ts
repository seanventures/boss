/** Numerical token guard; browser evidence must separately establish rendered styles. */
import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

const css = readFileSync("app/identity/identity.css", "utf8");
const homeCss = readFileSync("app/command-center/cc.css", "utf8");
const rgb = (hex: string) => [1, 3, 5].map((i) => Number.parseInt(hex.slice(i, i + 2), 16));
const luminance = (channels: number[]) => channels.map((n) => {
  const s = n / 255;
  return s <= 0.04045 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
}).reduce((sum, n, i) => sum + n * [0.2126, 0.7152, 0.0722][i], 0);

describe("enabled identity action text colors", () => {
  it.each(["idn-action-grad", "idn-action-hover"])("%s keeps normal-size white labels readable across its gradient", (token) => {
    const declaration = new RegExp(`--${token}:\\s*([^;]+);`).exec(css)?.[1];
    const stops = declaration?.match(/#[0-9a-f]{6}/gi);
    expect(stops).toHaveLength(2);
    const [start, end] = stops!.map(rgb);
    const text = rgb(/--idn-white:\s*(#[0-9a-f]{6})/i.exec(css)![1]);
    const ink = luminance(text);
    for (let step = 0; step <= 100; step += 1) {
      const bg = luminance(start.map((channel, i) => channel + (end[i] - channel) * step / 100));
      expect((Math.max(ink, bg) + 0.05) / (Math.min(ink, bg) + 0.05), `${token} at ${step}%`).toBeGreaterThanOrEqual(4.5);
    }
  });
});

describe("primary home action text colors", () => {
  it.each([".cc-btn.primary", ".cc-btn.primary:hover:not(:disabled)"])("%s keeps a white label readable across its gradient", (selector) => {
    const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    const rule = new RegExp(`${escaped}\\s*\\{([^}]+)}`).exec(homeCss)?.[1];
    const stops = /background:\s*([^;]+);/.exec(rule ?? "")?.[1].match(/#[0-9a-f]{6}/gi);
    expect(stops).toHaveLength(2);
    const [start, end] = stops!.map(rgb);
    for (let step = 0; step <= 100; step += 1) {
      const bg = luminance(start.map((channel, i) => channel + (end[i] - channel) * step / 100));
      expect(1.05 / (bg + 0.05), `${selector} at ${step}%`).toBeGreaterThanOrEqual(4.5);
    }
  });
});
