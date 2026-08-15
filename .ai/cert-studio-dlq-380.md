# Cert — BOSS-347 / BOSS-372 / BOSS-380

**Probed:** 2026-08-15  
**API:** `https://boss-api.local.sean.network`  
**Project:** `boss-dogfood`  
**Auth:** `X-Boss-Api-Key` from local key file (never printed). Principal `ceo`.  
**Health:** `GET /health` → 200, `status=ok`, `database_connected=true`, `openai_configured=true`, `github_configured=true`.  
**Edition:** `GET /v1/edition?project_id=boss-dogfood` → 200 `edition_id=community`.  
**Repo HEAD (this cert):** `3af9c64` `feat(ops): five remaining operator slices`.  
**Spend / flags:** none. No BYOK bind. No seed POST (catalog not empty). No GA stamp.

**Plane (left In Progress; owner can close from this cert):**

| Issue | UUID | Comment | Title |
|---|---|---|---|
| BOSS-347 | `eb0cdbf6-97de-476c-9890-608733722378` | `b8182649-aff1-468f-a089-9beb1187b050` | Agent Studio empty on dogfood — seed/setup |
| BOSS-372 | `55d4c253-51e2-40f7-89e3-7011c13517fa` | `8c6ee2c7-5bb5-4a97-9a5c-73175b29641d` | First-class DLQ remediations + fail-closed retry |
| BOSS-380 | `91557761-b6c4-4b39-9264-7de5d5575e69` | `050a57fe-8e4a-47d4-b01c-6f1f700bbf2d` | SecretRef consumption: factory, GitHub, Plane unwrap |
| BOSS-369 (not this cert) | `a8cf92b4-77ab-4298-be98-1ed79a1180a2` | — | BYOK paste / local_vault bind — stays open |

Ops `GET $OPS_BASE_URL/secret?key=PLANE_API_KEY` is not reachable from this workstation (`ops.local.sean.network` → 404; HTTP ops refused). Plane key came from the existing local key file. Never printed.

---

## Tests (noconftest, no DB)

`PYTHONPATH=.:engine .venv/bin/python -m pytest --noconftest`

| Suite | Result |
|---|---|
| `engine/tests/test_credential_resolve.py` | **PASS** (included in 19) |
| `engine/tests/test_a2a_transport_health.py` | **PASS** (unconfigured Redis is `not_configured`, not `degraded`) |
| `engine/tests/test_github_secret_resolve.py` | **PASS** |
| Extra no-DB (scripts + related engine): feature sweep, official reports, metatron TUI, byok bind script, sealed live script, working-software gate, `test_secretref_consumption`, `test_openai_adapter_secretref`, `test_guest_engine_secretref`, `test_deadletter_classify` | **115 passed** |

**19 + 115 = 134 passed, 0 failed.** API/DB suites (`test_agent_studio_seed.py`, `test_deadletter_remediations.py`) were **not** run (need Postgres).

---

## Code (repo, this tree)

### BOSS-347 — seed router

`apps/api/app/routers/agent_studio_seed.py` + `POST /v1/agent-studio/catalog/seed` in `main.py` (delegates to `_seed_catalog`).

- Definitions only: `external_effects=0`, `model_calls=0`, `jobs_enqueued=0`, `budget_reserved=false`, `launched_live_agents=false`.
- Receipt + dedupe `agent-studio:catalog-seed:{project_id}`.
- Requires `role_review`. Schema missing → **503**, never 500.
- Repo seed also installs specpack workflows (`seed_specpack_workflows`). Idempotent.

### BOSS-372 — remediations

`apps/api/app/routers/deadletter_ops.py` + `DeadletterRemediationService` in `engine/boss_engine/services/deadletters.py`.

- `GET /v1/deadletters/remediations` — empty list is valid.
- `POST /v1/deadletters/{id}/remediations` — `retry` / `requeue` / `escalate` / `hold`.
- `POST /v1/deadletters/{id}/retry` — always persists applied/blocked/failed.
- Unknown action 400. Missing DLQ 404. No silent requeue.

