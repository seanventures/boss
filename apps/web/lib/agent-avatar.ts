/**
 * boss-kit-v1: one pure function draws every agent face in BOSS (spec §6).
 *
 * `avatarSvg(spec, {size, state, variant})` returns an SVG string. The same string feeds React
 * (AgentAvatar), next/og (the share card) and the README cast export, so an agent is the same
 * picture on every surface — the property that makes a character feel like one character.
 *
 * Design rules the code holds:
 *   - No ids, no gradients, no clipPaths, no filters: many faces share one page, and a
 *     duplicate id in one inline SVG silently repaints another. Everything is flat shapes.
 *   - Nothing user-typed is ever written into the markup. Part ids are looked up in the kit
 *     manifests and an unknown id falls back to a seeded part, so the string is safe to inline.
 *   - Level of detail by size: 16 silhouette · 24 + ring and badge · 32 + eye and mouth bars ·
 *     48 + styled eyes and hat detail · 96 everything (idle motion is CSS) · 320 + soft shadow.
 *   - Motion is CSS (agent-world.css, `agav-*`) keyed off `data-state`; this file only names
 *     the parts (`av-*` classes) the keyframes move.
 */
import type { AvatarSpec, AvatarState, AvatarVariant } from "./agent-world-types";
import { CAST, CAST_BY_KEY } from "./avatar-kit/cast";
import { EMBLEMS, EMBLEM_BY_ID, type Emblem } from "./avatar-kit/emblems";
import { GENERIC_HATS, HAT_BY_ID, type Hat } from "./avatar-kit/hats";
import { GLASS, OUTLINE, PALETTES, PALETTE_BY_ID, STATE_COLOURS, type Palette } from "./avatar-kit/palettes";
import { own } from "./avatar-kit/dict";
import { svgDataUri } from "./svg-data-uri";
import { STATE_WORDS } from "./avatar-kit/states";
import { VISORS, VISOR_BY_ID, eyeBars, mouthBars, type Visor } from "./avatar-kit/visors";

export { STATE_WORDS } from "./avatar-kit/states";

export type Lod = 16 | 24 | 32 | 48 | 96 | 320;

/** The drawing tier for a pixel size. */
export function lodFor(size: number): Lod {
  if (size < 20) return 16;
  if (size < 32) return 24;
  if (size < 48) return 32;
  if (size < 96) return 48;
  if (size < 320) return 96;
  return 320;
}

/**
 * cyrb53: a fast 53-bit string hash. Replaces the old char-sum-mod-997, under which anagrams
 * ("qa_lead" / "lead_qa") always collided because a sum ignores order.
 */
export function cyrb53(str: string, seed = 0): number {
  let h1 = 0xdeadbeef ^ seed;
  let h2 = 0x41c6ce57 ^ seed;
  for (let i = 0; i < str.length; i++) {
    const ch = str.charCodeAt(i);
    h1 = Math.imul(h1 ^ ch, 2654435761);
    h2 = Math.imul(h2 ^ ch, 1597334677);
  }
  h1 = Math.imul(h1 ^ (h1 >>> 16), 2246822507) ^ Math.imul(h2 ^ (h2 >>> 13), 3266489909);
  h2 = Math.imul(h2 ^ (h2 >>> 16), 2246822507) ^ Math.imul(h1 ^ (h1 >>> 13), 3266489909);
  return 4294967296 * (2097151 & h2) + (h1 >>> 0);
}

export type Parts = { hat: Hat; emblem: Emblem; palette: Palette; visor: Visor };

