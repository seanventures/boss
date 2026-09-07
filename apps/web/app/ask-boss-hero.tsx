"use client";

/**
 * Ask BOSS — the first thing on the Command Center.
 *
 * What was here was a "Status Query" form that posted to Metatron: three inputs, a GET
 * submit, and a read-only paragraph back. It sat below the fold, it could only answer, and
 * two of its three fields asked the operator for ids they would have to go and look up.
 *
 * The agent can already do all of this and act as well, so the entry point is one line of
 * plain language at the top of the page. Scope is inferred, not typed — an operator who has
 * to find a task id before asking a question will go and read the table instead, which is
 * exactly the behaviour this is meant to replace.
 *
 * The animation earns its place by carrying state rather than decorating: the aura is calm
 * when idle, active while a turn runs, and amber once a turn passes what is normal. There is
 * no purely ornamental motion, and all of it collapses under prefers-reduced-motion.
 */

import type { ReactElement } from "react";
import { useCallback, useEffect, useRef, useState, useTransition } from "react";
import { createPortal } from "react-dom";

import { Markdown } from "./markdown";
import { notifyChanged } from "./changed";
import { facetsOf } from "./template-brief";
import { CRAFT, DETAILS, buildBrief } from "./template-details";
import { askBossAction, runAgentActionAction } from "./actions";

const NORMAL_SECONDS = 20;
// The same bound the dock uses. A deep turn is genuinely longer since the answering agent
// moved to Opus — 192s for 27 lookups, measured — and 120 handed control back in the middle of
// work that was going fine. Kept in step with agent-ui.tsx deliberately: two numbers for one
// behaviour is how the Command Center came to be reporting "running long" while the dock,
// asking the same agent the same kind of question, was not.
const STALL_SECONDS = 240;

type ProposedAction = {
  id: string;
  tool: string;
  args: Record<string, unknown>;
  label: string;
  why?: string;
};

/** Openers that demonstrate the range: read, diagnose, act. Each is a real request. */
const OPENERS = [
  { badge: "ST", tone: "accent", text: "What changed, what is blocked, and what should I do next?" },
  { badge: "RN", tone: "blue",   text: "Which runs failed today, and why?" },
  { badge: "QU", tone: "amber",  text: "Is anything starving or stuck in a queue?" },
  { badge: "$$", tone: "green",  text: "What is my spend so far this month?" },
];

/**
 * Starter outcomes, for the case the composer is worst at: an empty box and no idea.
 *
 * These are DIRECTIVES, not questions — each one goes down the governed path (panel, PRD,
 * code task), which is the same thing the separate "Assign an outcome" bar used to do. They
 * exist because a blank prompt is the hardest possible first run, and because the fastest way
 * to learn what BOSS does is to watch it do one.
 */
/**
 * Starter briefs.
 *
 * These were one-line prompts that fired a governed build the instant they were clicked —
 * which is both too little information to build from and too much commitment for a click. A
 * brief now DRAFTS into the composer: it says what the thing is, who it is for, what it must
 * do, and how you know it worked, and the operator edits it before pressing Build.
 *
 * Themes follow the categories YC and similar programmes keep asking for (developer tooling,
 * vertical SaaS, ops automation, data plumbing, agents). They are shipped in the bundle rather
 * than fetched, so they work offline and cost nothing to show; a live feed is a separate
 * capability and is noted in the UI rather than faked here.
 */
type Idea = {
  tag: string; title: string; brief: string;
  /** Hue for the card's thumbnail. A wall of identical grey cards is a list pretending to be a
   *  gallery — colour is what makes sixteen of them scannable rather than readable. */
  /** Which glyph the thumbnail draws. Inline SVG, not an image URL: the console must render
   *  with no network beyond its own origin, and a broken <img> is worse than no picture. */
  glyph: GlyphId;
};

type GlyphId = keyof typeof GLYPHS;

/** Small, flat marks — a suggestion of the domain, not an illustration of it. Each is a single
 *  path on a 24-box so they sit at the same optical weight. */

/** A miniature of the app the idea describes — an inbox, a board, a chart, a calendar, a
 *  timeline — drawn over the card's colour.
 *
 *  A gradient wash is not an image. The operator asked for imagery and got coloured
 *  rectangles twice; what a starter card wants to show is what the thing would LOOK like, so
 *  these are little UI mockups: window chrome, rows, columns, bars, milestones.
 *
 *  Drawn, not fetched, for the same reason as before — this console is reachable only over the
 *  tailnet, and a card that depends on a CDN is a broken image the day the CDN is unreachable.
 *  White at low opacity over the hue, so one set of shapes works on every colour.
 */

/*  NO STOCK PHOTOGRAPHY. Removed deliberately, and it is not coming back without a source of
 *  TOPICAL images.
 *
 *  Cards had a seeded Picsum photograph over the drawing. Picsum serves an ARBITRARY photo per
 *  seed — so "Docs freshness board" got an iceberg, "Lead triage inbox" got a mountain range
 *  and "Change calendar" got a suspension bridge at sunset. Stable per idea, and meaningless:
 *  a gallery of holiday scenery on a page about software you are about to build. It reads as
 *  decoration applied without looking at it, because that is exactly what it was.
 *
 *  The tools this imitates do not use stock photos either. They show the THING — a small
 *  rendering of the app's own interface. That is what MOCKUPS below draws: a board for a
 *  board, rows for an inbox, a chart for a report. It is topical by construction, needs no
 *  API key, no network, and cannot arrive as a picnic.
 */


/** The palette. FOUR TONES THAT BELONG TOGETHER, not a hue wheel.
 *
 *  Cards carried ` 152, 268, 196, 28, 340, 12` — points scattered at random around the
 *  colour circle, one per card. Sixteen unrelated colours next to each other is why a row of
 *  them read as arbitrary: nothing said these were one product. A template gallery wants the
 *  opposite — one surface, one language, an accent that varies just enough to tell cards
 *  apart. So the surface is fixed and only the accent moves, across four cool tones drawn from
 *  the console's own palette.
 */
/** THE PALETTE, TAKEN FROM WHAT ACTUALLY WORKED.
 *
 *  "Living Colors" — an app BOSS built and the operator liked on sight — has NO image files at
 *  all. Its look is twelve CSS gradients over four vivid tokens: 330 81% 58%, 262 83% 63%,
 *  24 95% 58%, 172 66% 42%. Saturation 66–95%, lightness 42–63%, painted across whole sections
 *  with two big blurred orbs for depth and white content on top.
 *
 *  These cards were the opposite: #161c2e / #1d2438 / #273049 — saturation about 25%, lightness
 *  12–20% — a near-black window covering the whole cover with a few coloured pixels in it. Dark
 *  UI screenshots, competently drawn and completely flat, which is what "the images are so
 *  poor" meant and it was right.
 *
 *  Same technique here: a three-stop gradient per domain, orbs, and the app's own layout drawn
 *  in WHITE over it. Not the earlier washed-out attempt — that failed because it used a random
 *  hue per card, a texture layer, a 48% wash over the bottom half, and shapes at 6–14% opacity
 *  that could not hold an edge. The colour is coherent per domain and the shapes are opaque.
 */
const DOMAIN_GRADIENTS: Record<string, [string, string, string]> = {
  Ops:      ["hsl(330 81% 58%)", "hsl(292 83% 60%)", "hsl(262 83% 63%)"],
  Money:    ["hsl(24 95% 58%)",  "hsl(38 94% 56%)",  "hsl(330 81% 58%)"],
  Team:     ["hsl(262 83% 63%)", "hsl(228 87% 62%)", "hsl(199 89% 55%)"],
  Content:  ["hsl(172 66% 45%)", "hsl(186 78% 48%)", "hsl(199 89% 55%)"],
  Sales:    ["hsl(24 95% 58%)",  "hsl(348 85% 60%)", "hsl(330 81% 58%)"],
  Quality:  ["hsl(199 89% 55%)", "hsl(230 85% 62%)", "hsl(262 83% 63%)"],
  Learning: ["hsl(142 71% 45%)", "hsl(160 70% 44%)", "hsl(172 66% 45%)"],
  Growth:   ["hsl(330 81% 58%)", "hsl(0 84% 62%)",   "hsl(24 95% 58%)"],
};

function gradientFor(tag: string): [string, string, string] {
  return DOMAIN_GRADIENTS[tag] ?? DOMAIN_GRADIENTS.Team;
}

/** The layout drawn ON the colour. White, at opacities that hold an edge — the previous
 *  version's 6–14% is why it read as weather rather than as a picture of anything. */
const UI = {
  chrome: "rgba(255,255,255,.10)",
  panel: "rgba(255,255,255,.14)",
  row: "rgba(255,255,255,.26)",
  line: "rgba(255,255,255,.34)",
  text: "rgba(255,255,255,.62)",
  bright: "rgba(255,255,255,.95)",
} as const;

/** The highlight colour on top of a coloured field is white, not another hue — a second
 *  saturated colour on a saturated ground is mud, which the first attempt proved. */
function accentFor(_title: string): string {
  return "rgba(255,255,255,.95)";
}

/** A line of "text": a rounded bar, the way every UI mock draws one. */
function Line({ x, y, w, h = 4, c = UI.text, o = 1 }: {
  x: number; y: number; w: number; h?: number; c?: string; o?: number;
}) {
  return <rect x={x} y={y} width={w} height={h} rx={h / 2} fill={c} opacity={o} />;
}

/** The app's own interface, in miniature — the thing a starter card should show.
 *
 *  Each of these is a small SCREENSHOT: a sidebar, a header, and content laid out the way the
 *  idea implies. An inbox is rows with avatars and a status pill; a board is columns of cards;
 *  an audit is a chart with an axis; a rotation is a month grid; an incident is a timeline.
 *  The accent appears two or three times at most — the selected item, the primary button, one
 *  data point — which is how real interfaces use colour and why this reads as a product
 *  instead of as decoration.
 */
/** The file name a card's photograph would have, if one has been supplied.
 *
 *  REAL IMAGES, SERVED BY THIS APP. Not a CDN: the console is tailnet-only, so an image
 *  fetched from the internet is a broken card the day that host is unreachable — which is why
 *  the seeded-Picsum attempt was removed. `public/` is same-origin, needs no key, and the file
 *  is in the repo, so what ships is what was reviewed.
 *
 *  A missing file is not an error. `onError` hides the <img> and the drawn mockup underneath
 *  shows instead, so the strip is complete with none of these present, all sixteen present, or
 *  any number in between — which is what makes it safe to add them one at a time.
 */
function photoFor(title: string): string {
  return `/starters/${slugFor(title)}.jpg`;
}

/** The slug used for both the file name and the accent, so they cannot disagree. */
function slugFor(title: string): string {
  return title.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
}

