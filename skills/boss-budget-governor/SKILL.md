---
name: boss-budget-governor
description: Use when implementing or reviewing BOSS budget policies, budget slider behavior, reservations, cost receipts, budget state, spend caps, or infra/model/tool spend enforcement.
---

# BOSS Budget Governor Skill

## When to use

Use this skill for any task involving:

- BudgetPolicy
- BudgetAllocation
- BudgetReservation
- CostReceipt
- Budget status widget/API
- Spend caps, model/tool/infra cost gates
- Reservation-denied decision cards

## Invariants

- Store money as integer micros, never floats.
- No spendy action without active reservation.
- Denied reservation means job must not lease or execute.
- Every consumed reservation creates or links to a CostReceipt.
- Budget hard caps fail closed.

## Budget state thresholds

- green: below soft cap
- yellow: at or above soft cap
- orange: at or above auto-lite-mode threshold
- red: at or above freeze threshold

## Tests to add/update

- Under-cap reservation succeeds.
- Over-cap reservation is denied.
- Denied reservation creates a decision card.
- Denied reservation prevents job leasing/execution.
- Consumed reservation updates actual spend.
