/**
 * THE TEMPLATE GALLERY'S DRAWINGS (Command Center, build-home.tsx): each template card opens with
 * a small drawing of THAT app's main screen, in the same idiom as "Start from an idea"
 * (idea-previews.tsx): 320×180, the same window chrome, palette, type sizes and density, labels
 * and data taken from the template's own brief (engine/boss_engine/data/factory_templates/*.yaml).
 *
 * Drawn inline with literal colours, like the idea drawings: a picture of an app's dark window,
 * not console chrome, so it does not follow the console theme. No images, no network.
 *
 * EVERY TEMPLATE HAS ONE: tests/every-template-has-a-drawing.test.ts fails when a template id in
 * the catalogue has no drawing here. A template the server adds before the console draws it gets
 * its category's drawing (CATEGORY), and an unknown category gets GENERIC, so a card is never blank.
 */
import React from "react";

import { Chrome } from "./idea-previews";

type Screen = { bar: string; body: React.ReactNode };

/** The idea drawings' palette, named. */
const C = {
  bg: "#0b1220", panel: "#111a2b", line: "#24314a", ink: "#e6ecf5", soft: "#a9b6cc", muted: "#93a1bb",
  dim: "#6b7a96", bar: "#1d2a42", bar2: "#2a3753", teal: "#2dd4bf", amber: "#f59e0b", gold: "#fbbf24",
  blue: "#60a5fa", sky: "#38bdf8", pink: "#f472b6", red: "#f87171", green: "#4ade80", violet: "#c4b5fd",
  indigo: "#818cf8", deep: "#0e1626",
} as const;

type TextOpts = { w?: number | string; a?: "start" | "middle" | "end"; m?: boolean; o?: number };

/** One label: x, y, size, colour, words. */
function T({ x, y, s, c, w, a, m, o, children }: { x: number; y: number; s: number; c: string; children: React.ReactNode } & TextOpts) {
  return (
    <text x={x} y={y} fill={c} fontSize={s} fontWeight={w} textAnchor={a} opacity={o}
          className={m ? "ip-mono" : undefined}>{children}</text>
  );
}

/** One box: x, y, width, height, radius, fill, and an optional stroke. */
function B({ x, y, w, h, r = 0, c, o, s }: { x: number; y: number; w: number; h: number; r?: number; c: string; o?: number; s?: string }) {
  return <rect x={x} y={y} width={w} height={h} rx={r} fill={c} opacity={o} stroke={s} />;
}

/** A button: a filled box with a centred label. */
function Btn({ x, y, w, h = 18, c, ink, label }: { x: number; y: number; w: number; h?: number; c: string; ink: string; label: string }) {
  return (
    <><B x={x} y={y} w={w} h={h} r={6} c={c} /><T x={x + w / 2} y={y + h / 2 + 3} s={7.5} c={ink} w={700} a="middle">{label}</T></>
  );
}

/** A pill: tinted background and the label in its colour. */
function Pill({ x, y, w, c, label }: { x: number; y: number; w: number; c: string; label: string }) {
  return (
    <><B x={x} y={y} w={w} h={13} r={6.5} c={c} o={0.2} /><T x={x + w / 2} y={y + 9} s={7} c={c} w={700} a="middle">{label}</T></>
  );
}

/** A gear: a toothed ring (a dashed thick stroke), a hub and a spoke at `angle` degrees. */
function Gear({ cx, cy, r, c, angle, teeth }: { cx: number; cy: number; r: number; c: string; angle: number; teeth: number }) {
  const dash = (2 * Math.PI * r) / teeth / 2;
  const rad = (angle * Math.PI) / 180;
  return (
    <>
      <circle cx={cx} cy={cy} r={r} fill="none" stroke={c} strokeWidth={6} strokeDasharray={`${dash} ${dash}`} />
      <circle cx={cx} cy={cy} r={r - 3} fill={C.panel} stroke={c} strokeWidth={2} />
      <line x1={cx} y1={cy} x2={cx + Math.cos(rad) * (r - 6)} y2={cy + Math.sin(rad) * (r - 6)} stroke={c} strokeWidth={2.5} strokeLinecap="round" />
      <circle cx={cx} cy={cy} r={3.5} fill={c} />
    </>
  );
}

/** An isometric crate at (x, y), side s. */
function Crate({ x, y, s, c, top, side }: { x: number; y: number; s: number; c: string; top: string; side: string }) {
  const h = s / 2;
  return (
    <>
      <polygon points={`${x},${y} ${x + s},${y - h} ${x + 2 * s},${y} ${x + s},${y + h}`} fill={top} />
      <polygon points={`${x},${y} ${x + s},${y + h} ${x + s},${y + h + s} ${x},${y + s}`} fill={c} />
      <polygon points={`${x + s},${y + h} ${x + 2 * s},${y} ${x + 2 * s},${y + s} ${x + s},${y + h + s}`} fill={side} />
    </>
  );
}

/** The Tile swap board: 8×8, five colours, laid out so a few lines are one swap away. */
const TILE_COLOURS = [C.pink, C.sky, C.gold, C.green, C.violet];
const BOARD = Array.from({ length: 64 }, (_, i) => TILE_COLOURS[(i * 3 + Math.floor(i / 8) * 2 + (i % 5 === 0 ? 1 : 0)) % 5]);

/** orders.csv: one empty email, one empty total, two repeated order ids. */
const CSV_ROWS = [
  ["A-101", "Lee", "lee@…", "42.00"], ["A-102", "Kim", "", "18.50"], ["A-102", "Ola", "ola@…", "9.99"],
  ["A-104", "Ray", "ray@…", "61.20"], ["A-104", "Sun", "sun@…", ""],
];

