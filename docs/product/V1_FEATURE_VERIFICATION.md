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
| Test system | official QA + `npm run verify` + pytest | factory verify scoped `6370178` | PARTIAL until official green |
| MVP feature lock | Community spec + FEATURES | needs explicit lock file | CODE |
| v1 feature spec final | PRD/TECHNICAL_SPEC + this ledger | living | CODE |
| OSS vs commercial | FEATURES.md editions | Cloud/Ent stay ✦ | CODE |
| Built / to-build / release | `/v1/release-planning/*` | 200 | E2E |
| Knowledge graph | `/v1/knowledge/graph/*` | 200 | E2E |
| On-call automation | `/v1/oncall-sre/*` | 200 | E2E |

## After v1

| Item | Code surface | Prior live note | 2026-08-15 verify (`ai/grok/v1-verify-identity-keys-feedback`) |
|---|---|---|---|
| Identity integration | `/v1/identity-providers`, OIDC/SAML/SCIM catalog | Community SSO ✅; paid gates 404 | **CODE** (+ catalog E2E). Live Community: `GET /v1/edition?project_id=boss-dogfood` `edition_id=community`, `account_connect_required=false`. Catalog 200, 10 packs, `configured_count=0` (honest empty). Unknown pack 404. Blueprint `apply=true` 422. `GET /v1/account-connect/link` **404** (not hosted Cloud identity). SAML metadata 200; ACS 503 until `BOSS_ENTERPRISE_SAML_ENABLED`; SCIM 401 without bearer. Tests: missing IdP fail-closed (`test_oidc_enabled_without_idp_config_fails_closed`, `test_oidc_enabled_without_provider_config_fails_closed`, SAML ACS no-provider 403, SCIM no-provider 403). **Do not claim hosted Cloud identity. Not public GA.** |
| CI/CD pipeline | `/v1/cicd/*` + delivery | catalog 200; live poll credential-gated | (other slice) |
| Key management | Agent KMS + SecretRefs | local_vault ok on last deploy | **CODE** (local_vault health E2E). Live `GET /v1/kms/health` `local_vault.ok=true`; Vault/OpenBao/platform store honest `ok=false` when unset. SecretRefs 200, 8 items, `sref_6b00a2d6a9424abba8a0` `local_vault`, no `secret_value`. Engine: `test_secretref_consumption` + `test_credential_resolve` (bound-but-broken fail-closed; env last-resort only when no usable ref). No new KMS hole. **Not public GA.** |
| Autonomous feedback loop | feedback_agent, learning_loop, Metatron | no silent self-mod | **CODE** (no-silent-self-mod E2E). Live `POST /v1/metatron/drive auto=false` `status=proposed`, 0 applied; decisions/DLQ `needs_human`. Overwatch `validation.ok=true`, applied 0, "Decisions and deadletters stay human." Factory apply always `open_pr=True` + "Open a PR only / Do not apply to production in place." Preview feedback proposes then requires confirm (`dispatched=false` until confirm). Learning loop `suggest_tuning` only. AGENTS.md: no silent self-mod. **Not public GA.** |
| Infra management | Resource Plane + ProviderOps | kernel 200; elastic/goldeneye 500 | (other slice) |
| UI simplification v1 | Mission Control areas | needs cut list | (other slice) |

## Isolated swarm

| Item | Code surface | Prior live note |
|---|---|---|
| Goldeneye features / metrics | `/v1/goldeneye/*`, elastic | 500 `goldeneye_nodes` |
| Signals analysis | reliability.scan, telemetry_mining, anomalies | exists |
| Complete CI/CD | cicd + delivery + security-scan gate | provenance yes; live Actions gated |
| Security vuln scanning (OS/code) | `/v1/security-scan` Semgrep/Trivy/OSV/Gitleaks/Checkov | scanners 200; latest 404 empty |

## Swarm rules

- Do not invent shipped. 500 with UndefinedTable is CODE+drift, not E2E.
- Prefer fail-closed empty-200 **and** ensure_tables — never fake metrics.
- Plane SoR: goldeneye / BOSS `cc97d5cc-e8c6-4893-ac14-7f646c37f3d7`.
- FEATURES Cloud/Enterprise stay Partial. No public GA stamp.
- `BOSS_MICROVM_CLAIMS_FROZEN` stays true.

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
