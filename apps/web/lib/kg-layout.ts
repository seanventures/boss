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

/** The first ring's least half-height, the room between rings (vertically; horizontally it is
 *  `aspect` times more), and the arc each neighbour takes along its ring (tester round 3). */
export const RING_FIRST = 120;
export const RING_GAP = 120;
/** Arc per neighbour along its ring: a 28 px node, its label beside it, and air on both sides,
 *  so ring nodes never touch like beads (tester round 3 P0) and a side label has a row to itself. */
export const LABEL_ARC = 46;
/** The widest a Focus ring is stretched to follow the canvas (wider than tall, like the canvas). */
export const RING_ASPECT_MAX = 2.2;
export const RING_ASPECT_MIN = 0.6;

/** An ellipse's perimeter (Ramanujan): the arc the ring's nodes share. */
export function ellipsePerimeter(a: number, b: number): number {
  return Math.PI * (3 * (a + b) - Math.sqrt((3 * a + b) * (a + 3 * b)));
}

/** Points along an ellipse at equal ARC LENGTH (not equal angle): every neighbour gets the same
 *  room, at the narrow ends as on the long sides. `from` is the polar angle of the first. */
function alongEllipse(a: number, b: number, count: number, from: number): Pt[] {
  const M = 1440;
  const cum: number[] = [0];
  const pts: Pt[] = [];
  for (let i = 0; i <= M; i += 1) {
    const t = (i / M) * Math.PI * 2;
    pts.push({ x: a * Math.cos(t), y: b * Math.sin(t) });
    if (i) cum.push(cum[i - 1] + Math.hypot(pts[i].x - pts[i - 1].x, pts[i].y - pts[i - 1].y));
  }
  const total = cum[M];
  // The parameter of the polar angle `from`, then its arc position.
  const t0 = (Math.atan2(a * Math.sin(from), b * Math.cos(from)) + Math.PI * 4) % (Math.PI * 2);
  const s0 = cum[Math.round((t0 / (Math.PI * 2)) * M) % M];
  const out: Pt[] = [];
  let k = 0;
  for (let j = 0; j < count; j += 1) {
    const want = (s0 + (j * total) / count) % total;
    // cum is increasing: walk from where the last one was (wrapping once).
    while (k < M && cum[k + 1] < want) k += 1;
    if (cum[k] > want) k = 0;
    while (k < M && cum[k + 1] < want) k += 1;
    const seg = cum[k + 1] - cum[k] || 1;
    const f = (want - cum[k]) / seg;
    out.push({ x: pts[k].x + (pts[k + 1].x - pts[k].x) * f, y: pts[k].y + (pts[k + 1].y - pts[k].y) * f });
  }
  return out;
}

/** The room one node's label takes along its ring (tester round 5): `row` at the ring's sides,
 *  where labels stack one per row beside their nodes, and `w` at its top and bottom, where they sit
 *  side by side above or below them. */
export type LabelSlot = { row: number; w: number };

/** The arc between two ring nodes that keeps their labels apart (tester round 5), at polar angle
 *  `phi` where the ring runs along unit tangent (tx, ty). A label goes beside its node where the
 *  ring faces sideways (|cos φ| ≥ 0.4, graph-canvas sidesFor), so two neighbours need `row` of
 *  VERTICAL separation; elsewhere it goes above or below, so they need `w` of HORIZONTAL
 *  separation. Never less than two nodes' width. */
export function slotAt(slot: LabelSlot, phi: number, tx = 0, ty = 1): number {
  const side = Math.abs(Math.cos(phi)) >= 0.4;
  const need = side ? slot.row / Math.max(0.3, Math.abs(ty)) : slot.w / Math.max(0.3, Math.abs(tx));
  return Math.max(NODE_R * 2 + 6, need);
}

const SLOT_STEPS = 1440;

/** How many label slots an ellipse (a × b) holds: ∮ ds / slot(φ). Linear in its size. */
export function slotMeasure(a: number, b: number, slot: LabelSlot): number {
  let m = 0;
  let px = a; let py = 0;
  for (let i = 1; i <= SLOT_STEPS; i += 1) {
    const t = (i / SLOT_STEPS) * Math.PI * 2;
    const x = a * Math.cos(t); const y = b * Math.sin(t);
    const d = Math.hypot(x - px, y - py) || 1e-9;
    m += d / slotAt(slot, Math.atan2(y, x), (x - px) / d, (y - py) / d);
    px = x; py = y;
  }
  return m;
}

