"use client";

/**
 * Drag and drop on the flow lane, hand-rolled on pointer events.
 *
 * No library: the lane is one row of absolutely positioned slots whose positions are already
 * computed (`geometry.ts`), so a drop target is "the nearest gap", and the whole job is pointer
 * bookkeeping. Pointer events cover mouse, pen and touch with one code path.
 *
 *  - START. Nothing happens until the pointer has moved 4px, so a click stays a click. On
 *    touch, a press must be held still for a moment first, so a swipe still scrolls the lane.
 *  - DURING. State lives in this module and in refs, and every frame is a transform written by
 *    `requestAnimationFrame`. React does not re-render while the pointer moves.
 *  - TARGET. The lane says where its gaps are and which are allowed (`LaneTarget.plan`). The
 *    nearest gap wins while the pointer is over the lane (or within SNAP of it). A refused gap
 *    shows a grey caret and drops nothing.
 *  - AUTO-SCROLL within EDGE_SCROLL px of either end of the lane.
 *  - END. One `drop` on release over an allowed gap. Esc, a release away from the lane, or a
 *    cancelled pointer drops nothing.
 *  - PLACE. A step dragged within its own lane and let go AWAY from every gap (outside the
 *    plan's `snap` box around each: on a straight flow, anywhere off the row; on a branched one,
 *    further than GAP_SNAP) is put down where it was let go: one `place` with the canvas point
 *    of its top-left corner. Letting go on a connector still reorders, as above. While a step is
 *    being placed, the lane draws it (and its arrows) where it would land.
 *
 * A drag can start on a node (a move within its own lane) or on a palette row (an insert into
 * whichever registered lane is under the pointer), which is why lanes register here instead of
 * being passed around.
 */
import type React from "react";
import { useCallback, useEffect, useRef } from "react";

import type { PaletteItem } from "../../../lib/workflow-editor-types";

export const DRAG_THRESHOLD = 4;
export const EDGE_SCROLL = 60;
export const SNAP = 40;
export const TOUCH_HOLD_MS = 350;
/** How near a gap (canvas units, each way) a dragged step must be let go to reorder rather than
 *  be placed, when its plan does not say. */
export const GAP_SNAP = 40;

export type DragSource = { kind: "node"; id: string } | { kind: "item"; item: PaletteItem };
/** `to` is the index the dragged or new step ends up at (an INSERT at the end uses -1).
 *  `x`/`y` are canvas units; `y` matters only where steps stack (a branched flow). */
export type DropGap = { x: number; y?: number; to: number; allowed: boolean; noop?: boolean };
/** `place`: a step may also be put down anywhere on the canvas (`LaneTarget.place`), and is
 *  when it is let go outside `snap` (half-width and half-height, canvas units) of every gap. */
export type Snap = { x: number; y: number };
export type DragPlan = { gaps: DropGap[]; place?: boolean; snap?: Snap };
/** The canvas point a placed step's top-left corner lands on. */
export type PlacePoint = { x: number; y: number };

export type LaneTarget = {
  /** The lane's visible box; the pointer must be over it (or within SNAP) to target a gap. */
  viewport(): HTMLElement | null;
  /** The scaled drawing; pointer x becomes canvas x through its rect. */
  canvas(): HTMLElement | null;
  scale(): number;
  /** Where this lane would take `source`, or null if it does not take it at all. */
  plan(source: DragSource): DragPlan | null;
  /** Draw the gap (or nothing, when `gap` is null), or the step where it would be placed. */
  preview(source: DragSource, plan: DragPlan, gap: number | null, place?: PlacePoint | null): void;
  drop(source: DragSource, gap: DropGap): void;
  /** Put a step down at a point on the canvas (only offered when its plan says `place`). */
  place?(source: DragSource, at: PlacePoint): void;
  /** Undo every preview. */
  reset(): void;
};

const lanes = new Set<LaneTarget>();

export function registerLane(lane: LaneTarget): () => void {
  lanes.add(lane);
  return () => { lanes.delete(lane); };
}

