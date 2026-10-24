/**
 * THE GRAPH'S GEOMETRY (KNOWLEDGE_GRAPH_PRODUCT_SPEC.md §5.3): pure, deterministic, tested
 * (tests/kg-layout.test.ts), like lib/workflow-graph.ts for the workflow canvas.
 *
 * World coordinates are what the layouts produce; the canvas maps them to the screen with one
 * transform `{k, x, y}` (screen = world·k + offset). Nodes and labels are drawn at a FIXED screen
 * size (a 28 px circle, 56 px bubbles, 14/16 px labels), so zooming spreads or packs the graph
 * without ever making a node bigger than another: size never encodes count or kind.
 *
 * Nothing here reads the DOM or a colour. A kind maps to a kg.css custom property NAME
 * (`--kg-class`), which the canvas resolves from the element's computed style, so the only
 * literal colours stay in kg.css (§6.4).
 */

export type Pt = { x: number; y: number };
export type Transform = { k: number; x: number; y: number };
export type Viewport = { w: number; h: number };

/** Screen radii (§5.3): every node 28 px across, area and layer bubbles 56 px. */
export const NODE_R = 14;
export const BUBBLE_R = 28;
export const ZOOM_MIN = 0.2;
export const ZOOM_MAX = 4;
export const FIT_PADDING = 48;
/** A fit never blows a two-node view up past this: the picture stays a graph, not two coins. */
export const FIT_MAX = 1.25;
export const ARROW_LEN = 15;
export const ARROW_WIDTH = 10;
export const EDGE_MIN = 1.5;
export const EDGE_MAX = 3;
export const LABEL_MAX = 28;
/** Ceilings (§5.3): the canvas never draws more. */
export const OVERVIEW_CEILING = 60;
export const FOCUS_CEILING = 200;

// ── determinism ─────────────────────────────────────────────────────────────────────────────

/** FNV-1a, 32 bit: the seed of a key. Same key, same number, on every machine. */
export function hashSeed(text: string): number {
  let h = 0x811c9dc5;
  for (let i = 0; i < text.length; i += 1) {
    h ^= text.charCodeAt(i);
    h = Math.imul(h, 0x01000193);
  }
  return h >>> 0;
}

