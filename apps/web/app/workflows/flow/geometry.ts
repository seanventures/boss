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

export function nodeWidth(type: StepView["type"] | undefined, density: Density): number {
  return type === "wait" ? WAIT_W[density] : NODE[density].w;
}

/** Positions for every node, column and gap at one density. */
export function laneGeometry(layout: FlowLayout, types: Map<string, StepView["type"]>,
                             density: Density, connector?: number): LaneGeometry {
  const node = NODE[density];
  const conn = connector ?? CONN[density];
  const flagW = FLAG_W[density];
  const columns = layout.columns;
  const colW: number[] = Array.from({ length: columns }, () => 0);
  for (const n of layout.nodes) {
    colW[n.column] = Math.max(colW[n.column] ?? 0, nodeWidth(types.get(n.id), density));
  }
  const rows = Math.max(1, layout.maxRows);
  const pitch = node.h + ROW_GAP;
  const bodyH = rows * node.h + (rows - 1) * ROW_GAP;
  const hasLoop = layout.edges.some((e) => e.kind === "loop");
  const height = PAD_Y * 2 + bodyH + (hasLoop ? LOOP_ROOM : 0);
  const midY = PAD_Y + bodyH / 2;

  // The connector after each column: wider where it carries a branch label.
  const column = new Map(layout.nodes.map((n) => [n.id, n.column]));
  const labelled = new Set(layout.edges
    .filter((e) => e.kind === "yes" || e.kind === "no" || e.kind === "branch")
    .map((e) => column.get(e.from)));
  const gapAfter = Array.from({ length: columns }, (_, c) => conn + (labelled.has(c) ? LABEL_ROOM : 0));
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
    const w = nodeWidth(types.get(n.id), density);
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
                              available: number, forced?: "compact"): LaneGeometry {
  if (forced === "compact") return laneGeometry(layout, types, "compact");
  const comfy = laneGeometry(layout, types, "comfortable");
  if (!(available > 0) || comfy.width <= available) return comfy;
  return laneGeometry(layout, types, "compact");
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
                              available: number): LaneGeometry {
  const full = laneGeometry(layout, types, "comfortable");
  if (!(available > 0) || full.width <= available) return full;
  for (let c = CONN.comfortable - 8; c >= ROOMY_MIN_CONN; c -= 8) {
    const g = laneGeometry(layout, types, "comfortable", c);
    if (g.width <= available) return g;
  }
  return full;
}