/** A panel inside the window — the surface content sits on. */
function Panel({ x = 78, y = 30, w = 222, h = 82 }: {
  x?: number; y?: number; w?: number; h?: number;
}) {
  return <rect x={x} y={y} width={w} height={h} rx="5" fill={UI.panel} />;
}

/** A row/card on a panel. */
function Row({ x, y, w, h = 16, c = UI.row, o = 1 }: {
  x: number; y: number; w: number; h?: number; c?: string; o?: number;
}) {
  return <rect x={x} y={y} width={w} height={h} rx="3.5" fill={c} opacity={o} />;
}

/** SIXTEEN IDEAS, SIXTEEN DRAWINGS.
 *
 *  The previous set had five, and mapped them by GLYPH — so seven of the sixteen ideas showed
 *  the identical rows-with-avatars picture and three more shared a second one. On screen that
 *  is three adjacent cards that differ only in accent colour, which is not a gallery: it is one
 *  image recoloured, and an operator said so.
 *
 *  Each idea now gets a layout that belongs to IT — a scorecard has criteria and ratings, a
 *  freshness board has age bars with stale rows in warning colour, a linter has underlined
 *  code, a rota has people against weeks. Two cards next to each other cannot look alike
 *  because no two draw the same thing.
 */
const MOCKUPS: Record<string, (k: string, a: string) => ReactElement> = {
  /** Milestones on a line: incidents, releases. */
  timeline: (k, a) => (
    <g key={k}>
      <Panel />
      <line x1="94" y1="71" x2="288" y2="71" stroke={UI.line} strokeWidth="2" strokeLinecap="round" />
      <line x1="94" y1="71" x2="196" y2="71" stroke={a} strokeWidth="2" strokeLinecap="round" />
      {[0, 1, 2, 3].map((i) => (
        <g key={i} transform={`translate(${100 + i * 62} 0)`}>
          <circle cx="0" cy="71" r={i === 1 ? 6 : 4.5} fill={i <= 1 ? a : UI.row} />
          <Line x={-20} y={i % 2 ? 84 : 50} w={40} h={3.5} c={i === 1 ? UI.bright : UI.text} />
          <Line x={-20} y={i % 2 ? 91 : 57} w={26} h={3} o={0.45} />
        </g>
      ))}
    </g>
  ),

  /** Bars with an axis and one column called out: spend, audits. */
  chart: (k, a) => (
    <g key={k}>
      <Panel />
      {[0, 1, 2].map((i) => (
        <line key={i} x1="90" y1={48 + i * 20} x2="290" y2={48 + i * 20}
              stroke={UI.line} strokeWidth="1" opacity=".5" />
      ))}
      {[26, 40, 31, 52, 38, 60, 45].map((h, i) => (
        <rect key={i} x={94 + i * 28} width="15" y={104 - h} height={h} rx="2.5"
              fill={i === 5 ? a : UI.row} />
      ))}
      <line x1="88" y1="106" x2="292" y2="106" stroke={UI.line} strokeWidth="1.5" />
    </g>
  ),

  /** Criteria with ratings: scorecards, reviews. */
  scorecard: (k, a) => (
    <g key={k}>
      <Panel />
      {[0, 1, 2, 3].map((i) => (
        <g key={i} transform={`translate(0 ${i * 19})`}>
          <Line x={88} y={40} w={62 - i * 6} h={4} c={i === 0 ? UI.bright : UI.text} />
          {[0, 1, 2, 3, 4].map((n) => (
            <circle key={n} cx={186 + n * 15} cy={42} r="4"
                    fill={n <= 3 - (i % 3) ? a : UI.row} />
          ))}
        </g>
      ))}
    </g>
  ),

  /** Documents with an age bar, the stale ones flagged: freshness, coverage of docs. */
  freshness: (k, a) => (
    <g key={k}>
      <Panel />
      {[0, 1, 2, 3].map((i) => {
        const stale = i === 1 || i === 3;
        return (
          <g key={i} transform={`translate(0 ${i * 19})`}>
            <Line x={88} y={40} w={54} h={4} c={stale ? UI.bright : UI.text} />
            <rect x={152} y={38} width="96" height="7" rx="3.5" fill={UI.row} />
            <rect x={152} y={38} width={stale ? 88 : 26 + i * 8} height="7" rx="3.5"
                  fill={stale ? "#e0a33e" : a} />
            <Line x={258} y={39} w={20} h={5} c={stale ? "#e0a33e" : UI.line} />
          </g>
        );
      })}
    </g>
  ),

  /** A list beside the thing you are reading: triage inboxes. */
  inbox: (k, a) => (
    <g key={k}>
      <Panel w={104} />
      <Panel x={188} w={112} />
      {[0, 1, 2, 3].map((i) => (
        <g key={i} transform={`translate(0 ${i * 19})`}>
          <Row x={84} y={36} w={92} c={i === 0 ? UI.row : UI.panel} />
          <circle cx="93" cy="44" r="3.5" fill={i === 0 ? a : UI.line} />
          <Line x={101} y={42} w={62 - i * 8} h={3} c={i === 0 ? UI.bright : UI.text} />
        </g>
      ))}
      <Line x={196} y={40} w={64} h={5} c={UI.bright} />
      {[0, 1, 2, 3].map((i) => (
        <Line key={i} x={196} y={54 + i * 11} w={i === 3 ? 52 : 92} h={3} o={0.5} />
      ))}
      <rect x="196" y="98" width="34" height="9" rx="4.5" fill={a} opacity=".9" />
    </g>
  ),

  /** A month grid with days marked: calendars, change windows. */
  calendar: (k, a) => (
    <g key={k}>
      <Panel />
      {[0, 1, 2, 3, 4, 5, 6].map((c) => (
        <Line key={c} x={90 + c * 29} y={38} w={9} h={3} c={UI.line} />
      ))}
      {[0, 1, 2].map((r) => [0, 1, 2, 3, 4, 5, 6].map((c) => {
        const on = (r === 0 && c === 3) || (r === 1 && c === 5) || (r === 2 && c === 1);
        return <rect key={`${r}-${c}`} x={88 + c * 29} y={48 + r * 20} width="23" height="15"
                     rx="3" fill={on ? a : UI.row} />;
      }))}
    </g>
  ),

  /** Columns of cards: kanban, bug triage. */
  board: (k, a) => (
    <g key={k}>
      {[0, 1, 2].map((c) => (
        <g key={c} transform={`translate(${80 + c * 76} 0)`}>
          <Line x={2} y={32} w={30 - c * 4} h={3} c={UI.bright} o={0.8} />
          <rect x="0" y="40" width="68" height="70" rx="5" fill={UI.panel} />
          {[0, 1, 2].map((r) => (
            (r === 2 && c === 2) ? null : (
              <g key={r} transform={`translate(0 ${r * 21})`}>
                <Row x={5} y={46} w={58} />
                <rect x="9" y="50" width="4" height="4" rx="1"
                      fill={c === 0 && r === 0 ? a : UI.line} />
                <Line x={17} y={51} w={38 - r * 8} h={3} o={0.7} />
              </g>
            )
          ))}
        </g>
      ))}
    </g>
  ),

  /** A numbered path with the current step open: onboarding, runbooks. */
  steps: (k, a) => (
    <g key={k}>
      <Panel />
      {[0, 1, 2, 3].map((i) => (
        <g key={i} transform={`translate(0 ${i * 19})`}>
          <circle cx="94" cy="42" r="6" fill={i <= 1 ? a : UI.row} />
          {i <= 1 ? (
            <path d="M91.5 42 L93.5 44 L96.5 40" stroke="#0d1220" strokeWidth="1.6"
                  fill="none" strokeLinecap="round" strokeLinejoin="round" />
          ) : null}
          {i < 3 ? <line x1="94" y1="49" x2="94" y2="55" stroke={UI.line} strokeWidth="1.5" /> : null}
          <Line x={106} y={40} w={i === 2 ? 96 : 66 - i * 6} h={4}
                c={i === 2 ? UI.bright : UI.text} />
          {i === 2 ? <Line x={106} y={47} w={130} h={3} o={0.45} /> : null}
        </g>
      ))}
    </g>
  ),

  /** Two variants and a winner: pricing experiments, A/B logs. */
  experiments: (k, a) => (
    <g key={k}>
      <Panel />
      {[0, 1, 2].map((i) => (
        <g key={i} transform={`translate(0 ${i * 24})`}>
          <Line x={88} y={40} w={34} h={4} c={UI.text} />
          <rect x={130} y={36} width={i === 0 ? 62 : 40} height="10" rx="5"
                fill={i === 0 ? a : UI.row} />
          <rect x={198} y={36} width={i === 0 ? 34 : 56} height="10" rx="5" fill={UI.row} />
          {i === 0 ? <circle cx="272" cy="41" r="5" fill={a} /> : null}
        </g>
      ))}
    </g>
  ),

  /** Tiles that are up or down: vendor status, service health. */
  statusgrid: (k, a) => (
    <g key={k}>
      {[0, 1, 2, 3, 4, 5].map((i) => {
        const down = i === 4;
        const x = 80 + (i % 3) * 74;
        const y = 34 + Math.floor(i / 3) * 42;
        return (
          <g key={i}>
            <rect x={x} y={y} width="66" height="34" rx="5" fill={UI.panel} />
            <circle cx={x + 11} cy={y + 12} r="4" fill={down ? "#e05c5c" : a} />
            <Line x={x + 20} y={y + 10} w={34 - (i % 3) * 5} h={4} c={UI.text} />
            <Line x={x + 8} y={y + 23} w={48} h={3} o={0.4} />
          </g>
        );
      })}
    </g>
  ),

  /** Dated entries, the newest expanded: decision logs, changelogs. */
  log: (k, a) => (
    <g key={k}>
      <Panel />
      <line x1="98" y1="36" x2="98" y2="106" stroke={UI.line} strokeWidth="1.5" />
      {[0, 1, 2].map((i) => (
        <g key={i} transform={`translate(0 ${i * 25})`}>
          <circle cx="98" cy="42" r={i === 0 ? 5 : 3.5} fill={i === 0 ? a : UI.row} />
          <Line x={110} y={38} w={i === 0 ? 92 : 62} h={4}
                c={i === 0 ? UI.bright : UI.text} />
          {i === 0 ? <Line x={110} y={46} w={140} h={3} o={0.45} /> : null}
          <Line x={252} y={39} w={26} h={3} c={UI.line} />
        </g>
      ))}
    </g>
  ),

  /** A message being composed from entries: changelog to email. */
  email: (k, a) => (
    <g key={k}>
      <Panel />
      <Line x={88} y={38} w={30} h={3} c={UI.line} />
      <Line x={124} y={38} w={74} h={3} c={UI.text} />
      <line x1="88" y1="47" x2="290" y2="47" stroke={UI.line} strokeWidth="1" />
      <Line x={88} y={54} w={110} h={5} c={UI.bright} />
      {[0, 1, 2].map((i) => (
        <Line key={i} x={88} y={66 + i * 9} w={i === 2 ? 120 : 196} h={3} o={0.45} />
      ))}
      <rect x="88" y="94" width="44" height="11" rx="5.5" fill={a} opacity=".92" />
      <Line x={96} y={98} w={28} h={3} c="#0d1220" o={0.55} />
    </g>
  ),

  /** A line climbing with its area filled: coverage, trends. */
  trend: (k, a) => (
    <g key={k}>
      <Panel />
      <path d="M92 96 L120 84 L148 88 L176 68 L204 72 L232 54 L260 46 L288 40 L288 106 L92 106 Z"
            fill={a} opacity=".16" />
      <polyline points="92,96 120,84 148,88 176,68 204,72 232,54 260,46 288,40"
                fill="none" stroke={a} strokeWidth="2.5"
                strokeLinecap="round" strokeLinejoin="round" />
      <circle cx="288" cy="40" r="4" fill={a} />
      <line x1="88" y1="106" x2="292" y2="106" stroke={UI.line} strokeWidth="1.5" />
    </g>
  ),

  /** A table with a countdown on each row: expiries, renewals. */
  expiry: (k, a) => (
    <g key={k}>
      <Panel />
      <Line x={88} y={38} w={40} h={3} c={UI.line} />
      <Line x={172} y={38} w={30} h={3} c={UI.line} />
      <Line x={244} y={38} w={26} h={3} c={UI.line} />
      {[0, 1, 2, 3].map((i) => {
        const soon = i === 0 || i === 2;
        return (
          <g key={i} transform={`translate(0 ${i * 17})`}>
            <Line x={88} y={50} w={64 - i * 6} h={4} c={UI.text} />
            <Line x={172} y={50} w={44} h={3} o={0.5} />
            <rect x={244} y={46} width="30" height="10" rx="5"
                  fill={soon ? "#e0a33e" : a} opacity={soon ? 0.9 : 0.65} />
          </g>
        );
      })}
    </g>
  ),

  /** Code with something underlined: linters, style guides. */
  lint: (k, a) => (
    <g key={k}>
      <Panel />
      {[0, 1, 2, 3, 4].map((i) => (
        <g key={i} transform={`translate(0 ${i * 15})`}>
          <Line x={86} y={40} w={8} h={3} c={UI.line} />
          <Line x={100} y={40} w={[120, 78, 150, 96, 62][i]} h={4}
                c={i === 2 ? UI.bright : UI.text} o={i === 2 ? 1 : 0.7} />
          {i === 2 ? (
            <path d="M100 47 q4 3 8 0 t8 0 t8 0 t8 0" stroke="#e05c5c" strokeWidth="1.6"
                  fill="none" />
          ) : null}
          {i === 2 ? <circle cx="286" cy="42" r="4" fill={a} /> : null}
        </g>
      ))}
    </g>
  ),

  /** People against weeks: rotas, fairness. */
  rota: (k, a) => (
    <g key={k}>
      <Panel />
      {[0, 1, 2, 3].map((r) => (
        <g key={r}>
          <circle cx="90" cy={44 + r * 18} r="4.5" fill={UI.line} />
          <Line x={100} y={42 + r * 18} w={26} h={3.5} c={UI.text} />
          {[0, 1, 2, 3, 4, 5].map((c) => {
            const on = (r + c) % 3 === 0;
            return <rect key={c} x={134 + c * 26} y={38 + r * 18} width="21" height="12"
                         rx="3" fill={on ? a : UI.row} opacity={on ? 0.9 : 1} />;
          })}
        </g>
      ))}
    </g>
  ),
};

