# BOSS QA / Test Engineering Review (Autonomous)

**Date**: 2026-06-05
**Reviewer**: QA / TEST ENGINEERING AGENT
**Scope**: unit, integration, e2e/acceptance, regression, error/perm/empty, state machine, build, smoke, coverage of recent changes.

## Coverage Assessment
- API: apps/api/tests/test_api.py has broad coverage incl recent: canonical work objects (KPI lifecycle), budget consume (perm + active res + receipt), budget forecast, bootstrap seed (3 cases), policy denial logging. Good state machine tests.
- Web: apps/web/tests/ has api-client.test.ts (updated for new endpoints), kpis.test.ts (new, mocks client), accessibility.test.ts, budget-status.test.ts, decisions, software-factory, providerops, etc. Commands, metatron, members, routing tests present.
- Worker: apps/worker/tests/test_worker.py covers lease, runner strategies, job kinds.
- Smoke/real-smoke: Makefile targets for local schema/bootstrap, real reservation->plan, PR proposal, merge, deploy (sandbox GitHub + OpenAI).
- Validators: k8s, infra, release-blockers (no-net).
- Recent gaps added targeted tests: good.
- AC/user-stories: many Given-When-Then in docs/user-stories.md cover states, perms, edges; touched by recent changes.

## Gaps Found
- Need more race/concurrency for budget (double reserve/consume), KPI upsert idempotency under load (GAP-AUTO-002).
- Permission failure reflection in UI for new panels.
- Persistence after refresh for project context + KPI list.
- Full e2e browser for new KPI create + budget reserve/consume flows (current uses unit + api-client).
- Coverage of error states in new UI components.
- Run full matrix on dirty tree before PR.

## Validation Plan
1. make lint (ruff + ts)
2. With DB: pytest apps/api/tests/test_api.py -k "kpi or budget or bootstrap or denial or canonical"
3. pnpm --filter boss-web test -- tests/kpis.test.ts tests/api-client.test.ts tests/accessibility.test.ts
4. make smoke (or subset)
5. make build
6. If docker: full make test
7. Fix any fail, re-run.

**Status**: Good coverage for closed gaps; P2 additions needed for full DoD (concurrency, e2e states). No blocking test holes for core.
**Files**: test files + Makefile.
