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
| Knowledge graph | `/v1/knowledge/graph/*` | 200 prior dogfood; this session pytest only | **CODE** — node/edge/subgraph/known-causes HTTP green; live shared-tag not re-probed; not GA |
| On-call automation | `/v1/oncall-sre/*` | 200 status/pages; notify is dry-run | **CODE** (dry-run ◐) — page/ack/resolve + simulated receipt; force_live 422 without creds; **not** live PagerDuty; not GA |

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

## Swarm rules

- Do not invent shipped. 500 with UndefinedTable is CODE+drift, not E2E.
- Prefer fail-closed empty-200 **and** ensure_tables — never fake metrics.
- Plane SoR: goldeneye / BOSS `cc97d5cc-e8c6-4893-ac14-7f646c37f3d7`.
- FEATURES Cloud/Enterprise stay Partial. No public GA stamp.
- `BOSS_MICROVM_CLAIMS_FROZEN` stays true.

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
