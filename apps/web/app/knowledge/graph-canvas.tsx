"use client";

/**
 * THE GRAPH CANVAS (KNOWLEDGE_GRAPH_PRODUCT_SPEC.md §5.3): an in-house <canvas> 2D renderer, no
 * graph dependency. Geometry is lib/kg-layout.ts (pure, tested); this file only draws it and
 * turns pointer and keys into the same few intents the toolbar has: select, focus, back, zoom.
 *
 * - devicePixelRatio-aware; nodes and labels outside the viewport are culled.
 * - Fits the window on load, on data change and on resize; wheel and pinch zoom to the cursor
 *   (0.2×–4×); drag pans; dragging a node moves it for this session; double-click focuses it.
 * - Same-size solid nodes coloured by kind, 1.5–3 px directed edges with 15 × 10 px filled
 *   arrowheads ending at the rim, edge labels for weights (and on the selected node's edges),
 *   14 px node labels and 16 px bubble labels on a --graph halo, colliding labels left for hover.
 * - Colours are kg.css custom properties read from the element's computed style.
 */
import React, { forwardRef, useCallback, useEffect, useImperativeHandle, useMemo, useRef, useState } from "react";

import {
  BEND, BUBBLE_R, LOOP_EDGES, NODE_R, bidirectional, distanceToEdge, edgeGeometry, edgeWidth, fitTransform,
  kindName, layerToken, middleEllipsis, nodeAt, placeLabels, toScreen, toWorld, weightOrder, zoomAt,
  type EdgeGeometry, type LabelBox, type Pt, type Transform,
} from "../../lib/kg-layout";

export type CanvasNode = {
  id: string;
  label: string;
  sub?: string;
  kind: string;
  symbol_kind?: string;
  layer: string;
  /** The kg.css custom property it is filled with (kindToken). */
  token: string;
  bubble: boolean;
  /** Arrived live: fades in where it was placed (§4.4). */
  fresh?: boolean;
  /** Which labels win when they collide: the bigger area's (its class count). */
  rank?: number;
};

export type CanvasEdge = { src: string; dst: string; kind: string; weight: number; label: string | null };

export type CanvasHandle = { zoomIn: () => void; zoomOut: () => void; fit: () => void };

type Props = {
  nodes: CanvasNode[];
  edges: CanvasEdge[];
  positions: Map<string, Pt>;
  /** Faint hop rings around the centre (Focus). */
  rings?: number[];
  centerId?: string | null;
  mode: "overview" | "focus";
  selectedId: string | null;
  onSelect: (id: string) => void;
  /** Double-click or Enter: focus on it (or drill into an area). */
  onActivate: (id: string) => void;
  /** Esc: back to the previous view. */
  onBack?: () => void;
  onZoom?: (k: number) => void;
  ariaLabel: string;
  /** Changes when the picture is new: fit again. */
  fitKey: string;
};

const EDGE_WORDS: Record<string, string> = {
  calls: "calls", imports: "imports", extends: "extends", dispatches_to: "dispatches to", overrides: "overrides",
  implements: "implements", references: "references", defines: "defines", exposes: "exposes", reads: "reads",
  writes: "writes", about: "about", changed: "changed", evidenced_by: "evidence", approved_by: "approved by",
  supersedes: "supersedes", caused_by: "caused by", fixed_by: "fixed by", consulted: "consulted", reused: "reused",
  proposed_by: "proposed by", produced: "produced", ruled_out: "ruled out", corrects: "corrects", governs: "governs",
};

export function edgeWords(kind: string): string {
  return EDGE_WORDS[kind] ?? kind.replace(/_/g, " ");
}

/** The console's default root size (app/layout-scale.css): the spec's px sizes are at this root. */
const ROOT_PX = 19;

const TOKENS = ["--graph", "--ink", "--ink-soft", "--muted", "--faint", "--border-strong", "--surface", "--sans"] as const;

type Drawn = { id: string; x: number; y: number; r: number };

