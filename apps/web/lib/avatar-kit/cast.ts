/**
 * The BOSS crew: every shipped role's face, hand-designed.
 *
 * `hand` characters (the §0.1 cast and the four "thinks like" archetypes) each own a hat no one
 * else wears. `picked` roles (the rest of the shipped pack) are assembled by hand from the
 * generic parts, so they are still fixed and distinct — no shipped role gets a seeded face.
 * Custom agents an operator creates are the only procedural faces (see agent-avatar.ts).
 *
 * Callsigns are invented words, never a real person's name. The Passport and every prompt get
 * their callsign from the role's persona once S-identity ships it; this table is the fallback
 * and the source for the README cast, so keep the two in step when a persona changes.
 */
import type { AvatarSpec } from "../agent-world-types";
import { dict } from "./dict";

export type CastMember = {
  roleKey: string;
  name: string;
  callsign?: string;
  tagline?: string;
  /** A "thinks like" archetype: inspired by a way of thinking, never a person. */
  archetype?: boolean;
  designed: "hand" | "picked";
  avatar: AvatarSpec;
};

const face = (roleKey: string, hat: string, emblem: string, palette: string, visor: string): AvatarSpec =>
  ({ kit: "boss-v1", seed: roleKey, hat, emblem, palette, visor });

const hand = (roleKey: string, name: string, callsign: string, tagline: string,
              parts: [string, string, string, string], archetype = false): CastMember =>
  ({ roleKey, name, callsign, tagline, designed: "hand", avatar: face(roleKey, ...parts), ...(archetype ? { archetype } : {}) });

const picked = (roleKey: string, name: string, parts: [string, string, string, string]): CastMember =>
  ({ roleKey, name, designed: "picked", avatar: face(roleKey, ...parts) });

