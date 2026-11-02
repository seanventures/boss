"use client";

/**
 * THE WHOLE FLOW IN ONE VIEW: one horizontal lane, Start to End, that never wraps.
 *
 * The same component draws a workflow being designed, a run in progress and the compact
 * preview above the JSON. Columns come from `layoutFlow` (the longest path from the entry over
 * every edge), siblings in a column stack and centre, and every position is computed in
 * `geometry.ts`, never measured.
 *
 * It fits itself: Comfortable nodes when the flow fits, Compact otherwise, and in Fit mode the
 * drawing scales down (never so far that its smallest text drops under 0.75rem, see
 * `minScale`) before it scrolls. When it still has to scroll, it scrolls inside itself only, and
 * a minimap appears beneath it.
 *
 * The selected step is always in view. From 1280px the inspector is a column beside the lane, so
 * the lane simply refits into the width left. Under 1280px the drawer floats over the lane's
 * right edge (`occludedRight`); the lane then gains room to scroll the selected step out from
 * under it.
 *
 * In design mode it is also the drop target for drags (`use-lane-drag.ts`) and handles the
 * flow's own keys: arrows move focus along the flow and between siblings, Alt+arrows move the
 * step. Moves are announced in a live region.
 *
 * STEPS GO WHERE THEY ARE PUT. A step let go on a connector is reordered (above); let go
 * anywhere else it stays where it was put, and its arrows follow it, live while it is dragged.
 * Where it was put is kept in the spec as an offset from its place on the grid
 * (lib/workflow-layout.ts), so it survives a save and a reload; Alt+Shift+arrows nudge it the
 * same way from the keyboard, and Tidy puts every step back. A flow with a moved step is drawn
 * with curved arrows, as a branched one is, since its connectors no longer sit in a row.
 *
 * ARROWS GO WHERE THEY ARE PUT TOO. An arrow's end has a handle (on hover, and on every arrow out
 * of the selected step); dragged onto another step, End or Start, the lane asks `onRewire` for
 * that link, and the editor makes it as one undoable edit or says why not
 * (lib/workflow-rewire.ts). From the keyboard the handle is a button that opens the list of
 * where the arrow can go. A straight flow draws no arrows to grab: any link but the one it has
 * would leave a step behind or go round in a circle.
 */
