/**
 * boss-kit-v1, the faces (spec §6, §12.4 test 10).
 *
 * The kit is a pure function from a spec to an SVG string, so the properties that make a
 * character feel like one character are checkable directly: the same seed is the same bytes,
 * two names never share a face, a small face drops detail rather than mush it, every colour
 * reads on the console's surfaces, and no part of the drawing pokes out of its porthole.
 */
import { describe, expect, it } from "vitest";

import { avatarDataUri, avatarSvg, cyrb53, lodFor, resolveParts, rerollSpec, seededSpec, specForRole } from "../lib/agent-avatar";
import { CAST } from "../lib/avatar-kit/cast";
import { EMBLEMS } from "../lib/avatar-kit/emblems";
import { GENERIC_HATS, HATS } from "../lib/avatar-kit/hats";
import { PALETTES, SURFACES, contrast } from "../lib/avatar-kit/palettes";
import { AVATAR_STATES } from "../lib/avatar-kit/states";
import { VISORS } from "../lib/avatar-kit/visors";

const faceKey = (s: { hat?: string; emblem?: string; palette?: string; visor?: string }) =>
  `${s.hat}|${s.emblem}|${s.palette}|${s.visor}`;

describe("a face is deterministic, and two agents never share one", () => {
  it("draws the same bytes for the same seed, every time", () => {
    const a = avatarSvg(seededSpec("agt_7f3k2q4m"), { size: 48, state: "working" });
    const b = avatarSvg({ kit: "boss-v1", seed: "agt_7f3k2q4m" }, { size: 48, state: "working" });
    expect(a).toBe(b);
  });

  it("gives anagrams different faces (the old char-sum hash could not)", () => {
    expect(cyrb53("qa_lead")).not.toBe(cyrb53("lead_qa"));
    expect(avatarSvg(seededSpec("qa_lead"), { size: 48 })).not.toBe(avatarSvg(seededSpec("lead_qa"), { size: 48 }));
  });

  it("gives every shipped role its own face", () => {
    const faces = CAST.map((c) => faceKey(c.avatar));
    expect(new Set(faces).size).toBe(CAST.length);
  });

  it("lets every hand-designed character own its hat, worn by no one else", () => {
    const hand = CAST.filter((c) => c.designed === "hand");
    expect(new Set(hand.map((c) => c.avatar.hat)).size).toBe(hand.length);
    for (const c of hand) expect(HATS.find((h) => h.id === c.avatar.hat)?.cast, c.roleKey).toBe(true);
  });

  it("never gives a custom agent a shipped role's face, or a cast hat", () => {
    const cast = new Set(CAST.map((c) => faceKey(c.avatar)));
    const castHats = new Set(HATS.filter((h) => h.cast).map((h) => h.id));
    for (let i = 0; i < 3000; i++) {
      const p = resolveParts(seededSpec(`agt_${i.toString(36)}x`));
      expect(cast.has(faceKey({ hat: p.hat.id, emblem: p.emblem.id, palette: p.palette.id, visor: p.visor.id }))).toBe(false);
      expect(castHats.has(p.hat.id)).toBe(false);
    }
  });

  it("spreads custom agents over the whole kit, not a corner of it", () => {
    const seen = { hat: new Set<string>(), emblem: new Set<string>(), palette: new Set<string>(), visor: new Set<string>() };
    for (let i = 0; i < 2000; i++) {
      const p = resolveParts(seededSpec(`custom-${i}`));
      seen.hat.add(p.hat.id); seen.emblem.add(p.emblem.id); seen.palette.add(p.palette.id); seen.visor.add(p.visor.id);
    }
    expect(seen.hat.size).toBe(GENERIC_HATS.length);
    expect(seen.emblem.size).toBe(EMBLEMS.length);
    expect(seen.palette.size).toBe(PALETTES.length);
    expect(seen.visor.size).toBe(VISORS.length);
  });

  it("rerolls only the seed: parts picked by hand stay picked", () => {
    const picked = { kit: "boss-v1" as const, seed: "agt_1", hat: "antenna", palette: "teal" };
    const next = rerollSpec(picked, "n1");
    expect(next).toEqual({ ...picked, seed: "agt_1~n1" });
    expect(rerollSpec(next, "n2").seed).toBe("agt_1~n2");
    expect(avatarSvg(rerollSpec(seededSpec("agt_1"), "n1"), { size: 48 })).not.toBe(avatarSvg(seededSpec("agt_1"), { size: 48 }));
  });

  it("draws a shipped role from its cast entry, not its seed", () => {
    expect(specForRole("qa")).toEqual(CAST.find((c) => c.roleKey === "qa")!.avatar);
    expect(specForRole("not_a_role")).toEqual(seededSpec("not_a_role"));
  });
});