/** The parts a spec draws: each named part if the kit has it, else the seed's pick. */
export function resolveParts(spec: AvatarSpec): Parts {
  const seed = spec.seed || "boss";
  const pick = <T,>(list: readonly T[], salt: number) => list[cyrb53(seed, salt) % list.length];
  const named = own(HAT_BY_ID, spec.hat);
  const parts = {
    hat: named || pick(GENERIC_HATS, 1),
    emblem: own(EMBLEM_BY_ID, spec.emblem) || pick(EMBLEMS, 2),
    palette: own(PALETTE_BY_ID, spec.palette) || pick(PALETTES, 3),
    visor: own(VISOR_BY_ID, spec.visor) || pick(VISORS, 4),
  };
  // A seeded face must never land exactly on a shipped role's face: a custom agent that looks
  // like the CTO is one agent wearing another's identity. Re-pick the hat until it doesn't.
  if (!named) {
    for (let salt = 11; CAST_FACES.has(faceKey(parts)) && salt < 40; salt++) parts.hat = pick(GENERIC_HATS, salt);
  }
  return parts;
}

const faceKey = (p: Parts) => `${p.hat.id}|${p.emblem.id}|${p.palette.id}|${p.visor.id}`;
const CAST_FACES = new Set(CAST.map((c) => `${c.avatar.hat}|${c.avatar.emblem}|${c.avatar.palette}|${c.avatar.visor}`));

/** A custom agent's face: everything from its seed (its agent_id). */
export function seededSpec(seed: string): AvatarSpec {
  return { kit: "boss-v1", seed };
}

/** Reroll changes only the seed; parts the operator picked by hand stay picked. */
export function rerollSpec(spec: AvatarSpec, nonce: string = Math.random().toString(36).slice(2, 8)): AvatarSpec {
  const base = (spec.seed || "boss").split("~")[0];
  return { ...spec, seed: `${base}~${nonce}` };
}

/** The default face for a shipped role, or the seeded face for anything else. */
export function specForRole(roleKey: string): AvatarSpec {
  return CAST_BY_KEY[roleKey]?.avatar ?? seededSpec(roleKey);
}

// ── Drawing ────────────────────────────────────────────────────────────────────────────────

const HEAD = "M34 26 H66 Q70 26 72.5 29 L76.5 34 Q78 36 78 39 V58 Q78 70 66 70 H34 Q22 70 22 58 V39 Q22 36 23.5 34 L27.5 29 Q30 26 34 26 Z";
const TORSO = "M22 84 C22 79.5 30 75.5 42 74.8 H58 C70 75.5 78 79.5 78 84 A44 44 0 0 1 22 84 Z";
const DISC = `<circle class="av-disc" cx="50" cy="50" r="44" fill="FILL" stroke="${OUTLINE}" stroke-width="1" vector-effect="non-scaling-stroke"/>`;

function disc(fill: string) {
  return DISC.replace("FILL", fill);
}

function emblemGlyph(e: Emblem, colour: string, scale: number, at: [number, number], width = 2.2): string {
  const s = e.s ? `<path d="${e.s}" fill="none" stroke="${colour}" stroke-width="${width}" stroke-linecap="round" stroke-linejoin="round"/>` : "";
  const f = e.f ? `<path d="${e.f}" fill="${colour}"/>` : "";
  return `<g transform="translate(${at[0]} ${at[1]}) scale(${scale})">${s}${f}</g>`;
}

