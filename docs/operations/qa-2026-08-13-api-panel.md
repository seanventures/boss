# QA Agent 2 — API / settings / functions panel

**Date:** 2026-08-13  
**Plane:** [BOSS-337](https://plane.local.sean.network) (In Progress; no new issue — umbrella QA)  
**Environment:** goldeneye dogfood (`boss-app`), **community** edition  
**API:** `https://boss-api.local.sean.network`  
**Project:** `boss-dogfood` / org `org_boss` / workspace `ws_boss`  
**Auth:** `BOSS_REQUIRE_API_AUTH=true`; key from `/srv/apps/boss-app/.env` via SSH (never printed)  
**Images:** api/web/worker **shared tag** `boss-*:3e6c906`  
**Method:** list/status GETs + safe POSTs (`/v1/deadletters/classify` with `escalate_model=false`, `/v1/intent/classify`). **Never** apply ChangeSet, real provision, `open_pr`, or live model `code.task`.  
**Artifact:** `/tmp/boss-qa-api-panel.json` (114 probes)

---

## Verdict

**YELLOW.** Core control-plane reads work (health, auth fail-closed, tasks/jobs/budget/decisions/events/receipts/deadletters). Seven live **500s** share one root cause: `ensure_schema` aborted on missing `kg_nodes.valid_from`, so later tables (`token_budget_policies`, `worker_instances`, `worker_autoscaling_*`) were never created.

Prefix-only 404s (`/v1/commands`, `/v1/decisions`, `/v1/learning`, …) are **not incomplete** — child list/status routes exist. Enterprise SAML metadata returned 200 (not 4xx). SCIM Users returned expected 401.

**Composite grade: YELLOW** — service exists; schema apply on live is incomplete.

---

## Deploy / runtime truth

| Item | Value |
|---|---|
| Edition | `community` (`GET /v1/edition`) |
| Auth | unauth `/v1/projects` → **401**; session principal `ceo` |
| Budget | `green`; cap $129; actual ~$39.23; available ~$89.77 |
| Deadletters | **41 open** — classify: model 17 / other 13 / sandbox 9 / git 1 / github 1 |
| Factory WIP | in_flight=0 queued=0 limit=5 admit=true |
| Features catalog | `count=0` (empty, not 404) |
| LLM ledger | 960 calls listed; model-gateway usage 561/30d |
| OpenAPI catalog | 791 live routes, 742 documented, coverage **0.938** |
| `ensure_schema` | **skipped** at boot: `column "valid_from" does not exist` |

---

## Table

Verdict key: **PASS** = expected 2xx/4xx; **WARN** = prefix-only 404 or empty-but-honest; **FAIL** = 5xx.

Incomplete? **Y** only when the service is mounted but the live route 500s (or a documented list/status is missing while siblings exist for a *different* reason than “no index”).

| Path | Method | Status | Verdict | Incomplete? | Notes |
|---|---|---|---|---|---|
| `/health` | GET | 200 | PASS | N | `status=ok`; DB/GitHub/OpenAI configured. Canonical health (not `/v1/health`). |
| `/v1/health` | GET | 404 | WARN | N | No `/v1/health` index. Use `/health`. Not paid-gated. |
| `/v1/auth/session` | GET | 200 | PASS | N | `auth_required=true`, transport OK, principal `ceo`. |
| `/v1/auth/session` | GET (unauth) | 401 | PASS | N | Fail-closed. |
| `/v1/projects` | GET | 200 | PASS | N | `boss-dogfood` present. |
| `/v1/projects` | GET (unauth) | 401 | PASS | N | Fail-closed. |
| `/v1/edition` | GET | 200 | PASS | N | `edition_id=community`; local auth allowed. |
| `/v1/commands` | GET | 404 | WARN | N | No list index. Write path is `POST /v1/commands/propose` only. |
| `/v1/commands/propose` | GET | 405 | PASS | N | POST-only. Not exercised (would create work). |
| `/v1/tasks` | GET | 200 | PASS | N | List live; needs `project_id`. |
| `/v1/jobs` | GET | 200 | PASS | N | List live; needs `project_id`. |
| `/v1/budget/status` | GET | 422 | PASS | N | Missing `scope_type`+`scope_id` (expected). |
| `/v1/budget/status` | GET | 200 | PASS | N | `scope_type=project` → **green**. |
| `/v1/decisions` | GET | 404 | WARN | N | No inbox-less index. Use `/v1/decisions/inbox`. |
| `/v1/decisions/inbox` | GET | 200 | PASS | N | Inbox populated (incl. reservation questions). |
| `/v1/events` | GET | 200 | PASS | N | Event feed live. |
| `/v1/receipts` | GET | 200 | PASS | N | Receipts live (dedupe keys present). |
| `/v1/deadletters` | GET | 200 | PASS | N | 41 open. Visible, not silent. |
| `/v1/deadletters/classify` | POST | 200 | PASS | N | Safe classify (`escalate_model=false`). Buckets: model/sandbox/other/git/github. |
| `/v1/features` | GET | 200 | PASS | N | Route complete; **catalog empty** (`count=0`). Seed already done — residual data gap. |
| `/v1/capability-manifest` | GET | 200 | PASS | N | Manifest JSON. |
| `/v1/capability-manifest/table` | GET | 200 | PASS | N | Text table. |
| `/v1/knowledge/graph` | GET | 404 | WARN | N | No graph index. Children exist. |
| `/v1/knowledge/status` | GET | 200 | PASS | N | Knowledge plane status. |
| `/v1/knowledge/graph/nodes` | GET | 200 | PASS | N | Nodes present (service-level extras added `valid_from`). |
| `/v1/knowledge/graph/edges` | GET | 405 | PASS | N | POST-only upsert. |
| `/v1/knowledge/graph/health` | GET | 200 | PASS | N | `healthy=true`; 2 nodes / 1 edge. |
| `/v1/learning` | GET | 404 | WARN | N | No index. Use `/corpus` + `/evaluate`. |
| `/v1/learning/corpus` | GET | 200 | PASS | N | Empty corpus for `agent=ceo`. |
| `/v1/learning/evaluate` | GET | 200 | PASS | N | `total=0`. |
| `/v1/intent` | GET | 404 | WARN | N | No index. Classify is POST. |
| `/v1/intent/classify` | POST | 200 | PASS | N | Safe classify of a budget-status phrase. |
| `/v1/llm-calls` | GET | 200 | PASS | N | 20/960 rows. |
| `/v1/llm-calls/summary` | GET | 200 | PASS | N | Stage rollup. |
| `/v1/token-efficiency` | GET | 404 | WARN | N | No index. Scorecard/routing work. |
| `/v1/token-efficiency/scorecard` | GET | 200 | PASS | N | Reads `llm_calls` only (table exists). |
| `/v1/token-efficiency/policies` | GET | **500** | FAIL | **Y** | `UndefinedTable token_budget_policies`. Service mounted. |
| `/v1/token-efficiency/consumption` | GET | **500** | FAIL | **Y** | Same missing table. |
| `/v1/token-efficiency/routing` | GET | 200 | PASS | N | Profile routing; no DB table. |
| `/v1/kms/backends` | GET | 200 | PASS | N | Catalog (local_vault, vault, aws_kms, gcp_kms, azure_kv). |
| `/v1/kms/health` | GET | 200 | PASS | N | Backends **not configured** (fail-closed `ok=false`, no secrets leaked). |
| `/v1/kms/keys` | GET | 422 | PASS | N | Requires `project_id`+`agent_id`. |
| `/v1/kms/keys` | GET | 200 | PASS | N | List only; `count=0`. No material returned. |
| `/v1/identity-providers` | GET | 200 | PASS | N | Catalog of 10 IdPs. |
| `/v1/identity-providers/project-status` | GET | 200 | PASS | N | `configured_count=0`. |
| `/v1/enterprise/identity-providers` | GET | 200 | PASS | N | Empty list (not 404/403 on community). |
| `/saml/v2/{project}/metadata` | GET | 200 | PASS | N | SP metadata XML (not 4xx). ACS still gated by `BOSS_ENTERPRISE_SAML_ENABLED`. |
| `/scim/v2/{project}/Users` | GET | 401 | PASS | N | Expected: enterprise SCIM bearer required. |
| `/v1/enterprise/scim/users` | GET | 200 | PASS | N | Empty list. |
| `/v1/enterprise/scim/groups` | GET | 200 | PASS | N | Empty list. |
| `/v1/reliability` | GET | 404 | WARN | N | No index. Children work. |
| `/v1/reliability/incidents` | GET | 200 | PASS | N | Incidents listed. |
| `/v1/reliability/sources` | GET | 200 | PASS | N | Empty sources. |
| `/v1/reliability/policy` | GET | 200 | PASS | N | Policy present. |
| `/v1/reliability/graph` | GET | 422 → 200 | PASS | N | Needs `service=`; with `service=api` → empty graph 200. |
| `/v1/reliability/learning` | GET | 200 | PASS | N | 1 confirmed SRE example. |
| `/v1/telemetry` | GET | 404 | WARN | N | No index. Catalog is the list surface. |
| `/v1/telemetry/connectors/catalog` | GET | 200 | PASS | N | prometheus/loki/grafana/… kinds. |
| `/v1/finops/summary` | GET | 200 | PASS | N | 30-day FinOps payload. |
| `/v1/costs` | GET | 200 | PASS | N | Empty cost events (`items=[]`). |
| `/v1/work-trackers` | GET | 200 | PASS | N | Plane default **active**. |
| `/v1/work-trackers/catalog` | GET | 200 | PASS | N | Plane in catalog. |
| `/v1/work-orchestration` | GET | 404 | WARN | N | No index. Overview exists. |
| `/v1/work-orchestration/overview` | GET | 200 | PASS | N | Overview live. |
| `/v1/work-orchestration/items` | GET | 405 | PASS | N | POST-only upsert. |
| `/v1/release-planning` | GET | 404 | WARN | N | No index. |
| `/v1/release-planning/status` | GET | 200 | PASS | N | Status snapshot. |
| `/v1/release-planning/board` | GET | 200 | PASS | N | Planned column has live tasks. |
| `/v1/release-planning/releases` | GET | 200 | PASS | N | `count=0`. |
| `/v1/work-plane` | GET | 404 | WARN | N | No index. |
| `/v1/work-plane/status` | GET | 200 | PASS | N | Needs `project_id`. |
| `/v1/work-plane/board` | GET | 200 | PASS | N | Board live. |
| `/v1/work-plane/overview` | GET | 200 | PASS | N | Empty initiatives/modules/cycles. |
| `/v1/work-plane/initiatives` | GET | 200 | PASS | N | `count=0`. |
| `/v1/factory/wip` | GET | 200 | PASS | N | admit=true; `fixture_supported=true`. |
| `/v1/code-tasks` | GET | 200 | PASS | N | Runs listed (incl. blocked/awaiting_plan). No live model step posted. |
| `/v1/previews` | GET | 200 | PASS | N | Preview rows present. |
| `/v1/sandboxes` | GET | 200 | PASS | N | List live. |
| `/v1/sandboxes/helpers` | GET | 422 | PASS | N | Fail-closed: sandbox host task missing (not 500). |
| `/v1/agent-studio` | GET | 404 | WARN | N | No index. Catalog/agents/panels work. |
| `/v1/agent-studio/catalog` | GET | 200 | PASS | N | Seeded catalog (backend_engineer, …). |
| `/v1/agent-studio/agents` | GET | 200 | PASS | N | Agents listed. |
| `/v1/agent-studio/panels` | GET | 200 | PASS | N | Panels listed. |
| `/v1/skills` | GET | 200 | PASS | N | Skill registry populated. |
| `/v1/skills/invocations` | GET | 200 | PASS | N | Invocations present (mock mode). |
| `/v1/model-gateway/usage` | GET | 200 | PASS | N | 30-day totals (561 calls). |
| `/v1/model-gateway/leases` | GET | 200 | PASS | N | `items=[]`. |
| `/v1/elastic` | GET | 404 | WARN | N | No index. Status/instances are the surface. |
| `/v1/elastic/status` | GET | **500** | FAIL | **Y** | `UndefinedTable worker_instances`. |
| `/v1/elastic/instances` | GET | **500** | FAIL | **Y** | Same. |
| `/v1/worker-autoscaling` | GET | 404 | WARN | N | No index. |
| `/v1/worker-autoscaling/status` | GET | **500** | FAIL | **Y** | `UndefinedTable worker_autoscaling_policies`. |
| `/v1/worker-autoscaling/policies` | GET | **500** | FAIL | **Y** | Same. |
| `/v1/worker-autoscaling/history` | GET | **500** | FAIL | **Y** | `UndefinedTable worker_autoscaling_history`. |
| `/v1/provider-conformance` | GET | 200 | PASS | N | Simulated PRV-10 report (`real_mode=false`). |
| `/v1/concurrency` | GET | 404 | WARN | N | No index. |
| `/v1/concurrency/status` | GET | 422 | PASS | N | Missing `project_id`. Follow-up with param hit 503 `InFailedSqlTransaction` (aborted txn after 500s) — not a missing route. |
| `/v1/concurrency/policy` | GET | 200 | PASS | N | Policy + specpack alias. |
| `/v1/safety/branch-protection` | GET | 200 | PASS | N | Live GitHub 403 on `paramg/boss-sandbox` protection (plan/auth); simulated accept on. |
| `/v1/safety/branch-protection/checklist` | GET | 200 | PASS | N | Dry-run checklist. |
| `/v1/cicd/catalog` | GET | 200 | PASS | N | github_actions + others. |
| `/v1/cicd/status` | GET | 200 | PASS | N | Status snapshot. |
| `/v1/cicd/runs` | GET | 200 | PASS | N | `count=0`. |
| `/v1/security-scan/scanners` | GET | 200 | PASS | N | semgrep/trivy/osv catalog. |
| `/v1/security-scan/runs` | GET | 200 | PASS | N | `count=0`. |
| `/v1/oncall-sre/status` | GET | 200 | PASS | N | 0 open pages; adapters not live-capable. |
| `/v1/oncall-sre/schedules` | GET | 200 | PASS | N | Empty. |
| `/v1/oncall-sre/policies` | GET | 200 | PASS | N | Empty. |
| `/v1/oncall-sre/pages` | GET | 200 | PASS | N | Empty. |
| `/v1/oncall-sre/adapters` | GET | 200 | PASS | N | pagerduty/opsgenie/slack env-gated. |
| `/v1/pipeline-adapters` | GET | 404 | WARN | N | No index. Catalog exists. |
| `/v1/pipeline-adapters/catalog` | GET | 200 | PASS | N | Adapter kinds; no secrets. |
| `/v1/meta/api-catalog` | GET | 200 | PASS | N | 17 domains / 791 routes. |
| `/v1/meta/openapi-coverage` | GET | 200 | PASS | N | 45 live routes missing from OpenAPI (incl. `POST /v1/deadletters/classify`). |

---

## Ranked incomplete APIs

Service exists (router mounted on live `3e6c906`) but the GET 500s because `ensure_schema` never created the table.

| Rank | Surface | Live 500 | Missing relation | Why incomplete |
|---|---|---|---|---|
| 1 | Token efficiency policies | `GET /v1/token-efficiency/policies` | `token_budget_policies` | Scorecard/routing work; policies/consumption crash. |
| 1 | Token efficiency consumption | `GET /v1/token-efficiency/consumption` | `token_budget_policies` | Same table. |
| 2 | Elastic fabric status | `GET /v1/elastic/status` | `worker_instances` | ProviderOps elastic reads unusable. |
| 2 | Elastic instances | `GET /v1/elastic/instances` | `worker_instances` | Same. |
| 3 | Worker autoscaling status | `GET /v1/worker-autoscaling/status` | `worker_autoscaling_policies` | Control-plane status unusable. |
| 3 | Worker autoscaling policies | `GET /v1/worker-autoscaling/policies` | `worker_autoscaling_policies` | Same. |
| 3 | Worker autoscaling history | `GET /v1/worker-autoscaling/history` | `worker_autoscaling_history` | Same. |

**Root cause (confirmed in api logs + `schema.sql` + `ensure_schema`):**

1. Live `kg_nodes` predates BOSS-355 columns `valid_from` / `valid_to`.
2. Lifespan runs `cursor.execute(schema.sql)` **before** additive column reconcile.
3. `CREATE INDEX idx_kg_nodes_valid ON kg_nodes(..., valid_from, valid_to)` fails.
4. The whole apply aborts: `[lifespan] ensure_schema skipped — column "valid_from" does not exist`.
5. Later `CREATE TABLE IF NOT EXISTS token_budget_policies / worker_instances / worker_autoscaling_*` never run.
6. FastAPI then 500s on those GETs (unhandled `ProgrammingError`).

Knowledge graph **reads still 200** because `KnowledgeGraphService.ensure_schema` adds the columns on first graph call — too late for the boot-time script.

Not ranked as incomplete:

- Prefix 404s (no index by design; children 200).
- 422 missing query params (`budget/status`, `kms/keys`, `reliability/graph`, `concurrency/status`).
- 405 POST-only (`commands/propose`, `knowledge/graph/edges`, `work-orchestration/items`).
- 401 SCIM (paid/enterprise token).
- Empty-but-200 catalogs (features, costs, on-call, KMS keys).

---

## Fix shipped on `ai/grok/community-ga` (not live until redeploy)

Fail-closed, ~10 lines:

1. **`ensure_schema`** — reconcile missing columns **first**, commit, then run `schema.sql`. Existing `kg_nodes` gets `valid_from` before the index; later tables are created.
2. **`ProgrammingError` handler** — `UndefinedTable` → **503** `"Required database relation is not applied on this runtime."` instead of a bare 500.

Tests: `apps/api/tests/test_schema_missing_fail_closed.py` (4 passed).

**Live remains 500 until** this branch is deployed **and** the API process restarts so `ensure_schema` can finish. Do not treat this panel as green until a follow-up probe of the seven paths returns 200 or 503 (never 500).

---

## 404 classification cheat-sheet

| Pattern | Meaning |
|---|---|
| `/v1/<domain>` 404 + `/v1/<domain>/<list\|status>` 200 | No index route. **Not incomplete.** |
| `/v1/<domain>/<list>` 500 + `UndefinedTable` | Service mounted, schema missing. **Incomplete.** |
| 422 `Field required` | Missing query params. **Not incomplete.** |
| 401/403 on `/scim` or `/v1/enterprise/*` writes | Paid/enterprise gate. **Expected.** |
| 405 on GET of a POST-only path | Write-only. **Expected.** |
| 200 empty `items` | Route complete; no rows. |

---

## Risks / follow-ups

- Redeploy `ai/grok/community-ga` (or cherry-pick `ensure_schema` + handler) and re-probe the seven 500s.
- 41 open deadletters remain a visibility backlog (classify works).
- `GET /v1/features` is empty on dogfood after prior seed — product gap, not a 500.
- `GET /v1/concurrency/status?project_id=` returned 503 `InFailedSqlTransaction` after the 500 burst; likely a poisoned connection. Re-check after the schema fix.
- SAML metadata 200 on community is honest SP XML; ACS stays disabled without the enterprise flag.
