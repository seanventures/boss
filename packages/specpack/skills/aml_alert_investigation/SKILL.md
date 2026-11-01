---
name: aml-alert-investigation
description: Role prompts for the aml_alert_investigation kg_investigation_pipeline instance (case study 4) — the governed agent calls that map an alerted customer's network, recommend close or escalate citing evidence and precedent, and draft a SAR-style narrative. A person makes every determination; agents never close an alert or file a report.
---

# AML alert investigation: role prompts

This instance of `kg_investigation_pipeline` investigates one transaction-monitoring alert. Its
deterministic steps — `alert_intake`, `evidence_collection`, `historical_pattern`, `aml_closure`
— are $0 graph queries and connector reads (`services/knowledge/aml_alert_pipeline.py`); the
three judgement calls are made by governed role agents, through the same
`make_code_agent(...).run_role(...)` path the regulatory instance uses:

| Role | Callsign | File | Capability | Judges |
| --- | --- | --- | --- | --- |
| Network Mapper | Mesh | `roles/network_mapper.md` | `aml_network_mapping` | which parties around the alerted customer are related, each citing the graph edges that show it |
| AML Analyst | Sift | `roles/aml_analyst.md` | `aml_analysis` | close as a false positive or escalate, with a confidence, citing evidence and any prior decision |
| SAR Narrator | Scribe | `roles/sar_narrator.md` | `sar_narrative` | a draft SAR-style narrative, only after a person escalated |

Every call answers with one JSON object between `=== JUDGEMENT ===` / `=== END JUDGEMENT ===`.
An unreadable answer is no answer: no related party, no recommendation, no narrative — never an
invented one. A recommendation that cites no evidence it was given is not a recommendation.

What the agents never do: close an alert, escalate it, or file anything. The `analyst_decision`
gate is a person's determination (with their reason), and `sar_filing` is a person's filing;
`aml_closure` refuses to run without them.
