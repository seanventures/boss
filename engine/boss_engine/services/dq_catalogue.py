"""The consultant's DQ RCA and remediation pipeline as catalogue entries (Instance A of
`kg_investigation_pipeline`, design §5.2), with every stage's step contract (§4.1) and its agent
DESIGN — numbered internal steps and the design cards — in the consultant's own words where the
screens show them (Monitoring, Context Enrichment, Historical Pattern are locked at Agent
Reference v0.1/v0.2/v0.3; RCA is in review; the rest not yet reviewed; the people are human
steps; the Context Curator and Profile Service are shared services).

`dq_incident_rca`        one Eagle exception group, Monitoring → Closure
`dq_incident_escalation` the Escalation Agent's daily sweep over every open incident
`dq_eagle_intake`        the scheduled trigger: each COB, an RCA per Error exception group

`DQ_CONTRACTS` holds each stage's contract; `DQ_STAGE_DESIGNS` its `steps` and `design`, merged in
when the contract schema carries them (`step_contracts.SUPPORTS_STEPS_AND_DESIGN`).
"""
from __future__ import annotations

from typing import Any, Callable

from .knowledge import step_contracts

PACK = "banking-dq"


def _reads(*kinds: str) -> list[dict[str, str]]:
    return [{"kind": k} for k in kinds]


