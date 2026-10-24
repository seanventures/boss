/**
 * THE GRAPH'S GEOMETRY IS DETERMINISTIC AND ON SPEC (KNOWLEDGE_GRAPH_PRODUCT_SPEC.md §5.3).
 * Same data, same picture; the fit keeps 48 px; zoom stays in 0.2×–4× and keeps the point under
 * the cursor; arrowheads are 15 × 10 px ending on the rim; edges are 1.5–3 px on a log scale.
 * Run against the real Fineract overview (36 areas, 191 call edges) and the 81-class neighbourhood.
 */
import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

import {
  ARROW_LEN, ARROW_WIDTH, BUBBLE_R, EDGE_MAX, EDGE_MIN, FIT_PADDING, NODE_R, ZOOM_MAX, ZOOM_MIN, bidirectional, bounds,
  clampZoom, edgeGeometry, edgeWidth, fitTransform, focusLayout, hashSeed, kindName, kindToken, middleEllipsis, nodeAt,
  overviewLayout, placeLabels, placeNear, ringCapacity, seeded, toScreen, toWorld, weightOrder, zoomAt,
} from "../lib/kg-layout";
import type { KgNeighbourhoodResponse, KgOverviewResponse } from "../lib/kg-view-types";

const overview = JSON.parse(readFileSync("tests/fixtures/kg-overview.json", "utf8")) as KgOverviewResponse;
const hood = JSON.parse(readFileSync("tests/fixtures/kg-neighbourhood.json", "utf8")) as KgNeighbourhoodResponse;
const all = [...overview.areas, ...overview.layers];
const pins = overview.layers.map((l) => l.id);

describe("determinism", () => {
  it("the seed and the PRNG are pure functions of the key", () => {
    expect(hashSeed("area:x")).toBe(hashSeed("area:x"));
    expect(hashSeed("area:x")).not.toBe(hashSeed("area:y"));
    const a = seeded(42); const b = seeded(42);
    expect([a(), a(), a()]).toEqual([b(), b(), b()]);
  });

  it("the overview gives the same picture for the same data, whatever the input order", () => {
    const one = overviewLayout(all, overview.edges, pins);
    const two = overviewLayout([...all].reverse(), [...overview.edges].reverse(), pins);
    expect([...one.entries()].sort()).toEqual([...two.entries()].sort());
    expect(one.size).toBe(all.length);
  });

  it("areas do not sit on top of each other, and the layer bubbles are pinned to the right", () => {
    const pos = overviewLayout(all, overview.edges, pins);
    const areas = overview.areas.map((a) => pos.get(a.id)!);
    let closest = Infinity;
    for (let i = 0; i < areas.length; i += 1) for (let j = i + 1; j < areas.length; j += 1) {
      closest = Math.min(closest, Math.hypot(areas[i].x - areas[j].x, areas[i].y - areas[j].y));
    }
    expect(closest).toBeGreaterThan(130);
    const maxX = Math.max(...areas.map((p) => p.x));
    for (const id of pins) expect(pos.get(id)!.x).toBeGreaterThan(maxX);
    expect(pos.get("layer:rules")!.y).toBeLessThan(pos.get("layer:learning")!.y);
  });

  it("focus is radial by hop: the centre at the origin, the heaviest neighbour at the top", () => {
    const { pos, rings } = focusLayout(hood.center.id, hood.nodes, hood.edges);
    expect(pos.get(hood.center.id)).toEqual({ x: 0, y: 0 });
    expect(pos.size).toBe(hood.nodes.length);
    const heaviest = [...hood.edges].sort((a, b) => b.weight - a.weight)[0];
    const top = pos.get(heaviest.dst)!;
    expect(top.x).toBeCloseTo(0, 0);
    expect(top.y).toBeLessThan(0);
    // 80 neighbours do not fit one ring: they continue outwards, every ring within capacity.
    expect(rings.length).toBeGreaterThan(1);
    const r = (id: string) => Math.round(Math.hypot(pos.get(id)!.x, pos.get(id)!.y));
    for (const ring of rings) {
      const on = hood.nodes.filter((n) => n.id !== hood.center.id && Math.abs(r(n.id) - ring) < 2).length;
      expect(on).toBeLessThanOrEqual(ringCapacity(ring));
    }
    expect(focusLayout(hood.center.id, [...hood.nodes].reverse(), hood.edges).pos).toEqual(pos);
  });

  it("a second hop sits near what it connects to on the first", () => {
    const nodes = [{ id: "c", hop: 0 }, { id: "a", hop: 1 }, { id: "b", hop: 1 }, { id: "a2", hop: 2 }];
    const edges = [{ src: "c", dst: "a", weight: 9 }, { src: "c", dst: "b", weight: 1 }, { src: "a", dst: "a2", weight: 1 }];
    const { pos } = focusLayout("c", nodes, edges);
    const ang = (id: string) => Math.atan2(pos.get(id)!.y, pos.get(id)!.x);
    expect(Math.abs(ang("a2") - ang("a"))).toBeLessThan(0.01);
    expect(Math.hypot(pos.get("a2")!.x, pos.get("a2")!.y)).toBeGreaterThan(Math.hypot(pos.get("a")!.x, pos.get("a")!.y));
  });

  it("a node that arrives live is placed beside its anchor, clear of what is drawn", () => {
    const taken = [{ x: 0, y: 0 }, { x: 70, y: 0 }, { x: -70, y: 0 }];
    const p = placeNear({ x: 0, y: 0 }, taken, "kgn_new");
    expect(Math.hypot(p.x, p.y)).toBeLessThanOrEqual(140);
    for (const t of taken) expect(Math.hypot(t.x - p.x, t.y - p.y)).toBeGreaterThanOrEqual(56);
    expect(placeNear({ x: 0, y: 0 }, taken, "kgn_new")).toEqual(p);
  });
});

