/**
 * Pure geometry for the Command Center's graphics (command-center spec §7). Every function here
 * is deterministic and unit-tested without a browser (tests/command-center-graphics.test.tsx).
 *
 * NOTHING HERE ESTIMATES. A line stops at now; a node's place depends only on its team and its
 * role_key, never on what it is doing, so the map never jitters between polls.
 */
import { TEAM_LABELS, TEAM_ORDER, type BoardAgent, type Mission, type TeamSummary } from "./agents-live-board";
import type { CommandCenterView, DayRow, Flow, HourBucket } from "./command-center-view";

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
  // THE RING FILLS THE PANEL: as large as the stage allows, leaving room outside it for the
  // largest face (44px) and the team labels.
  const radius = Math.max(60, Math.min(height / 2 - 58, width / 2 - 104));
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

/** An edge's opacity by how recent it is (§4.7, widened to the six hours the Floor draws): 0.9
 *  under 10 min, falling to 0.18 at 6 h. Computed on the poll, never tweened. */
export const FLOW_HOURS = 6;
export function recencyOpacity(lastAtMs: number, nowMs: number): number {
  const min = Math.max(0, (nowMs - lastAtMs) / 60_000);
  const end = FLOW_HOURS * 60;
  if (min <= 10) return 0.9;
  if (min >= end) return 0.18;
  return round(0.9 - ((min - 10) / (end - 10)) * 0.72);
}