import React, { useCallback, useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from "react";

import type {
  FlowLayout, FlowLink, GraphEdge, Palette, PaletteItem, Problem, Spec, SpecStep, StepView,
} from "../../../lib/workflow-editor-types";
import {
  afterGaps, bodiesOf, canMove as modelCanMove, insertStep, layoutFlow, linearOrder, REPEATS, rewireLink, START_TARGET,
} from "../../../lib/workflow-graph";
import { pinTip, unpinTip, useTips } from "./tip-layer";
import { layoutOffsets, type NodeOffset } from "../../../lib/workflow-layout";
import {
  afterGapPoints, caretX, END_ID, gapPoints, planDrag, shiftsFor, type GapArrows, type LanePlan,
} from "./drag-plan";
import {
  BASE_REM, chooseDensity, clampBox, cubicAt, fitScale, forwardRoute, offsetFromCanvas, overflows, placedGeometry,
  ROW_GAP, roomyGeometry, routeGrip, routePath, routeSamples, stepsCrossed,
  type LaneGeometry, type Point, type Route,
} from "./geometry";
import { Minimap } from "./minimap";
import { nodeDetail, viewOf } from "./node-facts";
import type { LaneAgent } from "../../agents/chain-model";
import { StepNode, type RunPhase } from "./step-node";
import { DRAG_THRESHOLD, GAP_SNAP, useLaneDrag, type DragSource, type DropGap, type LaneTarget } from "./use-lane-drag";

export type LaneRunState = { phase: string; meta: string; tip?: string;
  /** The engine recorded that this step failed into its error path (`output.error_path_taken`). */
  errorPath?: boolean;
  /** Who ran it, once the work is recorded (Agent World: face, live ring, how-glyph). */
  agent?: LaneAgent };
export type LaneZoom = "fit" | "full";

const RUN_PHASES = new Set<RunPhase>(["done", "moving", "needs_you", "failed", "future", "skipped"]);
const PROBE: SpecStep = { id: "__wf_probe__", kind: "capability", binding: {} };
const BRANCH_TIP = "Edit branches in JSON";
/** A step put down this close (canvas units) to its place on the grid goes back to it. */
const HOME_SNAP = 8;
/** How far one Alt+Shift+arrow nudges a step (Comfortable canvas units). */
const NUDGE = 16;

function runPhase(value: string | undefined): RunPhase {
  return value && RUN_PHASES.has(value as RunPhase) ? (value as RunPhase) : "future";
}

/** Where the work has flowed to, for the connector into a node on a run. A step that failed
 *  and was gone past (or skipped) still lets the line continue: the run carried on. */
export function flowInto(previous: RunPhase | "start", next: RunPhase): "past" | "moving" | "future" {
  const passed = previous === "start" || previous === "done" || previous === "skipped" || previous === "failed";
  if (!passed) return "future";
  return next === "moving" || next === "needs_you" ? "moving" : next === "future" ? "future" : "past";
}

/* ── Edges for a branched flow ─────────────────────────────────────────────────── */

type Drawn = { key: string; d: string; kind: GraphEdge["kind"] | "start" | "end" | "back";
               label?: { x: number; y: number; text: string; anchor?: "start" | "middle" | "end" };
               /** The arrow as the spec says it, when it is one the operator can point elsewhere:
                *  `from` where it leaves, `grip` where its end handle sits (near its head). */
               link?: FlowLink; from?: Point; grip?: Point;
               /** What `d` draws (geometry.ts), so where it runs can be checked against the steps. */
               route: Route; ends: string[] };

/** Arrows whose end can be dragged to another step. A Repeat's step and a path into its join
 *  are their block's own (lib/workflow-graph.ts `rewireLink` says why if asked). */
const RELINKABLE = new Set<Drawn["kind"]>(["next", "yes", "no", "branch", "end", "error"]);
/** The picker's value for End (for an error path: Stop the run). */
const END_VALUE = "__end__";

/** How far along its arrow an end handle sits: off the arrowhead, so arrows that meet at one
 *  step (or at End) keep handles of their own. */
const GRIP_T = 0.8;

const drawn = (r: Route) => ({ route: r, d: routePath(r) });

/** Every arrow of a branched flow, each as the route it is drawn along (geometry.ts): forward
 *  arrows go round any step between their ends (`forwardRoute`), ways back run along the bottom. */
export function drawEdges(layout: FlowLayout, g: LaneGeometry, spec: Spec): Drawn[] {
  const col = new Map(layout.nodes.map((n) => [n.id, n.column]));
  const out: Drawn[] = [];
  const right = (id: string) => { const b = g.boxes.get(id)!; return { x: b.x + b.w, y: b.y + b.h / 2 }; };
  const left = (id: string) => { const b = g.boxes.get(id)!; return { x: b.x, y: b.y + b.h / 2 }; };
  const bottom = g.height - 8;
  // What a body's way back says: how many times a Repeat may go round; that a For each goes
  // round once per item.
  const loopBadge = new Map<string, string>(spec.steps.flatMap((s): [string, string][] => {
    if (s.kind === "bounded_loop") return Number(s.max_iterations) ? [[s.id, `↻ ×${Number(s.max_iterations)}`]] : [];
    return s.kind === "for_each" ? [[s.id, "↻ each item"]] : [];
  }));
  const tails = new Map(spec.steps.map((s) => [s.id, bodiesOf(s).slice(-1)[0]] as const)
    .filter((x): x is readonly [string, string] => !!x[1]));
  const labelled = new Set<string>();
  // A For each's arrow into the step it runs can be pointed at another step; a Repeat's cannot.
  const forEach = new Set(spec.steps.filter((s) => s.kind === "for_each").map((s) => s.id));
  const relinkable = (e: GraphEdge) => RELINKABLE.has(e.kind) || (e.kind === "loop" && forEach.has(e.from));
  const backs = new Set(layout.edges.filter((e) => e.kind !== "error" && (col.get(e.to) ?? 0) <= (col.get(e.from) ?? 0))
    .map((e) => `${e.from}>${e.to}`));

  for (const n of layout.nodes.filter((x) => x.column === 0)) {
    out.push({ key: `start>${n.id}`, kind: "start", ends: [n.id],
               ...drawn(forwardRoute(g, { x: g.start.x + g.start.w, y: g.midY }, left(n.id), [n.id])) });
  }
  const back = (from: string, to: string, badge: string, link?: FlowLink) => {
    const a = g.boxes.get(from)!;
    const b = g.boxes.get(to)!;
    const ax = a.x + a.w / 2;
    const bx = b.x + b.w / 2;
    const route: Route = { from: { x: ax, y: a.y + a.h },
                           segs: [{ line: { x: ax, y: bottom } }, { line: { x: bx, y: bottom } }, { line: { x: bx, y: b.y + b.h } }] };
    out.push({ key: `back:${from}>${to}`, kind: "back", ends: [from, to], ...drawn(route),
               label: badge ? { x: (ax + bx) / 2, y: bottom - 4, text: badge } : undefined,
               ...(link ? { link, from: { x: ax, y: a.y + a.h }, grip: { x: bx, y: (bottom + b.y + b.h) / 2 } } : {}) });
  };
  for (const e of layout.edges) {
    if (!g.boxes.has(e.from) || !g.boxes.has(e.to)) continue;
    if (e.kind === "error") { out.push(errorEdge(e, g, col)); continue; }
    if ((col.get(e.to) ?? 0) <= (col.get(e.from) ?? 0)) {
      back(e.from, e.to, loopBadge.get(e.to) ?? loopBadge.get(e.from) ?? "",
           relinkable(e) ? { from: e.from, to: e.to, kind: e.kind } : undefined);
      continue;
    }
    const a = right(e.from);
    const b = left(e.to);
    const route = forwardRoute(g, a, b, [e.from, e.to], e.kind === "no" ? "below" : "above");
    let label: Drawn["label"];
    // Labels sit at the start of the edge, in the room geometry leaves after a fan-out.
    if (e.kind === "yes" || e.kind === "no") {
      // A path that goes round the steps between is labelled on the side it turns to.
      const pts = route.segs.length > 1 ? routeSamples(route) : [];
      const turns = pts.length ? pts[pts.length >> 1].y : b.y;
      label = { x: a.x + 6, y: e.kind === "yes" ? Math.min(a.y, turns) - 4 : Math.max(a.y, turns) + 12, text: e.kind };
    } else if (e.kind === "branch" && !labelled.has(e.from)) {
      // Under the fork, ending at its bar: every branch's arrow leaves the bar's middle and
      // fans out to the right, so a word beside the bar was always crossed by one of them.
      labelled.add(e.from);
      const box = g.boxes.get(e.from)!;
      label = { x: a.x, y: box.y + box.h + 14, text: "parallel", anchor: "end" };
    }
    out.push({ key: `${e.kind}:${e.from}>${e.to}`, kind: e.kind, ends: [e.from, e.to], ...drawn(route), label,
               ...(relinkable(e) ? { link: { from: e.from, to: e.to, kind: e.kind }, from: a, grip: routeGrip(route, GRIP_T) } : {}) });
    // A loop's body returns to its controller; the spec only names the way in.
    // A loop over several steps returns from the last of them.
    if (e.kind === "loop" && !backs.has(`${e.to}>${e.from}`)) {
      const tail = tails.get(e.from) ?? e.to;
      if (g.boxes.has(tail)) back(tail, e.from, loopBadge.get(e.from) ?? "");
    }
  }
  for (const id of layout.terminal) {
    if (!g.boxes.has(id)) continue;
    const a = right(id);
    const route = forwardRoute(g, a, { x: g.end.x, y: g.midY }, [id]);
    out.push({ key: `${id}>end`, kind: "end", ends: [id], ...drawn(route),
               link: { from: id, to: null, kind: "end" }, from: a, grip: routeGrip(route, GRIP_T) });
  }
  return out;
}

/**
 * AN ERROR PATH IS ITS OWN KIND OF ARROW: dashed, in the error colour, leaving from the BOTTOM of
 * the step (its other arrows leave from its right), and labelled "if it fails", so it is never
 * read as where the step goes when it works. On to a later step it curves there when nothing is
 * in its way, and otherwise runs under the steps between and comes up into its target; back to
 * an earlier step (a retry from further back) it runs along the bottom, as a way back does.
 */
function errorEdge(e: GraphEdge, g: LaneGeometry, col: Map<string, number>): Drawn {
  const a = g.boxes.get(e.from)!;
  const b = g.boxes.get(e.to)!;
  const link: FlowLink = { from: e.from, to: e.to, kind: "error" };
  const ends = [e.from, e.to];
  const start = { x: a.x + a.w * 0.7, y: a.y + a.h };
  // Hung from its top (flow.css), just under the step: its words grow with the root font, and a
  // baseline 12px down put their top under the step's own edge at 1920.
  const label = { x: start.x + 6, y: start.y + 3, text: "if it fails" };
  const bottom = g.height - 8;
  const forward = (col.get(e.to) ?? 0) > (col.get(e.from) ?? 0) && b.x > start.x + 24;
  if (forward) {
    const end = { x: b.x, y: b.y + b.h / 2 };
    const c1 = { x: start.x, y: start.y + Math.max(24, (end.y - start.y) / 2) };
    const c2 = { x: end.x - Math.max(24, (end.x - start.x) / 3), y: end.y };
    const route: Route = { from: start, segs: [{ c1, c2, to: end }] };
    if (!stepsCrossed(route, g.boxes, ends).length) {
      return { key: `error:${e.from}>${e.to}`, kind: "error", label, link, from: start, ends, ...drawn(route),
               grip: cubicAt(start, c1, c2, end, GRIP_T) };
    }
  }
  // Under the steps: down from the step, along the bottom, up into the target's underside.
  const bx = b.x + b.w * 0.3;
  const route: Route = { from: start, segs: [{ line: { x: start.x, y: bottom } }, { line: { x: bx, y: bottom } },
                                             { line: { x: bx, y: b.y + b.h } }] };
  return { key: `error:${e.from}>${e.to}`, kind: "error", label, link, from: start, ends, ...drawn(route),
           grip: { x: bx, y: (bottom + b.y + b.h) / 2 } };
}

/** Whether a point is inside a step: a `+` there would sit on the step's words. */
function underNode(p: { x: number; y: number }, g: LaneGeometry): boolean {
  for (const b of g.boxes.values()) {
    if (p.x > b.x + 4 && p.x < b.x + b.w - 4 && p.y > b.y + 4 && p.y < b.y + b.h - 4) return true;
  }
  return false;
}

/* ── Where an arrow goes, from a list ──────────────────────────────────────────── */

/** The keyboard's way to re-link an arrow (and a pointer's, without dragging): every step it
 *  could go to, and End. Opens focused on the list; Escape closes it. */
function LinkPicker({ link, at, words, choices, onPick, onClose }: {
  link: FlowLink; at: Point; words: string; choices: { id: string; title: string }[];
  onPick(target: string | null): void; onClose(): void;
}) {
  const ref = useRef<HTMLSelectElement>(null);
  useEffect(() => { ref.current?.focus(); }, []);
  const error = link.kind === "error";
  const label = error ? `${words}: if it fails, go to` : `${words} goes to`;
  return (
    <div className="wf-link-picker" role="group" aria-label={label} data-testid="wf-link-picker"
         style={{ left: at.x, top: at.y }}
         onClick={(e) => e.stopPropagation()} onPointerDown={(e) => e.stopPropagation()}
         onKeyDown={(e) => { if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); onClose(); } }}>
      <label className="wf-link-picker-label">
        <span>{error ? "If it fails, go to" : "Goes to"}</span>
        <select ref={ref} className="wf-select wf-select-sm" aria-label={label} data-testid="wf-link-target"
                value={link.to ?? END_VALUE}
                onChange={(e) => onPick(e.target.value === END_VALUE ? null : e.target.value)}>
          {choices.map((c) => <option key={c.id} value={c.id}>{c.title}</option>)}
          <option value={END_VALUE}>{error ? "Stop the run" : "End"}</option>
        </select>
      </label>
      <button type="button" className="wf-link" onClick={onClose} data-testid="wf-link-picker-close">Close</button>
    </div>
  );
}