### BOSS-380 — SecretRef consumption

Single helper: `engine/boss_engine/services/credential_resolve.py` `resolve_project_credential`.

Rules: usable SecretRef (`active` / `rotating` / `rotation_due`) first via `resolve_secret_ref`. Bound-but-broken → `SecretUnavailable`, **no env consolation**. Env is last-resort only when no usable ref is bound.

Wired consumers (lazy import, fail-closed):

| Consumer | Path |
|---|---|
| OpenAIAdapter | `openai_adapter.py` `_resolved_api_key` |
| CodeAgent factory | `code_agent.py` `_bind_lane_credentials` / `make_code_agent` |
| Factory guest | `governed_code_task_service.py` `_lane_api_key` / `_guest_engine_fields` |
| GitHub | `github_connections.py` `_resolve_secret` (`resolve_secret_ref` when id bound) |
| Plane | `plane_client.py` `_token` (SecretRef / `sref_*` first) |

---

## Live HTTP

Unauthenticated GETs on catalog / remediations / secret-refs / kms → **401** (fail-closed).

| Method | Path | HTTP | Evidence |
|---|---|---|---|
| GET | `/health` | **200** | ok + DB + openai/github configured |
| GET | `/v1/auth/session` | **200** | `auth_required=true`, `principal_member_id=ceo` |
| GET | `/v1/edition?project_id=boss-dogfood` | **200** | community |
| GET | `/v1/agent-studio/catalog?project_id=boss-dogfood` | **200** | **13 agents / 3 panels / 0 workflows** |
| GET | `/v1/agent-studio/catalog/seed` | **405** | route mounted; POST-only |
| GET | `/v1/agent-studio/seed` | **405** | alias mounted |
| GET | `/v1/deadletters/remediations?project_id=boss-dogfood` | **200** | `items=[]`, `actions=[retry,requeue,escalate,hold]` |
| GET | `/v1/deadletters?project_id=boss-dogfood&limit=100` | **200** | **43 open** historical |
| GET | `/v1/kms/health` | **200** | not 500; local_vault **not** ok (369) |
| GET | `/v1/secret-refs?project_id=boss-dogfood` | **200** | **7 refs**; **0 raw values**; **last4 absent** |
| GET | `/v1/github/connections?project_id=boss-dogfood` | **200** | 2 active; no token/secret fields |
| GET | `/v1/providers?project_id=boss-dogfood` | **200** | 4 accounts; env pointers only |
| GET | `/openapi.json` | **200** | catalog GET, seed POST, remediations GET, kms, secret-refs |

### Catalog (347)

All 13 published + active:

`backend_engineer`, `chief_architect`, `cfo`, `chief_operator`, `database_administrator`, `devops_engineer`, `frontend_engineer`, `full_stack_engineer`, `product_manager`, `qa_engineer`, `security_engineer`, `ui_designer`, `ux_engineer`

Panels: `architecture_risk_council`, `product_delivery_council`, `release_readiness_panel`.

`agent0.active_version.status=published`. Tests evidence: 17. Agent/panel/workflow **runs: 0**.

**Seed POST was not sent.** Catalog is not empty. Seed is no-spend, but the instruction is “only if empty.”

**Residual (do not block seed CLOSE):**

- `workflows=[]` on live catalog and `GET /v1/workflows` (0). Repo `38d8feb` seeds workflows; live catalog predates that install. Next idempotent seed **after** a shared-tag that includes `38d8feb` would add them. Do not POST from this cert.
- A2A health still **false-red `degraded`**: `configured=false`, `A2ATransportUnavailable` / `REDIS_URL is not configured`. Repo transport health is `not_configured` (tests pass). Live image has not picked up `38d8feb`.

### Remediations (372)

