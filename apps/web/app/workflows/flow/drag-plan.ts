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

/** One gap before each step of the order, and one after the last (`at` -1). */
export function gapPoints(order: string[], geometry: LaneGeometry): GapPoint[] {
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
      out.push({ at: k, x: next.x - geometry.conn / 2, y: mid(id) });
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

  const gaps: DropGap[] = gapPoints(order, geometry).map((p) => {
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