describe("detail follows size", () => {
  const qa = specForRole("qa");
  it("maps pixel sizes to the six tiers", () => {
    expect([16, 20, 24, 32, 48, 96, 320].map(lodFor)).toEqual([16, 24, 24, 32, 48, 96, 320]);
  });

  it("16px is a silhouette: no eyes, no mouth, no ring, no badge", () => {
    const s = avatarSvg(qa, { size: 16, state: "working" });
    for (const part of ["av-eyes", "av-mouth", "av-ring", "av-badge"]) expect(s).not.toContain(part);
    expect(s).toContain("av-hat");
  });

  it("24px adds the state ring and badge, still no eyes", () => {
    const s = avatarSvg(qa, { size: 24, state: "working" });
    expect(s).toContain("av-ring");
    expect(s).toContain("av-badge");
    expect(s).not.toContain("av-eyes");
  });

  it("32px adds the eye and mouth bars; 48px the styled eyes and hat detail", () => {
    const s32 = avatarSvg(qa, { size: 32 });
    const s48 = avatarSvg(qa, { size: 48 });
    expect(s32).toContain("av-eyes");
    expect(s32.match(/class="av-bar /g)).toHaveLength(3);
    expect(s48.length).toBeGreaterThan(s32.length);
    expect(s48).toContain('stroke="#ffffff"'); // the magnifier's glint, detail only
    expect(s32).not.toContain('d="M57 13.4');
  });

  it("320px adds the soft shadow, without an SVG filter", () => {
    expect(avatarSvg(qa, { size: 320 })).toContain('opacity="0.35"');
  });
});

describe("the faces are safe to inline anywhere", () => {
  it("uses no ids, filters, gradients or clip paths, so many faces on one page cannot collide", () => {
    for (const c of CAST) {
      for (const size of [16, 24, 32, 48, 96, 320]) {
        const s = avatarSvg(c.avatar, { size, state: "needs_you" });
        expect(s).not.toMatch(/\sid=|<filter|Gradient|clip-path|<clipPath|url\(#/);
      }
    }
  });

  it("never writes a spec's text into the markup: unknown part ids fall back to kit parts", () => {
    const hostile = { kit: "boss-v1" as const, seed: "x\"><script>alert(1)</script>", hat: "\"><img src=x>", palette: "red\" onload=\"x", visor: "<g>", emblem: "&" };
    const s = avatarSvg(hostile, { size: 48 });
    expect(s).not.toContain("<script");
    expect(s).not.toContain("<img");
    expect(s).not.toContain("onload");
    expect(s.match(/<svg/g)).toHaveLength(1);
  });

  it("encodes to a data URI for <img>, next/og and the A2A card", () => {
    const uri = avatarDataUri(specForRole("pm"), { size: 320 });
    // One encoder for every SVG drawn through <img> (lib/svg-data-uri.ts): base64, namespace kept.
    expect(uri.startsWith("data:image/svg+xml;base64,")).toBe(true);
    expect(Buffer.from(uri.split(",")[1], "base64").toString("utf8")).toBe(avatarSvg(specForRole("pm"), { size: 320 }));
  });

  it("draws every state for every variant without throwing", () => {
    for (const variant of ["agent", "auto", "you", "person", "boss"] as const) {
      for (const state of AVATAR_STATES) {
        const s = avatarSvg(seededSpec("x"), { size: 48, state, variant });
        expect(s).toContain(`data-state="${state}"`);
        expect(s).toContain(`data-variant="${variant}"`);
      }
    }
  });
});

describe("every colour reads on the console", () => {
  it("every palette's head plate is 3:1 or better on every surface, computed", () => {
    for (const p of PALETTES) {
      for (const bg of SURFACES) expect(contrast(p.body, bg), `${p.id} on ${bg}`).toBeGreaterThanOrEqual(3);
    }
  });

  it("every palette's eye light is 3:1 or better on the visor glass", () => {
    for (const p of PALETTES) expect(contrast(p.glow, "#0a1122"), p.id).toBeGreaterThanOrEqual(3);
  });
});

/** Every point a piece of SVG markup draws, measured from the markup itself: rect corners,
 *  circle and ellipse extremes (after a rotate), and every vertex and control point of an
 *  absolute path. Conservative: a rounded corner counts as square. */
function drawnPoints(svg: string): [number, number][] {
  const pts: [number, number][] = [];
  const num = (tag: string, a: string) => Number(new RegExp(`\\s${a}="(-?[\\d.]+)"`).exec(tag)?.[1] ?? 0);
  for (const m of svg.matchAll(/<rect\b[^>]*>/g)) {
    const [x, y, w, h] = ["x", "y", "width", "height"].map((a) => num(m[0], a));
    pts.push([x, y], [x + w, y], [x, y + h], [x + w, y + h]);
  }
  for (const m of svg.matchAll(/<(circle|ellipse)\b[^>]*>/g)) {
    const cx = num(m[0], "cx"), cy = num(m[0], "cy");
    const rx = m[1] === "circle" ? num(m[0], "r") : num(m[0], "rx");
    const ry = m[1] === "circle" ? rx : num(m[0], "ry");
    const rot = Number(/rotate\((-?[\d.]+)/.exec(m[0])?.[1] ?? 0) * Math.PI / 180;
    for (let t = 0; t < 2 * Math.PI; t += Math.PI / 12) {
      const ex = rx * Math.cos(t), ey = ry * Math.sin(t);
      pts.push([cx + ex * Math.cos(rot) - ey * Math.sin(rot), cy + ex * Math.sin(rot) + ey * Math.cos(rot)]);
    }
  }
  for (const m of svg.matchAll(/\sd="([^"]+)"/g)) {
    expect(m[1], "hats use absolute path commands only").not.toMatch(/[a-z]/);
    let cur: [number, number] = [0, 0];
    const bez = (ctrl: [number, number][]) => {
      // Sample the curve itself: control points sit outside it and would overstate the hat.
      const all = [cur, ...ctrl];
      for (let t = 0; t <= 1.0001; t += 0.05) {
        let row = all;
        while (row.length > 1) row = row.slice(1).map((q, i) => [row[i][0] + (q[0] - row[i][0]) * t, row[i][1] + (q[1] - row[i][1]) * t] as [number, number]);
        pts.push(row[0]);
      }
      cur = ctrl[ctrl.length - 1];
    };
    for (const seg of m[1].matchAll(/([MLHVQCZ])([^MLHVQCZ]*)/g)) {
      const n = seg[2].trim().split(/[\s,]+/).filter(Boolean).map(Number);
      const pair = (i: number): [number, number] => [n[i], n[i + 1]];
      if (seg[1] === "H") cur = [n[0], cur[1]];
      else if (seg[1] === "V") cur = [cur[0], n[0]];
      else if (seg[1] === "Q") for (let i = 0; i + 3 < n.length; i += 4) bez([pair(i), pair(i + 2)]);
      else if (seg[1] === "C") for (let i = 0; i + 5 < n.length; i += 6) bez([pair(i), pair(i + 2), pair(i + 4)]);
      else for (let i = 0; i + 1 < n.length; i += 2) cur = pair(i);
      pts.push(cur);
    }
  }
  return pts;
}

describe("every hat stays inside the porthole", () => {
  // The disc is centred at 50,50 with r 44; a hat outside it would be cut by the state ring or
  // by the chip around the face. Measured from what the hat actually draws, at full detail, in
  // every palette.
  it.each(HATS.map((h) => [h.id, h] as const))("%s", (_id, hat) => {
    for (const p of PALETTES) {
      const pts = drawnPoints(hat.draw(p, true));
      if (hat.id !== "none") expect(pts.length).toBeGreaterThan(3);
      for (const [x, y] of pts) {
        expect(Math.hypot(x - 50, y - 50), `${hat.id} draws ${x.toFixed(1)},${y.toFixed(1)}`).toBeLessThanOrEqual(44.5);
      }
    }
  });
});