/** mulberry32: a tiny seeded PRNG in [0, 1). Never Math.random — the same data gives the same picture. */
export function seeded(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

// ── layouts ─────────────────────────────────────────────────────────────────────────────────

export type LayoutNode = { id: string; key?: string; /** The label's box, screen px: the layout keeps them apart. */ w?: number; h?: number };
export type LayoutEdge = { src: string; dst: string; weight: number };

/** A bubble's label box (two lines under a 56 px bubble) when the caller does not measure it. */
export const OVERVIEW_BOX = { w: 190, h: 56 + 44 };

/**
 * Whole repo (§5.3, tester round 1 P0-1): every bubble AND its two-line label gets its own room —
 * nothing overlaps at Fit. World units are screen px at 100%, so the boxes are the labels' real
 * size. Steps, all deterministic (ids sorted; the seed is the keys):
 *   1. the most connected area in the middle, the rest on rings around it in connection order;
 *   2. a short seeded force pass pulls connected areas together;
 *   3. rectangle separation: no two label boxes overlap;
 *   4. the Rules and Learning bubbles pinned in a column to the right.
 */
export function overviewLayout(nodes: LayoutNode[], edges: LayoutEdge[], pinned: string[] = [], aspect = 1.75): Map<string, Pt> {
  // The canvas's width over its height: the picture takes the same shape, so it opens at 100%.
  // The pinned column takes a strip on the right; the rest of the picture fills what is left.
  const a = Math.min(3, Math.max(0.6, aspect - (pinned.length ? 0.55 : 0)));
  const pin = new Set(pinned);
  const free = nodes.filter((n) => !pin.has(n.id)).map((n) => ({ id: n.id, key: n.key ?? n.id, w: n.w ?? OVERVIEW_BOX.w, h: n.h ?? OVERVIEW_BOX.h }))
    .sort((a, b) => (a.key < b.key ? -1 : a.key > b.key ? 1 : 0));
  const n = free.length;
  const index = new Map(free.map((f, i) => [f.id, i]));
  const springs = edges
    .filter((e) => index.has(e.src) && index.has(e.dst) && e.src !== e.dst)
    .map((e) => ({ a: index.get(e.src)!, b: index.get(e.dst)!, w: Math.log10(Math.max(1, e.weight)) + 1 }))
    .sort((x, y) => x.a - y.a || x.b - y.b || x.w - y.w);
  const degree = new Float64Array(n);
  for (const s of springs) { degree[s.a] += s.w; degree[s.b] += s.w; }
  // 1. rings, the most connected first (ties by key).
  const order = free.map((_, i) => i).sort((a, b) => degree[b] - degree[a] || a - b);
  const pos: Pt[] = free.map(() => ({ x: 0, y: 0 }));
  let placed = 0;
  let ring = 0;
  while (placed < order.length) {
    if (ring === 0) { pos[order[0]] = { x: 0, y: 0 }; placed = 1; ring = 1; continue; }
    const r = ring * 210;
    const cap = Math.max(6, Math.floor((2 * Math.PI * r) / 230));
    const take = order.slice(placed, placed + cap);
    const turn = (hashSeed(free[take[0]].key) / 4294967296) * Math.PI * 2;
    take.forEach((idx, j) => {
      const a = turn + (j / take.length) * Math.PI * 2;
      pos[idx] = { x: Math.cos(a) * r * 1.35, y: Math.sin(a) * r * 0.85 };
    });
    placed += take.length;
    ring += 1;
  }
  // 2. a short force pass: connected areas drift together, everything is pulled to the middle.
  const K = 200;
  let temp = 60;
  for (let it = 0; it < 120; it += 1) {
    const dx = new Float64Array(n);
    const dy = new Float64Array(n);
    for (let i = 0; i < n; i += 1) {
      for (let j = i + 1; j < n; j += 1) {
        let vx = pos[i].x - pos[j].x;
        let vy = pos[i].y - pos[j].y;
        let d = Math.hypot(vx, vy);
        if (d < 0.01) { vx = 0.01 * (i - j); vy = 0.01; d = Math.hypot(vx, vy); }
        const f = (K * K) / d;
        dx[i] += (vx / d) * f; dy[i] += (vy / d) * f;
        dx[j] -= (vx / d) * f; dy[j] -= (vy / d) * f;
      }
    }
    for (const s of springs) {
      const vx = pos[s.a].x - pos[s.b].x;
      const vy = pos[s.a].y - pos[s.b].y;
      const d = Math.max(0.01, Math.hypot(vx, vy));
      const f = ((d * d) / K) * 0.08 * s.w;
      dx[s.a] -= (vx / d) * f; dy[s.a] -= (vy / d) * f;
      dx[s.b] += (vx / d) * f; dy[s.b] += (vy / d) * f;
    }
    for (let i = 0; i < n; i += 1) {
      // Wider than tall, like the canvas it fills.
      dx[i] -= pos[i].x * 0.9;
      dy[i] -= pos[i].y * 0.9 * a * a;
      const d = Math.hypot(dx[i], dy[i]);
      if (d > 0) {
        const step = Math.min(d, temp);
        pos[i].x += (dx[i] / d) * step;
        pos[i].y += (dy[i] / d) * step;
      }
    }
    temp = Math.max(2, temp * 0.97);
  }
  // 3. no two label boxes overlap.
  // 3. no two label boxes overlap — then pulled in until they nearly touch (round 2: the picture
  //    opens at a readable 100%, not shrunk to fit a loose spread).
  const boxes = free.map((f) => ({ w: f.w + 24, h: f.h + 20 }));
  separateBoxes(pos, boxes);
  for (let pass = 0; pass < 24; pass += 1) {
    const c = bounds(pos);
    const mx = (c.minX + c.maxX) / 2;
    const my = (c.minY + c.maxY) / 2;
    for (const p of pos) { p.x = mx + (p.x - mx) * 0.94; p.y = my + (p.y - my) * 0.94; }
    separateBoxes(pos, boxes);
  }
  const out = new Map<string, Pt>();
  const bb = bounds(pos);
  const cx = (bb.minX + bb.maxX) / 2;
  const cy = (bb.minY + bb.maxY) / 2;
  free.forEach((f, i) => out.set(f.id, { x: round(pos[i].x - cx), y: round(pos[i].y - cy) }));
  // 4. the layer bubbles, pinned right, in the order given.
  const present = new Map(nodes.map((nd) => [nd.id, nd]));
  const pins = pinned.filter((id) => present.has(id));
  const widest = Math.max(OVERVIEW_BOX.w, ...free.map((f) => f.w));
  const right = (n ? (bb.maxX - cx) : 0) + widest / 2 + Math.max(OVERVIEW_BOX.w, ...pins.map((id) => present.get(id)!.w ?? OVERVIEW_BOX.w)) / 2 + 40;
  pins.forEach((id, i) => {
    const y = (i - (pins.length - 1) / 2) * (OVERVIEW_BOX.h + 70);
    out.set(id, { x: round(n ? right : 0), y: round(y) });
  });
  return out;
}

/** Push apart any two boxes (centred on the node's x, from the bubble's top down through its
 *  label) that overlap, along the axis that needs the smaller move. Deterministic passes. */
export function separateBoxes(pos: Pt[], box: Array<{ w: number; h: number }>, passes = 200): void {
  for (let p = 0; p < passes; p += 1) {
    let moved = false;
    for (let i = 0; i < pos.length; i += 1) {
      for (let j = i + 1; j < pos.length; j += 1) {
        const ox = (box[i].w + box[j].w) / 2 - Math.abs(pos[i].x - pos[j].x);
        const oy = (box[i].h + box[j].h) / 2 - Math.abs(pos[i].y - pos[j].y);
        if (ox <= 0 || oy <= 0) continue;
        moved = true;
        if (ox * 0.6 < oy) {
          const s = (pos[i].x < pos[j].x || (pos[i].x === pos[j].x && i < j) ? -1 : 1) * (ox / 2 + 0.5);
          pos[i].x += s; pos[j].x -= s;
        } else {
          const s = (pos[i].y < pos[j].y || (pos[i].y === pos[j].y && i < j) ? -1 : 1) * (oy / 2 + 0.5);
          pos[i].y += s; pos[j].y -= s;
        }
      }
    }
    if (!moved) return;
  }
}

/** True when no two boxes overlap (what the overview promises; tests hold it to it). */
export function boxesClear(pos: Pt[], box: Array<{ w: number; h: number }>): boolean {
  for (let i = 0; i < pos.length; i += 1) {
    for (let j = i + 1; j < pos.length; j += 1) {
      if ((box[i].w + box[j].w) / 2 - Math.abs(pos[i].x - pos[j].x) > 0.5
        && (box[i].h + box[j].h) / 2 - Math.abs(pos[i].y - pos[j].y) > 0.5) return false;
    }
  }
  return true;
}

function round(v: number): number {
  return Math.round(v * 100) / 100;
}

// ── the Whole-repo selection: what one overview draws ──────────────────────────────────────

/** At most this many areas are drawn at once; the rest fold into one "N more areas" bubble. */
export const OVERVIEW_AREAS = 10;
export const MORE_ID = "more:areas";

export type OverviewPick<N, E> = {
  nodes: N[];
  /** Edges among what is drawn, those to folded areas summed onto the "more" bubble. */
  edges: E[];
  /** The areas folded into the "more" bubble (empty when everything fits). */
  folded: N[];
  page: number;
  pages: number;
  /** Which page each area is drawn on. */
  pageOf: Map<string, number>;
};

/**
 * Whole repo, readable at any project size (tester round 1 P0-1): the areas are ranked by size
 * (classes, then methods) and connections; page `page` draws OVERVIEW_AREAS of them — all of them
 * when there are at most OVERVIEW_AREAS + 2 — and folds the rest into one bubble whose edges are
 * the sums of theirs. Earlier pages fold into the same bubble, so every area is one press away.
 */
export function pickOverview<N extends { id: string; counts?: Record<string, number> }, E extends { src: string; dst: string; kind: string; weight: number; label: string | null }>(
  areas: N[], layers: N[], edges: E[], page: number, makeMore: (folded: N[], total: number) => N,
  perPage: number = OVERVIEW_AREAS,
): OverviewPick<N, E> {
  const PER = Math.max(3, Math.min(OVERVIEW_AREAS, Math.floor(perPage)));
  const size = (n: N) => (n.counts?.classes ?? 0) * 4 + (n.counts?.methods ?? n.counts?.functions ?? 0) + (n.counts?.packages ?? 0);
  const weight = new Map<string, number>();
  for (const e of edges) {
    weight.set(e.src, (weight.get(e.src) ?? 0) + e.weight);
    weight.set(e.dst, (weight.get(e.dst) ?? 0) + e.weight);
  }
  const ranked = [...areas].sort((a, b) => size(b) - size(a) || (weight.get(b.id) ?? 0) - (weight.get(a.id) ?? 0) || (a.id < b.id ? -1 : 1));
  const fits = ranked.length <= PER + 2;
  const pages = fits ? 1 : Math.ceil(ranked.length / PER);
  const p = Math.max(0, Math.min(page, pages - 1));
  const shown = fits ? ranked : ranked.slice(p * PER, (p + 1) * PER);
  const shownIds = new Set(shown.map((n) => n.id));
  const folded = ranked.filter((n) => !shownIds.has(n.id));
  const more = folded.length ? makeMore(folded, ranked.length) : null;
  const into = (id: string) => (shownIds.has(id) || layers.some((l) => l.id === id) ? id : more ? more.id : null);
  const merged = new Map<string, E>();
  for (const e of edges) {
    const s = into(e.src);
    const d = into(e.dst);
    if (!s || !d || s === d) continue;
    const k = `${s}\u0000${d}\u0000${e.kind}`;
    const prev = merged.get(k);
    if (!prev) { merged.set(k, { ...e, src: s, dst: d }); continue; }
    const w = prev.weight + e.weight;
    merged.set(k, { ...prev, weight: w, label: relabel(prev.label ?? e.label, w) });
  }
  const pageOf = new Map(ranked.map((n, i) => [n.id, fits ? 0 : Math.floor(i / PER)]));
  return { nodes: [...shown, ...(more ? [more] : []), ...layers], edges: [...merged.values()], folded, page: p, pages, pageOf };
}

/** "16,749 calls" with a new number: the API's own noun kept. */
function relabel(label: string | null, weight: number): string | null {
  if (!label) return null;
  const noun = label.replace(/^[\d,]+\s*/, "");
  const one = noun.replace(/s$/, "");
  return `${weight.toLocaleString("en-US")} ${weight === 1 ? one : noun}`;
}

/**
 * Which edges a Whole-repo view draws, and which carry their weight label (P0-1: no hairball).
 * Each node keeps its `perNode` strongest edges (in or out), plus every edge of the selection;
 * the `labelled` strongest drawn edges show "N calls" (the rest on hover or selection).
 */
export function pickEdges<E extends { src: string; dst: string; kind: string; weight: number }>(
  edges: E[], selected: string | null, opts: { perNode?: number; labelled?: number } = {},
): { drawn: E[]; labelled: Set<E> } {
  const per = opts.perNode ?? 2;
  const keep = new Set<E>();
  const incident = new Map<string, E[]>();
  for (const e of edges) {
    incident.set(e.src, [...(incident.get(e.src) ?? []), e]);
    incident.set(e.dst, [...(incident.get(e.dst) ?? []), e]);
    if (selected && (e.src === selected || e.dst === selected)) keep.add(e);
  }
  // Each bubble's `per` strongest connections, in or out.
  for (const list of incident.values()) {
    [...list].sort((a, b) => b.weight - a.weight || (a.src + a.dst < b.src + b.dst ? -1 : 1)).slice(0, per).forEach((e) => keep.add(e));
  }
  const drawn = edges.filter((e) => keep.has(e));
  const labelled = new Set([...drawn].filter((e) => e.weight > 1).sort((a, b) => b.weight - a.weight).slice(0, opts.labelled ?? 8));
  return { drawn, labelled };
}

export type FocusNode = { id: string; hop: number };

/** The first ring's least radius, the room between rings, and the arc one radial label needs. */
export const RING_FIRST = 150;
export const RING_GAP = 150;
/** Arc per neighbour: a 28 px node and room either side, so neither nodes nor their radial labels touch. */
export const LABEL_ARC = 34;

/**
 * Focus (§5.4, tester round 1 P1-4): radial by hop, ONE ring per hop, each ring large enough that
 * every node's label fits beside its neighbours' (LABEL_ARC px of arc each, at 100%). The first
 * hop is ordered by its edge weight to the centre (heaviest at the top, clockwise); a later hop
 * sits at the angle of what it connects to. Returns positions, the ring radii, each node's angle
 * (for its radial label) and its ring.
 */
export function focusLayout(centerId: string, nodes: FocusNode[], edges: LayoutEdge[]): {
  pos: Map<string, Pt>; rings: number[]; angle: Map<string, number>; ring: Map<string, number>;
} {
  const pos = new Map<string, Pt>([[centerId, { x: 0, y: 0 }]]);
  const rings: number[] = [];
  const angle = new Map<string, number>();
  const ringOf = new Map<string, number>();
  const byHop = new Map<number, FocusNode[]>();
  for (const nd of nodes) {
    if (nd.id === centerId) continue;
    const h = Math.max(1, nd.hop);
    byHop.set(h, [...(byHop.get(h) ?? []), nd]);
  }
  const neighbours = new Map<string, Array<{ id: string; w: number }>>();
  for (const e of edges) {
    neighbours.set(e.src, [...(neighbours.get(e.src) ?? []), { id: e.dst, w: e.weight }]);
    neighbours.set(e.dst, [...(neighbours.get(e.dst) ?? []), { id: e.src, w: e.weight }]);
  }
  let prev = 0;
  const hops = [...byHop.keys()].sort((a, b) => a - b);
  for (const h of hops) {
    const list = byHop.get(h)!;
    let ordered: Array<{ id: string; want: number }>;
    if (h === hops[0]) {
      const w = (id: string) => (neighbours.get(id) ?? []).filter((x) => x.id === centerId).reduce((s, x) => s + x.w, 0);
      ordered = [...list].sort((a, b) => w(b.id) - w(a.id) || (a.id < b.id ? -1 : 1)).map((nd) => ({ id: nd.id, want: 0 }));
    } else {
      ordered = list.map((nd) => {
        const parents = (neighbours.get(nd.id) ?? []).filter((x) => angle.has(x.id));
        const want = parents.length ? circularMean(parents.map((p) => angle.get(p.id)!)) : (hashSeed(nd.id) / 4294967296) * Math.PI * 2;
        return { id: nd.id, want: (want + Math.PI * 4) % (Math.PI * 2) };
      }).sort((a, b) => a.want - b.want || (a.id < b.id ? -1 : 1));
    }
    const need = (ordered.length * LABEL_ARC) / (2 * Math.PI);
    const radius = round(Math.max(prev ? prev + RING_GAP : RING_FIRST, need));
    const step = (Math.PI * 2) / ordered.length;
    const start = h === hops[0] ? -Math.PI / 2 : ordered[0].want;
    ordered.forEach((t, j) => {
      const a = start + j * step;
      angle.set(t.id, a);
      ringOf.set(t.id, rings.length);
      pos.set(t.id, { x: round(Math.cos(a) * radius), y: round(Math.sin(a) * radius) });
    });
    rings.push(radius);
    prev = radius;
  }
  return { pos, rings, angle, ring: ringOf };
}

/** A radial label's rotation and anchor: along its ray, upright on the left half. */
export function radialLabel(a: number, outward: boolean): { rotate: number; align: "left" | "right" } {
  const left = Math.cos(a) < -1e-9;
  const rotate = left ? a + Math.PI : a;
  const align = outward ? (left ? "right" : "left") : (left ? "left" : "right");
  return { rotate, align };
}

/** Where a node with many arrows coming in has its arrowheads: spread on a wider rim so each
 *  stays a distinct 15 × 10 px head instead of a blob (tester round 1 P1-3). */
export function arrowRim(r: number, fanIn: number): number {
  if (fanIn <= 6) return r;
  return r + Math.min(36, ((fanIn - 6) * ARROW_WIDTH) / (2 * Math.PI) + 4);
}

function circularMean(angles: number[]): number {
  const s = angles.reduce((acc, a) => acc + Math.sin(a), 0);
  const c = angles.reduce((acc, a) => acc + Math.cos(a), 0);
  return Math.atan2(s, c);
}

/** Where a node that arrived live goes: beside the node it hangs on, in the first free spot on a
 *  small ring around it — never a re-layout of what is already drawn (§4.4). */
export function placeNear(anchor: Pt, taken: Pt[], seedKey: string): Pt {
  const start = (hashSeed(seedKey) / 4294967296) * Math.PI * 2;
  for (let ring = 1; ring <= 6; ring += 1) {
    const r = ring * 70;
    const slots = Math.max(6, Math.floor((2 * Math.PI * r) / 60));
    for (let s = 0; s < slots; s += 1) {
      const a = start + (s / slots) * Math.PI * 2;
      const p = { x: round(anchor.x + Math.cos(a) * r), y: round(anchor.y + Math.sin(a) * r) };
      if (taken.every((t) => Math.hypot(t.x - p.x, t.y - p.y) >= 56)) return p;
    }
  }
  return { x: anchor.x + 70, y: anchor.y + 70 };
}

// ── the view transform ──────────────────────────────────────────────────────────────────────

export function clampZoom(k: number): number {
  return Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, k));
}