Empty list is **honest empty**, not 404/500. Actions advertised. Unauth 401. Historical 43 open DLs remain (TPM / 402 / 413 / max-turns / DO SecretRef / substrate) — out of scope; remediations make them actionable. No retry/escalate posted (would enqueue work).

### SecretRef / KMS (380)

`GET /v1/secret-refs` item keys include metadata only (`ref_kind`, `ref_key`, `storage_backend`, `status`, `provider`, …). Leak scan on `value` / `secret_value` / `raw` / `plaintext` / `token` / `api_key` / `password` → **0**. `last4` present on **0** rows.

| id (public) | provider | backend | status |
|---|---|---|---|
| `secret_132f0b7e2a5645d0afe6` | aws | environment | active |
| `secret_boss_dogfood_azure_env_BOSS_DOGFOOD_AZURE_CLIENT` | azure | environment | active |
| `secret_boss_dogfood_azure_env_BOSS_DOGFOOD_AZURE_SECRET` | azure | environment | active |
| `secret_boss_dogfood_azure_env_BOSS_DOGFOOD_AZURE_TENANT` | azure | environment | active |
| `secret_boss_dogfood_digitalocean_env_BOSS_DIGITALOCEAN_TOKEN` | digitalocean | environment | active |
| `secret_c3e67002a9434be3b6d9` | github | environment | active |
| `secret_7c6bc09399b24e77a58b` | openai | environment | active |

OpenAI provider `prov_78c91c3fca5d47a8846a` still `credential_refs.primary=env:OPENAI_API_KEY`, status `degraded`. **No `local_vault` pointer.** That is BOSS-369, not 380.

`GET /v1/kms/health` **200** (resolver-adjacent, no 500):

- `local_vault.ok=false` — master key missing (named 369 blocker)
- `vault` / `openbao` / `platform_secret_store` not configured
- `file` ok writable; `environment` ok read-only

Resolver is on the live lineage: remediations + seed routes are mounted, and those land after `254c85d` (SecretRef-first helper). No spendy unwrap was invoked. List/health paths that sit next to resolve did **not** 500.

---

## CLOSE recommendations

FEATURES stays ◐. **Not GA.** Do not stamp public/C1–C4. Do not bind BYOK. Do not enable flags.

| Ticket | Recommend | Why | Leave open |
|---|---|---|---|
| **BOSS-347** | **CLOSE** | Dogfood library is not empty. Seed route live (405 GET / OpenAPI POST). 13/13 published agents, 3 panels. Original “empty page” AC is met. | Workflows=0 and A2A false-red `degraded` until shared-tag ≥ `38d8feb` + optional idempotent seed. File as residual / follow-on if desired; do not keep 347 open for an empty catalog. |
| **BOSS-372** | **CLOSE** | Operator remediations API is live: list 200, honest empty, actions advertised, unauth 401. Retry/escalate/hold are first-class in repo + tests. | Do **not** auto-drain the 43 historical DLs. TPM/credits and missing DO creds stay operator work (no spend from this ticket). |
| **BOSS-380** | **CLOSE** | Resolver module exists, tests pin SecretRef-first / fail-closed broken ref, consumers are wired. Live list/health paths 200, **no raw secret values**, no last4 leak. | **BOSS-369** still owns local_vault write-through (`BOSS_KMS_MASTER_KEY` missing). Env last-resort remains until an operator bind. |

**Do not close BOSS-369.** Vault master is still absent; no bind was attempted.

---

## What this cert did not do

- POST `/v1/agent-studio/catalog/seed` (catalog not empty)
- POST remediations / retry / classify escalate
- Easy-connect / BYOK paste / local_vault write
- Model, GitHub write, or factory create
- Stamp GA or flip FEATURES to shipped
- Enable `BOSS_KMS_*`, `BOSS_FACTORY_ALLOW_CREATE`, `BOSS_QA_ALLOW_OFFICIAL`, or `--env-template`
