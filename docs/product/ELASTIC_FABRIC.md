# Elastic fabric — workers, infra, provider usage

**Status:** Active (Community BYOK path)  
**Runtime:** `engine/boss_engine/services/elastic_fabric.py`  
**APIs:** `/v1/elastic/*`, `/v1/provider-usage/*`  
**Builds on:** worker pools, Budget-Aware Autoscaler, ProviderOps ChangeSets

---

## What it is

The elastic fabric lets **agents, nodes, and projects** request worker capacity that
materializes as:

| Kind | Behavior |
|---|---|
| `local` | Process-level slots on this control plane (immediate) |
| `container` | Container-class slots tracked as instances (immediate) |
| `vm` / `cloud` | Provider VMs (DigitalOcean droplets today) via **budgeted ChangeSets**, or **simulated** instances when `BOSS_INFRA_REAL_PROVISION_ALLOWED=false` |

Every scale and usage event is durable. Cloud multi-tenant “managed fleets” remain a separate Cloud product; Community uses **your** provider connections.

---

## Operator flow

```http
POST /v1/elastic/capacity
{
  "project_id": "boss-dogfood",
  "actor": "ceo",
  "pool_key": "factory-workers",
  "kind": "local",
  "desired_workers": 4,
  "auto_reconcile": true
}
```

```http
GET /v1/elastic/status?project_id=boss-dogfood
GET /v1/elastic/instances?project_id=boss-dogfood&active_only=true
POST /v1/elastic/instances/{id}/heartbeat
POST /v1/elastic/instances/{id}/terminate
```

Cloud/VM (BYOK):

```http
POST /v1/elastic/capacity
{
  "project_id": "boss-dogfood",
  "pool_key": "do-workers",
  "kind": "vm",
  "provider_account_id": "pa_…",
  "desired_workers": 2,
  "cloud_spillover_enabled": true
}
```

→ Creates ChangeSet ops (`digitalocean.droplet.create`) when real provision is on,  
or `status=simulated` instances when real provision is off (honest fail-closed).

After ChangeSet **approve + apply**, instances register as `running` with `external_id`.

Queue-driven scaling remains on:

```http
POST /v1/autoscaler/plans
```

---

## Provider usage (per provider)

| API | Purpose |
|---|---|
| `GET /v1/provider-usage` | Ledger of API/compute/token/billing events |
| `GET /v1/provider-usage/summary` | Rollup by provider + category + cost_events join |
| `POST /v1/provider-usage/ingest` | Control plane / adapter instrumentation |
| `POST /v1/provider-usage/sync` | Inventory+billing sync for one `provider_account_id`, mirror costs |

Categories: `api_call`, `compute_hour`, `storage`, `network`, `model_token`, `infra`, `other`.

---

## Hard rules

1. No silent real provision — ChangeSet + budget + approval when spendy.  
2. Simulated path when real provision disabled — never pretends live VMs exist.  
3. Receipts on capacity requests and provider destroys.  
4. Usage ledger is append-only monitor data; cost_events remain FinOps source of truth.  

---

## Schema

- `worker_instances` — per-pool instance lifecycle  
- `provider_usage_events` — provider-by-provider usage  
- Existing `worker_pools`, `scale_plans`, `cost_events`, ChangeSets  

---

## Tests

`apps/api/tests/test_elastic_fabric.py`
