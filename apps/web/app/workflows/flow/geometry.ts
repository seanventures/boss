/**
 * Where everything on the lane sits, computed rather than measured.
 *
 * The lane is drawn from `layoutFlow` (columns and rows) and these constants, so every position
 * a drag, a connector or the minimap needs is a pure function of the spec. Nothing waits for
 * the DOM to lay out, and the drag maths can be tested without a browser.
 */
import type { FlowLayout, StepView } from "../../../lib/workflow-editor-types";

export type Density = "comfortable" | "compact";

export const NODE = {
  /** Three rows that always fit: a 2-line title beside the icon, the agent, the badges. */
  comfortable: { w: 188, h: 116 },
  /** Compact fits more steps by geometry, never by smaller text: a narrower node, as tall as it
   *  needs for the same three rows (a full-width 2-line title, the agent, then the icon and
   *  badges). 104px wide is what lets 11 steps fit a 1262px lane at the readable minimum. */
  compact: { w: 104, h: 100 },
} as const;
/** A Wait step only shows its duration, so it is drawn narrower. */
export const WAIT_W = { comfortable: 96, compact: 80 } as const;
/** The connector between two columns, which also holds the `+` button. Comfortable is the
 *  roomy lane a run is always drawn in (and 100% in the editor): room to read each step as its
 *  own thing, not a row of cards touching (operator, 2026-10-04). */
export const CONN = { comfortable: 56, compact: 16 } as const;
export const ROW_GAP = 16;
export const PAD_X = 20;
export const PAD_Y = 14;
/** The Start flag and the End cap (wider in Compact, whose text is larger). */
export const FLAG_W = { comfortable: 36, compact: 44 } as const;
/** Extra room after a column that fans out, so `yes`, `no` and `parallel` fit on their edges. */
export const LABEL_ROOM = 48;
/** Extra room before End when more than one path ends there. */
export const END_ROOM = 40;
/** Room under the nodes for a loop's back-edge and its badge. */
export const LOOP_ROOM = 26;

/**
 * READABLE TEXT IS A RULE, NOT A HOPE. Fit shrinks the drawing, and with it every word on it, so
 * the smallest text a density draws times the scale Fit picks must never go under MIN_TEXT_REM.
 *
 * MIN_FONT_REM is the smallest font each density sets on the canvas (flow.css; a test reads the
 * stylesheet and holds it to these numbers). The scale floor follows from it: Comfortable text is
 * already at the minimum, so a Comfortable lane is never shrunk; Compact text is set larger, so
 * Compact may shrink until it reaches the minimum and no further.
 */
export const MIN_TEXT_REM = 0.75;
export const MIN_FONT_REM = { comfortable: 0.75, compact: 0.875 } as const;
export function minScale(density: Density): number {
  return Math.min(1, MIN_TEXT_REM / MIN_FONT_REM[density]);
}

export type Box = { x: number; y: number; w: number; h: number };
export type LaneGeometry = {
  density: Density;
  width: number;
  height: number;
  node: { w: number; h: number };
  conn: number;
  /** Left edge and width of each column. */
  colX: number[];
  colW: number[];
  boxes: Map<string, Box>;
  /** Centre x of the connector before column b (b = 0..columns; `columns` is after the last). */
  gapX: number[];
  start: Box;
  end: Box;
  midY: number;
};

/**
 * HOW TALL A NODE IS, FROM THE TYPE IT HOLDS. A node's words are rem and the console's root font
 * grows with the screen (app/layout-scale.css: 19px to 1440px, 22.8px at 1920, 24.7px from
 * 2560), but NODE was measured at a 16px root. At 19px a Compact title's second line already ran
 * into the agent under it, and at 1920 every two-line title did (#551). So the height is the
 * three rows at the root font in use — never less than NODE's — while widths stay as they are
 * (a long title is cut with an ellipsis and is its tooltip; it never runs into another row).
 */
export const BASE_REM = 16;
export function nodeHeight(density: Density, rem: number = BASE_REM): number {
  const line = (size: number, height: number) => size * rem * height;
  const rows = density === "compact"
    // Two title lines (0.875rem × 1.2), the agent (a 20px face or its 0.875rem name), the icon
    // and badges (20px), two 5px gaps, 7px padding top and bottom and the border.
    ? 2 * line(0.875, 1.2) + Math.max(20, line(0.875, 1.25)) + 20 + 2 * 5 + 2 * 7 + 2
    // Two title lines (0.875rem × 1.25), the agent (a 20px face or its 0.75rem name), the badges
    // (16px), two 5px gaps, 10px padding top and bottom and the border.
    : 2 * line(0.875, 1.25) + Math.max(20, line(0.75, 1.25)) + 16 + 2 * 5 + 2 * 10 + 2;
  return Math.max(NODE[density].h, Math.ceil(rows));
}