/** A map's base: water-blue land tint, a street grid, and a park. */
function MapBase({ x, y, w, h }: { x: number; y: number; w: number; h: number }) {
  const streets: React.ReactNode[] = [];
  for (let i = 1; i < 6; i += 1) {
    streets.push(<line key={`v${i}`} x1={x + (w * i) / 6} y1={y} x2={x + (w * i) / 6} y2={y + h} stroke="#1b2a3e" strokeWidth={i % 2 ? 2 : 1} />);
  }
  for (let i = 1; i < 5; i += 1) {
    streets.push(<line key={`h${i}`} x1={x} y1={y + (h * i) / 5} x2={x + w} y2={y + (h * i) / 5} stroke="#1b2a3e" strokeWidth={i % 2 ? 1 : 2} />);
  }
  return (
    <><B x={x} y={y} w={w} h={h} r={7} c="#0f1a26" s={C.line} />{streets}</>
  );
}

/** Keyed by the template id (the yaml's `id`). */
const SCREENS: Record<string, Screen> = {
  "delivery-stops": {
    bar: "Delivery stops, Route",
    body: (
      <>
        <MapBase x={10} y={32} w={196} h={138} />
        <polyline points="40,62 96,88 150,70 176,132 74,146" fill="none" stroke={C.teal} strokeWidth={2.5} strokeLinejoin="round" />
        {[[40, 62, "1"], [96, 88, "2"], [150, 70, "3"], [176, 132, "4"], [74, 146, "5"]].map(([cx, cy, n]) => (
          <g key={String(n)}><circle cx={cx as number} cy={cy as number} r={7.5} fill={C.teal} stroke={C.bg} strokeWidth={2} /><T x={cx as number} y={(cy as number) + 3} s={7.5} c="#04201c" w={700} a="middle">{n}</T></g>
        ))}
        <T x={214} y={44} s={8.5} c={C.ink} w={700}>Today, 5 stops</T>
        {[["1", "Elm St 12", "9:10"], ["2", "Bay Rd 4", "9:32"], ["3", "Oak Ave 88", "9:50"], ["4", "Pier 2", "10:15"]].map(([n, place, at], i) => (
          <g key={n}>
            <B x={212} y={52 + i * 22} w={96} h={18} r={5} c={i === 1 ? "#0f2a28" : C.panel} s={i === 1 ? C.teal : C.line} />
            <T x={219} y={64 + i * 22} s={7.5} c={C.teal} w={700}>{n}</T>
            <T x={229} y={64 + i * 22} s={7.5} c={C.ink}>{place}</T>
            <T x={302} y={64 + i * 22} s={7} c={C.muted} a="end">{at}</T>
          </g>
        ))}
        <T x={214} y={152} s={7} c={C.dim}>Drag to reorder</T>
        <T x={214} y={165} s={7.5} c={C.soft}>18.4 km in total</T>
      </>
    ),
  },
  "flood-gauge-map": {
    bar: "Flood gauges, Map",
    body: (
      <>
        <MapBase x={10} y={32} w={196} h={138} />
        <path d="M10 150 C 50 130, 70 90, 110 92 S 170 50, 206 44" fill="none" stroke="#1e4f8f" strokeWidth={9} strokeLinecap="round" />
        {[[34, 136, C.blue], [74, 104, C.blue], [112, 92, C.gold], [150, 72, C.red], [186, 52, C.gold]].map(([cx, cy, c], i) => (
          <g key={i}><circle cx={cx as number} cy={cy as number} r={9} fill={c as string} opacity={0.25} /><circle cx={cx as number} cy={cy as number} r={4.5} fill={c as string} stroke={C.bg} strokeWidth={1.5} /></g>
        ))}
        <B x={212} y={32} w={96} h={82} r={7} c={C.panel} s={C.red} />
        <T x={220} y={46} s={7} c={C.muted}>Gauge 14, Mill Bridge</T>
        <T x={220} y={66} s={15} c={C.ink} w={700}>3.21 m</T>
        <Pill x={220} y={74} w={44} c={C.red} label="Flood" />
        <T x={220} y={104} s={7} c={C.dim}>read 08:45 today</T>
        <circle cx={218} cy={128} r={3.5} fill={C.blue} /><T x={226} y={131} s={7} c={C.soft}>Normal</T>
        <circle cx={218} cy={143} r={3.5} fill={C.gold} /><T x={226} y={146} s={7} c={C.soft}>Watch</T>
        <circle cx={218} cy={158} r={3.5} fill={C.red} /><T x={226} y={161} s={7} c={C.soft}>Flood</T>
      </>
    ),
  },
  "park-bench-map": {
    bar: "Park benches, Map",
    body: (
      <>
        <B x={10} y={32} w={300} h={138} r={7} c="#0e1f1a" s={C.line} />
        <ellipse cx={90} cy={98} rx={70} ry={48} fill="#15382a" />
        <ellipse cx={236} cy={128} rx={60} ry={34} fill="#15382a" />
        <ellipse cx={160} cy={58} rx={26} ry={14} fill="#173a5c" />
        <path d="M14 150 C 80 130, 120 120, 160 96 S 240 70, 306 60" fill="none" stroke="#3a4a3a" strokeWidth={5} strokeLinecap="round" />
        {[[54, 82], [106, 120], [196, 86], [250, 120], [140, 146]].map(([x, y], i) => (
          <g key={i}><B x={x - 6} y={y - 3} w={12} h={6} r={2} c={i === 2 ? C.gold : "#d6c7a1"} s={C.bg} /></g>
        ))}
        <circle cx={196} cy={86} r={11} fill="none" stroke={C.gold} strokeWidth={1.5} />
        <B x={210} y={36} w={96} h={52} r={7} c={C.panel} s={C.gold} />
        <T x={218} y={50} s={8} c={C.gold} w={700}>Bench 7</T>
        <T x={218} y={63} s={7} c={C.ink}>Best shade after 3pm,</T>
        <T x={218} y={74} s={7} c={C.ink}>looks over the pond.</T>
        <T x={218} y={84} s={6.5} c={C.dim}>note saved on this device</T>
      </>
    ),
  },
  "tile-swap": {
    bar: "Tile swap, Level 3",
    body: (
      <>
        <B x={12} y={31} w={142} h={142} r={6} c={C.deep} s={C.line} />
        {BOARD.map((c, i) => (
          <rect key={i} x={16 + (i % 8) * 17} y={35 + Math.floor(i / 8) * 17} width={15} height={15} rx={3.5} fill={c}
                opacity={i === 27 || i === 28 ? 1 : 0.82} stroke={i === 27 || i === 28 ? C.ink : undefined} strokeWidth={i === 27 || i === 28 ? 1.5 : undefined} />
        ))}
        <path d="M 74 77 q 8 -9 16 0" fill="none" stroke={C.ink} strokeWidth={1.5} />
        <T x={170} y={48} s={7.5} c={C.muted}>Score</T>
        <T x={170} y={68} s={17} c={C.ink} w={700}>1,240</T>
        <T x={170} y={90} s={7.5} c={C.muted}>Swaps left</T>
        <T x={170} y={106} s={12} c={C.gold} w={700}>8 / 20</T>
        <B x={170} y={114} w={130} h={7} r={3.5} c={C.bar} />
        <B x={170} y={114} w={78} h={7} r={3.5} c={C.gold} />
        <T x={170} y={140} s={7.5} c={C.soft}>Lines made: 7 of 12</T>
        <Btn x={170} y={150} w={60} c={C.bar} ink={C.ink} label="Restart" />
      </>
    ),
  },
  "orbit-dodge": {
    bar: "Orbit dodge",
    body: (
      <>
        <B x={1} y={25} w={318} h={154} c="#060a14" />
        {[[20, 40], [60, 160], [180, 36], [300, 150], [250, 44], [140, 168], [36, 110]].map(([x, y], i) => (
          <circle key={i} cx={x} cy={y} r={0.9} fill={C.ink} opacity={0.6} />
        ))}
        <circle cx={112} cy={102} r={70} fill="none" stroke={C.line} strokeWidth={1.2} strokeDasharray="3 3" />
        <circle cx={112} cy={102} r={50} fill="none" stroke={C.indigo} strokeWidth={1.4} />
        <circle cx={112} cy={102} r={30} fill="none" stroke={C.line} strokeWidth={1.2} strokeDasharray="3 3" />
        <circle cx={112} cy={102} r={16} fill="#3b2a7a" />
        <circle cx={107} cy={97} r={6} fill={C.violet} opacity={0.35} />
        <polygon points="162,102 154,96 154,108" fill={C.teal} />
        <polygon points="64,62 72,58 76,66 68,70" fill="#8a7f72" />
        <polygon points="150,150 158,146 160,155 152,158" fill="#8a7f72" />
        <polygon points="120,72 127,70 128,77 121,78" fill="#8a7f72" />
        <B x={212} y={34} w={96} h={70} r={7} c={C.panel} s={C.line} />
        <T x={220} y={48} s={7} c={C.muted}>Survived</T>
        <T x={220} y={66} s={15} c={C.ink} w={700}>00:42</T>
        <T x={220} y={82} s={7} c={C.soft}>Best 01:15</T>
        <T x={220} y={96} s={7} c={C.indigo} w={700}>Orbit 2 of 3</T>
        <T x={212} y={124} s={7.5} c={C.soft}>↑ ↓  change orbit</T>
        <T x={212} y={140} s={7.5} c={C.soft}>Space  pause</T>
      </>
    ),
  },
  "lantern-courier": {
    bar: "Lantern courier",
    body: (
      <>
        <B x={1} y={25} w={318} h={154} c="#080b12" />
        {[[20, 40, 60, 12], [100, 40, 12, 60], [150, 70, 70, 12], [40, 120, 90, 12], [180, 110, 12, 56], [240, 40, 12, 70]].map(([x, y, w, h], i) => (
          <rect key={i} x={x} y={y} width={w} height={h} rx={2} fill="#1f2738" />
        ))}
        <circle cx={70} cy={92} r={36} fill={C.gold} opacity={0.12} />
        <circle cx={70} cy={92} r={20} fill={C.gold} opacity={0.18} />
        <circle cx={70} cy={92} r={5} fill={C.gold} />
        <circle cx={70} cy={92} r={2} fill="#fff7d6" />
        <polygon points="160,140 200,124 200,156" fill={C.red} opacity={0.15} />
        <circle cx={160} cy={140} r={4.5} fill={C.red} />
        <polygon points="214,58 254,44 254,72" fill={C.red} opacity={0.15} />
        <circle cx={214} cy={58} r={4.5} fill={C.red} />
        <path d="M 278 160 v -24 a 12 12 0 0 1 24 0 v 24" fill="none" stroke={C.green} strokeWidth={3} />
        <T x={290} y={172} s={7} c={C.green} w={700} a="middle">Gate</T>
        <B x={262} y={32} w={48} h={20} r={6} c={C.panel} s={C.gold} />
        <T x={286} y={46} s={9} c={C.gold} w={700} a="middle" m>0:38</T>
        <T x={12} y={172} s={7} c={C.dim}>Avoid the patrols' light</T>
      </>
    ),
  },
  "gear-assembly": {
    bar: "Gear assembly",
    body: (
      <>
        <Gear cx={74} cy={86} r={36} c={C.amber} angle={-30} teeth={18} />
        <Gear cx={137} cy={78} r={24} c={C.sky} angle={45} teeth={12} />
        <Gear cx={180} cy={108} r={18} c={C.pink} angle={150} teeth={9} />
        <B x={214} y={34} w={94} h={92} r={7} c={C.panel} s={C.line} />
        <T x={222} y={48} s={7} c={C.muted}>Ratio</T>
        <T x={222} y={62} s={10} c={C.ink} w={700}>1 : 1.5 : 2</T>
        <circle cx={225} cy={78} r={3.5} fill={C.amber} /><T x={233} y={81} s={7.5} c={C.soft}>A</T><T x={300} y={81} s={7.5} c={C.ink} a="end" m>120°</T>
        <circle cx={225} cy={94} r={3.5} fill={C.sky} /><T x={233} y={97} s={7.5} c={C.soft}>B</T><T x={300} y={97} s={7.5} c={C.ink} a="end" m>−180°</T>
        <circle cx={225} cy={110} r={3.5} fill={C.pink} /><T x={233} y={113} s={7.5} c={C.soft}>C</T><T x={300} y={113} s={7.5} c={C.ink} a="end" m>240°</T>
        <T x={14} y={150} s={7.5} c={C.muted}>Turn</T>
        <B x={40} y={145} w={240} h={6} r={3} c={C.bar} />
        <B x={40} y={145} w={80} h={6} r={3} c={C.amber} />
        <circle cx={120} cy={148} r={7} fill={C.ink} stroke={C.amber} strokeWidth={2} />
        <T x={306} y={151} s={7.5} c={C.ink} a="end" m>120°</T>
      </>
    ),
  },
  "gallery-walk": {
    bar: "Gallery walk",
    body: (
      <>
        <polygon points="1,25 110,70 110,124 1,179" fill="#151d30" />
        <polygon points="319,25 210,70 210,124 319,179" fill="#151d30" />
        <polygon points="1,179 110,124 210,124 319,179" fill="#1c2236" />
        <polygon points="1,25 110,70 210,70 319,25" fill="#0c1220" />
        <B x={110} y={70} w={100} h={54} c="#10172a" />
        <polygon points="14,60 70,84 70,124 14,140" fill={C.sky} opacity={0.85} />
        <polygon points="78,88 100,97 100,118 78,124" fill={C.pink} opacity={0.75} />
        <polygon points="306,60 250,84 250,124 306,140" fill={C.amber} opacity={0.85} />
        <polygon points="242,88 220,97 220,118 242,124" fill={C.green} opacity={0.75} />
        <B x={140} y={80} w={40} h={30} c={C.violet} o={0.8} />
        <B x={100} y={150} w={120} h={22} r={7} c={C.panel} s={C.line} />
        <T x={160} y={164} s={7.5} c={C.ink} a="middle"><tspan fontWeight={700}>Blue Hour</tspan>, frame 3 of 8</T>
      </>
    ),
  },
  "crate-room": {
    bar: "Crate room",
    body: (
      <>
        <polygon points="160,44 306,112 160,178 14,112" fill="#121a2b" stroke={C.line} />
        <line x1={87} y1={78} x2={233} y2={145} stroke={C.line} />
        <line x1={233} y1={78} x2={87} y2={145} stroke={C.line} />
        <Crate x={60} y={92} s={22} c="#6b4a2a" top="#a5784a" side="#53381f" />
        <Crate x={150} y={64} s={20} c="#6b4a2a" top="#a5784a" side="#53381f" />
        <Crate x={196} y={104} s={24} c="#7a5530" top={C.gold} side="#5e3f20" />
        <Crate x={110} y={122} s={18} c="#6b4a2a" top="#a5784a" side="#53381f" />
        <line x1={244} y1={104} x2={262} y2={70} stroke={C.gold} />
        <B x={238} y={36} w={72} h={34} r={6} c={C.panel} s={C.gold} />
        <T x={246} y={50} s={8} c={C.gold} w={700} m>B-07</T>
        <T x={246} y={63} s={7} c={C.ink}>Hand tools</T>
        <T x={14} y={44} s={7} c={C.dim}>Click a crate to read it</T>
      </>
    ),
  },
  "constellation-viewer": {
    bar: "Constellations",
    body: (
      <>
        <B x={1} y={25} w={318} h={154} c="#060a18" />
        {[[30, 50], [290, 40], [250, 160], [24, 150], [200, 36], [140, 168], [300, 110], [70, 160]].map(([x, y], i) => (
          <circle key={i} cx={x} cy={y} r={0.9} fill={C.ink} opacity={0.5} />
        ))}
        <polyline points="80,56 104,84 96,122 128,150" fill="none" stroke={C.indigo} strokeOpacity={0.6} strokeWidth={1} />
        <polyline points="150,60 132,90 104,84" fill="none" stroke={C.indigo} strokeOpacity={0.6} strokeWidth={1} />
        <polyline points="118,100 126,102 134,104" fill="none" stroke={C.indigo} strokeOpacity={0.6} strokeWidth={1} />
        {[[80, 56, 3.5], [150, 60, 2.6], [104, 84, 2.4], [132, 90, 2.2], [118, 100, 2], [126, 102, 2], [134, 104, 2], [96, 122, 2.4], [128, 150, 3]].map(([x, y, r], i) => (
          <circle key={i} cx={x} cy={y} r={r} fill={i === 0 ? "#ffb38a" : "#dbe6ff"} />
        ))}
        <circle cx={80} cy={56} r={9} fill="none" stroke={C.teal} strokeWidth={1.5} />
        <T x={150} y={150} s={8} c={C.indigo} o={0.7}>Orion</T>
        <B x={196} y={40} w={112} h={56} r={7} c={C.panel} s={C.teal} />
        <T x={204} y={56} s={9} c={C.ink} w={700}>Betelgeuse</T>
        <T x={204} y={70} s={7.5} c={C.soft}>548 light-years</T>
        <T x={204} y={84} s={7} c={C.dim}>from the bundled catalog</T>
        <Btn x={248} y={150} w={60} c={C.teal} ink="#04201c" label="Enter VR" />
      </>
    ),
  },
  "desk-labels": {
    bar: "Desk labels",
    body: (
      <>
        <polygon points="1,179 110,84 210,84 319,179" fill="#121a2b" />
        {[0, 1, 2, 3].map((i) => (
          <line key={`h${i}`} x1={110 - i * 36} y1={84 + i * 32} x2={210 + i * 36} y2={84 + i * 32} stroke={C.line} />
        ))}
        {[0, 1, 2, 3, 4].map((i) => (
          <line key={`v${i}`} x1={110 + i * 25} y1={84} x2={1 + i * 79.5} y2={179} stroke={C.line} />
        ))}
        <line x1={80} y1={140} x2={80} y2={110} stroke={C.sky} /><circle cx={80} cy={140} r={3} fill={C.sky} />
        <B x={50} y={92} w={60} h={18} r={5} c={C.panel} s={C.sky} /><T x={80} y={104} s={7.5} c={C.ink} a="middle">Desk 4, Ana</T>
        <line x1={196} y1={120} x2={196} y2={96} stroke={C.gold} /><circle cx={196} cy={120} r={3} fill={C.gold} />
        <B x={170} y={78} w={52} h={18} r={5} c={C.panel} s={C.gold} /><T x={196} y={90} s={7.5} c={C.ink} a="middle">Printer</T>
        <line x1={150} y1={106} x2={150} y2={92} stroke={C.green} /><circle cx={150} cy={106} r={2.5} fill={C.green} />
        <B x={126} y={60} w={48} h={16} r={5} c={C.panel} s={C.green} /><T x={150} y={71} s={7} c={C.ink} a="middle">Kitchen</T>
        <Btn x={12} y={34} w={66} c={C.sky} ink="#04182a" label="Place label" />
        <B x={228} y={34} w={80} h={18} r={9} c={C.bar} /><T x={268} y={46} s={7} c={C.soft} a="middle">Desktop view ◐</T>
      </>
    ),
  },
  "workshop-measure": {
    bar: "Workshop measure",
    body: (
      <>
        <B x={1} y={25} w={318} h={154} c="#0d1320" />
        <polygon points="20,140 70,96 300,96 260,140" fill="#5a4026" />
        <polygon points="20,140 260,140 260,152 20,152" fill="#3f2c19" />
        <circle cx={78} cy={118} r={5} fill={C.teal} stroke={C.ink} strokeWidth={1.5} />
        <circle cx={232} cy={112} r={5} fill={C.teal} stroke={C.ink} strokeWidth={1.5} />
        <T x={78} y={134} s={7.5} c={C.ink} w={700} a="middle">A</T>
        <T x={232} y={128} s={7.5} c={C.ink} w={700} a="middle">B</T>
        <line x1={78} y1={118} x2={232} y2={112} stroke={C.teal} strokeWidth={1.8} strokeDasharray="4 3" />
        <B x={128} y={76} w={56} h={22} r={7} c={C.teal} />
        <T x={156} y={91} s={10} c="#04201c" w={700} a="middle">1.42 m</T>
        <circle cx={160} cy={52} r={8} fill="none" stroke={C.ink} strokeOpacity={0.6} />
        <line x1={160} y1={40} x2={160} y2={64} stroke={C.ink} strokeOpacity={0.6} />
        <line x1={148} y1={52} x2={172} y2={52} stroke={C.ink} strokeOpacity={0.6} />
        <Btn x={236} y={158} w={72} h={16} c={C.bar} ink={C.ink} label="Mark point" />
        <T x={12} y={170} s={7} c={C.dim}>2 points marked</T>
      </>
    ),
  },
  "invoice-desk": {
    bar: "Invoices, INV-0042",
    body: (
      <>
        <B x={0.5} y={25} w={60} h={154} c={C.deep} />
        <T x={9} y={44} s={7.5} c={C.muted}>Clients</T>
        <B x={6} y={52} w={48} h={14} r={4} c={C.blue} o={0.2} /><T x={10} y={62} s={7.5} c={C.blue} w={700}>Invoices</T>
        <T x={9} y={82} s={7.5} c={C.muted}>Reports</T>
        <T x={72} y={44} s={10} c={C.ink} w={700}>Northwind Ltd</T>
        <Pill x={256} y={34} w={44} c={C.gold} label="Due" />
        <T x={72} y={58} s={7} c={C.dim}>INV-0042, due Nov 30</T>
        <B x={70} y={64} w={238} h={14} r={3} c={C.bar} />
        <T x={76} y={74} s={7} c={C.muted}>ITEM</T><T x={220} y={74} s={7} c={C.muted}>QTY</T><T x={300} y={74} s={7} c={C.muted} a="end">AMOUNT</T>
        {[["Design review", "4", "$480.00"], ["API work", "6", "$720.00"], ["Hosting, Nov", "1", "$80.00"]].map(([item, qty, amt], i) => (
          <g key={item}>
            <T x={76} y={92 + i * 15} s={7.5} c={C.ink}>{item}</T>
            <T x={220} y={92 + i * 15} s={7.5} c={C.soft}>{qty}</T>
            <T x={300} y={92 + i * 15} s={7.5} c={C.ink} a="end">{amt}</T>
          </g>
        ))}
        <line x1={196} y1={132} x2={308} y2={132} stroke={C.line} />
        <T x={200} y={146} s={8} c={C.muted}>Total</T>
        <T x={300} y={147} s={11} c={C.ink} w={700} a="end">$1,280.00</T>
        <Btn x={236} y={154} w={72} h={18} c={C.green} ink="#062a14" label="Mark paid" />
      </>
    ),
  },
  "neighborhood-board": {
    bar: "Neighborhood board",
    body: (
      <>
        <T x={12} y={44} s={10} c={C.ink} w={700}>Notices</T>
        <Btn x={226} y={33} w={82} c={C.pink} ink="#2a0a1c" label="Post a notice" />
        {[
          ["Lost cat, Miso", "Grey tabby, Elm St.", "Nov 24", C.gold],
          ["Yard sale Sat", "Books, a bike, plants.", "Nov 22", C.green],
          ["Street sweep", "Move cars by 8am.", "Nov 20", C.sky],
          ["Choir practice", "Thursdays at the hall.", "Dec 1", C.violet],
        ].map(([title, line, until, c], i) => {
          const x = 12 + (i % 2) * 150;
          const y = 58 + Math.floor(i / 2) * 58;
          return (
            <g key={title}>
              <B x={x} y={y} w={146} h={52} r={7} c={C.panel} s={C.line} />
              <B x={x} y={y} w={4} h={52} r={2} c={c} />
              <T x={x + 12} y={y + 16} s={8.5} c={C.ink} w={700}>{title}</T>
              <T x={x + 12} y={y + 29} s={7.5} c={C.soft}>{line}</T>
              <T x={x + 12} y={y + 44} s={7} c={C.dim}>expires {until}</T>
            </g>
          );
        })}
      </>
    ),
  },
  "recipe-box": {
    bar: "Recipe box",
    body: (
      <>
        <B x={12} y={33} w={120} h={16} r={8} c={C.panel} s={C.line} />
        <T x={22} y={44} s={7.5} c={C.soft}>dinner</T>
        {["Lemon pasta", "Chickpea curry", "Fish tacos", "Tomato soup"].map((name, i) => (
          <g key={name}>
            <B x={12} y={56 + i * 28} w={120} h={24} r={6} c={i === 0 ? "#2a1d08" : C.panel} s={i === 0 ? C.amber : C.line} />
            <circle cx={26} cy={68 + i * 28} r={6} fill={[C.gold, C.amber, C.sky, C.red][i]} opacity={0.8} />
            <T x={38} y={71 + i * 28} s={7.5} c={C.ink} w={i === 0 ? 700 : undefined}>{name}</T>
          </g>
        ))}
        <T x={144} y={46} s={11} c={C.ink} w={700}>Lemon pasta</T>
        <T x={144} y={66} s={7.5} c={C.muted}>Servings</T>
        <B x={196} y={56} w={60} h={16} r={8} c={C.bar} />
        <T x={206} y={67} s={9} c={C.ink} a="middle">−</T><T x={226} y={67} s={8.5} c={C.gold} w={700} a="middle">4</T><T x={246} y={67} s={9} c={C.ink} a="middle">+</T>
        {[["400 g", "spaghetti"], ["2", "lemons"], ["60 g", "parmesan"], ["4 tbsp", "olive oil"]].map(([amt, what], i) => (
          <g key={what}>
            <T x={144} y={92 + i * 16} s={7.5} c={C.gold} w={700}>{amt}</T>
            <T x={186} y={92 + i * 16} s={7.5} c={C.ink}>{what}</T>
          </g>
        ))}
        <T x={144} y={166} s={7} c={C.dim}>scaled from 2 servings</T>
      </>
    ),
  },
  "oncall-card": {
    bar: "On-call",
    body: (
      <>
        <circle cx={30} cy={50} r={14} fill="#0c2a3a" />
        <circle cx={30} cy={46} r={5} fill={C.sky} />
        <path d="M21 59a9 8 0 0 1 18 0" fill={C.sky} />
        <T x={52} y={47} s={10} c={C.ink} w={700}>Raj Patel</T>
        <T x={52} y={60} s={7.5} c={C.muted}>on call until Fri 09:00</T>
        <Pill x={252} y={38} w={56} c={C.green} label="Paged: no" />
        <T x={12} y={86} s={7.5} c={C.muted}>Open incidents</T>
        <B x={12} y={92} w={296} h={18} r={4} c={C.panel} />
        <Pill x={18} y={94.5} w={36} c={C.red} label="SEV-2" />
        <T x={62} y={104} s={7.5} c={C.ink}>Checkout errors at 4%</T><T x={300} y={104} s={7} c={C.dim} a="end">14 min</T>
        <B x={12} y={113} w={296} h={18} r={4} c={C.panel} />
        <Pill x={18} y={115.5} w={36} c={C.gold} label="SEV-3" />
        <T x={62} y={125} s={7.5} c={C.ink}>Slow search in EU</T><T x={300} y={125} s={7} c={C.dim} a="end">2 h</T>
        <B x={12} y={138} w={296} h={34} r={6} c="#0c2a3a" s={C.sky} />
        <T x={20} y={151} s={7} c={C.sky} w={700}>Handoff from Maya</T>
        <T x={20} y={164} s={7.5} c={C.soft}>Cache flush is safe; watch the EU queue.</T>
      </>
    ),
  },
  "fixture-clock": {
    bar: "Terminal",
    body: (
      <>
        <T x={14} y={44} s={8.5} c={C.green} m>$</T><T x={26} y={44} s={8.5} c={C.ink} m>curl localhost:8080/now</T>
        <B x={14} y={52} w={226} h={22} r={5} c="#141b33" s={C.indigo} />
        <T x={22} y={66} s={8.5} c="#c7d2fe" m>{`{ "now": "2026-01-01T09:00:00Z" }`}</T>
        <T x={14} y={92} s={8.5} c={C.green} m>$</T><T x={26} y={92} s={8.5} c={C.ink} m>curl localhost:8080/users</T>
        {[["u_1", "ada", "admin"], ["u_2", "grace", "editor"], ["u_3", "linus", "viewer"]].map(([id, name, role], i) => (
          <g key={id}>
            <T x={22} y={110 + i * 13} s={8} c={C.muted} m>{id}</T>
            <T x={60} y={110 + i * 13} s={8} c={C.ink} m>{name}</T>
            <T x={112} y={110 + i * 13} s={8} c={C.violet} m>{role}</T>
          </g>
        ))}
        <B x={250} y={52} w={58} h={40} r={6} c={C.panel} s={C.line} />
        <T x={258} y={67} s={7} c={C.green} w={700}>● frozen</T>
        <T x={258} y={83} s={7} c={C.muted}>seed 42</T>
        <T x={14} y={168} s={8.5} c={C.green} m>$</T><B x={26} y={160} w={6} h={10} c={C.ink} />
      </>
    ),
  },
  "license-shelf": {
    bar: "Licenses, ./src",
    body: (
      <>
        <T x={12} y={44} s={10} c={C.ink} w={700}>42 packages</T>
        <Pill x={232} y={34} w={76} c={C.red} label="1 without one" />
        <B x={12} y={52} w={296} h={14} r={3} c={C.bar} />
        <T x={18} y={62} s={7} c={C.muted}>PACKAGE</T><T x={170} y={62} s={7} c={C.muted}>LICENSE</T>
        {[
          ["spf13/cobra", "MIT", C.green],
          ["gopkg.in/yaml.v3", "Apache-2.0", C.green],
          ["x/leftpad", "none found", C.red],
          ["google/uuid", "BSD-3-Clause", C.green],
        ].map(([pkg, lic, c], i) => (
          <g key={pkg}>
            <B x={12} y={70 + i * 24} w={296} h={20} r={3} c={i === 2 ? "#2a1218" : C.panel} />
            <T x={18} y={83 + i * 24} s={8} c={C.ink} m>{pkg}</T>
            <Pill x={168} y={73.5 + i * 24} w={i === 2 ? 56 : 64} c={c} label={lic} />
          </g>
        ))}
      </>
    ),
  },
  "census-slice": {
    bar: "Census, Slice",
    body: (
      <>
        <B x={12} y={33} w={86} h={18} r={9} c="#0c2a3a" s={C.sky} /><T x={55} y={45} s={7.5} c={C.sky} w={700} a="middle">Age 25–34 ▾</T>
        <B x={104} y={33} w={86} h={18} r={9} c="#0c2a3a" s={C.sky} /><T x={147} y={45} s={7.5} c={C.sky} w={700} a="middle">Region: North ▾</T>
        <T x={12} y={78} s={7.5} c={C.muted}>People in this slice</T>
        <T x={12} y={102} s={20} c={C.ink} w={700}>12,408</T>
        <T x={12} y={118} s={7.5} c={C.soft}>of 250,000 synthetic rows</T>
        <T x={12} y={164} s={7} c={C.dim} m>duckdb, 18 ms</T>
        {[["North", 88, C.sky], ["South", 62, C.bar2], ["East", 74, C.bar2], ["West", 50, C.bar2]].map(([name, h, c], i) => (
          <g key={name as string}>
            <B x={176 + i * 33} y={166 - (h as number)} w={22} h={h as number} r={3} c={c as string} />
            <T x={187 + i * 33} y={176} s={6.5} c={C.muted} a="middle">{name}</T>
          </g>
        ))}
      </>
    ),
  },
  "csv-quality-desk": {
    bar: "CSV quality, orders.csv",
    body: (
      <>
        {["order_id", "customer", "email", "total"].map((h, k) => (
          <g key={h}><B x={12 + k * 46} y={34} w={44} h={20} r={2} c={C.bar} /><T x={17 + k * 46} y={47} s={6.5} c={C.muted} m>{h}</T></g>
        ))}
        {CSV_ROWS.map((row, r) => row.map((v, k) => {
          const empty = v === "";
          const dup = k === 0 && CSV_ROWS.findIndex((other) => other[0] === v) !== r;
          return (
            <g key={`${r}-${k}`}>
              <rect x={12 + k * 46} y={56 + r * 22} width={44} height={20} rx={2}
                    fill={empty ? "#2a2008" : dup ? "#2a1218" : C.panel}
                    stroke={empty ? C.gold : dup ? C.red : undefined} strokeOpacity={0.7} />
              <T x={17 + k * 46} y={69 + r * 22} s={7} c={dup ? C.red : C.ink} m={k === 0}>{v}</T>
            </g>
          );
        }))}
        <B x={204} y={34} w={104} h={132} r={7} c={C.panel} s={C.line} />
        <T x={212} y={50} s={8} c={C.ink} w={700}>Report</T>
        <T x={212} y={70} s={7.5} c={C.soft}>Empty cells</T><T x={300} y={70} s={9} c={C.gold} w={700} a="end">14</T>
        <T x={212} y={88} s={7.5} c={C.soft}>Duplicate keys</T><T x={300} y={88} s={9} c={C.red} w={700} a="end">3</T>
        <T x={212} y={106} s={7.5} c={C.soft}>Rows</T><T x={300} y={106} s={9} c={C.ink} w={700} a="end">2,180</T>
        <Btn x={212} y={140} w={88} c={C.teal} ink="#04201c" label="Read report" />
      </>
    ),
  },
  "log-sift": {
    bar: "Log sift, app.log",
    body: (
      <>
        <T x={12} y={44} s={8.5} c={C.ink} w={700}>3 groups from 1,906 lines</T>
        {[
          ["×128", "Timeout calling payments", C.red],
          ["×42", "Null price in cart total", C.gold],
          ["×9", "Disk usage above 90%", C.sky],
        ].map(([n, line, c], i) => (
          <g key={line}>
            <B x={12} y={52 + i * 22 + (i > 0 ? 40 : 0)} w={296} h={18} r={4} c={C.panel} s={i === 0 ? (c as string) : undefined} />
            <B x={18} y={55 + i * 22 + (i > 0 ? 40 : 0)} w={34} h={12} r={6} c={c as string} o={0.2} />
            <T x={35} y={64 + i * 22 + (i > 0 ? 40 : 0)} s={7.5} c={c as string} w={700} a="middle">{n}</T>
            <T x={60} y={64 + i * 22 + (i > 0 ? 40 : 0)} s={7.5} c={C.ink}>{line}</T>
          </g>
        ))}
        <T x={22} y={84} s={6.8} c={C.muted} m>09:14:02 ERROR payments: timeout after 5s</T>
        <T x={22} y={96} s={6.8} c={C.muted} m>09:14:09 ERROR payments: timeout after 5s</T>
        <T x={22} y={108} s={6.8} c={C.dim} m>… 126 more like this</T>
      </>
    ),
  },
};

