/**
 * THE GRAPH'S PICTURE, TESTER ROUND 4, as classes:
 *   P0  2 hops / Show 200 drew a solid knot of edges at the centre and overlapping inner labels:
 *       a Focus draws the edges that say how each node is reached (focusEdges), names its first
 *       ring and the selection's neighbours always and never on each other (keep), and the outer
 *       ring on hover or zoom (LABEL_ZOOM_OUTER);
 *   P1  1 hop: ring nodes drawn without labels, labels floating inside the ring, names cut mid-word;
 *   P1  after a deselect the Rules and Learning bubbles lost their labels and the overview kept the
 *       position it was panned to for the drawer.
 */
import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

import {
  LABEL_ARC, LABEL_ZOOM_OUTER, NODE_R, fitTransform, focusEdges, focusKeepLabels, focusLayout, groupFocus, labelsClear,
  placeNodeLabels, wordEllipsis, type LabelItem, type Pt,
} from "../lib/kg-layout";

function twoHops(first = 30, second = 60) {
  const center = { id: "c", key: "c", kind: "symbol", layer: "code", label: "AbstractLoanRepaymentScheduleTransactionProcessor", hop: 0 };
  const nodes = [
    ...Array.from({ length: first }, (_, i) => ({ id: `a${i}`, key: `symbol:r:m/src/p${i % 5}/A${i}.java:A${i}`, kind: "symbol", layer: "code", label: `DuePenFeeIntPriInAdvanceLoanRepayment${i}Processor`, sub: `m/src/p${i % 5}/A${i}.java`, hop: 1 })),
    ...Array.from({ length: second }, (_, i) => ({ id: `b${i}`, key: `symbol:r:m/src/q${i % 9}/B${i}.java:B${i}`, kind: "symbol", layer: "code", label: `PortfolioAccountReadPlatform${i}`, sub: `m/src/q${i % 9}/B${i}.java`, hop: 2 })),
  ];
  const edges = [
    ...nodes.filter((n) => n.hop === 1).map((n, i) => ({ src: "c", dst: n.id, kind: "calls", weight: 90 - i, label: `${90 - i} calls` })),
    // Chords inside the first ring: what knotted the centre.
    ...nodes.filter((n) => n.hop === 1).map((n, i) => ({ src: n.id, dst: `a${(i + 11) % first}`, kind: "calls", weight: 5, label: null })),
    // Every outer node reached from three inner ones.
    ...nodes.filter((n) => n.hop === 2).flatMap((n, i) => [0, 7, 13].map((o) => ({ src: `a${(i + o) % first}`, dst: n.id, kind: "calls", weight: 40 - ((i + o) % 30), label: null }))),
  ];
  return { center, nodes: [center, ...nodes], edges };
}

describe("P0 a Focus draws how each node is reached, not every edge", () => {
  const { center, nodes, edges } = twoHops();
  const grouped = groupFocus(center.id, nodes, edges);
  const hops = new Map(grouped.nodes.map((n) => [n.id, n.hop]));

  it("every edge to the centre; one way in for each outer node; no chord across a ring", () => {
    const drawn = focusEdges(center.id, hops, grouped.edges);
    const toCentre = grouped.edges.filter((e) => e.src === center.id || e.dst === center.id);
    expect(drawn.filter((e) => e.src === center.id || e.dst === center.id)).toHaveLength(toCentre.length);
    expect(drawn.filter((e) => hops.get(e.src) === hops.get(e.dst))).toEqual([]);
    for (const [id, h] of hops) {
      if (h < 2) continue;
      expect(drawn.filter((e) => e.src === id || e.dst === id)).toHaveLength(1);
    }
    expect(drawn.length).toBeLessThan(grouped.edges.length / 2);
  });

  it("the selection's own edges are all drawn", () => {
    const sel = grouped.nodes.find((n) => n.hop === 1 && n.kind !== "group")!.id;
    const drawn = focusEdges(center.id, hops, grouped.edges, sel);
    const own = grouped.edges.filter((e) => e.src === sel || e.dst === sel);
    for (const e of own) expect(drawn).toContain(e);
  });

  it("the first ring and the selection's neighbours are the labels that always show", () => {
    const keep = focusKeepLabels(center.id, hops, grouped.edges, null);
    for (const [id, h] of hops) expect(keep.has(id)).toBe(h <= 1);
    const outer = grouped.nodes.find((n) => n.hop === 2 && n.kind !== "group")!.id;
    const withSel = focusKeepLabels(center.id, hops, grouped.edges, outer);
    const neighbours = grouped.edges.filter((e) => e.src === outer || e.dst === outer).map((e) => (e.src === outer ? e.dst : e.src));
    for (const id of neighbours) expect(withSel.has(id)).toBe(true);
  });
});

