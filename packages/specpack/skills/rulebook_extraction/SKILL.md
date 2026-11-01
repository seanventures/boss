---
name: rulebook-extraction
description: Role prompt for the rulebook_extraction workflow — the Rulebook Analyst reads the sections of a document a person uploaded and confirmed in Knowledge Graph › Documents, and proposes each data-quality rule it states (id, statement, dimension, severity, check, owner, CDEs, table.column references, regulatory reach) with its Warning and Error thresholds, quoting the clause as evidence. A person approves every rule in Rules review.
---

# Rulebook extraction: role prompt

The `rulebook_extraction` workflow has two steps: `extract` (capability `extract_rulebook_rules`,
`services/knowledge/rulebook_extract.py`) and `review` (`rules_review`, a person's gate). The
extraction is made by a governed role agent through the same `make_code_agent(...).run_role(...)`
path every role-backed step uses:

| Role | Callsign | File | Capability | Reads |
| --- | --- | --- | --- | --- |
| Rulebook Analyst | Rubric | `roles/rulebook_analyst.md` | `extract_rulebook_rules` | a batch of the document's sections (clauses), and proposes the rules they state |

Every call answers with one JSON object between `=== JUDGEMENT ===` / `=== END JUDGEMENT ===`.

What the engine holds the agent to (`rules.propose_dq_rule`):

- The rule id must be printed in the section it cites, and the quote must be a passage of that
  section, word for word. Otherwise the proposal is refused and the run reports it.
- An unreadable answer is no answer: that batch proposes nothing, and the run says which
  sections went unread. Nothing is ever filled in by the engine.
- Every rule is `proposed` and `inferred` until a person approves it in Rules review; its
  Warning/Error thresholds are approved with it.
- The rule is linked to the project's existing CDE, column, table and report-line nodes by name
  (CHECKS, APPLIES_TO, REACHES); a name with no node is recorded as unmatched, never created.

Measure an extraction against the NorthPeak Bank answer key with
`scripts/kg/measure_rulebook_extraction.py`.