/** One layout per idea, by title. Mapping by glyph is what produced seven identical cards. */
const MOCKUP_FOR: Record<string, keyof typeof MOCKUPS> = {
  "Incident timeline builder": "timeline",
  "Subscription audit": "chart",
  "Interview scorecard": "scorecard",
  "Docs freshness board": "freshness",
  "Lead triage inbox": "inbox",
  "Change calendar": "calendar",
  "Bug triage board": "board",
  "Onboarding path": "steps",
  "Pricing experiment log": "experiments",
  "Vendor status watch": "statusgrid",
  "Decision log": "log",
  "Changelog to email": "email",
  "Test coverage trend": "trend",
  "Access expiry tracker": "expiry",
  "Style guide linter": "lint",
  "Rotation fairness board": "rota",
};


const GLYPHS = {
  board: "M3 5h18v14H3zM9 5v14M15 5v14",
  chart: "M4 20V10M10 20V4M16 20v-7M22 20H2",
  clock: "M12 21a9 9 0 1 1 0-18 9 9 0 0 1 0 18zM12 7v5l3 2",
  inbox: "M3 13h5l2 3h4l2-3h5M4 13 6 5h12l2 8v6H4z",
  people: "M8 11a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM2 20a6 6 0 0 1 12 0M17 12a3 3 0 1 0 0-6M15 20h7a5 5 0 0 0-4-4.9",
  shield: "M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6z",
  spark: "M12 3l2.2 6.2L20 11l-5.8 1.8L12 19l-2.2-6.2L4 11l5.8-1.8z",
  route: "M6 4v9a4 4 0 0 0 4 4h8M18 13l3 4-3 4",
  tag: "M3 12l9-9h8v8l-9 9zM16 7h.01",
  book: "M5 4h11a3 3 0 0 1 3 3v13H8a3 3 0 0 0-3 3zM5 4v19",
  map: "M9 4 3 6v14l6-2 6 2 6-2V4l-6 2z M9 4v14M15 6v14",
  bell: "M6 9a6 6 0 1 1 12 0c0 5 2 6 2 6H4s2-1 2-6M10 20a2 2 0 0 0 4 0",
} as const;

/**
 * The cover art, drawn once and shown twice.
 *
 * It lived inline in the card, so when the enlarged view opened there was nothing to show and
 * the operator's first observation was "the image is missing" — the one thing that made the
 * card worth looking at vanished exactly where there was room for it. Lovable's detail page
 * leads with a large preview; ours led with a tag.
 *
 * `scope` keeps the gradient ids distinct, because the card and the full-page view can both be
 * mounted and two elements sharing an id is how one of them silently borrows the other's fill.
 *
 * NO SVG FILTERS. The depth used to come from two circles under `feGaussianBlur stdDeviation
 * 18`, which is a filter primitive: the browser rasterises the filtered region on its own pass,
 * and at the size the detail page renders it — a 320-wide viewBox scaled past 900px — that pass
 * lands a frame or more after everything else. "The image renders late" is exactly what that
 * looks like. Radial gradients produce the same soft orb with no filter graph at all, so the
 * artwork paints with the first frame like any other fill.
 */
function IdeaCover({ idea, scope = "c" }: { idea: Idea; scope?: string }) {
  const slug = `${scope}-${slugFor(idea.title)}`;
  const accent = accentFor(idea.title);
  return (
    // `meet`, NOT `slice`. Slice fills the box and throws away whatever does not fit, so the
    // moment a container's ratio differs from the drawing's 2.5:1 the art is cropped — the
    // detail page's 16/9 frame cut about 29% off the sides, taking the primary button in half
    // and most of the sidebar with it. `meet` fits the whole drawing in; the containers are
    // shaped 320/128 so there is nothing to letterbox, and if one ever drifts the picture
    // shrinks instead of losing a piece.
    <svg className="askboss-idea-shot" viewBox="0 0 320 128"
         preserveAspectRatio="xMidYMid meet">
      {/* THE COLOUR FIELD, the way Living Colors does it: a three-stop gradient across the
          whole area, then two large soft orbs for depth. That app has no image files —
          this IS how it looks good. */}
      <defs>
        <linearGradient id={`g-${slug}`} x1="0" y1="0" x2="1" y2="1">
          <stop offset="0%" stopColor={gradientFor(idea.tag)[0]} />
          <stop offset="52%" stopColor={gradientFor(idea.tag)[1]} />
          <stop offset="100%" stopColor={gradientFor(idea.tag)[2]} />
        </linearGradient>
        {/* The orbs, as gradients rather than blurred shapes — same softness, no filter pass. */}
        <radialGradient id={`o1-${slug}`}>
          <stop offset="0%" stopColor="rgba(255,255,255,.26)" />
          <stop offset="55%" stopColor="rgba(255,255,255,.13)" />
          <stop offset="100%" stopColor="rgba(255,255,255,0)" />
        </radialGradient>
        <radialGradient id={`o2-${slug}`}>
          <stop offset="0%" stopColor="rgba(255,255,255,.22)" />
          <stop offset="55%" stopColor="rgba(255,255,255,.10)" />
          <stop offset="100%" stopColor="rgba(255,255,255,0)" />
        </radialGradient>
      </defs>
      <rect x="0" y="0" width="320" height="128" fill={`url(#g-${slug})`} />
      <circle cx="34" cy="10" r="72" fill={`url(#o1-${slug})`} />
      <circle cx="300" cy="124" r="80" fill={`url(#o2-${slug})`} />
      {/* The sidebar, darkened rather than coloured, so the window still reads as a window
          against the field. */}
      <rect x="0" y="0" width="66" height="128" fill="rgba(10,14,28,.22)" />
      <circle cx="16" cy="16" r="4" fill={accent} />
      <Line x={25} y={14} w={26} h={4} c={UI.bright} o={0.9} />
      {[0, 1, 2, 3, 4].map((n) => (
        <g key={n}>
          <rect x="10" y={34 + n * 16} width="46" height="11" rx="3"
                fill={n === 1 ? UI.row : "transparent"} />
          <rect x="14" y={38 + n * 16} width="4" height="4" rx="1"
                fill={n === 1 ? accent : UI.line} />
          <Line x={22} y={38.5 + n * 16} w={28 - (n % 3) * 5} h={3}
                c={n === 1 ? UI.bright : UI.text} o={n === 1 ? 1 : 0.7} />
        </g>
      ))}
      {/* The content pane's header: a title and one primary button. The accent appears here
          and on exactly one thing below — sparingly, the way a real interface uses colour. */}
      <Line x={78} y={13} w={64} h={5} c={UI.bright} o={0.95} />
      <rect x="258" y="10" width="42" height="12" rx="6" fill={accent} opacity=".92" />
      <Line x={266} y={15} w={26} h={3} c="#0d1220" o={0.55} />
      {(MOCKUPS[MOCKUP_FOR[idea.title] ?? "timeline"])(idea.title, accent)}
    </svg>
  );
}

