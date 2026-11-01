---
name: dq-incident-rca
description: The consultant's DQ root-cause analysis and remediation pipeline (dq_incident_rca, Instance A of kg_investigation_pipeline) on NorthPeak Bank's data platform — the role agents, what each reads, writes and calls, and the role prompts of the four that reason (RCA, lanes, rule drafting, remediation). Agents prepare; people approve and a different person executes.
---

# DQ incident RCA and remediation: the agents

One Eagle exception group, investigated end to end (`services/knowledge/dq_incident_pipeline.py`).
Retrieval is deterministic — graph queries and connector reads, $0 and repeatable; only RCA, the
lanes, Rule Drafting and the Remediation Advisor reason, through the governed
`make_code_agent(...).run_role(...)` path, fed by the Context Curator.

| # | Agent | Callsign | Runs as | Prompt | Writes |
| --- | --- | --- | --- | --- | --- |
| 1 | Monitoring Agent | Watch | code | — | Incident (open or attach), AgentDecision IncidentOpened/IncidentAttached, CORRELATED_WITH |
| 2 | Context Enrichment Agent | Gather | code, no LLM | — | AgentDecision ContextAssembled (contextPack, asOf), CoverageGap |
| 3 | Historical Pattern Agent | Echo | code, no LLM | — | Precedent, MATCHED_INCIDENT, REFERENCES, AgentDecision HistoricalPatternMatched |
| 4 | RCA Agent | Probe | LLM | `roles/rca_agent.md` | AgentDecision RootCauseCandidates or InsufficientEvidence, USED_PRECEDENT {confidenceLift} |
| 5 | Pipeline Orchestrator | Conductor | code (routing policy) | — | AgentDecision Routed |
| 6 | Lane Agent | Trace | LLM, one per family | `roles/lane_agent.md` | AgentDecision Confirmed/RuledOut/CannotVerify |
| – | Rule Drafting Agent | Draft | LLM, false positives | `roles/rule_drafting_agent.md` | RuleProposal, a proposed rule in Rules review |
| 7 | Remediation Advisor | Remedy | LLM | `roles/remediation_advisor.md` | RemediationPackage (prepared only) |
| – | Human approval, Human execution | people | — | — | a person approves; a DIFFERENT person carries it out |
| 8 | Remediation Fix | design target | code | — | AgentDecision FixRecorded (no agent executes) |
| 10 | Incident Closure Agent | Seal | code | — | Precedent, ResolvedPattern, AgentDecision IncidentClosed |
| 9 | Escalation Agent | Ladder | code, daily | — | AgentDecision Escalated (its own scheduled workflow) |
| S | Context Curator | service | code | — | ranks and trims each LLM step's context; reports what it dropped |
| S | Profile Service | service | Olympus | — | exception profiles per data platform (A-D41) |

Every reasoning call answers with one JSON object between `=== JUDGEMENT ===` / `=== END
JUDGEMENT ===`. An unreadable answer is no answer; anything cited must be in the list the agent
was offered. RCA's InsufficientEvidence sends Context Enrichment, Historical Pattern and RCA
round again (at most two more rounds), each re-run widening its reach and writing a new decision.
