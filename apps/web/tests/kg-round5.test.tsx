// @vitest-environment happy-dom
/**
 * THE KNOWLEDGE GRAPH, TESTER ROUND 5, as classes:
 *   P1  2 hops / Show 200 were still a hairball: an outer hop is drawn as PACKAGE CLUSTERS on its
 *       ring (opened on click, their classes fanned beyond it), each beside its one way in; the
 *       first ring spaces its nodes by the room their LABELS need, so every one is named;
 *   P1  selecting a bubble re-laid / panned the overview under the panel: the panel reserves its
 *       column; a Whole repo with a rule selected opened as a knot: one framing for every view;
 *   P2  labels cut to one word; the tree contradicting search; mixed time zones; raw ISO
 *       attributes; the System menu ignoring Esc and outside presses; kg_tab sticking.
 */
import { readFileSync } from "node:fs";
import React, { act, useRef } from "react";
import { describe, expect, it } from "vitest";

import { RING_SLOT, layFocus } from "../app/knowledge/graph-view";
import { frameExtent, framePadding } from "../app/knowledge/graph-canvas";
import { attrText } from "../app/knowledge/node-panel";
import { withSearchHits } from "../app/knowledge/project-tree";
import {
  CLUSTER_R, FOCUS_CLUSTERS, NODE_R, RING_ASPECT_MAX, RING_ASPECT_MIN, charEllipsis, distinctTails, fitTransform, focusEdges,
  focusLayout, groupFocus, labelsClear, placeMembers, placeNodeLabels, slotMeasure, twoLines, type LabelItem, type LabelBox,
} from "../lib/kg-layout";
import { kgWhen, localizeName, localizeNames } from "../lib/kg-view";
import { useDetailsDismiss } from "../lib/details-dismiss";
import type { KgSearchHit, KgTreeRow } from "../lib/kg-view-types";
import { $, mount, unmount } from "./_helpers/live-render";

const css = readFileSync("app/knowledge/kg.css", "utf8");

/** A Focus like AbstractLoanRepaymentScheduleTransactionProcessor's: `first` neighbours, `second`
 *  more two hops out across 14 packages in 3 modules, each reached from three first-ring nodes. */
function hood(first: number, second: number) {
  const center = { id: "c", key: "c", kind: "symbol", layer: "code", label: "AbstractLoanRepaymentScheduleTransactionProcessor", hop: 0 };
  const mods = ["fineract-loan", "fineract-core", "fineract-provider"];
  const nodes = [
    ...Array.from({ length: first }, (_, i) => ({ id: `a${i}`, key: `symbol:r:m/a${i % 4}/A${i}.java:A${i}`, kind: "symbol", layer: "code",
      label: `DuePenFeeIntPriInAdvanceLoanRepayment${i}Processor`, sub: `m/a${i % 4}/A${i}.java`, hop: 1 })),
    ...Array.from({ length: second }, (_, i) => {
      const pkg = `${mods[i % 3]}/src/main/java/org/apache/fineract/portfolio/p${i % 14}/${i % 2 ? "service" : "domain"}`;
      return { id: `b${i}`, key: `symbol:r:${pkg}/B${i}.java:B${i}`, kind: "symbol", layer: "code", label: `PortfolioAccountReadPlatform${i}`, sub: `${pkg}/B${i}.java`, hop: 2 };
    }),
  ];
  const edges = [
    ...nodes.filter((n) => n.hop === 1).map((n, i) => ({ src: "c", dst: n.id, kind: "calls", weight: 90 - i, label: `${90 - i} calls` })),
    ...nodes.filter((n) => n.hop === 1).map((n, i) => ({ src: n.id, dst: `a${(i + 11) % first}`, kind: "calls", weight: 5, label: null })),
    ...nodes.filter((n) => n.hop === 2).flatMap((n, i) => [0, 7, 13].map((o) => ({ src: `a${(i + o) % first}`, dst: n.id, kind: "calls", weight: 40 - ((i + o) % 30), label: null }))),
  ];
  return { center, nodes: [center, ...nodes], edges };
}

/** The Focus graph-view lays out (round 5), for a canvas of `view`. */
function lay(first: number, second: number, view: { w: number; h: number }) {
  const { center, nodes, edges } = hood(first, second);
  const res = layFocus(center.id, nodes as never, edges as never, view);
  return { center, g: res.grouped, laid: res.laid, res };
}

