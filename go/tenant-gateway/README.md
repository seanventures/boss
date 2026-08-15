# Tenant gateway (S2)

Optional Go library + static binary for tenant/project edge routing.

This is **not** mounted on goldeneye. Production still uses **Traefik**.
The Python engine does **not** depend on this process.

| Claim | Truth |
|---|---|
| In-repo library/binary | Yes — this directory |
| Live cutover of `sean.network` / goldeneye | **No** |
| Engine requires the gateway | **No** |
| Community GA claim | **No** |
| Replaces Traefik/Envoy today | **No** |

## What it does

- Tenant + project routing (`X-Boss-Tenant` / `X-Boss-Project`, `/t/{tenant}/p/{project}/…`, `{tenant}.example`)
- Auth header **pass-through** (`Authorization`, `X-API-Key`, …)
- **Fail-closed** unknown / disabled tenant (typed JSON `404` / `403`)
- `GET /health` → `200` without a tenant
- Never echoes credentials in error bodies

It does **not** terminate TLS, rate-limit, or apply sandbox backpressure. Those stay residual.

## Run locally

```bash
# tests
go test ./...

# binary (listens on 127.0.0.1:8787 by default)
go run ./cmd/tenant-gateway -config testdata/tenants.example.json

curl -sS http://127.0.0.1:8787/health
curl -sS -H 'X-Boss-Tenant: ghost' -H 'Authorization: Bearer super-secret' \
  http://127.0.0.1:8787/v1/tasks
# → 404 {"error":{"code":"unknown_tenant",...}}  (body must not contain the bearer token)
```

From the repo root: `make test-go`.

Empty config is valid: only `/health` succeeds. There is no default backend.

## How this would replace Envoy / Traefik later

See [`docs/operations/TENANT_GATEWAY.md`](../../docs/operations/TENANT_GATEWAY.md).
Do not change live goldeneye routing in this slice.
