// @vitest-environment happy-dom
/**
 * THE GRAPH AREA, TESTER ROUND 3 (b07e24c3e), as classes:
 *   P0  dense Focus rings: labels never pile into a knot, never overlap each other or a node,
 *       never leave the visible canvas (the drawer's edge counts); ring nodes never touch;
 *       a group's label is never cut mid-word;
 *   P1  framing: every opening view shows everything (a page, a Focus), at 100% at most; a
 *       resize or a hide/show re-frames and paints the CURRENT picture, never the first one;
 *   P1  the drawer: the selection and its label come out from under it, the rest where it can;
 *   P1  a class whose methods only call each other says so instead of "0 connected".
 */
import React, { act } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { GraphCanvas, frameExtent, type CanvasNode } from "../app/knowledge/graph-canvas";
import { GraphArea } from "../app/knowledge/graph-area";
import {
  LABEL_ARC, NODE_R, fitTransform, focusLayout, groupFocus, labelsClear, placeNodeLabels, revealPan, tailEllipsis,
  type LabelItem, type Pt,
} from "../lib/kg-layout";
import { FX, mockKgFetch } from "./_helpers/kg-fetch";
import { $, mount, rerender, settle, unmount } from "./_helpers/live-render";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn() }), usePathname: () => "/", useSearchParams: () => new URLSearchParams(window.location.search) }));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
afterEach(() => { unmount(); vi.unstubAllGlobals(); });

/** A 2-hop Focus the size tester round 3 used: 30 on the first ring, 32 + groups on the second. */
function twoHops(first = 30, second = 60) {
  const center = { id: "c", key: "c", kind: "symbol", layer: "code", label: "LoanWritePlatformServiceJpaRepositoryImpl", hop: 0 };
  const nodes = [
    ...Array.from({ length: first }, (_, i) => ({ id: `a${i}`, key: `symbol:r:m/src/p${i % 5}/A${i}.java:A${i}`, kind: "symbol", layer: "code", label: `LoanAccountDomainService${i}`, sub: `m/src/p${i % 5}/A${i}.java`, hop: 1 })),
    ...Array.from({ length: second }, (_, i) => ({ id: `b${i}`, key: `symbol:r:m/src/q${i % 9}/B${i}.java:B${i}`, kind: "symbol", layer: "code", label: `PortfolioAccountReadPlatform${i}`, sub: `m/src/q${i % 9}/B${i}.java`, hop: 2 })),
  ];
  const edges = [
    ...nodes.filter((n) => n.hop === 1).map((n, i) => ({ src: "c", dst: n.id, kind: "calls", weight: 90 - i, label: `${90 - i} calls` })),
    ...nodes.filter((n) => n.hop === 2).map((n, i) => ({ src: `a${i % first}`, dst: n.id, kind: "calls", weight: 40 - (i % 30), label: null })),
  ];
  return { center, nodes: [center, ...nodes], edges };
}

/** What the canvas does each frame, without a canvas: node circles and label items at a transform. */
function frameLabels(pos: Map<string, Pt>, angle: Map<string, number>, k: number, view: { w: number; h: number }, center: string) {
  const fitted = fitTransform([...pos.values()], view, { extent: { x: 190, top: 44, bottom: 44 } });
  const t = { ...fitted, k: Math.min(fitted.k, k) };
  const items: LabelItem[] = [];
  const circles = [];
  for (const [id, p] of pos) {
    const x = p.x * t.k + t.x;
    const y = p.y * t.k + t.y;
    const r = Math.max(5, Math.min(NODE_R * Math.min(1, t.k * 1.25), (LABEL_ARC * t.k - 8) / 2));
    circles.push({ id: `o:${id}`, x, y: y - r, w: r * 2, h: r * 2, priority: 1e9 });
    const a = angle.get(id);
    const w14 = 20 * 7.6 * Math.max(0.8, Math.min(1, t.k));
    items.push({
      id, x, y, r, priority: id === center ? 1e7 : 1e6 - items.length,
      variants: [{ w: w14, h: 18 }, { w: w14 * 0.55, h: 18 }, { w: w14 * 0.32, h: 16 }],
      prefer: a === undefined ? "below" : "out", dir: a === undefined ? undefined : { x: Math.cos(a), y: Math.sin(a) },
      must: id === center,
    });
  }
  return { items, circles, t };
}

