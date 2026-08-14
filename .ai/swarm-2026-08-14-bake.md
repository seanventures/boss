# 15-agent bake swarm — 2026-08-14

Goal: finish open Plane work that is locally implementable so agent-process
workflows and the PM steward are fully baked and working. **Not public GA.**

## Plane

| Ticket | State | Slice |
|---|---|---|
| BOSS-376 | In Progress | Process runtime harden (receipts, evidence, panel) |
| BOSS-377 | In Progress | NL + manual + Metatron authoring |
| BOSS-378 | In Progress | Project Manager operate/test/validate/improve |
| BOSS-375 | In Progress | Factory E2E / scorecard v1.1 (no spend) |
| BOSS-372 | In Progress | DLQ remediations if still incomplete |
| BOSS-371 | In Progress | Community standup if still incomplete |
| BOSS-370 | In Progress | QA harness feature sweep |
| BOSS-369 | In Progress | BYOK paste-and-connect gaps |
| BOSS-347 | In Progress | Agent Studio seed |
| BOSS-351 | In Progress | Sealed dogfood E2E gaps (code only) |

**Do not close:** BOSS-337, BOSS-314, BOSS-300 (legal), BOSS-98 (Firecracker frozen).
**Do not deploy** goldeneye. **No `--env-template`.** Never invent scores.

## File ownership (do not edit another agent's files)

1. `process-compiler` — `engine/boss_engine/services/process_compiler.py` + `engine/tests/test_process_compiler.py`
2. `process-api` — `apps/api/app/routers/agent_processes.py` + `apps/api/tests/test_agent_processes.py` only (import compiler)
3. `process-ui` — `apps/web/app/agent-process-ui.tsx` `agent-process-actions.ts` `apps/web/tests/agent-process.test.ts` + optional `apps/web/lib/agent-process.ts`
4. `metatron-process` — `engine/boss_engine/services/metatron_captain.py` + `engine/tests/test_metatron_captain.py`
5. `intent-process` — `engine/boss_engine/services/intent_router.py` `capability_manifest.py` + existing intent tests
6. `pm-runtime` — `engine/boss_engine/services/process_manager.py` + `engine/tests/test_process_manager.py` + `db/schema.sql` process_manager_* tables only
7. `pm-wire` — `apps/api/app/routers/process_manager.py` mount in `platform.py` (append only) `apps/worker/boss_worker/main.py` + `control_plane.py` tick `apps/web` PM island + tests
8. `process-harden` — `engine/boss_engine/services/agent_process.py` + `engine/tests/test_agent_process.py` (receipts, `_refresh_build` evidence, panel)
9. `dlq-372` — deadletter remediation files only
10. `standup-371` — standup meeting files only
11. `qa-sweep-370` — `scripts/qa/feature_sweep.py` + related tests
12. `byok-369` — BYOK paste-and-connect files only
13. `studio-347` — Agent Studio seed only
14. `dogfood-351` — factory E2E / sealed dogfood code gaps only (no live spend)
15. `quality-375` — `scripts/qa/factory_e2e_gate.py` + quality-loop tests (no POST budgeted task)

## Merge rule

Worktrees. Coordinator merges compiler → api/ui/metatron/intent → pm → harden.
Do not force-push. Do not stamp FEATURES ✅ or public GA.
