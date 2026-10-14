/**
 * Where a drag can land, and how the lane makes room for it. Pure, so it is tested directly.
 *
 * Indices are positions in `linearOrder(spec)`, the order INSERT and MOVE count in, where every
 * linear stretch is contiguous and each branch is finished before the next starts.
 *
 * LINEAR FLOWS get the full treatment: the dragged node's slot closes up, every gap among the
 * remaining nodes is a target, and the nodes after the chosen gap slide right to open it.
 * Gap k among the remaining nodes is final position k, which is exactly MOVE's `to`.
 *
 * BRANCHED FLOWS keep their drawing still. There is a gap before each step of the order, drawn
 * where that step and the one before it meet (a branch's own stretch included), and the model
 * decides whether a drop there is allowed (`canMove`, or an insert probe). A refused gap shows
 * the grey caret: branch structure is edited in JSON in this release.
 */
import type { FlowLayout } from "../../../lib/workflow-editor-types";
import type { LaneGeometry } from "./geometry";
import type { DragPlan, DragSource, DropGap } from "./use-lane-drag";

/** How far neighbours slide apart to show where a step will land. */
export const SPREAD = 28;
export const END_ID = "__end__";

export type LanePlan = DragPlan & {
  /** The node whose slot closes up, on a move in a linear flow. */
  origin: string | null;
  /** Per element id (and END_ID), how far to translate it with nothing targeted. */
  base: Map<string, number>;
  /** The order the gaps index into: remaining nodes on a linear move, else every node. */
  ids: string[];
  linear: boolean;
};

/** The point between two steps of a branched flow where an insert at `at` would land. */
export type GapPoint = { at: number; x: number; y: number };

/** The arrows a branched flow draws, for putting a `+` on one: the one step an arrow comes into
 *  `id` from, and where `id`'s one way out goes (null: End). Undefined when there is not one. */
export type GapArrows = { into(id: string): string | undefined; out(id: string): string | null | undefined };

/** How far a `+` on an arrow keeps from every step: about a `+` across. */
const PLUS_CLEAR = 20;

/** The middle of the arrow from `a` (a box's right edge) to `b`, when it is longer than a
 *  connector and that middle is clear of every step: there a `+` sits away from both ends, where
 *  a connector-wide gap would put it against a step's edge (the lane draws these arrows as
 *  cubics symmetric about their middle). Null otherwise, and the gap stays where it was. */