/** The optional photograph, hidden until it decodes so a missing file is invisible. */
function IdeaPhoto({ title }: { title: string }) {
  return (
    // HIDDEN UNTIL IT LOADS, rather than shown until it fails. `onError` alone leaves a
    // broken-image icon wherever the handler has not run — in a server render, with JS still
    // loading, or for the moment before the request 404s.
    <img className="askboss-idea-photo" src={photoFor(title)} alt=""
         loading="lazy" decoding="async" style={{ display: "none" }}
         onLoad={(e) => { e.currentTarget.style.display = "block"; }}
         onError={(e) => { e.currentTarget.style.display = "none"; }} />
  );
}

const IDEAS: Idea[] = [
  {
    tag: "Ops", title: "Incident timeline builder", glyph: "clock",
    brief: `Build an incident timeline builder.
Who it is for: an on-call engineer writing the post-mortem the morning after.
What it must do:
- Take an alert log and a chat transcript and lay them on one timeline.
- Mark detection, escalation, mitigation and all-clear; compute time-to-detect and time-to-resolve.
- Let a human correct every milestone the tool proposes.
- Export a markdown post-mortem with the timeline embedded.
Done when: a real incident log produces a timeline with all four milestones and both durations, and the exported markdown opens cleanly.
Storage is one JSON file per incident, so it can live in the repo beside the code.`,
  },
  {
    tag: "Money", title: "Subscription audit", glyph: "chart",
    brief: `Build a subscription audit tool.
Who it is for: a founder who suspects they are paying for tools nobody opens.
What it must do:
- Import a card statement CSV and group recurring charges by vendor.
- Show monthly and annual cost, and flag anything whose price rose.
- Let each one be marked keep, cancel or renegotiate.
- Show the running total of what cancelling the marked ones would save.
Done when: a statement with two spellings of one vendor groups into a single row, and the savings total matches the marked rows.
Vendor matching is fuzzy but reviewable: any grouping can be split by hand.`,
  },
  {
    tag: "Team", title: "Interview scorecard", glyph: "people",
    brief: `Build an interview scorecard app.
Who it is for: a hiring manager tired of vague feedback.
What it must do:
- Define a role with its competencies.
- Let each interviewer score independently, hidden from the others until they submit.
- Show the spread and flag where interviewers disagree most.
- Produce one summary per candidate.
Done when: a second interviewer cannot see the first score before submitting, and the summary shows the spread.
Hiding the other scores until you commit is the anti-anchoring point of the tool.`,
  },
  {
    tag: "Content", title: "Docs freshness board", glyph: "book",
    brief: `Build a documentation freshness board.
Who it is for: whoever owns docs nobody trusts any more.
What it must do:
- Read a docs folder from a repository.
- For each page show last edited, the code paths it mentions, and whether those have changed since.
- Sort by most likely to be wrong.
- Let a page be marked reviewed, which resets its clock.
Done when: a page whose referenced code changed ranks above one whose did not, and marking it reviewed moves it down.
Likely-wrong is computed, not a status somebody has to remember to set.`,
  },
  {
    tag: "Sales", title: "Lead triage inbox", glyph: "inbox",
    brief: `Build a lead triage inbox.
Who it is for: a small team drowning in inbound that is mostly noise.
What it must do:
- Take a batch of enquiries and score each on fit against a profile you define.
- Draft a first reply for the good ones and a polite decline for the rest.
- Queue them for one-click send.
- Keep the fit profile as editable text.
Done when: every enquiry gets a score and a draft, and nothing leaves the queue without a click.
Nothing sends without a human pressing send.`,
  },
  {
    tag: "Ops", title: "Change calendar", glyph: "map",
    brief: `Build a change calendar.
Who it is for: a team that keeps deploying into someone else's launch.
What it must do:
- Record planned changes with owner, blast radius and rollback plan.
- Warn when two changes to the same system land on the same day.
- Support a freeze window.
- Show whose change collides with whose.
Done when: two changes to the same system on one day raise a warning, and a freeze cannot be booked over without a recorded reason.
Nobody can book over a freeze without stating a reason, and the reason is kept.`,
  },
  {
    tag: "Quality", title: "Bug triage board", glyph: "board",
    brief: `Build a bug triage board.
Who it is for: whoever runs triage and keeps re-litigating the same tickets.
What it must do:
- Import issues and cluster near-duplicates.
- Propose a severity from the words used.
- Show what has sat untriaged longest.
- Record who decided what, and when.
Done when: near-duplicates appear grouped, no merge happens without confirmation, and every decision has an author and a timestamp.
Clustering proposes; it never merges two issues without somebody saying so.`,
  },
  {
    tag: "Learning", title: "Onboarding path", glyph: "route",
    brief: `Build an onboarding path builder.
Who it is for: the person who explains the same system to every new joiner.
What it must do:
- Compose a path of steps: read this, run that, ship a tiny change.
- Put a checkpoint on each step.
- Show new joiners their progress.
- Show the author where people get stuck.
Done when: a checkpoint fails when its command fails, and the author can see which step stalls people.
A checkpoint is a command that runs, not a box somebody ticks.`,
  },
  {
    tag: "Money", title: "Pricing experiment log", glyph: "tag",
    brief: `Build a pricing experiment log.
Who it is for: a team changing prices and forgetting what they already tried.
What it must do:
- Record what changed, for whom, when, and the expected effect.
- Record the outcome when it lands.
- Warn when an experiment repeats one already run.
- Show the price history of every plan on one line.
Done when: a repeat of a past experiment is flagged before it is saved, and each plan shows its price history in order.
Every entry carries the date the price actually changed, not the date it was decided.`,
  },
  {
    tag: "Ops", title: "Vendor status watch", glyph: "bell",
    brief: `Build a vendor status watcher.
Who it is for: the engineer who finds out from a customer that a dependency is down.
What it must do:
- Track the status pages of the services you depend on.
- Show current state for all of them on one board.
- Keep history, so whether it was us or them is answerable a week later.
- Notify on state changes only.
Done when: a vendor going down and recovering produces exactly two notifications, and the history survives the vendor editing their page.
History is stored locally, because a vendor's own status page is edited after the fact.`,
  },
  {
    tag: "Team", title: "Decision log", glyph: "shield",
    brief: `Build a decision log.
Who it is for: a team that keeps rediscovering why something is the way it is.
What it must do:
- One page per decision: the question, the options, what was chosen, who decided.
- Record what would make us revisit it.
- Make it searchable and linkable from a code comment.
- Keep the template short.
Done when: a decision cannot be saved without a revisit condition, and search finds one by its question.
A decision with no revisit condition is refused at save: that field is the point.`,
  },
  {
    tag: "Growth", title: "Changelog to email", glyph: "spark",
    brief: `Build a changelog-to-email composer.
Who it is for: whoever owes customers a what-is-new note and never sends it.
What it must do:
- Read merged pull requests since the last send.
- Group them into themes and draft a short customer-facing email in plain language.
- Put technical detail in a collapsible section.
- Hold the draft for approval.
Done when: the draft covers every PR since the last send, and no email leaves without approval.
Nothing sends. It drafts and holds; a person presses send.`,
  },
  {
    tag: "Quality", title: "Test coverage trend", glyph: "chart",
    brief: `Build a coverage trend board.
Who it is for: a team whose coverage number moves and nobody knows why.
What it must do:
- Store coverage per commit and chart it.
- Attribute each drop to the change that caused it.
- Highlight files that lost coverage while being modified.
- Show the trend per directory, not only per project.
Done when: a commit that drops coverage is named as the cause, and per-directory trends differ from the project number.
Attribution is per file per commit, so a drop points at a change rather than a sprint.`,
  },
  {
    tag: "Ops", title: "Access expiry tracker", glyph: "shield",
    brief: `Build an access expiry tracker.
Who it is for: whoever grants temporary access that is still live a year later.
What it must do:
- Record each grant with an owner and an expiry.
- Warn before it lapses and list everything overdue.
- Produce the evidence an auditor asks for.
- Require an expiry on every grant.
Done when: a grant cannot be created without an expiry, and everything overdue is visible without navigating.
The overdue list is the home page: a tracker you have to go looking in is a list.`,
  },
  {
    tag: "Content", title: "Style guide linter", glyph: "book",
    brief: `Build a style guide linter for prose.
Who it is for: a team whose docs sound like four different companies.
What it must do:
- Define rules: terms to prefer, phrases to avoid, sentence length, voice.
- Check pages against them and quote the offending sentence.
- Propose a rewrite for each finding.
- Change nothing without a human accepting it.
Done when: each finding quotes its sentence and proposes a rewrite, and no file changes without acceptance.
Rules live in a file in the repo, so changing the house style is a pull request.`,
  },
  {
    tag: "Team", title: "Rotation fairness board", glyph: "people",
    brief: `Build a rotation fairness board.
Who it is for: a team where the same two people always take the weekend.
What it must do:
- Import on-call history and show who covered what.
- Weight each slot by how unsociable it was.
- Propose the next rotation to even it out.
- Show the arithmetic behind the proposal.
Done when: the proposed rotation reduces the spread, and the weights behind it are visible and editable.
Weights are visible and editable: whether a Sunday counts double is a team decision.`,
  },
];

/**
 * Research seeds — the step BEFORE a build.
 *
 * The composer could ask and it could build, so the only way in was to already know what you
 * wanted made. Discovery is the stage for when you do not: it researches a seed on the web and
 * produces a cited dossier plus candidate outcomes, which is where a directive should come
 * from when the idea is not yet formed.
 *
 * Phrased as questions, not products, because that is what separates research from a build.
 */
const RESEARCH: Array<{ label: string; seed: string }> = [
  { label: "On-call tooling",
    seed: "What are teams currently using to manage on-call rotas, and what do they complain about?" },
  { label: "Why internal tools die",
    seed: "Why do small teams abandon their internal tools within a year?" },
  { label: "Crowded categories",
    seed: "Which developer-tooling categories have had the most new entrants in the last year?" },
  { label: "Pricing for small teams",
    seed: "How do incident-management products differentiate on pricing for teams under 20 people?" },
];

const IDEAS_PER_PAGE = 4;

/**
 * POST to one of our own route handlers, with the same stall bound `ask` already had.
 *
 * Research and Build were Server Actions, which re-render the whole route to reply — on a
 * 20-27s console that meant pressing Research showed a spinner for 163 seconds for a call that
 * enqueues in about one. They were also the two paths with NO timeout, so nothing ever ended
 * the spin. Both are fixed here: a route handler returns JSON only, and the wait is bounded.
 */
async function postBounded(
  url: string,
  body: Record<string, unknown>,
): Promise<{ ok: boolean; data?: any; error?: string }> {
  const stalled = Symbol("stalled");
  try {
    const r = await Promise.race([
      fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
        cache: "no-store",
      }),
      new Promise<typeof stalled>((res) =>
        setTimeout(() => res(stalled), STALL_SECONDS * 1000)),
    ]);
    if (r === stalled) {
      return { ok: false,
               error: `No answer after ${STALL_SECONDS}s. It may still be running — nothing was `
                      + "cancelled. Check the area before trying again." };
    }
    const data = await (r as Response).json().catch(() => ({}));
    if (!(r as Response).ok) return { ok: false, error: data?.error ?? `HTTP ${(r as Response).status}` };
    return { ok: true, data };
  } catch (e) {
    return { ok: false, error: String((e as Error)?.message ?? e) };
  }
}

