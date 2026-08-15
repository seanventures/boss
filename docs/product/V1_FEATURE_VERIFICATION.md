# V1 feature verification ledger

Operator list to prove **built end-to-end** and **tracked in Plane**.  
Evidence-only. Missing tables / 404 / dry-run stay ◐. **Not public GA.**

Source request: token efficacy, autoscaling workers, distributed jobs, test
system, MVP lock, v1 spec, OSS vs commercial, built/to-build + release
planning, knowledge graph, on-call (**v1**); then identity, CI/CD, key
management, autonomous feedback, infra management, UI simplification;
isolated: Goldeneye metrics/signals, complete CI/CD, OS/code vuln scanning.

## Status key

| Mark | Meaning |
|---|---|
| E2E | Code + tests + live 2xx on dogfood (or honest empty) |
| CODE | Implemented + tests; live unproven or schema-drift |
| PARTIAL | Surface exists; gap named |
| MISSING | No first-class surface |

## V1 list

| Item | Code surface | Prior live note | Target |
|---|---|---|---|
| Token efficacy / consumption | `/v1/token-efficiency/*` | 2026-08-13: scorecard 200; policies/consumption 500 (`token_budget_policies`). 2026-08-15: CODE — `ensure_tables` + empty-200; live still 500 until redeploy | E2E or honest empty |
| Auto-scaling workers | `/v1/worker-autoscaling/*` | 2026-08-13: 500 (`worker_autoscaling_policies`). 2026-08-15: CODE — `ensure_tables` + empty-200; live still 500 until redeploy | E2E or honest empty |
| Distributed job/worker | jobs leases + worker poll | 2026-08-15: CODE — lease/heartbeat/reclaim/dedupe tests green. Live worker exists; this pass did not re-probe dogfood | E2E |
| Test system | official QA + `npm run verify` + pytest + `make test-v1-lock` | pyramid locked; official BugBox/HookWatch/ApproveIt **not** 5/5 | PARTIAL |
| MVP feature lock | `docs/product/V1_FEATURE_LOCK.md` + `sync-from-features` hook | lock file on `ai/grok/v1-verify-spec-release`; Cloud/Ent stay ✦; **not GA** | CODE |
| v1 feature spec final | V1_FEATURE_LOCK + PRD/TECHNICAL_SPEC + this ledger | Community v1 in / out / CE locked; evidence-only | CODE |
| OSS vs commercial | FEATURES.md ✦ + lock CE rows + release-planning test | CE rows cannot be Community shipped | CODE |
| Built / to-build / release | `/v1/release-planning/{status,board,releases}` | tests in `test_release_planning_api.py`; prior live 200; not re-dogfooded here | CODE |
| Knowledge graph | `/v1/knowledge/graph/*` | 200 prior dogfood; this session pytest only | **CODE** — node/edge/subgraph/known-causes HTTP green; live shared-tag not re-probed; not GA |
| On-call automation | `/v1/oncall-sre/*` | 200 status/pages; notify is dry-run | **CODE** (dry-run ◐) — page/ack/resolve + simulated receipt; force_live 422 without creds; **not** live PagerDuty; not GA |
| Token efficacy / consumption | `/v1/token-efficiency/*` | scorecard 200; policies/consumption 500 (`token_budget_policies`) | E2E or honest empty |
| Auto-scaling workers | `/v1/worker-autoscaling/*` | 500 (`worker_autoscaling_policies`) | E2E or honest empty |
| Distributed job/worker | jobs leases + worker poll | worker live on goldeneye | E2E |
| Test system | `make test-v1-lock` single unit gate (noconftest; skip-if-missing) → factory `npm run verify` → official inspect → `qa-live` | expanded lock on `ai/grok/e2e-test-gate`; official BugBox/HookWatch/ApproveIt **not** 5/5 | PARTIAL |
| MVP feature lock | Community spec + FEATURES | needs explicit lock file | CODE |
| v1 feature spec final | PRD/TECHNICAL_SPEC + this ledger | living | CODE |
| OSS vs commercial | FEATURES.md editions | Cloud/Ent stay ✦ | CODE |
| Built / to-build / release | `/v1/release-planning/*` | 200 | E2E |
| Knowledge graph | `/v1/knowledge/graph/*` | 200 | E2E |
| On-call automation | `/v1/oncall-sre/*` | 200 | E2E |

