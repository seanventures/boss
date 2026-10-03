/**
 * Head gear: the first thing that tells one crew member from another, at any size.
 *
 * Every hat stays inside the porthole (the disc centred at 50,50 with r 44, in the kit's
 * 100×100 box) — a test measures every drawn point — so no face is clipped and none needs a
 * clipPath. Paths use absolute commands only (M L H V Q C Z), which is what keeps them measurable.
 * The head plate's top edge is y 26.
 *
 * `cast` hats belong to one default character each and are not used by the procedural picker,
 * so a custom agent never wears Vera's magnifier. Colours come from the character's palette
 * plus a few fixed accents (navy, glass, the brand amber and coral) so a hat reads as an object,
 * not more robot.
 */
import { GLASS, type Palette } from "./palettes";

export type Hat = {
  id: string;
  label: string;
  /** Owned by one default character; never picked for a custom agent. */
  cast?: boolean;
  /** `detail` is true at 48px and up: highlights, rivets, stitching. */
  draw(p: Palette, detail: boolean): string;
};

const NAVY = "#1a2647";
const STEEL = "#2c3a55";
const AMBER = "#f5b544";
const AMBER_SHADE = "#c2832b";
const CORAL = "#f2647a";
const INK = "#eef3fb";
const SILVER = "#c3d0e8";

const star4 = (cx: number, cy: number, r: number, fill: string) => {
  const k = r * 0.36;
  return `<path d="M${cx} ${cy - r} L${cx + k} ${cy - k} L${cx + r} ${cy} L${cx + k} ${cy + k} L${cx} ${cy + r} `
    + `L${cx - k} ${cy + k} L${cx - r} ${cy} L${cx - k} ${cy - k} Z" fill="${fill}"/>`;
};

