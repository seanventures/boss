/**
 * A DQ root-cause pipeline in the reference design (screens "RCA and remediation pipeline",
 * "1. Incident Intake Agent"): its spec with numbered agent steps and design cards on the contracts,
 * a shared service and a rule-authoring side path, the designs view's answer (statuses, graph
 * versions, kind colours), and a last run for Monitoring.
 */
import type { Palette, Spec } from "../../lib/workflow-editor-types";
import type { RunContext } from "../../lib/workflow-api";
import { designsFrom } from "../../lib/pipeline-model";

export const DQ_PALETTE: Palette = {
  capabilities: [], customSteps: [], blocks: [], agents: [], humanRole: "human", riskFloor: 2,
  roleNames: { monitoring_agent: { name: "Incident Intake Agent", callsign: "" } },
  graphSteps: {}, bindable: { skills: [], mcp_servers: [] },
};

const MONITORING_STEPS = [
  { n: 1, name: "Read the group", reads: ["agent", "mcp_server", "system"], calls: [{ mcp: "dq-monitor", tool: "get_exception_group" }],
    explain: "Reads the exception group the DQ Monitor graded Error: one rule version, one COB." },
  { n: 2, name: "Resolve the rule", reads: ["data_quality_rule", "database_structure", "database_table"] },
  { n: 3, name: "Grade severity", reads: ["dq_threshold", "cde"] },
  { n: 4, name: "Set failure reason", reads: ["data_quality_rule"] },
  { n: 5, name: "Tag the exception", reads: ["data_quality_rule"] },
  { n: 6, name: "Match", reads: ["incident"] },
  { n: 7, name: "Open or attach", reads: ["data_quality_rule", "cde"], writes: ["incident", "agent_decision"],
    calls: [{ mcp: "service-desk", tool: "create_ticket" }] },
  { n: 8, name: "Link facts", writes: ["agent_decision"] },
  { n: 9, name: "Write trace, hand off", reads: ["agent"], writes: ["agent_decision"] },
];

export const DQ_SPEC: Spec = {
  entry_step: "monitoring",
  steps: [
    { id: "monitoring", kind: "external_agent", title: "Incident Intake Agent", next: ["context_enrichment"],
      description: "Opens or attaches an incident for each exception group the DQ Monitor grades Error, and sets its severity",
      contract: {
        trigger: "The DQ Monitor publishes an exception group (one rule version, one COB).",
        reads: ["agent", "mcp_server", "system", "data_quality_rule", "dq_threshold", "cde", "database_structure",
                "database_table", "incident"].map((kind) => ({ kind })),
        writes: [{ kind: "incident", required: true }, { kind: "agent_decision", required: true, decision_type: "IncidentOpened" }],
        calls: [{ mcp: "dq-monitor", tool: "get_exception_group" }, { mcp: "service-desk", tool: "create_ticket" }],
        handoff: { to: "context_enrichment", carries: ["incident_id", "severity"] },
        human_review: "None in the normal path.",
        steps: MONITORING_STEPS,
        design: {
          status: "locked", reference: "Design v0.1",
          under_revision: "Grading moved onto each rule.",
          decisions: [{ id: "MON-1", text: "severity only moves up" }, { id: "MON-2", text: "link facts only" }],
          policy: [{ id: "MON-P1", text: "severity ladder" }],
          applied_in: { kg: "v3", cg: "v3", items: ["gradedSeverity on the decision", "linkType on CORRELATED_WITH"] },
          open_questions: [{ id: "MON-Q1", text: "what the DQ Monitor's exception-group payload carries" }],
        },
      } },
    { id: "context_enrichment", kind: "external_agent", title: "Context Gathering Agent", next: ["rca"],
      contract: {
        reads: [{ kind: "cde" }, { kind: "regulatory_report" }],
        writes: [{ kind: "agent_decision", required: true, decision_type: "ContextAssembled" }],
        steps: [{ n: 1, name: "Load the incident", reads: ["cde"] },
                { n: 2, name: "Assemble, hand off", reads: ["regulatory_report"], writes: ["agent_decision"] }],
        design: { status: "in_review", reference: "Design v0.2" },
      } },
    { id: "rca", kind: "external_agent", title: "Root Cause Analysis Agent", next: ["human_approval"],
      contract: { reads: [{ kind: "cde" }], writes: [{ kind: "agent_decision" }] } },
    { id: "human_approval", kind: "decision", next: [], question: "Approve the prepared fix?",
      approval: { mode: "manual", approver_role: "human" } },
    { id: "context_curator", kind: "external_agent", title: "Context Budgeter", next: [],
      description: "Ranks and trims context for every LLM step",
      contract: { reads: [{ kind: "agent_decision" }],
                  design: { status: "shared_service", reference: "Chapter to write" } } },
    { id: "rule_drafting", kind: "external_agent", title: "Rule Proposal Agent", next: [], side_path: "Rule authoring",
      description: "Opens a rule authoring case",
      contract: { reads: [{ kind: "data_quality_rule" }], writes: [{ kind: "agent_decision" }] } },
  ],
} as unknown as Spec;