function onArrow(a: { x: number; y: number }, b: { x: number; y: number }, geometry: LaneGeometry) {
  if (b.x - a.x <= geometry.conn + 1) return null;
  const m = { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
  for (const box of geometry.boxes.values()) {
    if (m.x > box.x - PLUS_CLEAR && m.x < box.x + box.w + PLUS_CLEAR
        && m.y > box.y - PLUS_CLEAR && m.y < box.y + box.h + PLUS_CLEAR) return null;
  }
  return m;
}

/** The after-gaps (`afterGaps` in the model): right of the step they follow — right of its
 *  loop's body too, for a loop, so "after the loop" is not drawn between it and its body —
 *  or, with `arrows`, on the middle of the arrow out of it when that is longer. */
export function afterGapPoints(ats: number[], order: string[], geometry: LaneGeometry,
                               bodyOf: (id: string) => string | undefined = () => undefined,
                               arrows?: GapArrows): GapPoint[] {
  const out: GapPoint[] = [];
  for (const at of ats) {
    const id = order[-at - 2];
    const b = id ? geometry.boxes.get(id) : undefined;
    if (!b) continue;
    const body = bodyOf(id);
    const bb = body ? geometry.boxes.get(body) : undefined;
    const right = Math.max(b.x + b.w, bb ? bb.x + bb.w : 0);
    const to = arrows?.out(id);
    const tb = to ? geometry.boxes.get(to) : undefined;
    const head = to === null ? { x: geometry.end.x, y: geometry.midY } : tb ? { x: tb.x, y: tb.y + tb.h / 2 } : null;
    const mid = head && !bb ? onArrow({ x: right, y: b.y + b.h / 2 }, head, geometry) : null;
    out.push(mid ? { at, ...mid } : { at, x: right + geometry.conn / 2, y: b.y + b.h / 2 });
  }
  return out;
}

/** One gap before each step of the order, and one after the last (`at` -1). With `arrows`, the
 *  head of a path (whose step before it in the order is on another path) gets its gap on the
 *  middle of the arrow into it, when that arrow is longer than a connector. */
export function gapPoints(order: string[], geometry: LaneGeometry, arrows?: GapArrows): GapPoint[] {
  const box = (id: string) => geometry.boxes.get(id);
  const mid = (id: string) => { const b = box(id)!; return b.y + b.h / 2; };
  const out: GapPoint[] = [];
  order.forEach((id, k) => {
    const next = box(id);
    if (!next) return;
    const prev = k > 0 ? box(order[k - 1]) : undefined;
    if (prev && prev.x + prev.w < next.x) {
      out.push({ at: k, x: (prev.x + prev.w + next.x) / 2, y: (mid(order[k - 1]) + mid(id)) / 2 });
    } else {
      const from = arrows?.into(id);
      const fb = from ? box(from) : undefined;
      const on = fb ? onArrow({ x: fb.x + fb.w, y: fb.y + fb.h / 2 }, { x: next.x, y: mid(id) }, geometry) : null;
      out.push(on ? { at: k, ...on } : { at: k, x: next.x - geometry.conn / 2, y: mid(id) });
    }
  });
  const last = order[order.length - 1];
  if (last && box(last)) {
    const b = box(last)!;
    out.push({ at: -1, x: b.x + b.w + geometry.conn / 2, y: mid(last) });
  }
  return out;
}

export function planDrag(input: {
  layout: FlowLayout;
  geometry: LaneGeometry;
  order: string[];
  source: DragSource;
  canMove(id: string, to: number): boolean;
  canInsert(at: number): boolean;
  /** After-gaps of a branched flow: palette drops only (a move never lands in one). */
  after?: GapPoint[];
}): LanePlan {
  const { layout, geometry, order, source } = input;
  const base = new Map<string, number>();
  const y = geometry.midY;

  if (layout.linear) {
    if (source.kind === "node") {
      const from = order.indexOf(source.id);
      const box = geometry.boxes.get(source.id);
      const collapse = box ? box.w + geometry.conn : 0;
      const ids = order.filter((id) => id !== source.id);
      order.forEach((id, i) => { if (i > from) base.set(id, -collapse); });
      base.set(END_ID, -collapse);
      const gaps: DropGap[] = ids.map((id, g) => {
        const b = geometry.boxes.get(id)!;
        return { x: b.x + (base.get(id) ?? 0) - geometry.conn / 2, y, to: g,
                 allowed: g === from || input.canMove(source.id, g), noop: g === from };
      });
      gaps.push({ x: geometry.end.x - collapse - geometry.conn / 2, y, to: ids.length,
                  allowed: ids.length === from || input.canMove(source.id, ids.length),
                  noop: ids.length === from });
      return { gaps, origin: source.id, base, ids, linear: true };
    }
    const gaps: DropGap[] = order.map((id, g) => ({
      x: geometry.boxes.get(id)!.x - geometry.conn / 2, y, to: g, allowed: input.canInsert(g),
    }));
    gaps.push({ x: geometry.end.x - geometry.conn / 2, y, to: -1, allowed: input.canInsert(-1) });
    return { gaps, origin: null, base, ids: order, linear: true };
  }

  const points = source.kind === "item" ? [...gapPoints(order, geometry), ...(input.after ?? [])]
    : gapPoints(order, geometry);
  const gaps: DropGap[] = points.map((p) => {
    if (source.kind === "item") return { x: p.x, y: p.y, to: p.at, allowed: input.canInsert(p.at) };
    const index = order.indexOf(source.id);
    const k = p.at < 0 ? order.length : p.at;
    const to = k > index ? k - 1 : k;
    const noop = to === index;
    return { x: p.x, y: p.y, to, allowed: noop || input.canMove(source.id, to), noop };
  });
  return { gaps, origin: null, base, ids: order, linear: false };
}

/** The translation of every element while gap `gap` is targeted. */
export function shiftsFor(plan: LanePlan, gap: number | null): Map<string, number> {
  const out = new Map(plan.base);
  if (!plan.linear || gap === null) return out;
  plan.ids.forEach((id, i) => { if (i >= gap) out.set(id, (out.get(id) ?? 0) + SPREAD); });
  out.set(END_ID, (out.get(END_ID) ?? 0) + SPREAD);
  return out;
}

/** Where the caret sits for a gap: centred in the opened connector. */
export function caretX(plan: LanePlan, gap: number): number {
  const g = plan.gaps[gap];
  return plan.linear ? g.x + SPREAD / 2 : g.x;
}
