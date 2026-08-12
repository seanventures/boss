# Worker autoscaling — distributed scale-out control plane

**Status:** Active (Community + Enterprise)  
**Runtime:** `engine/boss_engine/services/worker_autoscaling.py`  
**APIs:** `/v1/worker-autoscaling/*`  
**Builds on:** elastic fabric (`/v1/elastic/*`), worker pools, job leases, receipts

---

## What it is

A **deterministic** control plane that turns **job queue depth** into **desired
worker counts** per pool, then applies capacity through **ElasticFabricService**.

| Step | Behavior |
|---|---|
| 1. Measure | `queue_depth` = ready `queued` + `failed_retryable` jobs; also `leased` / `running` |
| 2. Evaluate | `needed = ceil(queue_depth / max_queue_depth_per_worker)` clamped to `[min, max]` |
| 3. Gate | Scale-up held if `queue_depth < scale_up_threshold` or within `cooldown_seconds` |
| 4. Apply | `ElasticFabricService.request_capacity` + reconcile (local/container immediate; VM simulated when real provision off) |
| 5. Receipt | Every apply emits `worker_autoscaling.apply` + elastic capacity receipts |

Never silently provisions. Real cloud/VM writes require
`BOSS_INFRA_REAL_PROVISION_ALLOWED=true` and the existing ChangeSet path.

---

## Policies (multi-pool)

```http
PUT /v1/worker-autoscaling/policies
{
  "project_id": "boss-dogfood",
  "actor": "ceo",
  "pool_key": "factory-workers",
  "min_workers": 0,
  "max_workers": 8,
  "scale_up_threshold": 1,
  "cooldown_seconds": 30,
  "max_queue_depth_per_worker": 5,
  "kind": "local"
}
```

| Field | Meaning |
|---|---|
| `min_workers` / `max_workers` | Hard clamp for desired |
| `max_queue_depth_per_worker` | Jobs each worker is expected to absorb (sizing) |
| `scale_up_threshold` | Minimum queue depth before scale-up is allowed |
| `cooldown_seconds` | Min time between applies (blocks thrash) |
| `kind` | `local` \| `container` \| `vm` \| `cloud` (elastic fabric mapping) |
| `pool_key` | Multi-pool support — one policy per pool |

```http
GET /v1/worker-autoscaling/policies?project_id=boss-dogfood
```

---

## Evaluate / apply

```http
POST /v1/worker-autoscaling/evaluate
{ "project_id": "boss-dogfood", "actor": "ceo", "pool_key": "factory-workers" }
```

Returns queue snapshot + per-pool `needed_workers`, `desired_workers`, `action`
(`scale_up` | `scale_down` | `no_change` | `held_threshold` | `held_cooldown`).

```http
POST /v1/worker-autoscaling/apply
{
  "project_id": "boss-dogfood",
  "actor": "ceo",
  "pool_key": "factory-workers",
  "auto_reconcile": true,
  "force": false
}
```

- Default: respects threshold + cooldown.
- `force=true`: bypasses holds (still clamps min/max; still fabric-gated).
- Omit `pool_key` to evaluate/apply **all** policies for the project.

---

## Status / history

```http
GET /v1/worker-autoscaling/status?project_id=boss-dogfood
GET /v1/worker-autoscaling/history?project_id=boss-dogfood&pool_key=factory-workers
```

Status surfaces **desired vs actual**, leased jobs, queue depth, and policy action
per pool. History rows are durable evaluate/apply decisions with optional
`receipt_id`.

---

## Parallel multi-task scale-out path (demo)

1. Upsert policy with `max_queue_depth_per_worker=2`, `max_workers=4`, `cooldown_seconds=0`.
2. Enqueue N jobs (`POST /v1/jobs`).
3. `POST /evaluate` → `desired_workers = ceil(N/2)`.
4. `POST /apply` → elastic local instances = desired; receipts + events.
5. `GET /status` → actual_workers == desired_workers; queue + leased visible.

---

## Relation to Budget-Aware Autoscaler

| Surface | Role |
|---|---|
| `POST /v1/autoscaler/plans` | Budget/provider-health governed scale plan on worker_pools |
| `/v1/worker-autoscaling/*` | Explicit multi-pool policy + cooldown + elastic apply path |
| `/v1/elastic/*` | Capacity materialization (instances, usage, gates) |

Worker autoscaling **does not** bypass budget red/orange rules for spendy cloud
provision — elastic fabric still reserves budget and uses ChangeSets when real
provision is enabled.

---

## Schema

- `worker_autoscaling_policies`
- `worker_autoscaling_history`
- Uses existing `worker_pools`, `worker_instances`, `jobs`, `receipts`

---

## Tests

`apps/api/tests/test_worker_autoscaling_api.py`

- Pure `compute_desired_workers` unit cases
- Policy upsert → queue → evaluate → apply → status/history
- Multi-pool + cooldown hold + force
- No-op apply when idle