export const DQ_DESIGNS = designsFrom({
  stages: {
    monitoring: { status: "locked", reference: "Design v0.1", under_revision: "Rule authoring changed DQ grading.",
                  source: "contract", locked: { reference: "Design v0.1", status: "locked" },
                  can: { send_for_review: false, lock: false, revise: true } },
    context_enrichment: { status: "in_review", reference: "Design v0.2", source: "review",
                          can: { send_for_review: false, lock: true, revise: false } },
    rca: { status: "not_reviewed", reference: "", source: "default",
           can: { send_for_review: true, lock: false, revise: false } },
    human_approval: { status: "human_step", reference: "", source: "default",
                      can: { send_for_review: false, lock: false, revise: false } },
    context_curator: { status: "shared_service", reference: "Chapter to write", source: "contract",
                       can: { send_for_review: false, lock: false, revise: false } },
  },
  graph: { kg_version: 3, cg_version: 3 },
  kinds: {
    agent: { label: "Agent", color: "#1f8a99", layer: "lifecycle" },
    mcp_server: { label: "MCPServer", color: "#1f8a99", layer: "knowledge" },
    system: { label: "System", color: "#c27c0e", layer: "code" },
    data_quality_rule: { label: "DataQualityRule", color: "#c0392b", layer: "knowledge" },
    dq_threshold: { label: "DQThreshold", color: "#c0392b", layer: "knowledge" },
    cde: { label: "CDE", color: "#17866b", layer: "knowledge" },
    database_structure: { label: "DatabaseStructure", color: "#c27c0e", layer: "knowledge" },
    database_table: { label: "DatabaseTable", color: "#c27c0e", layer: "knowledge" },
    incident: { label: "Incident", color: null, layer: "ops" },
    agent_decision: { label: "AgentDecision", color: "#38bdf8", layer: "context" },
    regulatory_report: { label: "RegulatoryReport", color: "#2f6db3", layer: "knowledge" },
  },
});

const kinds = (...rows: [string, number, string[]?][]) =>
  rows.map(([kind, count, examples = []]) => ({ kind, count, examples, decision_types: [] as string[] }));

export const DQ_RUN: RunContext = {
  run_id: "wfr_dq0000000000000001", status: "waiting", started_at: "2026-10-05T18:00:00Z", completed_at: null,
  steps: {
    monitoring: { status: "succeeded", callsRecorded: true, calls: [{ tool: "create_ticket", server: "service-desk", count: 1 }],
                  reads: kinds(["data_quality_rule", 1, ["DQX-0588 v2"]], ["cde", 2]),
                  writes: kinds(["incident", 1, ["INC-501"]], ["agent_decision", 2]) },
    context_enrichment: { status: "succeeded", calls: [], reads: [], writes: [] },
    rca: { status: "running", calls: [], reads: [], writes: [] },
  },
};