describe("P0 dense Focus rings read: no knot, no overlap, nothing past the edge", () => {
  it("2 hops / Show 200: every placed label is clear of every other label and every node, and inside the visible canvas", () => {
    const { center, nodes, edges } = twoHops();
    const grouped = groupFocus(center.id, nodes, edges);
    const { pos, angle } = focusLayout(center.id, grouped.nodes, grouped.edges, { aspect: 1.9 });
    for (const view of [{ w: 1000, h: 520 }, { w: 1300, h: 640 }]) {
      for (const k of [1, 0.8, 0.6]) {
        const { items, circles } = frameLabels(pos, angle, k, view, center.id);
        // The drawer covers the right 360 px: labels stay left of it.
        const bounds = { x0: 4, y0: 4, x1: view.w - 360 - 4, y1: view.h - 4 };
        const placed = placeNodeLabels(items, circles, bounds);
        const boxes = [...placed.values()];
        expect(labelsClear(boxes)).toBe(true);
        for (const [id, b] of placed) {
          expect(b.x - b.w / 2).toBeGreaterThanOrEqual(bounds.x0 - 0.01);
          expect(b.x + b.w / 2).toBeLessThanOrEqual(bounds.x1 + 0.01);
          expect(b.y).toBeGreaterThanOrEqual(bounds.y0 - 0.01);
          expect(b.y + b.h).toBeLessThanOrEqual(bounds.y1 + 0.01);
          const own = `o:${id}`;
          for (const c of circles) {
            if (c.id === own) continue;
            const hit = b.x - b.w / 2 < c.x + c.w / 2 && b.x + b.w / 2 > c.x - c.w / 2 && b.y < c.y + c.h && b.y + b.h > c.y;
            expect(hit).toBe(false);
          }
        }
        // The centre is always named.
        expect(placed.has(center.id)).toBe(true);
      }
    }
  });

  it("at 100% nearly every neighbour is named, most in full (the layout leaves room for labels)", () => {
    const { center, nodes, edges } = twoHops(24, 30);
    const { pos, angle } = focusLayout(center.id, nodes, edges, { aspect: 2 });
    const { items, circles } = frameLabels(pos, angle, 1, { w: 2400, h: 1400 }, center.id);
    const placed = placeNodeLabels(items, circles, { x0: 4, y0: 4, x1: 2396, y1: 1396 });
    const full = [...placed.values()].filter((p) => p.variant === 0).length;
    expect(placed.size / items.length).toBeGreaterThan(0.9);
    expect(full / items.length).toBeGreaterThan(0.6);
  });

  it("a ring's nodes are spaced by arc length: neighbours never touch like beads, at the narrow ends too", () => {
    const { center, nodes, edges } = twoHops(30, 0);
    for (const aspect of [1, 1.6, 2.2]) {
      const { pos } = focusLayout(center.id, nodes, edges, { aspect });
      const ring = nodes.filter((n) => n.hop === 1).map((n) => pos.get(n.id)!);
      const sorted = [...ring].sort((p, q) => Math.atan2(p.y, p.x) - Math.atan2(q.y, q.x));
      for (let i = 0; i < sorted.length; i += 1) {
        const a = sorted[i]; const b = sorted[(i + 1) % sorted.length];
        expect(Math.hypot(a.x - b.x, a.y - b.y)).toBeGreaterThan(LABEL_ARC * 0.9);
      }
    }
  });

  it("the second ring sits clear of the first's labels: the gap is wider than a ring label, horizontally", () => {
    const { center, nodes, edges } = twoHops();
    const { rings, aspect } = focusLayout(center.id, nodes, edges, { aspect: 1.9 });
    expect(rings).toHaveLength(2);
    expect((rings[1] - rings[0]) * aspect).toBeGreaterThan(20 * 7.6);
  });

  it("a group's label is never cut mid-word: the package is shortened from its start", () => {
    expect(tailEllipsis("other packages", 20)).toBe("other packages");
    expect(tailEllipsis("fineract/portfolio/loanaccount/service", 22)).toBe("…/loanaccount/service");
    expect(tailEllipsis("fineract/portfolio/loanaccount/service", 20)).toBe("…/service");
    const { center, nodes, edges } = twoHops(80, 0);
    const g = groupFocus(center.id, nodes, edges).nodes.filter((n) => n.kind === "group");
    expect(g.length).toBeGreaterThan(0);
    for (const n of g) expect(n.label).toMatch(/^\+\d+ in (…\/)?[\w/]+( packages)?$/);
  });

  it("the frame reserves most of a side label beside the outer ring (the rest shortens or moves) and one line above and below", () => {
    const ring: CanvasNode[] = [{ id: "x", label: "PortfolioAccountReadPlatformService", kind: "symbol", layer: "code", token: "--kg-class", bubble: false, labelMode: "out", labelMax: 20 }];
    const ext = frameExtent(ring, "focus");
    expect(ext.x).toBeGreaterThan(20 * 7.6 * 0.7);
    expect(ext.top).toBeLessThan(60);
  });
});