describe("fit and zoom", () => {
  it("fits the bounding box with 48 px of padding (plus the label room) and centres it", () => {
    const pts = [{ x: -500, y: -200 }, { x: 500, y: 200 }];
    const view = { w: 1200, h: 700 };
    const t = fitTransform(pts, view, { extent: { x: 0, top: 0, bottom: 0 } });
    const a = toScreen(t, pts[0]); const b = toScreen(t, pts[1]);
    expect(Math.min(a.x, view.w - b.x)).toBeGreaterThanOrEqual(FIT_PADDING - 0.01);
    expect(Math.min(a.y, view.h - b.y)).toBeGreaterThanOrEqual(FIT_PADDING - 0.01);
    expect((a.x + b.x) / 2).toBeCloseTo(view.w / 2, 5);
    // The tight axis touches exactly the padding.
    expect(Math.min(a.x, a.y)).toBeCloseTo(FIT_PADDING, 5);
  });

  it("fits again on resize: a smaller window, a smaller scale, still inside", () => {
    const pos = overviewLayout(all, overview.edges, pins);
    const pts = [...pos.values()];
    const big = fitTransform(pts, { w: 1400, h: 800 });
    const small = fitTransform(pts, { w: 700, h: 500 });
    expect(small.k).toBeLessThan(big.k);
    const bb = bounds(pts.map((p) => toScreen(small, p)));
    expect(bb.minX).toBeGreaterThanOrEqual(0);
    expect(bb.maxX).toBeLessThanOrEqual(700);
  });

  it("one node never fits to a giant coin, and an empty view is identity-ish", () => {
    expect(fitTransform([{ x: 0, y: 0 }], { w: 800, h: 600 }).k).toBeLessThanOrEqual(1.25);
    expect(fitTransform([], { w: 800, h: 600 }).k).toBeGreaterThan(0);
  });

  it("zoom is clamped to 0.2×–4× and keeps the point under the cursor still", () => {
    expect(clampZoom(10)).toBe(ZOOM_MAX);
    expect(clampZoom(0.01)).toBe(ZOOM_MIN);
    const t = { k: 1, x: 100, y: 50 };
    const at = { x: 400, y: 300 };
    const before = toWorld(t, at);
    const z = zoomAt(t, 1.5, at);
    const after = toWorld(z, at);
    expect(after.x).toBeCloseTo(before.x, 6);
    expect(after.y).toBeCloseTo(before.y, 6);
    expect(zoomAt({ k: 3.9, x: 0, y: 0 }, 2, at).k).toBe(ZOOM_MAX);
  });
});

describe("edges", () => {
  it("width is 1.5 px, rising on a log scale to 3 px at the heaviest", () => {
    const max = 16749;
    expect(edgeWidth(1, max)).toBe(EDGE_MIN);
    expect(edgeWidth(max, max)).toBe(EDGE_MAX);
    expect(edgeWidth(100, max)).toBeGreaterThan(EDGE_MIN);
    expect(edgeWidth(100, max)).toBeLessThan(EDGE_MAX);
    expect(edgeWidth(1e9, max)).toBe(EDGE_MAX);
    expect(edgeWidth(5, 1)).toBe(EDGE_MIN);
    for (const e of overview.edges) {
      const w = edgeWidth(e.weight, max);
      expect(w).toBeGreaterThanOrEqual(1.5);
      expect(w).toBeLessThanOrEqual(3);
    }
  });

  it("the arrowhead is 15 px long and 10 px wide, its tip on the destination rim", () => {
    const g = edgeGeometry({ x: 0, y: 0 }, { x: 200, y: 0 }, NODE_R, NODE_R);
    expect(g.tip).toEqual({ x: 200 - NODE_R, y: 0 });
    expect(g.end.x).toBeCloseTo(200 - NODE_R - ARROW_LEN, 6);
    expect(Math.hypot(g.left.x - g.right.x, g.left.y - g.right.y)).toBeCloseTo(ARROW_WIDTH, 6);
    expect(g.start).toEqual({ x: NODE_R, y: 0 });
    const diag = edgeGeometry({ x: 0, y: 0 }, { x: 300, y: 400 }, BUBBLE_R, BUBBLE_R);
    expect(Math.hypot(diag.tip.x - 300, diag.tip.y - 400)).toBeCloseTo(BUBBLE_R, 6);
    expect(Math.hypot(diag.tip.x - diag.end.x, diag.tip.y - diag.end.y)).toBeCloseTo(ARROW_LEN, 6);
  });

  it("a bidirectional pair is two curves bent apart, each still ending on the rim", () => {
    const pairs = bidirectional([{ src: "a", dst: "b" }, { src: "b", dst: "a" }, { src: "a", dst: "c" }]);
    expect([...pairs].sort()).toEqual(["a\u0000b", "b\u0000a"]);
    const ab = edgeGeometry({ x: 0, y: 0 }, { x: 200, y: 0 }, NODE_R, NODE_R, 0.16);
    const ba = edgeGeometry({ x: 200, y: 0 }, { x: 0, y: 0 }, NODE_R, NODE_R, 0.16);
    expect(Math.sign(ab.ctrl.y)).toBe(-Math.sign(ba.ctrl.y));
    expect(Math.hypot(ab.tip.x - 200, ab.tip.y)).toBeCloseTo(NODE_R, 6);
    // Fineract has real bidirectional area pairs (provider ⇄ loan, …).
    expect(bidirectional(overview.edges).size).toBeGreaterThan(0);
  });
});