export const HATS: readonly Hat[] = [
  // ── Cast hats: one per default character ────────────────────────────────────────────────
  {
    id: "captain", label: "Captain's cap", cast: true,
    draw: (_p, d) => `<path d="M29 31 Q29 15 50 14 Q71 15 71 31 Z" fill="#e6edf5"/>`
      + `<rect x="29" y="26" width="42" height="6" rx="1.5" fill="${NAVY}"/>`
      + `<path d="M25 32 Q50 39 75 32 Q73.5 36 70 36.8 Q50 41 30 36.8 Q26.5 36 25 32 Z" fill="${GLASS}"/>`
      + `<path d="M45 23 L50 19.5 L55 23" fill="none" stroke="${AMBER}" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/>`
      + (d ? `<path d="M46.5 26 L50 23.6 L53.5 26" fill="none" stroke="${AMBER}" stroke-width="1.4" stroke-linecap="round"/>`
        + `<path d="M33 22 Q36 17 44 16" fill="none" stroke="#ffffff" stroke-width="1.4" opacity="0.6" stroke-linecap="round"/>` : ""),
  },
  {
    id: "rack", label: "Data stack", cast: true,
    draw: (p, d) => `<path d="M38 14 V27 Q50 31 62 27 V14 Z" fill="${p.shade}"/>`
      + `<ellipse cx="50" cy="14" rx="12" ry="3.5" fill="${p.body}"/>`
      + `<path d="M38 18.6 Q50 22.6 62 18.6 M38 23 Q50 27 62 23" fill="none" stroke="${GLASS}" stroke-width="1.3"/>`
      + (d ? `<circle cx="57.5" cy="21.6" r="1" fill="#4fe3c1"/><circle cx="57.5" cy="26" r="1" fill="${AMBER}"/>`
        + `<ellipse cx="46" cy="13.2" rx="5" ry="1.2" fill="#ffffff" opacity="0.35"/>` : ""),
  },
  {
    id: "pixel", label: "Pixel crest", cast: true,
    draw: (_p, d) => ([[34, 21, "#2ec5c5"], [40.5, 16, "#2f6fe0"], [47, 12, "#ec4899"], [53.5, 14.5, AMBER], [60, 19.5, "#4fe3c1"]] as const)
      .map(([x, y, c]) => `<rect x="${x}" y="${y}" width="6.2" height="6.2" rx="1.1" fill="${c}"/>`
        + (d ? `<rect x="${x + 1}" y="${y + 1}" width="2" height="2" rx="0.6" fill="#ffffff" opacity="0.55"/>` : "")).join("")
      + `<rect x="36" y="25" width="28" height="3.5" rx="1.75" fill="${STEEL}"/>`,
  },
  {
    id: "headlamp", label: "Headlamp", cast: true,
    draw: (_p, d) => `<path d="M23 33 Q50 24 77 33 L77 37.5 Q50 28.5 23 37.5 Z" fill="${NAVY}"/>`
      + `<rect x="43" y="22" width="14" height="10.5" rx="3" fill="${STEEL}"/>`
      + `<circle cx="50" cy="27.2" r="3.9" fill="${AMBER}"/>`
      + (d ? `<circle cx="48.8" cy="26" r="1.2" fill="#ffffff" opacity="0.8"/>`
        + `<path d="M44 18.5 L41 13 M50 17.5 V11.5 M56 18.5 L59 13" stroke="${AMBER}" stroke-width="1.5" stroke-linecap="round" opacity="0.6"/>` : ""),
  },
  {
    id: "siren", label: "Beacon", cast: true,
    draw: (_p, d) => `<rect x="40.5" y="21" width="19" height="7.5" rx="2" fill="${STEEL}"/>`
      + `<path d="M43 21.5 Q43 10.5 50 10.5 Q57 10.5 57 21.5 Z" fill="${CORAL}"/>`
      + `<rect x="46.5" y="13" width="2.4" height="7" rx="1.2" fill="#ffffff" opacity="0.55"/>`
      + (d ? `<path d="M39.5 15.5 L36 12.5 M60.5 15.5 L64 12.5 M38.5 21 H34.5 M61.5 21 H65.5" stroke="${CORAL}" stroke-width="1.6" stroke-linecap="round" opacity="0.75"/>` : ""),
  },
  {
    id: "northstar", label: "North star", cast: true,
    draw: (p, d) => `<rect x="48.8" y="17" width="2.4" height="10" rx="1.2" fill="${p.shade}"/>`
      + star4(50, 13, 6, INK)
      + (d ? `<circle cx="50" cy="13" r="1.4" fill="${AMBER}"/>` : ""),
  },
  {
    id: "hardhat", label: "Hard hat", cast: true,
    draw: (_p, d) => `<path d="M26 31 Q26 13 50 12 Q74 13 74 31 Z" fill="${AMBER}"/>`
      + `<rect x="47" y="12.6" width="6" height="18" rx="2.2" fill="${AMBER_SHADE}"/>`
      + `<rect x="21" y="29.5" width="58" height="5.5" rx="2.75" fill="${AMBER_SHADE}"/>`
      + (d ? `<path d="M31 25 Q32 17 42 15" fill="none" stroke="#ffffff" stroke-width="1.6" opacity="0.5" stroke-linecap="round"/>` : ""),
  },
  {
    id: "headphones", label: "Headphones", cast: true,
    draw: (p, d) => `<path d="M20.5 44 Q20.5 14 50 14 Q79.5 14 79.5 44" fill="none" stroke="${SILVER}" stroke-width="4.5" stroke-linecap="round"/>`
      + `<rect x="13" y="38" width="11" height="20" rx="4.5" fill="${SILVER}"/><rect x="76" y="38" width="11" height="20" rx="4.5" fill="${SILVER}"/>`
      + `<rect x="19.5" y="40.5" width="4.5" height="15" rx="2" fill="${NAVY}"/><rect x="76" y="40.5" width="4.5" height="15" rx="2" fill="${NAVY}"/>`
      + (d ? `<circle cx="16.8" cy="48" r="1.3" fill="${p.body}"/><circle cx="83.2" cy="48" r="1.3" fill="${p.body}"/>` : ""),
  },
  {
    id: "rook", label: "Tower crown", cast: true,
    draw: (p, d) => `<path d="M30 30 V14 H38 V19 H46 V14 H54 V19 H62 V14 H70 V30 Z" fill="${p.shade}"/>`
      + `<rect x="48.5" y="21.5" width="3" height="5" rx="1" fill="${GLASS}"/>`
      + (d ? `<path d="M66.5 12 L72.5 25.5" stroke="${CORAL}" stroke-width="2.6" stroke-linecap="round"/>`
        + `<path d="M66.5 12 L67.6 14.5" stroke="#ffffff" stroke-width="2.6" stroke-linecap="round"/>`
        + `<path d="M32 16.5 H36" stroke="#ffffff" stroke-width="1" opacity="0.45"/>` : ""),
  },
  {
    id: "crest", label: "Guard helm", cast: true,
    draw: (p, d) => `<path d="M45 16.5 Q44 8 52 7.5 Q62 8 66 16.5 Q58 13 54 15 Z" fill="${CORAL}"/>`
      + `<path d="M25 34 Q25 14 50 13 Q75 14 75 34 L71 34 Q70 19 50 18.5 Q30 19 29 34 Z" fill="${p.shade}"/>`
      + `<rect x="47.5" y="13" width="5" height="14" rx="2" fill="${p.shade}"/>`
      + (d ? `<circle cx="29.5" cy="29" r="1.2" fill="${INK}" opacity="0.7"/><circle cx="70.5" cy="29" r="1.2" fill="${INK}" opacity="0.7"/>` : ""),
  },
  {
    id: "magnifier", label: "Magnifier", cast: true,
    draw: (p, d) => `<path d="M53.5 26.5 L56.6 20.6" stroke="${p.shade}" stroke-width="3.2" stroke-linecap="round"/>`
      + `<circle cx="60" cy="15" r="5.6" fill="${p.glow}" fill-opacity="0.22" stroke="${INK}" stroke-width="2.4"/>`
      + (d ? `<path d="M57 13.4 Q58 11.6 60 11.4" fill="none" stroke="#ffffff" stroke-width="1.2" stroke-linecap="round"/>` : ""),
  },
  {
    id: "flag", label: "Flag mast", cast: true,
    draw: (p, d) => `<rect x="45" y="9" width="2" height="18" rx="1" fill="${STEEL}"/>`
      + `<circle cx="46" cy="8.4" r="1.6" fill="${AMBER}"/>`
      + `<path d="M47 9.5 L60 13 L47 16.5 Z" fill="${INK}"/>`
      + (d ? `<path d="M47 11.5 L54 13 L47 14.5 Z" fill="${p.body}"/>` : ""),
  },
  {
    id: "beret", label: "Beret", cast: true,
    draw: (p, d) => `<path d="M27 30 Q24 18.5 44 15.3 Q66 13 72 24 Q74 29.5 66 30.5 Z" fill="${p.shade}"/>`
      + `<circle cx="47.5" cy="13.6" r="2.2" fill="${p.shade}"/>`
      + (d ? `<path d="M31 27 Q48 22.5 68 26" fill="none" stroke="#ffffff" stroke-width="1" stroke-dasharray="1.6 1.6" opacity="0.4"/>` : ""),
  },
  {
    id: "quill", label: "Quill", cast: true,
    draw: (p, d) => `<path d="M24 33.5 Q50 26 76 33.5 L76 36.5 Q50 29 24 36.5 Z" fill="${NAVY}"/>`
      + `<path d="M57.5 30 Q56 17 66 9.5 Q65.5 21 60.5 30 Z" fill="${INK}"/>`
      + `<path d="M59 30 Q60.5 19 65 11.5" fill="none" stroke="${p.shade}" stroke-width="1.2"/>`
      + (d ? `<path d="M60.6 21 L58.2 19.4 M61.8 17.4 L59.8 15.6 M62 22.6 L64.4 20.8" stroke="${p.shade}" stroke-width="0.8" opacity="0.8"/>` : ""),
  },
  {
    id: "coins", label: "Coin stack", cast: true,
    draw: (_p, d) => ([[48, 23.5], [51.5, 19], [49, 14.5]] as const).map(([cx, cy]) =>
      `<ellipse cx="${cx}" cy="${cy + 3.4}" rx="9" ry="3" fill="${AMBER_SHADE}"/>`
      + `<rect x="${cx - 9}" y="${cy}" width="18" height="3.4" fill="${AMBER_SHADE}"/>`
      + `<ellipse cx="${cx}" cy="${cy}" rx="9" ry="3" fill="${AMBER}"/>`
      + (d ? `<ellipse cx="${cx}" cy="${cy}" rx="5" ry="1.4" fill="none" stroke="${AMBER_SHADE}" stroke-width="0.8"/>` : "")).join(""),
  },
  {
    id: "goggles", label: "Goggles", cast: true,
    draw: (_p, d) => `<path d="M23 31 Q50 24 77 31 L77 35 Q50 28 23 35 Z" fill="${STEEL}"/>`
      + `<circle cx="40" cy="25" r="6.8" fill="${GLASS}" stroke="${AMBER}" stroke-width="2.4"/>`
      + `<circle cx="60" cy="25" r="6.8" fill="${GLASS}" stroke="${AMBER}" stroke-width="2.4"/>`
      + (d ? `<circle cx="38" cy="23" r="1.6" fill="#ffffff" opacity="0.7"/><circle cx="58" cy="23" r="1.6" fill="#ffffff" opacity="0.7"/>` : ""),
  },
  {
    id: "comet", label: "Comet", cast: true,
    draw: (p, d) => `<path d="M51.5 26.5 L55.6 16" stroke="${p.shade}" stroke-width="2.4" stroke-linecap="round"/>`
      + `<ellipse cx="57" cy="12" rx="8" ry="2.6" fill="none" stroke="${p.glow}" stroke-width="1.2" opacity="0.8" transform="rotate(-20 57 12)"/>`
      + star4(57, 12, 5, INK)
      + (d ? `<circle cx="64" cy="9.6" r="1" fill="${AMBER}"/>` : ""),
  },
  {
    id: "headset", label: "Headset", cast: true,
    draw: (_p, d) => `<path d="M24 40 Q24 15 50 15 Q76 15 76 40" fill="none" stroke="${SILVER}" stroke-width="3" stroke-linecap="round"/>`
      + `<rect x="74" y="36" width="8" height="16" rx="3.5" fill="${SILVER}"/>`
      + `<path d="M76 50 Q75 62 61 62.5" fill="none" stroke="${SILVER}" stroke-width="2.2" stroke-linecap="round"/>`
      + `<circle cx="59.8" cy="62.5" r="2.3" fill="${AMBER}"/>`
      + (d ? `<circle cx="78" cy="44" r="1.2" fill="${CORAL}"/>` : ""),
  },
  {
    id: "mortarboard", label: "Mortarboard", cast: true,
    draw: (_p, d) => `<path d="M34 24 V31 Q50 36 66 31 V24 Z" fill="#141d33"/>`
      + `<path d="M24 21 L50 12 L76 21 L50 30 Z" fill="${NAVY}"/>`
      + `<path d="M50 21 L69.5 23.5 L69.8 32" fill="none" stroke="${AMBER}" stroke-width="1.4"/>`
      + `<rect x="68.4" y="31.5" width="2.8" height="6" rx="1" fill="${AMBER}"/>`
      + `<circle cx="50" cy="21" r="1.7" fill="${AMBER}"/>`
      + (d ? `<path d="M30 20.6 L50 13.8" stroke="#ffffff" stroke-width="0.9" opacity="0.3"/>` : ""),
  },
  {
    id: "clapper", label: "Clapper", cast: true,
    draw: (_p, d) => `<rect x="38" y="18.5" width="24" height="10.5" rx="1.5" fill="${GLASS}"/>`
      + `<path d="M37 17 L61 11 L62.4 15.4 L38.4 21.4 Z" fill="${INK}"/>`
      + `<path d="M42 15.8 L44.6 19.9 M48 14.3 L50.6 18.4 M54 12.8 L56.6 16.9" stroke="${GLASS}" stroke-width="2.2"/>`
      + (d ? `<rect x="41" y="22.5" width="12" height="1.4" rx="0.7" fill="${INK}" opacity="0.5"/>` : ""),
  },

  // ── Generic hats: the procedural picker draws from these ───────────────────────────────
  {
    id: "none", label: "Bare",
    draw: (p, d) => (d ? `<rect x="44" y="23.5" width="12" height="3" rx="1.5" fill="${p.shade}"/>` : ""),
  },
  {
    id: "antenna", label: "Antenna",
    draw: (p, d) => `<rect x="48.8" y="13" width="2.4" height="14" rx="1.2" fill="${p.shade}"/>`
      + `<circle cx="50" cy="12" r="3.6" fill="${p.glow}"/>`
      + (d ? `<circle cx="48.9" cy="10.9" r="1" fill="#ffffff"/>` : ""),
  },
  {
    id: "twin", label: "Twin antennas",
    draw: (p) => `<path d="M40 27 L34.5 15.5 M60 27 L65.5 15.5" stroke="${p.shade}" stroke-width="2.4" stroke-linecap="round"/>`
      + `<circle cx="34" cy="15" r="3" fill="${p.glow}"/><circle cx="66" cy="15" r="3" fill="${p.glow}"/>`,
  },
  {
    id: "dome", label: "Glass dome",
    draw: (p, d) => `<circle cx="50" cy="21" r="3.4" fill="${p.body}"/>`
      + `<path d="M36 27 Q36 11 50 11 Q64 11 64 27 Z" fill="${p.glow}" fill-opacity="0.2" stroke="${p.glow}" stroke-width="1.4" stroke-opacity="0.7"/>`
      + (d ? `<path d="M40.5 22 Q41 15.5 46 14" fill="none" stroke="#ffffff" stroke-width="1.2" opacity="0.6" stroke-linecap="round"/>` : ""),
  },
  {
    id: "halo", label: "Halo",
    draw: () => `<ellipse cx="50" cy="14" rx="15.5" ry="4.2" fill="none" stroke="#fff1cf" stroke-width="2.6" opacity="0.9"/>`,
  },
  {
    id: "crown", label: "Crown",
    draw: (_p, d) => `<path d="M33 28 V15 L41.5 21.5 L50 11 L58.5 21.5 L67 15 V28 Z" fill="${AMBER}"/>`
      + (d ? `<circle cx="50" cy="23" r="1.8" fill="${CORAL}"/><circle cx="40" cy="24.5" r="1.3" fill="#2ec5c5"/><circle cx="60" cy="24.5" r="1.3" fill="#2ec5c5"/>` : ""),
  },
  {
    id: "propeller", label: "Propeller",
    draw: (p, d) => `<path d="M32 29 Q32 17 50 17 Q68 17 68 29 Z" fill="${p.shade}"/>`
      + `<rect x="49" y="11.5" width="2" height="6" fill="${STEEL}"/>`
      + `<ellipse cx="43" cy="11" rx="7.5" ry="2" fill="${CORAL}"/><ellipse cx="57" cy="11" rx="7.5" ry="2" fill="#2ec5c5"/>`
      + (d ? `<path d="M41 19.5 V28.5 M50 18 V28.8 M59 19.5 V28.5" stroke="${p.body}" stroke-width="1.6" opacity="0.7"/>` : ""),
  },
  {
    id: "fin", label: "Fin",
    draw: (p, d) => `<path d="M38 28 Q40 13 54 9 Q50 18 62 28 Z" fill="${p.shade}"/>`
      + (d ? `<path d="M43 25 Q45 16 52 12.5" fill="none" stroke="#ffffff" stroke-width="1" opacity="0.4"/>` : ""),
  },
  {
    id: "cap", label: "Cap",
    draw: (p, d) => `<path d="M30 30 Q30 15 50 15 Q70 15 70 30 Z" fill="${p.shade}"/>`
      + `<path d="M50 28 Q70 25 80 31 Q70 34.5 50 32 Z" fill="${NAVY}"/>`
      + (d ? `<circle cx="50" cy="15.6" r="1.4" fill="${p.body}"/>` : ""),
  },
] as const;

export const HAT_BY_ID: Record<string, Hat> = Object.fromEntries(HATS.map((h) => [h.id, h]));
export const GENERIC_HATS = HATS.filter((h) => !h.cast);
