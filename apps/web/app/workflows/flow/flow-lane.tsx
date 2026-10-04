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
 */
import React, { useCallback, useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from "react";

import type {
  FlowLayout, GraphEdge, Palette, PaletteItem, Problem, Spec, SpecStep, StepView,
} from "../../../lib/workflow-editor-types";
import { canMove as modelCanMove, insertStep, layoutFlow, linearOrder } from "../../../lib/workflow-graph";
import { pinTip, unpinTip, useTips } from "./tip-layer";
import { caretX, END_ID, gapPoints, planDrag, shiftsFor, type LanePlan } from "./drag-plan";
import { chooseDensity, fitScale, overflows, type LaneGeometry } from "./geometry";
import { Minimap } from "./minimap";
import { nodeDetail, viewOf } from "./node-facts";
import type { LaneAgent } from "../../agents/chain-model";
import { StepNode, type RunPhase } from "./step-node";
import { useLaneDrag, type DragSource, type DropGap, type LaneTarget } from "./use-lane-drag";

export type LaneRunState = { phase: string; meta: string; tip?: string;
  /** Who ran it, once the work is recorded (Agent World: face, live ring, how-glyph). */
  agent?: LaneAgent };
export type LaneZoom = "fit" | "full";

const RUN_PHASES = new Set<RunPhase>(["done", "moving", "needs_you", "failed", "future", "skipped"]);
const PROBE: SpecStep = { id: "__wf_probe__", kind: "capability", binding: {} };
const BRANCH_TIP = "Edit branches in JSON";

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
               label?: { x: number; y: number; text: string } };