function character(parts: Parts, lod: Lod, state: AvatarState, seed: string): string {
  const { hat, emblem, palette: p, visor } = parts;
  const detail = lod >= 48;
  const vlod = lod <= 24 ? "band" : lod === 32 ? "bars" : "full";
  const h = cyrb53(seed, 9);
  // Blink every 5-8s, offset per agent so a room of faces never blinks in unison.
  const blink = `--agav-blink:${(5 + (h % 300) / 100).toFixed(2)}s;--agav-blink-delay:-${((h >>> 9) % 500 / 100).toFixed(2)}s`;

  const ears = `<rect x="16.5" y="40" width="7" height="16" rx="3" fill="${p.shade}"/><rect x="76.5" y="40" width="7" height="16" rx="3" fill="${p.shade}"/>`
    + (detail ? `<circle cx="20" cy="48" r="1.3" fill="${p.glow}"/><circle cx="80" cy="48" r="1.3" fill="${p.glow}"/>` : "");
  const sheen = lod >= 32 ? `<path d="M29.5 34 Q32 29.6 38.5 28.8" fill="none" stroke="#ffffff" stroke-width="2" stroke-linecap="round" opacity="0.32"/>` : "";
  const glass = visor.glass(p, vlod);
  const eyes = vlod === "band"
    ? `<rect x="${visor.strip[0]}" y="${visor.strip[1]}" width="${visor.strip[2]}" height="3.4" rx="1.7" fill="${p.glow}" opacity="0.9"/>`
    : `<g class="av-eyes" style="${blink}">${vlod === "bars" ? eyeBars(visor, p) : visor.styled(p)}</g>`;
  const mouth = vlod === "band" ? "" : `<g class="av-mouth">${mouthBars(visor, p)}</g>`;
  const plate = lod <= 24
    ? `<circle cx="50" cy="84.5" r="4.2" fill="${p.glow}" opacity="0.9"/>`
    : `<g class="av-emblem"><circle cx="50" cy="84.5" r="8" fill="${GLASS}"${detail ? ` stroke="${p.body}" stroke-width="1" stroke-opacity="0.6"` : ""}/>`
      + emblemGlyph(emblem, p.glow, 0.6, [50, 84.5]) + `</g>`;
  const tilt = state === "stuck" ? ` transform="rotate(4 50 50)"` : "";
  return `<g class="av-body">`
    + `<path class="av-torso" d="${TORSO}" fill="${p.shade}"/>`
    + `<rect x="43" y="67" width="14" height="9" rx="2" fill="${GLASS}"/>`
    + plate
    + `<g class="av-head"${tilt}>${ears}<path d="${HEAD}" fill="${p.body}"/>${sheen}`
    + `<g class="av-visor">${glass}</g>${eyes}${mouth}`
    + `<g class="av-hat">${hat.draw(p, detail)}</g></g>`
    + `</g>`;
}

const star4 = (cx: number, cy: number, r: number, fill: string) => {
  const k = r * 0.32;
  return `<path d="M${cx} ${cy - r} L${cx + k} ${cy - k} L${cx + r} ${cy} L${cx + k} ${cy + k} L${cx} ${cy + r} `
    + `L${cx - k} ${cy + k} L${cx - r} ${cy} L${cx - k} ${cy - k} Z" fill="${fill}"/>`;
};

/** The four faces that are not agents: Auto, You, A person, BOSS. */
function systemGlyph(variant: Exclude<AvatarVariant, "agent">, lod: Lod): string {
  const detail = lod >= 32;
  switch (variant) {
    case "auto":
      return disc("#0f1c3d") + `<g class="av-body av-core">${star4(50, 50, 23, "#2ec5c5")}`
        + (detail ? star4(69, 30, 6.5, "#c9dbff") + `<circle cx="32" cy="69" r="3.2" fill="#2f6fe0"/>` : "")
        + `<circle cx="50" cy="50" r="4.5" fill="#eef3fb"/></g>`;
    case "you":
      return disc("#172036") + `<g class="av-body"><circle cx="50" cy="39" r="12" fill="none" stroke="#eef3fb" stroke-width="4"/>`
        + `<path d="M26.5 80 Q27 59.5 50 59.5 Q73 59.5 73.5 80" fill="none" stroke="#eef3fb" stroke-width="4" stroke-linecap="round"/></g>`;
    case "person":
      return disc("#2a1f0e") + `<g class="av-body"><circle cx="50" cy="39" r="12" fill="#f5b544" fill-opacity="0.25" stroke="#f5b544" stroke-width="3.5"/>`
        + `<path d="M26.5 80 Q27 59.5 50 59.5 Q73 59.5 73.5 80" fill="#f5b544" fill-opacity="0.25" stroke="#f5b544" stroke-width="3.5" stroke-linecap="round"/></g>`;
    case "boss":
      return disc("#0b101e") + `<g class="av-body">`
        + `<path d="M50 23 L73.4 36.5 V63.5 L50 77 L26.6 63.5 V36.5 Z" fill="none" stroke="#2ec5c5" stroke-width="5" stroke-linejoin="round"/>`
        + `<path d="M50 40 L58.7 45 V55 L50 60 L41.3 55 V45 Z" fill="#2f6fe0"/></g>`;
  }
}

