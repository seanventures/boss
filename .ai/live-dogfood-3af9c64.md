# Live dogfood — shared tag `3af9c64`

**When:** 2026-08-15T14:02Z  
**Deploy:** IMAGE lines only. No `--env-template`. api+worker+web recreated. `/health` 200, db connected.

## Closed (evidence)

| Ticket | Evidence |
|---|---|
| BOSS-341 | GET `/v1/previews` 200, 20 items, `loopback_leaks=[]` |
| BOSS-347 | Seed 200: 13 agents / 3 panels / 33 workflows. A2A `not_configured` |
| BOSS-370 | Sweep tests green; live catalog/KMS/PM/activity/remediations/previews 200 |
| BOSS-372 | GET `/v1/deadletters/remediations` 200 (empty list valid) |
| BOSS-375 | Factory gate dry-run green; live `/v1/factory/wip` 200 |
| BOSS-376 | Simulated process `aprun_bbaf700588db4343a58b` 201 completed |
| BOSS-377 | Compile NL 200, 3 steps |
| BOSS-378 | PM GET+tick 200 |
| BOSS-379 | GET `/v1/activity` 200, total 44235 |
| BOSS-380 | Secret-refs 200, 7 items, no raw `secret_value` |

## Held (named blockers)

| Ticket | Blocker |
|---|---|
| BOSS-351 | `BOSS_FACTORY_ALLOW_CREATE` absent |
| BOSS-369 | durable KMS master missing |
| BOSS-381 | `BOSS_QA_ALLOW_OFFICIAL` unset; no invented scores |
| BOSS-300 | human legal |
| BOSS-337 / 314 / 293 | public-GA umbrellas |
| BOSS-292 | two-partner self-serve not proven |

Not public GA.
