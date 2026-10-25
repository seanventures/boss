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
  BEND, BUBBLE_R, LABEL_ARC, LABEL_MAX, LOOP_EDGES, NODE_R, arrowRim, bidirectional, distanceToEdge, edgeGeometry, edgeWidth,
  fitTransform, kindName, layerToken, middleEllipsis, nodeAt, placeLabels, placeNodeLabels, revealPan, toScreen, toWorld, weightOrder,
  zoomAt,
  type EdgeGeometry, type LabelBox, type LabelItem, type Pt, type Transform,
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
  /** Where its label goes first: under it (default), or on the side facing away from the Focus
   *  centre (ring nodes). Either way it moves to another side, or shortens, rather than overlap. */
  labelMode?: "below" | "out";
  /** Its polar angle around the Focus centre and its ring. */
  angle?: number;
  ring?: number;
  /** Characters before the label is middle-ellipsised (the full name is in the tooltip). */
  labelMax?: number;
  /** Its shortest form when the name has no room ("+38 more" for a group). */
  short?: string;
};

/** One edge's identity on the canvas: hover, labels and the strongest-edge set use it. */
export function edgeKey(e: { src: string; dst: string; kind: string }): string {
  return `${e.src}\u0000${e.dst}\u0000${e.kind}`;
}

export type CanvasEdge = { src: string; dst: string; kind: string; weight: number; label: string | null };

export type CanvasHandle = { zoomIn: () => void; zoomOut: () => void; fit: () => void };

