/**
 * Pure geometry for the Command Center's graphics (command-center spec §7). Owned by stream S2.
 * Every function here is deterministic and unit-tested without a browser.
 *
 * PHASE 0 STUB: the signatures are frozen; S2 fills the bodies.
 */
import type { BoardAgent, TeamSummary } from "./agents-live-board";
import type { Flow, HourBucket } from "./command-center-view";

/** Where a node sits on the Floor: centre (x, y) in the stage's own px, plus its arc. */
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

/** The Floor's map (§4.7 geometry): Leadership at 12 o'clock then the shipped team order
 *  clockwise, arc length ∝ members, members sorted by role_key. Same input, same output. */
export function floorLayout(teams: TeamSummary[], agents: BoardAgent[],
                            size: { width: number; height: number }): FloorLayout {
  void teams; void agents;
  return { width: size.width, height: size.height, cx: size.width / 2, cy: size.height / 2,
           radius: 180, arcs: [], nodes: [], idleR: 5 };
}

/** Cumulative series for Today (§4.9): `today` stops at now (hour index + fraction), `yesterday`
 *  spans 24h. Both arrays are [hourOffset, cumulative] points. */
export function cumulative(hours: HourBucket[], nowIndex: number, frac: number,
                           field: "work" | "moneyMicros" | "includedMicros"):
    { today: [number, number][]; yesterday: [number, number][]; todayTotal: number;
      yesterdayByNow: number; yesterdayTotal: number } {
  void hours; void nowIndex; void frac; void field;
  return { today: [], yesterday: [], todayTotal: 0, yesterdayByNow: 0, yesterdayTotal: 0 };
}

/** The day strip's x-scale: ms → px over [since, now]. */
export function stripScale(sinceMs: number, nowMs: number, width: number): (ms: number) => number {
  const span = Math.max(1, nowMs - sinceMs);
  return (ms: number) => ((Math.min(Math.max(ms, sinceMs), nowMs) - sinceMs) / span) * width;
}

/** Comets to fly (§5.2): the flows whose `newestActId` was not in the previous poll, at most 12
 *  (past that the rest only light their edges). The first poll (prev = null) flies none. */
export function newComets(prev: Flow[] | null, next: Flow[]): Flow[] {
  void prev; void next;
  return [];
}