export type Box = { minX: number; minY: number; maxX: number; maxY: number };

export function bounds(points: Pt[]): Box {
  if (!points.length) return { minX: 0, minY: 0, maxX: 0, maxY: 0 };
  let minX = Infinity; let minY = Infinity; let maxX = -Infinity; let maxY = -Infinity;
  for (const p of points) {
    if (p.x < minX) minX = p.x;
    if (p.y < minY) minY = p.y;
    if (p.x > maxX) maxX = p.x;
    if (p.y > maxY) maxY = p.y;
  }
  return { minX, minY, maxX, maxY };
}

/**
 * Fit the bounding box in the viewport with `padding` screen px on each side (§5.3: on load, on
 * data change and on resize). The node and its label extend past its centre by a fixed SCREEN
 * amount, which is reserved from the padding first, so a label at the edge is never cut.
 */
export function fitTransform(points: Pt[], view: Viewport, opts: { padding?: number; extent?: { x: number; top: number; bottom: number } } = {}): Transform {
  const padding = opts.padding ?? FIT_PADDING;
  const ext = opts.extent ?? { x: 70, top: BUBBLE_R, bottom: 46 };
  const bb = bounds(points);
  const availW = Math.max(40, view.w - 2 * padding - 2 * ext.x);
  const availH = Math.max(40, view.h - 2 * padding - ext.top - ext.bottom);
  const bw = bb.maxX - bb.minX;
  const bh = bb.maxY - bb.minY;
  const k = clampZoom(Math.min(FIT_MAX, bw > 0 ? availW / bw : FIT_MAX, bh > 0 ? availH / bh : FIT_MAX));
  const cx = (bb.minX + bb.maxX) / 2;
  const cy = (bb.minY + bb.maxY) / 2;
  // Centre the content box, shifted so the label space below is accounted for.
  return { k, x: view.w / 2 - cx * k, y: view.h / 2 - cy * k + (ext.top - ext.bottom) / 2 };
}