/**
 * The workflow every new project in this project follows.
 *
 * "Build →" does not create a project — it sends the directive down panel → PRD → then code.
 * Threading a per-press workflow choice through a meeting and an approval before it reaches
 * the eventual code task would be real plumbing for a choice nobody makes twice. Setting the
 * project's DEFAULT is one control that governs everything that follows, including whatever
 * the panel produces, and it is what workflow mappings are for.
 *
 * Writes through the `apply` action that already exists — the same one the Workflows area
 * uses — so there is one way a mapping gets created, not two that can disagree.
 */
function DefaultWorkflowPicker({ projectId }: { projectId: string }) {
  const [items, setItems] = useState<Array<{
    id: string; name: string; workflow_key: string; is_catalogue?: boolean }>>([]);
  const [current, setCurrent] = useState("");
  const [saving, setSaving] = useState(false);
  const [note, setNote] = useState("");

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const res = await fetch(
          `/api/delivery-workflows?view=installed&project_id=${encodeURIComponent(projectId)}`,
          { cache: "no-store" });
        if (res.ok && alive) setItems((await res.json()).items ?? []);
      } catch { /* the hero still works without it */ }
    })();
    return () => { alive = false; };
  }, [projectId]);

  const apply = async (key: string) => {
    if (!key) return;
    setSaving(true);
    setNote("");
    try {
      const res = await fetch("/api/delivery-workflows", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        // `project.new` is the work class the catalogue gives Standard Product Delivery, and
        // the first one a code task resolves against.
        body: JSON.stringify({ action: "apply", project_id: projectId,
                               workflow_key: key, work_class: "project.new" }),
      });
      // SAY WHICH WAY IT WENT, AND WHY.
      //
      // `failed (500)` was the whole message for a fault the API could describe exactly — an
      // enabled mapping already occupied the slot — so the operator got a status code for a
      // condition that had a sentence. A bare number sends someone to the logs for something
      // the response already said.
      const body = await res.json().catch(() => ({}));
      if (res.ok) {
        setCurrent(key);
        // "Saved, but X still wins" is a real outcome: an org-scoped mapping or a higher
        // priority can outrank this one, and the only other place that would surface is the
        // next project starting on the wrong workflow.
        setNote(String(body?.note ?? "saved"));
      } else {
        setNote(String(body?.detail ?? body?.error ?? `failed (${res.status})`).slice(0, 140));
      }
    } catch {
      setNote("could not save");
    } finally {
      setSaving(false);
    }
  };

  if (!items.length) return null;
  return (
    <div className="askboss-wf" data-testid="askboss-default-workflow">
      {/* Short. The full sentence lives in the tooltip — a label that is longer than the
          control it names is a label competing with the thing it describes. */}
      <label className="askboss-wf-label" htmlFor="askboss-wf-select"
             title="The workflow every new project in this project will follow">Workflow</label>
      <select id="askboss-wf-select" className="askboss-wf-select" value={current}
              disabled={saving} onChange={(e) => void apply(e.target.value)}>
        <option value="">Standard (default)</option>
        {items.map((w) => (
          <option key={w.id} value={w.workflow_key}>
            {w.name}{w.is_catalogue ? " (standard)" : ""}
          </option>
        ))}
      </select>
      {note ? <span className="askboss-wf-note" data-testid="askboss-wf-note">{note}</span> : null}
    </div>
  );
}