/** What the canvas places each frame (graph-canvas draw): the variants it measures, roughly. */
function labelsAt(l: ReturnType<typeof lay>, view: { w: number; h: number }) {
  const { center, g, laid } = l;
  const t = fitTransform([...laid.pos.values()], view, { padding: framePadding("focus"), extent: frameExtent(l.res.canvasNodes, "focus", 1, view.w) });
  const k = Math.min(1, t.k);
  const s = Math.max(0.8, k);
  const items: LabelItem[] = [];
  const circles: LabelBox[] = [];
  for (const n of g.nodes) {
    const p = laid.pos.get(n.id)!;
    const x = p.x * t.k + t.x; const y = p.y * t.k + t.y;
    const cluster = (n as { cluster?: boolean }).cluster;
    const r = cluster ? CLUSTER_R * Math.min(1, k * 1.25) : Math.max(5, Math.min(NODE_R * Math.min(1, k * 1.25), (36 * k - 6) / 2));
    circles.push({ id: `o:${n.id}`, x, y: y - r, w: r * 2, h: r * 2, priority: 1e9 });
    const a = laid.angle.get(n.id);
    const ch = 7.4 * s;
    items.push({
      id: n.id, x, y, r, priority: n.id === center.id ? 8e6 : cluster ? 5e6 : 1e6 - items.length,
      variants: cluster
        ? [{ w: Math.min(34, n.label.length) * ch + 10, h: 34 * s }, { w: 16 * ch + 10, h: 18 * s }]
        : [{ w: Math.min(22, n.label.length) * ch + 10, h: 18 * s }, { w: 18 * ch + 10, h: 34 * s }, { w: 14 * ch + 10, h: 17 * s }],
      prefer: a === undefined ? "below" : "out", dir: a === undefined ? undefined : { x: Math.cos(a), y: Math.sin(a) },
      must: n.id === center.id,
    });
  }
  return { placed: placeNodeLabels(items, circles, { x0: 4, y0: 4, x1: view.w - 4, y1: view.h - 4 }), circles, k: t.k };
}

// The canvas beside the reserved panel in a 1280 × 800 window (the tree a drawer).
const VIEW_1280 = { w: 680, h: 330 };

describe("P1 an outer hop is package clusters, never a ring of single classes", () => {
  for (const [name, second] of [["2 hops", 60], ["Show 200", 170]] as const) {
    it(`${name}: every outer node is in one of at most ${FOCUS_CLUSTERS} clusters, each reached by one drawn edge`, () => {
      const { center, g } = lay(24, second, VIEW_1280);
      const outer = g.nodes.filter((n) => n.hop === 2);
      expect(outer.length).toBeLessThanOrEqual(FOCUS_CLUSTERS);
      expect(outer.every((n) => (n as { cluster?: boolean }).cluster)).toBe(true);
      expect([...g.members.entries()].filter(([id]) => id.startsWith("group:2:")).reduce((s, [, m]) => s + m.length, 0)).toBe(second);
      const hops = new Map(g.nodes.map((n) => [n.id, n.id === center.id ? 0 : n.hop]));
      const drawn = focusEdges(center.id, hops, g.edges);
      for (const c of outer) expect(drawn.filter((e) => e.src === c.id || e.dst === c.id)).toHaveLength(1);
      // Two packages that share their last folders are still told apart.
      const names = outer.map((n) => n.label);
      expect(new Set(names).size).toBe(names.length);
    });
  }

  it("a cluster sits beside its strongest way in (its edge stays short)", () => {
    const { center, g, laid } = lay(24, 60, VIEW_1280);
    const hops = new Map(g.nodes.map((n) => [n.id, n.id === center.id ? 0 : n.hop]));
    const drawn = focusEdges(center.id, hops, g.edges);
    let near = 0; let all = 0;
    for (const c of g.nodes.filter((n) => n.hop === 2)) {
      const e = drawn.find((x) => x.src === c.id || x.dst === c.id)!;
      const parent = e.src === c.id ? e.dst : e.src;
      const d = Math.abs(laid.angle.get(c.id)! - laid.angle.get(parent)!) % (Math.PI * 2);
      all += 1; if (Math.min(d, Math.PI * 2 - d) < Math.PI / 2) near += 1;
    }
    expect(near / all).toBeGreaterThanOrEqual(0.75);
  });

  it("an opened cluster's classes fan out beyond the outer ring; nothing drawn moves", () => {
    const { laid } = lay(24, 60, VIEW_1280);
    const outer = laid.rings[laid.rings.length - 1];
    const at = { x: outer * laid.aspect * 0.7, y: -outer * 0.7 };
    const ids = Array.from({ length: 23 }, (_, i) => `m${i}`);
    const placed = placeMembers(at, outer, laid.aspect, ids);
    expect(placed.size).toBe(23);
    for (const p of placed.values()) expect(Math.hypot(p.x / laid.aspect, p.y)).toBeGreaterThan(outer + 20);
    expect([...placed.keys()].some((id) => laid.pos.has(id))).toBe(false);
  });
});

