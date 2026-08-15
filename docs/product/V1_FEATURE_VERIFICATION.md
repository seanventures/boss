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
| Infra management | Resource Plane + ProviderOps | kernel 200; elastic/goldeneye 500 |
| UI simplification v1 | Mission Control areas | needs cut list |

## Isolated swarm

| Item | Code surface | Prior live note | This slice (`ai/grok/v1-verify-goldeneye-sec`) |
|---|---|---|---|
| Goldeneye features / metrics | `/v1/goldeneye/*`, elastic | 500 `goldeneye_nodes` | **CODE.** Community = self-hosted kit + optional `/v1/goldeneye/*` (simulated default). Platform-only = managed multi-tenant fleet / hosted metal / Cloud Easy Connect as node source. `GET /status` now `ensure_tables` + honest `not_configured` empty-200; `host.connected=false` and `metrics=null` unless a node actually heartbeated. Never invents Sean's dogfood host. Elastic 500 stays out of this slice. Not GA. |
| Signals analysis | reliability.scan, telemetry_mining, anomalies | exists | **CODE.** Tests: `test_reliability.py` + `test_e2e_reliability_ops.py` (scan), `test_telemetry_mining.py` (mine, no invented scores; `inf` → `None`), `test_anomalies_scan.py` (empty list; retry_storm uses measured deadletter count, no score field). Not GA. |
| Complete CI/CD | cicd + delivery + security-scan gate | provenance yes; live Actions gated | **CODE.** Catalog/generate/validate/provenance E2E in `test_cicd_pipelines.py` + `test_cicd_pipelines_api.py`. Generated templates now include a Semgrep/Trivy/OSV (comments + optional PATH steps) security-scan gate. Live GitHub Actions remains `requires_credentials=true` on `/v1/pipeline-adapters/catalog`. Not GA. |
| Security vuln scanning (OS/code) | `/v1/security-scan` Semgrep/Trivy/OSV/Gitleaks/Checkov | scanners 200; latest 404 empty | **CODE.** OSS only: FS (Trivy) + deps (OSV) + secrets (Gitleaks) + SAST (Semgrep) + IaC (Checkov). `simulated=true` fixtures. Snyk catalog-only fail-closed. Receipts `security_scan:{project}:{run}`. `GET /latest` is honest 200 `not_configured` (no 404). Not a commercial Snyk claim. Not GA. |

## Swarm rules

- Do not invent shipped. 500 with UndefinedTable is CODE+drift, not E2E.
- Prefer fail-closed empty-200 **and** ensure_tables — never fake metrics.
- Plane SoR: goldeneye / BOSS `cc97d5cc-e8c6-4893-ac14-7f646c37f3d7`.
- FEATURES Cloud/Enterprise stay Partial. No public GA stamp.
- `BOSS_MICROVM_CLAIMS_FROZEN` stays true.