describe("hit testing, labels, keyboard order, kinds", () => {
  it("finds the node under the pointer within its rim plus 4 px", () => {
    const nodes = [{ id: "a", x: 100, y: 100, r: 14 }, { id: "b", x: 140, y: 100, r: 14 }];
    expect(nodeAt({ x: 100, y: 117 }, nodes)).toBe("a");
    expect(nodeAt({ x: 125, y: 100 }, nodes)).toBe("b");
    expect(nodeAt({ x: 100, y: 130 }, nodes)).toBeNull();
  });

  it("middle-ellipsises at 28 characters, keeping both ends", () => {
    const s = middleEllipsis("LoanWritePlatformServiceJpaRepositoryImpl");
    expect(s).toHaveLength(28);
    expect(s.startsWith("LoanWritePlatf")).toBe(true);
    expect(s.endsWith("ositoryImpl")).toBe(true);
    expect(middleEllipsis("Loan")).toBe("Loan");
  });

  it("places labels greedily by priority and holds back the ones that would collide", () => {
    const shown = placeLabels([
      { id: "low", x: 100, y: 100, w: 80, h: 20, priority: 1 },
      { id: "high", x: 110, y: 105, w: 80, h: 20, priority: 9 },
      { id: "far", x: 400, y: 100, w: 80, h: 20, priority: 1 },
    ]);
    expect([...shown].sort()).toEqual(["far", "high"]);
  });

  it("Tab order is by weight, the centre first", () => {
    expect(weightOrder(["a", "b", "c"], [{ src: "a", dst: "b", weight: 1 }, { src: "c", dst: "b", weight: 9 }], "a")).toEqual(["a", "b", "c"]);
    expect(weightOrder(["a", "b", "c"], [{ src: "a", dst: "b", weight: 1 }, { src: "c", dst: "b", weight: 9 }])).toEqual(["b", "c", "a"]);
  });

  it("every kind maps to a kg.css token and a name; no literal colour anywhere", () => {
    const css = readFileSync("app/knowledge/kg.css", "utf8");
    const kinds: Array<[string, string?]> = [["symbol", "class"], ["symbol", "interface"], ["symbol", "enum"], ["symbol", "record"],
      ["symbol", "method"], ["symbol", "function"], ["area"], ["package"], ["rule"], ["incident"], ["root_cause"], ["fix"], ["precedent"],
      ["investigation"], ["test_failure"], ["workaround"], ["review_finding"], ["decision"], ["fact"], ["run"], ["pull_request"],
      ["endpoint"], ["table"]];
    for (const [k, s] of kinds) {
      const token = kindToken(k, s);
      expect(token, k).toMatch(/^--kg-/);
      expect(css, token).toContain(`${token}:`);
      expect(kindName(k, s)).not.toMatch(/_/);
    }
    expect(kindToken("layer", null, "layer:rules")).toBe("--kg-layer-rules");
    expect(kindName("layer", null, "layer:learning")).toBe("Learning layer");
    // The interface is the operator's sky blue, the class the console blue.
    expect(css).toContain("--kg-interface: #7dd3fc;");
    expect(css).toContain("--kg-class: #5b9dff;");
    // Only kg.css holds hex colours: the area's TSX and the layout never spell one.
    for (const f of ["lib/kg-layout.ts", "app/knowledge/graph-canvas.tsx", "app/knowledge/graph-view.tsx", "app/knowledge/node-panel.tsx",
      "app/knowledge/project-tree.tsx", "app/knowledge/rules-review.tsx", "app/knowledge/kg-states.tsx", "app/knowledge/learned-strip.tsx",
      "app/knowledge/kg-settings.tsx", "app/knowledge/graph-area.tsx"]) {
      expect(readFileSync(f, "utf8"), f).not.toMatch(/#[0-9a-fA-F]{6}\b|rgba?\(/);
    }
  });
});