export function nodeWidth(type: StepView["type"] | undefined, density: Density, wrapW?: number): number {
  // Wrapped, a Wait is as wide as any step: its title ("Attached to an open incident, or not an
  // Error — stop") is read in full like the others.
  if (wrapW) return wrapW;
  return type === "wait" ? WAIT_W[density] : NODE[density].w;
}

/**
 * A FULL PAGE NAMES EVERY STEP IN FULL (operator, 2026-11-01: "Monito…", "Pipeline Orches…").
 * The wrapped lane (FlowLane `wrap`) is Comfortable with the node sized to its words: wide enough
 * for about 20 characters a line at the root font in use (the root grows to 22.8px at 1920, so a
 * fixed 188px held 12), titles wrapping to three lines, a name to two, and never an ellipsis.
 */
export const WRAP_CHARS_REM = 11;
export const WRAP_TITLE_LINES = 3;
export const WRAP_AGENT_LINES = 2;
export function wrapNodeWidth(rem: number = BASE_REM): number {
  return Math.max(NODE.comfortable.w, Math.round(WRAP_CHARS_REM * rem));
}
export function wrapNodeHeight(rem: number = BASE_REM): number {
  const line = (size: number, height: number) => size * rem * height;
  // Three title lines (0.875rem × 1.25), two name lines (0.75rem × 1.25) beside a 20px face, the
  // badges or the run's status (16px), two 5px gaps, 10px padding top and bottom and the border.
  return Math.ceil(WRAP_TITLE_LINES * line(0.875, 1.25) + Math.max(20, WRAP_AGENT_LINES * line(0.75, 1.25))
    + 16 + 2 * 5 + 2 * 10 + 2);
}
/** The smallest scale Fit takes a wrapped lane to; wider than that, it scrolls instead. */
export const WRAP_MIN_SCALE = 0.8;
export function wrapFitScale(natural: number, available: number): number {
  if (!(available > 0) || natural <= available) return 1;
  return Math.max(WRAP_MIN_SCALE, available / natural);
}

/** Positions for every node, column and gap at one density. */
export function laneGeometry(layout: FlowLayout, types: Map<string, StepView["type"]>,
                             density: Density, connector?: number, rem: number = BASE_REM,
                             wrap = false): LaneGeometry {
  const wrapW = wrap ? wrapNodeWidth(rem) : undefined;
  const node = wrap ? { w: wrapW!, h: wrapNodeHeight(rem) } : { w: NODE[density].w, h: nodeHeight(density, rem) };
  const conn = connector ?? CONN[density];
  const flagW = FLAG_W[density];
  const columns = layout.columns;
  const colW: number[] = Array.from({ length: columns }, () => 0);
  for (const n of layout.nodes) {
    colW[n.column] = Math.max(colW[n.column] ?? 0, nodeWidth(types.get(n.id), density, wrapW));
  }
  const rows = Math.max(1, layout.maxRows);
  const pitch = node.h + ROW_GAP;
  const bodyH = rows * node.h + (rows - 1) * ROW_GAP;
  // Error paths may run along the bottom too (back to an earlier step, or under the steps between).
  const hasLoop = layout.edges.some((e) => e.kind === "loop" || e.kind === "error");
  const height = PAD_Y * 2 + bodyH + (hasLoop ? LOOP_ROOM : 0);
  const midY = PAD_Y + bodyH / 2;

  // The connector after each column: wider where it carries a branch label.
  const column = new Map(layout.nodes.map((n) => [n.id, n.column]));
  const labelled = new Set(layout.edges
    .filter((e) => e.kind === "yes" || e.kind === "no" || e.kind === "branch")
    .map((e) => column.get(e.from)));
  const gapAfter = Array.from({ length: columns }, (_, c) => conn + (labelled.has(c) ? LABEL_ROOM : 0));
  // Several ends meet at End: room for their arrows to come together before it, so they never
  // cross each other or End's word on the way in.
  if (columns && layout.terminal.length > 1) gapAfter[columns - 1] += END_ROOM;
  const colX: number[] = [];
  let x = PAD_X + flagW + conn;
  for (let c = 0; c < columns; c += 1) {
    colX.push(x);
    x += (colW[c] || node.w) + gapAfter[c];
  }
  const endX = x;
  const width = endX + flagW + PAD_X;

  const boxes = new Map<string, Box>();
  for (const n of layout.nodes) {
    const w = nodeWidth(types.get(n.id), density, wrapW);
    const cw = colW[n.column] || node.w;
    const offset = (rows - Math.max(1, n.rowsInColumn)) / 2;
    boxes.set(n.id, {
      x: colX[n.column] + (cw - w) / 2,
      y: PAD_Y + (offset + n.row) * pitch,
      w, h: node.h,
    });
  }
  const gapX: number[] = [];
  for (let b = 0; b <= columns; b += 1) {
    const before = b === 0 ? conn : gapAfter[b - 1];
    gapX.push((b < columns ? colX[b] : endX) - before / 2);
  }
  const flagH = 28;
  return {
    density, width, height, node, conn, colX, colW, boxes, gapX, midY,
    start: { x: PAD_X, y: midY - flagH / 2, w: flagW, h: flagH },
    end: { x: endX, y: midY - flagH / 2, w: flagW, h: flagH },
  };
}

