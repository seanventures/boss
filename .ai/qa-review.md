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
- (pre) Need more race/concurrency for budget (double reserve/consume), KPI upsert idempotency under load (GAP-AUTO-002).
- (pre) Permission failure reflection in UI for new panels.
- (pre) Persistence after refresh for project context + KPI list.
- Full e2e browser for new KPI create + budget reserve/consume flows (current uses unit + api-client).
- Coverage of error states in new UI components.
- Run full matrix on dirty tree before PR.

**GAP-AUTO-002 addressed this session (P2)**: Added 7+ targeted tests exercising:
  - concurrent budget reserves (distinct cats, real DB+threads)
  - concurrent consume on same res (double-submit path)
  - concurrent/double KPI upsert same key (idempotent via ON CONFLICT + events)
  - 403 perm failure reflected as apiReachable=false + blockedReason="API returned 403" in kpis + budget-status feeds
  - project context + KPI list persistence simulated via re-fetch (refresh/reload)
  - client contract 403 paths for kpi/budget
- All pass; extended existing files only.

## Validation Plan
1. make lint (ruff + ts) - PASS
2. With DB: pytest apps/api/tests/test_api.py -k "kpi or budget or bootstrap or denial or canonical" (+new concurrent) - 27 passed
3. pnpm --filter boss-web test -- tests/kpis.test.ts tests/api-client.test.ts tests/budget-status.test.ts - 39 passed (kpis 5, budget-status 3, api-client 31)
4. make smoke (or subset) - skipped port
5. make build / tsc - PASS
6. If docker: full make test
7. Fix any fail, re-run. (conftest.py minimally extended for tolerant clean to support ephemeral schema-only postgres; test-only file)

**Status**: Good coverage for closed gaps + GAP-AUTO-002 now has explicit race/perm/persist tests (all GREEN). Remaining P2: full browser e2e, more UX states (see GAP-AUTO-001). No blocking test holes.
**Files**: test files + Makefile + .ai/ (qa-test, qa-review, etc).