describe("P1 the first ring spaces its nodes by the room their labels need: 2 hops reads at 1280 × 800", () => {
  for (const [name, second] of [["1 hop", 0], ["2 hops", 60], ["Show 200", 170]] as const) {
    it(`${name}: the centre, every cluster and (nearly) every first-ring node named; no two labels touch; none on a node`, () => {
      const l = lay(34, second, VIEW_1280);
      const { placed, circles, k } = labelsAt(l, VIEW_1280);
      // Fewer on the first ring (the rest in groups) rather than a ring of unnamed ones.
      expect(l.g.nodes.filter((n) => n.hop === 1 && n.kind !== "group").length).toBeGreaterThanOrEqual(4);
      expect(k).toBeGreaterThanOrEqual(0.45);
      const unnamed = l.g.nodes.filter((n) => !placed.has(n.id));
      // At 1 hop every node; at 2 hops (a 330 px tall canvas opens near 50%, and this estimate of
      // label widths is generous) at most four first-ring nodes wait for hover — never a cluster,
      // never the centre.
      expect(unnamed.filter((n) => (n as { cluster?: boolean }).cluster || n.id === l.center.id)).toEqual([]);
      expect(unnamed.length).toBeLessThanOrEqual(second ? 4 : 0);
      expect(labelsClear([...placed.values()])).toBe(true);
      for (const [id, b] of placed) {
        for (const c of circles) {
          if (c.id === `o:${id}`) continue;
          const hit = b.x - b.w / 2 < c.x + c.w / 2 - 1 && b.x + b.w / 2 > c.x - c.w / 2 + 1 && b.y < c.y + c.h - 1 && b.y + b.h > c.y + 1;
          expect(hit ? `${id} on ${c.id}` : "").toBe("");
        }
      }
    });
  }

  it("a ring holds as many label slots as it has nodes, and more on a bigger ring", () => {
    const { laid, g } = lay(16, 0, VIEW_1280);
    const onRing = g.nodes.filter((n) => n.hop === 1).length;
    expect(slotMeasure(laid.rings[0] * laid.aspect, laid.rings[0], RING_SLOT)).toBeGreaterThanOrEqual(onRing - 0.01);
    expect(slotMeasure(200, 100, RING_SLOT)).toBeCloseTo(2 * slotMeasure(100, 50, RING_SLOT), 5);
  });
});