function gapDistance(gap: DropGap, x: number, y?: number): number {
  return y === undefined || gap.y === undefined ? Math.abs(gap.x - x) : Math.hypot(gap.x - x, gap.y - y);
}

/** The gap nearest to a canvas point, or null when there are none (or none within `snap`). */
export function nearestGap(x: number, gaps: DropGap[], y?: number, snap?: Snap): number | null {
  let best: number | null = null;
  let bestDistance = Infinity;
  gaps.forEach((gap, index) => {
    if (snap && (Math.abs(gap.x - x) > snap.x
                 || (y !== undefined && gap.y !== undefined && Math.abs(gap.y - y) > snap.y))) return;
    const d = gapDistance(gap, x, y);
    if (d < bestDistance) { bestDistance = d; best = index; }
  });
  return best;
}

function within(rect: DOMRect, x: number, y: number, slack: number): boolean {
  return x >= rect.left - slack && x <= rect.right + slack && y >= rect.top - slack && y <= rect.bottom + slack;
}

type Session = {
  source: DragSource;
  pointerId: number;
  startX: number; startY: number;
  x: number; y: number;
  from: HTMLElement;
  home: LaneTarget | null;
  touch: boolean;
  armed: boolean;
  holdTimer: number | null;
  active: boolean;
  ghost: HTMLElement | null;
  grabX: number; grabY: number;
  lane: LaneTarget | null;
  plans: Map<LaneTarget, DragPlan | null>;
  gap: number | null;
  /** Where a step being placed would land (canvas units), when it is not over a gap. */
  place: PlacePoint | null;
  frame: number | null;
  onEnd?: (dropped: boolean) => void;
};

let session: Session | null = null;

/** Whether a drag is under way, for code that must not react to the pointer meanwhile. */
export function isDragging(): boolean {
  return !!session?.active;
}

function planFor(s: Session, lane: LaneTarget): DragPlan | null {
  if (!s.plans.has(lane)) s.plans.set(lane, lane.plan(s.source));
  return s.plans.get(lane) ?? null;
}

function laneUnder(s: Session): LaneTarget | null {
  const candidates = s.home ? [s.home] : [...lanes];
  for (const lane of candidates) {
    const el = lane.viewport();
    if (!el) continue;
    if (within(el.getBoundingClientRect(), s.x, s.y, SNAP) && planFor(s, lane)) return lane;
  }
  return null;
}

function makeGhost(s: Session) {
  const rect = s.from.getBoundingClientRect();
  const clone = s.from.cloneNode(true) as HTMLElement;
  clone.removeAttribute("id");
  clone.removeAttribute("data-testid");
  clone.removeAttribute("data-tip");
  // The copy is drawn outside its lane, so it carries the lane's density with it: a compact
  // node must not re-flow into the comfortable layout on its way across.
  const ghost = document.createElement("div");
  ghost.className = "wf-ghost";
  ghost.setAttribute("aria-hidden", "true");
  const density = s.from.closest<HTMLElement>("[data-density]")?.dataset.density;
  if (density) ghost.dataset.density = density;
  ghost.appendChild(clone);
  const scale = s.home?.scale() ?? 1;
  const w = s.from.offsetWidth || rect.width / scale;
  const h = s.from.offsetHeight || rect.height / scale;
  Object.assign(ghost.style, {
    position: "fixed", left: "0px", top: "0px", width: `${w}px`, height: `${h}px`, margin: "0",
    pointerEvents: "none", zIndex: "10001", transformOrigin: "0 0",
  });
  s.grabX = s.startX - rect.left;
  s.grabY = s.startY - rect.top;
  ghost.dataset.scale = String(scale);
  document.body.appendChild(ghost);
  s.ghost = ghost;
}

