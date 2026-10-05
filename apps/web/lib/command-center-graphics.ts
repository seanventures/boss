/**
 * Pure geometry for the Command Center's graphics (command-center spec §7). Every function here
 * is deterministic and unit-tested without a browser (tests/command-center-graphics.test.tsx).
 *
 * NOTHING HERE ESTIMATES. A line stops at now; a node's place depends only on its team and its
 * role_key, never on what it is doing, so the map never jitters between polls.
 */
import { TEAM_LABELS, TEAM_ORDER, type BoardAgent, type TeamSummary } from "./agents-live-board";
import type { Flow, HourBucket } from "./command-center-view";

/** Where a node sits on the Floor: centre (x, y) in the stage's own px, plus its angle. */
export type FloorNode = { roleKey: string; team: string; x: number; y: number; angle: number };
export type FloorArc = { team: string; label: string; start: number; end: number; members: number;
                         /** `+n idle` when idle agents are not drawn one by one (> 120 agents). */
                         idleFolded: number };
export type FloorLayout = {
  width: number; height: number; cx: number; cy: number; radius: number;
  arcs: FloorArc[]; nodes: FloorNode[];
  /** Idle dot radius: 5 (10 px dots), 3 past 48 agents. */
  idleR: number;
};

/** Past this many agents idle dots shrink; past FOLD_AT they are not drawn one by one. */
export const SMALL_AT = 48;
export const FOLD_AT = 120;
/** The gap between two team arcs, in radians. */
const GAP = 0.07;

function teamRank(key: string): number {
  const i = TEAM_ORDER.indexOf(key);
  return i < 0 ? TEAM_ORDER.length : i;
}

/** The Floor's map (§4.7 geometry): Leadership at 12 o'clock then the shipped team order
 *  clockwise, arc length ∝ members, members sorted by role_key. Same input, same output. */
export function floorLayout(teams: TeamSummary[], agents: BoardAgent[],
                            size: { width: number; height: number }): FloorLayout {
  const width = Math.max(1, size.width);
  const height = Math.max(1, size.height);
  const cx = width / 2;
  const cy = height / 2;
  // 180px on the 720×440 stage; smaller stages keep room for the arc labels outside the ring.
  const radius = Math.max(60, Math.min(180, height / 2 - 44, width / 2 - 70));
  // Membership is the roster's: each agent sits in its own team (agents[].team). A team named
  // only in teams[] with nobody in it draws no arc.
  const byTeam = new Map<string, string[]>();
  for (const a of agents) {
    const key = a.team || "yours";
    byTeam.set(key, [...(byTeam.get(key) ?? []), a.roleKey]);
  }
  const keys = [...byTeam.keys()].sort((a, b) => teamRank(a) - teamRank(b) || a.localeCompare(b));
  const total = agents.length;
  const idle = new Set(agents.filter((a) => a.state === "idle").map((a) => a.roleKey));
  const gaps = keys.length > 1 ? GAP * keys.length : 0;
  const usable = Math.PI * 2 - gaps;
  const arcs: FloorArc[] = [];
  const nodes: FloorNode[] = [];
  let at = -Math.PI / 2 + (keys.length > 1 ? GAP / 2 : 0);
  for (const key of keys) {
    const members = [...(byTeam.get(key) ?? [])].sort();
    const span = total ? (usable * members.length) / total : 0;
    const label = teams.find((t) => t.key === key)?.label || TEAM_LABELS[key]?.label
      || key.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());
    arcs.push({ team: key, label, start: at, end: at + span, members: members.length,
                idleFolded: total > FOLD_AT ? members.filter((m) => idle.has(m)).length : 0 });
    members.forEach((roleKey, i) => {
      const angle = at + (span * (i + 0.5)) / members.length;
      nodes.push({ roleKey, team: key, angle,
                   x: round(cx + radius * Math.cos(angle)), y: round(cy + radius * Math.sin(angle)) });
    });
    at += span + (keys.length > 1 ? GAP : 0);
  }
  return { width, height, cx, cy, radius, arcs, nodes, idleR: total > SMALL_AT ? 3 : 5 };
}

const round = (n: number) => Math.round(n * 100) / 100;

/** An SVG arc path along the ring from `start` to `end` (radians, clockwise). */
export function arcPath(cx: number, cy: number, r: number, start: number, end: number): string {
  const s = Math.min(end - start, Math.PI * 2 - 0.0001);
  const x0 = cx + r * Math.cos(start), y0 = cy + r * Math.sin(start);
  const x1 = cx + r * Math.cos(start + s), y1 = cy + r * Math.sin(start + s);
  return `M ${round(x0)} ${round(y0)} A ${r} ${r} 0 ${s > Math.PI ? 1 : 0} 1 ${round(x1)} ${round(y1)}`;
}

/** A hand-off's curve: bowed toward the centre, so edges read as passing through the team. */
export function edgeCurve(a: { x: number; y: number }, b: { x: number; y: number },
                          centre: { x: number; y: number }, pull = 0.45):
    { d: string; c: { x: number; y: number } } {
  const mx = (a.x + b.x) / 2, my = (a.y + b.y) / 2;
  let c = { x: mx + (centre.x - mx) * pull, y: my + (centre.y - my) * pull };
  // An edge from the centre itself (You, BOSS) bends sideways instead, or it would be straight.
  if (Math.hypot(mx - centre.x, my - centre.y) < 1 || Math.hypot(a.x - centre.x, a.y - centre.y) < 60) {
    const dx = b.x - a.x, dy = b.y - a.y;
    const len = Math.hypot(dx, dy) || 1;
    c = { x: mx - (dy / len) * len * 0.18, y: my + (dx / len) * len * 0.18 };
  }
  return { d: `M ${round(a.x)} ${round(a.y)} Q ${round(c.x)} ${round(c.y)} ${round(b.x)} ${round(b.y)}`,
           c: { x: round(c.x), y: round(c.y) } };
}

