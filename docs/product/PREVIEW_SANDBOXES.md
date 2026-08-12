# Persistent preview sandboxes (Workload B)

Short note for **BOSS-11**. Long-lived, reservable preview apps run on the
**sandbox host**, not as children of `boss-api`. BOSS keeps a durable ledger;
the host owns the process.

## Control-plane API

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/v1/preview-sandboxes` | Reserve / create (upsert by `project_id` + `app_id`) |
| `GET` | `/v1/preview-sandboxes?project_id=` | List (reaps expired) |
| `GET` | `/v1/preview-sandboxes/{id}` | Status |
| `POST` | `/v1/preview-sandboxes/{id}/heartbeat` | TTL heartbeat (extends `expires_at`) |
| `POST` | `/v1/preview-sandboxes/{id}/release` | Release (host DELETE + local `released`) |

Body fields for reserve: `project_id`, `app_id`, `actor`, optional
`image_or_build`, `run_cmd`, `env`, `port`, `ttl_seconds`, `run_id`, `repo`,
`branch`, `simulate`.

## Fail-closed vs simulate

- Without `BOSS_SANDBOX_AGENT_URL` (or when the host is unreachable), reserve
  **fails closed** with HTTP **503** and a durable `status=failed` row.
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
