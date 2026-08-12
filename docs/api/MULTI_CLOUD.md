# Multi-cloud API (`/v1/multi-cloud/*`)

Operator-facing HTTP surface for the **unified multi-cloud catalog**: infra, model,
and bare-metal skills across AWS, Azure, DigitalOcean, and OVHcloud.

- **Product:** [`docs/product/MULTI_CLOUD_INFRA_MODELS.md`](../product/MULTI_CLOUD_INFRA_MODELS.md)
- **Runtime:** `engine/boss_engine/services/multi_cloud_catalog.py`
- **OpenAPI:** `api/openapi.yaml` (tag **Multi-cloud**)

These routes are **read-only discovery**. Provisioning, destroy, inventory sync,
and model invoke still go through ProviderOps accounts, ChangeSets, Model Gateway,
and elastic fabric (see Related surfaces).

## Auth and base URL

```bash
export BOSS_API_URL="${BOSS_API_URL:-http://127.0.0.1:8000}"
export BOSS_API_KEY="${BOSS_API_KEY:?set BOSS_API_KEY}"

auth() { echo "Authorization: Bearer ${BOSS_API_KEY}"; }
```

---

## Catalog

### `GET /v1/multi-cloud/catalog`

Full unified catalog of providers and skills. Optional category filter.

```bash
# Full catalog
curl -sS -H "$(auth)" \
  "$BOSS_API_URL/v1/multi-cloud/catalog" | jq .

# Category slice (examples: infra, models, bare_metal, compute, storage, dns, billing)
curl -sS -H "$(auth)" \
  "$BOSS_API_URL/v1/multi-cloud/catalog?category=bare_metal" | jq .

curl -sS -H "$(auth)" \
  "$BOSS_API_URL/v1/multi-cloud/catalog?category=models" | jq .
```

| Query | Required | Description |
|---|---|---|
| `category` | no | Filter providers/skills for a category |

**Full catalog** response shape (conceptual):

```json
{
  "version": 1,
  "providers": [ /* per-provider entries */ ],
  "skill_index": [ /* flattened skills */ ]
}
```

**Category filter** response shape (conceptual):

```json
{
  "version": 1,
  "category": "bare_metal",
  "providers": [ /* filtered */ ],
  "count": 0
}
```

---

## Provider detail

### `GET /v1/multi-cloud/providers/{provider_key}`

Static catalog entry merged with the live Resource Plane definition and
capabilities when registered.

```bash
for key in aws azure digitalocean ovhcloud; do
  echo "=== $key ==="
  curl -sS -H "$(auth)" \
    "$BOSS_API_URL/v1/multi-cloud/providers/${key}" | jq '{catalog: .catalog.name // .catalog, capabilities: .capabilities}'
done
```

| Path param | Description |
|---|---|
| `provider_key` | `aws` \| `azure` \| `digitalocean` \| `ovhcloud` (and aliases as cataloged) |

**200** response (conceptual):

```json
{
  "catalog": { "provider": "aws", "infra_skills": [], "model_skills": [] },
  "resource_plane": { "provider": "aws", "capabilities": [] },
  "capabilities": ["aws.ec2.instance.create", "aws.bedrock.converse"]
}
```

**404** if `provider_key` is unknown.

---

## Skill index

### `GET /v1/multi-cloud/skills`

Flattened skill index from the multi-cloud catalog (infra + model skills).

```bash
curl -sS -H "$(auth)" \
  "$BOSS_API_URL/v1/multi-cloud/skills" | jq .

# Count and sample skill ids
curl -sS -H "$(auth)" \
  "$BOSS_API_URL/v1/multi-cloud/skills" | jq '{count, sample: .skill_index[:10]}'
```

**200** response (conceptual):

```json
{
  "skill_index": [ /* skill entries / ids */ ],
  "count": 0
}
```

---

## Typical operator flow (discover → connect → act)

```bash
# 1. Discover providers and skills
curl -sS -H "$(auth)" "$BOSS_API_URL/v1/multi-cloud/catalog" | jq .
curl -sS -H "$(auth)" "$BOSS_API_URL/v1/multi-cloud/skills" | jq .

# 2. Inspect one provider (catalog + live resource plane)
curl -sS -H "$(auth)" \
  "$BOSS_API_URL/v1/multi-cloud/providers/azure" | jq .

# 3. Connect credentials via ProviderOps (not under /v1/multi-cloud)
#    POST /v1/provider-accounts
#    POST /v1/provider-accounts/{id}/test
#    POST /v1/provider-accounts/{id}/sync

# 4. Provision via ChangeSet using skill_ids from the catalog
#    POST /v1/changesets  → approve → apply

# 5. Track usage
#    GET  /v1/provider-usage?project_id=...
#    POST /v1/provider-usage/sync
```

