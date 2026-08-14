# Earned autonomy + value-based scheduling

Short note for **BOSS-348** (gap analysis G1–G3). Autonomy is earned from
evidence; trust and priority never bypass budget, policy, or approvals.

## Surfaces

| ID | Principle | API / code |
|----|-----------|------------|
| **G1** | Autonomy **score exists** and is readable | `GET /v1/roles/{role_key}/autonomy/score?project_id=…` → `trust_score`, `autonomy_level`, `max_allowed_risk_tier`, counters |
| **G2** | Trust **gates risk tiers** (fail-closed) | `POST /v1/roles/{role_key}/autonomy/check` — denies with `trust_score_below_risk_tier_floor` when trust &lt; floor for the requested `risk_tier` |
| **G3** | Schedule prefers **value-per-dollar** under budget pressure | `POST /v1/work-orchestration/schedule-preference`; `JobLeaseService.claim_job` re-ranks candidates by value density when budget is yellow/orange/red |

Also available:

- `POST /v1/roles/{role_key}/autonomy/evaluate` — evidence gather + upgrade/downgrade review
- `POST /v1/roles/{role_key}/trust-evidence` — idempotent trust observations
- `POST /v1/work-orchestration/score-preview` — pure priority + value density
- Work admission: `POST /v1/work-orchestration/items/{id}/admission`

## Trust → risk-tier floors (policy)

From `packages/specpack/policies/autonomy_ladder.yaml` (`trust_risk_tiers`):

| risk_tier | min `trust_score` |
|-----------|-------------------|
| 0 | 0 |
| 1 | 40 |
| 2 | 60 |
| 3 | 80 |

Higher tiers never require *less* trust (floors are forced monotonic). A role may
have a high autonomy ladder level but still be denied a high risk tier if trust
is too low — fail closed.

## Value ranking under budget pressure

- **green**: order by `priority_score`, then value density, then lower cost.
- **yellow / orange / red**: order by **value density** (priority / $) first.
- Ranking never overrides the Budget Governor freezes already enforced at claim
  time (`orange` defers non-P0/P1, `red` freezes infra / non-P0 / high-impact
  tier-2 effects).

## Implementation map

- Score + floors: `engine/boss_engine/services/autonomy_engine.py`
- Gate: `engine/boss_engine/services/role_governance.py` → `check_autonomy`
- Density math: `engine/boss_engine/services/work_value_scoring.py`
- Schedule helper: `WorkOrchestrationService.schedule_preference`
- Claim re-rank: `JobLeaseService._prefer_value_under_budget_pressure`
- Router: `apps/api/app/routers/earned_autonomy.py`