describe("P1 the panel reserves its column; a selection never moves the picture", () => {
  it("tree | canvas | panel, and canvas | panel in a 1280 window; a drawer only in a narrow graph", () => {
    expect(css).toMatch(/\.kg-work \{[^}]*grid-template-columns: 14\.21053rem minmax\(0, 1fr\) var\(--kg-panel-w\);/);
    expect(css).toMatch(/@container kg \(max-width: 1180px\) \{\s*\.kg-work, \.kg-work\[data-tree-collapsed\] \{ grid-template-columns: minmax\(0, 1fr\) var\(--kg-panel-w\); \}/);
    // Outside the narrow graph the panel is in the flow, not over the canvas.
    const wide = css.slice(0, css.indexOf("@container kg (max-width: 860px)"));
    expect(wide).not.toMatch(/\.kg-col-panel \{[^}]*position: absolute/);
  });

  it("the overview layout and the frame key read no selection", () => {
    const src = readFileSync("app/knowledge/graph-view.tsx", "utf8");
    const memo = src.slice(src.indexOf("const overviewPicture = useMemo"), src.indexOf("const picture = useMemo"));
    expect(memo).not.toMatch(/selected/);
    const key = src.slice(src.indexOf("const fitKey ="), src.indexOf("const legend = useMemo"));
    expect(key).not.toMatch(/selected|expanded/);
    expect(key).toMatch(/o\|\$\{area\}/);
    expect(key).toMatch(/f\|\$\{view\.node\}/);
  });
});

describe("P2 labels are cut by character or wrapped, never to one word", () => {
  it("two lines at a word boundary, the rest cut by character", () => {
    expect(twoLines("AdvancedPaymentScheduleTransactionProcessor", 18)).toEqual(["AdvancedPayment", "ScheduleTransacti…"]);
    expect(twoLines("LoanCharge", 18)).toEqual(["LoanCharge"]);
    expect(charEllipsis("CreocoreLoanRepaymentScheduleTransactionProcessor", 14)).toBe("CreocoreLoanR…");
    expect(charEllipsis("Heavens", 14)).toBe("Heavens");
  });

  it("package names that share a tail say their module", () => {
    expect(distinctTails(["fineract-loan/src/x/loanaccount/service", "fineract-core/src/y/loanaccount/service", "a/b/loanschedule/domain"]))
      .toEqual(["fineract-loan/…/loanaccount/service", "fineract-core/…/loanaccount/service", "loanschedule/domain"]);
  });
});

describe("P2 one clock: the viewer's zone, labelled, everywhere", () => {
  it("a run's engine-written UTC time is re-said by kgWhen, once", () => {
    const now = Date.UTC(2026, 9, 25, 9, 0);
    const out = localizeName("B4 · Incident: early repayments · Oct 25, 07:13", now);
    expect(out).toBe(`B4 · Incident: early repayments · ${kgWhen("2026-10-25T07:13:00.000Z")}`);
    expect(localizeName(out, now)).toBe(out);
    expect(localizeName("LoanCharge", now)).toBe("LoanCharge");
    // A date ahead of now by more than a day is last year's.
    expect(localizeName("A scan · Dec 31, 23:00", now)).toBe(`A scan · ${kgWhen("2025-12-31T23:00:00.000Z")}`);
    expect(localizeNames({ nodes: [{ label: "A run · Oct 25, 07:13", sub: "Oct 25, 07:13" }] }).nodes[0].sub).toBe("Oct 25, 07:13");
  });

  it("time attributes read in the viewer's zone; enum values read as words", () => {
    expect(attrText("last_seen_at", "2026-10-25T07:20:40Z")).toBe(kgWhen("2026-10-25T07:20:40Z"));
    expect(attrText("learning_kind", "root_cause")).toBe("root cause");
    expect(attrText("path", "fineract-loan/src/A.java")).toBe("fineract-loan/src/A.java");
  });
});

describe("P2 the tree and the search box agree", () => {
  const row = (id: string, match = true): KgTreeRow => ({ key: id, parent_key: null, level: "item", node_id: id, label: id, kind: "root_cause", layer: "learning", count: 0, has_children: false, match });
  const hit = (id: string, layer: string): KgSearchHit => ({ node: { id, key: id, kind: "root_cause", layer, label: `L ${id}`, trust_tier: "observed" }, match: "text" } as unknown as KgSearchHit);

  it("a learning search finds is a tree match, once", () => {
    const rows = withSearchHits([row("kgn_a")], [hit("kgn_a", "learning"), hit("kgn_b", "learning"), hit("kgn_c", "rules")], "learning");
    expect(rows.map((r) => r.node_id)).toEqual(["kgn_a", "kgn_b"]);
    expect(rows[1].match).toBe(true);
  });

  it("code the tree found stays where it lives; search adds code only when the tree found none", () => {
    const code = (id: string): KgTreeRow => ({ ...row(id), layer: "code", kind: "class" } as KgTreeRow);
    expect(withSearchHits([code("kgn_x")], [hit("kgn_y", "code")], "project").map((r) => r.node_id)).toEqual(["kgn_x"]);
    expect(withSearchHits([], [hit("kgn_y", "code")], "project").map((r) => r.node_id)).toEqual(["kgn_y"]);
  });
});

describe("P2 a header menu closes on Esc and on a press outside it", () => {
  function Menu() {
    const ref = useRef<HTMLDetailsElement>(null);
    const { onToggle } = useDetailsDismiss(ref);
    return <div><details ref={ref} onToggle={onToggle} data-testid="m"><summary>System</summary><p>panel</p></details><p data-testid="out">elsewhere</p></div>;
  }
  const open = async () => {
    const d = $('[data-testid="m"]') as HTMLDetailsElement;
    await act(async () => { d.open = true; d.dispatchEvent(new Event("toggle")); });
    return d;
  };

  it("Esc", async () => {
    await mount(<Menu />);
    const d = await open();
    await act(async () => { document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })); });
    expect(d.open).toBe(false);
    unmount();
  });

  it("a press outside", async () => {
    await mount(<Menu />);
    const d = await open();
    await act(async () => { $('[data-testid="out"]')!.dispatchEvent(new Event("pointerdown", { bubbles: true })); });
    expect(d.open).toBe(false);
    unmount();
  });
});

describe("P2 the URL says the tab on screen; the first render reads no URL", () => {
  it("kg_tab follows the tab with replaceState(null); the deep link applies after hydration", () => {
    const src = readFileSync("app/knowledge/graph-area.tsx", "utf8");
    expect(src).toMatch(/useState<Deep>\(\{ tab: null, node: null, scan: null, sub: null \}\)/);
    expect(src).not.toMatch(/useState<Deep>\(\(\) => readDeep\(\)\)/);
    expect(src).toMatch(/window\.history\.replaceState\(null, "", url\.toString\(\)\);/);
    expect(src).not.toMatch(/replaceState\(window\.history\.state/);
  });
});