/** An edge's stroke width by volume: 1.25 + log2(n), at most 5. */
export function edgeWidth(n: number): number {
  return Math.min(5, 1.25 + Math.log2(Math.max(1, n)));
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


// ── What each agent is doing, from the board's own facts (§4.7, design pass) ─────────────────

/** How the Floor draws an agent, from real data only:
 *  working  presence says it is working (or in a panel);
 *  waiting  a run waits on you at its hop, or a decision it raised waits (attention.roleKey);
 *  stuck    a failed step is attributed to it;
 *  on_run   it is on the hop chain of a run still in flight, between steps;
 *  recent   its last work ended within FLOW_HOURS;
 *  rested   it has worked before, longer ago;
 *  never    it has never worked (`No work yet`, no `since`). */
export type FloorKind = "working" | "waiting" | "stuck" | "on_run" | "recent" | "rested" | "never";
export const KIND_SIZE: Record<FloorKind, number> = {
  working: 44, waiting: 44, stuck: 40, on_run: 38, recent: 32, rested: 26, never: 20,
};
const WORKING = new Set(["working", "in_panel", "thinking", "speaking"]);
const STOPPED_KINDS = new Set(["failed_step", "gate_override", "budget_refused"]);

/** The runs still in flight: not done, with at least one agent on their chain. */
export function liveMissions(missions: Mission[]): Mission[] {
  return missions.filter((m) => m.state !== "done");
}

/** Each agent's kind (see FloorKind). Same input, same output; tested without a browser. */
export function floorKinds(view: Pick<CommandCenterView, "agents" | "missions" | "attention">, nowMs: number):
    Map<string, FloorKind> {
  const waiting = new Set<string>();
  const onRun = new Set<string>();
  const stoppedOn = new Set<string>();
  for (const m of liveMissions(view.missions)) {
    const hops = m.hops.filter((h): h is Extract<typeof h, { kind: "hop" }> => h.kind === "hop" && !!h.roleKey);
    // A STOPPED run is not running (the RUNNING rule): its last agent is stuck, the rest rest.
    if (m.state === "stopped") { if (hops.length) stoppedOn.add(hops[hops.length - 1].roleKey as string); continue; }
    for (const h of hops) onRun.add(h.roleKey as string);
    if (m.state === "waiting_you" && hops.length) waiting.add(hops[hops.length - 1].roleKey as string);
  }
  for (const a of view.attention) if (a.kind === "approval" && a.roleKey) waiting.add(a.roleKey);
  const stuck = new Set(view.attention.filter((a) => STOPPED_KINDS.has(a.kind) && a.roleKey).map((a) => a.roleKey as string));
  for (const k of stoppedOn) stuck.add(k);
  const out = new Map<string, FloorKind>();
  for (const a of view.agents) {
    const since = Date.parse(a.since ?? "");
    let k: FloorKind;
    if (WORKING.has(a.state)) k = "working";
    else if (a.state === "needs_you" || waiting.has(a.roleKey)) k = "waiting";
    else if (a.state === "stuck" || stuck.has(a.roleKey)) k = "stuck";
    else if (onRun.has(a.roleKey)) k = "on_run";
    else if (Number.isFinite(since) && nowMs - since <= FLOW_HOURS * 3_600_000) k = "recent";
    else if (Number.isFinite(since) || (a.line && a.line !== "No work yet" && a.line !== "Idle")) k = "rested";
    else k = "never";
    out.set(a.roleKey, k);
  }
  return out;
}

/** The footer's counts: working, on a run (waiting or between steps — not working), at rest. */
export function floorCounts(kinds: Map<string, FloorKind>):
    { working: number; onRuns: number; stuck: number; idle: number } {
  let working = 0, onRuns = 0, stuck = 0, idle = 0;
  for (const k of kinds.values()) {
    if (k === "working") working += 1;
    else if (k === "waiting" || k === "on_run") onRuns += 1;
    else if (k === "stuck") stuck += 1;
    else idle += 1;
  }
  return { working, onRuns, stuck, idle };
}

/** A run's chain on the ring: its hop agents in order (repeats collapsed), whether it ends at
 *  You (waiting on you), and its words for the tooltip. */
export type Chain = { runId: string; keys: string[]; toYou: boolean; tone: "accent" | "amber" | "red"; tip: string };
export function chainsOf(missions: Mission[], known: Set<string>): Chain[] {
  const out: Chain[] = [];
  for (const m of liveMissions(missions)) {
    const keys: string[] = [];
    for (const h of m.hops) {
      if (h.kind !== "hop" || !h.roleKey || !known.has(h.roleKey)) continue;
      if (keys[keys.length - 1] !== h.roleKey) keys.push(h.roleKey);
    }
    if (!keys.length) continue;
    const toYou = m.state === "waiting_you";
    const tone = toYou ? "amber" : m.state === "stopped" || m.state === "blocked" ? "red" : "accent";
    const step = m.stepCount ? ` · step ${m.stepIndex} of ${m.stepCount}` : "";
    out.push({ runId: m.runId, keys, toYou, tone, tip: `${m.subject || m.workflowName}${step} · ${m.verb}` });
  }
  return out;
}

// ── The day strip's window (design pass) ──────────────────────────────────────────────────

/** The strip's time window: `fit` zooms to the runs on it (the earliest start in the full
 *  window, less 10% padding, at least an hour, snapped to a nice tick), `full` is the whole
 *  `hours`. Returns [since, now] in ms and 3-6 ticks. */
export function stripWindow(rows: Pick<DayRow, "startedAt">[], nowMs: number, hours: number,
                            mode: "fit" | "full", maxTicks = 6): { since: number; ticks: number[] } {
  const full = nowMs - hours * 3_600_000;
  let since = full;
  if (mode === "fit") {
    const starts = rows.map((r) => Date.parse(r.startedAt)).filter(Number.isFinite).map((t) => Math.max(t, full));
    const first = starts.length ? Math.min(...starts) : full;
    const span = Math.max(3_600_000, (nowMs - first) * 1.1);
    since = Math.max(full, nowMs - span);
  }
  const span = nowMs - since;
  const steps = [5, 10, 15, 30, 60, 120, 180, 240, 360].map((m) => m * 60_000);
  const step = steps.find((s) => span / s <= maxTicks) ?? 360 * 60_000;
  const snapped = mode === "fit" ? Math.max(full, Math.floor(since / step) * step) : since;
  const ticks: number[] = [];
  for (let t = Math.ceil(snapped / step) * step; t <= nowMs; t += step) ticks.push(t);
  // A tick this close to now would print over the `now` label (seen on a phone).
  const room = (nowMs - snapped) * 0.14;
  return { since: snapped, ticks: ticks.filter((t) => nowMs - t >= room).slice(-maxTicks) };
}
