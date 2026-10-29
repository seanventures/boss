/**
 * Graph area: the frozen contract (KNOWLEDGE_GRAPH_PRODUCT_SPEC.md §2.1, §3; Phase 0).
 *
 * The wire shapes live once, in the typed client (packages/api-client, "KNOWLEDGE GRAPH VIEW"),
 * and are re-exported here so the area imports one module. tests/fixtures/kg-*.json are real
 * northpeak-bank-b rows in these shapes (tests/kg-view-fixtures.test.ts holds them to it), so the
 * Graph area (S4) builds against them before the routes (S1, S2a, S2b) land.
 *
 * Beside the types: the closed vocabularies, and the product layer of a kind — the console's
 * reading of engine scopes.product_layer(kind), one function, one test on each side.
 */
export type {
  KgAnchorKind,
  KgConnectionGroup,
  KgLearningEvidence,
  KgLearningItem,
  KgLearningKind,
  KgLineage,
  KgLineageUse,
  KgLiveEdge,
  KgLiveResponse,
  KgNeighbourhoodResponse,
  KgNeighbourNode,
  KgNodeDetailResponse,
  KgOverviewResponse,
  KgProductLayer,
  KgProvenance,
  KgProvenanceEvent,
  KgRollupEdgeKind,
  KgRule,
  KgRuleActionRequest,
  KgRuleActionResponse,
  KgRuleCategory,
  KgRuleEvidence,
  KgRuleReview,
  KgRuleScope,
  KgRuleStatus,
  KgRulesResponse,
  KgRunComparison,
  KgRunProof,
  KgRunProofItem,
  KgRunProofResponse,
  KgSearchHit,
  KgSearchResponse,
  KgSymbolKind,
  KgTreeChildrenResponse,
  KgTreeRow,
  KgTreeTab,
  KgTrustTier,
  KgVectorInfo,
  KgViewEdge,
  KgViewEdgeKind,
  KgViewLevel,
  KgViewNode,
  ProjectArchiveRequest,
  ProjectArchiveResponse,
} from "@boss/api-client";

import type {
  KgAnchorKind, KgLearningKind, KgProductLayer, KgRuleCategory, KgRuleStatus, KgTrustTier, KgViewEdgeKind,
} from "@boss/api-client";

/** The chips, in order. `domain` is reserved (spec §1 non-goals) and never offered. */
export const KG_LAYERS = ["code", "rules", "learning"] as const satisfies readonly KgProductLayer[];

export const KG_RULE_STATUSES = ["proposed", "approved", "rejected", "superseded"] as const satisfies readonly KgRuleStatus[];

export const KG_RULE_CATEGORIES = [
  "validation", "calculation", "limit", "state_transition", "authorization", "other",
] as const satisfies readonly KgRuleCategory[];

export const KG_LEARNING_KINDS = [
  "investigation", "test_failure", "fix", "workaround", "review_finding",
  "decision", "root_cause", "precedent", "fact",
] as const satisfies readonly KgLearningKind[];

export const KG_ANCHOR_KINDS = [
  "run", "incident", "pull_request", "release", "spec", "runbook", "alert_rule", "topic",
] as const satisfies readonly KgAnchorKind[];

export const KG_TRUST_TIERS = ["observed", "governed", "inferred", "approved"] as const satisfies readonly KgTrustTier[];

export const KG_VIEW_EDGE_KINDS = [
  "calls", "imports", "extends",
  "dispatches_to", "overrides", "implements", "references", "defines", "exposes", "reads", "writes",
  "about", "changed", "evidenced_by", "approved_by", "supersedes", "caused_by", "fixed_by",
  "consulted", "reused", "proposed_by", "produced", "ruled_out", "corrects", "governs",
] as const satisfies readonly KgViewEdgeKind[];

/** Infra topology kinds the product shows as Code (spec §2.1). */
const CODE_TOPOLOGY = new Set(["service", "resource", "deployment", "datastore", "external", "repo"]);
const CODE_STORAGE = new Set(["config_key", "endpoint", "file", "module", "queue", "symbol", "table"]);
const LEARNING = new Set<string>([...KG_LEARNING_KINDS, ...KG_ANCHOR_KINDS]);
const DOMAIN = new Set(["regulation", "clause", "document"]);

/** The product layer of a kg_nodes kind (or a view-only kind: area, package). Unknown → null:
 *  the caller says so rather than guessing a chip. */
export function productLayerOf(kind: string): KgProductLayer | null {
  if (kind === "area" || kind === "package" || CODE_STORAGE.has(kind) || CODE_TOPOLOGY.has(kind)) return "code";
  if (kind === "rule") return "rules";
  if (LEARNING.has(kind)) return "learning";
  if (DOMAIN.has(kind)) return "domain";
  return null;
}

/** Anchors are drawn smaller-labelled and never counted as learned (spec §2.1). */
export function isLearnedKind(kind: string): kind is KgLearningKind {
  return (KG_LEARNING_KINDS as readonly string[]).includes(kind);
}

/** The view routes, relative to the control plane. */
export const KG_VIEW_BASE = "/v1/knowledge/view";
