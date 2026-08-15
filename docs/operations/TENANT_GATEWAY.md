# S2 tenant gateway — optional Go edge (not live)

**Status:** ◐ library + binary in-tree · **not mounted** · **not Community GA**  
**Plane:** [BOSS-101](https://plane.local.sean.network) (S2 child of BOSS-97)  
**Code:** [`go/tenant-gateway/`](../../go/tenant-gateway/)  
**Spec:** [`docs/planning/oss-readiness/10-go-concurrency-services.md`](../planning/oss-readiness/10-go-concurrency-services.md) (S2)

This document is the honesty contract for the Go tenant gateway. It is a
**library/binary for OSS / Enterprise scale experiments**. It is **not** a
cutover of `sean.network` or goldeneye.

## Current production edge

| Environment | Edge today | This binary |
|---|---|---|
| goldeneye (`boss.local.sean.network`, `boss-api.local.sean.network`) | **Traefik** host routers in `docker-compose.yaml` / `deploy/goldeneye.compose.yaml` | **Not used** |
| Compose / Community single-node | Traefik labels, or FastAPI `:8000` directly | **Not used** |
| Historical spec language (“Envoy edge”) | Spec 10 still says Envoy; **this repo never shipped an in-tree Envoy**. Live routing is Traefik. | — |

Do **not** rewrite Traefik (or Envoy) on goldeneye in this slice. Do **not**
add this process to the default compose/Helm install. Do **not** flip
`go_scale_track.s2_go_tenant_gateway` to `true` without a live probe of a
mounted binary.

The Python engine must keep running when this binary is absent
(`LocalSubprocessProvider`, Traefik or direct `:8000`).

## What shipped in this slice

Smallest fail-closed proxy:

- Tenant / project routing from path (`/t/{tenant}/p/{project}/…`), headers
  (`X-Boss-Tenant`, `X-Boss-Project`), or host (`{tenant}.example`)
- Auth header pass-through (`Authorization`, `X-API-Key`, …)
- Unknown tenant → typed JSON `404` (`unknown_tenant`); disabled tenant → `403`
  (`tenant_forbidden`); missing tenant → `400` (`missing_tenant`)
- `GET /health` → `200` `{"status":"ok","service":"boss-tenant-gateway"}`
- Error bodies never echo inbound credentials

No TLS termination, rate limits, or sandbox-pool backpressure. Those remain
residual (spec 10 ticket AC).

## How it would replace Traefik / Envoy later

This is a **future** cutover plan, not a change to run now.

1. **Measure.** Prove the current Traefik / FastAPI edge is the limiter
   (RPS, TLS, tenant fan-out) with a soak report linked from BOSS-101.
   See [`GO_SCALE_TRACK.md`](GO_SCALE_TRACK.md) exit criteria.
2. **Parity table.** Encode today’s Traefik host rules as a static tenant
   table (same hostnames, same upstream ports, same fail-closed missing
   config). No business logic, budgets, or leases in the proxy.
3. **Shadow.** Run `tenant-gateway` beside Traefik on a non-production
   bind (loopback or a dedicated tailnet host). Compare status codes and
   `X-Boss-Tenant` injection. Production Traefik stays the listener.
4. **TLS.** Terminate where Traefik does today (or keep an outer TLS
   terminator). This binary does not yet terminate TLS.
5. **Cutover (explicit owner approval).** Point the existing hostnames at
   the Go binary **only** after shadow parity and a rollback (Traefik
   labels restored). One environment at a time. Never a silent DNS flip.
6. **Envoy.** If an environment still has a per-tenant Envoy, replace it
   the same way: static table → shadow → cutover. Do not run Envoy *and*
   this gateway on the same hostname.

Until step 5, FEATURES stays ◐ and Community docs must not say “Go
gateway is live.”

## Run locally

```bash
cd go/tenant-gateway
go test ./...
go run ./cmd/tenant-gateway -config testdata/tenants.example.json
curl -sS http://127.0.0.1:8787/health
```

Repo target: `make test-go`.

## Residual

- TLS termination
- Rate-limit / sandbox-pool backpressure
- Dynamic tenant discovery (this table is static JSON)
- Helm / compose / goldeneye mount
- Live probe that would justify `s2_go_tenant_gateway: true`
- Firecracker / S1 — **do not unfreeze**; out of scope
