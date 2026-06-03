# Codex Prompt 04 — Budget governor, reservations, and cost receipts

Goal: Implement the financially controlled execution path: quote → reserve → execute → consume/release → cost receipt.

Context to read first:

- AGENTS.md
- specpack/objects/budget.yaml
- specpack/policies/budget_v0.yaml
- specpack/workflows/budget_gated_job.yaml
- docs/TECHNICAL_SPEC.md section Budget governor

Constraints:

- A denied reservation must prevent job leasing/execution for spendy jobs.
- Budget status must include cap, actual, reserved, burn approximation, and state color.
- Use integer micros for money. Do not use floats for stored money.

Implement:

1. Budget quote service.
2. Budget reserve service.
3. Consume/release reservation service.
4. Cost receipt creation.
5. Budget status API.
6. Worker integration: spendy job handlers require reservation.
7. Decision card on budget denial.

Done when:

- Over-cap reservation is denied.
- Under-cap reservation is active then consumed/released.
- Budget status endpoint shows actual/reserved/cap/state.
- Tests cover hard cap and denied-reservation no-lease behavior.