/** Zoom by `factor` keeping the world point under `at` (screen) where it is: wheel and pinch to the cursor. */
export function zoomAt(t: Transform, factor: number, at: Pt): Transform {
  const k = clampZoom(t.k * factor);
  const r = k / t.k;
  return { k, x: at.x - (at.x - t.x) * r, y: at.y - (at.y - t.y) * r };
}

export function toScreen(t: Transform, p: Pt): Pt {
  return { x: p.x * t.k + t.x, y: p.y * t.k + t.y };
}

export function toWorld(t: Transform, p: Pt): Pt {
  return { x: (p.x - t.x) / t.k, y: (p.y - t.y) / t.k };
}

// ── edges ───────────────────────────────────────────────────────────────────────────────────

/** Thin and uniform (§5.3): 1.5 px, rising on a log scale to 3 px for the heaviest weight in view. */
export function edgeWidth(weight: number, maxWeight: number): number {
  if (!(weight > 1) || !(maxWeight > 1)) return EDGE_MIN;
  const t = Math.log(weight) / Math.log(maxWeight);
  return Math.min(EDGE_MAX, Math.max(EDGE_MIN, EDGE_MIN + (EDGE_MAX - EDGE_MIN) * t));
}

/** The pairs drawn as two curves: an edge whose reverse is also in view. */
export function bidirectional(edges: Array<{ src: string; dst: string }>): Set<string> {
  const keys = new Set(edges.map((e) => `${e.src}\u0000${e.dst}`));
  const out = new Set<string>();
  for (const e of edges) if (e.src !== e.dst && keys.has(`${e.dst}\u0000${e.src}`)) out.add(`${e.src}\u0000${e.dst}`);
  return out;
}

