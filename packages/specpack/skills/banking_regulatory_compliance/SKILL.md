---
name: banking-regulatory-compliance
description: Role prompts for the banking_regulatory_compliance kg_investigation_pipeline instance — the governed agent calls that reason about a clause's evidence instead of a keyword match or a fixed confidence ladder.
---

# Banking regulatory compliance: role prompts

This instance of `kg_investigation_pipeline` runs ten stages. Two of them —
`context_enrichment` and `historical_pattern` — reuse P5a's `graph_steps` functions verbatim and
stay purely deterministic graph traversal: no model call, no judgement, just "what is reachable
within N hops" and "what shares a neighbor". Everything a human would call a *judgement call* is
made by a governed agent, through the same `make_code_agent(...).run_role(...)` call every other
role-backed capability in this codebase uses (see `regulatory_trace.py`'s `trace_regulation`),
not a keyword match or a hard-coded confidence number.

Five roles, five prompts (`roles/*.md`), each read by
`banking_regulatory_pipeline.py` and handed to the agent as the role prompt for that turn:

| Role | File | Capability | Judges |
| --- | --- | --- | --- |
| Regulatory Intake | `roles/regulatory_intake.md` | `clause_intake` | which clauses need a case (mechanical; the agent is not asked to open cases that don't exist in the document) |
| Compliance Analyst | `roles/compliance_analyst.md` | `compliance_assessment` | implemented / partly / gap, confidence, and which rule/code node (if any) is the citation |
| Pipeline Orchestrator | `roles/pipeline_orchestrator.md` | `compliance_orchestration` | which finding family (`payment_crediting`, `payment_allocation`, `prepayment`) the assessment belongs in |
| Lane Agent | `roles/lane_agent.md` | `lane_confirmation` | confirmed vs. ruled out, with a stated reason |
| Remediation Advisor | `roles/remediation_advisor.md` | `remediation_package` | the change brief and the regression test a fix needs |

Every call answers with one JSON object between `=== JUDGEMENT ===` / `=== END JUDGEMENT ===`
markers (the same convention `regulatory_trace._parse_judgement` uses), so a malformed or missing
answer degrades to the same "no judgement, flag it as a gap/ruled-out, never a guess" behaviour
`trace_regulation` already has — nothing here invents a verdict when the model answers nothing
readable.