/** What the canvas does each frame (graph-canvas draw), without a canvas. */
function frame(pos: Map<string, Pt>, angle: Map<string, number>, ring: Map<string, number>, keep: Set<string>, k: number, view: { w: number; h: number }, center: string) {
  const fitted = fitTransform([...pos.values()], view, { extent: { x: 190, top: 44, bottom: 44 } });
  const t = { ...fitted, k: Math.min(fitted.k, k) };
  const items: LabelItem[] = [];
  const circles = [];
  for (const [id, p] of pos) {
    const x = p.x * t.k + t.x;
    const y = p.y * t.k + t.y;
    const r = Math.max(5, Math.min(NODE_R * Math.min(1, t.k * 1.25), (LABEL_ARC * t.k - 8) / 2));
    circles.push({ id: `o:${id}`, x, y: y - r, w: r * 2, h: r * 2, priority: 1e9 });
    // The canvas's zoom culling: an outer node that is not kept waits for hover or zoom.
    if (id !== center && !keep.has(id) && (ring.get(id) ?? 0) >= 1 && t.k < LABEL_ZOOM_OUTER) continue;
    const a = angle.get(id);
    const w14 = 24 * 7.6 * Math.max(0.8, Math.min(1, t.k));
    items.push({
      id, x, y, r, priority: id === center ? 1e7 : 1e6 - items.length,
      variants: [{ w: w14, h: 18 }, { w: w14 * 0.55, h: 18 }, { w: w14 * 0.32, h: 16 }],
      prefer: a === undefined ? "below" : "out", dir: a === undefined ? undefined : { x: Math.cos(a), y: Math.sin(a) },
      must: id === center, keep: keep.has(id),
    });
  }
  return { items, circles, t };
}

describe("P0/P1 every first-ring node is named, no two labels touch, none floats inside the ring", () => {
  for (const [name, first, second] of [["1 hop", 34, 0], ["2 hops", 30, 60], ["Show 200", 28, 170]] as const) {
    it(name, () => {
      const { center, nodes, edges } = twoHops(first, second);
      const grouped = groupFocus(center.id, nodes, edges);
      const hops = new Map(grouped.nodes.map((n) => [n.id, n.hop]));
      const keep = focusKeepLabels(center.id, hops, grouped.edges, null);
      const { pos, angle, ring } = focusLayout(center.id, grouped.nodes, grouped.edges, { aspect: 1.4 });
      for (const view of [{ w: 460, h: 330 }, { w: 1000, h: 520 }]) {
        for (const k of [1, 0.6, 0.42]) {
          const { items, circles, t } = frame(pos, angle, ring, keep, k, view, center.id);
          const bounds = { x0: 4, y0: 4, x1: view.w - 4, y1: view.h - 4 };
          const placed = placeNodeLabels(items, circles, bounds);
          const boxes = [...placed.values()];
          expect(labelsClear(boxes)).toBe(true);
          for (const id of keep) expect(placed.has(id)).toBe(true);
          // Outward or along its ring, never toward the centre.
          const c = { x: t.x, y: t.y };
          for (const [id, b] of placed) {
            if (id === center.id) continue;
            const p = pos.get(id)!;
            const node = { x: p.x * t.k + t.x, y: p.y * t.k + t.y };
            const label = { x: b.x, y: b.y + b.h / 2 };
            expect(Math.hypot(label.x - c.x, label.y - c.y)).toBeGreaterThan(Math.hypot(node.x - c.x, node.y - c.y) * 0.98 - 1);
          }
        }
      }
    });
  }
});

describe("P1 a name is cut at a word, never inside one", () => {
  const whole = (full: string, cut: string) => {
    const [head, tail] = cut.split("…");
    return full.startsWith(head) && full.endsWith(tail ?? "");
  };
  it.each([
    ["DuePenFeeIntPriInAdvancePriPenFeeIntLoanRepaymentScheduleTransactionProcessor", 24],
    ["CreocoreLoanRepaymentScheduleTransactionProcessor", 20],
    ["fineract-investor-reports-core", 16],
    ["AbstractLoanRepaymentScheduleTransactionProcessor", 14],
  ])("%s at %i", (full, max) => {
    const cut = wordEllipsis(full, max);
    expect(cut.length).toBeLessThanOrEqual(max);
    expect(whole(full, cut)).toBe(true);
    // The boundary on each side of "…" is a word boundary: the next character starts a word.
    const [head, tail] = cut.split("…");
    if (head) expect(/[A-Z\-_./ ]/.test(full.charAt(head.length)) || head.endsWith("-")).toBe(true);
    if (tail) {
      const before = full.charAt(full.length - tail.length - 1);
      expect(/[a-z0-9\-_./ ]/.test(before) && (/[A-Z\-_./]/.test(tail.charAt(0)) || /[-_./]$/.test(before))).toBe(true);
    }
    expect(head.length + tail.length).toBeGreaterThan(0);
  });
  it("short names are left whole", () => {
    expect(wordEllipsis("LoanTransaction", 20)).toBe("LoanTransaction");
  });
});

describe("P1 the drawer opening and closing never leaves the overview changed", () => {
  const src = readFileSync("app/knowledge/graph-canvas.tsx", "utf8");
  const view = readFileSync("app/knowledge/graph-view.tsx", "utf8");
  it("a drawer change repaints with its own bounds", () => {
    expect(src).toMatch(/useEffect\(\(\) => \{ schedule\(\); \}, \[schedule, draw, selectedId, hover, occludeRight\]\);/);
  });
  it("closing it pans the picture back by what opening it moved", () => {
    expect(src).toContain("revealed.current += dx");
    expect(src).toMatch(/x: t\.current\.x - revealed\.current/);
  });
  it("every Whole-repo bubble, Rules and Learning included, keeps its label", () => {
    expect(view).toContain("keepLabel: true");
  });
});
