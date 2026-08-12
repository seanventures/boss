# Live QA memory (do not forget)

Last deep campaign: 2026-08-11/12. Plane **BOSS-337**. Report: `docs/operations/LIVE_QA_2026-08-11.md`.

## Settled deploy truth
- App: `boss-app` on goldeneye → `boss.local.sean.network` + `boss-api.local.sean.network`
- Images: api/web `view2`, worker `norebuild` (skew)
- Edition: **community**
- Health OK (DB/GitHub/OpenAI); worker idle polling OK

## Settled P0
- ProviderOps enterprise-gated; community omits `/v1/providers` etc.
- Web still SSR-fans ProviderOps on **every** area → permanent `apiReachable=false` banner class of bug
- Same failure class as delivery edition-split tests warn about

## Settled P1
- 39 open deadletters (mostly code.task): max_turns, TPM, credits, timeouts, allowlist, missing git in worker, code-tasks/step 500, NoneType
- Worker lacks `git`; api has it

## Settled P2
- SSR ~6.6MB / ~5s; OpenAPI version 0.2.1 vs product 0.11.0; reliability incidents open

## Next mandate
Much deeper E2E functional audit of UI panels/features that look baked but don't work. Catalog half-baked surfaces with evidence.

## Deep UI functional audit (2026-08-12 follow-up)

Full catalog: `docs/operations/LIVE_UI_FUNCTIONAL_AUDIT_2026-08-11.md`

### Additional settled findings
- **Self-Improvement UI DEAD**: `/v1/self-improvement/*` 404; live has only `/v1/improvement-programs/*`.
- **Sandboxes host down**: connection refused from API; empty sandboxes.
- **Previews**: many URLs are `http://127.0.0.1:*` — operator-unreachable.
- **GitHub MC SHELL**: 0 connections/bindings despite github_configured for factory token.
- **CI/CD SHELL**: 0 deployment targets; observability not deployed.
- **Agent Studio SHELL**: empty agents/panels; A2A degraded.
- **Enterprise/Account Connect DEAD** on community (expected edition) but UI still shown.
- **React hydration #418** on work, code_tasks, shepherd, reports, pipeline, org.
- **Works**: metatron, command propose, agent chat, budget, canary/eval/anomaly/finops actions.
- **Code tasks FLAKY**: high DLQ; worker norebuild + no git.

### Mandate
Do not treat Mission Control as “done.” Track bake-out backlog in BOSS-337 until P0/P1 closed or accepted.

## PRD critical gaps (2026-08-12)

Six parallel agents synthesized → `docs/operations/PRD_CRITICAL_GAPS_2026-08-12.md`.

**Remember:** Kernel ≠ closed loop. Top critical: factory dogfood reliability, orchestration glue (next_move), ProviderOps/SIM community packaging vs UI, sandboxes/previews, GitHub MC wiring, earned autonomy G1–G3. Do not add more panels before closing dogfood loops + edition honesty.

## Plane system of record (mandatory)

All live QA / PRD gap work is tracked in **goldeneye Plane** project BOSS.

- Umbrella: **BOSS-337** (In Progress)
- Children: **BOSS-338–351** (see `docs/operations/PLANE_TRACKING_LIVE_QA.md`)
- Always dedupe against Plane before creating issues; update issue state with work.

## Product decision — community API keys (2026-08-12)

**Operator decision:** Community edition **must** support third-party API keys and infra API keys.

**Code (branch `ai/grok/community-provider-keys`):**
- Community mounts: `provider-definitions`, `providers` (test/rotate/…), `resources`, `resource-receipts`
- Already community: `entitlements/sources`, `secret-refs`, env model keys
- Remain paid: changesets, worker-pools, autoscaler, resource-janitor, self-improvement, account-connect, marketplace, SSO

Plane: BOSS-350 (decision), BOSS-338 (related UI). Deploy new api image before goldeneye reflects this.