function frame() {
  const s = session;
  if (!s || !s.active) return;
  s.frame = null;
  if (s.ghost) {
    const scale = Number(s.ghost.dataset.scale) || 1;
    s.ghost.style.transform = `translate(${s.x - s.grabX}px, ${s.y - s.grabY}px) scale(${scale})`;
  }
  const lane = laneUnder(s);
  if (s.lane && s.lane !== lane) s.lane.reset();
  s.lane = lane;
  let scrolling = false;
  if (lane) {
    const plan = planFor(s, lane)!;
    const viewport = lane.viewport();
    const canvas = lane.canvas();
    if (viewport) {
      const r = viewport.getBoundingClientRect();
      const fromLeft = s.x - r.left;
      const fromRight = r.right - s.x;
      if (r.width > 0 && viewport.scrollWidth > viewport.clientWidth) {
        const step = (d: number) => Math.ceil(((EDGE_SCROLL - Math.max(0, d)) / EDGE_SCROLL) * 14);
        if (fromLeft < EDGE_SCROLL && viewport.scrollLeft > 0) {
          viewport.scrollLeft -= step(fromLeft); scrolling = true;
        } else if (fromRight < EDGE_SCROLL && viewport.scrollLeft + viewport.clientWidth < viewport.scrollWidth) {
          viewport.scrollLeft += step(fromRight); scrolling = true;
        }
      }
    }
    const origin = canvas?.getBoundingClientRect();
    const scale = lane.scale() || 1;
    const cx = (s.x - (origin?.left ?? 0)) / scale;
    const cy = (s.y - (origin?.top ?? 0)) / scale;
    const placing = !!plan.place && s.source.kind === "node" && lane === s.home && !!lane.place;
    s.gap = nearestGap(cx, plan.gaps, cy, placing ? plan.snap ?? { x: GAP_SNAP, y: GAP_SNAP } : undefined);
    s.place = placing && s.gap === null ? { x: cx - s.grabX / scale, y: cy - s.grabY / scale } : null;
    lane.preview(s.source, plan, s.gap, s.place);
  } else {
    s.gap = null;
    s.place = null;
  }
  if (scrolling) schedule();
}

function schedule() {
  if (session && session.frame === null) session.frame = window.requestAnimationFrame(frame);
}

function activate(s: Session) {
  s.active = true;
  makeGhost(s);
  document.documentElement.dataset.wfDragging = "true";
  schedule();
}

function swallowClick(e: Event) {
  e.stopPropagation();
  e.preventDefault();
}

function finish(dropped: boolean) {
  const s = session;
  if (!s) return;
  session = null;
  window.removeEventListener("pointermove", onMove);
  window.removeEventListener("pointerup", onUp);
  window.removeEventListener("pointercancel", onCancel);
  window.removeEventListener("keydown", onKey, true);
  document.removeEventListener("touchmove", blockTouchScroll);
  if (s.holdTimer !== null) window.clearTimeout(s.holdTimer);
  if (s.frame !== null) window.cancelAnimationFrame(s.frame);
  if (s.active) {
    // The release that ends a drag must not also click whatever it landed on.
    window.addEventListener("click", swallowClick, { capture: true, once: true });
    window.setTimeout(() => window.removeEventListener("click", swallowClick, { capture: true }), 0);
    s.ghost?.remove();
    delete document.documentElement.dataset.wfDragging;
  }
  for (const lane of s.plans.keys()) lane.reset();
  s.onEnd?.(dropped);
}

function onMove(e: PointerEvent) {
  const s = session;
  if (!s || e.pointerId !== s.pointerId) return;
  s.x = e.clientX;
  s.y = e.clientY;
  if (!s.active) {
    const moved = Math.hypot(s.x - s.startX, s.y - s.startY) >= DRAG_THRESHOLD;
    if (!moved) return;
    // A touch that moves before the hold is a scroll, not a drag.
    if (s.touch && !s.armed) { finish(false); return; }
    activate(s);
    return;
  }
  schedule();
}

