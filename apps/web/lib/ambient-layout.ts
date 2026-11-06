/**
 * THE FLOW CHART'S LAYOUT — a small layered (Sugiyama-style) DAG layout, left to right, in pure
 * functions so it is tested without a browser and drawn the same on every screen.
 *
 *   1. Break cycles: an edge that points back in the walk (an escalation answered by a handoff,
 *      an override pointing at the row it overrides) is drawn as an arc above, never ranked.
 *   2. Rank by the longest path from the signals, so "CoS → SRE lead → Investigator" reads as
 *      three steps rather than three agents stacked in one column. The outcome is always last.
 *   3. Order each column by the barycentre of its neighbours (one pass down, one back), which
 *      removes most crossings on graphs this size (≤ 30 nodes).
 *   4. Spread the ports where several arrows meet one node, so a squad's votes fan in visibly
 *      and a dissent splits off as its own arrow.
 *
 * No library: the console ships none, and this is ~150 lines it can own.
 */
import type { FlowEdge, FlowNode } from "./ambient";

export const NODE_W = 132;
export const NODE_H = 70;
export const GAP_X = 66;
export const GAP_Y = 26;
export const PAD = 24;

export type PlacedNode = FlowNode & { x: number; y: number; w: number; h: number; col: number; row: number };
export type PlacedEdge = FlowEdge & { d: string; lx: number; ly: number; back: boolean; count: number; key: string };
export type Layout = { width: number; height: number; nodes: PlacedNode[]; edges: PlacedEdge[]; cols: number };

const TERMINAL = new Set(["outcome"]);
const NOT_RANKED = new Set(["overrides"]);

