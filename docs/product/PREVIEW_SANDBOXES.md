# Persistent preview sandboxes (Workload B)

Short note for **BOSS-11**. Long-lived, reservable preview apps run on the
**sandbox host**, not as children of `boss-api`. BOSS keeps a durable ledger;
the host owns the process.

Community, OSS, and local editions permit simulated reservations only. Real
host reserve, heartbeat, and release return 409 before any host request until
these effects have an approved ChangeSet, budget reservation, and
ResourceReceipt. Existing records remain readable.

## Control-plane API

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/v1/preview-sandboxes` | Reserve / create (upsert by `project_id` + `app_id`) |
| `GET` | `/v1/preview-sandboxes?project_id=&actor=` | List (reaps expired) |
| `GET` | `/v1/preview-sandboxes/{id}?project_id=&actor=` | Status |
| `POST` | `/v1/preview-sandboxes/{id}/heartbeat` | TTL heartbeat (extends `expires_at`) |
| `POST` | `/v1/preview-sandboxes/{id}/release` | Release (host DELETE + local `released`) |

Body fields for reserve: `project_id`, `app_id`, `actor`, optional
`image_or_build`, `run_cmd`, `env`, `port`, `ttl_seconds`, `run_id`, `repo`,
`branch`, `simulate`.

Heartbeat and release bodies require `project_id` and `actor`. The URL sandbox
ID must belong to that project. Reserve, heartbeat, and release require both
`providerops_manage` and `workflow_execute` for the transport-authenticated
actor; list and get require `providerops_manage`. Responses never include
submitted environment values.

## Fail-closed vs simulate

- In Community, a non-simulated reserve returns **409** before creating a
  reservation or contacting the host. Host-backed heartbeat and release also
  return **409** without changing the row or host.
- Without `BOSS_SANDBOX_AGENT_URL` (or when the host is unreachable), reserve
  **fails closed** with HTTP **503** and a durable `status=failed` row in editions
  where host-backed previews are enabled.
- Pass `"simulate": true` (or `BOSS_PREVIEW_SANDBOX_SIMULATE=1`) for tests /
  local demos with no host — durable records + heartbeat/TTL still apply.

## Host contract (sandbox-agent)

```
POST   /v1/sandbox/apps
GET    /v1/sandbox/apps/{app_id}
POST   /v1/sandbox/apps/{app_id}/heartbeat
DELETE /v1/sandbox/apps/{app_id}
```

See `docs/infra/BOSS_SANDBOX_SERVICE_SPEC.md` §6.

## Implementation

- Service: `engine/boss_engine/services/preview_sandbox_service.py`
- Router: `apps/api/app/routers/preview_sandboxes.py`
- Table: `preview_sandboxes` (`db/schema.sql`)
- Existing short-lived process/docker/rootfs previews remain under `/v1/previews`