/** Comfortable when the whole flow fits at full size, Compact otherwise. */
export function chooseDensity(layout: FlowLayout, types: Map<string, StepView["type"]>,
                              available: number, forced?: "compact", rem: number = BASE_REM): LaneGeometry {
  if (forced === "compact") return laneGeometry(layout, types, "compact", undefined, rem);
  const comfy = laneGeometry(layout, types, "comfortable", undefined, rem);
  if (!(available > 0) || comfy.width <= available) return comfy;
  return laneGeometry(layout, types, "compact", undefined, rem);
}

/** Fit: shrink to the width available, never above 1 and never below the density's floor, so
 *  its smallest text stays at MIN_TEXT_REM or more (see minScale). */
export function fitScale(natural: number, available: number, density: Density): number {
  if (!(available > 0) || natural <= available) return 1;
  return Math.max(minScale(density), available / natural);
}

/** Whether the lane has to scroll at this scale (and so shows the minimap). */
export function overflows(natural: number, scale: number, available: number): boolean {
  return available > 0 && natural * scale > available + 0.5;
}

/** The tightest connector a roomy lane closes to before it scrolls instead. */
export const ROOMY_MIN_CONN = 24;

/**
 * A ROOMY LANE THAT FITS WHEN IT NEARLY DOES. Comfortable nodes at full size; the connectors
 * close from 56px towards 24px when that is all it takes to fit, so a four-step flow 33px too
 * wide does not get a scrollbar and arrows for 33px. Anything wider keeps its full spacing and
 * scrolls inside the lane.
 */
export function roomyGeometry(layout: FlowLayout, types: Map<string, StepView["type"]>,
                              available: number, rem: number = BASE_REM, wrap = false): LaneGeometry {
  const full = laneGeometry(layout, types, "comfortable", undefined, rem, wrap);
  if (!(available > 0) || full.width <= available) return full;
  for (let c = CONN.comfortable - 8; c >= ROOMY_MIN_CONN; c -= 8) {
    const g = laneGeometry(layout, types, "comfortable", c, rem, wrap);
    if (g.width <= available) return g;
  }
  return full;
}

/* ── Steps the operator has moved ─────────────────────────────────────────────────── */

/** One column and one row of the lane, per density: what a stored offset is scaled by. Offsets
 *  are stored in Comfortable units (lib/workflow-layout.ts), so a step moved one column right
 *  in Compact is one column right in Comfortable too. */
function pitch(density: Density): { x: number; y: number } {
  return { x: NODE[density].w + CONN[density], y: NODE[density].h + ROW_GAP };
}

/** A stored (Comfortable) offset in this density's canvas units. */
export function offsetToCanvas(o: { dx: number; dy: number }, density: Density): { dx: number; dy: number } {
  const from = pitch("comfortable");
  const to = pitch(density);
  return { dx: (o.dx * to.x) / from.x, dy: (o.dy * to.y) / from.y };
}

/** A move on this density's canvas, as the offset to store. */
export function offsetFromCanvas(o: { dx: number; dy: number }, density: Density): { dx: number; dy: number } {
  const from = pitch(density);
  const to = pitch("comfortable");
  return { dx: Math.round((o.dx * to.x) / from.x), dy: Math.round((o.dy * to.y) / from.y) };
}

/** The top-left corner a moved box may not cross: inside the padding, never under the Start flag's row. */
export function clampBox(x: number, y: number): { x: number; y: number } {
  return { x: Math.max(PAD_X, x), y: Math.max(PAD_Y / 2, y) };
}