export function layoutFlow(nodes: FlowNode[], edges: FlowEdge[]): Layout {
  const order = [...nodes].map((n, i) => ({ n, i })).sort((a, b) => a.n.stage - b.n.stage || a.i - b.i).map((x) => x.n);
  const index = new Map(order.map((n, i) => [n.id, i]));
  const ids = new Set(order.map((n) => n.id));
  const usable = edges.filter((e) => ids.has(e.from) && ids.has(e.to) && e.from !== e.to);

  // 1. cycle breaking: DFS in (stage, input) order; an edge to a node on the stack is a back edge.
  const out = new Map<string, FlowEdge[]>(order.map((n) => [n.id, []]));
  for (const e of usable) if (!NOT_RANKED.has(e.kind)) out.get(e.from)!.push(e);
  const back = new Set<FlowEdge>(usable.filter((e) => NOT_RANKED.has(e.kind)));
  const state = new Map<string, 0 | 1 | 2>();
  const visit = (id: string) => {
    state.set(id, 1);
    for (const e of [...out.get(id)!].sort((a, b) => index.get(a.to)! - index.get(b.to)!)) {
      const s = state.get(e.to) ?? 0;
      if (s === 1) back.add(e);
      else if (s === 0) visit(e.to);
    }
    state.set(id, 2);
  };
  for (const n of order) if (!state.get(n.id)) visit(n.id);
  // An override comes AFTER the decision it overrides: rank it as overridden → override (the
  // arrow itself is drawn back, as an arc, pointing at the row it overrode).
  const forward = [...usable.filter((e) => !back.has(e)),
                   ...usable.filter((e) => e.kind === "overrides").map((e) => ({ ...e, from: e.to, to: e.from }))];

  // 2. longest-path ranks (Kahn order).
  const rank = new Map<string, number>(order.map((n) => [n.id, 0]));
  const indeg = new Map<string, number>(order.map((n) => [n.id, 0]));
  for (const e of forward) indeg.set(e.to, indeg.get(e.to)! + 1);
  const queue = order.filter((n) => indeg.get(n.id) === 0).map((n) => n.id);
  while (queue.length) {
    const id = queue.shift()!;
    for (const e of forward.filter((x) => x.from === id)) {
      rank.set(e.to, Math.max(rank.get(e.to)!, rank.get(id)! + 1));
      indeg.set(e.to, indeg.get(e.to)! - 1);
      if (indeg.get(e.to) === 0) queue.push(e.to);
    }
  }
  // A voice with no arrow in (a reviewer who only voted) sits just before what it acted on,
  // not back beside the signal with a long arrow across the chart.
  for (const n of [...order].reverse()) {
    if (n.type === "signal" || forward.some((e) => e.to === n.id)) continue;
    const next = forward.filter((e) => e.from === n.id).map((e) => rank.get(e.to)!);
    if (next.length) rank.set(n.id, Math.max(rank.get(n.id)!, Math.min(...next) - 1));
  }
  // A node no edge reaches sits by its stage; the outcome closes the chart.
  const linked = new Set(usable.flatMap((e) => [e.from, e.to]));
  for (const n of order) if (!linked.has(n.id)) rank.set(n.id, Math.min(n.stage, Math.max(0, ...rank.values())));
  const maxNonTerminal = Math.max(0, ...order.filter((n) => !TERMINAL.has(n.type)).map((n) => rank.get(n.id)!));
  for (const n of order) if (TERMINAL.has(n.type)) rank.set(n.id, Math.max(rank.get(n.id)!, maxNonTerminal + 1));
  // Compress empty columns.
  const used = [...new Set(rank.values())].sort((a, b) => a - b);
  const colOf = new Map(used.map((r, i) => [r, i]));
  const col = new Map(order.map((n) => [n.id, colOf.get(rank.get(n.id)!)!]));
  const cols = used.length;

  // 3. ordering inside columns by barycentre.
  const columns: string[][] = Array.from({ length: cols }, () => []);
  for (const n of order) columns[col.get(n.id)!].push(n.id);
  const pos = new Map<string, number>();
  const setPos = () => columns.forEach((c) => c.forEach((id, i) => pos.set(id, i)));
  setPos();
  const neighbours = (id: string, dir: "in" | "out") =>
    usable.filter((e) => (dir === "in" ? e.to === id && col.get(e.from)! < col.get(id)! : e.from === id && col.get(e.to)! > col.get(id)!))
      .map((e) => (dir === "in" ? e.from : e.to));
  const sweep = (c: number, dir: "in" | "out") => {
    const bary = (id: string) => {
      const ns = neighbours(id, dir);
      return ns.length ? ns.reduce((t, x) => t + pos.get(x)!, 0) / ns.length : pos.get(id)!;
    };
    columns[c] = [...columns[c]].map((id, i) => ({ id, b: bary(id), i })).sort((a, b) => a.b - b.b || a.i - b.i).map((x) => x.id);
    setPos();
  };
  for (let c = 1; c < cols; c++) sweep(c, "in");
  for (let c = cols - 2; c >= 0; c--) sweep(c, "out");

  // Coordinates: each column centred on the tallest.
  const tallest = Math.max(1, ...columns.map((c) => c.length));
  const height = PAD * 2 + tallest * NODE_H + (tallest - 1) * GAP_Y;
  const width = PAD * 2 + cols * NODE_W + Math.max(0, cols - 1) * GAP_X;
  const byId = new Map(order.map((n) => [n.id, n]));
  const placed = new Map<string, PlacedNode>();
  columns.forEach((c, ci) => {
    const colH = c.length * NODE_H + (c.length - 1) * GAP_Y;
    const top = (height - colH) / 2;
    c.forEach((id, ri) => {
      placed.set(id, { ...byId.get(id)!, x: PAD + ci * (NODE_W + GAP_X), y: top + ri * (NODE_H + GAP_Y), w: NODE_W, h: NODE_H, col: ci, row: ri });
    });
  });

  // 4. edges: merge duplicates, spread ports, curve.
  const merged = new Map<string, { e: FlowEdge; count: number }>();
  for (const e of usable) {
    const key = `${e.from}>${e.to}>${e.kind}>${e.label}>${e.dissent}`;
    const m = merged.get(key);
    if (m) m.count += 1; else merged.set(key, { e, count: 1 });
  }
  const list = [...merged.entries()];
  const port = (id: string, side: "in" | "out", key: string) => {
    const mine = list.filter(([, { e }]) => !back.has(e) && (side === "in" ? e.to === id : e.from === id));
    const sorted = mine.sort((a, b) => placed.get(side === "in" ? a[1].e.from : a[1].e.to)!.y - placed.get(side === "in" ? b[1].e.from : b[1].e.to)!.y);
    const i = sorted.findIndex(([k]) => k === key);
    const k = sorted.length;
    return k <= 1 ? 0 : (i - (k - 1) / 2) * Math.min(12, (NODE_H - 16) / (k - 1));
  };
  const edgesOut: PlacedEdge[] = list.map(([key, { e, count }]) => {
    const a = placed.get(e.from)!;
    const b = placed.get(e.to)!;
    const isBack = back.has(e) || b.col <= a.col;
    if (isBack) {
      // Over the top, unless a node sits above either end in its column: then underneath.
      const below = a.row > 0 || b.row > 0;
      const x1 = a.x + a.w / 2, y1 = below ? a.y + a.h : a.y, x2 = b.x + b.w / 2, y2 = below ? b.y + b.h : b.y;
      const lift = Math.max(36, Math.abs(x2 - x1) / 3);
      const turn = below ? Math.max(y1, y2) + lift : Math.min(y1, y2) - lift;
      return { ...e, key, count, back: true, d: `M ${x1} ${y1} C ${x1} ${turn}, ${x2} ${turn}, ${x2} ${y2}`,
               lx: (x1 + x2) / 2, ly: below ? turn - lift * 0.25 : turn + lift * 0.25 };
    }
    const x1 = a.x + a.w, y1 = a.y + a.h / 2 + port(e.from, "out", key);
    const x2 = b.x, y2 = b.y + b.h / 2 + port(e.to, "in", key);
    const dx = Math.max(24, (x2 - x1) / 2);
    const d = `M ${x1} ${y1} C ${x1 + dx} ${y1}, ${x2 - dx} ${y2}, ${x2} ${y2}`;
    // The label sits at the curve's midpoint (t = .5 of the cubic) — or, for an arrow that
    // skips columns, in the last gap before its target, so it never lands on a node it passes.
    const skips = b.col - a.col > 1;
    return { ...e, key, count, back: false, d, lx: skips ? x2 - GAP_X / 2 : (x1 + x2) / 2, ly: skips ? y2 : (y1 + y2) / 2 };
  });
  // Two labels on one spot (votes fanning into a proposal) step apart vertically.
  const seen: PlacedEdge[] = [];
  for (const e of edgesOut) {
    while (seen.some((o) => Math.abs(o.lx - e.lx) < 56 && Math.abs(o.ly - e.ly) < 14)) e.ly += 15;
    seen.push(e);
  }
  // Top-of-canvas room for arcs.
  const arcBottom = Math.max(height, ...edgesOut.filter((e) => e.back).map((e) => e.ly + 18));
  const arcTop = Math.min(0, ...edgesOut.filter((e) => e.back).map((e) => e.ly - 18));
  if (arcTop < 0) {
    const shift = -arcTop;
    for (const p of placed.values()) p.y += shift;
    for (const e of edgesOut) {
      e.d = e.d.replace(/(-?\d+(?:\.\d+)?) (-?\d+(?:\.\d+)?)/g, (_m, x: string, y: string) => `${x} ${Number(y) + shift}`);
      e.ly += shift;
    }
    return { width, height: arcBottom + shift, nodes: order.map((n) => placed.get(n.id)!), edges: edgesOut, cols };
  }
  return { width, height: arcBottom, nodes: order.map((n) => placed.get(n.id)!), edges: edgesOut, cols };
}

/** The node the eye should go to while an item is live: Needs you, else the furthest node. */
export function activeNodeId(layout: Layout, lifecycle: string): string | null {
  if (lifecycle === "needs_you") return layout.nodes.find((n) => n.type === "needs_you")?.id ?? null;
  if (!["proposed", "claimed", "in_progress"].includes(lifecycle)) return null;
  const last = [...layout.nodes].sort((a, b) => b.col - a.col || a.row - b.row)[0];
  return last?.id ?? null;
}
