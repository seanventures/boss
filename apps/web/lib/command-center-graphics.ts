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

/** An edge's opacity by how recent it is (§4.7): 0.9 under 5 min, falling to 0.2 at 60 min.
 *  Computed on the poll, never tweened. `FLOW_HOURS` stays the "recent agent" window. */
export const FLOW_HOURS = 6;
export const FLOW_MINUTES = 60;
export function recencyOpacity(lastAtMs: number, nowMs: number): number {
  const min = Math.max(0, (nowMs - lastAtMs) / 60_000);
  if (min <= 5) return 0.9;
  if (min >= FLOW_MINUTES) return 0.2;
  return round(0.9 - ((min - 5) / (FLOW_MINUTES - 5)) * 0.7);
}

/** An edge's stroke width by volume: thin, 1 + log2(n)/2, at most 2.5 (the map, not a pipe). */
export function edgeWidth(n: number): number {
  return round(Math.min(2.5, 1 + Math.log2(Math.max(1, n)) / 2));
}

// ── Labels that never collide or leave the stage (fix pass D9/D16) ─────────────────────────

export type NameItem = { key: string; x: number; y: number; angle: number; size: number; text: string;
                         /** Lower draws first and wins a collision. */ rank: number };
export type NamePlace = { x: number; y: number; align: "left" | "right" | "center"; w: number; h: number };
type Box = { l: number; t: number; r: number; b: number };
const overlaps = (a: Box, b: Box) => a.l < b.r && b.l < a.r && a.t < b.b && b.t < a.b;

/** Where each active face's name goes: just outside the face, away from the ring's centre,
 *  inside the stage. A name that would overlap a higher-ranked name — or another face — is not
 *  drawn (the face's tooltip and the mini card still say it). Pure; tested. */
export function placeNames(items: NameItem[], stage: { width: number; height: number },
                           charW = 6.4, lineH = 13): Map<string, NamePlace> {
  const out = new Map<string, NamePlace>();
  const faces: (Box & { key: string })[] = items.map((i) => ({
    key: i.key, l: i.x - i.size / 2, t: i.y - i.size / 2, r: i.x + i.size / 2, b: i.y + i.size / 2 }));
  const taken: Box[] = [];
  for (const it of [...items].sort((a, b) => a.rank - b.rank || a.key.localeCompare(b.key))) {
    const w = Math.ceil(it.text.length * charW) + 4, h = lineH;
    const cos = Math.cos(it.angle), sin = Math.sin(it.angle);
    const gap = it.size / 2 + 5;
    const align: NamePlace["align"] = cos > 0.35 ? "left" : cos < -0.35 ? "right" : "center";
    let ax = it.x + cos * gap, ay = it.y + sin * gap;
    // `left` = text starts at ax; `right` = text ends at ax; `center` = centred on ax.
    let l = align === "left" ? ax : align === "right" ? ax - w : ax - w / 2;
    let t = align === "center" ? (sin >= 0 ? ay : ay - h) : ay - h / 2;
    l = Math.max(2, Math.min(stage.width - w - 2, l));
    t = Math.max(2, Math.min(stage.height - h - 2, t));
    const box = { l, t, r: l + w, b: t + h };
    if (taken.some((b) => overlaps(b, box)) || faces.some((f) => f.key !== it.key && overlaps(f, box))) continue;
    taken.push(box);
    ax = align === "left" ? l : align === "right" ? l + w : l + w / 2;
    ay = t + h / 2;
    out.set(it.key, { x: round(ax), y: round(ay), align, w, h });
  }
  return out;
}

/** A team label on the ring, fitted to the stage at any size: it shrinks (to 55%), then slides
 *  inward, and only past that is it shortened with `…` — never cut by the panel's edge. */
/** Short names for the shipped teams, used only when the full name does not fit (re-test 9).
 *  Quality, Media and Cost are short already. The full name is always the arc's <title>. */
export const TEAM_ABBREV: Readonly<Record<string, string>> = {
  architecture: "Arch.", engineering: "Eng.", operations: "Ops", leadership: "Lead.", thinkers: "Think.",
};

/** The smallest a team label is ever drawn: readable, never the 55% it once shrank to. */
export const LABEL_MIN_SCALE = 0.85;

