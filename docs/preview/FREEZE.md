# Feature freeze for the preview

**Rule:** no new feature work lands on `main` until every golden journey in the
[preview plan](PREVIEW_PLAN.md#golden-journeys) has been green **5 days in a row**.
Until then, `main` takes only preview-readiness work: fixes, tests, docs, and the
journey runner itself.

Ticket: BOSSAPP-1285. Journeys: BOSSAPP-1284.

## What counts

| Counts as | Meaning |
|---|---|
| A green journey | It ran end to end on a real install with a real model, and BOSS's Acceptance Agent accepted the outcome. A run that a person had to nudge, retry by hand, or waive is not green. |
| A green day | Every journey that day was green, on both the laptop install and the shared server. A journey that did not run that day makes the day red, not "skipped". |
| The counter | Consecutive green days. Any red journey resets it to 0. The freeze lifts at 5. |
| Allowed during the freeze | Bug fixes, test and journey checks, docs, security fixes, and work tickets labelled for preview readiness. |
| Not allowed | New features, new screens, refactors that are not needed for a fix. |

## When a journey fails

1. File a fix ticket that links the failing journey run.
2. Fix the cause as a class, not the single instance (see `CONTRIBUTING.md`).
3. Add a permanent check to the journey, so the same bug fails the journey next time.
   Link the check from the fix ticket.
4. The counter restarts from 0 the next day.

## What exists today

| Piece | State on `main` |
|---|---|
| Acceptance Agent verdict | Yes: `engine/boss_engine/services/acceptance/verdict.py` (BOSS, not the model, checks each cited command against its record). |
| Daily journey runner (J1–J9) | **Not yet** (BOSSAPP-1284). The console journeys in `tests/e2e/harness/journeys.py` are UI checks, not these golden journeys. |
| Green-day counter | **Not yet.** Until the runner reports daily, the 5-day window cannot start. |
| Release gates that run today | `scripts/test-tier.sh`, `scripts/e2e-gate.sh`, `scripts/enterprise-lane.sh` ([TEST_TIERS.md](../operations/TEST_TIERS.md)). They are necessary but do not count toward the 5 days. |
