# BOSS API surface (product summary)

**Status:** Active  
**Human docs index:** [`docs/api/README.md`](../api/README.md)  
**Live discovery:** `GET /v1/meta/api-catalog`  
**Coverage report:** `GET /v1/meta/openapi-coverage`  
**Machine schema:** `/openapi.json` · [`api/openapi.yaml`](../../api/openapi.yaml)  
**Generated route list:** [`docs/api/ROUTE_INDEX.md`](../api/ROUTE_INDEX.md)

---

## Principle

Every operator control and agent capability that BOSS exposes in Mission Control
or engine services should be reachable over **HTTP under `/v1/*`**, with:

| Concern | Rule |
|---------|------|
| Scope | `project_id` (org/workspace via project) |
| Actor | `actor` on mutating calls; RBAC via `require_project_actor` |
| Effects | Receipts + unique `dedupe_key` for external/side effects |
| Spend | Budget reservation before spendy work |
| Leases | Worker jobs claim with TTL + heartbeat |
| Gates | Fail closed (auth, credentials, edition, provision flags) |
| Docs | OpenAPI entry + domain guide |

The control plane API is the primary product surface. `apps/web` is a client, not a second source of business logic.

---

## Domain map → guides

| Domain | Prefix examples | Guide |
|--------|-----------------|--------|
| Meta | `/v1/meta/*`, `/v1/edition`, `/v1/capabilities`, `/health` | [docs/api/README.md](../api/README.md) |
| Core | `/v1/commands`, `/tasks`, `/plans`, `/jobs`, `/receipts`, `/budget`, `/events`, `/decisions` | [CORE_CONTROL_PLANE.md](../api/CORE_CONTROL_PLANE.md) |
| Agents | `/v1/agent-studio`, `/skills`, `/a2a`, `/shepherd` | [AGENTS_AND_SKILLS.md](../api/AGENTS_AND_SKILLS.md) |
| Models | `/v1/model-gateway/*` | [AGENTS_AND_SKILLS.md](../api/AGENTS_AND_SKILLS.md) |
| Delivery | `/v1/code-tasks`, `/github`, `/delivery`, `/previews`, `/sandboxes` | [ROUTE_INDEX.md](../api/ROUTE_INDEX.md) + OpenAPI |
| Providers | `/v1/providers`, `/changesets`, `/elastic`, `/multi-cloud`, `/finops` | [PROVIDERS_AND_INFRA.md](../api/PROVIDERS_AND_INFRA.md) |
| Goldeneye | `/v1/goldeneye/*` | [PROVIDERS_AND_INFRA.md](../api/PROVIDERS_AND_INFRA.md) |
| Work | `/v1/work-trackers`, `/work-orchestration` | [WORK_AND_TRACKERS.md](../api/WORK_AND_TRACKERS.md) |
| Cloud | `/v1/cloud/*`, `/account-connect` | [CLOUD_AND_EDITIONS.md](../api/CLOUD_AND_EDITIONS.md) |
| Governance | `/v1/assurance/*`, `/agent-groups`, `/integration-registry` | [ASSURANCE_GOVERNANCE.md](../api/ASSURANCE_GOVERNANCE.md) |
| Ops | `/v1/reliability`, `/dashboard`, `/qa`, `/diag` | [docs/api/README.md](../api/README.md) + OpenAPI |

Domain taxonomy for the live catalog is defined in `engine/boss_engine/services/api_catalog.py`.

---

## Authentication (product view)

- **Community / local:** optional or API-key transport via `BOSS_REQUIRE_API_AUTH` + `BOSS_API_KEYS` (`Authorization: Bearer` or `X-BOSS-API-Key`).
- **Principal binding:** `BOSS_API_KEY_PRINCIPALS` maps keys to project members so clients cannot spoof `actor`.
- **Cloud / Enterprise:** Account Connect, OIDC, SAML ACS, SCIM — edition-gated, fail closed when misconfigured.
- **Webhooks:** signature verification; exempt from transport API keys.

Details and curl examples: [docs/api/README.md](../api/README.md).

---

## Vertical slice (dogfood)

1. `POST /v1/commands/propose` — CEO command → task/job/plan  
2. Budget `quote` → `reserve` → work under lease → `consume` + receipt  
3. Provider writes only via ChangeSets + ResourceReceipts  
4. Decisions in inbox for risk/spend/destructive gates  
5. Events feed shows full trail  

Implementation build order and hard rules: root [`AGENTS.md`](../../AGENTS.md).

---

## Regeneration

```bash
# Route index markdown (METHOD + path, grouped)
python scripts/generate_api_route_index.py

# Generate both static contracts from the mounted FastAPI editions
python scripts/generate_openapi_contract.py --edition enterprise
python scripts/generate_openapi_contract.py --edition community

# Fail if either generated contract or its mounted route set has drifted
make openapi-validate
```

Live:

```bash
curl -sS -H "Authorization: Bearer $BOSS_KEY" \
  "$BOSS_API/v1/meta/api-catalog" | jq '{route_count, documentation_coverage}'

curl -sS -H "Authorization: Bearer $BOSS_KEY" \
  "$BOSS_API/v1/meta/openapi-coverage" | jq .
```

---

## Adding a new control

1. Implement service method in `engine/boss_engine/services/`.  
2. Add FastAPI route in `apps/api/app/main.py` or a domain router (e.g. governance).  
3. Run `sync_openapi_from_routes.py` and `generate_api_route_index.py`.  
4. Document in the matching `docs/api/*.md` guide with at least one curl flow.  
5. Add a focused API test; cover budget/receipt if the path can spend or side-effect.  

---

## Related product docs

- PRD / TECHNICAL_SPEC v2.0  
- Community / Cloud edition specs  
- `packages/specpack/workflows/ceo_command_to_pr.yaml`  
- `packages/specpack/policies/default_safety.yaml`  
- `packages/specpack/policies/resource_plane_policy.yaml`  