/** The point at t ∈ [0,1] on a quadratic curve (the comet's position; no DOM measuring). */
export function quadAt(a: { x: number; y: number }, c: { x: number; y: number },
                       b: { x: number; y: number }, t: number): { x: number; y: number } {
  const u = 1 - t;
  return { x: u * u * a.x + 2 * u * t * c.x + t * t * b.x, y: u * u * a.y + 2 * u * t * c.y + t * t * b.y };
}

/** An edge's opacity by how recent it is (§4.7): 0.9 under 5 min, falling to 0.25 at 60 min.
 *  Computed on the poll, never tweened. */
export function recencyOpacity(lastAtMs: number, nowMs: number): number {
  const min = Math.max(0, (nowMs - lastAtMs) / 60_000);
  if (min <= 5) return 0.9;
  if (min >= 60) return 0.25;
  return round(0.9 - ((min - 5) / 55) * 0.65);
}

/** An edge's stroke width: 1 + log2(n), at most 4. */
export function edgeWidth(n: number): number {
  return Math.min(4, 1 + Math.log2(Math.max(1, n)));
}

/** Cumulative series for Today (§4.9): `today` stops at now (hour index + fraction), `yesterday`
 *  spans 24h. Both arrays are [hourOffset, cumulative] points. `nowIndex` is the current hour's
 *  bucket (24-47); `frac` how far into it now is (0-1). */
export function cumulative(hours: HourBucket[], nowIndex: number, frac: number,
                           field: "work" | "moneyMicros" | "includedMicros"):
    { today: [number, number][]; yesterday: [number, number][]; todayTotal: number;
      yesterdayByNow: number; yesterdayTotal: number } {
  const v = (i: number) => Number(hours[i]?.[field] ?? 0) || 0;
  const nowH = Math.max(0, Math.min(23, nowIndex - 24));
  const f = Math.max(0, Math.min(1, frac));
  const yesterday: [number, number][] = [[0, 0]];
  let y = 0;
  for (let h = 0; h < 24; h += 1) { y += v(h); yesterday.push([h + 1, y]); }
  const today: [number, number][] = [[0, 0]];
  let t = 0;
  for (let h = 0; h < nowH; h += 1) { t += v(24 + h); today.push([h + 1, t]); }
  // The current hour: what finished in it so far lands at now, never past it.
  t += v(24 + nowH);
  today.push([nowH + f, t]);
  let yByNow = 0;
  for (let h = 0; h < nowH; h += 1) yByNow += v(h);
  yByNow += v(nowH) * f;
  const anything = t > 0 || y > 0;
  return { today: anything ? today : [], yesterday: anything ? yesterday : [], todayTotal: t,
           yesterdayByNow: field === "work" ? Math.round(yByNow) : yByNow, yesterdayTotal: y };
}

/** Which bucket now falls in, and how far into it: from today's first bucket (index 24). */
export function nowPosition(hours: HourBucket[], nowMs: number): { index: number; frac: number } {
  const start = Date.parse(hours[24]?.start ?? "");
  if (!Number.isFinite(start)) return { index: 24, frac: 0 };
  const h = (nowMs - start) / 3_600_000;
  const whole = Math.max(0, Math.min(23, Math.floor(h)));
  return { index: 24 + whole, frac: Math.max(0, Math.min(1, h - whole)) };
}

/** A round axis maximum (1, 2, 5 × 10ⁿ) at or above `n`. */
export function niceMax(n: number): number {
  if (n <= 0) return 1;
  const p = 10 ** Math.floor(Math.log10(n));
  for (const m of [1, 2, 2.5, 5, 10]) if (m * p >= n) return m * p;
  return 10 * p;
}

/** The day strip's x-scale: ms → px over [since, now]. */
export function stripScale(sinceMs: number, nowMs: number, width: number): (ms: number) => number {
  const span = Math.max(1, nowMs - sinceMs);
  return (ms: number) => ((Math.min(Math.max(ms, sinceMs), nowMs) - sinceMs) / span) * width;
}

/** At most this many comets are queued from one poll; the rest only light their edges. */
export const COMET_CAP = 12;

/** Comets to fly (§5.2): the flows whose `newestActId` was not in the previous poll, at most 12
 *  (past that the rest only light their edges). The first poll (prev = null) flies none. */
export function newComets(prev: Flow[] | null, next: Flow[]): Flow[] {
  return freshFlows(prev, next).slice(0, COMET_CAP);
}

/** Every flow whose newest act is new since the previous poll (none on the first). */
export function freshFlows(prev: Flow[] | null, next: Flow[]): Flow[] {
  if (prev === null) return [];
  const seen = new Set(prev.map((f) => f.newestActId));
  return next.filter((f) => f.newestActId && !seen.has(f.newestActId));
}

/** `$4.12`: money in micros, two decimals. */
export function money(micros: number): string {
  return `$${(Math.max(0, micros) / 1_000_000).toFixed(2)}`;
}
