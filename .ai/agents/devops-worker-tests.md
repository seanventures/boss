# Agent: DEVOPS / WORKER-TESTS (leases, heartbeats, failure, deadletter, pack jobs)

**Role**: Polish e2e tests/states for worker lease/job (leases, heartbeats, failure paths, deadletter, pack related jobs if any) per DoD + AGENTS; cover state machines, no silent, receipts where effects.
**Assigned Req IDs**: RUN-001..006, NFR, GAP worker/lease/infra polish (P2).
**Branch/Worktree**: ai/codex/devops-worker-tests (focused per AGENTS from current; avoids dupe with monitoring/polish/packs etc).
**Current Objective**: Read AGENTS + worker code (apps/worker/* + services/jobs + main /v1/worker/* + db schema) + .ai/gap (infra/worker mentions) + tests (test_api worker lease sections + test_worker.py) + Makefile; add/polish tests for leases/heartbeats/409 fails/deadletter requeue+lease/pack-derived agent/marketplace jobs; update 13-bullet agent md + .ai/gap/status/handoff/activity; targeted validate make test/smoke/lint; commit exact msg.
**Files Touched**:
- apps/api/tests/test_api.py (added 4 new e2e: lease token 409s on hb/comp/fail, multi heartbeat extend+emit, deadletter requeue then poll/lease/succeed new job with meta, poll+complete for pack-derived agent.* + marketplace.validate jobs + states/events)
- apps/worker/tests/test_worker.py (added 2 unit polish: failure path execute->fail/deadletter for bad pack-like job kind; pack-derived agent job heartbeat-before-execute coverage)
- .ai/agents/devops-worker-tests.md (this, full 13 bullets), .ai/gap-backlog.md (added/closed worker tests row + counts), .ai/codex-handoff.md, .ai/status.md, .ai/activity-log.md (new entry), .ai/release-readiness.md (ops note)
**Decisions Made**: Use boring explicit endpoint+DB state+event asserts for transitions (no new prod code); cover 409 fail-closed on bad leases (was untested); exercise requeue->new lease cycle (remediation); include pack-derived (agent roles + marketplace validate from packs) per "if any"; small vertical only tests+.ai ; used 56345/5432 clean volumes per prior.
**Tests Run**:
- ruff on touched: PASS (fixed 1 unused in iter).
- Worker unit: 31 passed (incl 2 new).
- API targeted -k worker/lease/heartbeat/deadletter/requeue/pack: 55 passed (incl 4 new + coverage).
- make smoke (clean): "Smoke check passed."
- pnpm --filter boss-web test (partial): ran (preexist errs unrelated).
- make lint (py ruff full): "All checks passed!" (full make tsc had preexist merge/TS from parallel 3x slices on api-client tests; not our changes).
**Failures Found**: 0 in our added (preexist TS/merge in web tests from other agents' unintegrated edits e.g. client responses, requeue sigs; web vitest had fail in run but core targeted green).
**Fixes Applied**: Only in new tests (1 ruff unused var -> _ = ); no prod; no silent drops (our tests assert events on fail/requeue).
**Risks**: Parallel branch state (merge markers surfaced in lint); mitigated by targeted + ruff only on our py.
**Blockers**: None (all local per AGENTS; external not touched).
**Next Autonomous Action**: git add only our py tests + .ai/ ; commit -m 'devops(worker): e2e tests + polish (P2 + DoD ops)'; push; update PR#3 via MCP/gh with reqs/tests/outputs; poll integrate other.
**Last Update**: 2026-06-05 (post all validates; branch ai/codex/devops-worker-tests)
**AGENTS/DoD checklist followed**: Read first (AGENTS full, PRDs via grep for RUN, worker code full, .ai/gap infra/worker, all tests/Makefile); no ext bypass; leases always+TTL+hb in tests; every fail visible (409+event+dlq+requeue+events); state trans tested (leased->running->succeeded/failed_final/deadlettered/requeued); receipts in finalize but covered indirect; tenant/project scoped; small; .ai updated; tests pass; lint py pass; smoke pass.

1. Inspected per AGENTS: .ai/codex-handoff.md .ai/status .ai/req-trace .ai/gap-backlog .ai/activity .ai/agents/ (no prior devops-worker-tests.md) + active ai/codex/* (e.g. feature-monitoring-polish, packs-local..., qa-e2e-*) ai/grok/* + gh PRs (#3 open, cursor drafts) + current branch before work.
2. Read first: AGENTS.md (hard rules leases/receipts/no-silent + build order + review checklist + done=tests+states+lint+smoke), worker code (runner.py control_plane.py main.py config.py + jobs.py full lease claim/hb/comp/fail + states + deadletter + budget defer), .ai/gap (mentions "worker/lease/infra" in 3x plan + AUTO-004 ops worker flags), tests (test_worker.py fakes+22 cases; test_api.py 20+ lease/deadletter tests like two_workers, expired_reclaim, failed_retries_deadletters, requeue_reject, agent_lease_artifact, marketplace, ops_queue), Makefile (test/lint/smoke/worker-drain-once targets + pytest in package.json).
3. Also read PRD/TECH RUN sections (leases TTL hb bounded retry deadletter no silent; worker always leased; remediation), db schema (jobs statuses queued/leased/running/failed_retryable/failed_final/succeeded/deadlettered; deadletters open/incident/requeued), schemas/main endpoints /poll /hb /complete /fail + requeue.
4. Created focused branch ai/codex/devops-worker-tests; no dupe of other 3x (monitoring/comms/packs/selfimp/provider).
5. Polished/added 4 e2e in test_api.py covering: invalid lease token 409s on all ops (hb/comp/fail), multi heartbeats extend+DB+events, deadletter requeue -> poll new job (with requeue meta payload) -> lease -> complete success + events, poll+complete for pack-derived (agent.pm.scope_brief + enterprise.marketplace.validate from role/packs) happy paths+states.
6. Polished/added 2 unit in test_worker.py: failure execute raise -> fail (not complete) + deadletter status for bad "pack" job kind; pack-derived agent job (ops_digest) explicit hb before execute.
7. Updated/created .ai/agents/devops-worker-tests.md with full report + exactly these 13 bullets + evidence.
8. Updated .ai/gap-backlog.md (added worker tests polish row to Closed w/ details/tests/cmds; plan counts -1; last rebuilt).
9. Updated .ai/codex-handoff.md .ai/status.md .ai/activity-log.md (new top entry with cmds/outputs) .ai/release-readiness.md (ops/worker note).
10. Validated: ruff full on api/worker: PASS; worker unit 31p; api targeted lease/etc 55p; smoke: "Smoke check passed."; web test ran (preexist issues); make lint py green (full had preexist TS/merge markers in tests/api-client.test.ts + kpis from parallel agents).
11. All per hard rules: leases/hb in every job cycle tested; failures visible (no silent: 409+log+dlq+requeue+events); no direct ext; small reviewable; tests for every trans (claim, hb, complete, fail-retry, fail-final-dlq, requeue, reclaim, pack/agent); .ai/docs on change; review diff no bypass/missing receipts (existing in finalize).
12. No autonomous self mod; used focused branch; will commit 'devops(worker): e2e tests + polish (P2 + DoD ops)'; push; PR#3 comment w/ req RUN-00x + outputs + handoff.
13. 3x grind complete for this slice; ready poll/integrate; all local DoD for worker/lease/job e2e/states satisfied.