export const CAST: readonly CastMember[] = [
  // The §0.1 cast, in the order the operator named it.            hat           emblem       palette   visor
  hand("engineering_captain", "Engineering Captain", "Helm", "Who owns it, and what ships next?", ["captain", "chevrons", "blue", "band"]),
  hand("backend_engineer", "Backend Engineer", "Strata", "Data first, then the endpoint.", ["rack", "layers", "purple", "lens"]),
  hand("frontend_engineer", "Frontend Engineer", "Prism", "If it janks, it isn't done.", ["pixel", "brackets", "coral", "band"]),
  hand("operations_engineer", "Operations Engineer", "Relay", "Every deploy has a way back.", ["headlamp", "wrench", "amber", "slit"]),
  hand("sre", "Site Reliability Engineer", "Beacon", "What's the SLO, and are we in it?", ["siren", "pulse", "mint", "scope"]),
  hand("pm", "Product Manager", "Juno", "What problem, for whom, by when?", ["northstar", "compass", "teal", "band"]),
  hand("chief_architect", "Chief Architect", "Atlas", "Name the trade-off before the choice.", ["hardhat", "blueprint", "blue", "grid"]),
  hand("software_engineer", "Software Engineer", "Forge", "Smallest diff that passes.", ["headphones", "braces", "blue", "band"]),
  hand("adversarial_reviewer", "Adversarial Reviewer", "Rook", "I'll find what you missed.", ["rook", "target", "amber", "monocle"]),
  hand("security_reviewer", "Security Reviewer", "Sentry", "Who else can reach this?", ["crest", "lock", "slate", "shield"]),
  hand("qa", "QA Lead", "Vera", "Show me the test.", ["magnifier", "check", "green", "lens"]),
  hand("release_manager", "Release Manager", "Pilot", "Ready when the checklist is green.", ["flag", "rocket", "green", "slit"]),
  hand("design_critic", "Design Critic", "Muse", "Would a first-time user get this?", ["beret", "nib", "pink", "band"]),
  hand("docs_writer", "Documentation Writer", "Quill", "If it's not written, it didn't ship.", ["quill", "page", "slate", "glasses"]),
  hand("finops_sentinel", "FinOps Sentinel", "Ledger", "What does this cost per run?", ["coins", "dollar", "mint", "grid"]),
  // Archetypes: optional "thinks like" agents.
  hand("first_principles_researcher", "The First-Principles Researcher", "Axiom", "What must be true?", ["goggles", "atom", "purple", "lens"], true),
  hand("visionary_founder", "The Visionary Founder", "Nova", "What does this look like at 100×?", ["comet", "spark", "teal", "scope"], true),
  hand("relentless_operator", "The Relentless Operator", "Tempo", "What ships today?", ["headset", "stopwatch", "coral", "slit"], true),
  hand("teacher_who_simplifies", "The Teacher Who Simplifies", "Sage", "Let me make that simple.", ["mortarboard", "bulb", "pink", "glasses"], true),
  // The rest of the shipped pack: fixed faces assembled from the generic parts.
  picked("metatron", "Metatron", ["halo", "star", "teal", "scope"]),
  picked("cto", "CTO", ["crown", "chip", "teal", "band"]),
  picked("coo", "COO", ["crown", "gear", "amber", "band"]),
  picked("cao", "Chief Architecture Officer", ["crown", "blueprint", "slate", "grid"]),
  picked("vp_agents", "VP Agents", ["crown", "people", "pink", "glasses"]),
  picked("program_manager", "Program Manager", ["cap", "people", "teal", "band"]),
  picked("deployment_manager", "Deployment Manager", ["cap", "rocket", "green", "slit"]),
  picked("infra_steward", "Infra Steward", ["dome", "cloud", "slate", "band"]),
  picked("integration_validator", "Integration Validator", ["twin", "check", "mint", "lens"]),
  picked("media_publisher", "Media Publisher", ["antenna", "play", "pink", "band"]),
  picked("video_editor", "Video Editor", ["clapper", "play", "coral", "lens"]),
  picked("model_spend_analyst", "Model Spend Analyst", ["fin", "chart", "mint", "grid"]),
  picked("providerops_manager", "ProviderOps Manager", ["twin", "cycle", "purple", "slit"]),
  picked("resource_janitor", "Resource Janitor", ["propeller", "cycle", "green", "band"]),
  picked("secrets_steward", "Secrets Steward", ["dome", "key", "amber", "shield"]),
  picked("archivist", "Archivist", ["dome", "page", "slate", "glasses"]),
  // The knowledge-graph case studies' roles (incident response, banking-regulatory, banking-aml).
  picked("incident_commander", "Incident Commander", ["siren", "flag", "coral", "slit"]),
  picked("compliance_officer", "Compliance Officer", ["crest", "check", "slate", "glasses"]),
  picked("compliance_analyst", "Compliance Analyst", ["magnifier", "page", "blue", "lens"]),
  picked("pipeline_orchestrator", "Pipeline Orchestrator", ["antenna", "flag", "blue", "band"]),
  picked("lane_agent", "Lane Agent", ["hardhat", "braces", "amber", "band"]),
  picked("remediation_advisor", "Remediation Advisor", ["headlamp", "wrench", "green", "band"]),
  picked("aml_network_mapper", "Network Mapper", ["antenna", "people", "teal", "scope"]),
  picked("aml_analyst", "AML Analyst", ["magnifier", "target", "purple", "lens"]),
  picked("sar_narrator", "SAR Narrator", ["quill", "page", "coral", "glasses"]),
  // The banking-dq pack's DQ RCA and remediation pipeline (Instance A of the investigation pipeline).
  picked("dq_monitoring_agent", "Incident Intake Agent", ["siren", "eye", "amber", "band"]),
  picked("dq_context_enrichment_agent", "Context Gathering Agent", ["antenna", "layers", "teal", "lens"]),
  picked("dq_historical_pattern_agent", "Past Incidents Agent", ["mortarboard", "cycle", "slate", "glasses"]),
  picked("dq_rca_agent", "Root Cause Analysis Agent", ["magnifier", "atom", "blue", "scope"]),
  picked("dq_pipeline_orchestrator", "Flow Orchestrator", ["headset", "compass", "purple", "band"]),
  picked("dq_lane_agent", "Swimlane Agent", ["goggles", "check", "mint", "lens"]),
  picked("dq_rule_drafting_agent", "Rule Proposal Agent", ["beret", "nib", "pink", "glasses"]),
  picked("dq_remediation_advisor", "Fix Planner", ["hardhat", "wrench", "green", "slit"]),
  picked("dq_incident_closure_agent", "Close and Learn Agent", ["crest", "check", "teal", "monocle"]),
  picked("dq_escalation_agent", "Escalation Watch", ["flag", "stopwatch", "coral", "slit"]),
  picked("rulebook_analyst", "Rulebook Analyst", ["dome", "page", "teal", "lens"]),
];

export const CAST_BY_KEY: Readonly<Record<string, CastMember>> = dict(CAST.map((c) => [c.roleKey, c]));

/**
 * Panel seats that share a character. Growth and Data resolve to the PM, Customer Advocate to the
 * Design Critic, Privacy & Safety to the Security Reviewer: right for persona and lineage, but a
 * meeting would seat three identical Junos. A seat look keeps the role's hat and palette (whose
 * crew it is) and gives the seat its own emblem and visor. Keyed by every name a panel uses.
 */
type SeatLook = Pick<AvatarSpec, "emblem" | "visor">;
const SEATS: readonly [readonly string[], SeatLook][] = [
  // The visor is the part that still reads at 24px (the emblem drops out below 32px), so each is
  // one whose glass differs from its agent's at the smallest size: band vs slit vs lens vs shield.
  [["growth", "growth / marketing"], { emblem: "bolt", visor: "slit" }],
  [["data", "data / analytics"], { emblem: "chart", visor: "lens" }],
  [["customer_advocate", "customer advocate"], { emblem: "people", visor: "slit" }],
  [["privacy_safety", "privacy & safety"], { emblem: "key", visor: "lens" }],
];
export const SEAT_LOOKS: Readonly<Record<string, SeatLook>> = dict(SEATS.flatMap(([names, look]) => names.map((n) => [n, look] as const)));