export const BEND = 0.16;

export type EdgeGeometry = {
  /** Where the line leaves the source rim. */
  start: Pt;
  /** The quadratic control point (equal to the midpoint for a straight edge). */
  ctrl: Pt;
  /** Where the line stops: the arrowhead's base. */
  end: Pt;
  /** The arrowhead: its tip touches the destination rim; base corners left and right. */
  tip: Pt;
  left: Pt;
  right: Pt;
  /** Where the edge's label sits. */
  label: Pt;
};

/**
 * One directed edge in SCREEN space between two node centres with screen radii. `bend` curves it
 * (a fraction of its length, to the left of travel), for the two halves of a bidirectional pair.
 * The arrowhead is ARROW_LEN long and ARROW_WIDTH wide, filled, ending exactly at the rim.
 */
export function edgeGeometry(p: Pt, q: Pt, rSrc: number, rDst: number, bend = 0): EdgeGeometry {
  const dx = q.x - p.x;
  const dy = q.y - p.y;
  const len = Math.max(0.001, Math.hypot(dx, dy));
  const nx = -dy / len;
  const ny = dx / len;
  const mid = { x: (p.x + q.x) / 2, y: (p.y + q.y) / 2 };
  const ctrl = { x: mid.x + nx * bend * len, y: mid.y + ny * bend * len };
  const dirIn = unit({ x: q.x - ctrl.x, y: q.y - ctrl.y });
  const dirOut = unit({ x: ctrl.x - p.x, y: ctrl.y - p.y });
  const tip = { x: q.x - dirIn.x * rDst, y: q.y - dirIn.y * rDst };
  const base = { x: tip.x - dirIn.x * ARROW_LEN, y: tip.y - dirIn.y * ARROW_LEN };
  const half = ARROW_WIDTH / 2;
  const left = { x: base.x - dirIn.y * half, y: base.y + dirIn.x * half };
  const right = { x: base.x + dirIn.y * half, y: base.y - dirIn.x * half };
  const start = { x: p.x + dirOut.x * rSrc, y: p.y + dirOut.y * rSrc };
  // A quadratic's point at t = 0.5 is (p + 2c + q) / 4.
  const label = bend ? { x: (p.x + 2 * ctrl.x + q.x) / 4, y: (p.y + 2 * ctrl.y + q.y) / 4 } : mid;
  return { start, ctrl, end: base, tip, left, right, label };
}