function ring(state: AvatarState, p: Palette | null): string {
  const body = p?.body ?? STATE_COLOURS.thinking;
  const c = (stroke: string, extra = "", cls = "av-ring") =>
    `<circle class="${cls}" cx="50" cy="50" r="47.5" fill="none" stroke="${stroke}" stroke-width="3"${extra}/>`;
  switch (state) {
    case "thinking": return c(STATE_COLOURS.thinking, ` stroke-dasharray="1.5 5.5" stroke-linecap="round"`);
    case "speaking": return c(body);
    case "working": return c(body, ` opacity="0.22"`, "av-ring-track") + c(body, ` stroke-dasharray="92 210" stroke-linecap="round"`);
    case "needs_you": return c(STATE_COLOURS.needs_you);
    case "done": return c(STATE_COLOURS.done);
    case "stuck": return c(STATE_COLOURS.stuck);
    case "paused": return c(STATE_COLOURS.quiet, ` stroke-dasharray="5 4"`);
    case "retired": return c(STATE_COLOURS.quiet, ` stroke-dasharray="1.5 4" opacity="0.7"`);
    default: return "";
  }
}

/** The state badge: a glyph, so the state is never colour or motion alone. */
function badge(state: AvatarState, p: Palette | null): string {
  const tint: string = {
    failed: STATE_COLOURS.stuck, stopped: STATE_COLOURS.quiet,
    thinking: STATE_COLOURS.thinking, speaking: p?.glow ?? STATE_COLOURS.ink, working: p?.body ?? STATE_COLOURS.thinking,
    needs_you: STATE_COLOURS.needs_you, done: STATE_COLOURS.done, stuck: STATE_COLOURS.stuck,
    paused: STATE_COLOURS.quiet, retired: STATE_COLOURS.quiet, idle: "",
  }[state];
  if (!tint) return "";
  const glyph: Record<string, string> = {
    thinking: [-4.6, 0, 4.6].map((x) => `<circle cx="${x}" cy="0" r="1.9" fill="${tint}"/>`).join(""),
    speaking: `<rect x="-6" y="-2.5" width="2.6" height="5" rx="1.3" fill="${tint}"/><rect x="-1.3" y="-5.5" width="2.6" height="11" rx="1.3" fill="${tint}"/><rect x="3.4" y="-2.5" width="2.6" height="5" rx="1.3" fill="${tint}"/>`,
    working: emblemGlyph(EMBLEM_BY_ID.gear, tint, 0.78, [0, 0], 2.4),
    needs_you: `<path d="M-5 3 V-1 A5 5 0 0 1 5 -1 V3 L6.6 4.8 H-6.6 Z" fill="${tint}"/><circle cx="0" cy="6.8" r="1.6" fill="${tint}"/>`,
    done: `<path d="M-5.5 0.2 L-1.6 4.2 L5.8 -4.4" fill="none" stroke="${tint}" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/>`,
    stuck: `<rect x="-1.5" y="-6.6" width="3" height="8.4" rx="1.5" fill="${tint}"/><circle cx="0" cy="5" r="1.7" fill="${tint}"/>`,
    paused: `<rect x="-4.6" y="-5" width="3.2" height="10" rx="1" fill="${tint}"/><rect x="1.4" y="-5" width="3.2" height="10" rx="1" fill="${tint}"/>`,
    // Failed: a cross (it went wrong). Stopped: a stop square (it went no further). Each its own
    // glyph, so neither reads as stuck's "!" or paused's bars by colour alone.
    failed: `<path d="M-4.6 -4.6 L4.6 4.6 M4.6 -4.6 L-4.6 4.6" fill="none" stroke="${tint}" stroke-width="3" stroke-linecap="round"/>`,
    stopped: `<rect x="-4.8" y="-4.8" width="9.6" height="9.6" rx="1.6" fill="${tint}"/>`,
    retired: `<rect x="-6.4" y="-5.4" width="12.8" height="3.6" rx="1" fill="${tint}"/><path d="M-5.2 -1 H5.2 V5.6 H-5.2 Z M-2 1.6 H2" fill="none" stroke="${tint}" stroke-width="1.8" stroke-linejoin="round"/>`,
  };
  return `<g class="av-badge" transform="translate(82 82)"><circle cx="0" cy="0" r="13" fill="${GLASS}" stroke="${tint}" stroke-width="2.5"/>`
    + `<g class="av-badge-glyph">${glyph[state]}</g></g>`;
}