/**
 * The lane with the operator's moves applied: each moved box shifted by its offset (scaled to
 * the density), and the drawing grown to hold them, with End kept to the right of every step.
 * The lane's own positions (`colX`, `gapX`) are unchanged: they are where the grid is.
 */
export function placedGeometry(g: LaneGeometry, offsets: Map<string, { dx: number; dy: number }>): LaneGeometry {
  if (!offsets.size) return g;
  const boxes = new Map(g.boxes);
  for (const [id, raw] of offsets) {
    const box = boxes.get(id);
    if (!box) continue;
    const o = offsetToCanvas(raw, g.density);
    const at = clampBox(box.x + o.dx, box.y + o.dy);
    boxes.set(id, { ...box, x: at.x, y: at.y });
  }
  let right = 0;
  let bottom = 0;
  for (const b of boxes.values()) {
    right = Math.max(right, b.x + b.w);
    bottom = Math.max(bottom, b.y + b.h);
  }
  const endX = Math.max(g.end.x, right + g.conn);
  const width = Math.max(g.width, endX + g.end.w + PAD_X);
  // Room under the lowest step for back-edges (they run along the bottom), as the lane leaves.
  const height = Math.max(g.height, bottom + PAD_Y + LOOP_ROOM);
  return { ...g, boxes, width, height, end: { ...g.end, x: endX } };
}

/* ── Arrows that keep off the steps ───────────────────────────────────────────────── */

export type Point = { x: number; y: number };
/** One piece of an arrow: a straight line or a cubic, each from where the last one ended. */
export type Seg = { line: Point } | { c1: Point; c2: Point; to: Point };
/** An arrow as it is drawn: where it starts and its pieces. Its path (`routePath`), the points
 *  it passes through (`routeSamples`) and where its end handle sits (`routeGrip`) all come from it,
 *  so what is tested is what is drawn. */
export type Route = { from: Point; segs: Seg[] };

/** A point `t` of the way along the cubic a → c1 → c2 → b. */
export function cubicAt(a: Point, c1: Point, c2: Point, b: Point, t: number): Point {
  const u = 1 - t;
  return {
    x: u * u * u * a.x + 3 * u * u * t * c1.x + 3 * u * t * t * c2.x + t * t * t * b.x,
    y: u * u * u * a.y + 3 * u * u * t * c1.y + 3 * u * t * t * c2.y + t * t * t * b.y,
  };
}

const endOf = (s: Seg): Point => ("line" in s ? s.line : s.to);

export function routePath(r: Route): string {
  let d = `M${r.from.x},${r.from.y}`;
  for (const s of r.segs) {
    d += "line" in s ? ` L${s.line.x},${s.line.y}` : ` C${s.c1.x},${s.c1.y} ${s.c2.x},${s.c2.y} ${s.to.x},${s.to.y}`;
  }
  return d;
}

/** Points along the arrow no more than about `step` apart, from its start to its end. */
export function routeSamples(r: Route, step = 4): Point[] {
  const out: Point[] = [r.from];
  let at = r.from;
  for (const s of r.segs) {
    const to = endOf(s);
    const reach = "line" in s ? Math.hypot(to.x - at.x, to.y - at.y)
      : Math.hypot(s.c1.x - at.x, s.c1.y - at.y) + Math.hypot(s.c2.x - s.c1.x, s.c2.y - s.c1.y)
        + Math.hypot(to.x - s.c2.x, to.y - s.c2.y);
    const n = Math.max(1, Math.ceil(reach / step));
    for (let i = 1; i <= n; i += 1) {
      const t = i / n;
      out.push("line" in s ? { x: at.x + (to.x - at.x) * t, y: at.y + (to.y - at.y) * t } : cubicAt(at, s.c1, s.c2, to, t));
    }
    at = to;
  }
  return out;
}

/** Whether a point is inside a box (not on its edge: an arrow starts and ends on one). */
export function insideBox(p: Point, b: Box, margin = 1): boolean {
  return p.x > b.x + margin && p.x < b.x + b.w - margin && p.y > b.y + margin && p.y < b.y + b.h - margin;
}

/** The steps an arrow runs through, other than the ones it joins (`ends`). A step put down over
 *  one of those is not counted: a point inside the arrow's own step is it leaving or arriving,
 *  and a step lying over where the arrow starts or ends cannot be kept out of. */
export function stepsCrossed(r: Route, boxes: Map<string, Box>, ends: Iterable<string>): string[] {
  const skip = new Set(ends);
  const own = [...skip].map((id) => boxes.get(id)).filter((b): b is Box => !!b);
  const pts = routeSamples(r);
  const tips = [pts[0], pts[pts.length - 1]];
  const along = pts.filter((p) => !own.some((b) => insideBox(p, b, -1)));
  return [...boxes].filter(([id, b]) => !skip.has(id) && !tips.some((p) => insideBox(p, b))
    && along.some((p) => insideBox(p, b))).map(([id]) => id);
}