function unit(v: Pt): Pt {
  const d = Math.hypot(v.x, v.y) || 1;
  return { x: v.x / d, y: v.y / d };
}

/** Distance from a point to an edge's drawn curve (sampled): what the pointer is over. */
export function distanceToEdge(pt: Pt, g: EdgeGeometry): number {
  let best = Infinity;
  let prev = g.start;
  for (let i = 1; i <= 16; i += 1) {
    const t = i / 16;
    const a = (1 - t) * (1 - t);
    const b = 2 * (1 - t) * t;
    const c = t * t;
    const cur = { x: a * g.start.x + b * g.ctrl.x + c * g.tip.x, y: a * g.start.y + b * g.ctrl.y + c * g.tip.y };
    best = Math.min(best, segDistance(pt, prev, cur));
    prev = cur;
  }
  return best;
}

function segDistance(p: Pt, a: Pt, b: Pt): number {
  const vx = b.x - a.x;
  const vy = b.y - a.y;
  const l2 = vx * vx + vy * vy;
  const t = l2 ? Math.max(0, Math.min(1, ((p.x - a.x) * vx + (p.y - a.y) * vy) / l2)) : 0;
  return Math.hypot(p.x - (a.x + t * vx), p.y - (a.y + t * vy));
}

// ── hit testing and labels ──────────────────────────────────────────────────────────────────

export type ScreenNode = { id: string; x: number; y: number; r: number };

/** The node under a screen point (the nearest whose rim, plus a 4 px grace, contains it). */
export function nodeAt(pt: Pt, nodes: ScreenNode[]): string | null {
  let best: string | null = null;
  let bestD = Infinity;
  for (const n of nodes) {
    const d = Math.hypot(pt.x - n.x, pt.y - n.y);
    if (d <= n.r + 4 && d < bestD) { best = n.id; bestD = d; }
  }
  return best;
}

/** Middle-ellipsised to `max` characters (§5.3): the start and the end both stay readable. */
export function middleEllipsis(text: string, max = LABEL_MAX): string {
  if (text.length <= max) return text;
  const keep = max - 1;
  const head = Math.ceil(keep / 2);
  return `${text.slice(0, head)}…${text.slice(text.length - (keep - head))}`;
}

export type LabelBox = { id: string; x: number; y: number; w: number; h: number; priority: number };

/** Greedy label placement: highest priority first, a label that would overlap one already placed
 *  is left for hover and zoom. Returns the ids whose labels are drawn. */
export function placeLabels(boxes: LabelBox[], gap = 2): Set<string> {
  const placed: LabelBox[] = [];
  const out = new Set<string>();
  for (const b of [...boxes].sort((a, c) => c.priority - a.priority || (a.id < c.id ? -1 : 1))) {
    const hit = placed.some((p) => b.x - b.w / 2 - gap < p.x + p.w / 2 && b.x + b.w / 2 + gap > p.x - p.w / 2
      && b.y - gap < p.y + p.h && b.y + b.h + gap > p.y);
    if (!hit) { placed.push(b); out.add(b.id); }
  }
  return out;
}

/** Keyboard order (§5.3: Tab cycles nodes in weight order): heaviest total edge weight first. */
export function weightOrder(ids: string[], edges: LayoutEdge[], first?: string | null): string[] {
  const w = new Map<string, number>();
  for (const e of edges) {
    w.set(e.src, (w.get(e.src) ?? 0) + e.weight);
    w.set(e.dst, (w.get(e.dst) ?? 0) + e.weight);
  }
  const sorted = [...ids].sort((a, b) => (w.get(b) ?? 0) - (w.get(a) ?? 0) || (a < b ? -1 : a > b ? 1 : 0));
  if (first && sorted.includes(first)) return [first, ...sorted.filter((id) => id !== first)];
  return sorted;
}

// ── kinds: colour token, name, size ─────────────────────────────────────────────────────────