const MEMO = new Map<string, string>();

/** The face, as an SVG string. Pure and memoised: same inputs, same bytes, on every surface. */
export function avatarSvg(spec: AvatarSpec, o: { size: number; state?: AvatarState; variant?: AvatarVariant }): string {
  const lod = lodFor(o.size);
  const state: AvatarState = own(STATE_WORDS, o.state) ? o.state! : "idle";
  // HOW IT LOOKS: a failed face wears stuck's red ring and "!" (something went wrong), a stopped
  // one paused's quiet dashes (nothing is happening). The word (aria, data-state) stays its own.
  const look: AvatarState = state === "failed" ? "stuck" : state === "stopped" ? "paused" : state;
  const variant: AvatarVariant = o.variant ?? "agent";
  const key = `${variant}|${spec.seed}|${spec.hat ?? ""}|${spec.emblem ?? ""}|${spec.palette ?? ""}|${spec.visor ?? ""}|${o.size}|${state}`;
  const hit = MEMO.get(key);
  if (hit) return hit;

  const parts = variant === "agent" ? resolveParts(spec) : null;
  const quiet = look === "paused" || look === "retired";
  const body = parts
    ? (lod === 320 ? `<circle cx="51.5" cy="53" r="44" fill="#000000" opacity="0.35"/>` : "")
      + (state === "needs_you" && lod >= 24
        ? `<circle class="av-halo" cx="50" cy="50" r="47.5" fill="none" stroke="${STATE_COLOURS.needs_you}" stroke-width="5" opacity="0.5"/>` : "")
      + disc(parts.palette.disc)
      + (lod >= 48 ? `<circle cx="50" cy="50" r="35" fill="none" stroke="${parts.palette.body}" stroke-width="0.8" opacity="0.14"/>` : "")
      + `<g class="av-char"${quiet ? ` opacity="0.55"` : ""}>${character(parts, lod, look, spec.seed || "boss")}</g>`
    : (state === "needs_you" && lod >= 24
        ? `<circle class="av-halo" cx="50" cy="50" r="47.5" fill="none" stroke="${STATE_COLOURS.needs_you}" stroke-width="5" opacity="0.5"/>` : "")
      + `<g class="av-char"${quiet ? ` opacity="0.55"` : ""}>${systemGlyph(variant as Exclude<AvatarVariant, "agent">, lod)}</g>`;
  const decorations = lod >= 24 ? ring(look, parts?.palette ?? null) + badge(state, parts?.palette ?? null) : "";

  const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100" width="${o.size}" height="${o.size}" `
    + `class="ag-av" data-kit="boss-v1" data-lod="${lod}" data-state="${state}" data-variant="${variant}" aria-hidden="true" focusable="false">`
    + body + decorations + `</svg>`;
  if (MEMO.size > 600) MEMO.clear();
  MEMO.set(key, svg);
  return svg;
}

/** The face as a data: URI, for <img>, next/og and the A2A card's iconUrl. */
export function avatarDataUri(spec: AvatarSpec, o: { size: number; state?: AvatarState; variant?: AvatarVariant }): string {
  return svgDataUri(avatarSvg(spec, o));
}
