/**
 * Visors: the face of a BOSS crew robot. A dark glass plate with light behind it — the eyes and
 * the three mouth bars are light, never a human feature, which keeps every character clear of
 * the uncanny valley and of anyone's likeness.
 *
 * Geometry is in the kit's 100×100 box. The head plate spans x 22-78, y 26-70.
 * Level of detail: "band" (≤24px) is the glass and one strip of light; "bars" (32px) adds eyes
 * as plain bars and the mouth; "full" (≥48px) draws each visor's own eye shape and details.
 */
import { GLASS, type Palette } from "./palettes";
import { dict } from "./dict";

export type VisorLod = "band" | "bars" | "full";

export type Visor = {
  id: string;
  label: string;
  /** The glass, plus any frame drawn around it. */
  glass(p: Palette, lod: VisorLod): string;
  /** Eye centres, used for the plain bars at 32px. */
  eyes: [number, number][];
  /** The styled eyes at 48px and up. */
  styled(p: Palette): string;
  /** Where the mouth bars sit, and whether they need a grill of their own below the glass. */
  mouthY: number;
  grill?: boolean;
  /** Light-strip for the tiny sizes: x, y, width. */
  strip: [number, number, number];
};

const f = (n: number) => Number(n.toFixed(2));
const rect = (x: number, y: number, w: number, h: number, rx: number, fill: string, extra = "") =>
  `<rect x="${f(x)}" y="${f(y)}" width="${f(w)}" height="${f(h)}" rx="${f(rx)}" fill="${fill}"${extra}/>`;
const circle = (cx: number, cy: number, r: number, fill: string, extra = "") =>
  `<circle cx="${f(cx)}" cy="${f(cy)}" r="${f(r)}" fill="${fill}"${extra}/>`;

const BAND = (p: Palette) => rect(28, 37, 44, 25, 10, GLASS) + rect(30, 38.5, 18, 3, 1.5, p.glow, ` opacity="0.12"`);
const pills = (p: Palette, xs: number[], y: number, w: number, h: number) =>
  xs.map((x) => rect(x - w / 2, y - h / 2, w, h, h / 2, p.glow)).join("");