/** The kg.css custom property a node is filled with. Never a literal colour. */
export function kindToken(kind: string, symbolKind?: string | null, id?: string): string {
  if (kind === "layer") return id === "layer:rules" ? "--kg-layer-rules" : "--kg-layer-learning";
  if (kind === "area" || kind === "package") return "--kg-layer-code";
  if (kind === "symbol" || kind === "class" || kind === "interface" || kind === "enum" || kind === "record"
      || kind === "method" || kind === "function") {
    const s = kind === "symbol" ? (symbolKind ?? "class") : kind;
    return ({ class: "--kg-class", interface: "--kg-interface", enum: "--kg-enum", record: "--kg-record",
              method: "--kg-method", function: "--kg-function" } as Record<string, string>)[s] ?? "--kg-class";
  }
  const map: Record<string, string> = {
    rule: "--kg-rule", incident: "--kg-incident", root_cause: "--kg-root-cause", fix: "--kg-fix",
    precedent: "--kg-precedent", investigation: "--kg-investigation", test_failure: "--kg-test-failure",
    workaround: "--kg-workaround", review_finding: "--kg-review-finding", decision: "--kg-decision",
    fact: "--kg-fact", endpoint: "--kg-endpoint", table: "--kg-table", file: "--kg-file", module: "--kg-file",
    run: "--kg-anchor", pull_request: "--kg-anchor", release: "--kg-anchor", spec: "--kg-anchor",
    runbook: "--kg-anchor", alert_rule: "--kg-anchor", topic: "--kg-anchor",
  };
  return map[kind] ?? "--kg-function";
}

const KIND_NAMES: Record<string, string> = {
  area: "Area", package: "Package", class: "Class", interface: "Interface", enum: "Enum", record: "Record",
  method: "Method", function: "Function", rule: "Rule", incident: "Incident", root_cause: "Root cause",
  fix: "Fix", precedent: "Precedent", investigation: "Investigation", test_failure: "Test failure",
  workaround: "Workaround", review_finding: "Review finding", decision: "Decision", fact: "Fact",
  endpoint: "Endpoint", table: "Table", run: "Run", pull_request: "Pull request", release: "Release",
  spec: "Spec", runbook: "Runbook", alert_rule: "Alert rule", topic: "Topic", file: "File", module: "Module",
  config_key: "Config key", queue: "Queue", service: "Service", resource: "Resource", deployment: "Deployment",
  datastore: "Datastore", external: "External", repo: "Repository", group: "Group",
};

/** The kind in words (tooltip, legend, panel: colour is never the only signal, §5.10). */
export function kindName(kind: string, symbolKind?: string | null, id?: string): string {
  if (kind === "layer") return id === "layer:rules" ? "Rules layer" : "Learning layer";
  if (kind === "symbol") return KIND_NAMES[symbolKind ?? "class"] ?? "Symbol";
  return KIND_NAMES[kind] ?? kind.replace(/_/g, " ").replace(/^\w/, (c) => c.toUpperCase());
}

/** The legend key of a node: one row per colour+name present in the view. */
export function legendKey(kind: string, symbolKind?: string | null, id?: string): string {
  if (kind === "symbol") return symbolKind ?? "class";
  if (kind === "layer") return id ?? "layer";
  return kind;
}

/** Bubbles (areas, packages at the overview level, the two layer bubbles) are 56 px; all else 28. */
export function isBubble(kind: string): boolean {
  return kind === "area" || kind === "layer" || kind === "package";
}

/** Edges that read as the learning loop take the target's layer colour (§5.3). */
export const LOOP_EDGES = new Set(["consulted", "reused", "approved_by"]);

export function layerToken(layer: string): string {
  return layer === "rules" ? "--kg-layer-rules" : layer === "learning" ? "--kg-layer-learning" : "--kg-layer-code";
}

export type LabelChoice = { id: string; x: number; y: number; priority: number; variants: Array<{ w: number; h: number }> };

/**
 * Every node keeps a label (tester round 2: "never draw an unlabelled node"). Highest priority
 * first, each label takes the first of its variants (full name, then shorter middle-ellipsised
 * names, then without its second line) that clears the circles and the labels already placed;
 * when none does, the shortest is drawn all the same. Returns the variant index per id.
 */
export function placeNodeLabels(items: LabelChoice[], obstacles: LabelBox[], gap = 2): Map<string, number> {
  const placed: LabelBox[] = [];
  const out = new Map<string, number>();
  const hits = (b: LabelBox, others: LabelBox[], self: string) => others.some((p) => p.id !== self && p.id !== `o:${self}`
    && b.x - b.w / 2 - gap < p.x + p.w / 2 && b.x + b.w / 2 + gap > p.x - p.w / 2 && b.y - gap < p.y + p.h && b.y + b.h + gap > p.y);
  for (const it of [...items].sort((a, b) => b.priority - a.priority || (a.id < b.id ? -1 : 1))) {
    let chosen = it.variants.length - 1;
    for (let v = 0; v < it.variants.length; v += 1) {
      const box = { id: it.id, x: it.x, y: it.y, w: it.variants[v].w, h: it.variants[v].h, priority: it.priority };
      if (!hits(box, obstacles, it.id) && !hits(box, placed, it.id)) { chosen = v; break; }
    }
    out.set(it.id, chosen);
    placed.push({ id: it.id, x: it.x, y: it.y, w: it.variants[chosen].w, h: it.variants[chosen].h, priority: it.priority });
  }
  return out;
}

