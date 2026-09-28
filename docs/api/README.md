# BOSS HTTP API — documentation index

The control plane API is the **primary product surface**. Mission Control, workers, MCP tools, and external operators are all clients of this API.

| Resource | Location |
|----------|----------|
| Live OpenAPI (JSON) | `GET /openapi.json` (when API is running) |
| Contract (YAML) | `api/openapi.yaml` in the source checkout |
| Machine catalog | `GET /v1/meta/api-catalog` |
| OpenAPI coverage | `GET /v1/meta/openapi-coverage` |
| Full route list | [`ROUTE_INDEX.md`](ROUTE_INDEX.md) (generated) |
| Product summary | [`docs/product/API_SURFACE.md`](../product/API_SURFACE.md) |

Default local base URL: `http://127.0.0.1:8000` (see `make dev`).

---

## Authentication

### Transport auth (API keys)

`/health` is always public. `/v1/*` requires transport auth when any of these are set:

- `BOSS_REQUIRE_API_AUTH=true`
- OIDC enabled (`BOSS_OIDC_*`)
- Enterprise SAML enabled (`BOSS_ENTERPRISE_SAML_ENABLED=true`)

Configure keys with `BOSS_API_KEYS` (comma-separated). Send **one** of:

```http
Authorization: Bearer <key>
X-BOSS-API-Key: <key>
```

If auth is required but **no** keys (and no OIDC/SAML) are configured, BOSS **fails closed** with `503`.

Optional principal binding: `BOSS_API_KEY_PRINCIPALS` as `key:member_id` pairs. When set, mutations cannot claim an `actor` other than the bound member (or allowed delegates).

```bash
export BOSS_API="http://127.0.0.1:8000"
export BOSS_KEY="dev-key"

curl -sS -H "Authorization: Bearer $BOSS_KEY" \
  "$BOSS_API/v1/auth/session" | jq .
```

### Session inspection

`GET /v1/auth/session` reports whether auth is required, transport authentication status, principal binding, and resolved member id.

### Edition notes

| Edition | Auth defaults | Notes |
|---------|---------------|--------|
| **Community** | Local API keys; auth often optional in dev | Full self-hosted control plane; BYOK models/infra |
| **Cloud** | Account Connect + hosted identity paths | Easy Connect, team invites, managed workers |
| **Enterprise** | OIDC / SAML + SCIM | `/saml/v2/{project_id}/*`, `/scim/v2/{project_id}/*` |

Webhook ingress (`/v1/webhooks/github`, `/plane`, `/bitbucket`, reliability alerts, preview web proxy) uses **signature** auth and is exempt from transport API keys.

### Actor vs transport principal

- **Transport**: proves the client may call the API (API key / OIDC / SAML).
- **Actor**: project member id on mutating bodies (`actor`) or query params; enforced via RBAC (`require_project_actor`).

Always pass a real project-scoped actor on writes (seed creates `ceo`, `system`, etc.).

---

## Domain table