function drawEdges(layout: FlowLayout, g: LaneGeometry, spec: Spec): Drawn[] {
  const col = new Map(layout.nodes.map((n) => [n.id, n.column]));
  const out: Drawn[] = [];
  const right = (id: string) => { const b = g.boxes.get(id)!; return { x: b.x + b.w, y: b.y + b.h / 2 }; };
  const left = (id: string) => { const b = g.boxes.get(id)!; return { x: b.x, y: b.y + b.h / 2 }; };
  const curve = (a: { x: number; y: number }, b: { x: number; y: number }) => {
    const k = Math.max(12, (b.x - a.x) / 2);
    return `M${a.x},${a.y} C${a.x + k},${a.y} ${b.x - k},${b.y} ${b.x},${b.y}`;
  };
  const bottom = g.height - 8;
  const loopMax = new Map(spec.steps.filter((s) => s.kind === "bounded_loop")
    .map((s) => [s.id, Number(s.max_iterations) || 0]));
  const labelled = new Set<string>();
  const backs = new Set(layout.edges.filter((e) => (col.get(e.to) ?? 0) <= (col.get(e.from) ?? 0))
    .map((e) => `${e.from}>${e.to}`));

  for (const n of layout.nodes.filter((x) => x.column === 0)) {
    out.push({ key: `start>${n.id}`, kind: "start",
               d: curve({ x: g.start.x + g.start.w, y: g.midY }, left(n.id)) });
  }
  const back = (from: string, to: string, badge: number) => {
    const a = g.boxes.get(from)!;
    const b = g.boxes.get(to)!;
    const ax = a.x + a.w / 2;
    const bx = b.x + b.w / 2;
    out.push({ key: `back:${from}>${to}`, kind: "back",
               d: `M${ax},${a.y + a.h} V${bottom} H${bx} V${b.y + b.h}`,
               label: badge ? { x: (ax + bx) / 2, y: bottom - 4, text: `↻ ×${badge}` } : undefined });
  };
  for (const e of layout.edges) {
    if (!g.boxes.has(e.from) || !g.boxes.has(e.to)) continue;
    if ((col.get(e.to) ?? 0) <= (col.get(e.from) ?? 0)) {
      back(e.from, e.to, loopMax.get(e.to) ?? loopMax.get(e.from) ?? 0);
      continue;
    }
    const a = right(e.from);
    const b = left(e.to);
    let label: Drawn["label"];
    // Labels sit at the start of the edge, in the room geometry leaves after a fan-out.
    if (e.kind === "yes" || e.kind === "no") {
      label = { x: a.x + 6, y: e.kind === "yes" ? Math.min(a.y, b.y) - 4 : Math.max(a.y, b.y) + 12, text: e.kind };
    } else if (e.kind === "branch" && !labelled.has(e.from)) {
      labelled.add(e.from);
      label = { x: a.x + 6, y: a.y - 8, text: "parallel" };
    }
    out.push({ key: `${e.kind}:${e.from}>${e.to}`, kind: e.kind, d: curve(a, b), label });
    // A loop's body returns to its controller; the spec only names the way in.
    if (e.kind === "loop" && !backs.has(`${e.to}>${e.from}`)) back(e.to, e.from, loopMax.get(e.from) ?? 0);
  }
  for (const id of layout.terminal) {
    if (!g.boxes.has(id)) continue;
    out.push({ key: `${id}>end`, kind: "end", d: curve(right(id), { x: g.end.x, y: g.midY }) });
  }
  return out;
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
}): React.JSX.Element {
  useTips();
  const { spec, palette, mode, selection = null, problems, runState, onSelect, onInsert, onMove,
          renderGapAction, readOnly } = p;
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
  const geometry = useMemo(() => chooseDensity(layout, types, available,
    p.density === "compact" ? "compact" : undefined), [layout, types, available, p.density]);
  const scale = zoom === "fit" ? fitScale(geometry.width, available, geometry.density) : 1;
  const scrolls = overflows(geometry.width, scale, available);
  // Under a drawer: when the drawing reaches under it, give the lane that much more to scroll,
  // so the last steps can be brought clear of it. Density and scale are untouched.
  const cover = Math.max(0, Math.min(p.occludedRight ?? 0, available));
  const drawn = geometry.width * scale;
  const spare = cover > 0 && drawn > available - cover ? cover : 0;
  const sizerW = drawn + spare;
  const scaleRef = useRef(scale);
  scaleRef.current = scale;

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

  /* ── Announcements ── */
  const [announcement, setAnnouncement] = useState("");
  const announceMove = useCallback((id: string, to: number) => {
    const title = views.get(id)?.title ?? id;
    setAnnouncement(`Moved ${title} to ${to + 1} of ${order.length}`);
  }, [views, order.length]);

  /* ── Drag target ── */
  // A node moved by drag or keyboard keeps focus after the re-render that moves it.
  const focusAfterRender = useRef<string | null>(null);
  const lane: LaneTarget | null = design && (onMove || onInsert) ? {
    viewport: () => viewportRef.current,
    canvas: () => canvasRef.current,
    scale: () => scaleRef.current,
    plan: (source: DragSource) => {
      if (source.kind === "node" && !onMove) return null;
      if (source.kind === "item" && !onInsert) return null;
      return planDrag({ layout, geometry, order, source, canMove, canInsert });
    },
    preview: (source, plan, gap) => {
      const lp = plan as LanePlan;
      rootRef.current?.setAttribute("data-dragging", source.kind);
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
  const linear = layout.linear;
  const edges = useMemo(() => (linear ? [] : drawEdges(layout, geometry, spec)),
    [linear, layout, geometry, spec]);
  const phases = new Map(order.map((id) => [id, runPhase(runState?.[id]?.phase)]));

  // The `+` on each connector: every gap of a linear flow, and the allowed ones of a branched one.
  const gaps: { at: number; x: number; y: number }[] = !design || !renderGapAction ? []
    : linear
      ? geometry.gapX.map((x, b) => ({ at: b >= order.length ? -1 : b, x, y: geometry.midY }))
      : gapPoints(order, geometry).filter((g) => canInsert(g.at));

  const slotRef = (id: string) => (el: HTMLDivElement | null) => {
    if (el) slots.current.set(id, el); else slots.current.delete(id);
  };

  const lastPhase = (index: number): RunPhase | "start" => (index <= 0 ? "start" : phases.get(order[index - 1]) ?? "future");

  return (
    <div className="wf-lane" ref={rootRef} data-mode={mode} data-density={geometry.density}
         data-linear={linear ? "true" : "false"} data-scrolls={scrolls ? "true" : undefined}
         data-empty={steps.length ? undefined : "true"} data-testid="wf-lane">
      <div className="wf-lane-viewport" ref={viewportRef} id={viewportId}>
        <div className="wf-lane-sizer" style={{ width: sizerW, height: geometry.height * scale }}>
          <div className="wf-lane-canvas" ref={canvasRef} role="group" aria-label={p.label ?? "Workflow steps"}
               style={{ width: geometry.width, height: geometry.height,
                        transform: scale === 1 ? undefined : `scale(${scale})` }}
               data-scale={scale} onKeyDown={onKeyDown}
               onClick={design && onSelect ? (e) => {
                 if (!(e.target as HTMLElement).closest(".wf-slot, .wf-gap, button")) onSelect(null);
               } : undefined}>
            {edges.length ? (
              <svg className="wf-edges" width={geometry.width} height={geometry.height} aria-hidden>
                <defs>
                  <marker id={`${viewportId}-arrow`} viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7"
                          markerHeight="7" orient="auto-start-reverse">
                    <path d="M0,0 L8,4 L0,8 z" className="wf-edge-head" />
                  </marker>
                </defs>
                {edges.map((e) => (
                  <g key={e.key} data-kind={e.kind}>
                    <path d={e.d} className="wf-edge" data-kind={e.kind}
                          markerEnd={e.kind === "start" ? undefined : `url(#${viewportId}-arrow)`} />
                    {e.label ? <text x={e.label.x} y={e.label.y} className="wf-edge-label"
                                     textAnchor={e.kind === "back" ? "middle" : "start"}>{e.label.text}</text> : null}
                  </g>
                ))}
              </svg>
            ) : null}

            <span className="wf-flag" data-kind="start" style={{ left: geometry.start.x, top: geometry.start.y,
                                                                   width: geometry.start.w, height: geometry.start.h }}>
              Start
            </span>

            {navOrder.map((id) => {
              const box = geometry.boxes.get(id);
              const view = views.get(id);
              if (!box || !view) return null;
              const index = order.indexOf(id);
              const phase = phases.get(id) ?? "future";
              return (
                <div key={id} className="wf-slot" ref={slotRef(id)} data-slot={id}
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
                            detail={details.get(id)}
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
              <span className="wf-flag" data-kind="end">End</span>
            </div>

            {gaps.map((g) => (
              <div key={`gap:${g.at}`} className="wf-gap" data-at={g.at}
                   style={{ left: g.x, top: g.y }}>
                {renderGapAction!(g.at)}
              </div>
            ))}

            <div className="wf-drop-caret" ref={caretRef} hidden aria-hidden data-testid="wf-caret" />
          </div>
        </div>
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

      {mode === "design" && steps.length ? (
        <div className="wf-zoom" role="group" aria-label="Zoom">
          <button type="button" className="wf-zoom-btn" aria-pressed={zoom === "fit"} onClick={() => setZoom("fit")}
                  data-tip="Fit the whole flow (F)">Fit</button>
          <button type="button" className="wf-zoom-btn" aria-pressed={zoom === "full"} onClick={() => setZoom("full")}>
            100%
          </button>
        </div>
      ) : null}

      <div className="wf-sr" aria-live="polite" data-testid="wf-announce">{announcement}</div>
    </div>
  );
}
