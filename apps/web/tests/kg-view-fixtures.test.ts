/**
 * THE GRAPH FIXTURES ARE THE CONTRACT (KNOWLEDGE_GRAPH_PRODUCT_SPEC.md §6, Phase 0).
 *
 * The Graph area (S4) builds against tests/fixtures/kg-*.json before the routes exist, so each
 * one must stay in the shape packages/api-client declares and the routes will return. Code rows
 * are real northpeak-bank-b rows; rules and learnings are composed on real code rows (none
 * exist yet) and say so in `_fixture`.
 */
import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

import {
  KG_LAYERS, KG_LEARNING_KINDS, KG_RULE_CATEGORIES, KG_RULE_STATUSES, KG_TRUST_TIERS, KG_VIEW_EDGE_KINDS,
  productLayerOf,
  type KgLiveResponse, type KgNeighbourhoodResponse, type KgNodeDetailResponse, type KgOverviewResponse,
  type KgRulesResponse, type KgSearchResponse, type KgTreeChildrenResponse, type KgViewEdge, type KgViewNode,
} from "../lib/kg-view-types";

const load = <T,>(name: string): T => JSON.parse(readFileSync(`tests/fixtures/kg-${name}.json`, "utf8")) as T;
const overview = load<KgOverviewResponse>("overview");
const hood = load<KgNeighbourhoodResponse>("neighbourhood");
const detail = load<KgNodeDetailResponse>("node-detail");
const tree = load<Record<string, KgTreeChildrenResponse>>("tree");
const search = load<KgSearchResponse>("search");
const rules = load<KgRulesResponse>("rules-review");
const live = load<KgLiveResponse>("learning-live");

const LAYERS = new Set<string>([...KG_LAYERS, "domain"]);
function node(n: KgViewNode) {
  expect(typeof n.id, JSON.stringify(n)).toBe("string");
  expect(n.key.length).toBeGreaterThan(0);
  expect(n.label.length).toBeGreaterThan(0);
  expect(LAYERS.has(n.layer), n.layer).toBe(true);
  expect(KG_TRUST_TIERS as readonly string[]).toContain(n.trust_tier);
  if (n.kind !== "layer") expect(productLayerOf(n.kind), n.kind).toBe(n.layer);
  if (n.status) expect(KG_RULE_STATUSES as readonly string[]).toContain(n.status);
}
function edges(es: KgViewEdge[], ids: Set<string>) {
  for (const e of es) {
    expect(KG_VIEW_EDGE_KINDS as readonly string[]).toContain(e.kind);
    expect(ids.has(e.src) && ids.has(e.dst), `${e.src} → ${e.dst}`).toBe(true);
    expect(e.weight).toBeGreaterThan(0);
    if (e.label !== null) expect(e.label).toMatch(/^[\d,]+ \w+/);
  }
}

describe("the Graph fixtures hold the Phase 0 contract", () => {
  it("overview: 36 real Fineract areas, their call edges, and the two layer bubbles", () => {
    overview.areas.forEach(node);
    overview.layers.forEach(node);
    expect(overview.areas).toHaveLength(36);
    expect(overview.layers.map((l) => l.id)).toEqual(["layer:rules", "layer:learning"]);
    edges(overview.edges, new Set([...overview.areas, ...overview.layers].map((n) => n.id)));
    expect(overview.edges[0]).toMatchObject({ kind: "calls", label: "16,749 calls" });
    expect(overview.totals).toEqual({ nodes: 51319, edges: 234187 });
  });

  it("neighbourhood: a centre at hop 0, neighbours at their hop, truncation counted", () => {
    expect(hood.center.hop).toBe(0);
    hood.nodes.forEach(node);
    expect(hood.nodes.length).toBeLessThanOrEqual(hood.limit + 1);
    expect(hood.nodes.every((n) => n.hop >= 0 && n.hop <= hood.depth)).toBe(true);
    edges(hood.edges, new Set(hood.nodes.map((n) => n.id)));
    expect(hood.truncated).toBe(hood.hidden.nodes > 0);
  });

  it("node detail: every section the panel draws is present", () => {
    node(detail.node);
    expect(detail.connections.length).toBeGreaterThan(0);
    for (const g of detail.connections) {
      expect(g.items.length).toBeLessThanOrEqual(Math.min(20, g.total));
      g.items.forEach(node);
    }
    expect(detail.vector.embedded).toBe(false);
    expect(detail.provenance.events.length).toBeGreaterThan(0);
  });

  it("tree: each level's rows name their parent", () => {
    for (const [name, page] of Object.entries(tree)) {
      if (name === "_fixture") continue;
      expect(page.rows.length, name).toBeGreaterThan(0);
      for (const r of page.rows) {
        expect(r.parent_key, `${name} ${r.key}`).toBe(page.parent);
        expect(LAYERS.has(r.layer)).toBe(true);
      }
    }
  });

  it("search: name-only hits, ranked exact > prefix > text", () => {
    const order = { exact: 0, prefix: 1, text: 2, meaning: 3 };
    search.hits.forEach((h) => node(h.node));
    const ranks = search.hits.map((h) => order[h.match]);
    expect(ranks).toEqual([...ranks].sort((a, b) => a - b));
    expect(search.meaning).toBe(false);
  });

  it("rules review: counts agree with the items; every rule has real evidence", () => {
    const counted = Object.fromEntries(KG_RULE_STATUSES.map((s) => [s, rules.items.filter((r) => r.status === s).length]));
    expect(rules.counts).toEqual(counted);
    for (const r of rules.items) {
      node(r.node);
      expect(KG_RULE_CATEGORIES as readonly string[]).toContain(r.category);
      expect(r.evidence.length).toBeGreaterThan(0);
      for (const e of r.evidence) expect(e.symbol_id).toMatch(/^kgn_[0-9a-f]{20}$/);
      expect(r.node.trust_tier === "approved").toBe(r.status === "approved");
    }
  });

  it("learning live: learned items carry provenance and evidence", () => {
    for (const l of live.learnings) {
      node(l.node);
      expect(KG_LEARNING_KINDS as readonly string[]).toContain(l.learning_kind);
      expect(l.evidence.length).toBeGreaterThan(0);
      expect(l.provenance.agent_role).toBeTruthy();
      expect(l.provenance.run_id).toBeTruthy();
    }
    expect(live.cursor).toBeTruthy();
  });
});