## After v1

| Item | Code surface | Prior live note | 2026-08-15 verify |
|---|---|---|---|
| Identity integration | `/v1/identity-providers`, OIDC/SAML/SCIM catalog | Community SSO ✅; paid gates 404 | **CODE** (+ catalog E2E). Live Community: `GET /v1/edition?project_id=boss-dogfood` `edition_id=community`, `account_connect_required=false`. Catalog 200, 10 packs, `configured_count=0` (honest empty). Unknown pack 404. Blueprint `apply=true` 422. `GET /v1/account-connect/link` **404** (not hosted Cloud identity). SAML metadata 200; ACS 503 until `BOSS_ENTERPRISE_SAML_ENABLED`; SCIM 401 without bearer. Tests: missing IdP fail-closed (`test_oidc_enabled_without_idp_config_fails_closed`, `test_oidc_enabled_without_provider_config_fails_closed`, SAML ACS no-provider 403, SCIM no-provider 403). **Do not claim hosted Cloud identity. Not public GA.** |
| CI/CD pipeline | `/v1/cicd/*` + delivery | catalog 200; live poll credential-gated | (other slice) |
| Key management | Agent KMS + SecretRefs | local_vault ok on last deploy | **CODE** (local_vault health E2E). Live `GET /v1/kms/health` `local_vault.ok=true`; Vault/OpenBao/platform store honest `ok=false` when unset. SecretRefs 200, 8 items, `sref_6b00a2d6a9424abba8a0` `local_vault`, no `secret_value`. Engine: `test_secretref_consumption` + `test_credential_resolve` (bound-but-broken fail-closed; env last-resort only when no usable ref). No new KMS hole. **Not public GA.** |
| Autonomous feedback loop | feedback_agent, learning_loop, Metatron | no silent self-mod | **CODE** (no-silent-self-mod E2E). Live `POST /v1/metatron/drive auto=false` `status=proposed`, 0 applied; decisions/DLQ `needs_human`. Overwatch `validation.ok=true`, applied 0, "Decisions and deadletters stay human." Factory apply always `open_pr=True` + "Open a PR only / Do not apply to production in place." Preview feedback proposes then requires confirm (`dispatched=false` until confirm). Learning loop `suggest_tuning` only. AGENTS.md: no silent self-mod. **Not public GA.** |
| Infra management | Resource Plane + ProviderOps | kernel 200; elastic/goldeneye 500 | **CODE** `ensure_tables` + honest empty; real provision fail-closed without `BOSS_INFRA_REAL_PROVISION_ALLOWED`; ChangeSet/ResourceReceipt on blocked write. Live 2xx unproven this slice (not E2E). |
| UI simplification v1 | Mission Control areas | needs cut list | **CODE** cut list `docs/product/UI_V1_CUT.md`; Community primary nav = activity/jobs/decisions/reliability/factory+QA/budget/SSO; Cloud/elastic/goldeneye gated or `not_configured` |

## Isolated swarm

| Item | Code surface | Prior live note | This slice (`ai/grok/v1-verify-goldeneye-sec`) |
|---|---|---|---|
| Goldeneye features / metrics | `/v1/goldeneye/*`, elastic | 500 `goldeneye_nodes` | **CODE.** Community = self-hosted kit + optional `/v1/goldeneye/*` (simulated default). Platform-only = managed multi-tenant fleet / hosted metal / Cloud Easy Connect as node source. `GET /status` now `ensure_tables` + honest `not_configured` empty-200; `host.connected=false` and `metrics=null` unless a node actually heartbeated. Never invents Sean's dogfood host. Elastic 500 stays out of this slice. Not GA. |
| Signals analysis | reliability.scan, telemetry_mining, anomalies | exists | **CODE.** Tests: `test_reliability.py` + `test_e2e_reliability_ops.py` (scan), `test_telemetry_mining.py` (mine, no invented scores; `inf` → `None`), `test_anomalies_scan.py` (empty list; retry_storm uses measured deadletter count, no score field). Not GA. |
| Complete CI/CD | cicd + delivery + security-scan gate | provenance yes; live Actions gated | **CODE.** Catalog/generate/validate/provenance E2E in `test_cicd_pipelines.py` + `test_cicd_pipelines_api.py`. Generated templates now include a Semgrep/Trivy/OSV (comments + optional PATH steps) security-scan gate. Live GitHub Actions remains `requires_credentials=true` on `/v1/pipeline-adapters/catalog`. Not GA. |
| Security vuln scanning (OS/code) | `/v1/security-scan` Semgrep/Trivy/OSV/Gitleaks/Checkov | scanners 200; latest 404 empty | **CODE.** OSS only: FS (Trivy) + deps (OSV) + secrets (Gitleaks) + SAST (Semgrep) + IaC (Checkov). `simulated=true` fixtures. Snyk catalog-only fail-closed. Receipts `security_scan:{project}:{run}`. `GET /latest` is honest 200 `not_configured` (no 404). Not a commercial Snyk claim. Not GA. |