// ── Focus that stays readable at any size (tester round 2: 2 hops / Show 200) ─────────────

/** At most this many neighbours are drawn on a hop's ring; the rest are grouped. */
export const FOCUS_RING_CAP: Record<number, number> = { 1: 28, 2: 32, 3: 32 };
/** At most this many group bubbles per hop; anything beyond joins one "+N more" group. */
export const FOCUS_GROUPS = 6;

export type GroupNode = { id: string; key: string; kind: string; label: string; layer: "code" | "rules" | "learning"; trust_tier: "observed"; sub?: string; hop: number };

/** What a neighbour is grouped under: its package (the folder its file is in), or its layer. */
export function groupKey(n: { layer: string; sub?: string; key: string }): { key: string; label: string } {
  if (n.layer !== "code") return { key: n.layer, label: n.layer === "rules" ? "rules" : "learnings" };
  const path = n.sub && n.sub.includes("/") ? n.sub : n.key.split(":")[2] ?? "";
  const dirs = path.split("/").filter(Boolean).slice(0, -1);
  const tail = dirs.slice(-2).join("/") || "elsewhere";
  return { key: dirs.join("/") || "elsewhere", label: tail };
}

/**
 * Cap each hop's ring at FOCUS_RING_CAP nodes — the strongest connected — and fold the rest into
 * group bubbles by package ("+12 in loanaccount/service"), at most FOCUS_GROUPS per hop. Edges to
 * a folded node move to its group, summed. Deterministic: ties by id.
 */
export function groupFocus<N extends { id: string; key: string; layer: string; sub?: string; hop: number; label: string },
  E extends { src: string; dst: string; kind: string; weight: number; label: string | null }>(
  centerId: string, nodes: N[], edges: E[],
): { nodes: Array<N | GroupNode>; edges: E[]; members: Map<string, N[]> } {
  const weight = new Map<string, number>();
  for (const e of edges) {
    weight.set(e.src, (weight.get(e.src) ?? 0) + e.weight);
    weight.set(e.dst, (weight.get(e.dst) ?? 0) + e.weight);
  }
  const into = new Map<string, string>();
  const members = new Map<string, N[]>();
  const out: Array<N | GroupNode> = [];
  const hops = [...new Set(nodes.map((n) => n.hop))].sort((a, b) => a - b);
  for (const h of hops) {
    const list = nodes.filter((n) => n.hop === h);
    if (h === 0 || list.some((n) => n.id === centerId)) { out.push(...list); continue; }
    const cap = FOCUS_RING_CAP[h] ?? 32;
    const ranked = [...list].sort((a, b) => (weight.get(b.id) ?? 0) - (weight.get(a.id) ?? 0) || (a.id < b.id ? -1 : 1));
    if (ranked.length <= cap + 2) { out.push(...ranked); continue; }
    const keep = ranked.slice(0, cap);
    out.push(...keep);
    const rest = ranked.slice(cap);
    const byGroup = new Map<string, { label: string; items: N[] }>();
    for (const n of rest) {
      const g = groupKey(n);
      const cur = byGroup.get(g.key) ?? { label: g.label, items: [] };
      cur.items.push(n);
      byGroup.set(g.key, cur);
    }
    // A group holds two or more; lone ones join "other packages" (and a lone "other" is drawn as itself).
    const groups = [...byGroup.entries()].filter(([, g]) => g.items.length >= 2)
      .sort((a, b) => b[1].items.length - a[1].items.length || (a[0] < b[0] ? -1 : 1));
    const named = groups.slice(0, FOCUS_GROUPS - (groups.length >= FOCUS_GROUPS ? 1 : 0));
    const namedKeys = new Set(named.map(([k]) => k));
    const other = [...byGroup.entries()].filter(([k]) => !namedKeys.has(k)).flatMap(([, g]) => g.items);
    if (other.length === 1) { out.push(other[0]); other.length = 0; }
    const make = (key: string, label: string, items: N[]) => {
      const id = `group:${h}:${key}`;
      members.set(id, items);
      for (const n of items) into.set(n.id, id);
      out.push({ id, key: id, kind: "group", label: `+${items.length} in ${label}`, layer: items[0].layer === "code" ? "code" : items[0].layer as "rules" | "learning",
        trust_tier: "observed", sub: `${items.length} more, grouped`, hop: h });
    };
    for (const [key, g] of named) make(key, g.label, g.items);
    if (other.length) make("other", "other packages", other);
  }
  const merged = new Map<string, E>();
  for (const e of edges) {
    const s = into.get(e.src) ?? e.src;
    const d = into.get(e.dst) ?? e.dst;
    if (s === d) continue;
    const k = `${s}\u0000${d}\u0000${e.kind}`;
    const prev = merged.get(k);
    if (!prev) { merged.set(k, { ...e, src: s, dst: d }); continue; }
    const w = prev.weight + e.weight;
    const noun = (prev.label ?? e.label ?? "").replace(/^[\d,]+\s*/, "");
    merged.set(k, { ...prev, weight: w, label: noun ? `${w.toLocaleString("en-US")} ${w === 1 ? noun.replace(/s$/, "") : noun.endsWith("s") ? noun : `${noun}s`}` : null });
  }
  return { nodes: out, edges: [...merged.values()], members };
}
