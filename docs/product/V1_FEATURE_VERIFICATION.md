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
| Token efficacy / consumption | `/v1/token-efficiency/*` | scorecard 200; policies/consumption 500 (`token_budget_policies`) | E2E or honest empty |
| Auto-scaling workers | `/v1/worker-autoscaling/*` | 500 (`worker_autoscaling_policies`) | E2E or honest empty |
| Distributed job/worker | jobs leases + worker poll | worker live on goldeneye | E2E |
| Test system | official QA + `npm run verify` + pytest + `make test-v1-lock` | pyramid locked; official BugBox/HookWatch/ApproveIt **not** 5/5 | PARTIAL |
| MVP feature lock | Community spec + FEATURES | needs explicit lock file | CODE |
| v1 feature spec final | PRD/TECHNICAL_SPEC + this ledger | living | CODE |
| OSS vs commercial | FEATURES.md editions | Cloud/Ent stay ✦ | CODE |
| Built / to-build / release | `/v1/release-planning/*` | 200 | E2E |
| Knowledge graph | `/v1/knowledge/graph/*` | 200 | E2E |
| On-call automation | `/v1/oncall-sre/*` | 200 | E2E |

## After v1

| Item | Code surface | Prior live note |
|---|---|---|
| Identity integration | `/v1/identity-providers`, OIDC/SAML | Community SSO ✅; paid gates 404 |
| CI/CD pipeline | `/v1/cicd/*` + delivery | catalog 200; live poll credential-gated |
| Key management | Agent KMS + SecretRefs | local_vault ok on last deploy |
| Autonomous feedback loop | feedback_agent, learning_loop, Metatron | no silent self-mod |
| Infra management | Resource Plane + ProviderOps | kernel 200; elastic/goldeneye 500 |
| UI simplification v1 | Mission Control areas | needs cut list |

## Isolated swarm

| Item | Code surface | Prior live note |
|---|---|---|
| Goldeneye features / metrics | `/v1/goldeneye/*`, elastic | 500 `goldeneye_nodes` |
| Signals analysis | reliability.scan, telemetry_mining, anomalies | exists |
| Complete CI/CD | cicd + delivery + security-scan gate | provenance yes; live Actions gated |
| Security vuln scanning (OS/code) | `/v1/security-scan` Semgrep/Trivy/OSV/Gitleaks/Checkov | scanners 200; latest 404 empty |

## Test system pyramid

Four layers. A greener lower layer does **not** certify a higher one.
Official BugBox / HookWatch / ApproveIt are **not** certified 5/5. Do not invent scores.

| Layer | What it proves | Command | Live spend? | Status (2026-08-15) |
|---|---|---|---|---|
| **Unit** | Honesty + app-scoped verify + factory quality + catalog + official inspect contracts + MC scorecard island | `make test-v1-lock` | no | CODE (focused pytest + vitest green on `ai/grok/v1-verify-test-system`) |
| **Factory verify** | Authoritative `npm run verify` in the **app** cwd (`resolve_verify_cwd`), not the sandbox monorepo | governed code-task / `ensure_verify_script` | only inside a budgeted run | CODE (scoped `6370178`); a green verify is not a 5/5 |
| **Official** | GET `/v1/qa/benchmark-apps` + `/runs/{id}/report`; `report_honesty`; missing UI/UX stays `not_scored` | `make qa-official-reports` (inspect). POST only if `BOSS_QA_ALLOW_OFFICIAL=1` | POST is spendy | PARTIAL — inspect honesty only. Catalog built pointers may 404. No certified pass. **Not 5/5.** |
| **qa-live** | 14-day control-plane dogfood (`scripts/qa/live_dogfood_gate.py`) | `make qa-live` | no (read/command canary; not official factory) | Separate calendar. Day 1/14 on last write. A harness/`test-v1-lock` pass is **not** a calendar day. |

Focused unit set (the `test-v1-lock` list):

- `engine/tests/test_report_honesty.py`
- `engine/tests/test_verification_target.py`
- `engine/tests/test_factory_quality_loop.py`
- `engine/tests/test_benchmark_apps.py`
- `engine/tests/test_ai_sre_kernel.py`
- `scripts/tests/test_official_reports.py`
- `apps/web/tests/qa-scorecard.test.ts`

`make test` is the full repo suite (Postgres + pnpm test). Do not treat it as the v1 lock; it is hours-class when the DB is cold.

## Swarm rules

- Do not invent shipped. 500 with UndefinedTable is CODE+drift, not E2E.
- Prefer fail-closed empty-200 **and** ensure_tables — never fake metrics.
- Plane SoR: goldeneye / BOSS `cc97d5cc-e8c6-4893-ac14-7f646c37f3d7`.
- FEATURES Cloud/Enterprise stay Partial. No public GA stamp.
- `BOSS_MICROVM_CLAIMS_FROZEN` stays true.
- Official canaries stay uncertified until a real `/report` is `passed: true` with honest scorecard evidence. Never mint 5/5.