## Test system pyramid

Four layers. A greener lower layer does **not** certify a higher one.
Official BugBox / HookWatch / ApproveIt are **not** certified 5/5. Do not invent scores.

| Layer | What it proves | Command | Live spend? | Status (2026-08-15) |
|---|---|---|---|---|
| **Unit** | Honesty + app-scoped verify + factory quality + catalog + official inspect + SRE/token/lock contracts + MC scorecard + edition visibility | `make test-v1-lock` (skip-if-missing; not `make test`) | no | CODE (single gate on `ai/grok/e2e-test-gate`; missing sibling files skipped) |
| **Factory verify** | Authoritative `npm run verify` in the **app** cwd (`resolve_verify_cwd`), not the sandbox monorepo | governed code-task / `ensure_verify_script` | only inside a budgeted run | CODE (scoped `6370178`); a green verify is not a 5/5 |
| **Official** | GET `/v1/qa/benchmark-apps` + `/runs/{id}/report`; `report_honesty`; missing UI/UX stays `not_scored` | `make qa-official-reports` (inspect). POST only if `BOSS_QA_ALLOW_OFFICIAL=1` | POST is spendy | PARTIAL — inspect honesty only. Catalog built pointers may 404. No certified pass. **Not 5/5.** |
| **qa-live** | 14-day control-plane dogfood (`scripts/qa/live_dogfood_gate.py`) | `make qa-live` | no (read/command canary; not official factory) | Separate calendar. Day 1/14 on last write. A harness/`test-v1-lock` pass is **not** a calendar day. |

Focused unit set (`make test-v1-lock`; `--noconftest`; skip if the file is absent on this branch):

Present on `ai/grok/e2e-test-gate`:

- `engine/tests/test_ai_sre_kernel.py`
- `engine/tests/test_verification_target.py`
- `engine/tests/test_report_honesty.py`
- `engine/tests/test_factory_quality_loop.py`
- `engine/tests/test_benchmark_apps.py`
- `scripts/tests/test_official_reports.py`
- `apps/web/tests/qa-scorecard.test.ts`
- `apps/web/tests/edition-visibility.test.ts` (cheap vitest; no browser)

Skipped on this branch (join the gate when they land; Makefile does not fail):

- `test_ai_sre_overwatch.py`
- `test_sre_runbooks.py`
- `test_sre_ask.py`
- `test_sre_bench.py`
- `test_reliability_mcp_tools.py`
- `test_token_worker_fail_closed.py`
- `test_v1_feature_lock.py`

`make test` is the full repo suite (Postgres + pnpm test). Do not treat it as the v1 lock; it is hours-class when the DB is cold.

## Swarm rules

- Do not invent shipped. 500 with UndefinedTable is CODE+drift, not E2E.
- Prefer fail-closed empty-200 **and** ensure_tables — never fake metrics.
- Plane SoR: goldeneye / BOSS `cc97d5cc-e8c6-4893-ac14-7f646c37f3d7`.
- FEATURES Cloud/Enterprise stay Partial. No public GA stamp.
- `BOSS_MICROVM_CLAIMS_FROZEN` stays true.
- Official canaries stay uncertified until a real `/report` is `passed: true` with honest scorecard evidence. Never mint 5/5.

## Verification evidence (2026-08-15) — tokens / workers / jobs

