/**
 * The NorthPeak regulatory pipeline as the API serves it (workflow `northpeak_regulatory_
 * compliance_mine` v4, base `banking_regulatory_compliance`): its spec, the palette entries its
 * steps bind, the role names the capabilities view returns, and a last run's context trace in
 * `view=context_trace`'s shape (a run waiting at the Compliance Officer's approval).
 */
import type { Palette, Spec } from "../../lib/workflow-editor-types";
import type { RunContext } from "../../lib/workflow-api";

const cap = (key: string, label: string, summary: string, extra: Record<string, unknown> = {}) => ({
  key, label, summary, risk_tier: 1, required_inputs: [], optional_inputs: [], produces: [], skills: [],
  ...extra,
});

export const REG_PALETTE: Palette = {
  capabilities: [
    cap("clause_intake", "Clause intake", "Open one ComplianceCase per changed clause of a pasted regulation document.",
        { captain: "security_reviewer" }),
    cap("compliance_assessment", "Compliance assessment",
        "Classify the subject's assembled evidence implemented/partly/gap, citing a graph node.",
        { captain: "security_reviewer", role_agent: "compliance_analyst", needs_model: true }),
    cap("compliance_orchestration", "Compliance orchestration",
        "Route the run's finding to its remediation lane by family.",
        { captain: "program_manager", role_agent: "pipeline_orchestrator" }),
    cap("lane_confirmation", "Lane confirmation", "Confirm or rule out the family's finding by reading the cited code.",
        { captain: "chief_architect", role_agent: "lane_agent", needs_model: true }),
    cap("remediation_package", "Remediation package", "Propose the change brief, test, owner and approver.",
        { captain: "software_engineer", role_agent: "remediation_advisor" }),
    cap("compliance_closure", "Compliance closure", "Record this investigation's resolution as a Precedent.",
        { captain: "chief_architect" }),
  ],
  customSteps: [], blocks: [], agents: [], humanRole: "human", riskFloor: 2,
  roleNames: {
    compliance_analyst: { name: "Compliance Analyst", callsign: "Tally" },
    lane_agent: { name: "Lane Agent", callsign: "Plumb" },
    remediation_advisor: { name: "Remediation Advisor", callsign: "Mend" },
    compliance_officer: { name: "Compliance Officer", callsign: "Writ" },
  },
  graphSteps: {
    context_enrichment: { label: "Context enrichment",
      summary: "Collects the rules, code, systems, owners and reports connected to the case within a hop cap." },
    historical_pattern: { label: "Historical pattern",
      summary: "Finds prior incidents and precedents that share the case's links, and asks the ITSM system for matching tickets." },
  },
  bindable: { skills: [], mcp_servers: [] },
};

export const OUTSIDE = { id: "ext_e9286d5947b94127", display_name: "NorthPeak review agent",
                         endpoint_host: "review.northpeak.example" };

const capStep = (id: string, next: string[] = []) => ({ id, kind: "capability", next, binding: { capability: id } });

export const REG_SPEC: Spec = {
  steps: [
    capStep("clause_intake", ["evidence_loop"]),
    { id: "context_enrichment", kind: "deterministic", next: [],
      contract: {
        trigger: "after intake opens a new case; never on attach",
        reads: [{ kind: "clause" }, { kind: "rule" }, { kind: "symbol" }, { kind: "RegulatoryReport", via: "REPORTED_IN" }],
        writes: [{ kind: "agent_decision", required: true, decision_type: "ContextAssembled" }, { kind: "coverage_gap" }],
        policy: { max_hops: 3, list_cap: 50 },
        llm: "none",
        decisions: ["A-D30 new incidents only", "A-D32 no LLM for retrieval"],
      } },
    { id: "evidence_loop", kind: "bounded_loop", body: "context_enrichment", max_iterations: 2,
      expression: { path: "steps.context_enrichment.output.missing_kinds", op: "truthy" },
      next: ["historical_pattern"] },
    { id: "historical_pattern", kind: "deterministic", next: ["compliance_assessment"],
      contract: { calls: [{ mcp: "itsm", tool: "get_incident" }, { mcp: "itsm", tool: "list_incidents" }] } },
    capStep("compliance_assessment", ["compliance_orchestration"]),
    capStep("compliance_orchestration", ["lanes"]),
    { id: "lanes", kind: "for_each", body: "lane_confirmation", max_items: 25, concurrency: 3,
      items: "steps.compliance_orchestration.output.finding_families", next: ["remediation_package"] },
    capStep("lane_confirmation"),
    capStep("remediation_package", ["bank_review"]),
    { id: "bank_review", kind: "external_agent", title: "Bank's own review", next: ["human_approval"],
      captain: { external_agent: OUTSIDE.id },
      contract: { reads: [{ kind: "clause" }, { kind: "agent_decision" }, { kind: "remediation_package" }],
                  writes: [{ kind: "agent_decision", required: true, decision_type: "bank_review" }] } },
    { id: "human_approval", kind: "decision", next: ["compliance_closure"],
      question: "Approve the remediation package and hand it to NorthPeak's engineering team?",
      approval: { mode: "manual", approver_role: "compliance_officer" } },
    capStep("compliance_closure"),
  ],
};

const kinds = (...rows: [string, number, string[]?, string[]?][]) =>
  rows.map(([kind, count, examples = [], decision_types = []]) => ({ kind, count, examples, decision_types }));

export const REG_RUN: RunContext = {
  run_id: "wfr_81e5454e260c41f6bb50", status: "waiting",
  started_at: "2026-11-01T04:08:32Z", completed_at: null,
  steps: {
    clause_intake: { status: "succeeded", reads: [], calls: [],
                     writes: kinds(["agent_decision", 1, [], ["ClauseIntake"]], ["compliance_case", 2]) },
    context_enrichment: { status: "succeeded", calls: [],
      reads: kinds(["rule", 1, ["Repayment-like transaction classification"]], ["symbol", 11, ["LoanTransactionType.isRepaymentType"]],
                   ["incident", 1, ["INC-2088"]]),
      writes: kinds(["agent_decision", 1, [], ["ContextAssembled"]]) },
    evidence_loop: { status: "succeeded", reads: [], writes: [], calls: [] },
    historical_pattern: { status: "succeeded", calls: [], reads: kinds(["incident", 1, ["INC-2088"]]),
                          writes: kinds(["agent_decision", 1, [], ["HistoricalPatternMatched"]]) },
    compliance_assessment: { status: "succeeded", calls: [], reads: kinds(["rule", 1]),
                             writes: kinds(["agent_decision", 1, [], ["Assessment"]]) },
    compliance_orchestration: { status: "succeeded", reads: [], writes: [], calls: [] },
    lanes: { status: "succeeded", reads: [], writes: [], calls: [] },
    lane_confirmation: { status: "succeeded", calls: [], reads: [],
                         writes: kinds(["agent_decision", 1, [], ["Confirmed"]]) },
    remediation_package: { status: "succeeded", reads: [], calls: [], writes: kinds(["remediation_package", 1]) },
    bank_review: { status: "succeeded", calls: [{ tool: "review.northpeak.example", count: 1 }],
                   reads: kinds(["clause", 2], ["agent_decision", 58], ["remediation_package", 11]),
                   writes: kinds(["agent_decision", 1, [], ["bank_review"]]) },
    human_approval: { status: "waiting", reads: [], writes: [], calls: [] },
    compliance_closure: { status: "pending", reads: [], writes: [], calls: [] },
  },
};