/** Points along an ellipse at equal SLOT measure: nodes closer at the sides (one label row
 *  each), wider apart at the top and bottom (a label's width each). `from` is a polar angle. */
export function alongEllipseSlots(a: number, b: number, count: number, from: number, slot: LabelSlot): Pt[] {
  const cum: number[] = [0];
  const pts: Pt[] = [{ x: a, y: 0 }];
  for (let i = 1; i <= SLOT_STEPS; i += 1) {
    const t = (i / SLOT_STEPS) * Math.PI * 2;
    const p = { x: a * Math.cos(t), y: b * Math.sin(t) };
    const d = Math.hypot(p.x - pts[i - 1].x, p.y - pts[i - 1].y) || 1e-9;
    cum.push(cum[i - 1] + d / slotAt(slot, Math.atan2(p.y, p.x), (p.x - pts[i - 1].x) / d, (p.y - pts[i - 1].y) / d));
    pts.push(p);
  }
  const total = cum[SLOT_STEPS];
  const t0 = (Math.atan2(a * Math.sin(from), b * Math.cos(from)) + Math.PI * 4) % (Math.PI * 2);
  const s0 = cum[Math.round((t0 / (Math.PI * 2)) * SLOT_STEPS) % SLOT_STEPS];
  const out: Pt[] = [];
  for (let j = 0; j < count; j += 1) {
    const want = (s0 + (j * total) / count) % total;
    let lo = 0; let hi = SLOT_STEPS;
    while (hi - lo > 1) { const mid = (lo + hi) >> 1; if (cum[mid] <= want) lo = mid; else hi = mid; }
    const seg = cum[hi] - cum[lo] || 1;
    const f = (want - cum[lo]) / seg;
    out.push({ x: pts[lo].x + (pts[hi].x - pts[lo].x) * f, y: pts[lo].y + (pts[hi].y - pts[lo].y) * f });
  }
  return out;
}

/**
 * Focus (§5.4; tester rounds 1-3): radial by hop, ONE ring per hop, each ring an ellipse as wide
 * as the canvas is (`aspect`, at most RING_ASPECT_MAX) and long enough that every neighbour has
 * LABEL_ARC px of it at 100%, nodes spaced by arc length. The first hop is ordered by its edge
 * weight to the centre (heaviest at the top, clockwise); a later hop sits at the angle of what it
 * connects to. Returns positions, each ring's half-height (its half-width is `aspect` times it),
 * each node's polar angle (where its label goes: outward) and its ring.
 */