Branch: `ai/grok/v1-verify-tokens-workers` off `ai/grok/v1-feature-verify` (`a5c8951`).
Plane: BOSS-388 (umbrella). **Not public GA. FEATURES not stamped.**

### Token efficacy / consumption — CODE (not E2E)

| | |
|---|---|
| Surface | `engine/boss_engine/services/token_efficiency.py`, `apps/api/app/routers/token_efficiency.py` |
| Harden | `ensure_tables()` `CREATE TABLE IF NOT EXISTS token_budget_policies`; GET policies/consumption/scorecard fail-closed empty-200 when the relation is missing |
| Honesty | Missing `llm_calls` / policies → `calls=0`, `total_tokens=0`, `series=[]`, `cache_ratio=None`, utilization `None`. Defaults are documented caps (`source=default`), never invented usage |
| Tests | `engine/tests/test_token_worker_fail_closed.py` (`test_list_policies_missing_table_returns_defaults_not_raise`, `test_consumption_missing_tables_zeros_never_invented`, `test_scorecard_missing_llm_calls_is_empty_n_a`, `test_ensure_tables_returns_false_when_ddl_cannot_run`); `apps/api/tests/test_token_efficiency_api.py` (`test_ensure_tables_is_idempotent`, `test_unconfigured_policies_http_are_defaults_not_invented`, plus existing scorecard/policies/consumption) |
| Remaining live gap | Dogfood 2026-08-13 still 500 until this branch is deployed so `ensure_tables` can create `token_budget_policies`. Do not mark E2E until GET `/policies` + `/consumption` are 200 (or honest empty) on the shared tag |

### Auto-scaling workers — CODE (not E2E)

| | |
|---|---|
| Surface | `engine/boss_engine/services/worker_autoscaling.py`, `apps/api/app/routers/worker_autoscaling.py` |
| Harden | `ensure_tables()` for `worker_autoscaling_policies` + `worker_autoscaling_history`; GET status/policies/history empty-200 when unconfigured; fabric/`jobs` missing-relation does not invent worker counts |
| Tests | `test_autoscaling_reads_missing_tables_empty_not_raise`; `apps/api/tests/test_worker_autoscaling_api.py` (`test_compute_desired_workers_deterministic`, `test_policy_upsert_evaluate_apply_status_history`, `test_multi_pool_and_cooldown`, `test_apply_is_idempotent_when_no_change`, `test_ensure_tables_and_unconfigured_status_are_honest`) |
| Remaining live gap | Dogfood 2026-08-13 still 500 on `/status` `/policies` `/history` until redeploy. Elastic `worker_instances` 500 is a sibling gap (not claimed closed here) |

### Distributed job / worker — CODE

| | |
|---|---|
| Surface | `engine/boss_engine/services/jobs.py` (`enqueue_job` ON CONFLICT, `claim_job` lease + reclaim expired, `heartbeat`); `apps/worker/boss_worker/runner.py` poll/heartbeat/complete; `POST /v1/jobs` now accepts `dedupe_key` |
| Tests already present | `test_two_workers_cannot_claim_same_job`, `test_expired_lease_can_be_reclaimed`, `test_lease_heartbeat_and_complete_fail_on_invalid_lease_token`, `test_multiple_heartbeats_extend_lease_and_emit_events`, `test_chaos_lease_reclaim_recovers` |
| Hole closed | `apps/api/tests/test_job_lease_distributed.py` — `test_enqueue_job_dedupe_key_is_idempotent`, `test_http_job_create_dedupes_on_dedupe_key` |
| Remaining live gap | Worker process is live on goldeneye; this pass did not SSH-probe poll/heartbeat on the shared tag. Not E2E |

Focused pytest (2026-08-15): 6 engine noconftest + 27 API (token/worker/job lease+dedupe) passed against isolated Postgres.

## KG + on-call evidence (2026-08-15, `ai/grok/v1-verify-kg-oncall`)

Plane **BOSS-390** (child of BOSS-388). Base `a5c8951`. Not public GA. Did not merge `main`.

