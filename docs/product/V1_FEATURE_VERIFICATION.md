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

| Item | Code surface | Prior live note |
|---|---|---|
| Identity integration | `/v1/identity-providers`, OIDC/SAML | Community SSO ✅; paid gates 404 |
| CI/CD pipeline | `/v1/cicd/*` + delivery | catalog 200; live poll credential-gated |
| Key management | Agent KMS + SecretRefs | local_vault ok on last deploy |
| Autonomous feedback loop | feedback_agent, learning_loop, Metatron | no silent self-mod |
| Infra management | Resource Plane + ProviderOps | Isolated **E2E**: empty GET `/v1/elastic/status`, `/v1/goldeneye/status`, `/v1/provider-usage/summary` → 200 `not_configured`, `host.connected=false`, no invented metrics. `POST /v1/infra/droplets` without `BOSS_INFRA_REAL_PROVISION_ALLOWED` → blocked + failed Receipt + ResourceReceipt. Live dogfood 2xx still unproven. Not public GA. |
| UI simplification v1 | Mission Control areas | Isolated **E2E**: `docs/product/UI_V1_CUT.md`; Community primary nav hides Cloud Easy Connect; `AreaId` still includes hidden areas. Not public GA. |

## Isolated swarm

| Item | Code surface | Prior live note |
|---|---|---|
| Goldeneye features / metrics | `/v1/goldeneye/*`, elastic | Isolated E2E 200 `not_configured` + `host.connected=false` after `ensure_tables` |
| Signals analysis | reliability.scan, telemetry_mining, anomalies | exists |
| Complete CI/CD | cicd + delivery + security-scan gate | provenance yes; live Actions gated |
| Security vuln scanning (OS/code) | `/v1/security-scan` Semgrep/Trivy/OSV/Gitleaks/Checkov | scanners 200; latest 404 empty |

## Swarm rules

- Do not invent shipped. 500 with UndefinedTable is CODE+drift, not E2E.
- Prefer fail-closed empty-200 **and** ensure_tables — never fake metrics.
- Plane SoR: goldeneye / BOSS `cc97d5cc-e8c6-4893-ac14-7f646c37f3d7`.
- FEATURES Cloud/Enterprise stay Partial. No public GA stamp.
- `BOSS_MICROVM_CLAIMS_FROZEN` stays true.

## 2026-08-15 — infra + UI v1 E2E (`ai/grok/e2e-infra-ui`)

- Plane **BOSS-391** (parent BOSS-388). Not public GA. Do not merge `main`.
- Isolated HTTP E2E: empty + missing-schema GETs stay 200 `not_configured`.
- Isolated HTTP E2E: `POST /v1/infra/droplets` fail-closes without the provision
  flag and records a failed Receipt + ResourceReceipt.
- Web: Community nav does not show Cloud Easy Connect un-gated; `AreaId` still
  includes hidden areas. Firecracker stays frozen.

## 2026-08-15 — infra + UI v1 (`ai/grok/v1-verify-infra-ui`)

- Plane **BOSS-391** (parent BOSS-388). Not public GA. Do not merge `main`.
- Elastic / Goldeneye / provider-usage: `ensure_tables` + `status=not_configured`
  + `host.connected=false` unless a live heartbeat exists. Never invent connected.
- `InfrastructureService.create_droplet` fail-closes before the adapter when
  `BOSS_INFRA_REAL_PROVISION_ALLOWED` is false; writes Receipt + ResourceReceipt.
- Community home nav cut documented in `UI_V1_CUT.md`. Areas not deleted.
- Tests: `apps/api/tests/test_infra_v1_honesty.py`, edition-visibility v1 cut,
  infra-fabric 500→not_configured. Firecracker stays frozen.
