/**
 * The Graph area's routes, answered from the Phase 0 fixtures (tests/fixtures/kg-*.json, real
 * northpeak-bank-b rows in the frozen wire shapes). `mockKgFetch(over)` stubs global fetch; an
 * override answers one path (a status + body, or a function of the URL) so a test can put the
 * area in any state: indexing, an error, an empty layer, a truncated neighbourhood.
 */
import { readFileSync } from "node:fs";
import { vi } from "vitest";

import type {
  KgLiveResponse, KgNeighbourhoodResponse, KgNodeDetailResponse, KgOverviewResponse, KgRulesResponse,
  KgSearchResponse, KgTreeChildrenResponse,
} from "../../lib/kg-view-types";

const load = <T,>(name: string): T => JSON.parse(readFileSync(`tests/fixtures/kg-${name}.json`, "utf8")) as T;

export const FX = {
  overview: load<KgOverviewResponse>("overview"),
  hood: load<KgNeighbourhoodResponse>("neighbourhood"),
  detail: load<KgNodeDetailResponse>("node-detail"),
  tree: load<Record<string, KgTreeChildrenResponse>>("tree"),
  search: load<KgSearchResponse>("search"),
  rules: load<KgRulesResponse>("rules-review"),
  live: load<KgLiveResponse>("learning-live"),
};

export const REPOS = {
  project_id: "northpeak-bank-b",
  repositories: [{
    repo: "paramg/northpeak-fineract-b", branch: "develop", status: "indexed",
    summary: "51,319 nodes, 234,187 edges from 6,508 files at 63dd10e on develop",
    files: 6508, parsed: 6508, graph: { nodes: 51319, edges: 234187 },
  }],
};

export const STATUS = {
  agent_access: "on", retention_years: 1, embedding_model: null, vector_index: "not_created",
  budget_category: "knowledge", ship_gate: "case_study_primary_not_run", cost_usd: null,
  sources: ["adr folder", "connector document"], repositories: REPOS.repositories,
  promotion_candidates: [{ id: "fact-1", node_key: "fact:loan|disburse|future", body: "Disbursement dates are validated against the office calendar" }],
};

/** A fact node's detail: composed on the method fixture (no fact exists on the live DB). */
export const FACT_DETAIL: KgNodeDetailResponse = {
  ...FX.detail,
  node: { id: "kgn_fixture_fact_01", key: "fact:loan|disburse|future", kind: "fact", layer: "learning", label: "Disbursement dates are validated", trust_tier: "inferred" },
  statement: "Disbursement dates are validated against the office calendar before a loan is disbursed.",
  connections: [], lineage: null,
  provenance: { created_by: "backend_engineer", created_by_kind: "agent", agent_role: "backend_engineer", agent_name: "Builder", run_id: "run_fixture_builder_1", at: "2026-10-24T10:02:17Z", events: [{ at: "2026-10-24T10:02:17Z", what: "recorded", by: "Builder" }] },
};

export type Answer = { status?: number; body: unknown } | ((url: URL) => { status?: number; body: unknown });

function treeFor(url: URL): KgTreeChildrenResponse {
  const tab = url.searchParams.get("tab") ?? "project";
  const parent = url.searchParams.get("parent");
  const q = url.searchParams.get("q");
  if (q) {
    const all = Object.entries(FX.tree).filter(([k]) => k !== "_fixture").flatMap(([, p]) => p.rows).filter((r) => (tab === "project" ? r.layer === "code" : r.layer === (tab === "rules" ? "rules" : "learning")));
    const hits = all.filter((r) => r.label.toLowerCase().includes(q.toLowerCase()));
    const keys = new Set(hits.map((h) => h.key));
    const byKey = new Map(all.map((r) => [r.key, r]));
    const rows = [...hits.map((h) => ({ ...h, match: true }))];
    for (const h of hits) {
      let p = h.parent_key;
      while (p && !keys.has(p)) { const row = byKey.get(p); if (!row) break; keys.add(p); rows.unshift({ ...row, match: false }); p = row.parent_key; }
    }
    return { tab: tab as KgTreeChildrenResponse["tab"], parent: null, q, rows, total: rows.length, next_offset: null };
  }
  if (tab === "rules" && !parent) return FX.tree.rules;
  if (tab === "learning" && !parent) return FX.tree.learning;
  if (tab === "project") {
    const page = Object.entries(FX.tree).find(([k, p]) => k !== "_fixture" && p.tab === "project" && p.parent === parent)?.[1];
    if (page) return page;
  }
  return { tab: tab as KgTreeChildrenResponse["tab"], parent, q: null, rows: [], total: 0, next_offset: null };
}

export const calls: string[] = [];

export function mockKgFetch(over: Record<string, Answer> = {}) {
  calls.length = 0;
  const routes: Array<[RegExp, Answer]> = [
    ...Object.entries(over).map(([k, v]) => [new RegExp(k), v] as [RegExp, Answer]),
    [/\/v1\/knowledge\/repositories\/index$/, { body: { repositories: REPOS.repositories, message: "Indexing started." } }],
    [/\/v1\/knowledge\/repositories$/, { body: REPOS }],
    [/\/v1\/knowledge\/graph-status$/, { body: STATUS }],
    [/\/v1\/knowledge\/view\/overview$/, { body: FX.overview }],
    [/\/v1\/knowledge\/view\/neighbourhood$/, { body: FX.hood }],
    [/\/v1\/knowledge\/view\/node\/kgn_fixture_fact_01$/, { body: FACT_DETAIL }],
    [/\/v1\/knowledge\/view\/node\//, { body: FX.detail }],
    [/\/v1\/knowledge\/view\/tree\/children$/, (u) => ({ body: treeFor(u) })],
    [/\/v1\/knowledge\/view\/search$/, (u) => ({ body: { ...FX.search, q: u.searchParams.get("q") ?? FX.search.q } })],
    [/\/v1\/knowledge\/view\/rules\/[^/]+\/(approve|edit|reject)$/, { body: { rule: FX.rules.items[1], previous: null } }],
    [/\/v1\/knowledge\/view\/rules$/, { body: FX.rules }],
    [/\/v1\/knowledge\/view\/live$/, { body: { ...FX.live, active_runs: 0 } }],
    [/\/v1\/knowledge\/nodes\//, { body: { why: [{ name: "Disbursement", body: "A loan is disbursed only once it is approved", trust_tier: "governed" }] } }],
    [/\/v1\/knowledge\/blast-radius$/, { body: { affected: [{ id: "kgn_5988f00298fe4878a253", name: "bulkLoanDisbursal" }] } }],
    [/\/v1\/knowledge\/facts\//, { body: { ok: true } }],
    [/\/api\/delivery-workflows$/, { body: { id: "wfr_1" } }],
  ];
  const fn = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(typeof input === "string" ? input : input instanceof URL ? input.href : input.url, "http://console.test");
    calls.push(`${(init?.method ?? "GET").toUpperCase()} ${url.pathname}${url.search}`);
    for (const [re, a] of routes) {
      if (!re.test(url.pathname)) continue;
      const r = typeof a === "function" ? a(url) : a;
      return new Response(JSON.stringify(r.body), { status: r.status ?? 200, headers: { "content-type": "application/json" } });
    }
    return new Response(JSON.stringify({ detail: `no fixture for ${url.pathname}` }), { status: 404 });
  });
  vi.stubGlobal("fetch", fn);
  return fn;
}