| Check | Result |
|---|---|
| KG node/edge/subgraph/known-causes | PASS — `test_knowledge_graph.py` + `test_knowledge_graph_api.py` |
| On-call page / ack / resolve | PASS — `test_oncall_sre_api.py` |
| Dry-run notify, no adapter creds | PASS — receipt `succeeded`, `simulated=true`, `posted=false`, `effect_kind=oncall_sre.notify` |
| `force_live` without creds | PASS — HTTP 422 fail-closed |
| Reliability incident → page | PASS — new `test_http_reliability_page_simulated_receipt_and_kg_writeback` (receipt + KG incident/service/`correlates_with`) |
| Focused suite | **21 passed** (`boss_v1_kg_oncall`) |
| Live PagerDuty / Opsgenie page | **Not claimed** — no tokens; dry-run only |
| Live shared-tag 2xx this session | **Not re-probed** (local `/health` 404). Prior note 200 stays historical. |

## Evidence — identity / keys / feedback (2026-08-15)

Branch `ai/grok/v1-verify-identity-keys-feedback` from `a5c8951`. Plane umbrella
[BOSS-388](https://plane.local.sean.network) (In Progress; this slice does not
close the umbrella). Live host `https://boss-api.local.sean.network`.

Focused tests (isolated `boss_idp_test` + engine noconftest):

- Engine 55 passed: `test_oidc.py`, `test_credential_resolve.py`,
  `test_secretref_consumption.py`, `test_metatron_captain.py` (incl. new
  PR-only factory apply), `test_openai_adapter_secretref.py`,
  `test_guest_engine_secretref.py`, `test_improvement_programs.py`.
- API 103 passed: identity catalog 13, edition routers 65, preview feedback 8,
  agent KMS 4, Metatron HTTP 4, learning loop 6, improvement programs 1,
  `test_oidc_enabled_without_provider_config_fails_closed`, SAML runtime
  (missing provider 403), SCIM runtime (missing provider 403).
- Did **not** run API tests against live `boss` DB (deadlock / pollution).

Not public GA. Do not merge to `main` from this verify branch.

## 2026-08-15 — infra + UI v1 (`ai/grok/v1-verify-infra-ui`)

- Plane **BOSS-391** (parent BOSS-388). Not public GA. Do not merge `main`.
- Elastic / Goldeneye / provider-usage: `ensure_tables` + `status=not_configured`
  + `host.connected=false` unless a live heartbeat exists. Never invent connected.
- `InfrastructureService.create_droplet` fail-closes before the adapter when
  `BOSS_INFRA_REAL_PROVISION_ALLOWED` is false; writes Receipt + ResourceReceipt.
- Community home nav cut documented in `UI_V1_CUT.md`. Areas not deleted.
- Tests: `apps/api/tests/test_infra_v1_honesty.py`, edition-visibility v1 cut,
  infra-fabric 500→not_configured. Firecracker stays frozen.

### Isolated HTTP E2E (2026-08-15) — not live dogfood, not public GA

Branch: `ai/grok/e2e-tokens-workers` from `596828a`.
Database: throwaway Postgres `boss_e2e_tokens_workers` (not the shared `boss` DB).
Plane: BOSS-388. FEATURES not stamped. Goldeneye not deployed.

| Item | Isolated HTTP E2E | Result |
|---|---|---|
| Token GETs after DROP `token_budget_policies` | `/scorecard` `/policies` `/consumption` | 200 empty/defaults; `ensure_tables` recreates |
| PUT policy then GET consumption | no `llm_calls` rows | `calls=0` `total_tokens=0` `cost_usd_micros=0`; stored caps only |
| Worker GETs empty + missing tables | `/status` `/policies` `/history` | 200 empty |
| Queue-driven evaluate | PUT policy, enqueue 5 jobs, POST `/evaluate` | `needed_workers=3`; `worker_instances=0`; no apply/capacity events while `BOSS_INFRA_REAL_PROVISION_ALLOWED` is false |
| Jobs | POST same `dedupe_key` twice; two polls; heartbeat; expire+reclaim | one job; exclusive lease; heartbeat 200; reclaim `attempt=2` + `job.lease_reclaimed` even after heartbeat flipped status to `running` |

34 focused tests passed (new E2E file + prior token/worker/lease tests). Live dogfood still unproven until this tag is deployed. **Not public GA.**