/** Where an arrow's end handle sits: `t` of the way along a single curve, and otherwise on its
 *  last piece, the one that comes into its target, so arrows meeting at one step keep handles
 *  of their own. */
export function routeGrip(r: Route, t: number): Point {
  const last = r.segs[r.segs.length - 1];
  const at = r.segs.length > 1 ? endOf(r.segs[r.segs.length - 2]) : r.from;
  if (!last) return r.from;
  if ("line" in last) return { x: at.x + (last.line.x - at.x) * 0.5, y: at.y + (last.line.y - at.y) * 0.5 };
  return cubicAt(at, last.c1, last.c2, last.to, r.segs.length > 1 ? 0.5 : t);
}

/** How far a forward arrow curves out of its step before it heads for its target. */
export function bendOf(a: Point, b: Point): number {
  return b.x >= a.x + 24 ? Math.max(12, (b.x - a.x) / 2) : Math.max(48, Math.abs(b.x - a.x) / 2);
}

/** The plain curve from the right of one step to the left of another. */
export function curveRoute(a: Point, b: Point): Route {
  const k = bendOf(a, b);
  return { from: a, segs: [{ c1: { x: a.x + k, y: a.y }, c2: { x: b.x - k, y: b.y }, to: b }] };
}

/**
 * AN ARROW GOES ROUND THE STEPS IT WOULD CROSS. A forward arrow is a plain curve from the right of
 * its step to the left of its target. When that curve would run through a step on the way (an
 * arrow that skips a column: a Branch path rejoining the other path further on, a short path
 * ending at End past a longer one), it leaves its step, turns into a clear lane above or below
 * the steps between, runs along it and turns down (or up) into its target. The lane is the
 * nearest clear one: in the gap between two rows when there is one, else over the top row or
 * under the bottom one; of two as near, the side `prefer` names (a Branch's No path goes below,
 * where its label is). A target put down close beside or under its step, where there is no
 * room for a lane, gets a tighter curve instead. `ends` are the steps the arrow joins.
 */
export function forwardRoute(g: Pick<LaneGeometry, "boxes" | "conn" | "height">, a: Point, b: Point,
                             ends: string[], prefer: "above" | "below" = "above"): Route {
  const plain = curveRoute(a, b);
  if (!stepsCrossed(plain, g.boxes, ends).length) return plain;
  const skip = new Set(ends);
  const candidates: Route[] = [];
  if (b.x > a.x + 24) {
    // The turn out of the step and into the target fits the connector beside each.
    const s = Math.max(4, Math.min(16, g.conn / 2));
    const between = [...g.boxes].filter(([id, box]) => !skip.has(id) && box.x < b.x && box.x + box.w > a.x)
      .map(([, box]) => box);
    const lanes = new Set<number>();
    for (const box of between) {
      lanes.add(box.y - ROW_GAP / 2);
      lanes.add(box.y + box.h + ROW_GAP / 2);
    }
    const top = 4;
    const floor = g.height - 4;
    const around = (lane: number): Route => ({ from: a, segs: [
      { c1: { x: a.x + s, y: a.y }, c2: { x: a.x + s, y: lane }, to: { x: a.x + 2 * s, y: lane } },
      { line: { x: b.x - 2 * s, y: lane } },
      { c1: { x: b.x - s, y: lane }, c2: { x: b.x - s, y: b.y }, to: b },
    ] });
    candidates.push(...[...lanes].map((y) => Math.min(floor, Math.max(top, y)))
      .sort((p, q) => (Math.abs(p - a.y) + Math.abs(p - b.y)) - (Math.abs(q - a.y) + Math.abs(q - b.y))
        || (prefer === "above" ? p - q : q - p))
      .map(around));
  }
  // A target put down close beside or under its step: a tighter curve, which bows out less.
  for (const k of [24, 12, 6]) {
    candidates.push({ from: a, segs: [{ c1: { x: a.x + k, y: a.y }, c2: { x: b.x - k, y: b.y }, to: b }] });
  }
  let best = plain;
  let fewest = stepsCrossed(plain, g.boxes, ends).length;
  for (const r of candidates) {
    const hits = stepsCrossed(r, g.boxes, ends).length;
    if (!hits) return r;
    if (hits < fewest) { best = r; fewest = hits; }
  }
  return best;
}
