/**
 * Meet the cast: writes docs/cast/<role>.svg and docs/cast/cast-grid.svg from avatarSvg, the
 * same function every console surface draws with, so the README can never show a face the
 * product does not (spec §11.3). GitHub renders SVG, so the repo's front page shows the crew.
 *
 *   pnpm --filter boss-web cast:export
 *
 * The script runs under vitest (tests/the-cast-is-exported.test.ts with CAST_EXPORT=1) because
 * the web package has no TypeScript runner of its own and Node 20 cannot strip types; the same
 * test, without the variable, fails when docs/cast has drifted from the kit.
 */
import { avatarSvg } from "../lib/agent-avatar";
import { CAST } from "../lib/avatar-kit/cast";
import { EMBLEMS } from "../lib/avatar-kit/emblems";
import { HATS } from "../lib/avatar-kit/hats";
import { PALETTES } from "../lib/avatar-kit/palettes";
import { VISORS } from "../lib/avatar-kit/visors";

const esc = (s: string) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
const FONT = "Sora, Manrope, -apple-system, 'Segoe UI', Helvetica, Arial, sans-serif";

/** One face, placed inside a larger SVG. */
function placed(svg: string, x: number, y: number, size: number): string {
  return svg.replace(/^<svg /, `<svg x="${x}" y="${y}" `).replace(/width="\d+" height="\d+"/, `width="${size}" height="${size}"`);
}

export function castGridSvg(): string {
  const crew = CAST.filter((c) => c.designed === "hand");
  const cols = 5, cellW = 232, cellH = 262, pad = 36, head = 92;
  const rows = Math.ceil(crew.length / cols);
  const w = pad * 2 + cols * cellW, h = head + pad + rows * cellH;
  const cells = crew.map((c, i) => {
    const x = pad + (i % cols) * cellW, y = head + Math.floor(i / cols) * cellH;
    const cx = x + cellW / 2;
    return placed(avatarSvg(c.avatar, { size: 320 }), cx - 74, y, 148)
      + `<text x="${cx}" y="${y + 176}" text-anchor="middle" font-size="19" font-weight="700" fill="#eef3fb">${esc(c.callsign ?? c.name)}</text>`
      + `<text x="${cx}" y="${y + 198}" text-anchor="middle" font-size="13" fill="#9fb0ce">${esc(c.name)}</text>`
      + `<text x="${cx}" y="${y + 222}" text-anchor="middle" font-size="12.5" font-style="italic" fill="#c3d0e8">${esc(`“${c.tagline ?? ""}”`)}</text>`
      + (c.archetype ? `<text x="${cx}" y="${y + 242}" text-anchor="middle" font-size="11" letter-spacing="1.2" fill="#a78bfa">THINKS LIKE · AI AGENT</text>` : "");
  }).join("");
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${w} ${h}" width="${w}" height="${h}" font-family="${FONT}">`
    + `<rect width="${w}" height="${h}" rx="18" fill="#090d17"/>`
    + `<text x="${pad}" y="52" font-size="26" font-weight="700" fill="#eef3fb">The BOSS crew</text>`
    + `<text x="${pad}" y="76" font-size="14" fill="#9fb0ce">AI agents with roles, not people. Every one says it is an AI. boss-kit-v1 · Apache-2.0</text>`
    + cells + `</svg>`;
}

/** The part ids a persona's `avatar` may name: what the API validates against (spec §4.2). */
export function kitManifest() {
  return {
    kit: "boss-v1",
    hats: HATS.filter((h) => !h.cast).map((h) => h.id),
    cast_hats: Object.fromEntries(CAST.filter((c) => HATS.find((h) => h.id === c.avatar.hat)?.cast).map((c) => [c.avatar.hat!, c.roleKey])),
    emblems: EMBLEMS.map((e) => e.id),
    palettes: PALETTES.map((p) => p.id),
    visors: VISORS.map((v) => v.id),
    cast: Object.fromEntries(CAST.map((c) => [c.roleKey, { ...c.avatar, ...(c.callsign ? { callsign: c.callsign } : {}) }])),
  };
}

/** Every file the export writes, by name. */
export function castFiles(): Record<string, string> {
  const out: Record<string, string> = {
    "cast-grid.svg": castGridSvg() + "\n",
    "kit-manifest.json": JSON.stringify(kitManifest(), null, 2) + "\n",
  };
  for (const c of CAST) out[`${c.roleKey}.svg`] = avatarSvg(c.avatar, { size: 320 }) + "\n";
  return out;
}