export const GraphCanvas = forwardRef<CanvasHandle, Props>(function GraphCanvas(props, ref) {
  const { nodes, edges, positions, rings, centerId, mode, selectedId, onSelect, onActivate, onBack, onZoom, ariaLabel, fitKey } = props;
  const wrap = useRef<HTMLDivElement>(null);
  const canvas = useRef<HTMLCanvasElement>(null);
  const t = useRef<Transform>({ k: 1, x: 0, y: 0 });
  const size = useRef({ w: 800, h: 520 });
  const moved = useRef(new Map<string, Pt>());
  const born = useRef(new Map<string, number>());
  const drawn = useRef<Drawn[]>([]);
  const geoms = useRef<Array<{ e: CanvasEdge; g: EdgeGeometry }>>([]);
  const frame = useRef<number | null>(null);
  const [hover, setHover] = useState<{ what: "node" | "edge"; id: string; x: number; y: number } | null>(null);
  const hoverRef = useRef(hover);
  hoverRef.current = hover;
  const sel = useRef(selectedId);
  sel.current = selectedId;

  const byId = useMemo(() => new Map(nodes.map((n) => [n.id, n])), [nodes]);
  const pairs = useMemo(() => bidirectional(edges), [edges]);
  const maxW = useMemo(() => edges.reduce((m, e) => Math.max(m, e.weight), 1), [edges]);
  const order = useMemo(() => weightOrder(nodes.map((n) => n.id), edges, centerId), [nodes, edges, centerId]);

  const posOf = useCallback((id: string): Pt | undefined => moved.current.get(id) ?? positions.get(id), [positions]);

  const draw = useCallback(() => {
    frame.current = null;
    const el = canvas.current;
    const ctx = el?.getContext?.("2d") ?? null;
    if (!el || !ctx) return;
    const { w, h } = size.current;
    const dpr = typeof window !== "undefined" ? Math.max(1, window.devicePixelRatio || 1) : 1;
    if (el.width !== Math.round(w * dpr) || el.height !== Math.round(h * dpr)) {
      el.width = Math.round(w * dpr);
      el.height = Math.round(h * dpr);
    }
    const cs = getComputedStyle(el);
    const tok = (name: string) => cs.getPropertyValue(name).trim();
    const colour = new Map<string, string>();
    const c = (name: string) => { if (!colour.has(name)) colour.set(name, tok(name)); return colour.get(name)!; };
    for (const name of TOKENS) c(name);
    const font = c("--sans") || "sans-serif";
    // Text follows the console's type scale: the spec's 14 / 16 / 12 px at the default root size.
    const root = parseFloat(getComputedStyle(document.documentElement).fontSize) || ROOT_PX;
    const fs = (px: number) => Math.round(px * (root / ROOT_PX) * 10) / 10;
    const tr = t.current;
    const now = typeof performance !== "undefined" ? performance.now() : 0;
    let animating = false;

    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    ctx.fillStyle = c("--graph");
    ctx.fillRect(0, 0, w, h);

    // Hop rings.
    if (mode === "focus" && rings?.length && centerId) {
      const cp = posOf(centerId);
      if (cp) {
        const s = toScreen(tr, cp);
        ctx.save();
        ctx.strokeStyle = c("--border-strong");
        ctx.globalAlpha = 0.45;
        ctx.lineWidth = 1;
        for (const r of rings) { ctx.beginPath(); ctx.arc(s.x, s.y, r * tr.k, 0, Math.PI * 2); ctx.stroke(); }
        ctx.restore();
      }
    }

    // Screen positions, culled.
    const margin = 120;
    const screen = new Map<string, Drawn>();
    for (const n of nodes) {
      const p = posOf(n.id);
      if (!p) continue;
      const s = toScreen(tr, p);
      // Every node the same size: 28 px (56 px bubbles) from 62.5% zoom up; zoomed further out
      // the whole picture shrinks together (never below 16 / 28 px), so nothing piles up.
      const r = Math.max(n.bubble ? 14 : 8, (n.bubble ? BUBBLE_R : NODE_R) * Math.min(1, tr.k * 1.6));
      screen.set(n.id, { id: n.id, x: s.x, y: s.y, r });
    }
    const visible = (d: Drawn) => d.x > -margin && d.x < w + margin && d.y > -margin && d.y < h + margin;
    drawn.current = [...screen.values()].filter(visible);

    // Edges.
    const hv = hoverRef.current;
    const selId = sel.current;
    const g: Array<{ e: CanvasEdge; g: EdgeGeometry }> = [];
    const edgeLabels: Array<LabelBox & { text: string; at: Pt; hot: boolean }> = [];
    ctx.save();
    for (const e of edges) {
      const a = screen.get(e.src);
      const b = screen.get(e.dst);
      if (!a || !b || (!visible(a) && !visible(b))) continue;
      const bend = pairs.has(`${e.src}\u0000${e.dst}`) ? BEND : 0;
      const geo = edgeGeometry(a, b, a.r, b.r, bend);
      g.push({ e, g: geo });
      const key = `${e.src}\u0000${e.dst}\u0000${e.kind}`;
      const hot = !!selId && (e.src === selId || e.dst === selId);
      const hovered = hv?.what === "edge" && hv.id === key;
      let stroke = c("--muted");
      let alpha = 0.7;
      if (LOOP_EDGES.has(e.kind)) { stroke = c(layerToken(byId.get(e.dst)?.layer ?? "code")); alpha = 0.95; }
      if (hot || hovered) { stroke = LOOP_EDGES.has(e.kind) ? stroke : c("--ink-soft"); alpha = 1; }
      const fresh = born.current.get(e.dst) ?? born.current.get(e.src);
      if (fresh !== undefined) { const f = Math.min(1, (now - fresh) / 600); alpha *= f; if (f < 1) animating = true; }
      ctx.globalAlpha = alpha;
      ctx.strokeStyle = stroke;
      ctx.fillStyle = stroke;
      ctx.lineWidth = edgeWidth(e.weight, maxW);
      ctx.setLineDash(e.kind === "dispatches_to" ? [6, 5] : []);
      ctx.beginPath();
      ctx.moveTo(geo.start.x, geo.start.y);
      if (bend) ctx.quadraticCurveTo(geo.ctrl.x, geo.ctrl.y, geo.end.x, geo.end.y);
      else ctx.lineTo(geo.end.x, geo.end.y);
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.beginPath();
      ctx.moveTo(geo.tip.x, geo.tip.y);
      ctx.lineTo(geo.left.x, geo.left.y);
      ctx.lineTo(geo.right.x, geo.right.y);
      ctx.closePath();
      ctx.fill();
      // Labels: weights on Whole repo, weight > 1 in Focus, the selected node's edges, hover always.
      const show = hot || hovered || (e.label && (mode === "overview" || e.weight > 1));
      if (show) {
        const text = e.label ?? edgeWords(e.kind);
        ctx.font = `500 ${fs(12)}px ${font}`;
        const tw = ctx.measureText(text).width + 12;
        edgeLabels.push({ id: key, x: geo.label.x, y: geo.label.y - fs(18) / 2, w: tw, h: fs(18), priority: (hot || hovered ? 5000 : 0) + Math.log10(Math.max(1, e.weight)), text, at: geo.label, hot: hot || hovered });
      }
    }
    ctx.restore();
    geoms.current = g;

    // Nodes.
    const labels: Array<LabelBox & { text: string; size: number; weight: number }> = [];
    for (const d of drawn.current) {
      const n = byId.get(d.id)!;
      const isSel = d.id === selId;
      let alpha = 1;
      if (n.fresh) {
        if (!born.current.has(n.id)) born.current.set(n.id, now);
        const f = Math.min(1, (now - born.current.get(n.id)!) / 600);
        alpha = f;
        if (f < 1) animating = true;
      }
      ctx.save();
      ctx.globalAlpha = alpha;
      const fill = c(n.token);
      if (isSel) {
        ctx.shadowColor = fill;
        ctx.shadowBlur = 18;
      }
      ctx.fillStyle = fill;
      ctx.beginPath();
      ctx.arc(d.x, d.y, d.r, 0, Math.PI * 2);
      ctx.fill();
      ctx.shadowBlur = 0;
      if (isSel) {
        ctx.strokeStyle = c("--ink");
        ctx.lineWidth = 3;
        ctx.beginPath();
        ctx.arc(d.x, d.y, d.r + 4.5, 0, Math.PI * 2);
        ctx.stroke();
      } else if (hv?.what === "node" && hv.id === d.id) {
        ctx.strokeStyle = c("--ink-soft");
        ctx.lineWidth = 1.5;
        ctx.beginPath();
        ctx.arc(d.x, d.y, d.r + 3, 0, Math.PI * 2);
        ctx.stroke();
      }
      ctx.restore();
      const text = n.bubble && n.sub ? `${middleEllipsis(n.label)} · ${n.sub}` : middleEllipsis(n.label);
      const sizePx = fs(n.bubble ? 16 : 14);
      const weight = n.bubble || isSel ? 600 : 400;
      ctx.font = `${weight} ${sizePx}px ${font}`;
      const tw = ctx.measureText(text).width + 8;
      const pri = isSel ? 10000 : (hv?.what === "node" && hv.id === d.id) ? 9000 : n.bubble ? 3000 + Math.min(4000, Math.log10(1 + (n.rank ?? 0)) * 500) : d.id === centerId ? 8000 : 1000 - order.indexOf(d.id);
      // Kept inside the canvas: a label at the edge slides in rather than being cut.
      const lx = Math.min(Math.max(d.x, tw / 2 + 4), Math.max(tw / 2 + 4, w - tw / 2 - 4));
      labels.push({ id: d.id, x: lx, y: d.y + d.r + 6, w: tw, h: sizePx + 6, priority: pri, text, size: sizePx, weight });
    }

    // One placement for node and edge labels: nodes first, a colliding label waits for hover/zoom.
    const shown = placeLabels([...labels, ...edgeLabels.map((l) => ({ ...l, priority: l.priority + (l.hot ? 6000 : -2000) }))]);
    for (const l of edgeLabels) {
      if (!shown.has(l.id)) continue;
      ctx.save();
      ctx.font = `500 ${fs(12)}px ${font}`;
      ctx.globalAlpha = 0.96;
      ctx.fillStyle = c("--surface");
      roundRect(ctx, l.at.x - l.w / 2, l.at.y - l.h / 2, l.w, l.h, l.h / 2);
      ctx.fill();
      ctx.strokeStyle = c("--border-strong");
      ctx.lineWidth = 1;
      ctx.stroke();
      ctx.fillStyle = l.hot ? c("--ink") : c("--ink-soft");
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText(l.text, l.at.x, l.at.y + 0.5);
      ctx.restore();
    }
    for (const l of labels) {
      if (!shown.has(l.id)) continue;
      ctx.save();
      ctx.font = `${l.weight} ${l.size}px ${font}`;
      ctx.textAlign = "center";
      ctx.textBaseline = "top";
      ctx.lineJoin = "round";
      ctx.lineWidth = 5;
      ctx.strokeStyle = c("--graph");
      ctx.strokeText(l.text, l.x, l.y);
      ctx.fillStyle = l.id === selId ? c("--ink") : c("--ink-soft");
      ctx.fillText(l.text, l.x, l.y);
      ctx.restore();
    }
    if (animating) schedule();
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [nodes, edges, rings, centerId, mode, byId, pairs, maxW, order, posOf]);

  const schedule = useCallback(() => {
    if (frame.current !== null || typeof window === "undefined") return;
    frame.current = window.requestAnimationFrame ? window.requestAnimationFrame(() => draw()) : (setTimeout(draw, 16) as unknown as number);
  }, [draw]);

  const setT = useCallback((next: Transform) => {
    t.current = next;
    onZoom?.(next.k);
    schedule();
  }, [onZoom, schedule]);

  const fit = useCallback(() => {
    const pts = nodes.map((n) => posOf(n.id)).filter((p): p is Pt => !!p);
    const ext = mode === "overview" ? { x: 40, top: BUBBLE_R, bottom: BUBBLE_R + 24 } : { x: 40, top: NODE_R + 4, bottom: NODE_R + 24 };
    setT(fitTransform(pts, size.current, { extent: ext }));
  }, [nodes, posOf, mode, setT]);

  useImperativeHandle(ref, () => ({
    zoomIn: () => setT(zoomAt(t.current, 1.25, { x: size.current.w / 2, y: size.current.h / 2 })),
    zoomOut: () => setT(zoomAt(t.current, 0.8, { x: size.current.w / 2, y: size.current.h / 2 })),
    fit,
  }), [fit, setT]);

  // New picture: forget session drags of nodes that are gone, fit again.
  useEffect(() => {
    for (const id of [...moved.current.keys()]) if (!byId.has(id)) moved.current.delete(id);
    fit();
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fitKey]);

  useEffect(() => { schedule(); }, [schedule, selectedId, hover]);

  // Fit to the window on resize.
  useEffect(() => {
    const el = wrap.current;
    if (!el) return undefined;
    const measure = () => {
      const w = el.clientWidth || 800;
      const h = el.clientHeight || 520;
      if (w === size.current.w && h === size.current.h) return false;
      size.current = { w, h };
      return true;
    };
    measure();
    fit();
    if (typeof ResizeObserver === "undefined") return undefined;
    const ro = new ResizeObserver(() => { if (measure()) fit(); });
    ro.observe(el);
    return () => ro.disconnect();
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => () => { if (frame.current !== null && typeof window !== "undefined") window.cancelAnimationFrame?.(frame.current); }, []);

  // Wheel zoom to the cursor (a non-passive listener, so the page does not scroll under it).
  useEffect(() => {
    const el = canvas.current;
    if (!el) return undefined;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      const r = el.getBoundingClientRect();
      setT(zoomAt(t.current, Math.exp(-e.deltaY * 0.0015), { x: e.clientX - r.left, y: e.clientY - r.top }));
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, [setT]);

  // Pointer: pan, drag a node, pinch, hover, click.
  const pointers = useRef(new Map<number, Pt>());
  const gesture = useRef<{ kind: "pan" | "node" | "pinch"; id?: string; start: Pt; last: Pt; dist?: number; moved: boolean } | null>(null);
  const local = (e: React.PointerEvent | React.MouseEvent): Pt => {
    const r = canvas.current!.getBoundingClientRect();
    return { x: e.clientX - r.left, y: e.clientY - r.top };
  };
  const onPointerDown = (e: React.PointerEvent) => {
    const p = local(e);
    pointers.current.set(e.pointerId, p);
    try { canvas.current?.setPointerCapture?.(e.pointerId); } catch { /* not capturable */ }
    if (pointers.current.size === 2) {
      const [a, b] = [...pointers.current.values()];
      gesture.current = { kind: "pinch", start: p, last: { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 }, dist: Math.hypot(a.x - b.x, a.y - b.y), moved: true };
      return;
    }
    const id = nodeAt(p, drawn.current);
    gesture.current = { kind: id ? "node" : "pan", id: id ?? undefined, start: p, last: p, moved: false };
  };
  const onPointerMove = (e: React.PointerEvent) => {
    const p = local(e);
    if (pointers.current.has(e.pointerId)) pointers.current.set(e.pointerId, p);
    const gst = gesture.current;
    if (gst?.kind === "pinch" && pointers.current.size >= 2) {
      const [a, b] = [...pointers.current.values()];
      const dist = Math.hypot(a.x - b.x, a.y - b.y);
      const mid = { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
      const z = zoomAt(t.current, dist / Math.max(1, gst.dist ?? dist), mid);
      setT({ ...z, x: z.x + (mid.x - gst.last.x), y: z.y + (mid.y - gst.last.y) });
      gst.dist = dist; gst.last = mid;
      return;
    }
    if (gst && pointers.current.has(e.pointerId)) {
      if (!gst.moved && Math.hypot(p.x - gst.start.x, p.y - gst.start.y) > 3) gst.moved = true;
      if (gst.moved) {
        if (gst.kind === "node" && gst.id) {
          moved.current.set(gst.id, toWorld(t.current, p));
          schedule();
        } else if (gst.kind === "pan") {
          setT({ ...t.current, x: t.current.x + (p.x - gst.last.x), y: t.current.y + (p.y - gst.last.y) });
        }
        gst.last = p;
        return;
      }
    }
    // Hover.
    const id = nodeAt(p, drawn.current);
    if (id) {
      if (hoverRef.current?.id !== id) setHover({ what: "node", id, x: p.x, y: p.y });
      return;
    }
    const near = geoms.current.find(({ g }) => distanceToEdge(p, g) < 5);
    if (near) {
      const key = `${near.e.src}\u0000${near.e.dst}\u0000${near.e.kind}`;
      if (hoverRef.current?.id !== key) setHover({ what: "edge", id: key, x: p.x, y: p.y });
      return;
    }
    if (hoverRef.current) setHover(null);
  };
  const onPointerUp = (e: React.PointerEvent) => {
    pointers.current.delete(e.pointerId);
    const gst = gesture.current;
    if (pointers.current.size === 0) gesture.current = null;
    if (gst && !gst.moved && gst.kind === "node" && gst.id) onSelect(gst.id);
  };
  const onDoubleClick = (e: React.MouseEvent) => {
    const id = nodeAt(local(e), drawn.current);
    if (id) onActivate(id);
  };

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Tab") {
      const i = selectedId ? order.indexOf(selectedId) : -1;
      const next = e.shiftKey ? i - 1 : i + 1;
      // Past either end, Tab leaves the canvas as it would anywhere else.
      if (next < 0 || next >= order.length) return;
      e.preventDefault();
      onSelect(order[next]);
      return;
    }
    if (e.key === "Enter" && selectedId) { e.preventDefault(); onActivate(selectedId); return; }
    if (e.key === "Escape" && onBack) { e.preventDefault(); onBack(); return; }
    if (e.key === "+" || e.key === "=") { e.preventDefault(); setT(zoomAt(t.current, 1.25, { x: size.current.w / 2, y: size.current.h / 2 })); return; }
    if (e.key === "-" || e.key === "_") { e.preventDefault(); setT(zoomAt(t.current, 0.8, { x: size.current.w / 2, y: size.current.h / 2 })); return; }
    if (e.key === "0") { e.preventDefault(); fit(); }
  };

  const tip = (() => {
    if (!hover) return null;
    if (hover.what === "node") {
      const n = byId.get(hover.id);
      if (!n) return null;
      return { title: n.label, line: [kindName(n.kind, n.symbol_kind, n.id), n.sub].filter(Boolean).join(" · ") };
    }
    const [src, dst, kind] = hover.id.split("\u0000");
    const e = edges.find((x) => x.src === src && x.dst === dst && x.kind === kind);
    if (!e) return null;
    return { title: `${byId.get(src)?.label ?? src} → ${byId.get(dst)?.label ?? dst}`, line: e.label ?? edgeWords(e.kind) };
  })();

  return (
    <div ref={wrap} className="kg-canvas-wrap" data-testid="kg-canvas-wrap">
      <canvas ref={canvas} className="kg-canvas" data-testid="kg-canvas" data-mode={mode} data-nodes={nodes.length}
              data-edges={edges.length} tabIndex={0} role="img" aria-label={ariaLabel}
              aria-roledescription="graph" onKeyDown={onKeyDown}
              onPointerDown={onPointerDown} onPointerMove={onPointerMove} onPointerUp={onPointerUp}
              onPointerCancel={onPointerUp} onPointerLeave={() => setHover(null)} onDoubleClick={onDoubleClick} />
      {tip && hover ? (
        <div className="kg-tip" role="tooltip" data-testid="kg-tip" style={{ left: hover.x + 14, top: hover.y + 14 }}>
          <span className="kg-tip-title">{tip.title}</span>
          {tip.line ? <span className="kg-tip-line">{tip.line}</span> : null}
        </div>
      ) : null}
    </div>
  );
});

function roundRect(ctx: CanvasRenderingContext2D, x: number, y: number, w: number, h: number, r: number) {
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.arcTo(x + w, y, x + w, y + h, r);
  ctx.arcTo(x + w, y + h, x, y + h, r);
  ctx.arcTo(x, y + h, x, y, r);
  ctx.arcTo(x, y, x + w, y, r);
  ctx.closePath();
}

/** The legend (§5.3): every kind colour present in the view, collapsible, bottom-left. */
export function GraphLegend({ items }: { items: Array<{ key: string; token: string; name: string }> }) {
  const [open, setOpen] = useState(true);
  if (!items.length) return null;
  return (
    <div className="kg-legend" data-testid="kg-legend" data-open={open || undefined}>
      <button type="button" className="kg-legend-toggle" aria-expanded={open} onClick={() => setOpen((v) => !v)}
              data-testid="kg-legend-toggle">
        Legend {open ? "▾" : "▸"}
      </button>
      {open ? (
        <ul className="kg-legend-list">
          {items.map((i) => (
            <li key={i.key} className="kg-legend-item" data-kind={i.key}>
              <span className="kg-dot" style={{ background: `var(${i.token})` }} aria-hidden />
              {i.name}
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}

/** List view (§5.10): the current view as a table — for screen readers, keyboards and tests. */
export function GraphTable({ nodes, edges, selectedId, onSelect, onActivate }: {
  nodes: CanvasNode[]; edges: CanvasEdge[]; selectedId: string | null;
  onSelect: (id: string) => void; onActivate: (id: string) => void;
}) {
  const label = new Map(nodes.map((n) => [n.id, n.label]));
  return (
    <div className="kg-table-wrap" data-testid="kg-list-view">
      <table className="inf-table kg-table">
        <caption className="ag-sr">Nodes in this view</caption>
        <thead><tr><th scope="col">Node</th><th scope="col">Kind</th><th scope="col">Detail</th></tr></thead>
        <tbody>
          {nodes.map((n) => (
            <tr key={n.id} aria-selected={n.id === selectedId || undefined} data-testid="kg-list-node">
              <td>
                <button type="button" className="inf-link" onClick={() => onSelect(n.id)} onDoubleClick={() => onActivate(n.id)}>
                  <span className="kg-dot" style={{ background: `var(${n.token})` }} aria-hidden /> {n.label}
                </button>
              </td>
              <td>{kindName(n.kind, n.symbol_kind, n.id)}</td>
              <td className="inf-muted">{n.sub ?? ""}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <table className="inf-table kg-table">
        <caption className="ag-sr">Edges in this view</caption>
        <thead><tr><th scope="col">From</th><th scope="col">Edge</th><th scope="col">To</th><th scope="col">Weight</th></tr></thead>
        <tbody>
          {edges.map((e) => (
            <tr key={`${e.src}-${e.dst}-${e.kind}`} data-testid="kg-list-edge">
              <td>{label.get(e.src) ?? e.src}</td>
              <td>{edgeWords(e.kind)}</td>
              <td>{label.get(e.dst) ?? e.dst}</td>
              <td className="inf-num">{e.label ?? e.weight.toLocaleString("en-US")}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