/* ── The lane ──────────────────────────────────────────────────────────────────── */

export function FlowLane(p: {
  spec: Spec; palette: Palette | null; mode: "design" | "run" | "preview";
  density?: "auto" | "compact"; selection?: string | null; problems?: Problem[];
  runState?: Record<string, LaneRunState>; onSelect?(id: string | null): void;
  onInsert?(at: number, item: PaletteItem): void; onMove?(id: string, to: number): void;
  canMove?(id: string, to: number): boolean; renderGapAction?(at: number): React.ReactNode;
  /** Fit (default) or 100%. Controlled when given; otherwise the lane keeps its own. */
  zoom?: LaneZoom; onZoomChange?(zoom: LaneZoom): void;
  /** Read-only: no drag, no keyboard moves, no gap actions. */
  readOnly?: boolean;
  /** The lane's accessible name. */
  label?: string;
  /** How much of the lane's right edge something floats over (the inspector drawer), in px. */
  occludedRight?: number;
  /** A step put down off the grid (its offset from its grid place), or back on it (null). */
  onPlace?(id: string, offset: NodeOffset | null): void;
  /** Every step back on the grid. */
  onResetLayout?(): void;
  /** An arrow's end let go on another step (`target`; null for End, START_TARGET for Start), or
   *  a new target picked for it from the list. The editor decides (lib/workflow-rewire.ts). */
  onRewire?(link: FlowLink, target: string | null): void;
}): React.JSX.Element {
  useTips();
  const { spec, palette, mode, selection = null, problems, runState, onSelect, onInsert, onMove,
          onPlace, onResetLayout, onRewire, renderGapAction, readOnly } = p;
  const design = mode === "design" && !readOnly;
  const steps = spec.steps;

  const layout = useMemo(() => layoutFlow(spec), [spec]);
  const order = useMemo(() => linearOrder(spec), [spec]);
  const views = useMemo(() => new Map(steps.map((s) => [s.id, viewOf(s, palette)])), [steps, palette]);
  const details = useMemo(() => new Map(steps.map((s) => [s.id, nodeDetail(s, palette)])), [steps, palette]);
  const types = useMemo(() => new Map([...views].map(([id, v]) => [id, v.type] as [string, StepView["type"]])), [views]);
  const problemOf = useMemo(() => {
    const m = new Map<string, string>();
    for (const x of problems ?? []) if (x.step_id && !m.has(x.step_id)) m.set(x.step_id, x.message);
    return m;
  }, [problems]);
  // Tab and arrow order: along the flow, top to bottom within a column.
  const navOrder = useMemo(() => [...layout.nodes].sort((a, b) => a.column - b.column || a.row - b.row)
    .map((n) => n.id).filter((id) => views.has(id)), [layout, views]);

  const rootRef = useRef<HTMLDivElement>(null);
  const viewportRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLDivElement>(null);
  const caretRef = useRef<HTMLDivElement>(null);
  const slots = useRef(new Map<string, HTMLDivElement>());
  const viewportId = useId();

  const [available, setAvailable] = useState(0);
  useLayoutEffect(() => {
    const el = viewportRef.current;
    if (!el) return;
    const read = () => setAvailable(el.clientWidth);
    read();
    if (typeof ResizeObserver === "undefined") {
      window.addEventListener("resize", read);
      return () => window.removeEventListener("resize", read);
    }
    const ro = new ResizeObserver(read);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const [ownZoom, setOwnZoom] = useState<LaneZoom>("fit");
  const zoom = p.zoom ?? ownZoom;
  const setZoom = (z: LaneZoom) => { if (p.onZoomChange) p.onZoomChange(z); else setOwnZoom(z); };
  // ROOMY OR FIT. A run is always drawn roomy: Comfortable nodes at full size, the lane
  // scrolling sideways inside itself (never the page), with the step in flight kept in view.
  // Shrinking eleven steps into one screen made them unreadable. The editor keeps Fit as its
  // default, for seeing and dragging the whole flow, and 100% is the same roomy lane.
  // A run whose host controls the zoom (the run's full page) gets Fit and 100% like the editor.
  const runZoom = mode === "run" && p.zoom !== undefined;
  const roomy = p.density !== "compact" && ((mode === "run" && !runZoom) || zoom === "full");
  // The root font the node's words are set in (it grows with the screen): nodes are as tall as
  // their rows at that size, so a title never runs into the row under it (geometry nodeHeight).
  const [rem, setRem] = useState(BASE_REM);
  useLayoutEffect(() => {
    const read = () => {
      const px = parseFloat(getComputedStyle(document.documentElement).fontSize);
      setRem(Number.isFinite(px) && px > 0 ? px : BASE_REM);
    };
    read();
    window.addEventListener("resize", read);
    return () => window.removeEventListener("resize", read);
  }, []);
  const grid = useMemo(() => (roomy ? roomyGeometry(layout, types, available, rem)
    : chooseDensity(layout, types, available, p.density === "compact" ? "compact" : undefined, rem)),
  [roomy, layout, types, available, p.density, rem]);
  // Where the operator has put steps, and (while one is being dragged off the grid) where it is.
  const offsets = useMemo(() => layoutOffsets(spec), [spec]);
  const settled = useMemo(() => placedGeometry(grid, offsets), [grid, offsets]);
  const [live, setLive] = useState<{ id: string; x: number; y: number } | null>(null);
  const liveRef = useRef(live);
  liveRef.current = live;
  const geometry = useMemo(() => {
    const home = live ? grid.boxes.get(live.id) : undefined;
    if (!live || !home) return settled;
    const moved = new Map(offsets);
    moved.set(live.id, offsetFromCanvas({ dx: live.x - home.x, dy: live.y - home.y }, grid.density));
    return placedGeometry(grid, moved);
  }, [grid, settled, offsets, live]);
  // The scale is the settled drawing's: a step dragged past the edge must not rescale the lane
  // under the pointer.
  const scale = roomy ? 1 : fitScale(settled.width, available, settled.density);
  const scrolls = overflows(geometry.width, scale, available);
  // Under a drawer: when the drawing reaches under it, give the lane that much more to scroll,
  // so the last steps can be brought clear of it. Density and scale are untouched.
  const cover = Math.max(0, Math.min(p.occludedRight ?? 0, available));
  const drawn = geometry.width * scale;
  const spare = cover > 0 && drawn > available - cover ? cover : 0;
  const sizerW = drawn + spare;
  const scaleRef = useRef(scale);
  scaleRef.current = scale;

  // WHICH WAY IT CAN STILL SCROLL, for the fade and the arrow on that edge. Computed from the
  // drawing's width and the viewport's scroll position, not measured from layout.
  const [ends, setEnds] = useState({ left: false, right: false });
  useEffect(() => {
    const v = viewportRef.current;
    if (!v) return;
    const read = () => {
      const max = Math.max(0, sizerW - available);
      const next = { left: v.scrollLeft > 1, right: v.scrollLeft < max - 1 };
      setEnds((e) => (e.left === next.left && e.right === next.right ? e : next));
    };
    read();
    v.addEventListener("scroll", read, { passive: true });
    return () => v.removeEventListener("scroll", read);
  }, [sizerW, available]);
  const nudge = (dir: -1 | 1) => {
    const v = viewportRef.current;
    if (!v) return;
    const by = Math.max(160, Math.round(available * 0.7)) * dir;
    if (typeof v.scrollBy === "function") v.scrollBy({ left: by, behavior: "smooth" });
    else v.scrollLeft += by;
  };

  const canMoveProp = p.canMove;
  const canMove = useCallback((id: string, to: number) =>
    (canMoveProp ? canMoveProp(id, to) : modelCanMove(spec, id, to)), [canMoveProp, spec]);
  const canInsert = useMemo(() => {
    const cache = new Map<number, boolean>();
    return (at: number) => {
      if (!cache.has(at)) cache.set(at, insertStep(spec, at, PROBE) !== spec);
      return cache.get(at)!;
    };
  }, [spec]);

  // Which arrow a `+` of a branched flow sits on: the one into a path's head, the one out of a
  // path's last step (drag-plan.ts `GapArrows`), so it is not drawn against a step's edge.
  const arrows = useMemo<GapArrows>(() => {
    const drawn = layout.edges.filter((e) => e.kind !== "loop" && e.kind !== "error");
    const one = <T,>(xs: T[]) => (xs.length === 1 ? xs[0] : undefined);
    return {
      into: (id) => one(drawn.filter((e) => e.to === id))?.from,
      out: (id) => {
        const outs = drawn.filter((e) => e.from === id);
        return outs.length ? one(outs)?.to : layout.terminal.includes(id) ? null : undefined;
      },
    };
  }, [layout]);
  // After a path's last step, where the gap before the next step in the order cannot serve.
  // A flow with a moved step is drawn as a branched one is: curved arrows, gaps between steps.
  // An error path is drawn as an arrow of its own, so a flow with one is drawn as a branched one.
  const errorPaths = useMemo(() => layout.edges.filter((e) => e.kind === "error"), [layout]);
  const linear = layout.linear && !offsets.size && !live && !errorPaths.length;
  const afterPoints = useMemo(() => {
    if (layout.linear) return [];
    const bodies = new Map(steps.filter((s) => REPEATS.has(s.kind) && s.body)
      .map((s) => [s.id, String(s.body)]));
    return afterGapPoints(afterGaps(spec), order, geometry, (id) => bodies.get(id), arrows);
  }, [layout.linear, spec, steps, order, geometry, arrows]);

  /* ── Announcements ── */
  const [announcement, setAnnouncement] = useState("");
  const announceMove = useCallback((id: string, to: number) => {
    const title = views.get(id)?.title ?? id;
    setAnnouncement(`Moved ${title} to ${to + 1} of ${order.length}`);
  }, [views, order.length]);

  const announcePlace = useCallback((id: string, home: boolean) => {
    const title = views.get(id)?.title ?? id;
    setAnnouncement(home ? `${title} is back in its place` : `Placed ${title}`);
  }, [views]);

  /** Put `id`'s box at canvas point `at` (this density's units), as an offset from its grid place. */
  const placeAt = useCallback((id: string, at: { x: number; y: number }) => {
    const home = grid.boxes.get(id);
    if (!home || !onPlace) return;
    const to = clampBox(at.x, at.y);
    const dx = to.x - home.x;
    const dy = to.y - home.y;
    const back = Math.abs(dx) < HOME_SNAP && Math.abs(dy) < HOME_SNAP;
    onPlace(id, back ? null : offsetFromCanvas({ dx, dy }, grid.density));
    announcePlace(id, back);
  }, [grid, onPlace, announcePlace]);

  /* ── Drag target ── */
  // A node moved by drag or keyboard keeps focus after the re-render that moves it.
  const focusAfterRender = useRef<string | null>(null);
  const clearLive = () => { if (liveRef.current) setLive(null); };
  const lane: LaneTarget | null = design && (onMove || onInsert || onPlace) ? {
    viewport: () => viewportRef.current,
    canvas: () => canvasRef.current,
    scale: () => scaleRef.current,
    plan: (source: DragSource) => {
      if (source.kind === "node" && !onMove && !onPlace) return null;
      if (source.kind === "item" && !onInsert) return null;
      const drawn = linear === layout.linear ? layout : { ...layout, linear };
      const plan = planDrag({ layout: drawn, geometry, order, source,
                              canMove: onMove ? canMove : () => false, canInsert, after: afterPoints });
      // Off the row of a straight flow, or away from a branched one's gaps, a step is placed.
      const snap = drawn.linear ? { x: Infinity, y: geometry.node.h / 2 + ROW_GAP } : { x: GAP_SNAP, y: GAP_SNAP };
      return source.kind === "node" && onPlace ? { ...plan, place: true, snap } : plan;
    },
    preview: (source, plan, gap, place) => {
      const lp = plan as LanePlan;
      rootRef.current?.setAttribute("data-dragging", source.kind);
      if (place && source.kind === "node") {
        // Being put down off the grid: nothing slides apart; the step and its arrows are drawn
        // where it would land (the ghost under the pointer is the step itself).
        for (const [id, el] of slots.current) {
          el.style.transform = "";
          el.toggleAttribute("data-origin", id === source.id);
        }
        if (caretRef.current) caretRef.current.hidden = true;
        unpinTip();
        const cur = liveRef.current;
        if (!cur || cur.id !== source.id || Math.abs(cur.x - place.x) >= 1 || Math.abs(cur.y - place.y) >= 1) {
          setLive({ id: source.id, x: place.x, y: place.y });
        }
        return;
      }
      clearLive();
      const shifts = shiftsFor(lp, gap);
      for (const [id, el] of slots.current) {
        const dx = shifts.get(id) ?? 0;
        el.style.transform = dx ? `translateX(${dx}px)` : "";
        el.toggleAttribute("data-origin", id === lp.origin);
      }
      const caret = caretRef.current;
      if (!caret) return;
      if (gap === null) { caret.hidden = true; unpinTip(); return; }
      const allowed = lp.gaps[gap].allowed;
      caret.hidden = false;
      caret.style.left = `${caretX(lp, gap) - 1.5}px`;
      caret.style.top = `${(lp.gaps[gap].y ?? geometry.midY) - geometry.node.h / 2}px`;
      caret.style.height = `${geometry.node.h}px`;
      caret.dataset.allowed = String(allowed);
      // The reason a drop is refused is the shared tooltip, pinned to the caret: fixed on the
      // page, so the lane's edge can never clip it, and shown although the pointer is mid-drag.
      if (allowed) { caret.removeAttribute("data-tip"); unpinTip(); } else { caret.dataset.tip = BRANCH_TIP; pinTip(caret); }
    },
    place: (source, at) => {
      if (source.kind !== "node") return;
      setLive(null);
      placeAt(source.id, at);
      focusAfterRender.current = source.id;
    },
    drop: (source, gap: DropGap) => {
      if (source.kind === "node") {
        onMove?.(source.id, gap.to);
        announceMove(source.id, gap.to);
        focusAfterRender.current = source.id;
      } else {
        onInsert?.(gap.to, source.item);
      }
    },
    reset: () => {
      clearLive();
      rootRef.current?.removeAttribute("data-dragging");
      for (const el of slots.current.values()) {
        el.style.transform = "";
        el.removeAttribute("data-origin");
      }
      if (caretRef.current) caretRef.current.hidden = true;
      unpinTip();
    },
  } : null;
  const { pressNode } = useLaneDrag(lane);

  /* ── Focus and keys ── */
  useEffect(() => {
    const id = focusAfterRender.current;
    if (!id) return;
    focusAfterRender.current = null;
    slots.current.get(id)?.querySelector<HTMLElement>(".wf-node")?.focus();
  });

  const focusNode = (id: string | undefined) => {
    if (id) slots.current.get(id)?.querySelector<HTMLElement>(".wf-node")?.focus();
  };

  const onKeyDown = (e: React.KeyboardEvent<HTMLDivElement>) => {
    const el = (e.target as HTMLElement).closest<HTMLElement>(".wf-node[data-step-id]");
    const id = el?.dataset.stepId;
    if (!id) return;
    const horizontal = e.key === "ArrowLeft" || e.key === "ArrowRight";
    const arrow = horizontal || e.key === "ArrowUp" || e.key === "ArrowDown";
    if (arrow && e.altKey && e.shiftKey) {
      // Nudge the step off (or along) the grid: the keyboard's way to put it somewhere.
      e.preventDefault();
      const box = geometry.boxes.get(id);
      if (!design || !onPlace || !box) return;
      const by = NUDGE * (grid.node.w / 188);
      const dx = e.key === "ArrowLeft" ? -by : e.key === "ArrowRight" ? by : 0;
      const dy = e.key === "ArrowUp" ? -by : e.key === "ArrowDown" ? by : 0;
      placeAt(id, { x: box.x + dx, y: box.y + dy });
      focusAfterRender.current = id;
      return;
    }
    if (horizontal && e.altKey) {
      e.preventDefault();
      if (!design || !onMove) return;
      const from = order.indexOf(id);
      const to = from + (e.key === "ArrowRight" ? 1 : -1);
      if (from < 0 || to < 0 || to >= order.length) return;
      if (!canMove(id, to)) { setAnnouncement(BRANCH_TIP); return; }
      onMove(id, to);
      announceMove(id, to);
      focusAfterRender.current = id;
      return;
    }
    if (horizontal && !e.metaKey && !e.ctrlKey) {
      e.preventDefault();
      const at = navOrder.indexOf(id);
      focusNode(navOrder[at + (e.key === "ArrowRight" ? 1 : -1)]);
      return;
    }
    if ((e.key === "ArrowUp" || e.key === "ArrowDown") && !e.altKey) {
      const me = layout.nodes.find((n) => n.id === id);
      if (!me || me.rowsInColumn < 2) return;
      e.preventDefault();
      const row = me.row + (e.key === "ArrowDown" ? 1 : -1);
      focusNode(layout.nodes.find((n) => n.column === me.column && n.row === row)?.id);
    }
  };

  // Keep the selected node in view, and clear of the drawer: when something else selects it
  // (the JSON caret, a problem, an insert) or the drawer opens over it.
  useEffect(() => {
    const v = viewportRef.current;
    const box = selection ? geometry.boxes.get(selection) : undefined;
    if (!v || !box || (!scrolls && !spare)) return;
    const offset = Math.max(0, (available - sizerW) / 2);
    const left = offset + box.x * scale;
    const right = offset + (box.x + box.w) * scale;
    const visible = available - cover;
    if (left < v.scrollLeft + 24) v.scrollLeft = Math.max(0, left - 48);
    else if (right > v.scrollLeft + visible - 24) v.scrollLeft = right - visible + 48;
  }, [selection, geometry, scale, scrolls, spare, cover, available, sizerW]);

  /* ── Drawing ── */
  const edges = useMemo(() => (linear ? [] : drawEdges(layout, geometry, spec)),
    [linear, layout, geometry, spec]);

  /* ── Re-linking an arrow ── */
  // An arrow's end can be grabbed (its handle shows on hover, on the arrows out of the selected
  // step, and on the one picked) and let go on another step, End or Start; or, from the
  // keyboard, its handle opens a list of where it can go. The lane only says which arrow and
  // where: whether that is allowed is the editor's (the model, then the validator).
  const relinkable = design && !!onRewire;
  const [hovered, setHovered] = useState<string | null>(null);
  const [picked, setPicked] = useState<string | null>(null);
  const [relink, setRelink] = useState<{ key: string; x: number; y: number; over: string | null | undefined } | null>(null);
  const handles = relinkable ? edges.filter((e) => e.link && e.grip) : [];
  const pickedEdge = picked ? handles.find((e) => e.key === picked) : undefined;
  const titleOf = (id: string | null) => (id === null ? "End" : views.get(id)?.title ?? id);
  const linkWords = (l: FlowLink) => {
    if (l.kind === "error") return `If ${titleOf(l.from)} fails, it goes to ${l.to === null ? "nothing" : titleOf(l.to)}`;
    const what = l.kind === "yes" ? "The Yes path" : l.kind === "no" ? "The No path" : l.kind === "branch" ? "A branch"
      : l.kind === "loop" ? "What it runs for each item" : "The arrow";
    return `${what} from ${titleOf(l.from)} to ${titleOf(l.to)}`;
  };
  /** What is under a canvas point: a step, End (null), Start, or nothing (undefined). */
  const targetAt = useCallback((x: number, y: number): string | null | undefined => {
    const inside = (b: { x: number; y: number; w: number; h: number }, pad = 0) =>
      x >= b.x - pad && x <= b.x + b.w + pad && y >= b.y - pad && y <= b.y + b.h + pad;
    for (const [id, b] of geometry.boxes) if (inside(b)) return id;
    if (inside(geometry.end, 8)) return null;
    if (inside(geometry.start, 8)) return START_TARGET;
    return undefined;
  }, [geometry]);
  const toCanvas = (clientX: number, clientY: number) => {
    const r = canvasRef.current?.getBoundingClientRect();
    const k = scaleRef.current || 1;
    return { x: (clientX - (r?.left ?? 0)) / k, y: (clientY - (r?.top ?? 0)) / k };
  };
  const relinkTo = (link: FlowLink, target: string | null) => {
    setPicked(null);
    if (target === link.to) return;
    const where = target === START_TARGET ? "Start" : target === null && link.kind === "error" ? "Stop the run" : titleOf(target);
    setAnnouncement(`${linkWords(link)}: linking to ${where}`);
    onRewire?.(link, target);
  };
  const pressGrip = (edge: Drawn, e: React.PointerEvent<HTMLElement>) => {
    if (e.button !== 0 || !edge.link) return;
    e.preventDefault();
    e.stopPropagation();
    const link = edge.link;
    const start = { x: e.clientX, y: e.clientY };
    let moved = false;
    const move = (ev: PointerEvent) => {
      if (!moved && Math.hypot(ev.clientX - start.x, ev.clientY - start.y) < DRAG_THRESHOLD) return;
      moved = true;
      const at = toCanvas(ev.clientX, ev.clientY);
      setRelink({ key: edge.key, x: at.x, y: at.y, over: targetAt(at.x, at.y) });
    };
    const stop = (ev: PointerEvent) => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", stop);
      window.removeEventListener("pointercancel", stop);
      window.removeEventListener("keydown", esc);
      setRelink(null);
      if (!moved || ev.type !== "pointerup") return;
      const at = toCanvas(ev.clientX, ev.clientY);
      const target = targetAt(at.x, at.y);
      if (target === undefined) { setAnnouncement(`${linkWords(link)} is unchanged`); return; }
      relinkTo(link, target);
    };
    const esc = (ev: KeyboardEvent) => {
      if (ev.key !== "Escape") return;
      moved = false;
      stop(new PointerEvent("pointercancel"));
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", stop);
    window.addEventListener("pointercancel", stop);
    window.addEventListener("keydown", esc);
  };
  const relinking = relink ? handles.find((e) => e.key === relink.key) : undefined;
  /** Whether the step under a dragged arrow's end would take it, as far as the spec can say. */
  const overState = (id: string): "ok" | "no" | undefined => {
    if (!relink || !relinking?.link || relink.over !== id) return undefined;
    return "refused" in rewireLink(spec, relinking.link, id) ? "no" : "ok";
  };
  const phases = new Map(order.map((id) => [id, runPhase(runState?.[id]?.phase)]));
  /** On a run, the steps that took their error path: what the engine recorded on the step
   *  (`output.error_path_taken`), or else a failed step whose `on_error` step has started. */
  const tookError = new Set(mode === "run" ? errorPaths
    .filter((e) => runState?.[e.from]?.errorPath
      || (phases.get(e.from) === "failed" && (phases.get(e.to) ?? "future") !== "future"))
    .map((e) => e.from) : []);

  // The `+` on each connector: every gap of a linear flow, and the allowed ones of a branched one.
  const gaps: { at: number; x: number; y: number }[] = !design || !renderGapAction ? []
    : linear
      ? geometry.gapX.map((x, b) => ({ at: b >= order.length ? -1 : b, x, y: geometry.midY }))
      : [...gapPoints(order, geometry, arrows), ...afterPoints].filter((g) => canInsert(g.at) && !underNode(g, geometry));

  const slotRef = (id: string) => (el: HTMLDivElement | null) => {
    if (el) slots.current.set(id, el); else slots.current.delete(id);
  };

  const lastPhase = (index: number): RunPhase | "start" => (index <= 0 ? "start" : phases.get(order[index - 1]) ?? "future");

  return (
    <div className="wf-lane" ref={rootRef} data-mode={mode} data-density={geometry.density}
         data-linear={linear ? "true" : "false"} data-scrolls={scrolls ? "true" : undefined}
         data-empty={steps.length ? undefined : "true"} data-testid="wf-lane">
      {/* THE ARROWS SIT IN GUTTERS BESIDE THE STEPS, never over them, and only while the lane
          scrolls. Not tab stops: keyboard focus moving along the steps already scrolls them. */}
      <div className="wf-lane-row" data-gutters={scrolls ? "true" : undefined}>
      {scrolls ? (
        <button type="button" className="wf-lane-arrow" data-side="left" data-testid="wf-lane-left"
                aria-controls={viewportId} aria-label="Scroll the steps left" tabIndex={-1}
                data-off={!ends.left || undefined} disabled={!ends.left}
                onClick={() => nudge(-1)}>‹</button>
      ) : null}
      <div className="wf-lane-viewport" ref={viewportRef} id={viewportId}>
        <div className="wf-lane-sizer" style={{ width: sizerW, height: geometry.height * scale }}>
          <div className="wf-lane-canvas" ref={canvasRef} role="group" aria-label={p.label ?? "Workflow steps"}
               style={{ width: geometry.width, height: geometry.height,
                        transform: scale === 1 ? undefined : `scale(${scale})` }}
               data-scale={scale} onKeyDown={onKeyDown}
               onClick={design && (onSelect || onRewire) ? (e) => {
                 if ((e.target as HTMLElement).closest(".wf-slot, .wf-gap, button, .wf-link-picker")) return;
                 setPicked(null);
                 onSelect?.(null);
               } : undefined}>
            {edges.length ? (
              <svg className="wf-edges" width={geometry.width} height={geometry.height} aria-hidden>
                <defs>
                  <marker id={`${viewportId}-arrow`} viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7"
                          markerHeight="7" orient="auto-start-reverse">
                    <path d="M0,0 L8,4 L0,8 z" className="wf-edge-head" />
                  </marker>
                  {errorPaths.length ? (
                    <marker id={`${viewportId}-arrow-error`} viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7"
                            markerHeight="7" orient="auto-start-reverse">
                      <path d="M0,0 L8,4 L0,8 z" className="wf-edge-head" data-kind="error" />
                    </marker>
                  ) : null}
                </defs>
                {edges.map((e) => (
                  <g key={e.key} data-kind={e.kind} data-edge={e.link ? e.key : undefined}
                     data-taken={(e.kind === "error" && !!e.link && tookError.has(e.link.from)) || undefined}
                     data-picked={picked === e.key || hovered === e.key || undefined}
                     data-relinking={relink?.key === e.key || undefined}>
                    <path d={e.d} className="wf-edge" data-kind={e.kind}
                          markerEnd={e.kind === "start" ? undefined
                            : `url(#${viewportId}-arrow${e.kind === "error" ? "-error" : ""})`} />
                    {relinkable && e.link ? (
                      <path d={e.d} className="wf-edge-hit" data-testid={`wf-edge-hit-${e.key}`}
                            onPointerEnter={() => setHovered(e.key)}
                            onPointerLeave={() => setHovered((h) => (h === e.key ? null : h))}
                            onClick={(ev) => { ev.stopPropagation(); setPicked(e.key); }} />
                    ) : null}
                    {e.label ? <text x={e.label.x} y={e.label.y} className="wf-edge-label"
                                     textAnchor={e.label.anchor ?? (e.kind === "back" ? "middle" : "start")}>{e.label.text}</text> : null}
                  </g>
                ))}
                {relink && relinking?.from ? (
                  <path className="wf-edge wf-edge-relink" data-testid="wf-relink-line"
                        d={`M${relinking.from.x},${relinking.from.y} L${relink.x},${relink.y}`}
                        markerEnd={`url(#${viewportId}-arrow)`} />
                ) : null}
              </svg>
            ) : null}

            <span className="wf-flag" data-kind="start" data-testid="wf-start"
                  data-flow={mode === "run" ? (order.some((id) => phases.get(id) !== "future") ? "past" : "future") : undefined}
                  style={{ left: geometry.start.x, top: geometry.start.y,
                           width: geometry.start.w, height: geometry.start.h }}>
              <i className="wf-bubble" aria-hidden />
              <span className="wf-flag-label">Start</span>
            </span>

            {navOrder.map((id) => {
              const box = geometry.boxes.get(id);
              const view = views.get(id);
              if (!box || !view) return null;
              const index = order.indexOf(id);
              const phase = phases.get(id) ?? "future";
              return (
                <div key={id} className="wf-slot" ref={slotRef(id)} data-slot={id}
                     data-error-path={tookError.has(id) || undefined}
                     data-placed={offsets.has(id) || live?.id === id ? "true" : undefined}
                     data-link-target={overState(id)}
                     style={{ left: box.x, top: box.y, width: box.w, height: box.h }}
                     onPointerDown={lane && onMove ? (e) => {
                       if ((e.target as HTMLElement).closest(".wf-node")) pressNode(id, e);
                     } : undefined}>
                  {linear ? (
                    <i className="wf-conn" aria-hidden style={{ width: geometry.conn, left: -geometry.conn }}
                       data-flow={mode === "run" ? flowInto(lastPhase(index), phase) : undefined} />
                  ) : null}
                  <StepNode view={view} mode={mode === "run" ? "run" : "design"}
                            selected={selection === id} problem={problemOf.get(id)}
                            detail={details.get(id)} errorPath={tookError.has(id)}
                            run={mode === "run" ? { phase, meta: runState?.[id]?.meta ?? "",
                                                    tip: runState?.[id]?.tip,
                                                    agent: runState?.[id]?.agent } : undefined}
                            onSelect={onSelect ? () => onSelect(id) : undefined} />
                </div>
              );
            })}

            <div className="wf-slot wf-slot-end" ref={slotRef(END_ID)}
                 style={{ left: geometry.end.x, top: geometry.end.y, width: geometry.end.w, height: geometry.end.h }}>
              {linear ? (
                <i className="wf-conn" aria-hidden style={{ width: geometry.conn, left: -geometry.conn }}
                   data-flow={mode === "run" ? flowInto(lastPhase(order.length), "done") : undefined} />
              ) : null}
              <span className="wf-flag" data-kind="end" data-testid="wf-end"
                    data-flow={mode === "run" ? flowInto(lastPhase(order.length), "done") : undefined}>
                <i className="wf-bubble" aria-hidden />
                <span className="wf-flag-label">End</span>
              </span>
            </div>

            {gaps.map((g) => (
              <div key={`gap:${g.at}`} className="wf-gap" data-at={g.at}
                   style={{ left: g.x, top: g.y }}>
                {renderGapAction!(g.at)}
              </div>
            ))}

            {handles.map((e) => (
              <button key={`grip:${e.key}`} type="button" className="wf-edge-end"
                      data-testid={`wf-edge-end-${e.key}`} data-kind={e.kind}
                      data-on={hovered === e.key || picked === e.key || relink?.key === e.key
                               || (!!selection && e.link!.from === selection) || undefined}
                      tabIndex={selection && e.link!.from === selection ? 0 : -1}
                      aria-label={`${linkWords(e.link!)}. Drag to another step, or press Enter to choose where it goes`}
                      data-tip="Drag onto another step to link it there"
                      style={{ left: e.grip!.x, top: e.grip!.y }}
                      onPointerEnter={() => setHovered(e.key)}
                      onPointerLeave={() => setHovered((h) => (h === e.key ? null : h))}
                      onPointerDown={(ev) => pressGrip(e, ev)}
                      onClick={(ev) => { ev.stopPropagation(); setPicked(e.key); }} />
            ))}
            {pickedEdge?.link ? (
              <LinkPicker key={pickedEdge.key} link={pickedEdge.link} at={pickedEdge.grip!}
                          words={linkWords(pickedEdge.link)}
                          choices={navOrder.filter((id) => id !== pickedEdge.link!.from).map((id) => ({ id, title: titleOf(id) }))}
                          onPick={(target) => relinkTo(pickedEdge.link!, target)}
                          onClose={() => {
                            const key = pickedEdge.key;
                            setPicked(null);
                            [...(rootRef.current?.querySelectorAll<HTMLElement>(".wf-edge-end") ?? [])]
                              .find((el) => el.dataset.testid === `wf-edge-end-${key}`)?.focus();
                          }} />
            ) : null}
            {relink ? (
              <div className="wf-relink-tip" data-testid="wf-relink-tip" data-ok={relink.over !== undefined || undefined}
                   style={{ left: relink.x, top: relink.y }} aria-hidden>
                {relink.over === undefined ? "Let go on a step" : relink.over === START_TARGET ? "Start" : titleOf(relink.over)}
              </div>
            ) : null}

            <div className="wf-drop-caret" ref={caretRef} hidden aria-hidden data-testid="wf-caret" />
          </div>
        </div>
      </div>

      {scrolls ? (
        <button type="button" className="wf-lane-arrow" data-side="right" data-testid="wf-lane-right"
                aria-controls={viewportId} aria-label="Scroll the steps right" tabIndex={-1}
                data-off={!ends.right || undefined} disabled={!ends.right}
                onClick={() => nudge(1)}>›</button>
      ) : null}
      {scrolls ? (
        <>
          <span className="wf-lane-fade" data-side="left" data-on={ends.left || undefined} aria-hidden />
          <span className="wf-lane-fade" data-side="right" data-on={ends.right || undefined} aria-hidden />
        </>
      ) : null}
      </div>

      {scrolls ? (
        <Minimap viewport={viewportRef} controls={viewportId} width={geometry.width} scale={scale}
                 selected={selection}
                 dots={navOrder.map((id) => {
                   const b = geometry.boxes.get(id)!;
                   return { id, x: b.x + b.w / 2, problem: problemOf.has(id),
                            phase: mode === "run" ? phases.get(id) : undefined };
                 })} />
      ) : null}

      {(mode === "design" || runZoom) && steps.length ? (
        <div className="wf-zoom" role="group" aria-label="Zoom" data-testid="wf-zoom">
          {design && onResetLayout && offsets.size ? (
            <button type="button" className="wf-zoom-btn" data-testid="wf-tidy" onClick={onResetLayout}
                    data-tip="Put every step back in its place">Tidy</button>
          ) : null}
          <button type="button" className="wf-zoom-btn" aria-pressed={zoom === "fit"} onClick={() => setZoom("fit")} data-testid="wf-zoom-fit"
                  data-tip="Fit the whole flow (F)">Fit</button>
          <button type="button" className="wf-zoom-btn" aria-pressed={zoom === "full"} onClick={() => setZoom("full")} data-testid="wf-zoom-full">
            100%
          </button>
        </div>
      ) : null}

      {errorPaths.length ? (
        <ul className="wf-sr" data-testid="wf-error-paths" aria-label="If a step fails">
          {errorPaths.map((e) => (
            <li key={`${e.from}>${e.to}`} data-testid={`wf-error-path-${e.from}`}>
              {`If ${titleOf(e.from)} fails, go to ${titleOf(e.to)}`}
              {tookError.has(e.from) ? ": it failed, and the run went there" : ""}
            </li>
          ))}
        </ul>
      ) : null}
      <div className="wf-sr" aria-live="polite" data-testid="wf-announce">{announcement}</div>
    </div>
  );
}
