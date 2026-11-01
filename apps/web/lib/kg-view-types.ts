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
  KgContextCard,
  KgKindMeta,
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
  KgAnchorKind, KgKindMeta, KgLearningKind, KgProductLayer, KgRuleCategory, KgRuleStatus, KgTrustTier, KgViewEdgeKind,
} from "@boss/api-client";

/** The chips, in order: the Knowledge Graph (code, rules, learning, domain) and the Context
 *  Graph — what agents did and decided (design §4.2, §4.7.3). */
export const KG_LAYERS = ["code", "rules", "learning", "domain", "context"] as const satisfies readonly KgProductLayer[];

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
  "decided", "remedied_by", "worked_on", "concerns", "cites", "precedent",
] as const satisfies readonly KgViewEdgeKind[];

/** Infra topology kinds the product shows as Code (spec §2.1). */
const CODE_TOPOLOGY = new Set(["service", "resource", "deployment", "datastore", "external", "repo"]);
const CODE_STORAGE = new Set(["config_key", "endpoint", "file", "module", "queue", "symbol", "table"]);
const LEARNING = new Set<string>([...KG_LEARNING_KINDS, ...KG_ANCHOR_KINDS]);
const DOMAIN = new Set(["regulation", "clause", "document"]);
/** The Context kinds the engine itself writes; a pack's own (a compliance case) arrive with
 *  their layer on every node, and in the overview's `kinds` (lib/kg-layout registerKinds). */
const CONTEXT = new Set(["agent_decision", "coverage_gap", "resolved_pattern"]);

/** The product layer of a kg_nodes kind (or a view-only kind: area, package). Unknown → null:
 *  the caller says so rather than guessing a chip. */
export function productLayerOf(kind: string): KgProductLayer | null {
  if (kind === "area" || kind === "package" || CODE_STORAGE.has(kind) || CODE_TOPOLOGY.has(kind)) return "code";
  if (kind === "rule") return "rules";
  if (LEARNING.has(kind)) return "learning";
  if (DOMAIN.has(kind)) return "domain";
  if (CONTEXT.has(kind)) return "context";
  const packed = kindMetaOf(kind)?.layer;
  return packed ?? null;
}

/** Anchors are drawn smaller-labelled and never counted as learned (spec §2.1). */
export function isLearnedKind(kind: string): kind is KgLearningKind {
  return (KG_LEARNING_KINDS as readonly string[]).includes(kind);
}

/**
 * THE KINDS' WORDS AND COLOURS, FROM THE ONTOLOGY PACKS (design §4.3). The overview answers with
 * every Domain and Context kind's label, plural, layer and colour (`kinds`); kgRead registers
 * them here, and the canvas, the legend, the tree and the panel name and colour a kind from this
 * — so a project's new pack (an AML case, a regulatory report) needs no console change.
 */
const KIND_META = new Map<string, KgKindMeta>();
let kindsVersion = 0;

export function registerKinds(kinds: Record<string, KgKindMeta> | null | undefined): void {
  if (!kinds || typeof kinds !== "object") return;
  for (const [kind, meta] of Object.entries(kinds)) {
    if (!meta || typeof meta !== "object") continue;
    const prev = KIND_META.get(kind);
    if (!prev || prev.color !== meta.color || prev.label !== meta.label) kindsVersion += 1;
    KIND_META.set(kind, meta);
  }
}

export function kindMetaOf(kind: string): KgKindMeta | undefined {
  return KIND_META.get(kind);
}

/** The custom property a pack kind's colour is set on (kindStyleVars), safe as a CSS name. */
export function kindVar(kind: string): string {
  return `--kg-kind-${kind.replace(/[^a-z0-9_-]/gi, "-")}`;
}

/** Every registered colour as custom properties, set on the graph so the canvas reads them like
 *  its own tokens. Changes when a pack's colours arrive (`kindsVersion`). */
export function kindStyleVars(): { vars: Record<string, string>; version: number } {
  const vars: Record<string, string> = {};
  for (const [kind, meta] of KIND_META) {
    if (meta.color && /^#[0-9a-f]{3,8}$/i.test(meta.color)) vars[kindVar(kind)] = meta.color;
  }
  return { vars, version: kindsVersion };
}

/** The view routes, relative to the control plane. */
export const KG_VIEW_BASE = "/v1/knowledge/view";