export function fitArcLabel(label: string, x: number, y: number, anchor: "start" | "end" | "middle",
                            stage: { width: number; height: number }, charW = 9.6):
    { x: number; y: number; scale: number; text: string; width: number } {
  const margin = 6;
  const room = anchor === "start" ? stage.width - margin - x : anchor === "end" ? x - margin
    : Math.min(x - margin, stage.width - margin - x) * 2;
  // Full name, shrunk no further than LABEL_MIN_SCALE; else its short name at full size; else
  // the short name slid inward. Never an ellipsis, never unreadably small.
  let text = label;
  let scale = Math.max(LABEL_MIN_SCALE, Math.min(1, room / Math.max(1, label.length * charW)));
  if (label.length * charW * scale > room) {
    const short = TEAM_ABBREV[label.trim().toLowerCase()];
    if (short) { text = short; scale = 1; }
  }
  const width = text.length * charW * scale;
  // Slide in when the label still runs past an edge (a narrow stage).
  let nx = x;
  if (anchor === "start") nx = Math.min(x, stage.width - margin - width);
  else if (anchor === "end") nx = Math.max(x, margin + width);
  else nx = Math.min(Math.max(x, margin + width / 2), stage.width - margin - width / 2);
  const ny = Math.min(Math.max(y, 10), stage.height - 8);
  return { x: round(nx), y: round(ny), scale: round(scale), text, width: round(width) };
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
/** Hop states that mean "this is where the run is now" (MissionRow's currentHopIndex). */
const LANE_LIVE = new Set(["working", "thinking", "speaking", "needs_you", "stuck"]);

export function floorKinds(view: Pick<CommandCenterView, "agents" | "missions" | "attention">, nowMs: number):
    Map<string, FloorKind> {
  const waiting = new Set<string>();
  const onRun = new Set<string>();
  const stoppedOn = new Set<string>();
  const onStep = new Set<string>();
  for (const m of liveMissions(view.missions)) {
    const hops = m.hops.filter((h): h is Extract<typeof h, { kind: "hop" }> => h.kind === "hop" && !!h.roleKey);
    // A STOPPED run is not running (the RUNNING rule): its last agent is stuck, the rest rest.
    if (m.state === "stopped") { if (hops.length) stoppedOn.add(hops[hops.length - 1].roleKey as string); continue; }
    // ONE SOURCE FOR "WORKING" (re-test 10): a lane says "Working · step 2 of 5" from the run's
    // own rows while presence can still say idle (between one act closing and the next opening).
    // The agent on a running run's current hop is working, on the Floor as on the lane.
    if (m.state === "running" && hops.length) {
      const cur = [...hops].reverse().find((h) => LANE_LIVE.has(h.state)) ?? hops[hops.length - 1];
      onStep.add(cur.roleKey as string);
    }
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
    if (WORKING.has(a.state) || onStep.has(a.roleKey)) k = "working";
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

/** The strip's time window: `fit` frames LIVE work first — from the earliest live run's start
 *  (or, with nothing live, the latest three finished runs' earliest start) less 10% padding, at
 *  least an hour, snapped to a nice tick; `full` is the whole `hours`. Older runs on the strip
 *  clip at the left edge (`‹`). Returns `since` in ms and up to `maxTicks` ticks. */
export function stripWindow(rows: (Pick<DayRow, "startedAt"> & { finishedAt?: string | null })[], nowMs: number, hours: number,
                            mode: "fit" | "full", maxTicks = 6): { since: number; ticks: number[]; label: string } {
  const full = nowMs - hours * 3_600_000;
  let since = full;
  if (mode === "fit") {
    // FIT FRAMES EVERY RUN THE STRIP LISTS (re-test 1): from the earliest listed start, so no
    // listed run is a stub at the left edge, with 5% padding and never under half an hour.
    const starts = rows.map((r) => Date.parse(r.startedAt)).filter(Number.isFinite).map((t) => Math.max(t, full));
    const first = starts.length ? Math.min(...starts) : full;
    const span = Math.max(STRIP_MIN_SPAN, (nowMs - first) * 1.05);
    since = Math.max(full, nowMs - span);
  }
  const span = nowMs - since;
  const steps = [5, 10, 15, 30, 60, 120, 180, 240, 360].map((m) => m * 60_000);
  const step = steps.find((s) => span / s <= maxTicks) ?? 360 * 60_000;
  const ticks: number[] = [];
  for (let t = Math.ceil(since / step) * step; t <= nowMs; t += step) ticks.push(t);
  // A tick this close to now would print over the `now` label (seen on a phone); one at the
  // left edge would sit under the lane labels.
  const room = span * 0.14;
  return { since, label: stripLabel(span, hours),
           ticks: ticks.filter((t) => nowMs - t >= room && t - since >= span * 0.04).slice(-maxTicks) };
}

/** Fit never frames less than this: half an hour, so a run that just started is not the whole track. */
export const STRIP_MIN_SPAN = 30 * 60_000;

/** The strip's heading for a window of `spanMs` (re-test 1: it said 24H while showing 40 minutes):
 *  `Last 24h` / `Last 12h` for the whole window, whole hours from 2h (`Last 6h`), else minutes
 *  rounded to 5 (`Last 45m`). */
export function stripLabel(spanMs: number, hours: number): string {
  const mins = spanMs / 60_000;
  if (mins >= hours * 60 - 1) return `Last ${hours}h`;
  if (mins >= 120) return `Last ${Math.min(hours, Math.round(mins / 60))}h`;
  return `Last ${Math.max(5, Math.round(mins / 5) * 5)}m`;
}

/** The heading the strip shows for these rows (the page's section head uses it). */
export function stripHeading(rows: (Pick<DayRow, "startedAt"> & { finishedAt?: string | null })[], nowMs: number,
                             zoom: "fit" | "full", windowHours: number): string {
  return stripWindow(rows, nowMs, windowHours, zoom).label;
}

/** An idle agent is a dot, not a face (spec §4.7): these kinds draw as faces. */
export const FACE_KINDS: ReadonlySet<FloorKind> = new Set<FloorKind>(["working", "waiting", "stuck", "on_run"]);
/** An idle dot's diameter: 10px, 6px past 48 agents (§4.7). */
export function idleDot(total: number): number {
  return total > SMALL_AT ? 6 : 10;
}