/** A category's drawing, for a template the console has not drawn yet. */
const CATEGORY: Record<string, Screen> = {
  web: {
    bar: "Web app",
    body: (
      <>
        <B x={0.5} y={25} w={60} h={154} c={C.deep} />
        <B x={8} y={34} w={44} h={10} r={3} c={C.blue} o={0.8} />
        {[0, 1, 2].map((i) => <B key={i} x={8} y={56 + i * 16} w={36} h={6} r={3} c={C.bar2} />)}
        {[0, 1, 2].map((i) => (
          <g key={i}><B x={72 + i * 80} y={36} w={74} h={56} r={7} c={C.panel} s={C.line} /><B x={80 + i * 80} y={46} w={40} h={6} r={3} c={C.bar2} /><B x={80 + i * 80} y={62} w={56} h={14} r={4} c={[C.blue, C.pink, C.gold][i]} o={0.35} /></g>
        ))}
        {[0, 1, 2].map((i) => <B key={i} x={72} y={104 + i * 22} w={234} h={18} r={4} c={C.panel} />)}
      </>
    ),
  },
  maps: {
    bar: "Map",
    body: (
      <>
        <MapBase x={10} y={32} w={300} h={138} />
        {[[70, 70, C.teal], [160, 110, C.gold], [240, 64, C.pink]].map(([x, y, c], i) => (
          <g key={i}><circle cx={x as number} cy={y as number} r={9} fill={c as string} opacity={0.25} /><circle cx={x as number} cy={y as number} r={4.5} fill={c as string} /></g>
        ))}
      </>
    ),
  },
  game: {
    bar: "Game",
    body: (
      <>
        <B x={1} y={25} w={318} h={154} c="#060a14" />
        <B x={60} y={140} w={200} h={8} r={4} c={C.bar2} />
        <circle cx={120} cy={120} r={10} fill={C.teal} />
        <polygon points="200,128 212,108 224,128" fill={C.red} />
        <circle cx={260} cy={70} r={5} fill={C.gold} />
        <T x={14} y={44} s={9} c={C.ink} w={700} m>SCORE 0420</T>
      </>
    ),
  },
  "3d": {
    bar: "3D scene",
    body: (
      <>
        <polygon points="160,150 290,110 160,70 30,110" fill="#121a2b" stroke={C.line} />
        <Crate x={124} y={84} s={36} c="#1f3b66" top={C.blue} side="#162c4d" />
      </>
    ),
  },
  xr: {
    bar: "XR scene",
    body: (
      <>
        <polygon points="1,179 110,90 210,90 319,179" fill="#121a2b" />
        {[0, 1, 2].map((i) => <line key={i} x1={110 - i * 50} y1={90 + i * 40} x2={210 + i * 50} y2={90 + i * 40} stroke={C.line} />)}
        <line x1={160} y1={130} x2={160} y2={92} stroke={C.sky} /><circle cx={160} cy={130} r={3} fill={C.sky} />
        <B x={126} y={70} w={68} h={20} r={5} c={C.panel} s={C.sky} /><T x={160} y={83} s={7.5} c={C.ink} a="middle">Label</T>
      </>
    ),
  },
  data: {
    bar: "Data",
    body: (
      <>
        {Array.from({ length: 5 }, (_, r) => <B key={r} x={12} y={34 + r * 22} w={180} h={20} r={3} c={r === 0 ? C.bar : C.panel} />)}
        {[60, 90, 44, 110, 72].map((h, i) => <B key={i} x={206 + i * 20} y={166 - h} w={14} h={h} r={3} c={i === 3 ? C.teal : C.bar2} />)}
      </>
    ),
  },
  internal: {
    bar: "Terminal",
    body: (
      <>
        <T x={14} y={46} s={9} c={C.green} m>$</T><T x={26} y={46} s={9} c={C.ink} m>./tool --help</T>
        {[0, 1, 2, 3].map((i) => <B key={i} x={26} y={60 + i * 16} w={[160, 120, 190, 90][i]} h={6} r={3} c={C.bar2} />)}
        <T x={14} y={150} s={9} c={C.green} m>$</T><B x={26} y={142} w={6} h={11} c={C.ink} />
      </>
    ),
  },
};