Hard rules for write paths:

1. Credentials only via SecretRef / env refs — never returned on public APIs.
2. Real provider writes require `BOSS_INFRA_REAL_PROVISION_ALLOWED` and account write mode.
3. Budget reservation before spendy create/model invoke.
4. Every write is a ChangeSet + Receipt with a unique `dedupe_key`.

---

## Related surfaces

| Surface | Path / docs |
|---|---|
| Goldeneye PaaS | [`docs/api/GOLDENEYE.md`](./GOLDENEYE.md) |
| Provider accounts / ChangeSets | `/v1/provider-accounts/*`, `/v1/changesets/*` |
| Elastic capacity | `/v1/elastic/*` |
| Provider usage ledger | `/v1/provider-usage/*` |
| Model gateway | `/v1/model-gateway/*` |
| API catalog | `GET /v1/meta/api-catalog` / `GET /v1/meta/api-catalog/providers` |
| Product design | [`docs/product/MULTI_CLOUD_INFRA_MODELS.md`](../product/MULTI_CLOUD_INFRA_MODELS.md) |

## Elastic fabric & provider usage (adjacent)

Useful companions when multi-cloud skills spill into workers or cost tracking.

### Elastic fabric (`/v1/elastic/*`)

```bash
export PROJECT_ID="${PROJECT_ID:-boss-dogfood}"

# Overview
curl -sS -H "$(auth)" \
  "$BOSS_API_URL/v1/elastic/status?project_id=${PROJECT_ID}" | jq .

# Request capacity
curl -sS -X POST -H "$(auth)" -H "Content-Type: application/json" \
  "$BOSS_API_URL/v1/elastic/capacity" \
  -d "{
    \"project_id\": \"${PROJECT_ID}\",
    \"actor\": \"ceo\",
    \"desired_workers\": 2,
    \"pool_key\": \"default\",
    \"auto_reconcile\": true,
    \"cloud_spillover_enabled\": false,
    \"reason\": \"load spike\"
  }" | jq .

# List instances
curl -sS -H "$(auth)" \
  "$BOSS_API_URL/v1/elastic/instances?project_id=${PROJECT_ID}&active_only=true" | jq .

# Reconcile a pool
POOL_ID="<worker-pool-id>"
curl -sS -X POST -H "$(auth)" -H "Content-Type: application/json" \
  "$BOSS_API_URL/v1/elastic/pools/${POOL_ID}/reconcile" \
  -d "{\"project_id\":\"${PROJECT_ID}\",\"actor\":\"ceo\",\"cloud_spillover_enabled\":false}" | jq .

# Heartbeat / terminate instance
INSTANCE_ID="<instance-id>"
curl -sS -X POST -H "$(auth)" -H "Content-Type: application/json" \
  "$BOSS_API_URL/v1/elastic/instances/${INSTANCE_ID}/heartbeat" \
  -d "{\"project_id\":\"${PROJECT_ID}\",\"actor\":\"worker\",\"status\":\"ready\"}" | jq .

curl -sS -X POST -H "$(auth)" -H "Content-Type: application/json" \
  "$BOSS_API_URL/v1/elastic/instances/${INSTANCE_ID}/terminate" \
  -d "{\"project_id\":\"${PROJECT_ID}\",\"actor\":\"ceo\",\"reason\":\"scale down\",\"destroy_provider_resource\":false}" | jq .
```

### Provider usage (`/v1/provider-usage/*`)

```bash
# Ledger
curl -sS -H "$(auth)" \
  "$BOSS_API_URL/v1/provider-usage?project_id=${PROJECT_ID}&limit=50" | jq .

# Summary
curl -sS -H "$(auth)" \
  "$BOSS_API_URL/v1/provider-usage/summary?project_id=${PROJECT_ID}" | jq .

# Ingest a control-plane event
curl -sS -X POST -H "$(auth)" -H "Content-Type: application/json" \
  "$BOSS_API_URL/v1/provider-usage/ingest" \
  -d "{
    \"project_id\": \"${PROJECT_ID}\",
    \"actor\": \"system\",
    \"provider\": \"aws\",
    \"operation\": \"bedrock.converse\",
    \"category\": \"api_call\",
    \"quantity\": 1,
    \"unit\": \"count\"
  }" | jq .

# Sync inventory/billing for an account
ACCOUNT_ID="<provider-account-id>"
curl -sS -X POST -H "$(auth)" -H "Content-Type: application/json" \
  "$BOSS_API_URL/v1/provider-usage/sync" \
  -d "{
    \"project_id\": \"${PROJECT_ID}\",
    \"actor\": \"ceo\",
    \"provider_account_id\": \"${ACCOUNT_ID}\"
  }" | jq .
```