export function AskBossHero({
  projectId,
  actor,
  orgId,
}: {
  projectId: string;
  actor: string;
  orgId?: string;
}) {
  const [input, setInput] = useState("");
  const [reply, setReply] = useState("");
  const [proposed, setProposed] = useState<ProposedAction[]>([]);
  const [threadId, setThreadId] = useState<string | null>(null);
  //: What BOSS is doing right now, and whether something outlived the turn that started it.
  //: Both already existed in the dock; the Command Center is the FIRST place an operator asks
  //: anything, and it was the one place still showing a spinner and a stopwatch.
  const [steps, setSteps] = useState<string[]>([]);
  const [probe, setProbe] = useState<{ id: string } | null>(null);
  const [landing, setLanding] = useState(false);
  //: THE GALLERY. Compared against lovable.dev/templates — 204 templates in a grid — ours were
  //: sixteen cards on a conveyor you cannot scan. "See all" fixes that; facets were tried and
  //: removed, because sixteen items that fit on one screen do not need filtering.
  const [gridMode, setGridMode] = useState(false);
  //: READ BEFORE YOU COMMIT. Clicking a card put a wall of markdown in the composer and
  //: called that a preview. A build costs money; what it must do and how you will know it
  //: worked should be legible before you start one.
  //:
  //: A FULL PAGE, NOT A DIALOG. It was a centred sheet floating over the console, and with a
  //: cover image, a pitch, five feature cards and eight standards in it, it did not fit:
  //: everything below "Designed for" was inside an 88vh scroll box with the dashboard showing
  //: around the edges. Lovable's is a page — its own screen, its own back link, two columns.
  //: Ours is now the same, because that is what this much content needs.
  const [openIdea, setOpenIdea] = useState<Idea | null>(null);
  const [error, setError] = useState("");
  const [elapsed, setElapsed] = useState(0);
  const [ranAction, setRanAction] = useState<string | null>(null);
  const stripRef = useRef<HTMLDivElement>(null);
  const [busy, startTransition] = useTransition();
  const boxRef = useRef<HTMLTextAreaElement>(null);

  // SURVIVE A RE-RENDER OF THE PAGE UNDERNEATH.
  //
  // Server actions that revalidate ("/") re-render the whole console tree, and this panel's
  // answer went with it -- observed live: the reply arrived and vanished in the same frame.
  // askBossAction no longer revalidates, but runAgentActionAction still must (a button click
  // really does change the board), and any future action might. Rather than audit every call
  // site forever, the panel restores its own last answer. sessionStorage, not local: an answer
  // about live state should not outlive the tab that asked.
  const SESSION_KEY = `boss.ask.${projectId}`;
  useEffect(() => {
    try {
      const raw = window.sessionStorage.getItem(SESSION_KEY);
      if (!raw) return;
      const saved = JSON.parse(raw) as {
        reply?: string; proposed?: ProposedAction[]; threadId?: string | null;
      };
      if (saved.reply) setReply(saved.reply);
      if (Array.isArray(saved.proposed)) setProposed(saved.proposed);
      if (saved.threadId) setThreadId(saved.threadId);
    } catch {}
    // Restore once, on mount, for this project.
  }, [SESSION_KEY]);

  useEffect(() => {
    try {
      if (!reply && proposed.length === 0) window.sessionStorage.removeItem(SESSION_KEY);
      else window.sessionStorage.setItem(SESSION_KEY, JSON.stringify({ reply, proposed, threadId }));
    } catch {}
  }, [SESSION_KEY, reply, proposed, threadId]);

  useEffect(() => {
    if (!busy) { setElapsed(0); return; }
    const t0 = Date.now();
    const id = window.setInterval(() => setElapsed(Math.floor((Date.now() - t0) / 1000)), 1000);
    return () => window.clearInterval(id);
  }, [busy]);

  /* THE TEMPLATE PAGE IS A PLACE, so it behaves like one.
   *
   * A full-screen view that the Back button walks straight out of is worse than a dialog: the
   * operator's muscle memory for "get me out of this" would leave the console entirely. It has
   * a URL, Back closes it, and a link to it opens it. `template=` is a query param on the same
   * route, so this is history manipulation, not navigation — nothing re-renders on the server.
   */
  const templateOf = useCallback(
    (slug: string) => IDEAS.find((i) => slugFor(i.title) === slug) ?? null, []);

  useEffect(() => {
    const sync = () => {
      try {
        const slug = new URL(window.location.href).searchParams.get("template") ?? "";
        setOpenIdea(slug ? templateOf(slug) : null);
      } catch { /* non-browser */ }
    };
    sync();                                    // a deep link opens straight onto the template
    window.addEventListener("popstate", sync);
    return () => window.removeEventListener("popstate", sync);
  }, [templateOf]);

  const showTemplate = useCallback((idea: Idea) => {
    setOpenIdea(idea);
    try {
      const url = new URL(window.location.href);
      url.searchParams.set("template", slugFor(idea.title));
      window.history.pushState(null, "", url.toString());
    } catch { /* non-browser */ }
  }, []);

  const closeTemplate = useCallback(() => {
    setOpenIdea(null);
    try {
      const url = new URL(window.location.href);
      if (!url.searchParams.has("template")) return;
      url.searchParams.delete("template");
      // replace, not back(): `back()` would leave the site for anyone who arrived on a link.
      window.history.replaceState(null, "", url.toString());
    } catch { /* non-browser */ }
  }, []);

  /* Escape closes it, and the page underneath does not scroll while it is open — a full-screen
   * view over a scrollable console otherwise scrolls the console when you reach its end. */
  useEffect(() => {
    if (!openIdea) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") closeTemplate(); };
    window.addEventListener("keydown", onKey);
    const previous = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      window.removeEventListener("keydown", onKey);
      document.body.style.overflow = previous;
    };
  }, [openIdea, closeTemplate]);

  const ask = useCallback((text: string) => {
    const message = text.trim();
    if (!message || busy) return;
    setError("");
    setReply("");
    setProposed([]);
    const fd = new FormData();
    fd.set("message", message);
    fd.set("project_id", projectId);
    fd.set("actor", actor);
    if (threadId) fd.set("thread_id", threadId);
    startTransition(async () => {
      // Bound the WAIT, not the work: a turn that outlives STALL is reported, never presented
      // as cancelled, because it may still land server-side.
      const stalled = Symbol("stalled");
      const r = await Promise.race([
        askBossAction(null, fd),
        new Promise<typeof stalled>((res) =>
          window.setTimeout(() => res(stalled), STALL_SECONDS * 1000)),
      ]);
      if (r === stalled) {
        // "ASK AGAIN IN A MOMENT" IS WORK THIS PAGE CAN DO. The turn was not cancelled, its
        // steps are still arriving above, and its answer lands in the thread by itself — so
        // this watches for it. Asking again would start a SECOND turn and pay for it twice.
        setLanding(true);
        setError(
          `Still working — past ${STALL_SECONDS}s. Nothing was cancelled; the answer will ` +
          "appear here on its own when it lands.",
        );
        return;
      }
      if (!r.ok || !r.data) { setError(r.error ?? "request failed"); return; }
      const d = r.data as Record<string, unknown>;
      if (typeof d.thread_id === "string") setThreadId(d.thread_id);
      setProposed(Array.isArray(d.proposed_actions) ? (d.proposed_actions as ProposedAction[]) : []);
      const msgs = Array.isArray(d.messages) ? (d.messages as Array<Record<string, unknown>>) : [];
      const last = [...msgs].reverse().find((m) => m.role === "assistant");
      setReply(String((d.reply as string) ?? (last?.content as string) ?? ""));
    });
  }, [busy, projectId, actor, threadId]);

  // WHAT IT IS DOING, WHILE IT IS DOING IT — the dock's poller, here too.
  //
  // A stopwatch counting toward a stall bound tells the operator only that time is passing. The
  // steps tell them the turn is alive and what it is looking at, which is the difference
  // between waiting and wondering whether to press it again.
  useEffect(() => {
    if (!threadId || (!busy && !landing && !probe)) { if (!busy && !landing) setSteps([]); return; }
    let alive = true;
    let cursor = 0;
    const tick = () => {
      const q = new URLSearchParams({
        view: "progress", project_id: projectId, actor, thread_id: threadId,
        since: String(cursor),
      }).toString();
      fetch(`/api/agent?${q}`, { cache: "no-store" })
        .then((r) => (r.ok ? r.json() : null))
        .then((d: { steps?: { id: number; label: string }[]; cursor?: number;
                    investigation?: { id: string } | null } | null) => {
          if (!alive || !d) return;
          if (Array.isArray(d.steps) && d.steps.length) {
            cursor = Number(d.cursor ?? cursor);
            setSteps((prev) => [...prev, ...d.steps!.map((x) => x.label)].slice(-4));
          }
          setProbe(d.investigation ? { id: d.investigation.id } : null);
        })
        .catch(() => {});
    };
    const id = window.setInterval(tick, 1500);
    tick();
    return () => { alive = false; window.clearInterval(id); };
  }, [busy, landing, probe, threadId, projectId, actor]);

  // WATCH FOR THE ANSWER THAT OUTRAN THE BOUND, rather than telling them to go and look.
  useEffect(() => {
    if (!landing || !threadId) return;
    let alive = true;
    const started = Date.now();
    const id = window.setInterval(() => {
      if (!alive) return;
      if (Date.now() - started > 20 * 60_000) { setLanding(false); return; }
      const q = new URLSearchParams({
        view: "messages", project_id: projectId, actor, thread_id: threadId,
      }).toString();
      fetch(`/api/agent?${q}`, { cache: "no-store" })
        .then((r) => (r.ok ? r.json() : null))
        .then((d: { messages?: Array<Record<string, unknown>> } | null) => {
          if (!alive || !d?.messages?.length) return;
          const last = [...d.messages].reverse().find((m) => m.role === "assistant");
          if (!last) return;
          setReply(String(last.content ?? ""));
          setError("");
          setLanding(false);
        })
        .catch(() => {});
    }, 4000);
    return () => { alive = false; window.clearInterval(id); };
  }, [landing, threadId, projectId, actor]);

  const accept = useCallback((a: ProposedAction) => {
    setRanAction(a.id);
    startTransition(async () => {
      const r = await runAgentActionAction(projectId, actor, a.tool, a.args, threadId ?? undefined);
      const data = r.data as Record<string, unknown> | undefined;
      const ok = r.ok && data?.ok !== false;
      // The Factory card, the Workflows screen and the monitoring panel render the rows this
      // just changed. Same signal the dock sends.
      if (ok) notifyChanged("command-center", { tool: a.tool });
      setReply((prev) =>
        `${prev}\n\n${ok ? "✓" : "⚠"} **${a.label}**${ok ? " — done." : ` failed: ${data?.error ?? r.error ?? "unknown error"}`}`);
      // An offer is spent once taken: leaving it on screen invites a second, duplicate run.
      setProposed((p) => p.filter((x) => x.id !== a.id));
      setRanAction(null);
    });
  }, [projectId, actor, threadId]);

  /**
   * Send the text down the GOVERNED path instead of the chat one.
   *
   * This was a second composer sitting directly underneath ("Assign an outcome to your
   * team…"), so the page opened with two prompts side by side and no way to tell which took
   * what. They are one box now: the same words either get answered or get built, and the
   * operator picks which by pressing a different button rather than by choosing a lane first.
   */
  const assign = useCallback((text: string) => {
    const directive = text.trim();
    if (!directive || busy) return;
    setError(""); setReply(""); setProposed([]);
    startTransition(async () => {
      const r = await postBounded("/api/builds",
                                  { project_id: projectId, directive, actor });
      const d = r.data as Record<string, unknown> | undefined;
      if (!r.ok) {
        // Build used to swallow this entirely: the action returned void, so a failure and a
        // success looked identical — "Working", then nothing, forever.
        setError(r.error ?? "Could not start the build.");
        return;
      }
      // SAY WHAT STARTED. This announced a panel, because that is what used to happen; the
      // press now starts the workflow run, and the run is the thing to go and watch.
      const runId = String((d?.run as Record<string, unknown> | undefined)?.id ?? d?.id ?? "");
      setInput("");
      setReply(
        `**Started.** Taking you to the workflow…\n\n` +
        (runId ? `Run \`${runId}\`.` : ""),
      );
      // GO WHERE THE WORK IS. Telling somebody to "open Workflow" after they pressed Build is
      // asking them to do the navigation the press already implied — and they had no way to
      // know the run had appeared, because the page they were on does not show runs.
      const url = new URL(window.location.href);
      url.searchParams.set("area", "delivery_workflows");
      if (runId) url.searchParams.set("wfr", runId);
      window.history.pushState({}, "", url.toString());
      window.dispatchEvent(new PopStateEvent("popstate"));
      window.dispatchEvent(new CustomEvent("boss:navigate"));
    });
  }, [busy, projectId, actor]);

  /**
   * Put a brief in the composer. Deliberately does NOT build.
   *
   * Clicking a suggestion used to fire a governed build immediately — a click that spends money
   * on a one-line spec nobody had read. It drafts now: the operator edits the brief, then
   * presses Build when they mean it.
   */
  const draftIdea = useCallback((idea: Idea) => {
    setInput(idea.brief);
    setError(""); setReply(""); setProposed([]);
    // Let the textarea size to the brief before the caret lands at the end.
    window.requestAnimationFrame(() => {
      const box = boxRef.current;
      if (!box) return;
      box.focus();
      box.setSelectionRange(box.value.length, box.value.length);
      box.style.height = "auto";
      box.style.height = `${Math.min(box.scrollHeight, 320)}px`;
    });
  }, []);

  // The nine-second auto-rotate is gone: a panel that changes while you are reading it is
  // worse than one that does not, and scrolling shows the whole set without waiting for it.
  /**
   * Start a Discovery run: research first, decide what to build after.
   *
   * Restored after a careless edit removed it along with the idea-paging state it happened to
   * sit next to — the build caught it, which is the argument for typechecking before deploying.
   */
  const research = useCallback((seedText: string) => {
    const seed = seedText.trim();
    if (!seed || busy) return;
    setError(""); setReply(""); setProposed([]);
    startTransition(async () => {
      const r = await postBounded("/api/discoveries", { project_id: projectId, seed, actor });
      if (!r.ok) { setError(r.error ?? "Could not start the research run."); return; }
      setInput("");
      setReply(
        `**Researching.** Discovery run \`${r.data?.run_id ?? ""}\` is gathering sources.\n\n` +
        `It produces a cited dossier and candidate outcomes — watch it under **Discovery**. ` +
        `When something looks worth building, bring it back here and press Build.`,
      );
    });
  }, [busy, projectId, actor]);

  // A SLOW DRIFT THE OPERATOR CAN OVERRIDE.
  //
  // Manual scrolling alone means the cards sit still and most people never learn there are
  // sixteen. The strip drifts continuously instead — slowly enough to read across, not in
  // page-sized jumps, so nothing you are looking at is ever yanked away.
  //
  // It stops the moment attention lands on it: pointer over the strip OR the composer, focus
  // inside either, a turn running, an answer on screen, or any text typed. Motion that
  // competes with what you are doing is worse than no motion.
  const [stripHeld, setStripHeld] = useState(false);
  // Defaults to OFF for anyone who asked the OS for less motion, but it is a VISIBLE toggle
  // rather than a silent gate: drifting was requested, and a requested feature that quietly
  // does nothing because of a system setting is indistinguishable from a bug.
  const [drift, setDrift] = useState<boolean | null>(null);
  useEffect(() => {
    if (drift !== null) return;
    setDrift(!window.matchMedia?.("(prefers-reduced-motion: reduce)").matches);
  }, [drift]);
  useEffect(() => {
    const strip = stripRef.current;
    if (!strip) return;
    if (stripHeld || drift === false || busy || reply || input.trim()) return;
    let raf = 0;
    let last = 0;
    // ACCUMULATE THE SUB-PIXEL REMAINDER.
    //
    // This is why the drift did nothing at all. 14px/s is about 0.23px per frame at 60fps, and
    // scrollLeft is rounded to whole pixels — so `scrollLeft = scrollLeft + 0.23` rounded
    // straight back to the same integer, every frame, forever. The motion has to be banked
    // until it is worth at least one pixel and then applied.
    let carry = 0;
    const PX_PER_SECOND = 16;          // about one card every fifteen seconds
    const step = (t: number) => {
      if (last) {
        const end = strip.scrollWidth - strip.clientWidth;
        if (end > 0) {
          carry += ((t - last) / 1000) * PX_PER_SECOND;
          const whole = Math.floor(carry);
          if (whole >= 1) {
            carry -= whole;
            // Wrap rather than stop: a strip parked at the end looks broken.
            const next = strip.scrollLeft + whole;
            strip.scrollLeft = next >= end ? 0 : next;
          }
        }
      }
      last = t;
      raf = window.requestAnimationFrame(step);
    };
    raf = window.requestAnimationFrame(step);
    return () => window.cancelAnimationFrame(raf);
  }, [stripHeld, drift, busy, reply, input]);

  /**
   * Step the strip by one card.
   *
   * The arrows used scrollBy with behavior:"smooth" while the strip had CSS scroll-snap on it.
   * The two fight: snap pulls the position back mid-animation, so a click appeared to do
   * nothing. Snap is gone from the CSS and this measures a real card instead of a magic 320px.
   */
  const nudge = useCallback((dir: 1 | -1) => {
    const strip = stripRef.current;
    if (!strip) return;
    const card = strip.querySelector<HTMLElement>(".askboss-idea");
    const step = (card?.offsetWidth ?? 240) + 10;
    strip.scrollLeft += dir * step;
  }, []);

  const slow = elapsed >= NORMAL_SECONDS;
  const state = busy ? (slow ? "slow" : "thinking") : reply || error ? "answered" : "idle";

  return (
    <section className="askboss" data-state={state} aria-label="Ask BOSS">
      <div className="askboss-aura" aria-hidden />
      <div className="askboss-inner">
        {/* The console's standard header shape — eyebrow, title, status pills on the right,
            the same one the Meeting room uses. This was a bespoke card with its own type
            scale, which is why it read as a different product bolted onto the page. */}
        <header className="mr-header askboss-head">
          <div className="mr-header-main">
            <span className="eyebrow">BOSS Agent</span>
            <h3 className="mr-title">Your autonomous software factory.</h3>
            {/* The first line on the first page described PLUMBING — "reads live control-plane
                state and acts with your approval" — which tells a visitor how it is wired, not
                what it is for.
                
                It then said "without the meetings", which sold the wrong thing: the governed
                panel IS the product. Agents convene it and it produces the PRD, so naming it as
                friction to avoid undersold the part that makes the output trustworthy. */}
            <p className="askboss-sub">
              Describe an outcome. BOSS agents research it, debate it in a governed panel,
              deliver the PRD, build it and ship it —{" "}
              <strong>with your approval at every gate.</strong>
            </p>
          </div>
          <div className="mr-header-meta">
            <span className={`cap-badge tone-${busy ? "amber" : "green"}`}>
              {busy ? (slow ? "working · slow" : "working") : "ready"}
            </span>
            <span className="mr-meta-item">reads live state</span>
            <span className="mr-meta-item">acts on approval</span>
            {busy ? <span className="mr-meta-item">⏱ {elapsed}s</span> : null}
          </div>
        </header>

        <form
          className="askboss-form"
          onSubmit={(e) => { e.preventDefault(); ask(input); }}
        >
          <div
            className="askboss-box"
            onMouseEnter={() => setStripHeld(true)}
            onMouseLeave={() => setStripHeld(false)}
          >
            <textarea
              ref={boxRef}
              className="askboss-input"
              value={input}
              rows={1}
              disabled={busy}
              placeholder="Ask a question, or describe something to build…"
              onChange={(e) => {
                setInput(e.target.value);
                // A brief is many lines; a fixed one-row box hid all but the first.
                e.target.style.height = "auto";
                e.target.style.height = `${Math.min(e.target.scrollHeight, 320)}px`;
              }}
              // Enter sends; Shift+Enter is a newline. A chat box that needs a mouse to send
              // is not a chat box.
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); ask(input); }
              }}
              data-testid="askboss-input"
            />
            <div className="askboss-actions-inline">
              <button
                className="askboss-send"
                type="submit"
                disabled={busy || !input.trim()}
                data-testid="askboss-send"
                title="Answer this from live control-plane state"
              >
                {busy ? <span className="askboss-spinner" aria-hidden /> : "Ask"}
              </button>
              <button
                className="askboss-assign"
                type="button"
                disabled={busy || !input.trim()}
                data-testid="askboss-research"
                title="Research this first — web sources, cited dossier, candidate outcomes"
                onClick={() => research(input)}
              >
                Research
              </button>
              <button
                className="askboss-assign"
                type="button"
                disabled={busy || !input.trim()}
                data-testid="askboss-assign"
                title="Send this down the governed path — panel, PRD, then code"
                onClick={() => assign(input)}
              >
                Build →
              </button>
            </div>
            <DefaultWorkflowPicker projectId={projectId} />
          </div>

          {!reply && !busy ? (
            <div className="askboss-openers">
              {OPENERS.map((o) => (
                <button
                  key={o.text}
                  type="button"
                  className="askboss-opener"
                  onClick={() => { setInput(o.text); ask(o.text); }}
                >
                  <span className={`mr-avatar tone-${o.tone}`} aria-hidden>{o.badge}</span>
                  {o.text}
                </button>
              ))}
            </div>
          ) : null}

          {!reply && !busy ? (
            <div className="askboss-ideas">
              {/* COMPACT ON PURPOSE. These were four full-width sentences, which is a lot of
                  height for a secondary path and pushed the composer — the thing that should
                  read first — down the panel. The chip carries a short label; the full question
                  goes into the composer on click, where it can be edited before it runs. */}
              <div className="askboss-research-row">
                <span className="askboss-research-label">Not sure yet?</span>
                {RESEARCH.map((r) => (
                  <button
                    key={r.seed}
                    type="button"
                    className="askboss-seed"
                    title={r.seed}
                    onClick={() => {
                      setInput(r.seed);
                      window.requestAnimationFrame(() => {
                        const box = boxRef.current;
                        if (!box) return;
                        box.focus();
                        box.setSelectionRange(box.value.length, box.value.length);
                        box.style.height = "auto";
                        box.style.height = `${Math.min(box.scrollHeight, 320)}px`;
                      });
                    }}
                  >
                    {r.label}
                  </button>
                ))}
                <span className="askboss-research-hint">→ then press Research</span>
              </div>
              {/* A SCROLL STRIP, NOT A SLIDESHOW.
                  These flipped pages on a nine-second timer, which means the one you were
                  reading could leave while you read it, and seeing the whole set required
                  waiting. All sixteen sit in one horizontal strip now: scroll or drag at
                  whatever pace you want, nothing moves on its own, and the arrows are there
                  for a trackpad that will not scroll sideways. */}
              <div className="askboss-ideas-head">
                <span className="askboss-ideas-label">Or start something</span>
                {/* NO FILTERS. Eight chips were added by analogy with Lovable, which has 204
                    templates and genuinely needs them. We have sixteen, all visible in one
                    press of "See all" — so the chips cost a row of screen and a decision to
                    save nobody any scrolling. The operator's judgement, and it is right:
                    filtering is for sets you cannot see the end of. */}
                <div className="askboss-ideas-nav">
                  {/* Sixteen cards you can see at once, or a row you scroll. A gallery is the
                      one you can scan; the row is better when you already know what you want. */}
                  <button type="button" className="askboss-arrow askboss-arrow-wide"
                          data-testid="askboss-grid-toggle"
                          aria-pressed={gridMode}
                          onClick={() => setGridMode((g) => !g)}>
                    {gridMode ? "Row" : "See all"}
                  </button>
                  <button
                    type="button"
                    className="askboss-arrow"
                    aria-label={drift ? "Pause auto-scroll" : "Resume auto-scroll"}
                    title={drift ? "Pause auto-scroll" : "Resume auto-scroll"}
                    onClick={() => setDrift((d) => !d)}
                  >{drift ? "❙❙" : "▶"}</button>
                  <button type="button" className="askboss-arrow" aria-label="Scroll left"
                          onClick={() => nudge(-1)}>←</button>
                  <button type="button" className="askboss-arrow" aria-label="Scroll right"
                          onClick={() => nudge(1)}>→</button>
                </div>
              </div>
              <div
                className="askboss-idea-strip"
                data-grid={gridMode ? "true" : undefined}
                ref={stripRef}
                onMouseEnter={() => setStripHeld(true)}
                onMouseLeave={() => setStripHeld(false)}
                onFocusCapture={() => setStripHeld(true)}
                onBlurCapture={() => setStripHeld(false)}
              >
                {IDEAS.map((i) => (
                  <button
                    key={i.title}
                    type="button"
                    className="askboss-idea"
                    style={{ ["--idea-accent" as string]: accentFor(i.title) }}
                    onClick={() => showTemplate(i)}
                    data-testid={`askboss-idea-${i.tag.toLowerCase()}`}
                  >
                    {/* AN IMAGE, NOT A COLOURED RECTANGLE.
                        Two versions shipped a gradient wash and the operator said, correctly,
                        that a wash is not an image. A starter card should show what the thing
                        would LOOK like, so this is a miniature of the app: window chrome and a
                        layout matching the idea — an inbox is rows, a triage board is columns,
                        an audit is bars, a rotation is a calendar, an incident is a timeline.

                        DRAWN, NOT FETCHED. This console is reachable only over the tailnet, so
                        a card built on a CDN is a broken image the day the CDN is unreachable.
                        SVG costs no request, cannot 404, and is the same every time. */}
                    <span className="askboss-idea-cover" aria-hidden>
                      {/* The app, in miniature. Window chrome and a layout that matches what
                          the idea is — an inbox looks like rows, a board looks like columns —
                          so the card previews the thing rather than tinting a rectangle.
                          Shared with the enlarged view, which is where it was missing. */}
                      <IdeaCover idea={i} scope="card" />
                      <IdeaPhoto title={i.title} />
                      <span className="askboss-idea-mark">
                        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"
                             strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
                          <path d={GLYPHS[i.glyph]} />
                        </svg>
                      </span>
                    </span>
                    <span className="askboss-idea-tag">{i.tag}</span>
                    <span className="askboss-idea-title">{i.title}</span>
                    {/* The first brief line just restates the title ("PR review-time tracker"
                        / "a pull-request review-time tracker"). WHO it is for is the line that
                        actually tells them apart. */}
                    <span className="askboss-idea-hint">
                      {(i.brief.split("Who it is for:")[1] ?? "").split("\n")[0].trim()
                        || i.brief.split("\n")[0].replace(/^Build /, "")}
                    </span>
                    {/* A small overlay, not an expansion. Rendering this inline grew the card
                        to about three times its height and shoved the row around, which is
                        worse than showing nothing. It floats above the card now, so the strip
                        never changes size, and it is capped to two lines. */}
                    <span className="askboss-idea-peek">
                      {(i.brief
                        .split("What it must do:")[1]?.split("Done when:")[0]
                        ?.split("\n").filter((l) => l.trim().startsWith("-"))
                        .slice(0, 2).map((l) => l.replace(/^\s*-\s*/, "").trim())
                        .join(" · ") ?? "").slice(0, 150)}
                    </span>
                  </button>
                ))}
              </div>
              <p className="askboss-ideas-foot">
                Open one to read what it must do and how you will know it worked. Nothing
                builds until you press Build.
              </p>
            </div>
          ) : null}
        </form>

        {/* THE TEMPLATE PAGE.
            Lovable's card opens a PAGE with the template's full story — its own screen, a back
            link, a large preview on the left and a rail on the right carrying the title, the
            description, the button and who it is for. Ours was a centred dialog, and once it
            held a cover image, a pitch, five feature cards and eight standards it stopped
            fitting: everything past "Designed for" lived in an 88vh scroll box with the
            dashboard visible around the edges. The operator's read — "it does not render well
            inline" — is the correct one, and the answer is a page.

            Where this deliberately differs from theirs: they show a screenshot of a finished
            thing. Ours does not exist yet — BOSS builds it, to this specification — so the
            specification IS the preview, and the honest thing to show is the contract the
            build will be held to. */}
        {/* PORTALLED TO THE BODY, and it has to be.
            `.bx-shell` carries `isolation: isolate`, which makes each shell a self-contained
            stacking context — and the console renders MORE THAN ONE of them (Next streams the
            tree into a hidden <div id="S:0"> holding a second complete .bx-shell). Two isolated
            contexts are compared as whole units in DOM order, so a `position: fixed; z-index:
            70` element inside one of them cannot paint above ANYTHING in the other, whatever
            number you give it. Measured live: the project strip covered the template page, its
            own "Back to templates" header included, leaving no way out of a full-screen view
            except navigating elsewhere and returning — which is exactly what the operator
            described having to do.
            Raising the z-index cannot fix that. Leaving the shell can. */}
        {openIdea && typeof document !== "undefined" ? createPortal(
          <div className="tpl-page" data-testid="askboss-sheet" role="dialog" aria-modal="true"
               aria-label={openIdea.title}>
            <header className="tpl-page-head">
              <button type="button" className="tpl-back" aria-label="Close"
                      onClick={closeTemplate}>
                ← Back to templates
              </button>
              <span className="tpl-crumb">Templates · {openIdea.tag}</span>
            </header>

            <div className="tpl-page-inner">
              {/* TWO COLUMNS, the way a detail page is shaped: the thing on the left at the
                  size it deserves, and everything needed to decide on the right — so the
                  button is never below the fold on a wide screen. */}
              <div className="tpl-top">
                <div className="tpl-preview">
                  <div className="tpl-shot" aria-hidden>
                    <IdeaCover idea={openIdea} scope="page" />
                    <IdeaPhoto title={openIdea.title} />
                  </div>
                  <p className="tpl-shot-note">
                    An impression of the app, not a screenshot — this one has not been built
                    yet. What is below is the specification BOSS will be held to.
                  </p>
                </div>

                <aside className="tpl-rail">
                  <span className="askboss-idea-tag">{openIdea.tag}</span>
                  <h1 className="tpl-title">{openIdea.title}</h1>
                  {/* The pitch, in the words somebody deciding would use. Two sentences beat a
                      bullet list for the question "is this the thing I want". */}
                  <p className="tpl-pitch">
                    {DETAILS[openIdea.title]?.pitch ?? facetsOf(openIdea.brief).audience}
                  </p>

                  <div className="tpl-actions">
                    {/* The composer gets the FULL brief — features, screens and quality bar —
                        not the five-bullet version the card was built from. What you read is
                        what is asked for. */}
                    <button type="button" className="tpl-go" data-testid="askboss-sheet-build"
                            onClick={() => {
                              draftIdea({ ...openIdea,
                                          brief: buildBrief(openIdea.title, openIdea.brief) });
                              closeTemplate();
                            }}>
                      Start this build →
                    </button>
                    {/* Reading the brief is not the same as being sure. Asking first costs a
                        conversation; being wrong costs a build. */}
                    <button type="button" className="askboss-ghost"
                            data-testid="askboss-sheet-ask"
                            onClick={() => {
                              setInput(`I am considering this build:\n\n${openIdea.title} — `
                                       + `${facetsOf(openIdea.brief).audience}\n\n`
                                       + "What would you want settled before starting it, and "
                                       + "is there anything here we have already built?");
                              closeTemplate();
                            }}>
                      Ask BOSS about it first
                    </button>
                  </div>
                  <p className="tpl-nothing">
                    Nothing is spent by opening this. Pressing Build drafts it into the
                    composer; you still send it yourself.
                  </p>

                  {DETAILS[openIdea.title]?.designedFor?.length ? (
                    <section className="askboss-sheet-for">
                      <h4>Designed for</h4>
                      <ul>
                        {DETAILS[openIdea.title].designedFor.map((w) => <li key={w}>{w}</li>)}
                      </ul>
                    </section>
                  ) : null}
                </aside>
              </div>

              <div className="askboss-sheet-body">
                {/* THE ANSWER TO "THE CONTENT IS VERY LESS".
                    Five bullets specify an MVP, and an MVP is exactly what BOSS built. These
                    cards are the capability list — and they are not marketing copy: the same
                    text is appended to the brief by `buildBrief`, so every card here is
                    something the build is held to. Reading it and not getting it would be the
                    same broken promise as a green health check over a dead app. */}
                {DETAILS[openIdea.title]?.features?.length ? (
                  <section>
                    <h4>Features it ships with</h4>
                    <div className="askboss-sheet-features" data-testid="askboss-sheet-features">
                      {DETAILS[openIdea.title].features.map((f) => (
                        <div className="askboss-feature" key={f.title}>
                          <strong>{f.title}</strong>
                          <span>{f.detail}</span>
                        </div>
                      ))}
                    </div>
                  </section>
                ) : null}

                <section>
                  <h4>Scope</h4>
                  <ul className="askboss-sheet-list">
                    {facetsOf(openIdea.brief).mustDo.map((line) => <li key={line}>{line}</li>)}
                  </ul>
                  {DETAILS[openIdea.title]?.surfaces?.length ? (
                    <p className="askboss-sheet-screens">
                      <span>Screens</span>
                      {DETAILS[openIdea.title].surfaces.map((s) => (
                        <em key={s}>{s}</em>
                      ))}
                    </p>
                  ) : null}
                </section>

                {/* THE QUALITY BAR. Shared across all sixteen because it is not per-template:
                    an empty state, a keyboard path and a real error message are owed by every
                    one of them, and the first builds shipped without any of the three because
                    nothing in the brief asked. */}
                <section className="askboss-sheet-craft">
                  <h4>Held to this standard</h4>
                  <ul>{CRAFT.map((c) => <li key={c}>{c}</li>)}</ul>
                </section>

                {/* THE ACCEPTANCE TEST, given its own weight. It is the sentence the build is
                    judged against, and it was buried in the middle of a paragraph. */}
                {facetsOf(openIdea.brief).doneWhen ? (
                  <section className="askboss-sheet-done">
                    <h4>Done when</h4>
                    <p>{facetsOf(openIdea.brief).doneWhen}</p>
                  </section>
                ) : null}

                {facetsOf(openIdea.brief).note ? (
                  <p className="askboss-sheet-note">{facetsOf(openIdea.brief).note}</p>
                ) : null}

                {/* WHAT PRESSING BUILD ACTUALLY STARTS. Every other template gallery hands you
                    a finished artefact; this one starts a governed pipeline with your approval
                    at each gate, and somebody about to spend money on it should be told so
                    before they press rather than after. */}
                <section className="askboss-sheet-flow">
                  <h4>What BOSS will do</h4>
                  <ol>
                    <li>Research the idea and cite what it finds</li>
                    <li>Convene a panel of roles to argue the shape of it</li>
                    <li>Write a PRD — <strong>you approve it</strong> before any code</li>
                    <li>Build, review and test it, then open a pull request</li>
                    <li>Deploy it when you approve the release</li>
                  </ol>
                </section>

                {/* The same pair as the rail, repeated at the end. On a narrow screen the rail
                    is above a very long page, and a decision you reach at the bottom should
                    not need a scroll back to the top to act on. */}
                <div className="tpl-actions tpl-actions-end">
                  <button type="button" className="tpl-go"
                          onClick={() => {
                            draftIdea({ ...openIdea,
                                        brief: buildBrief(openIdea.title, openIdea.brief) });
                            closeTemplate();
                          }}>
                    Start this build →
                  </button>
                  <button type="button" className="askboss-ghost" onClick={closeTemplate}>
                    Back to templates
                  </button>
                </div>
              </div>
            </div>
          </div>,
          document.body,
        ) : null}

        {/* WHAT IT IS DOING, NOT HOW LONG IT HAS BEEN DOING IT.
            A stopwatch counting toward a stall bound says only that time is passing, and "43s
            / ~120s · running long" reads as a warning about work that is going exactly to
            plan. The most recent real step is evidence the turn is alive AND tells the
            operator what is being looked at — the dock has had this; the Command Center, which
            is where people ask first, did not. */}
        {busy || landing || probe ? (
          <p className="askboss-status" data-slow={slow && !probe} aria-live="polite"
             data-testid="askboss-status">
            <span className="askboss-status-dot" aria-hidden />
            {steps.length
              ? steps[steps.length - 1]
              : probe
                ? "Debugging — reading the code, the database and the host. A few minutes."
                : landing
                  ? "Still working. The answer will appear here on its own."
                  : slow
                    ? "Still working — it has not been dropped."
                    : "Reading the system and choosing tools…"}
            <span className="askboss-elapsed">
              {/* No stall budget against a debugging session or a turn already past the bound:
                  both are expected to take minutes, and warning about them teaches the operator
                  to ignore the warning on the turn that really has died. */}
              {busy && !landing ? `${elapsed}s${slow ? ` / ~${STALL_SECONDS}s` : ""}` : "working"}
            </span>
          </p>
        ) : null}
        {/* The trail, oldest first — four is enough to show it is chaining without pushing the
            answer off the screen when it arrives. */}
        {(busy || landing) && steps.length > 1 ? (
          <ol className="askboss-steps" data-testid="askboss-steps">
            {steps.slice(0, -1).map((x, i) => <li key={`${x}-${i}`}>{x}</li>)}
          </ol>
        ) : null}

        {error ? (
          <p className="askboss-error" data-testid="askboss-error">⚠ {error}</p>
        ) : null}

        {reply ? (
          <div className="askboss-answer" data-testid="askboss-answer">
            <Markdown text={reply} />
          </div>
        ) : null}

        {proposed.length > 0 ? (
          <div className="askboss-actions" data-testid="askboss-actions">
            <p className="askboss-actions-head">BOSS can do this for you — nothing runs until you choose:</p>
            <div className="askboss-actions-row">
              {proposed.map((a) => (
                <button
                  key={a.id}
                  type="button"
                  className="button agent-proposal-btn"
                  disabled={busy}
                  data-running={ranAction === a.id}
                  title={a.why}
                  onClick={() => accept(a)}
                  data-testid={`askboss-action-${a.tool}`}
                >
                  {ranAction === a.id ? "…" : a.label}
                </button>
              ))}
            </div>
          </div>
        ) : null}

        {reply || error ? (
          <div className="askboss-foot">
            <button type="button" className="askboss-reset" onClick={() => {
              setReply(""); setProposed([]); setError(""); setInput("");
              boxRef.current?.focus();
            }}>
              Ask something else
            </button>
            {/* `org`, NOT `org_id`. The page reads ?org=; a link carrying org_id drops the
                org, the picker appears mid-session, and the stored-org effect bounces you back
                a second later. That flicker was this link. */}
            <a className="askboss-reset"
               href={`/?${orgId ? `org=${encodeURIComponent(orgId)}&` : ""}area=agent`}>
              Open the full agent panel →
            </a>
          </div>
        ) : null}
      </div>
    </section>
  );
}