/** Any other template: a plain window with a header and rows. */
const GENERIC: Screen = {
  bar: "App",
  body: (
    <>
      <B x={12} y={34} w={120} h={10} r={5} c={C.bar2} />
      {[0, 1, 2, 3].map((i) => <B key={i} x={12} y={56 + i * 26} w={296} h={20} r={5} c={C.panel} s={C.line} />)}
    </>
  ),
};

/** The template ids that have their own drawing: what the coverage test checks the catalogue against. */
export const DRAWN_TEMPLATES: readonly string[] = Object.keys(SCREENS);

/** Which drawing a template gets: its own, its category's, or the generic one. */
export function templatePreviewKind(id: string, category = ""): "template" | "category" | "generic" {
  if (Object.prototype.hasOwnProperty.call(SCREENS, id)) return "template";
  if (Object.prototype.hasOwnProperty.call(CATEGORY, category.trim().toLowerCase())) return "category";
  return "generic";
}

/** The card's top panel: a decorative drawing (the card's own words name it), hidden from readers.
 *  Memoised on its two strings, so typing in the prompt does not redraw every card. */
export const TemplatePreview = React.memo(function TemplatePreview({ id, category = "" }: { id: string; category?: string }) {
  const kind = templatePreviewKind(id, category);
  const screen = kind === "template" ? SCREENS[id] : kind === "category" ? CATEGORY[category.trim().toLowerCase()] : GENERIC;
  return (
    <svg className="ip-svg" aria-hidden="true" focusable="false" viewBox="0 0 320 180"
         preserveAspectRatio="xMidYMid meet" data-preview={kind}>
      <Chrome bar={screen.bar} />
      {screen.body}
    </svg>
  );
});
