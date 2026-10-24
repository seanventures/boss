/**
 * THE PARITY TABLE FOR THE MOVED KNOWLEDGE SCREENS (KNOWLEDGE_GRAPH_PRODUCT_SPEC.md §5.1, rule
 * "a redesign keeps every capability"). Reliability's knowledge explorer and settings
 * (app/knowledge-console.tsx) and Approvals' fact promotion queue (app/knowledge-promotion-ui.tsx)
 * moved into the Graph area. Each row is one capability the old screens had, where it was, where it
 * is now, and how tests/kg-parity.test.tsx proves it renders: open the Graph area on `tab`, press
 * `press` in order, and `testid` is on screen.
 */
export type ParityRow = {
  id: string;
  capability: string;
  was: string;
  now: string;
  check: { tab: "graph" | "rules" | "settings"; press?: string[]; testid: string };
};

export const KG_PARITY: readonly ParityRow[] = [
  {
    id: "search", capability: "Search what the project knows",
    was: "Reliability › Knowledge explorer › Search (symbol, fact or decision)",
    now: "Graph › toolbar search (⌘K): name-ranked hits, Enter focuses the best match; Search by meaning when an embedding model is set",
    check: { tab: "graph", testid: "kg-search-input" },
  },
  {
    id: "search-hits", capability: "A search hit opens the node",
    was: "Reliability › Knowledge explorer › hit button (opens Why)",
    now: "Graph › search dropdown › hit → Focus + detail panel + tree opened to it",
    check: { tab: "graph", press: ["@search:disburse"], testid: "kg-search-hit" },
  },
  {
    id: "why", capability: "Why (the facts that explain a node)",
    was: "Reliability › Knowledge explorer › Why",
    now: "Graph › detail panel › Why and blast radius › Why",
    check: { tab: "graph", press: ["@node:kgn_ce1dc84503de413d8ebc"], testid: "kg-why" },
  },
  {
    id: "blast", capability: "Blast radius",
    was: "Reliability › Knowledge explorer › Show blast radius",
    now: "Graph › detail panel › Why and blast radius › Blast radius (each affected node is clickable)",
    check: { tab: "graph", press: ["@node:kgn_ce1dc84503de413d8ebc"], testid: "kg-blast" },
  },
  {
    id: "index-now", capability: "Index now / Re-index a repository",
    was: "Reliability › Knowledge settings › Code › Index / Re-index",
    now: "Graph › Settings › Code › Index / Re-index, and Graph › toolbar › Rebuild",
    check: { tab: "settings", testid: "kg-index-now" },
  },
  {
    id: "rebuild", capability: "Rebuild the graph from the toolbar",
    was: "(none: only Re-index in settings)",
    now: "Graph › toolbar › Built … · Rebuild",
    check: { tab: "graph", testid: "kg-rebuild" },
  },
  {
    id: "repo-status", capability: "Each repository's code graph status",
    was: "Reliability › Knowledge settings › Code (summary line per repository)",
    now: "Graph › Settings › Code, and the Graph tab's indexing state while it indexes",
    check: { tab: "settings", testid: "knowledge-code-repo" },
  },
  {
    id: "agent-access", capability: "What agents get (the knowledge_graph setting)",
    was: "Reliability › Knowledge settings › Agents get",
    now: "Graph › Settings › Agents get",
    check: { tab: "settings", testid: "knowledge-agent-access" },
  },
  {
    id: "embedding", capability: "Embedding model, its check, and why meaning search is off",
    was: "Reliability › Knowledge settings › Embedding",
    now: "Graph › Settings › Embedding (link to Setup › Repo and models › Embedding model)",
    check: { tab: "settings", testid: "knowledge-embedding" },
  },
  {
    id: "retention", capability: "Retention",
    was: "Reliability › Knowledge settings › Retention",
    now: "Graph › Settings › Retention",
    check: { tab: "settings", testid: "knowledge-retention" },
  },
  {
    id: "settings-rest", capability: "Budget category, sources, case study, cost",
    was: "Reliability › Knowledge settings",
    now: "Graph › Settings",
    check: { tab: "settings", testid: "kg-settings" },
  },
  {
    id: "promote", capability: "Promote (approve) an inferred fact",
    was: "Approvals › Knowledge facts waiting for a person › Approve fact",
    now: "Graph › Rules review › Facts › Approve fact",
    check: { tab: "rules", press: ["kg-review-sub-facts"], testid: "kg-fact-promote" },
  },
  {
    id: "reject", capability: "Reject an inferred fact",
    was: "Approvals › Knowledge facts waiting for a person › Reject fact",
    now: "Graph › Rules review › Facts › Reject fact",
    check: { tab: "rules", press: ["kg-review-sub-facts"], testid: "kg-fact-reject" },
  },
  {
    id: "forget", capability: "Forget a fact",
    was: "(API only: POST /v1/knowledge/facts/{id}/forget)",
    now: "Graph › detail panel of a fact › Actions › Forget (confirm)",
    check: { tab: "graph", press: ["@node:kgn_fixture_fact_01"], testid: "kg-panel-forget" },
  },
];