export const VISORS: readonly Visor[] = [
  {
    id: "band", label: "Band",
    glass: BAND, eyes: [[40.5, 48], [59.5, 48]], mouthY: 55.5, strip: [34, 46.5, 32],
    styled: (p) => pills(p, [40.5, 59.5], 48, 8, 8.5),
  },
  {
    id: "lens", label: "Lenses",
    glass: (p, lod) => {
      const rim = lod === "band" ? "" : ` stroke="${p.shade}" stroke-width="2.6"`;
      return rect(46, 45, 8, 4, 2, p.shade) + circle(39, 47, 9.5, GLASS, rim) + circle(61, 47, 9.5, GLASS, rim)
        + rect(42, 57.5, 16, 7, 3.5, GLASS);
    },
    eyes: [[39, 47], [61, 47]], mouthY: 59.7, grill: true, strip: [33, 45.5, 34],
    styled: (p) => circle(39, 47, 4, p.glow) + circle(61, 47, 4, p.glow)
      + circle(40.6, 45.3, 1.2, "#ffffff", ` opacity="0.9"`) + circle(62.6, 45.3, 1.2, "#ffffff", ` opacity="0.9"`),
  },
  {
    id: "slit", label: "Slit",
    glass: () => rect(26, 40.5, 48, 13.5, 6.75, GLASS) + rect(43, 57.5, 14, 7, 3.5, GLASS),
    eyes: [[50, 47.25]], mouthY: 59.7, grill: true, strip: [33, 45.5, 34],
    styled: (p) => rect(36, 44.5, 28, 5.5, 2.75, p.glow) + rect(48.5, 44.5, 3, 5.5, 1.5, "#ffffff", ` opacity="0.85"`),
  },
  {
    id: "grid", label: "Blueprint",
    glass: (p, lod) => BAND(p) + (lod === "full"
      ? [36, 44, 52, 60, 68].map((x) => rect(x - 0.4, 38, 0.8, 23, 0.4, p.glow, ` opacity="0.13"`)).join("")
        + [44, 52].map((y) => rect(29, y - 0.4, 42, 0.8, 0.4, p.glow, ` opacity="0.13"`)).join("")
      : ""),
    eyes: [[40, 48], [60, 48]], mouthY: 55.5, strip: [34, 46.5, 32],
    styled: (p) => rect(36, 44, 8, 8, 1.6, p.glow) + rect(56, 44, 8, 8, 1.6, p.glow),
  },
  {
    id: "shield", label: "Shield",
    glass: () => `<path d="M28 38 H72 V49 Q72 60 50 64.5 Q28 60 28 49 Z" fill="${GLASS}"/>`,
    eyes: [[40, 48], [60, 48]], mouthY: 55.5, strip: [34, 46.5, 32],
    styled: (p) => `<path d="M34.5 45.2 L46 47 L45.4 51.2 L34.5 50 Z" fill="${p.glow}"/>`
      + `<path d="M65.5 45.2 L54 47 L54.6 51.2 L65.5 50 Z" fill="${p.glow}"/>`,
  },
  {
    id: "scope", label: "Scope",
    glass: (p, lod) => BAND(p) + (lod === "band" ? "" : circle(60, 47.5, 10.5, GLASS, ` stroke="${p.shade}" stroke-width="3"`)),
    eyes: [[40, 48], [60, 47.5]], mouthY: 56, strip: [34, 46.5, 32],
    styled: (p) => rect(35.5, 45.5, 9, 5, 2.5, p.glow) + circle(60, 47.5, 4.6, p.glow)
      + circle(60, 47.5, 7.2, "none", ` stroke="${p.glow}" stroke-width="0.8" opacity="0.5"`),
  },
  {
    id: "monocle", label: "Monocle",
    glass: (p, lod) => BAND(p) + (lod === "full"
      ? `<path d="M33.5 54.5 Q29 60 25 63" fill="none" stroke="#f5b544" stroke-width="1" stroke-dasharray="1.6 1.2"/>` : ""),
    eyes: [[40, 48], [60, 48]], mouthY: 56, strip: [34, 46.5, 32],
    styled: (p) => circle(40, 48, 3.8, p.glow) + circle(40, 48, 8, "none", ` stroke="#f5b544" stroke-width="2"`)
      + rect(55, 46.5, 9.5, 3.6, 1.8, p.glow),
  },
  {
    id: "glasses", label: "Glasses",
    glass: (p, lod) => BAND(p) + (lod === "band" ? "" :
      circle(40, 48, 8.2, "none", ` stroke="${p.glow}" stroke-width="1.6" opacity="0.75"`)
      + circle(60, 48, 8.2, "none", ` stroke="${p.glow}" stroke-width="1.6" opacity="0.75"`)
      + rect(47.8, 46.6, 4.4, 1.6, 0.8, p.glow, ` opacity="0.75"`)),
    eyes: [[40, 48], [60, 48]], mouthY: 56, strip: [34, 46.5, 32],
    styled: (p) => `<path d="M36 49.5 Q40 44.5 44 49.5" fill="none" stroke="${p.glow}" stroke-width="2.6" stroke-linecap="round"/>`
      + `<path d="M56 49.5 Q60 44.5 64 49.5" fill="none" stroke="${p.glow}" stroke-width="2.6" stroke-linecap="round"/>`,
  },
] as const;

export const VISOR_BY_ID: Readonly<Record<string, Visor>> = dict(VISORS.map((v) => [v.id, v]));

/** Plain eye bars for 32px: the same for every visor, so a small face stays calm and legible. */
export function eyeBars(v: Visor, p: Palette): string {
  const w = v.eyes.length === 1 ? 22 : 8;
  return v.eyes.map(([x, y]) => rect(x - w / 2, y - 1.8, w, 3.6, 1.8, p.glow)).join("");
}

/** The three mouth bars. Each is its own rect so speech can move them one at a time. */
export function mouthBars(v: Visor, p: Palette): string {
  return [45.5, 50, 54.5].map((cx, i) =>
    `<rect class="av-bar av-bar-${i + 1}" x="${f(cx - 1.3)}" y="${f(v.mouthY)}" width="2.6" height="2.6" rx="1.1" fill="${p.glow}"/>`).join("");
}
