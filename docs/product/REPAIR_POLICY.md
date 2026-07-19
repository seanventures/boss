# Budget-variable repair depth + tier escalation (VAG #109)

> Spec for Plane #109 (Engine & Model Quality / VAG module). Implemented on
> `feat/vag-repair-policy`. Pure and model-free.

## 1. Why

VAG Phase 3 gave the pipeline a repair loop: on a failed gate, feed the concrete evidence back and
retry, up to a **fixed** `code_repair_max_retries`. A fixed count is wrong both ways — it burns budget
on a doomed cheap model, and it caps a task a little more depth would finish.

## 2. Two levers

- **Budget-variable depth.** Repair depth is what the *remaining* budget affords (capped by the retry
  cap), not a constant: `budget_variable_depth = min(remaining / est_attempt_cost, max_retries)`.
  Ample budget → go deeper; tight budget → stop before overspending.
- **Escalate the tier on poor $/verified.** If the cheap implementer has burned `escalate_after`
  attempts and verified **nothing**, that's an *infinite* $/verified. Switching to a stronger, pricier
  model is counter-intuitively **cheaper per verified outcome** — one $0.20 call that passes beats ten
  $0.02 calls that don't. So escalate to the strong tier, *if the budget affords it* (else stop).

## 3. The decision

`decide_repair(...)` returns the next action after a gate result — `stop` (verified / depth exhausted /
can't afford the next attempt) or `retry` on the `cheap` or `strong` tier — with the reason and the
budget left after. `dollars_per_verified(spent, verified)` is the metric it optimizes (None = infinite,
the escalation signal). `plan_repair(...)` gives the up-front plan for the console / run evidence.

## 4. Implementation

- **`apps/api/app/services/repair_policy.py`** — `affordable_attempts`, `budget_variable_depth`,
  `dollars_per_verified`, `decide_repair`, `plan_repair`. All pure.
- **`POST /v1/repair-policy`** — with `attempt`+`spent_usd_micros`, decide the next action; else return
  the plan. Reads tiers/caps from config, overridable per call.
- Config: `BOSS_REPAIR_ESCALATE_AFTER` (1), `BOSS_REPAIR_EST_ATTEMPT_COST_USD_MICROS` (200k); reuses
  `code_repair_max_retries`, `code_task_budget_usd_micros`, and the `code_agent_low_cost_*`/`strong_*`
  tiers.
- **9 tests** (`apps/api/tests/test_repair_policy.py`): budget math, $/verified, stop conditions,
  cheap→strong escalation, budget-can't-afford-strong, no-strong-tier fallback, plan, endpoint.

## 5. Next

Wire `decide_repair` into the governed repair loop (replace the fixed-count `for` loop): consult it
each attempt, route the implementer to the returned tier via the existing per-step engine routing, and
record the escalation + final $/verified in the run evidence. Feed real per-attempt costs back to
sharpen `est_attempt_cost`.