def _w(kind: str, required: bool = False, decision_type: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {"kind": kind, "required": required}
    if decision_type:
        out["decision_type"] = decision_type
    return out


def _c(mcp: str, tool: str) -> dict[str, str]:
    return {"mcp": mcp, "tool": tool}


def _n(note_id: str, text: str) -> dict[str, str]:
    return {"id": note_id, "text": text}


def _s(n: int, name: str, explain: str, reads: tuple = (), writes: tuple = (),
       calls: tuple = ()) -> dict[str, Any]:
    out: dict[str, Any] = {"n": n, "name": name, "explain": explain}
    if reads:
        out["reads"] = list(reads)
    if writes:
        out["writes"] = list(writes)
    if calls:
        out["calls"] = [_c(*c) for c in calls]
    return out


# ── Step contracts (reads / writes / calls / trigger / handoff / human review / policy / llm) ──

DQ_CONTRACTS: dict[str, dict[str, Any]] = {
    "monitoring": {
        "trigger": "Eagle publishes an exception group (one rule version, one COB). The Pipeline "
                   "Orchestrator receives it, starts the DQ pipeline and invokes Monitoring with "
                   "the group ID and the function.",
        "reads": _reads("agent", "mcp_server", "system", "exception_group", "data_quality_rule",
                        "dq_threshold", "cde", "database_structure", "database_table",
                        "incident"),
        "writes": [_w("incident"), _w("agent_decision", False, "IncidentOpened|IncidentAttached"),
                   _w("coverage_gap"), _w("agent_run"), _w("agent_tool_call")],
        "links": [{"from": "incident", "to": "incident", "rel": "CORRELATED_WITH"}],
        "invoked_by": "pipeline_orchestrator",
        "calls": [_c("eagle", "get_exception_group"), _c("itsm", "list_incidents"),
                  _c("itsm", "create_incident"), _c("itsm", "attach_to_incident")],
        "handoff": {"to": "pipeline_orchestrator", "next": "context_enrichment",
                    "carries": ["incident_id", "severity"],
                    "text": "Returns the incident ID, opened or attached, and the severity to the "
                            "Pipeline Orchestrator. A new incident goes to Context Enrichment; an "
                            "attach does not restart the pipeline."},
        "human_review": "None in the normal path. A policy parameter missing from both the "
                        "function policy and the defaults stops the run and goes to human review.",
        "policy": {"opens_on": "Error", "severity": "only rises"},
        "llm": "none",
        "decisions": ["severity only moves up", "link facts only", "one failure reason per DQ "
                      "rule", "ticket to the rule owner", "a Warning writes nothing"],
    },
    "context_enrichment": {
        "trigger": "Invoked by the Pipeline Orchestrator after Monitoring opens a new incident — "
                   "never on an attach. Re-runs on demand, or when the Pipeline Orchestrator "
                   "re-invokes it after RCA records InsufficientEvidence; a re-run writes a new "
                   "decision and keeps the old one.",
        "reads": _reads("agent", "mcp_server", "system", "incident", "agent_decision",
                        "data_quality_rule", "cde", "database_structure", "database_table",
                        "database_column", "feed", "lineage", "owner", "regulatory_report",
                        "report_line", "mdrm", "issue", "examination_finding"),
        "writes": [_w("agent_decision", True, "ContextAssembled"), _w("coverage_gap"),
                   _w("agent_run"), _w("agent_tool_call")],
        "calls": [_c("olympus", "get_exception_profile"), _c("olympus", "list_recent_changes"),
                  _c("itsm", "get_incident")],
        "invoked_by": "pipeline_orchestrator",
        "handoff": {"to": "pipeline_orchestrator", "next": "historical_pattern",
                    "carries": ["decision_id", "context_pack"],
                    "text": "Returns the decision ID to the Pipeline Orchestrator, which invokes "
                            "Historical Pattern. RCA reads the pack through the Context Curator."},
        "human_review": "None. Missing metadata — lineage, CDE mapping, owner, MDRM route, "
                        "profile — is recorded as a coverage gap and the run continues.",
        "policy": {"max_hops": 3, "change_lookback_days": 14, "list_cap": 25,
                   "data_access": "standard_profile"},
        "llm": "none",
        "decisions": ["new incidents only", "facts only", "no LLM for retrieval",
                      "references and counts", "reach never changes severity"],
    },
    "historical_pattern": {
        "trigger": "Invoked by the Pipeline Orchestrator after Context Enrichment for a new "
                   "incident; never on an attach. Re-runs whenever Enrichment re-runs, and on "
                   "demand. RCA never searches history itself.",
        "reads": _reads("agent", "mcp_server", "system", "data_quality_rule",
                        "database_structure", "cde", "business_calendar", "calendar_day",
                        "resolved_pattern", "incident", "agent_decision", "precedent"),
        "writes": [_w("agent_decision", True, "HistoricalPatternMatched"), _w("precedent"),
                   _w("agent_run"), _w("agent_tool_call")],
        "links": [{"from": "precedent", "to": "incident", "rel": "MATCHED_INCIDENT"},
                  {"from": "precedent", "to": "resolved_pattern", "rel": "REFERENCES"}],
        "calls": [_c("itsm", "update_incident")],
        "invoked_by": "pipeline_orchestrator",
        "handoff": {"to": "pipeline_orchestrator", "next": "rca",
                    "carries": ["decision_id", "matched"],
                    "text": "Returns the decision ID to the Pipeline Orchestrator, which invokes "
                            "RCA. RCA links its own USED_PRECEDENT {confidenceLift} to the "
                            "precedents it relies on."},
        "human_review": "None. Zero matches is a fact, not InsufficientEvidence. Severity is "
                        "never changed.",
        "policy": {"lookback_days": 730, "tiers": 2, "list_cap": 25},
        "llm": "none",
        "decisions": ["facts only", "two search tiers", "deterministic matching",
                      "only Historical Pattern creates Precedents"],
    },
    "rca": {
        "trigger": "After Historical Pattern, in each round of the evidence loop.",
        "reads": _reads("incident", "agent_decision", "coverage_gap", "precedent",
                        "resolved_pattern", "data_quality_rule", "cde"),
        "writes": [_w("agent_decision", True, "RootCauseCandidates|InsufficientEvidence")],
        "handoff": {"to": "pipeline_orchestrator", "carries": ["candidates", "rca_verdict"],
                    "text": "Returns the candidate root causes, or InsufficientEvidence, to the "
                            "Pipeline Orchestrator, which re-runs Enrichment and Historical "
                            "Pattern or routes the candidates to the lanes."},
        "human_review": "None: lanes confirm or rule out; a person approves any fix.",
        "policy": {"token_budget": 6000, "max_rounds": 3},
        "llm": "agent",
        "decisions": ["cites only offered evidence", "InsufficientEvidence re-runs Enrichment "
                      "and Historical Pattern, at most twice"],
    },
    "pipeline_orchestrator": {
        "trigger": "After the evidence loop ends (conclusive, or its rounds used up).",
        "reads": _reads("agent_decision", "incident"),
        "writes": [_w("agent_decision", True, "Routed")],
        "handoff": {"to": "lanes", "carries": ["lanes"],
                    "text": "Starts one lane per candidate root-cause family above the routing "
                            "floor, in parallel."},
        "human_review": "None. No candidate above the routing floor starts no lane.",
        "policy": {"min_route_confidence": 0.3},
        "llm": "none",
        "decisions": ["one lane per candidate family"],
    },
    "lane": {
        "trigger": "One per routed family (lanes).",
        "reads": _reads("incident", "agent_decision", "precedent", "data_quality_rule",
                        "dq_threshold", "system", "feed"),
        "writes": [_w("agent_decision", True, "Confirmed|RuledOut|CannotVerify")],
        "handoff": {"to": "pipeline_orchestrator", "next": "remediation_advisor",
                    "carries": ["lane_verdict", "cause"],
                    "text": "Returns its verdict to the Pipeline Orchestrator. A confirmed cause "
                            "goes to the Remediation Advisor; a confirmed false positive also "
                            "opens a rule authoring case with Rule Drafting."},
        "human_review": "None. CannotVerify leaves the incident for a person.",
        "policy": {"token_budget": 4000},
        "llm": "agent",
        "decisions": ["a verdict cites evidence it was offered"],
    },
    "rule_drafting": {
        "trigger": "After the lanes: a lane that confirmed a false positive (rule_config) opens "
                   "a rule authoring case.",
        "reads": _reads("agent_decision", "data_quality_rule", "dq_threshold", "incident"),
        "writes": [_w("agent_decision", True, "RuleProposed|NothingToDraft"),
                   _w("rule_proposal"), _w("rule")],
        "handoff": {"to": "remediation_advisor", "carries": ["proposal"],
                    "text": "Leaves the rule proposal in Rules review for a person, and hands on "
                            "to the Remediation Advisor."},
        "human_review": "Every rule change is a proposal a person approves in Rules review.",
        "policy": {"token_budget": 3000},
        "llm": "agent",
        "decisions": ["proposes; never changes a rule"],
    },
    "remediation_advisor": {
        "trigger": "After the lanes and rule drafting.",
        "reads": _reads("agent_decision", "incident", "precedent", "resolved_pattern", "owner"),
        "writes": [_w("agent_decision", True, "RemediationPrepared|NoPackage"),
                   _w("remediation_package")],
        "handoff": {"to": "human_approval", "carries": ["remediation_package_id"],
                    "text": "Hands the prepared remediation package to a person for approval; "
                            "nothing has been done yet."},
        "human_review": "A person approves at Human approval; a different person carries it out.",
        "policy": {"token_budget": 4000, "prepare_only": True},
        "llm": "agent",
        "decisions": ["prepared only", "executor and approver are different people"],
    },
    "human_approval": {
        "trigger": "A remediation package is prepared.",
        "reads": _reads("remediation_package"),
        "human_review": "A person approves the prepared fix; a rejection stops the run.",
        "llm": "none",
    },
    "human_execution": {
        "trigger": "After a person approved the fix.",
        "reads": _reads("remediation_package"),
        "human_review": "A DIFFERENT person than the approver carries the fix out and records "
                        "the change reference; the engine refuses the approver.",
        "llm": "none",
    },
    "remediation_fix": {
        "trigger": "After Human execution.",
        "reads": _reads("remediation_package", "agent_decision"),
        "writes": [_w("agent_decision", True, "FixRecorded")],
        "calls": [_c("itsm", "update_incident")],
        "handoff": {"to": "incident_closure", "carries": ["change_ref"],
                    "text": "Hands the change reference to Incident Closure, which re-runs the "
                            "check before closing."},
        "human_review": "Records what the people did; refuses to run without both answers.",
        "llm": "none",
        "decisions": ["prepare-only cap: no agent executes fixes"],
    },
    "incident_closure": {
        "trigger": "After the fix is recorded, or when no cause was confirmed.",
        "reads": _reads("incident", "agent_decision", "remediation_package", "precedent",
                        "resolved_pattern", "data_quality_rule"),
        "writes": [_w("agent_decision", True, "IncidentClosed|FixNotConfirmed|LeftOpen"),
                   _w("precedent"), _w("resolved_pattern")],
        "calls": [_c("eagle", "rerun_check"), _c("itsm", "update_incident")],
        "handoff": {"carries": ["precedent_id"],
                    "text": "Leaves the precedent for the next investigation's Historical "
                            "Pattern to find."},
        "human_review": "Never closes on a failing re-run; an unconfirmed incident stays open.",
        "llm": "none",
        "decisions": ["re-run before closing", "maintains resolved patterns",
                      "writes the precedent"],
    },
    "escalation": {
        "trigger": "Daily, over every open incident (its own scheduled workflow).",
        "reads": _reads("incident", "owner", "cde", "calendar_day", "report_line"),
        "writes": [_w("agent_decision", True, "EscalationSweep|Escalated")],
        "calls": [_c("itsm", "update_incident")],
        "human_review": "The top rung is the Chief Data Officer.",
        "policy": {"ladder_business_days": [1, 3, 5], "regulatory_reach": "one rung sooner"},
        "llm": "none",
        "decisions": ["escalation only rises"],
    },
    "eagle_intake": {
        "trigger": "Each morning (scheduled) or on demand: Eagle's latest close of business.",
        "reads": _reads("exception_group"),
        "writes": [_w("agent_decision", True, "IntakeRouted")],
        "calls": [_c("eagle", "list_exception_groups")],
        "human_review": "Starts investigations; every fix still needs two people.",
        "llm": "none",
    },
    "context_curator": {
        "trigger": "Called by every LLM step before it is asked.",
        "reads": _reads("agent_decision"),
        "human_review": "None: it ranks and trims, and reports what it dropped.",
        "llm": "none",
    },
    "profile_service": {
        "trigger": "Called by Context Enrichment (and on demand) for a rule on its platform.",
        "reads": _reads("system"),
        "calls": [_c("olympus", "get_exception_profile")],
        "human_review": "None: a platform or rule it does not profile is said so.",
        "llm": "none",
    },
}

# ── Agent designs: numbered internal steps and the design cards ─────────────────────────────

_EAGLE, _ITSM, _OLY = "eagle", "itsm", "olympus"
DQ_STAGE_DESIGNS: dict[str, dict[str, Any]] = {
    "monitoring": {
        "steps": [
            _s(1, "Read the group", "Reads the exception group Eagle published: one rule "
               "version, one COB, its grade and measure.", ("agent", "mcp_server", "system",
                                                             "exception_group"),
               calls=((_EAGLE, "get_exception_group"),)),
            _s(2, "Resolve the rule", "Finds the rule in the graph by its rule id, with its "
               "table and schema; a rule not there yet is a coverage gap.",
               ("data_quality_rule", "database_structure", "database_table")),
            _s(3, "Grade severity", "Grades the measure against the rule's own Warning and "
               "Error thresholds; only Error opens or attaches.", ("dq_threshold", "cde")),
            _s(4, "Set failure reason", "One failure reason per DQ rule (A-D10).",
               ("data_quality_rule",)),
            _s(5, "Tag the exception", "Tags the CDEs, column and platform.",
               ("data_quality_rule",)),
            _s(6, "Match", "Looks for an open incident on the same rule.", ("incident",),
               calls=((_ITSM, "list_incidents"),)),
            _s(7, "Open or attach", "Opens a ticket to the rule owner's group, or attaches the "
               "group to the open one.", ("data_quality_rule", "cde"),
               ("incident", "agent_decision"),
               ((_ITSM, "create_incident"), (_ITSM, "attach_to_incident"))),
            _s(8, "Link facts", "Links the incident to its group, rule, CDEs and owner, and "
               "CORRELATED_WITH earlier incidents on the rule (linkType); facts only.",
               writes=("incident",)),
            _s(9, "Write trace, hand off", "Writes the decision with gradedSeverity and "
               "policyVersion, the run and its tool calls, and hands off.", ("agent",),
               ("agent_decision", "coverage_gap", "agent_run", "agent_tool_call")),
        ],
        "design": {
            "status": "locked", "reference": "Agent Reference v0.1",
            "example": "EGL-0588 v2 on COB 2026-10-05 (locked running example).",
            "under_revision": "Rule authoring changed DQ grading (A-D87 to A-D89, KG v17): each "
                              "rule has its own Warning and Error conditions; Eagle grades "
                              "every run; Warning goes to the OMNIS dashboard and Monitoring "
                              "writes nothing; only Error opens or attaches to an incident; "
                              "OP-SEV-01 retired. Open: how incident severity is set, and what "
                              "happens when the next COB is Warning or clean. The steps below "
                              "show the locked v0.1 design.",
            "decisions": [_n("A-D8", "severity only moves up"), _n("A-D9", "link facts only"),
                          _n("A-D10", "one failure reason per DQ rule"),
                          _n("A-D11", "ticket to the rule owner"),
                          _n("A-D12", "Pipeline Orchestrator supervises"),
                          _n("A-D13", "policyVersion on every run")],
            "applied_in": {"kg": "v17", "cg": "v4", "items": [
                "gradedSeverity on the decision and severity that can only rise (CG-G1)",
                "linkType on CORRELATED_WITH (CG-G2)", "policyVersion on the run (CG-G4)"]},
            "open_questions": [
                _n("CG-G3", "what Eagle's exception-group payload carries, and push or poll"),
                _n("KG-G1", "rule owner to ITSM assignment group")],
        },
    },
    "context_enrichment": {
        "steps": [
            _s(1, "Load the incident", "Loads the incident Monitoring opened and its decision.",
               ("agent", "incident", "agent_decision")),
            _s(2, "Rule footprint", "The rule's CDEs, columns, tables and feed.",
               ("data_quality_rule", "cde", "database_column", "database_table",
                "database_structure")),
            _s(3, "Owners", "The rule owner, CDE stewards and system owner, with their ITSM "
               "groups.", ("owner", "system")),
            _s(4, "Subscriptions", "The feeds that load the failing table and the ones that read "
               "it.", ("feed", "database_table", "system")),
            _s(5, "Upstream lineage", "Column lineage upstream within the hop cap; a feed hop "
               "nobody registered is a coverage gap.", ("lineage", "database_column", "feed")),
            _s(6, "Regulatory reach", "CDE —REPORTED_IN→ report lines, their reports and MDRM "
               "codes (KG-G11).", ("cde", "report_line", "regulatory_report", "mdrm")),
            _s(7, "Known issues", "Issues and examination findings at every level (A-D40).",
               ("issue", "examination_finding")),
            _s(8, "Exception profile", "The rule's baseline on its platform from the Profile "
               "Service (A-D41).", ("system",), calls=((_OLY, "get_exception_profile"),)),
            _s(9, "Recent changes", "Changes on the platform, rule and feed in the look-back.",
               ("system", "feed"), calls=((_OLY, "list_recent_changes"),)),
            _s(10, "Assemble, hand off", "Writes ContextAssembled with contextPack and asOf, "
               "the coverage gaps, the run and its tool calls.", ("agent",),
               ("agent_decision", "coverage_gap", "agent_run", "agent_tool_call"),
               ((_ITSM, "get_incident"),)),
        ],
        "design": {
            "status": "locked", "reference": "Agent Reference v0.2",
            "example": "INC-501 · EGL-0588 v2 on COB 2026-10-05 (locked running example).",
            "decisions": [_n("A-D30", "new incidents only"), _n("A-D31", "facts only"),
                          _n("A-D32", "no LLM for retrieval"),
                          _n("A-D33", "references and counts"),
                          _n("A-D34", "reach never changes severity"),
                          _n("A-D36", "coverage gaps"), _n("A-D38", "regulatory reach"),
                          _n("A-D40", "known issues at every level"),
                          _n("A-D41", "profile service")],
            "policy": [_n("OP-ENR-01", "hop cap 3"), _n("OP-ENR-02", "change lookback 14 days"),
                       _n("OP-ENR-03", "list cap 25"),
                       _n("dataAccess", "Standard profile only (Orchestration Policy v0.2.0)")],
            "applied_in": {"kg": "v16", "cg": "v4", "items": [
                "CDE —REPORTED_IN→ RegulatoryReport (KG-G11)",
                "isProfileDimension on HAS_COLUMN (KG-G12)",
                "ContextAssembled with contextPack and asOf (CG-G9)"]},
        },
    },
    "historical_pattern": {
        "steps": [
            _s(1, "Load the pack", "Loads the incident and Enrichment's decision.",
               ("agent", "incident", "agent_decision")),
            _s(2, "Sibling rules", "Related rules and rules on the same CDE or column.",
               ("data_quality_rule", "database_structure", "cde")),
            _s(3, "Reference data", "The business calendar (business day of the quarter) and "
               "the resolved patterns with their durable fixes.",
               ("system", "cde", "business_calendar", "calendar_day", "resolved_pattern")),
            _s(4, "find_past_incidents", "Closed incidents in two tiers: the same rule, then "
               "sibling rules or the same CDE, two years back.",
               ("incident", "resolved_pattern", "agent_decision")),
            _s(5, "Write precedents", "Precedents, MATCHED_INCIDENT and REFERENCES, the run "
               "and its tool calls.",
               writes=("precedent", "agent_decision", "agent_run", "agent_tool_call")),
            _s(6, "Post briefing", "A work note on the ticket.", ("mcp_server",),
               calls=((_ITSM, "update_incident"),)),
            _s(7, "Hand off", "Writes HistoricalPatternMatched.", writes=("agent_decision",)),
        ],
        "design": {
            "status": "locked", "reference": "Agent Reference v0.3",
            "example": "INC-501 · EGL-0588 v2 on COB 2026-10-05 (business day 3 of Q4); "
                       "lookback from 2024-10-05.",
            "decisions": [_n("A-D48", "facts only"), _n("A-D49", "two search tiers"),
                          _n("A-D50", "deterministic matching"),
                          _n("A-D51", "KG data passed as parameters"),
                          _n("A-D52", "grouping by pattern"), _n("A-D53", "negative precedents"),
                          _n("A-D55", "only Historical Pattern creates Precedents"),
                          _n("A-D57", "recurrence facts")],
            "applied_in": {"kg": "v16", "cg": "v4", "items": [
                "BusinessCalendar and CalendarDay (KG-G17)", "fixDurability (KG-G16)",
                "MATCHED_INCIDENT, Precedent recurrence facts, SameColumn and timingAlignment "
                "(CG-G12 to CG-G16)"]},
            "open_questions": [
                _n("HP-Q1", "promotionCandidate versus GovernanceFlag and RulePromotion. "
                            "Per-cause confidenceLift — being settled in the RCA review.")],
        },
    },
    "rca": {
        "steps": [
            _s(1, "Read the curated pack", "The Context Curator's pack, within the token budget.",
               ("incident", "agent_decision", "coverage_gap")),
            _s(2, "Weigh the precedents", "Tier-1 and tier-2 precedents and their patterns.",
               ("precedent", "resolved_pattern")),
            _s(3, "Tier-1 explanations", "Causes a precedent explains, checked against this "
               "exception.", ("precedent",)),
            _s(4, "Tier-2 explanations", "Causes the evidence explains without a precedent.",
               ("data_quality_rule", "cde")),
            _s(5, "Candidates or InsufficientEvidence", "Writes the candidates with confidence "
               "and USED_PRECEDENT {confidenceLift}, or what is missing.",
               writes=("agent_decision",)),
        ],
        "design": {
            "status": "in_review", "reference": "",
            "decisions": [_n("RCA-1", "cites only evidence it was offered"),
                          _n("RCA-2", "InsufficientEvidence re-runs Enrichment and Historical "
                                      "Pattern, at most twice")],
            "open_questions": [_n("HP-Q1", "per-cause confidenceLift")],
        },
    },
    "pipeline_orchestrator": {
        "steps": [_s(1, "Read the candidates", "RCA's latest round.", ("agent_decision",)),
                  _s(2, "Route each family", "One lane per family above the routing floor.",
                     ("incident",), ("agent_decision",))],
        "design": {"status": "not_reviewed", "reference": ""},
    },
    "lane": {
        "steps": [_s(1, "Read the family's evidence", "Candidates, precedents, thresholds.",
                     ("incident", "agent_decision", "precedent", "data_quality_rule",
                      "dq_threshold")),
                  _s(2, "Confirm or rule out", "A verdict citing evidence.", ("system", "feed"),
                     ("agent_decision",))],
        "design": {"status": "not_reviewed", "reference": ""},
    },
    "rule_drafting": {
        "steps": [_s(1, "Read the false positive", "The confirmed rule_config lane and the "
                     "rule's thresholds.", ("agent_decision", "data_quality_rule",
                                            "dq_threshold")),
                  _s(2, "Draft the rule change", "A rule proposal into Rules review.",
                     ("incident",), ("rule_proposal", "rule", "agent_decision"))],
        "design": {"status": "not_reviewed", "reference": ""},
    },
    "remediation_advisor": {
        "steps": [_s(1, "Read the confirmed cause", "The lane verdict and the templates.",
                     ("agent_decision", "resolved_pattern", "precedent")),
                  _s(2, "Prepare the package", "Fix, target, executor, approver, change content.",
                     ("owner", "incident"), ("remediation_package", "agent_decision"))],
        "design": {"status": "not_reviewed", "reference": ""},
    },
    "human_approval": {"design": {"status": "human_step", "reference": ""}},
    "human_execution": {"design": {"status": "human_step", "reference": ""}},
    "remediation_fix": {
        "steps": [_s(1, "Record the execution", "Who approved, who carried it out, the change.",
                     ("remediation_package", "agent_decision"), ("agent_decision",),
                     ((_ITSM, "update_incident"),))],
        "design": {"status": "not_reviewed", "reference": "",
                   "decisions": [_n("PREP-1", "prepare-only cap: no agent executes fixes")]},
    },
    "incident_closure": {
        "steps": [_s(1, "Re-run the check", "Eagle re-runs the rule after the fix.",
                     ("data_quality_rule",), calls=((_EAGLE, "rerun_check"),)),
                  _s(2, "Finalise the cause", "From the confirmed lane and the package.",
                     ("agent_decision", "remediation_package")),
                  _s(3, "Close the incident", "Graph and ticket.", ("incident",),
                     ("agent_decision",), ((_ITSM, "update_incident"),)),
                  _s(4, "Maintain resolved patterns", "An existing pattern seen again, or a new "
                     "one.", ("resolved_pattern",), ("resolved_pattern",)),
                  _s(5, "Write the precedent", "For the next investigation.", ("precedent",),
                     ("precedent",))],
        "design": {"status": "not_reviewed", "reference": ""},
    },
    "escalation": {
        "steps": [_s(1, "Sweep open incidents", "Every open DQ incident.", ("incident",)),
                  _s(2, "Apply the ladder", "Business days open on the calendar; regulatory "
                     "reach one rung sooner.", ("calendar_day", "cde", "report_line", "owner")),
                  _s(3, "Escalate", "Note it on the ticket and record it.",
                     writes=("agent_decision",), calls=((_ITSM, "update_incident"),))],
        "design": {"status": "not_reviewed", "reference": ""},
    },
    "context_curator": {
        "design": {"status": "shared_service", "reference": "Chapter to write",
                   "decisions": [_n("CC-1", "ranks and trims context to each agent's token "
                                            "budget; routes nothing; reports what it dropped")]},
    },
    "profile_service": {
        "design": {"status": "shared_service",
                   "reference": "Built at the investigation checkpoint",
                   "decisions": [_n("A-D41", "deterministic exception profiles on each data "
                                             "platform")]},
    },
}


def contract(stage: str) -> dict[str, Any]:
    """A stage's contract, with its steps and design when the schema carries them."""
    out = dict(DQ_CONTRACTS[stage])
    if getattr(step_contracts, "SUPPORTS_STEPS_AND_DESIGN", False):
        out.update(DQ_STAGE_DESIGNS.get(stage) or {})
    return out


#: The pipeline's subject on a gate card: the group, its rule and the incident.
_SUBJECT = ["{steps.monitoring.output.incident_id} · {steps.monitoring.output.group_label}",
            "{steps.monitoring.output.group_label}", "{input.exception_group_id}"]


def entries(step: Callable[..., dict[str, Any]], from_input: Callable[[str], dict],
            ) -> list[dict[str, Any]]:
    """The three catalogue entries, built with the catalogue's own step helpers."""
    def cap(step_id: str, capability: str, title: str, inputs: dict | None = None,
            nxt: list[str] | None = None, **extra: Any) -> dict[str, Any]:
        return {**step(step_id, capability, inputs or {}, nxt=nxt or []), "title": title,
                "contract": contract(step_id), **extra}

    rca = {
        "key": "dq_incident_rca",
        "name": "DQ RCA and Remediation",
        "description": "Investigate an Eagle exception group end to end: open or attach the "
                       "incident, assemble its context and history, find the root cause, confirm "
                       "it per family, prepare the fix — a person approves, a different person "
                       "executes — and close it with a precedent.",
        "objective": (
            "Investigate one Eagle exception group on NorthPeak Bank's data platform. Monitoring "
            "opens or attaches one incident for an Error group and never diagnoses. Context "
            "Enrichment and Historical Pattern are fixed graph reads, never a model. The RCA "
            "Agent proposes candidate causes citing the evidence, or says the evidence is "
            "insufficient, which runs Enrichment and Historical Pattern again (at most twice). "
            "Each candidate family goes to its lane; a confirmed false positive becomes a rule "
            "proposal. The Remediation Advisor prepares the fix; a person approves it and a "
            "different person carries it out — no agent executes. Closure re-runs the check, "
            "closes the incident and writes the precedent the next investigation finds."),
        "work_class": "any",
        "requires": {"ontology_pack": PACK, "mcp": ("eagle", "olympus", "itsm")},
        "pipeline": {
            "loops": [{"from": "rca", "to": ["context_enrichment", "historical_pattern"],
                       "label": "InsufficientEvidence → re-run Enrichment and Historical "
                                "Pattern", "max_rounds": 2}],
            "side_workflows": [{"key": "dq_incident_escalation", "stage": "escalation",
                                "label": "9 · Escalation Agent — runs daily over every open "
                                         "incident and applies the escalation ladder",
                                "schedule": "daily"}],
            "side_paths": [{"from": "lanes", "to": "rule_drafting",
                            "label": "False Positive lane: rule proposal"}],
            "shared_services": ["context_curator", "profile_service"],
            "design_target": {"stage": "remediation_fix",
                              "label": "8 · Remediation Fix: design target"},
        },
        "steps": [
            cap("monitoring", "dq_monitoring", "Monitoring",
                {"exception_group_id": from_input("exception_group_id")}, ["incident_route"]),
            {"id": "incident_route", "kind": "condition", "title": "A new incident?",
             "expression": {"path": "steps.monitoring.output.continue_pipeline", "op": "truthy"},
             "on_true": "evidence_loop", "on_false": "attached_stop", "next": []},
            {"id": "attached_stop", "kind": "timer", "wait_seconds": 0, "binding": {},
             "title": "Attached to an open incident, or not an Error — stop", "next": []},
            {"id": "evidence_loop", "kind": "bounded_loop", "title": "Evidence loop",
             "body": ["context_enrichment", "historical_pattern", "rca"], "max_iterations": 3,
             "expression": {"path": "steps.rca.output.rca_verdict", "op": "eq",
                            "value": "InsufficientEvidence"},
             "on_exhausted": "continue", "next": ["pipeline_orchestrator"]},
            cap("context_enrichment", "dq_context_enrichment", "Context Enrichment"),
            cap("historical_pattern", "dq_historical_pattern", "Historical Pattern"),
            cap("rca", "dq_rca", "RCA"),
            cap("pipeline_orchestrator", "dq_orchestration", "Pipeline Orchestrator", nxt=["lanes"]),
            {"id": "lanes", "kind": "for_each", "title": "Lanes",
             "items": "steps.pipeline_orchestrator.output.lanes", "body": "lane",
             "max_items": 5, "concurrency": 3, "item_label": "root-cause family",
             "next": ["rule_drafting"]},
            cap("lane", "dq_lane", "Lane", {"item": {"from_item": ""}}),
            cap("rule_drafting", "dq_rule_drafting", "Rule Drafting", nxt=["remediation_advisor"],
                side_path="Rule authoring"),
            cap("remediation_advisor", "dq_remediation_package", "Remediation Advisor",
                nxt=["package_route"]),
            {"id": "package_route", "kind": "condition", "title": "A fix prepared?",
             "expression": {"path": "steps.remediation_advisor.output.package_ready",
                            "op": "truthy"},
             "on_true": "human_approval", "on_false": "left_open", "next": []},
            {"id": "left_open", "kind": "timer", "wait_seconds": 0, "binding": {},
             "title": "No confirmed cause — the incident stays open",
             "next": ["incident_closure"]},
            {"id": "human_approval", "kind": "decision", "title": "Human approval",
             "subject": _SUBJECT, "summary": ["{steps.remediation_advisor.output.note}"],
             "question": "Approve the prepared fix?",
             "guidance": "The Remediation Advisor prepared it from the confirmed cause; nothing "
                         "has been done. Approving lets a DIFFERENT person carry it out — you "
                         "cannot also do the execution.",
             "approval": {"mode": "manual", "approver_role": "human"},
             "contract": contract("human_approval"), "next": ["human_execution"]},
            {"id": "human_execution", "kind": "decision", "title": "Human execution",
             "subject": _SUBJECT, "summary": ["{steps.remediation_advisor.output.note}"],
             "question": "Carry out the approved fix, then confirm it here with the change "
                         "reference.",
             "guidance": "Separation of duties: the person who approved the fix cannot carry it "
                         "out. BOSS executes nothing (prepare-only); write the change ticket "
                         "(e.g. CHG-7760) in your note.",
             "approval": {"mode": "manual", "approver_role": "human",
                          "distinct_from": ["human_approval"]},
             "recommendation": {"reason_label": "Carried out",
                                "reason_prompt": "The change reference and what you did."},
             "contract": contract("human_execution"), "next": ["remediation_fix"]},
            cap("remediation_fix", "dq_remediation_fix", "Remediation Fix (design target)",
                nxt=["incident_closure"]),
            cap("incident_closure", "dq_incident_closure", "Incident Closure"),
            # Drawn in the pipeline, run elsewhere: the daily Escalation Agent (its own
            # scheduled workflow) and the two shared services.
            cap("escalation", "dq_escalation", "Escalation Agent",
                side_path="Daily over every open incident", stage_number=9),
            {"id": "context_curator", "kind": "deterministic", "title": "Context Curator",
             "description": "Ranks and trims context to each agent's token budget; routes "
                            "nothing.",
             "binding": {"inputs": {"capability": "dq_context_curator"}},
             "contract": contract("context_curator"), "next": []},
            {"id": "profile_service", "kind": "deterministic", "title": "Profile Service",
             "description": "Deterministic exception profiles on each data platform (A-D41).",
             "binding": {"inputs": {"capability": "dq_profile_service"}},
             "contract": contract("profile_service"), "next": []},
        ],
    }
    escalation = {
        "key": "dq_incident_escalation",
        "name": "DQ Incident Escalation",
        "description": "Every day, apply the escalation ladder to each open data-quality "
                       "incident: the longer it stays open, the higher it goes.",
        "objective": "Sweep every open DQ incident, count the business days it has been open on "
                     "the bank's calendar, and raise its escalation to the next rung when due "
                     "(regulatory reach one rung sooner). Escalation only rises.",
        "work_class": "any",
        "requires": {"ontology_pack": PACK, "mcp": ("itsm",)},
        "steps": [cap("escalation", "dq_escalation", "Escalation Agent",
                      {"as_of": from_input("as_of")})],
    }
    intake = {
        "key": "dq_eagle_intake",
        "name": "DQ Eagle Intake",
        "description": "Read Eagle's latest close of business and start an RCA for each "
                       "exception group graded Error.",
        "objective": "The Pipeline Orchestrator reads Eagle's exception groups for the COB and "
                     "starts one dq_incident_rca run per Error group not already under "
                     "investigation.",
        "work_class": "any",
        "requires": {"ontology_pack": PACK, "mcp": ("eagle",)},
        "steps": [cap("eagle_intake", "dq_eagle_intake", "Eagle intake",
                      {"cob": from_input("cob")})],
    }
    return [rca, escalation, intake]


#: How Start asks for each entry's brief (workflow_catalogue.BRIEFS).
BRIEFS: dict[str, dict[str, Any]] = {
    "dq_incident_rca": {
        "directive": ("What should this investigation focus on? (optional)",
                      "The exception group's own evidence is always assembled."),
        "example": {"title": "Investigate EGL-0588 v2 on COB 2026-10-05",
                    "exception_group_id": "EXG-20261005-0003",
                    "directive": "Principal outstanding no longer reconciles to its components."},
        "required": ("exception_group_id",),
    },
    "dq_incident_escalation": {
        "directive": ("Anything to note? (optional)", "The sweep covers every open incident."),
        "example": {"title": "Escalation sweep", "as_of": "2026-10-09",
                    "directive": "Daily sweep."},
    },
    "dq_eagle_intake": {
        "directive": ("Anything to note? (optional)", "Eagle's latest COB is read."),
        "example": {"title": "Eagle intake for COB 2026-10-07", "cob": "2026-10-07",
                    "directive": "Investigate the COB's Error groups."},
    },
}

FIELD_TEXT: dict[str, dict[str, Any]] = {
    "exception_group_id": {"type": "string", "title": "Exception group",
                           "description": "The Eagle exception group to investigate (Knowledge "
                                          "Graph › Documents › DQ sample lists them by COB), e.g. "
                                          "EXG-20261005-0003 (EGL-0588 v2 on COB 2026-10-05).",
                           "examples": ["EXG-20261005-0003"]},
    "as_of": {"type": "string", "title": "As of (optional)",
              "description": "The day the ladder is applied for (YYYY-MM-DD). Default: today.",
              "examples": ["2026-10-09"]},
    "cob": {"type": "string", "title": "Close of business (optional)",
            "description": "Eagle's COB to read (YYYY-MM-DD). Default: the latest.",
            "examples": ["2026-10-07"]},
}
