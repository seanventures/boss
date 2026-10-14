/**
 * Where the operator has put a step on the canvas, kept in the spec it describes.
 *
 * THE LANE LAYS ITSELF OUT (app/workflows/flow/geometry.ts: columns from the longest path, rows
 * for siblings). A step dragged off that grid keeps how far it was moved from where the lane
 * would have put it, as an OFFSET, so the flow still reads Start to End when steps are added
 * around it, and a spec with no layout at all draws exactly as it always has.
 *
 *     { "steps": [...], "layout": { "positions": { "review": { "dx": 40, "dy": -64 } } } }
 *
 * BACKWARD COMPATIBLE BY CONSTRUCTION. `layout` is optional and drawing-only: the engine carries
 * every top-level key it does not read through `normalise_workflow_spec` untouched, and nothing
 * that runs a workflow reads it. Offsets are in Comfortable canvas units (geometry.ts NODE /
 * CONN); a Compact lane scales them by its own pitch. A position for a step that is not in the
 * spec is ignored when drawing and dropped by the next edit (`pruneLayout`).
 *
 * Like workflow-graph.ts, every function returns the SAME spec when it changes nothing.
 */
import type { Spec } from "./workflow-editor-types";

export type NodeOffset = { dx: number; dy: number };

type Obj = Record<string, unknown>;
const asObj = (v: unknown): Obj => (v && typeof v === "object" && !Array.isArray(v) ? (v as Obj) : {});

/** How far a step may be moved from its place, either way, in canvas units. Far enough for any
 *  sensible drawing; it keeps a hand-typed or corrupted layout from making a canvas miles wide. */
export const MAX_OFFSET = 4000;

function clean(v: unknown): NodeOffset | null {
  const o = asObj(v);
  const dx = Number(o.dx);
  const dy = Number(o.dy);
  if (!Number.isFinite(dx) || !Number.isFinite(dy)) return null;
  const clamp = (n: number) => Math.max(-MAX_OFFSET, Math.min(MAX_OFFSET, Math.round(n)));
  const out = { dx: clamp(dx), dy: clamp(dy) };
  return out.dx || out.dy ? out : null;
}

/** Every step's offset the spec records, for steps it still has. */
export function layoutOffsets(spec: Spec): Map<string, NodeOffset> {
  const ids = new Set((Array.isArray(spec?.steps) ? spec.steps : []).map((s) => s.id));
  const out = new Map<string, NodeOffset>();
  for (const [id, raw] of Object.entries(asObj(asObj(spec?.layout).positions))) {
    const offset = clean(raw);
    if (offset && ids.has(id)) out.set(id, offset);
  }
  return out;
}

/** Whether any step has been moved off the lane's own layout. */
export function hasPlacedSteps(spec: Spec): boolean {
  return layoutOffsets(spec).size > 0;
}

function withPositions(spec: Spec, positions: Record<string, NodeOffset>): Spec {
  const layout = { ...asObj(spec.layout) };
  if (Object.keys(positions).length) layout.positions = positions;
  else delete layout.positions;
  const next: Spec = { ...spec };
  if (Object.keys(layout).length) next.layout = layout;
  else delete next.layout;
  return next;
}

function positionsOf(spec: Spec): Record<string, NodeOffset> {
  const out: Record<string, NodeOffset> = {};
  for (const [id, offset] of layoutOffsets(spec)) out[id] = offset;
  return out;
}

const same = (a: NodeOffset | undefined, b: NodeOffset | null) =>
  (!a && !b) || (!!a && !!b && a.dx === b.dx && a.dy === b.dy);

/** `id` moved by `offset` from where the lane would put it (null: back where the lane puts it). */
export function placeStep(spec: Spec, id: string, offset: NodeOffset | null): Spec {
  if (!(Array.isArray(spec?.steps) && spec.steps.some((s) => s.id === id))) return spec;
  const positions = positionsOf(spec);
  const wanted = offset ? clean(offset) : null;
  if (same(positions[id], wanted) && !hasStale(spec)) return spec;
  if (wanted) positions[id] = wanted; else delete positions[id];
  return withPositions(spec, positions);
}

/** Every step back where the lane puts it. */
export function clearLayout(spec: Spec): Spec {
  if (!("layout" in spec)) return spec;
  return withPositions(spec, {});
}

function hasStale(spec: Spec): boolean {
  const raw = asObj(asObj(spec?.layout).positions);
  return Object.keys(raw).length !== layoutOffsets(spec).size;
}

/** The layout without positions for steps the spec no longer has (or that say nothing). */
export function pruneLayout(spec: Spec): Spec {
  if (!("layout" in spec) || !hasStale(spec)) return spec;
  return withPositions(spec, positionsOf(spec));
}

/** A renamed step keeps its place. */
export function renameInLayout(spec: Spec, from: string, to: string): Spec {
  const raw = asObj(asObj(spec?.layout).positions);
  if (!(from in raw)) return spec;
  const positions: Record<string, NodeOffset> = {};
  for (const [id, value] of Object.entries(raw)) {
    const offset = clean(value);
    if (offset) positions[id === from ? to : id] = offset;
  }
  return pruneLayout(withPositions(spec, positions));
}