| Domain | Prefixes (examples) | Guide |
|--------|---------------------|--------|
| **core** | `/v1/commands`, `/tasks`, `/plans`, `/decisions`, `/jobs`, `/receipts`, `/events`, `/budget` | [CORE_CONTROL_PLANE.md](CORE_CONTROL_PLANE.md) |
| **agents** | `/v1/agent-studio`, `/skills`, `/toolchain`, `/knowledge`, `/a2a`, `/shepherd`, `/agent-groups` | [AGENTS_AND_SKILLS.md](AGENTS_AND_SKILLS.md) |
| **delivery** | `/v1/code-tasks`, `/github`, `/delivery`, `/previews`, `/sandboxes` | see ROUTE_INDEX + OpenAPI |
| **providers** | `/v1/providers`, `/changesets`, `/elastic`, `/multi-cloud`, `/finops` | [PROVIDERS_AND_INFRA.md](PROVIDERS_AND_INFRA.md) · [MULTI_CLOUD.md](MULTI_CLOUD.md) |
| **goldeneye** | `/v1/goldeneye/*` | [PROVIDERS_AND_INFRA.md](PROVIDERS_AND_INFRA.md) · [GOLDENEYE.md](GOLDENEYE.md) |
| **work** | `/v1/work-trackers`, `/work-orchestration` | [WORK_AND_TRACKERS.md](WORK_AND_TRACKERS.md) |
| **cloud** | `/v1/cloud/*`, `/account-connect`, `/edition` | [CLOUD_AND_EDITIONS.md](CLOUD_AND_EDITIONS.md) |
| **governance** | `/v1/assurance/*`, `/integration-registry`, `/break-glass` | [ASSURANCE_GOVERNANCE.md](ASSURANCE_GOVERNANCE.md) |
| **models** | `/v1/model-gateway/*` | [AGENTS_AND_SKILLS.md](AGENTS_AND_SKILLS.md) |
| **ops** | `/v1/reliability`, `/dashboard`, `/qa`, `/diag` | OpenAPI + ROUTE_INDEX |
| **meta** | `/v1/meta/*`, `/v1/capabilities`, `/health` | this file |

Live domain grouping is also returned by the catalog endpoint (source of truth for agents).

---

## Discovery: API catalog & OpenAPI coverage

### Full catalog

```bash
curl -sS -H "Authorization: Bearer $BOSS_KEY" \
  "$BOSS_API/v1/meta/api-catalog" | jq '{route_count, documented_route_count, documentation_coverage, domains: [.domains[] | {id, title, route_count}]}'
```

Response shape (v1):

- `domains[]`: `{ id, title, description, route_count, documented_count, routes[] }`
- Each route: `{ method, path, name, summary, tags, in_openapi, domain }`
- `openapi_url`, `docs.markdown_index`, `docs.openapi_yaml`

### Single domain

```bash
curl -sS -H "Authorization: Bearer $BOSS_KEY" \
  "$BOSS_API/v1/meta/api-catalog/core" | jq '{id, route_count, routes: .routes[:5]}'
```

### Coverage report

```bash
curl -sS -H "Authorization: Bearer $BOSS_KEY" \
  "$BOSS_API/v1/meta/openapi-coverage" | jq '{route_count, documented_route_count, documentation_coverage, missing_from_openapi_count}'
```

Use coverage when adding routes: implement → `scripts/sync_openapi_from_routes.py` → document in the matching guide → regenerate `ROUTE_INDEX.md`.

---

## Conventions

### `project_id`

Almost every list/read is scoped with `?project_id=...`. Mutations put `project_id` in the JSON body. Cross-project access is rejected.

### `actor`

Mutating endpoints require an `actor` that is a project member with the needed permission (e.g. `command_submit`, `task_create`, `budget_manage`, `providerops_manage`, `workflow_manage`). Unknown or unauthorized actors are denied and audited.

### Receipts & `dedupe_key`

External or side-effecting work must produce a **Receipt** with a unique `dedupe_key`. Replays with the same key must not double-apply. Look up with:

```bash
curl -sS -H "Authorization: Bearer $BOSS_KEY" \
  "$BOSS_API/v1/receipts/{dedupe_key}"
```

Provider writes use **ResourceReceipts** and **ChangeSets**.

### Budgets

Spendy actions (models, tools, infra provision) require a **BudgetReservation** before execution:

1. `POST /v1/budget/quote` — dry-run affordability  
2. `POST /v1/budget/reserve` — hold funds  
3. Do work under a leased job  
4. `POST /v1/budget/consume` (with receipt) or `POST /v1/budget/release`

Jobs can set `requires_reservation` / `reservation_id`. Cap exhaustion freezes provisioning paths rather than silent overspend.

### Fail-closed gates

Examples that **must not** degrade to mocks:

| Gate | Effect when closed |
|------|--------------------|
| Missing provider credential | Test/sync fails; receipt/decision, not fake success |
| `BOSS_INFRA_REAL_PROVISION_ALLOWED` unset/false | Real droplet/VM provision blocked |
| Auth required, no keys | `503` |
| Edition/entitlement missing | Cloud/Enterprise features return blocked status |
| Lease expired / wrong fence | Worker complete rejected |
| Destructive ChangeSet without approval | Apply blocked |

Every failure must be visible: retry, deadletter, or decision escalation.

### Workers & leases

Workers claim work via `POST /v1/worker/poll`, heartbeat with `POST /v1/worker/heartbeat`, and complete/fail with lease tokens. No lease → no side effects.

### Data boundary

Tasks, jobs, artifacts, and receipts carry `data_boundary` (`public` … `local-only`) for export and comms policy.

---

## Quick start (CEO command loop)

```bash
export BOSS_API=http://127.0.0.1:8000
export BOSS_KEY=dev-key   # if auth enabled
export PROJECT=proj_demo  # use your seeded project id
HDR=(-H "Authorization: Bearer $BOSS_KEY" -H "Content-Type: application/json")

# 1) Propose a CEO command → task + job + plan artifact
curl -sS "${HDR[@]}" -d "{
  \"project_id\": \"$PROJECT\",
  \"actor\": \"ceo\",
  \"command\": \"Ship a health check endpoint with tests\",
  \"constraints\": {}
}" "$BOSS_API/v1/commands/propose" | jq .

# 2) Inspect tasks, jobs, events
curl -sS -H "Authorization: Bearer $BOSS_KEY" \
  "$BOSS_API/v1/tasks?project_id=$PROJECT&limit=10" | jq .
curl -sS -H "Authorization: Bearer $BOSS_KEY" \
  "$BOSS_API/v1/jobs?project_id=$PROJECT&limit=10" | jq .
curl -sS -H "Authorization: Bearer $BOSS_KEY" \
  "$BOSS_API/v1/events?project_id=$PROJECT&limit=20" | jq .

# 3) Budget status
curl -sS -H "Authorization: Bearer $BOSS_KEY" \
  "$BOSS_API/v1/budget/status?scope_type=project&scope_id=$PROJECT" | jq .

# 4) Decision inbox
curl -sS -H "Authorization: Bearer $BOSS_KEY" \
  "$BOSS_API/v1/decisions/inbox?project_id=$PROJECT" | jq .
```

---

## Regenerating docs

```bash
# Route index (markdown)
python scripts/generate_api_route_index.py

# Merge implemented routes into OpenAPI stubs (main.py + routers/*.py)
# Default is a conservative splice; add --prefix /v1/foo to limit the set.
PYTHONPATH=.:engine python scripts/sync_openapi_from_routes.py

# Optional parity check
PYTHONPATH=.:engine python scripts/validate_openapi_route_parity.py
```

---

## Domain guides

1. [CORE_CONTROL_PLANE.md](CORE_CONTROL_PLANE.md) — commands, tasks, plans, decisions, jobs, receipts, budget, events  
2. [PROVIDERS_AND_INFRA.md](PROVIDERS_AND_INFRA.md) — providers, changesets, elastic, multi-cloud, finops, goldeneye  
3. [AGENTS_AND_SKILLS.md](AGENTS_AND_SKILLS.md) — agent studio, skills, model-gateway, a2a  
4. [WORK_AND_TRACKERS.md](WORK_AND_TRACKERS.md) — work-orchestration, work-trackers  
5. [CLOUD_AND_EDITIONS.md](CLOUD_AND_EDITIONS.md) — edition, cloud/*, account-connect  
6. [ASSURANCE_GOVERNANCE.md](ASSURANCE_GOVERNANCE.md) — assurance, agent-groups, integration-registry  

Machine-generated path list: [ROUTE_INDEX.md](ROUTE_INDEX.md).