export function focusLayout(centerId: string, nodes: FocusNode[], edges: LayoutEdge[], opts: {
  aspect?: number; gap?: number; first?: number; arc?: (id: string) => number;
  /** A hop's label slot (screen px at 100%): `row` along the ring at its sides, `w` at its top. */
  slot?: (hop: number) => LabelSlot | undefined;
} = {}): {
  pos: Map<string, Pt>; rings: number[]; aspect: number; angle: Map<string, number>; ring: Map<string, number>;
} {
  // Wider than tall in a wide canvas; taller than wide (down to RING_ASPECT_MIN) in a narrow one
  // beside the drawer, so the picture fills what is visible either way.
  const ax = Math.min(RING_ASPECT_MAX, Math.max(RING_ASPECT_MIN, opts.aspect ?? 1));
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
  const unitPerimeter = ellipsePerimeter(ax, 1);
  for (const h of hops) {
    const list = byHop.get(h)!;
    let ordered: Array<{ id: string; want: number }>;
    if (h === hops[0]) {
      const w = (id: string) => (neighbours.get(id) ?? []).filter((x) => x.id === centerId).reduce((s, x) => s + x.w, 0);
      ordered = [...list].sort((a, b) => w(b.id) - w(a.id) || (a.id < b.id ? -1 : 1)).map((nd) => ({ id: nd.id, want: 0 }));
    } else {
      ordered = list.map((nd) => {
        // Beside its STRONGEST way in — the one edge a Focus draws for it (focusEdges) — so that
        // edge stays short (tester round 5: a cluster at the mean of many parents drew its edge
        // across the whole first ring). The centre is no parent here: it sits at no angle.
        const parents = (neighbours.get(nd.id) ?? []).filter((x) => angle.has(x.id) && x.id !== centerId);
        const top = parents.reduce((m, p) => Math.max(m, p.w), 0);
        const strongest = parents.filter((p) => p.w === top);
        const want = parents.length ? circularMean(strongest.map((p) => angle.get(p.id)!)) : (hashSeed(nd.id) / 4294967296) * Math.PI * 2;
        return { id: nd.id, want: (want + Math.PI * 4) % (Math.PI * 2) };
      }).sort((a, b) => a.want - b.want || (a.id < b.id ? -1 : 1));
    }
    const start = h === hops[0] ? -Math.PI / 2 : ordered[0].want;
    const slot = opts.slot?.(h);
    let at: Pt[];
    let radius: number;
    if (slot) {
      // LABEL SLOTS BY ARC LENGTH (tester round 5): a label beside a node at the ring's side needs
      // one row of the arc; one above or below it at the top needs its width. Nodes are spaced by
      // the room their LABEL needs where they sit, so every one on the ring has its slot.
      const per = slotMeasure(ax, 1, slot);
      radius = round(Math.max(prev ? prev + (opts.gap ?? RING_GAP) : (opts.first ?? RING_FIRST), ordered.length / per));
      at = alongEllipseSlots(radius * ax, radius, ordered.length, start, slot);
      if (h !== hops[0]) {
        // An outer ring turned so each node (cluster) sits as near as it can to what it hangs on:
        // its one edge in stays short and crosses no other node's label (tester round 5).
        const dist = (p: Pt, want: number) => { const d = Math.abs(Math.atan2(p.y, p.x) - want) % (Math.PI * 2); return Math.min(d, Math.PI * 2 - d); };
        const cost = (pts: Pt[]) => pts.reduce((s, p, j) => s + dist(p, ordered[j].want), 0);
        let best = cost(at);
        for (let k = 1; k < 72; k += 1) {
          const cand = alongEllipseSlots(radius * ax, radius, ordered.length, start + (k / 72) * Math.PI * 2, slot);
          const c = cost(cand);
          if (c < best - 1e-9) { best = c; at = cand; }
        }
      }
    } else {
      // Each node's share of its ring: LABEL_ARC, or what `arc` says (a cluster bubble and its
      // two-line label take more than a class).
      const arcSum = ordered.reduce((s, t) => s + (opts.arc ? opts.arc(t.id) : LABEL_ARC), 0);
      const need = arcSum / unitPerimeter;
      radius = round(Math.max(prev ? prev + (opts.gap ?? RING_GAP) : (opts.first ?? RING_FIRST), need));
      at = alongEllipse(radius * ax, radius, ordered.length, start);
    }
    ordered.forEach((t, j) => {
      const p = { x: round(at[j].x), y: round(at[j].y) };
      angle.set(t.id, Math.atan2(p.y, p.x));
      ringOf.set(t.id, rings.length);
      pos.set(t.id, p);
    });
    rings.push(radius);
    prev = radius;
  }
  return { pos, rings, aspect: ax, angle, ring: ringOf };
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

/** Shortened at WORD boundaries (tester round 4: "Creo…sor", "DuePenl…cessor", "fineract…ts-core"):
 *  the first words and the last kept whole — "DuePen…TransactionProcessor", "fineract-…-core" —
 *  camelCase humps, "-", "_", ".", "/" and spaces are where a name may be cut. A single word
 *  longer than `max` falls back to the middle ellipsis. */
export function wordEllipsis(text: string, max = LABEL_MAX): string {
  if (text.length <= max) return text;
  const words = text.match(/[A-Z]+(?![a-z])|[A-Z]?[a-z0-9]+|[^A-Za-z0-9]+|[A-Z]/g) ?? [text];
  if (words.length < 2) return middleEllipsis(text, max);
  let head = 0; let tail = words.length;
  let len = 1; // the ellipsis
  // The last word first (what kind of thing it is: Processor, Service), then the first, alternating.
  const take = (fromEnd: boolean) => {
    const w = fromEnd ? words[tail - 1] : words[head];
    if (head >= tail || len + w.length > max) return false;
    len += w.length;
    if (fromEnd) tail -= 1; else head += 1;
    return true;
  };
  let end = take(true);
  let start = take(false);
  while (end || start) { end = take(true); start = take(false); }
  // Room for one whole word only: the first ("Abstract…"), else the last ("…Processor").
  if (!head) {
    if (words[0].length + 1 <= max) return `${words[0].replace(/[-_./\s]+$/, "")}…`;
    return tail < words.length ? `…${words.slice(tail).join("").replace(/^[-_./\s]+/, "")}` : middleEllipsis(text, max);
  }
  if (tail === words.length) return `${words.slice(0, head).join("").replace(/[-_./\s]+$/, "")}…`;
  const trim = (s: string) => s.replace(/[-_./\s]+$/, "");
  const lead = (s: string) => s.replace(/^[-_./\s]+/, "");
  const a = words.slice(0, head).join("");
  const b = words.slice(tail).join("");
  // Keep a separator the name had ("fineract-…-core"); drop a dangling one otherwise.
  return /[-_./]$/.test(a) || /^[-_./]/.test(b) ? `${a}…${b}` : `${trim(a)}…${lead(b)}`;
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

/** Where a label may go around its node, in the order tried. */
export type LabelSide = "below" | "above" | "right" | "left" | "above2" | "below2";

export type LabelItem = {
  id: string;
  /** The node, screen px: the label touches it wherever it goes. */
  x: number; y: number; r: number;
  priority: number;
  /** Its forms, longest first (full name, shorter middle-ellipsised names, one line). */
  variants: Array<{ w: number; h: number }>;
  /** "below" (bubbles, a few neighbours), or "out": the side facing away from the Focus centre. */
  prefer?: "below" | "out";
  /** Outward unit vector (Focus rings). */
  dir?: Pt;
  /** Drawn even with no room (the selection, the hovered node, the centre). */
  must?: boolean;
  /** Always shown and never on another label (the selection's first hop, a Whole-repo bubble):
   *  with no room beside its node it moves outward, joined to its node by a leader line. */
  keep?: boolean;
};

export type PlacedLabel = { variant: number; side: LabelSide; x: number; y: number; w: number; h: number;
  /** Moved outward from its node: drawn with a leader line back to it. */
  leader?: boolean };

/** The rectangle labels must stay inside: the canvas minus what the details drawer covers. */
export type LabelBounds = { x0: number; y0: number; x1: number; y1: number };

function sidesFor(it: LabelItem): LabelSide[] {
  let first: LabelSide = "below";
  if (it.prefer === "out" && it.dir) {
    if (Math.abs(it.dir.x) >= 0.4) first = it.dir.x > 0 ? "right" : "left";
    else first = it.dir.y < 0 ? "above" : "below";
  }
  // A ring node's label never goes INWARD (tester round 4: "Money" and "FineractSt…Processor" sat
  // inside the ring, "+9 in loanaccount/domain" read as the centre's label): only outward or along.
  const out = it.prefer === "out";
  const rest: LabelSide[] = first === "left" ? (out ? ["below", "above"] : ["below", "above", "right"])
    : first === "right" ? (out ? ["below", "above"] : ["below", "above", "left"])
    : first === "above" ? (out ? ["right", "left"] : ["below", "right", "left"])
    : out ? ["right", "left"] : ["above", "right", "left"];
  // A ring's top and bottom are where neighbours sit side by side: their labels take turns on a
  // second row out (never further, so each still reads as its node's).
  const extra: LabelSide[] = it.prefer === "out" ? (first === "above" ? ["above2"] : first === "below" ? ["below2"] : []) : [];
  return [first, ...extra, ...rest];
}

/** A label's box on one side of its node (x = its centre, y = its top), slid inside `b` along
 *  the node where that keeps it touching the node; null when it cannot be inside. */
export function labelBox(it: Pick<LabelItem, "x" | "y" | "r">, side: LabelSide, w: number, h: number, b: LabelBounds, gap = 5): { x: number; y: number } | null {
  let x: number; let y: number;
  if (side === "below" || side === "above" || side === "below2" || side === "above2") {
    const row = side === "below2" || side === "above2" ? h + 2 : 0;
    y = side === "below" || side === "below2" ? it.y + it.r + gap - 1 + row : it.y - it.r - gap - h - row;
    // Slid in from an edge, never so far it no longer spans its node.
    const lo = b.x0 + w / 2; const hi = b.x1 - w / 2;
    x = hi < lo ? it.x : Math.min(Math.max(it.x, lo), hi);
    if (Math.abs(x - it.x) > w / 2 - 2) return null;
  } else {
    x = side === "right" ? it.x + it.r + gap + w / 2 : it.x - it.r - gap - w / 2;
    y = it.y - h / 2;
    if (x - w / 2 < b.x0 || x + w / 2 > b.x1) return null;
  }
  if (y < b.y0 || y + h > b.y1) return null;
  return { x, y };
}

function overlaps(a: LabelBox, c: LabelBox, gap: number): boolean {
  return a.x - a.w / 2 - gap < c.x + c.w / 2 && a.x + a.w / 2 + gap > c.x - c.w / 2 && a.y - gap < c.y + c.h && a.y + a.h + gap > c.y;
}

/**
 * Node labels that never overlap (tester round 3 P0): highest priority first, each label takes
 * the first of its forms (full name, then shorter middle-ellipsised ones) that fits on any side
 * of its node — its preferred side first — clear of every circle, every label already placed and
 * the edge of the visible canvas. A label with no room anywhere waits for hover or zoom (its full
 * name is in the tooltip), unless it `must` show (the selection, the centre): then its shortest
 * form goes on its preferred side regardless. Deterministic: ties by id.
 */
export function placeNodeLabels(items: LabelItem[], obstacles: LabelBox[], bounds: LabelBounds, gap = 2): Map<string, PlacedLabel> {
  const placed: LabelBox[] = [];
  const out = new Map<string, PlacedLabel>();
  const clear = (bx: LabelBox, self: string) => !obstacles.some((o) => o.id !== `o:${self}` && overlaps(bx, o, gap))
    && !placed.some((p) => overlaps(bx, p, gap));
  for (const it of [...items].sort((a, b) => b.priority - a.priority || (a.id < b.id ? -1 : 1))) {
    let got: PlacedLabel | null = null;
    const sides = sidesFor(it);
    for (let v = 0; v < it.variants.length && !got; v += 1) {
      const { w, h } = it.variants[v];
      for (const side of sides) {
        const at = labelBox(it, side, w, h, bounds);
        if (!at) continue;
        const bx = { id: it.id, x: at.x, y: at.y, w, h, priority: it.priority };
        if (clear(bx, it.id)) { got = { variant: v, side, x: at.x, y: at.y, w, h }; break; }
      }
    }
    if (!got && it.keep) {
      // Outward along its ray, in steps, the shortest form first: clear of every label already
      // placed (circles it may cross; its leader line says whose it is).
      const v = it.variants.length - 1;
      const { w, h } = it.variants[v];
      const dir = it.dir ?? { x: 0, y: 1 };
      for (let step = 1; step <= 14 && !got; step += 1) {
        const off = step * (h + 4);
        const probe = { ...it, x: it.x + dir.x * off, y: it.y + dir.y * off, r: 2 };
        for (const side of sides) {
          const at = labelBox(probe, side, w, h, bounds);
          if (!at) continue;
          const bx = { id: it.id, x: at.x, y: at.y, w, h, priority: it.priority };
          if (!placed.some((p) => overlaps(bx, p, gap))) { got = { variant: v, side, x: at.x, y: at.y, w, h, leader: true }; break; }
        }
      }
    }
    if (!got && (it.must || it.keep)) {
      const v = it.variants.length - 1;
      const { w, h } = it.variants[v];
      const loose = { x0: -1e9, y0: -1e9, x1: 1e9, y1: 1e9 };
      const at = labelBox(it, sides[0], w, h, bounds) ?? labelBox(it, sides[0], w, h, loose)!;
      got = { variant: v, side: sides[0], x: at.x, y: at.y, w, h };
    }
    if (!got) continue;
    out.set(it.id, got);
    placed.push({ id: it.id, x: got.x, y: got.y, w: got.w, h: got.h, priority: it.priority });
  }
  return out;
}

/** True when no two boxes (x = centre, y = top) overlap: what the placement promises. */
export function labelsClear(boxes: Array<{ x: number; y: number; w: number; h: number }>): boolean {
  for (let i = 0; i < boxes.length; i += 1) {
    for (let j = i + 1; j < boxes.length; j += 1) {
      const a = boxes[i]; const c = boxes[j];
      if (a.x - a.w / 2 < c.x + c.w / 2 - 0.5 && a.x + a.w / 2 > c.x - c.w / 2 + 0.5 && a.y < c.y + c.h - 0.5 && a.y + a.h > c.y + 0.5) return false;
    }
  }
  return true;
}

/**
 * How far to pan left (≤ 0) so the selection and its label clear the details drawer, using the
 * room on the left first so the rest of the picture (the pinned Rules and Learning bubbles too)
 * comes out from under it where it can (tester round 3 P1): `must` boxes always end visible,
 * `rest` only as far as the left margin allows. Boxes are screen px, x = left edge.
 */
export function revealPan(must: Array<{ x: number; w: number }>, rest: Array<{ x: number; w: number }>, visibleRight: number, margin = 16): number {
  const all = [...must, ...rest];
  if (!all.length) return 0;
  const right = (bs: Array<{ x: number; w: number }>) => Math.max(...bs.map((b) => b.x + b.w));
  const left = Math.min(...all.map((b) => b.x));
  const needMust = must.length ? Math.max(0, right(must) - (visibleRight - margin)) : 0;
  const needAll = Math.max(0, right(all) - (visibleRight - margin));
  const slack = Math.max(0, left - margin);
  return -Math.max(needMust, Math.min(needAll, slack));
}

/** The tail of a path-like name, ellipsised at the START ("…/loanaccount/service"): a package
 *  group's label keeps the part that tells packages apart and is never cut mid-word. */
export function tailEllipsis(text: string, max: number): string {
  if (text.length <= max) return text;
  const parts = text.split("/").filter(Boolean);
  let tail = "";
  for (let i = parts.length - 1; i >= 0; i -= 1) {
    const next = tail ? `${parts[i]}/${tail}` : parts[i];
    if (next.length + 2 > max && tail) break;
    tail = next;
  }
  return tail.length + 2 > max ? middleEllipsis(tail, max) : `…/${tail}`;
}

// ── Focus that stays readable at any size (tester round 2: 2 hops / Show 200) ─────────────

/** At most this many neighbours are drawn on a hop's ring; the rest are grouped. */
export const FOCUS_RING_CAP: Record<number, number> = { 1: 28, 2: 32, 3: 32 };
/** At most this many group bubbles per hop; anything beyond joins one "+N more" group. */
export const FOCUS_GROUPS = 6;
/** At most this many cluster bubbles on an outer ring (the rest share "other packages"). */
export const FOCUS_CLUSTERS = 8;
/** An outer hop with at most this many nodes draws them as themselves, not clusters. */
export const CLUSTER_MIN_HOP = 3;
/** A cluster bubble's screen radius: between a class (14) and an area (28). */
export const CLUSTER_R = 19;
/** The arc a cluster bubble and its two-line label take along its ring. */
export const CLUSTER_ARC = 104;
/** A group's package name is shortened from its start past this (its label is never cut). */
export const GROUP_NAME_MAX = 20;

export type GroupNode = { id: string; key: string; kind: string; label: string; layer: "code" | "rules" | "learning"; trust_tier: "observed"; sub?: string; hop: number;
  /** An outer hop's package cluster (opens on click), not an overflow group of the first ring. */
  cluster?: boolean };

/** The shortest folder tails (two folders at least) that tell a set of package paths apart. */
export function distinctTails(paths: string[]): string[] {
  const parts = paths.map((p) => p.split("/").filter(Boolean));
  const tail = (ps: string[]) => ps.slice(-2).join("/") || "elsewhere";
  const count = new Map<string, number>();
  for (const ps of parts) count.set(tail(ps), (count.get(tail(ps)) ?? 0) + 1);
  // A shared tail: the module (the path's first folder) says which one — "fineract-loan/…/service".
  return parts.map((ps) => (count.get(tail(ps))! > 1 && ps.length > 2 ? `${ps[0]}/…/${ps.slice(-2).join("/")}` : tail(ps)));
}

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
  centerId: string, nodes: N[], edges: E[], caps: Record<number, number> = FOCUS_RING_CAP,
  opts: { clusterFrom?: number; clusters?: number } = {},
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
    if (opts.clusterFrom !== undefined && h >= opts.clusterFrom && list.length > CLUSTER_MIN_HOP) {
      // TESTER ROUND 5 P1: an outer hop is never a ring of single classes. Every node of it goes
      // into its package's cluster bubble ("loanaccount/domain · 37 classes"), drawn one per
      // package; a cluster opens on click. Lone packages share one "other packages" cluster.
      const byPkg = new Map<string, { label: string; items: N[] }>();
      for (const n of list) {
        const g = groupKey(n);
        const cur = byPkg.get(g.key) ?? { label: g.label, items: [] };
        cur.items.push(n);
        byPkg.set(g.key, cur);
      }
      const rankOf = (items: N[]) => items.reduce((s, n) => s + (weight.get(n.id) ?? 0), 0);
      const sized = [...byPkg.entries()].filter(([, g]) => g.items.length >= 2)
        .sort((a, b) => b[1].items.length - a[1].items.length || rankOf(b[1].items) - rankOf(a[1].items) || (a[0] < b[0] ? -1 : 1));
      const max = Math.max(1, opts.clusters ?? FOCUS_CLUSTERS);
      const named = sized.slice(0, sized.length > max ? max - 1 : max);
      const namedKeys = new Set(named.map(([k]) => k));
      const rest = [...byPkg.entries()].filter(([k]) => !namedKeys.has(k)).sort((a, b) => (a[0] < b[0] ? -1 : 1));
      const restItems = rest.flatMap(([, g]) => g.items);
      const add = (key: string, label: string, items: N[], packages: number) => {
        const id = `group:${h}:${key}`;
        const sorted = [...items].sort((a, b) => (weight.get(b.id) ?? 0) - (weight.get(a.id) ?? 0) || (a.id < b.id ? -1 : 1));
        members.set(id, sorted);
        for (const n of items) into.set(n.id, id);
        const noun = items[0].layer === "code" ? (items.length === 1 ? "class" : "classes") : (items.length === 1 ? "item" : "items");
        out.push({ id, key: id, kind: "group", label, layer: items[0].layer === "code" ? "code" : items[0].layer as "rules" | "learning",
          trust_tier: "observed", sub: packages > 1 ? `${items.length} ${noun} · ${packages} packages` : `${items.length} ${noun}`, hop: h, cluster: true });
      };
      // Two packages with the same last two folders ("loanaccount/service" in two modules) are
      // told apart by as many more folders as it takes.
      const names = distinctTails(named.map(([key]) => key));
      named.forEach(([key, g], i) => add(key, names[i], g.items, 1));
      if (restItems.length) add("other", rest.length === 1 ? tailEllipsis(rest[0][1].label, GROUP_NAME_MAX) : "other packages", restItems, rest.length);
      continue;
    }
    const cap = caps[h] ?? FOCUS_RING_CAP[h] ?? 32;
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
      out.push({ id, key: id, kind: "group", label: `+${items.length} in ${tailEllipsis(label, GROUP_NAME_MAX)}`, layer: items[0].layer === "code" ? "code" : items[0].layer as "rules" | "learning",
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


/** A ring node's label is shown only from this zoom up (or on hover, or when it is the selection
 *  or one hop from it): 2 hops / Show 200 name the first ring, the rest on hover or zoom. */
export const LABEL_ZOOM_OUTER = 1.35;

/**
 * WHAT A FOCUS DRAWS (tester round 4 P0: 2 hops and Show 200 were a solid knot of edges at the
 * centre). Every edge to the centre; for each node further out, its strongest edge to the ring
 * inside it — the tree that says how it is reached; and every edge of the selection. The rest
 * (chords between two nodes of one ring, a second route in) are in the list view and the panel.
 */
export function focusEdges<E extends { src: string; dst: string; weight: number }>(
  centerId: string, hops: Map<string, number>, edges: E[], selectedId?: string | null,
): E[] {
  const keep = new Set<E>();
  const best = new Map<string, E>();
  for (const e of edges) {
    if (e.src === centerId || e.dst === centerId) { keep.add(e); continue; }
    if (selectedId && selectedId !== centerId && (e.src === selectedId || e.dst === selectedId)) keep.add(e);
    const hs = hops.get(e.src); const hd = hops.get(e.dst);
    if (hs === undefined || hd === undefined || Math.abs(hs - hd) !== 1) continue;
    const outer = hs > hd ? e.src : e.dst;
    const prev = best.get(outer);
    if (!prev || e.weight > prev.weight || (e.weight === prev.weight && `${e.src}${e.dst}` < `${prev.src}${prev.dst}`)) best.set(outer, e);
  }
  for (const e of best.values()) keep.add(e);
  return edges.filter((e) => keep.has(e));
}

/** Whose label always shows in a Focus: the centre's first ring, and the selection's neighbours. */
export function focusKeepLabels(centerId: string, hops: Map<string, number>, edges: Array<{ src: string; dst: string }>, selectedId?: string | null): Set<string> {
  const out = new Set<string>();
  for (const [id, h] of hops) if (h <= 1) out.add(id);
  if (selectedId) {
    out.add(selectedId);
    for (const e of edges) {
      if (e.src === selectedId) out.add(e.dst);
      if (e.dst === selectedId) out.add(e.src);
    }
  }
  out.add(centerId);
  return out;
}

// ── tester round 5: outer hops as clusters, members fanned out, labels that read ─────────────

/**
 * Where an OPENED cluster's members go: on an arc beyond the outermost ring, centred on the
 * cluster's own angle, LABEL_ARC apart (a further row out when one row would pass a quarter of
 * the ring). Nothing already drawn moves: opening a cluster is never a re-layout.
 */
export function placeMembers(at: Pt, outer: number, aspect: number, ids: string[], gap = 80): Map<string, Pt> {
  const out = new Map<string, Pt>();
  if (!ids.length) return out;
  const theta = Math.atan2(at.y * aspect, at.x);   // the ellipse parameter of the cluster's angle
  let row = 0; let i = 0;
  while (i < ids.length) {
    const r = outer + gap + row * 52;
    const per = Math.max(3, Math.floor((ellipsePerimeter(r * aspect, r) / 4) / LABEL_ARC));
    const take = ids.slice(i, i + per);
    // The arc step as a parameter step: the ellipse's local speed at theta.
    const local = Math.hypot(r * aspect * Math.sin(theta), r * Math.cos(theta)) || r;
    const step = LABEL_ARC / local;
    take.forEach((id, j) => {
      const t = theta + (j - (take.length - 1) / 2) * step + (row % 2 ? step / 2 : 0);
      out.set(id, { x: round(Math.cos(t) * r * aspect), y: round(Math.sin(t) * r) });
    });
    i += take.length;
    row += 1;
  }
  return out;
}

/**
 * How many labels a ring shows (tester round 5: cap visible labels per ring by available arc
 * length): its screen perimeter over the room one label takes along it. A ring's top and bottom
 * hold labels side by side (`labelW` each), its sides stack them a row apart, so the room per
 * label is the mean of the two.
 */
export function ringLabelCapacity(radius: number, aspect: number, k: number, labelW = 96, rowH = 24): number {
  const per = ellipsePerimeter(radius * aspect * k, radius * k);
  return Math.max(4, Math.floor(per / ((labelW + rowH) / 2)));
}

/** Cut at a WORD with an ellipsis (tester round 6: "+2 in core/se…", "On-time repayment bra…"):
 *  at the last camelCase hump, "-", "_", ".", "/" or space that keeps at least 40% of the room;
 *  only a name whose first word alone is longer than that is cut by character (round 5: never
 *  down to a lone short word — "Early…", "Creo…"). A "/" stays: it says the name is a path. */
export function charEllipsis(text: string, max: number): string {
  if (text.length <= max) return text;
  const room = Math.max(1, max - 1);
  const words = text.match(/[A-Z]+(?![a-z])|[A-Z]?[a-z0-9]+|[^A-Za-z0-9]+|[A-Z]/g) ?? [text];
  let head = "";
  for (const w of words) {
    if ((head + w).length > room) break;
    head += w;
  }
  head = head.replace(/[-_.\s]+$/, "");
  if (head.length >= room * 0.4) return `${head}…`;
  return `${text.slice(0, room).replace(/[-_./\s]+$/, "")}…`;
}

/**
 * A name on two lines at a word boundary (camelCase humps, "-", "_", ".", "/", spaces), each at
 * most `max` characters; what does not fit is cut on the second line by character, with an
 * ellipsis. One line when it fits: "AdvancedPaymentScheduleTransactionProcessor" at 18 →
 * ["AdvancedPayment", "ScheduleTransacti…"].
 */
export function twoLines(text: string, max: number): string[] {
  if (text.length <= max) return [text];
  const words = text.match(/[A-Z]+(?![a-z])|[A-Z]?[a-z0-9]+|[^A-Za-z0-9]+|[A-Z]/g) ?? [text];
  let first = "";
  let i = 0;
  while (i < words.length && (first + words[i]).length <= max) { first += words[i]; i += 1; }
  if (!first || first.length < 4) return [charEllipsis(text, max)];
  const rest = words.slice(i).join("").replace(/^\s+/, "");
  return rest ? [first.replace(/\s+$/, ""), charEllipsis(rest, max)] : [first];
}