describe("P1 the drawer never covers the selection or its label", () => {
  it("pans the selection out, using the room on the left to bring the rest out too", () => {
    // Visible up to 700 px; the selection's label ends at 760; the picture starts at 300.
    expect(revealPan([{ x: 640, w: 120 }], [{ x: 300, w: 60 }, { x: 680, w: 140 }], 700)).toBe(-136);
    // Not enough room on the left for everything: the selection still comes out.
    expect(revealPan([{ x: 640, w: 120 }], [{ x: 20, w: 60 }, { x: 900, w: 140 }], 700)).toBe(-76);
    expect(revealPan([{ x: 100, w: 50 }], [], 700)).toBe(-0);
  });
});

describe("P1 framing: every picture opens whole; resize and hide/show paint the current one", () => {
  type RO = { cb: () => void };
  const observers: RO[] = [];
  const stubRO = () => vi.stubGlobal("ResizeObserver", class { cb: () => void; constructor(cb: () => void) { this.cb = cb; observers.push(this); } observe() {} disconnect() {} });
  const node = (id: string): CanvasNode => ({ id, label: id, kind: "symbol", layer: "code", token: "--kg-class", bubble: false });
  const spread = (n: number, gap: number) => new Map(Array.from({ length: n }, (_, i) => [`n${i}`, { x: (i % 5) * gap, y: Math.floor(i / 5) * gap }]));

  it("a new picture (a page, a Focus) is framed whole, never left at the last pan; a resize frames the CURRENT one", async () => {
    stubRO();
    const zooms: number[] = [];
    const props = (positions: Map<string, Pt>, fitKey: string) => ({
      nodes: [...positions.keys()].map(node), edges: [], positions, mode: "overview" as const, selectedId: null,
      onSelect: () => {}, onActivate: () => {}, onZoom: (k: number) => zooms.push(k), ariaLabel: "g", fitKey,
    });
    const small = spread(4, 40);
    const big = spread(20, 400);
    await mount(<GraphCanvas {...props(small, "a")} />);
    expect(zooms.at(-1)).toBe(1);
    // Page 2: a new picture opens whole, below 100% if it must, never clipped.
    await rerender(<GraphCanvas {...props(big, "b")} />);
    const wantBig = fitTransform([...big.values()], { w: 1000, h: 520 }, { extent: frameExtent([...big.keys()].map(node), "overview") }).k;
    expect(zooms.at(-1)).toBeCloseTo(wantBig, 5);
    // The window is resized: the observer, registered at mount, frames the picture on screen NOW.
    const wrap = $('[data-testid="kg-canvas-wrap"]')!;
    wrap.getBoundingClientRect = () => ({ width: 800, height: 400, top: 0, left: 0, right: 800, bottom: 400, x: 0, y: 0, toJSON: () => ({}) });
    await act(async () => observers.forEach((o) => o.cb()));
    const want = fitTransform([...big.values()], { w: 800, h: 400 }, { extent: frameExtent([...big.keys()].map(node), "overview") }).k;
    expect(zooms.at(-1)).toBeCloseTo(want, 5);
    // Hidden (0 × 0) keeps the last real size: nothing re-frames to a made-up one.
    const before = zooms.length;
    wrap.getBoundingClientRect = () => ({ width: 0, height: 0, top: 0, left: 0, right: 0, bottom: 0, x: 0, y: 0, toJSON: () => ({}) });
    await act(async () => observers.forEach((o) => o.cb()));
    expect(zooms.length).toBe(before);
  });
});

describe("P1 a class whose methods only call each other", () => {
  it("says what it holds instead of an empty '0 connected'", async () => {
    const center = { ...FX.hood.center, counts: { methods: 12 } };
    window.history.replaceState(null, "", `/?area=knowledge&kg_node=${center.id}`);
    mockKgFetch({
      "/view/neighbourhood": { body: { ...FX.hood, center, nodes: [{ ...center, hop: 0 }], edges: [], truncated: false, hidden: { nodes: 0, edges: 0 } } },
      "/view/node/": { body: { ...FX.detail, node: center } },
    });
    await mount(<GraphArea projectId="p" actor="ceo" />);
    await settle();
    expect($('[data-testid="kg-focus-alone"]')?.textContent).toMatch(/its 12 methods call only each other/);
  });
});