function onUp(e: PointerEvent) {
  const s = session;
  if (!s || e.pointerId !== s.pointerId) return;
  if (!s.active) { finish(false); return; }
  s.x = e.clientX;
  s.y = e.clientY;
  // Re-resolve against the release point: a fast flick can end between frames.
  if (s.frame !== null) { window.cancelAnimationFrame(s.frame); s.frame = null; }
  frame();
  const lane = s.lane;
  const plan = lane ? planFor(s, lane) : null;
  const gap = plan && s.gap !== null ? plan.gaps[s.gap] : null;
  const source = s.source;
  const place = lane && !gap ? s.place : null;
  finish(!!(lane && ((gap && gap.allowed && !gap.noop) || place)));
  if (lane && gap && gap.allowed && !gap.noop) lane.drop(source, gap);
  else if (lane && place) lane.place?.(source, place);
}

function onCancel(e: PointerEvent) {
  if (session && e.pointerId === session.pointerId) finish(false);
}

function onKey(e: KeyboardEvent) {
  if (e.key !== "Escape" || !session) return;
  e.preventDefault();
  e.stopPropagation();
  finish(false);
}

function blockTouchScroll(e: TouchEvent) {
  if (session?.active || session?.armed) e.preventDefault();
}

/** Starts watching a press that may become a drag. Call from `onPointerDown`. */
export function beginDrag(source: DragSource, e: { pointerId: number; pointerType?: string; button: number;
                            clientX: number; clientY: number }, from: HTMLElement,
                          home: LaneTarget | null = null, onEnd?: (dropped: boolean) => void): void {
  if (e.button !== 0 && e.pointerType !== "touch") return;
  if (session) finish(false);
  const touch = e.pointerType === "touch";
  const s: Session = {
    source, pointerId: e.pointerId, startX: e.clientX, startY: e.clientY, x: e.clientX, y: e.clientY,
    from, home, touch, armed: !touch, holdTimer: null, active: false, ghost: null, grabX: 0, grabY: 0,
    lane: null, plans: new Map(), gap: null, place: null, frame: null, onEnd,
  };
  session = s;
  if (touch) {
    s.holdTimer = window.setTimeout(() => {
      s.holdTimer = null;
      if (session !== s) return;
      s.armed = true;
      activate(s);
    }, TOUCH_HOLD_MS);
  }
  window.addEventListener("pointermove", onMove);
  window.addEventListener("pointerup", onUp);
  window.addEventListener("pointercancel", onCancel);
  window.addEventListener("keydown", onKey, true);
  document.addEventListener("touchmove", blockTouchScroll, { passive: false });
}

/**
 * Registers a lane as a drop target for as long as it is mounted, and returns the press
 * handler for its own nodes. The target object is read through a ref, so the lane can pass a
 * fresh one every render without re-registering.
 */
export function useLaneDrag(target: LaneTarget | null): {
  pressNode(id: string, e: React.PointerEvent<HTMLElement>): void;
} {
  const ref = useRef(target);
  ref.current = target;
  const proxyRef = useRef<LaneTarget | null>(null);
  const enabled = !!target;
  useEffect(() => {
    if (!enabled) return;
    const proxy: LaneTarget = {
      viewport: () => ref.current?.viewport() ?? null,
      canvas: () => ref.current?.canvas() ?? null,
      scale: () => ref.current?.scale() ?? 1,
      plan: (source) => ref.current?.plan(source) ?? null,
      preview: (source, plan, gap, place) => ref.current?.preview(source, plan, gap, place),
      drop: (source, gap) => ref.current?.drop(source, gap),
      place: (source, at) => ref.current?.place?.(source, at),
      reset: () => ref.current?.reset(),
    };
    proxyRef.current = proxy;
    const off = registerLane(proxy);
    return () => { off(); proxyRef.current = null; };
  }, [enabled]);
  const pressNode = useCallback((id: string, e: React.PointerEvent<HTMLElement>) => {
    if (!proxyRef.current) return;
    const node = (e.currentTarget.querySelector<HTMLElement>(".wf-node") ?? e.currentTarget);
    beginDrag({ kind: "node", id }, e.nativeEvent, node, proxyRef.current);
  }, []);
  return { pressNode };
}