type Props = {
  nodes: CanvasNode[];
  edges: CanvasEdge[];
  positions: Map<string, Pt>;
  /** Faint hop rings around the centre (Focus): half-heights; their half-width is `ringAspect` times. */
  rings?: number[];
  ringAspect?: number;
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
  /** Edges (edgeKey) whose weight label shows; absent = every edge with a weight > 1. */
  labelled?: Set<string> | null;
  /** A click on empty canvas. */
  onClear?: () => void;
  /** Screen px along the right edge covered by an open drawer: the fit centres in what is left. */
  occludeRight?: number;
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

/** The opening view's least scale: below it labels stop reading as designed. */
export const READABLE = 0.85;
/** Text never shrinks below this share of its size (12 px node labels at the default root). */
export const MIN_TEXT = 0.8;

/**
 * The width a picture is framed in beside the details drawer: what the drawer leaves, unless that
 * is a sliver (a narrow window, where the drawer covers most of the canvas) — then the whole
 * canvas, the drawer over its edge, so the picture is not squeezed to a stamp.
 */
export function visibleWidth(canvasW: number, occlude: number): number {
  const left = canvasW - occlude;
  return occlude > 0 && left < Math.max(300, canvasW * 0.35) ? canvasW : Math.max(240, left);
}

function rootScale(): number {
  if (typeof document === "undefined") return 1;
  return (parseFloat(getComputedStyle(document.documentElement).fontSize) || ROOT_PX) / ROOT_PX;
}

/** A label's width at 100% (screen px), estimated from its characters: the frame and the
 *  drawer's pan use it before anything is measured. */
export function estimateLabel(n: Pick<CanvasNode, "label" | "labelMax" | "bubble" | "sub">, scale = 1): number {
  const chars = Math.min(n.label.length, n.labelMax ?? LABEL_MAX);
  const name = chars * (n.bubble ? 9 : 7.6) * scale + 10;
  const sub = n.bubble && n.sub ? n.sub.length * 6.6 * scale + 10 : 0;
  return Math.max(name, sub);
}

/**
 * The screen room a picture needs past its outer node centres (fitTransform's extent): under
 * a Whole-repo bubble its two lines, half its label each side; beside a Focus ring node its
 * side label, above and below it one line. So a fitted view never cuts a label at the edge.
 */
export function frameExtent(nodes: Array<Pick<CanvasNode, "label" | "labelMax" | "bubble" | "sub" | "labelMode">>, mode: "overview" | "focus", scale = 1, viewW = Infinity): { x: number; top: number; bottom: number } {
  if (mode === "overview") {
    const widest = Math.max(160, ...nodes.map((n) => estimateLabel(n, scale)));
    return { x: Math.min(170, widest / 2 + 12), top: BUBBLE_R + 6, bottom: BUBBLE_R + 50 };
  }
  // A side label past the outer ring may shorten or move above/below (the placement does), so
  // the frame keeps room for most of one, not the longest name at full length.
  const side = Math.max(60, ...nodes.filter((n) => n.labelMode === "out").map((n) => estimateLabel(n, scale)));
  // In a narrow view the labels give way (shorter, above or below) before the picture does.
  return { x: Math.min(NODE_R + 10 + Math.min(130, side * 0.7), viewW * 0.12), top: NODE_R + 30, bottom: NODE_R + 30 };
}

const TOKENS = ["--graph", "--ink", "--ink-soft", "--muted", "--faint", "--border-strong", "--surface", "--sans"] as const;

type Drawn = { id: string; x: number; y: number; r: number };

export const GraphCanvas = forwardRef<CanvasHandle, Props>(function GraphCanvas(props, ref) {
  const { nodes, edges, positions, rings, ringAspect = 1, centerId, mode, selectedId, onSelect, onActivate, onBack, onZoom, ariaLabel, fitKey, labelled, occludeRight = 0, onClear } = props;
  const wrap = useRef<HTMLDivElement>(null);
  const canvas = useRef<HTMLCanvasElement>(null);
  const t = useRef<Transform>({ k: 1, x: 0, y: 0 });
  // The same default the area lays its pictures out to (graph-view stageSize) until measured.
  const size = useRef({ w: 1000, h: 520 });
  const moved = useRef(new Map<string, Pt>());
  const born = useRef(new Map<string, number>());
  const drawn = useRef<Drawn[]>([]);
  const labelBoxes = useRef(new Map<string, { x: number; y: number; w: number; h: number }>());
  const geoms = useRef<Array<{ e: CanvasEdge; g: EdgeGeometry }>>([]);
  const frame = useRef<number | null>(null);
  const [hover, setHover] = useState<{ what: "node" | "edge"; id: string; x: number; y: number } | null>(null);
  const hoverRef = useRef(hover);
  hoverRef.current = hover;
  const occludeRef = useRef(occludeRight);
  occludeRef.current = occludeRight;
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
    const colour = new Map<string, string>();
    const c = (name: string) => { if (!colour.has(name)) colour.set(name, cs.getPropertyValue(name).trim()); return colour.get(name)!; };
    for (const name of TOKENS) c(name);
    const font = c("--sans") || "sans-serif";
    // Text follows the console's type scale: the spec's 14 / 16 / 12 px at the default root size,
    // and shrinks with the picture below 100% — never under 80% (tester round 3: ~7 px labels):
    // what has no room at that size waits for zoom or hover instead of overlapping.
    const root = parseFloat(getComputedStyle(document.documentElement).fontSize) || ROOT_PX;
    const tr = t.current;
    const shrink = Math.min(1, Math.max(MIN_TEXT, tr.k));
    // Labels stay inside what is visible: the canvas minus the details drawer over its right edge.
    const bounds = { x0: 4, y0: 4, x1: visibleWidth(w, occludeRef.current) - 4, y1: h - 4 };
    const fs = (px: number) => Math.round(px * (root / ROOT_PX) * shrink * 10) / 10;
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
        for (const r of rings) { ctx.beginPath(); ctx.ellipse(s.x, s.y, r * ringAspect * tr.k, r * tr.k, 0, 0, Math.PI * 2); ctx.stroke(); }
        ctx.restore();
      }
    }

    // Screen positions, culled. Every node the same size: 28 px (56 px bubbles) from 80% zoom up;
    // zoomed further out the whole picture shrinks together, and a Focus ring's nodes never grow
    // past the arc they share, so they never touch like beads (tester round 3 P0).
    const margin = 160;
    const screen = new Map<string, Drawn>();
    const ringCap = mode === "focus" ? Math.max(5, (LABEL_ARC * tr.k - 8) / 2) : Infinity;
    for (const n of nodes) {
      const p = posOf(n.id);
      if (!p) continue;
      const s = toScreen(tr, p);
      const full = (n.bubble ? BUBBLE_R : NODE_R) * Math.min(1, tr.k * 1.25);
      const r = n.bubble ? Math.max(12, full) : Math.max(5, Math.min(full, n.ring !== undefined ? ringCap : full));
      screen.set(n.id, { id: n.id, x: s.x, y: s.y, r });
    }
    const visible = (d: Drawn) => d.x > -margin && d.x < w + margin && d.y > -margin && d.y < h + margin;
    drawn.current = [...screen.values()].filter(visible);

    // Arrowheads spread on a wider rim where many arrive, so each stays a distinct head.
    const fanIn = new Map<string, number>();
    for (const e of edges) fanIn.set(e.dst, (fanIn.get(e.dst) ?? 0) + 1);

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
      const geo = edgeGeometry(a, b, a.r, arrowRim(b.r, fanIn.get(e.dst) ?? 0), bend);
      g.push({ e, g: geo });
      const key = edgeKey(e);
      const touchesSel = !!selId && (e.src === selId || e.dst === selId);
      const hovered = hv?.what === "edge" && hv.id === key;
      // The selection's edges stand out — except the Focus centre's, which are every edge.
      const hot = hovered || (touchesSel && selId !== centerId);
      let stroke = c("--muted");
      let alpha = mode === "overview" ? 0.55 : 0.6;
      if (LOOP_EDGES.has(e.kind)) { stroke = c(layerToken(byId.get(e.dst)?.layer ?? "code")); alpha = 0.95; }
      if (hot) { stroke = LOOP_EDGES.has(e.kind) ? stroke : c("--ink-soft"); alpha = 1; }
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
      // Labels (P1-3): the weight where it says something (> 1) and the edge is one of the
      // strongest; the type only on hover or on the selected node's own edges.
      const weighty = e.weight > 1 && !!e.label && (labelled ? labelled.has(key) : true);
      if (hot || weighty) {
        // "16,749 calls" says its kind already; a weightless edge says its kind ("implements").
        const text = e.weight > 1 && e.label ? e.label : edgeWords(e.kind);
        ctx.font = `500 ${fs(12)}px ${font}`;
        const tw = ctx.measureText(text).width + 12;
        const lh = fs(18);
        edgeLabels.push({ id: key, x: geo.label.x, y: geo.label.y - lh / 2, w: tw, h: lh, priority: (hot ? 5e5 : 0) + Math.log10(Math.max(1, e.weight)) * 100, text, at: geo.label, hot });
      }
    }
    ctx.restore();
    geoms.current = g;

    // Nodes.
    type Line = { text: string; size: number; weight: number; muted: boolean };
    type Sized = { lines: Line[]; w: number; h: number };
    const labels: Array<{ id: string; item: LabelItem; sized: Sized[] }> = [];
    // Each node's circle is an obstacle no label may cover.
    const obstacles: LabelBox[] = drawn.current.map((d) => ({ id: `o:${d.id}`, x: d.x, y: d.y - d.r, w: d.r * 2, h: d.r * 2, priority: 1e9 }));
    for (const d of drawn.current) {
      const n = byId.get(d.id)!;
      const isSel = d.id === selId;
      const isHover = hv?.what === "node" && hv.id === d.id;
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
      if (isSel) { ctx.shadowColor = fill; ctx.shadowBlur = 18; }
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
      } else if (isHover) {
        ctx.strokeStyle = c("--ink-soft");
        ctx.lineWidth = 1.5;
        ctx.beginPath();
        ctx.arc(d.x, d.y, d.r + 3, 0, Math.PI * 2);
        ctx.stroke();
      }
      ctx.restore();
      const max = n.labelMax ?? LABEL_MAX;
      const name = (m: number) => middleEllipsis(n.label, m);
      const weight = isSel ? 600 : 400;
      // The full name first; shorter middle-ellipsised names (and a bubble without its second
      // line) when it would collide or leave the canvas.
      const variants: Line[][] = n.bubble
        ? [
          [{ text: name(max), size: fs(16), weight: 600, muted: false }, ...(n.sub ? [{ text: n.sub, size: fs(13), weight: 400, muted: true }] : [])],
          [{ text: name(Math.min(max, 16)), size: fs(16), weight: 600, muted: false }],
          [{ text: n.short ?? name(10), size: fs(14), weight: 600, muted: false }],
        ]
        : [
          [{ text: name(max), size: fs(14), weight, muted: false }],
          ...(n.short ? [] : [[{ text: name(Math.min(max, 14)), size: fs(14), weight, muted: false }]]),
          [{ text: n.short ?? name(8), size: fs(12), weight, muted: false }],
        ];
      const sized = variants.map((lines) => {
        let tw = 0;
        for (const l of lines) { ctx.font = `${l.weight} ${l.size}px ${font}`; tw = Math.max(tw, ctx.measureText(l.text).width + 10); }
        return { lines, w: tw, h: lines.reduce((acc, l) => acc + l.size + 4, 0) };
      });
      const pri = isSel ? 1e7 : isHover ? 9e6 : d.id === centerId ? 8e6 : n.bubble ? 5e6 + (n.rank ?? 0) : 1e6 - order.indexOf(d.id);
      const out = n.labelMode === "out" && n.angle !== undefined;
      labels.push({
        id: d.id, sized,
        item: {
          id: d.id, x: d.x, y: d.y, r: d.r, priority: pri, variants: sized.map((v) => ({ w: v.w, h: v.h })),
          prefer: out ? "out" : "below", dir: out ? { x: Math.cos(n.angle!), y: Math.sin(n.angle!) } : undefined,
          must: isSel || isHover || d.id === centerId,
        },
      });
    }

    // One placement for node and edge labels, around the circles, inside the visible canvas:
    // node labels first (each on its best free side, or shortened, or left for hover — never on
    // another label), then edge labels in what is left.
    const chosen = placeNodeLabels(labels.map((l) => l.item), obstacles, bounds);
    const nodeBoxes: LabelBox[] = [...chosen.entries()].map(([id, p]) => ({ id, x: p.x, y: p.y, w: p.w, h: p.h, priority: 2e9 }));
    const inside = (l: LabelBox) => l.x - l.w / 2 >= bounds.x0 && l.x + l.w / 2 <= bounds.x1 && l.y >= bounds.y0 && l.y + l.h <= bounds.y1;
    const shownEdges = placeLabels([...obstacles, ...nodeBoxes, ...edgeLabels.filter(inside)]);
    for (const l of edgeLabels) {
      if (!shownEdges.has(l.id) || !inside(l)) continue;
      ctx.save();
      ctx.font = `500 ${fs(12)}px ${font}`;
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
      const p = chosen.get(l.id);
      if (!p) continue;
      const v = l.sized[p.variant];
      // An opaque backdrop in the canvas colour: an edge never runs through a label (round 3 P1).
      ctx.save();
      ctx.fillStyle = c("--graph");
      roundRect(ctx, p.x - p.w / 2, p.y - 1, p.w, p.h + 1, 5);
      ctx.fill();
      ctx.restore();
      let y = p.y;
      for (const line of v.lines) {
        ctx.save();
        ctx.font = `${line.weight} ${line.size}px ${font}`;
        ctx.textAlign = "center";
        ctx.textBaseline = "top";
        ctx.fillStyle = l.id === selId ? c("--ink") : line.muted ? c("--muted") : c("--ink-soft");
        ctx.fillText(line.text, p.x, y + 1);
        ctx.restore();
        y += line.size + 4;
      }
    }
    labelBoxes.current = chosen;
    if (animating) schedule();
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [nodes, edges, rings, ringAspect, centerId, mode, byId, pairs, maxW, order, posOf, labelled]);
  // Every frame paints the CURRENT picture: a callback registered once (the resize observer, a
  // timer) reaches the latest draw through this ref, never the one of the render it was made in
  // (tester round 3 P1: a resize or hide/show painted the old picture until Fit).
  const drawRef = useRef(draw);
  drawRef.current = draw;

  const schedule = useCallback(() => {
    if (frame.current !== null || typeof window === "undefined") return;
    const run = () => drawRef.current();
    frame.current = window.requestAnimationFrame ? window.requestAnimationFrame(run) : (setTimeout(run, 16) as unknown as number);
  }, []);

  const setT = useCallback((next: Transform) => {
    t.current = next;
    onZoom?.(next.k);
    schedule();
  }, [onZoom, schedule]);

  /**
   * Frame the picture. `readable` (the opening view, round 2): at 100% when it fits, never below
   * READABLE (so 56 px bubbles and 16 px labels read as designed), centred with padding; Fit (the
   * button, 0) shows everything whatever the scale. Room is kept for the labels past the outer
   * nodes. The legend has its own gutter under the canvas, so nothing is reserved for it.
   */
  const frameView = useCallback((readable: boolean) => {
    // Framed against the canvas as it is NOW (a picture can arrive before the observer reports a
    // new size): a hidden canvas (0 × 0) keeps its last real size.
    const r = wrap.current?.getBoundingClientRect();
    if (r && r.width >= 2 && r.height >= 2) size.current = { w: Math.round(r.width), h: Math.round(r.height) };
    const pts = nodes.map((n) => posOf(n.id)).filter((p): p is Pt => !!p);
    // A new picture is framed in the part the details drawer leaves visible.
    const view = { w: visibleWidth(size.current.w, occludeRef.current), h: size.current.h };
    const ext = frameExtent(nodes, mode, rootScale(), view.w);
    const fitted = fitTransform(pts, view, { extent: ext });
    // The opening view never cuts anything off (tester round 3 P1: a page or a Focus opened
    // clipped, labels clamped over nodes, until Fit): everything is framed, at 100% at most;
    // the layouts are sized to the canvas so that is a readable scale.
    if (!readable || fitted.k <= 1) { setT(fitted); return; }
    setT(zoomAt(fitted, 1 / fitted.k, { x: view.w / 2, y: view.h / 2 }));
  }, [nodes, posOf, mode, setT]);
  const homeRef = useRef<() => void>(() => undefined);
  const fit = useCallback(() => frameView(false), [frameView]);
  const home = useCallback(() => frameView(true), [frameView]);
  homeRef.current = home;

  useImperativeHandle(ref, () => ({
    zoomIn: () => setT(zoomAt(t.current, 1.25, { x: size.current.w / 2, y: size.current.h / 2 })),
    zoomOut: () => setT(zoomAt(t.current, 0.8, { x: size.current.w / 2, y: size.current.h / 2 })),
    fit,
  }), [fit, setT]);

  // A new picture opens at the readable scale; session drags of nodes that are gone are forgotten.
  useEffect(() => {
    for (const id of [...moved.current.keys()]) if (!byId.has(id)) moved.current.delete(id);
    home();
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fitKey]);

  // The details drawer slides over the right edge: the selection AND its label are panned out
  // from under it, never re-zoomed (round 2) — using the room on the left first, so the rest of
  // the picture (the pinned Rules and Learning bubbles) comes out too where it can (round 3).
  useEffect(() => {
    // The Focus centre: the whole ring is re-framed beside the drawer.
    if (mode === "focus" && selectedId && selectedId === centerId) { home(); return; }
    if (!occludeRight || !selectedId) return;
    const boxOf = (id: string) => {
      const p = posOf(id);
      const n = byId.get(id);
      if (!p || !n) return null;
      const s = toScreen(t.current, p);
      const r = n.bubble ? BUBBLE_R : NODE_R;
      const placed = labelBoxes.current.get(id);
      const half = estimateLabel(n, rootScale()) / 2;
      const left = Math.min(s.x - r, placed ? placed.x - placed.w / 2 : s.x - half);
      const right = Math.max(s.x + r, placed ? placed.x + placed.w / 2 : s.x + half);
      return { x: left, w: right - left };
    };
    const sel = boxOf(selectedId);
    if (!sel) return;
    const rest = nodes.map((n) => boxOf(n.id)).filter((b): b is { x: number; w: number } => !!b);
    const dx = revealPan([sel], mode === "overview" ? rest : [], size.current.w - occludeRight);
    if (dx) setT({ ...t.current, x: t.current.x + dx });
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [occludeRight, selectedId]);

  useEffect(() => { schedule(); }, [schedule, draw, selectedId, hover]);

  // Fit to the window on resize, and repaint whenever the canvas is shown again (the Graph tab
  // stays mounted, hidden, under Rules review): a hidden canvas measures 0 and keeps its last
  // real size, so coming back paints the current picture at once (tester round 3 P1).
  useEffect(() => {
    const el = wrap.current;
    if (!el) return undefined;
    const measure = () => {
      const r = el.getBoundingClientRect();
      const w = Math.round(r.width || el.clientWidth);
      const h = Math.round(r.height || el.clientHeight);
      if (w < 2 || h < 2) return "hidden" as const;
      if (w === size.current.w && h === size.current.h) return "same" as const;
      size.current = { w, h };
      return "changed" as const;
    };
    const react = () => {
      const m = measure();
      if (m === "hidden") return;
      if (m === "changed") homeRef.current();
      // Paint now: the bitmap was cleared or stretched by the change.
      if (frame.current !== null && typeof window !== "undefined") { window.cancelAnimationFrame?.(frame.current); frame.current = null; }
      drawRef.current();
    };
    react();
    const onVisible = () => { if (document.visibilityState !== "hidden") react(); };
    document.addEventListener("visibilitychange", onVisible);
    if (typeof ResizeObserver === "undefined") return () => document.removeEventListener("visibilitychange", onVisible);
    const ro = new ResizeObserver(react);
    ro.observe(el);
    return () => { ro.disconnect(); document.removeEventListener("visibilitychange", onVisible); };
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
    // A press or a drag starts: no tooltip left behind from where the pointer was (P1-13).
    if (hoverRef.current) setHover(null);
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
      const key = edgeKey(near.e);
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
    // A click on empty canvas clears the selection (and closes its details).
    else if (gst && !gst.moved && gst.kind === "pan" && pointers.current.size === 0) onClear?.();
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
        <div className="kg-tip" role="tooltip" data-testid="kg-tip"
             style={tipPlace(hover, size.current)}>
          <span className="kg-tip-title">{tip.title}</span>
          {tip.line ? <span className="kg-tip-line">{tip.line}</span> : null}
        </div>
      ) : null}
    </div>
  );
});

/** The tooltip beside the pointer, flipped to stay inside the canvas near its right and bottom edges. */
export function tipPlace(at: { x: number; y: number }, view: { w: number; h: number }): React.CSSProperties {
  const right = at.x > view.w - 300;
  const below = at.y < view.h - 90;
  return {
    left: right ? undefined : at.x + 14, right: right ? Math.max(8, view.w - at.x + 14) : undefined,
    top: below ? at.y + 14 : undefined, bottom: below ? undefined : Math.max(8, view.h - at.y + 14),
  };
}

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
