/**
 * boss-kit-v1 palettes. Each is one colour family taken from the console's brand tokens
 * (design-system.css, components/v2/tokens.css), never a new hue.
 *
 *   body  — the head plate. Must read at 3:1 or better on --surface / --surface-2 (tested).
 *   shade — torso, ear pods, hat shadows: the same hue, one step down.
 *   disc  — the porthole behind the character: the hue, nearly black, so the head pops.
 *   glow  — eyes, mouth and emblem light: the hue, nearly white.
 *
 * Hex values (not var()) because the same SVG is drawn by next/og and the cast export, where
 * no stylesheet exists. Where a value IS a token, the token is named beside it.
 */

export type Palette = { id: string; label: string; body: string; shade: string; disc: string; glow: string };

export const PALETTES: readonly Palette[] = [
  { id: "teal", label: "Teal", body: "#2ec5c5" /* --teal */, shade: "#1c8f95", disc: "#0d2531", glow: "#c4fbf6" },
  { id: "blue", label: "Blue", body: "#2f6fe0" /* --accent */, shade: "#2152ad", disc: "#0f1c3d", glow: "#c9dbff" },
  { id: "mint", label: "Mint", body: "#4fe3c1" /* --v2-mint */, shade: "#2aa88d", disc: "#0c2828", glow: "#d6fff4" },
  { id: "purple", label: "Purple", body: "#a78bfa" /* --v2-purple */, shade: "#7459d4", disc: "#1b1740", glow: "#ebe4ff" },
  { id: "amber", label: "Amber", body: "#f5b544" /* --amber */, shade: "#c2832b", disc: "#2a1f0e", glow: "#fff1cf" },
  { id: "pink", label: "Pink", body: "#ec4899" /* tone-pink */, shade: "#b22c71", disc: "#2b1227", glow: "#ffd6ec" },
  { id: "green", label: "Green", body: "#34d399" /* --green */, shade: "#1f9a6d", disc: "#0c2820", glow: "#d3ffeb" },
  { id: "slate", label: "Slate", body: "#9fb0ce" /* --muted */, shade: "#6b7ea4", disc: "#172036", glow: "#eef3fb" },
  { id: "coral", label: "Coral", body: "#f2647a" /* --red */, shade: "#b7404f", disc: "#2b121a", glow: "#ffd9df" },
] as const;

export const PALETTE_BY_ID: Record<string, Palette> = Object.fromEntries(PALETTES.map((p) => [p.id, p]));

/** The visor glass, the neck and the badge plates: one near-black for every character. */
export const GLASS = "#0a1122";
/** The 1px outline that keeps every avatar at 3:1 on the console surfaces (--strong). */
export const OUTLINE = "#26365c";
/** State colours, the same tokens the rest of the console uses for the same meanings. */
export const STATE_COLOURS = {
  thinking: "#2ec5c5", // --teal
  needs_you: "#f5b544", // --amber
  done: "#34d399", // --green
  stuck: "#f2647a", // --red
  quiet: "#64769a", // --faint
  ink: "#eef3fb", // --ink
} as const;
/** The surfaces an avatar sits on (--surface, --surface-2, --void): the contrast test's backdrop. */
export const SURFACES = ["#101a30", "#0e1730", "#090d17"] as const;

/** WCAG relative luminance and contrast, so the 3:1 rule is computed, not eyeballed. */
export function luminance(hex: string): number {
  const n = hex.replace("#", "");
  const ch = [0, 2, 4].map((i) => parseInt(n.slice(i, i + 2), 16) / 255)
    .map((c) => (c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4));
  return 0.2126 * ch[0] + 0.7152 * ch[1] + 0.0722 * ch[2];
}

export function contrast(a: string, b: string): number {
  const [x, y] = [luminance(a), luminance(b)].sort((p, q) => q - p);
  return (x + 0.05) / (y + 0.05);
}
