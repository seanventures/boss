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
export const FOCUS_CEILING = 300;

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

export type LayoutNode = { id: string; key?: string };
export type LayoutEdge = { src: string; dst: string; weight: number };

/**
 * Whole repo: a seeded force layout (Fruchterman–Reingold, fixed iterations) of the area bubbles,
 * with the `pinned` bubbles (Rules, Learning) in a column to the right. Ids are sorted first, so
 * the input order never changes the picture; the seed is the keys themselves.
 */
export function overviewLayout(nodes: LayoutNode[], edges: LayoutEdge[], pinned: string[] = []): Map<string, Pt> {
  const pin = new Set(pinned);
  const free = nodes.filter((n) => !pin.has(n.id)).map((n) => ({ id: n.id, key: n.key ?? n.id }))
    .sort((a, b) => (a.key < b.key ? -1 : a.key > b.key ? 1 : 0));
  const out = new Map<string, Pt>();
  const n = free.length;
  const K = 130; // ideal distance between bubbles: room for a 16 px label and its count
  // Gravity pulls harder vertically, so the picture is wider than tall, like the canvas it fills.
  const GRAVITY_X = 3;
  const GRAVITY_Y = 4.4;
  const pos = free.map((f, i) => {
    const r = seeded(hashSeed(f.key));
    const angle = (i / Math.max(1, n)) * Math.PI * 2 + r() * 0.6;
    const radius = K * Math.sqrt(n) * (0.25 + r() * 0.2);
    return { x: Math.cos(angle) * radius, y: Math.sin(angle) * radius };
  });
  const index = new Map(free.map((f, i) => [f.id, i]));
  const springs = edges
    .filter((e) => index.has(e.src) && index.has(e.dst) && e.src !== e.dst)
    .map((e) => ({ a: index.get(e.src)!, b: index.get(e.dst)!, w: 1 + Math.log10(Math.max(1, e.weight)) }))
    // The input order must not change the sums either.
    .sort((x, y) => x.a - y.a || x.b - y.b || x.w - y.w);
  const ITER = 320;
  let temp = K;
  for (let it = 0; it < ITER; it += 1) {
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
      const f = ((d * d) / K) * 0.12 * s.w;
      dx[s.a] -= (vx / d) * f; dy[s.a] -= (vy / d) * f;
      dx[s.b] += (vx / d) * f; dy[s.b] += (vy / d) * f;
    }
    for (let i = 0; i < n; i += 1) {
      // Gravity keeps the unconnected areas on the page instead of drifting off it.
      dx[i] -= pos[i].x * GRAVITY_X;
      dy[i] -= pos[i].y * GRAVITY_Y;
      const d = Math.hypot(dx[i], dy[i]);
      if (d > 0) {
        const step = Math.min(d, temp);
        pos[i].x += (dx[i] / d) * step;
        pos[i].y += (dy[i] / d) * step;
      }
    }
    temp = Math.max(1, temp * 0.985);
  }
  separate(pos, 135, 60);
  // Centre the free bubbles on the origin.
  const bb = bounds(pos);
  const cx = (bb.minX + bb.maxX) / 2;
  const cy = (bb.minY + bb.maxY) / 2;
  free.forEach((f, i) => out.set(f.id, { x: round(pos[i].x - cx), y: round(pos[i].y - cy) }));
  // The layer bubbles, pinned right, in the order given.
  const present = new Set(nodes.map((nd) => nd.id));
  const pins = pinned.filter((id) => present.has(id)).map((id) => ({ id }));
  const right = (n ? (bb.maxX - cx) : 0) + 190;
  pins.forEach((p, i) => {
    const y = (i - (pins.length - 1) / 2) * 150;
    out.set(p.id, { x: round(right), y: round(y) });
  });
  return out;
}

/** Push apart any two points closer than `min`: a few deterministic passes. */
function separate(pos: Pt[], min: number, passes: number) {
  for (let p = 0; p < passes; p += 1) {
    let moved = false;
    for (let i = 0; i < pos.length; i += 1) {
      for (let j = i + 1; j < pos.length; j += 1) {
        const vx = pos[j].x - pos[i].x;
        const vy = pos[j].y - pos[i].y;
        const d = Math.hypot(vx, vy);
        if (d >= min) continue;
        moved = true;
        const ux = d > 0.001 ? vx / d : 1;
        const uy = d > 0.001 ? vy / d : 0;
        const push = (min - d) / 2;
        pos[i].x -= ux * push; pos[i].y -= uy * push;
        pos[j].x += ux * push; pos[j].y += uy * push;
      }
    }
    if (!moved) return;
  }
}

function round(v: number): number {
  return Math.round(v * 100) / 100;
}

export type FocusNode = { id: string; hop: number };

/** The first ring's radius, the gap between rings, and the arc each node needs on a ring. */
export const RING_FIRST = 190;
export const RING_STEP = 92;
export const RING_SLOT = 76;

/** How many nodes fit on a ring of radius r. */
export function ringCapacity(r: number): number {
  return Math.max(6, Math.floor((2 * Math.PI * r) / RING_SLOT));
}

/**
 * Focus: radial by hop. The centre is fixed at the origin; the first hop is ordered by its edge
 * weight to the centre, heaviest at the top and clockwise from there; later hops sit at the angle
 * of what they connect to on the hop before. A hop with more nodes than one ring holds continues
 * on the next ring out (staggered), so labels keep their room. Returns positions and the ring
 * radii each hop occupies (the canvas draws them faintly).
 */
export function focusLayout(centerId: string, nodes: FocusNode[], edges: LayoutEdge[]): { pos: Map<string, Pt>; rings: number[] } {
  const pos = new Map<string, Pt>([[centerId, { x: 0, y: 0 }]]);
  const rings: number[] = [];
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
  const angle = new Map<string, number>();
  let radius = RING_FIRST;
  const hops = [...byHop.keys()].sort((a, b) => a - b);
  for (const h of hops) {
    const list = byHop.get(h)!;
    let ordered: Array<{ id: string; want: number }>;
    if (h === 1) {
      const w = (id: string) => (neighbours.get(id) ?? []).filter((x) => x.id === centerId).reduce((s, x) => s + x.w, 0);
      ordered = [...list].sort((a, b) => w(b.id) - w(a.id) || (a.id < b.id ? -1 : 1)).map((nd) => ({ id: nd.id, want: 0 }));
    } else {
      ordered = list.map((nd) => {
        const parents = (neighbours.get(nd.id) ?? []).filter((x) => angle.has(x.id));
        const want = parents.length ? circularMean(parents.map((p) => angle.get(p.id)!)) : hashSeed(nd.id) / 4294967296 * Math.PI * 2;
        return { id: nd.id, want };
      }).sort((a, b) => a.want - b.want || (a.id < b.id ? -1 : 1));
    }
    let i = 0;
    let ring = 0;
    while (i < ordered.length) {
      const cap = ringCapacity(radius);
      const take = ordered.slice(i, i + cap);
      const step = (Math.PI * 2) / take.length;
      const stagger = ring % 2 ? step / 2 : 0;
      const start = h === 1 ? -Math.PI / 2 : take[0].want;
      take.forEach((t, j) => {
        const a = start + stagger + j * step;
        angle.set(t.id, a);
        pos.set(t.id, { x: round(Math.cos(a) * radius), y: round(Math.sin(a) * radius) });
      });
      rings.push(radius);
      i += take.length;
      ring += 1;
      radius += RING_STEP;
    }
    radius += 36; // a little air between one hop and the next
  }
  return { pos, rings };
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
    fact: "--kg-fact", endpoint: "--kg-endpoint", table: "--kg-table",
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
