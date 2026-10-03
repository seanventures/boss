/**
 * Chest emblems: what the crew member is FOR, drawn as one glyph on a glass badge.
 *
 * Each glyph is drawn in a box from -8 to 8 around the origin and placed on the chest plate by
 * the renderer. Lines are strokes (`s`), solid shapes are fills (`f`). Original drawings, no icon
 * set copied, so the kit stays Apache-2.0 with no attribution chain.
 */

export type Emblem = { id: string; label: string; s?: string; f?: string };

export const EMBLEMS: readonly Emblem[] = [
  { id: "chevrons", label: "Chevrons", s: "M-6 -1 L0 -5.5 L6 -1 M-6 5 L0 0.5 L6 5" },
  { id: "layers", label: "Layers", s: "M-7 -3 L0 -6.5 L7 -3 L0 0.5 Z M-7 1 L0 4.5 L7 1 M-7 5 L0 8 L7 5" },
  { id: "brackets", label: "Brackets", s: "M-3.5 -6 L-8 0 L-3.5 6 M3.5 -6 L8 0 L3.5 6 M1.6 -7 L-1.6 7" },
  { id: "wrench", label: "Wrench", s: "M-6 6 L1 -1 M1 -1 A4.2 4.2 0 1 0 5.6 -6.6 L3.4 -4.4 L4.4 -2.4 L6.4 -3.4 L8 -5" },
  { id: "pulse", label: "Pulse", s: "M-8 0.5 H-4 L-2 -5.5 L1.4 6 L3.6 0.5 H8" },
  { id: "compass", label: "Compass", s: "M0 -7 A7 7 0 1 1 0 7 A7 7 0 1 1 0 -7 Z", f: "M0 -5.6 L2 0 L0 5.6 L-2 0 Z" },
  { id: "blueprint", label: "Set square", s: "M-6.5 6.5 V-6.5 L6.5 6.5 Z M-3.5 3.5 V0 L0 3.5 Z" },
  { id: "braces", label: "Braces", s: "M-3 -7 Q-6 -7 -6 -4 V-2 Q-6 0 -8 0 Q-6 0 -6 2 V4 Q-6 7 -3 7 M3 -7 Q6 -7 6 -4 V-2 Q6 0 8 0 Q6 0 6 2 V4 Q6 7 3 7" },
  { id: "target", label: "Target", s: "M0 -7 A7 7 0 1 1 0 7 A7 7 0 1 1 0 -7 Z M0 -3.4 A3.4 3.4 0 1 1 0 3.4 A3.4 3.4 0 1 1 0 -3.4 Z", f: "M0 -1.2 A1.2 1.2 0 1 1 0 1.2 A1.2 1.2 0 1 1 0 -1.2 Z" },
  { id: "lock", label: "Lock", s: "M-3.6 -1 V-3.8 A3.6 3.6 0 0 1 3.6 -3.8 V-1", f: "M-6 -1 H6 V7 H-6 Z" },
  { id: "check", label: "Check", s: "M-6.5 0 L-2 4.8 L7 -5.5" },
  { id: "rocket", label: "Rocket", s: "M0 -8 Q5.5 -3 3 4 H-3 Q-5.5 -3 0 -8 Z M-3 4 L-5.5 7.5 M3 4 L5.5 7.5 M0 4 V7.5", f: "M0 -3.6 A1.6 1.6 0 1 1 0 -0.4 A1.6 1.6 0 1 1 0 -3.6 Z" },
  { id: "nib", label: "Pen nib", s: "M0 -8 L5.5 1 L0 8 L-5.5 1 Z M0 1 V8", f: "M0 -0.4 A1.3 1.3 0 1 1 0 2.2 A1.3 1.3 0 1 1 0 -0.4 Z" },
  { id: "page", label: "Page", s: "M-5.5 -7.5 H2.5 L6 -4 V7.5 H-5.5 Z M-2.5 -2 H3 M-2.5 1.5 H3 M-2.5 5 H1" },
  { id: "dollar", label: "Dollar", s: "M4.6 -4.4 Q3 -6.6 0 -6.6 Q-4.6 -6.6 -4.6 -3.2 Q-4.6 0 0 0 Q4.6 0 4.6 3.2 Q4.6 6.6 0 6.6 Q-3.2 6.6 -4.8 4.4 M0 -8.6 V8.6" },
  { id: "atom", label: "Atom", s: "M-8 0 A8 3 0 1 0 8 0 A8 3 0 1 0 -8 0 Z M-4 -6.9 A8 3 60 1 0 4 6.9 A8 3 60 1 0 -4 -6.9 Z M4 -6.9 A8 3 -60 1 0 -4 6.9 A8 3 -60 1 0 4 -6.9 Z", f: "M0 -1.4 A1.4 1.4 0 1 1 0 1.4 A1.4 1.4 0 1 1 0 -1.4 Z" },
  { id: "spark", label: "Spark", f: "M0 -8 L2.2 -2.2 L8 0 L2.2 2.2 L0 8 L-2.2 2.2 L-8 0 L-2.2 -2.2 Z" },
  { id: "stopwatch", label: "Stopwatch", s: "M0 -5 A6 6 0 1 1 0 7 A6 6 0 1 1 0 -5 Z M0 1 V-2.4 M-2.4 -7.8 H2.4 M0 -7.8 V-5 M5 -4.6 L6.4 -6" },
  { id: "bulb", label: "Bulb", s: "M-4.2 1.2 A5.4 5.4 0 1 1 4.2 1.2 Q2.6 3 2.6 5 H-2.6 Q-2.6 3 -4.2 1.2 Z M-2.2 7.4 H2.2" },
  { id: "gear", label: "Gear", s: "M0 -4.2 A4.2 4.2 0 1 1 0 4.2 A4.2 4.2 0 1 1 0 -4.2 Z M0 -8 V-5.2 M0 5.2 V8 M-8 0 H-5.2 M5.2 0 H8 M-5.7 -5.7 L-3.7 -3.7 M5.7 5.7 L3.7 3.7 M-5.7 5.7 L-3.7 3.7 M5.7 -5.7 L3.7 -3.7" },
  { id: "shield", label: "Shield", s: "M0 -7.6 L6.6 -5 V0 Q6.6 5.2 0 8 Q-6.6 5.2 -6.6 0 V-5 Z M-3 0.2 L-0.6 2.6 L3.4 -2" },
  { id: "eye", label: "Eye", s: "M-8 0 Q0 -7 8 0 Q0 7 -8 0 Z", f: "M0 -2.6 A2.6 2.6 0 1 1 0 2.6 A2.6 2.6 0 1 1 0 -2.6 Z" },
  { id: "play", label: "Play", f: "M-4 -6.5 L6.5 0 L-4 6.5 Z" },
  { id: "cloud", label: "Cloud", s: "M-5.5 5 A3.6 3.6 0 0 1 -5.2 -2.2 A4.6 4.6 0 0 1 3.6 -3.4 A4.2 4.2 0 0 1 5.6 5 Z" },
  { id: "key", label: "Key", s: "M-4 -3.4 A3.6 3.6 0 1 1 -4 3.8 A3.6 3.6 0 1 1 -4 -3.4 Z M-0.4 0.2 H8 M5 0.2 V3.6 M7.6 0.2 V3" },
  { id: "cycle", label: "Cycle", s: "M6 -2 A6.2 6.2 0 0 0 -5 -3.2 M-6 2 A6.2 6.2 0 0 0 5 3.2 M-5 -3.2 L-5.6 -7 M-5 -3.2 L-1.4 -4 M5 3.2 L5.6 7 M5 3.2 L1.4 4" },
  { id: "chart", label: "Chart", s: "M-7.5 7 H7.5 M-5 7 V1.5 M-1.6 7 V-3.6 M1.8 7 V-0.6 M5.2 7 V-6.4" },
  { id: "people", label: "People", s: "M-3.2 -5.6 A2.6 2.6 0 1 1 -3.2 -0.4 A2.6 2.6 0 1 1 -3.2 -5.6 Z M4 -4.4 A2.2 2.2 0 1 1 4 0 A2.2 2.2 0 1 1 4 -4.4 Z M-8 7 Q-8 1.6 -3.2 1.6 Q1.6 1.6 1.6 7 M1.6 3 Q3 1.8 4.4 1.8 Q8 1.8 8 6" },
  { id: "bolt", label: "Bolt", f: "M2 -8.2 L-5.4 1.2 H-0.6 L-2.2 8.2 L5.4 -1.2 H0.6 Z" },
  { id: "chip", label: "Chip", s: "M-4.6 -4.6 H4.6 V4.6 H-4.6 Z M-2.2 -7.6 V-4.6 M2.2 -7.6 V-4.6 M-2.2 4.6 V7.6 M2.2 4.6 V7.6 M-7.6 -2.2 H-4.6 M-7.6 2.2 H-4.6 M4.6 -2.2 H7.6 M4.6 2.2 H7.6" },
  { id: "star", label: "Star", f: "M0 -8 L2.3 -2.6 L8 -2.4 L3.6 1.2 L5 7 L0 3.8 L-5 7 L-3.6 1.2 L-8 -2.4 L-2.3 -2.6 Z" },
  { id: "chalk", label: "Chalk", s: "M-7 5 L3 -5 L6 -2 L-4 8 Z M-8 -6 H-1 M-8 -2.5 H-4" },
] as const;

export const EMBLEM_BY_ID: Record<string, Emblem> = Object.fromEntries(EMBLEMS.map((e) => [e.id, e]));
