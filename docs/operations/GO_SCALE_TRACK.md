# Go scale track (BOSS-97 / BOSS-101 / BOSS-102)

**Status:** Backlog — optional Enterprise scale, **not** a Community GA blocker  
**Plane:** [BOSS-97](https://plane.local.sean.network) (parent), children **BOSS-98** (S1), **BOSS-101** (S2), **BOSS-102** (S3); adjacent **BOSS-99** contract + **BOSS-100** / **BOSS-103** Python prep  
**Spec:** [`docs/planning/oss-readiness/10-go-concurrency-services.md`](../planning/oss-readiness/10-go-concurrency-services.md)  
**Community path:** Python worker fabric + concurrency governance + elastic fabric + Python event/webhook delivery  
**Do not:** implement a new Go runtime, rewrite the engine, or mark these tickets Done because this document exists

---

## Verdict

| Claim | Truth |
|---|---|
| Community GA needs a Go runtime | **No.** Community ships the Python API + Python worker + (optional) Python sandbox host. |
| In-repo Go services exist | **S2 library/binary only.** `go/tenant-gateway/` (BOSS-101). No Helm/compose/goldeneye mount. S1 and S3 are not in-tree on this branch. |
| A live Go tenant gateway is mounted | **No.** Edge is Traefik (compose) or the FastAPI process itself. See [`TENANT_GATEWAY.md`](TENANT_GATEWAY.md). |
| A live Go webhook/event dispatcher is mounted | **No.** Fan-out is `event_dispatch.py` + leased `webhook.outbound.deliver`. |
| S1 Firecracker exists somewhere | **Yes, as Python** on goldeneye (`sandbox-service.py` on `:8077`), plus in-repo `sandbox-host/` (Python). The HTTP contract is named “Python ⇄ Go”; the conforming host today is **not Go**. **Do not unfreeze Firecracker from this slice.** |
| `/v1/concurrency-load` measures a Go pool | **No.** In-process Python stub harness (zero model cost). Honesty fields: `runner=python_stub`, `go_scale_track.go_runtime_shipped=false`. |

Community GA language must not say “Go gateway”, “Go dispatcher”, or “Go concurrency services are live.”

---

## Ticket map

| Ticket | Planned Go service | Community / current equivalent (shipped, Python) | GA required? |
|---|---|---|---|
| **BOSS-97** | Parent: Go concurrency services for 100s of concurrent tasks | Horizontal **Python workers** (`apps/worker/boss_worker/`), Postgres leases, **BOSS-100** `/v1/concurrency/*`, **BOSS-103** stub load harness, **elastic fabric** + **worker autoscaling** | **No** |
| **BOSS-98** (S1, child of 97; not this slice) | Go Firecracker sandbox pool (warm-pool, density) | `LocalSubprocessProvider` (OSS default); `MicroVMProvider` HTTP client; in-repo `sandbox-host/sandbox_agent.py`; goldeneye **Python** Firecracker host. Contract: `sandbox_contract.py` / [`PYTHON_GO_SANDBOX_CONTRACT.md`](../infra/PYTHON_GO_SANDBOX_CONTRACT.md) | **No** (sandbox claims are a separate freeze — [`SANDBOX_GA_STATUS.md`](../product/SANDBOX_GA_STATUS.md)) |
| **BOSS-101** (S2) | Go tenant gateway / proxy (TLS, tenant routing, auth passthrough, rate limit, backpressure vs sandbox pool) | Compose/Goldeneye **Traefik** host routers; FastAPI as the control-plane listener; **model gateway** (`/v1/model-gateway/*`) is MEB/BYOK — **not** S2. In-repo optional binary: `go/tenant-gateway/` (routing + auth pass-through + fail-closed + `/health` only). **Not mounted.** | **No** |
| **BOSS-102** (S3) | Go high-fanout event + webhook dispatcher | `event_dispatch.py` (same-txn allowlisted enqueue), `event_subscription_service.py`, `webhooks.py` (HMAC outbound + signed inbound), worker kind `webhook.outbound.deliver` with Receipt + retry/deadletter. Runbook: [`WEBHOOKS.md`](WEBHOOKS.md) | **No** |

Parent **BOSS-97** stays open until a *measured* Enterprise scale program actually starts. Shipping the S2 **library** is not a live cutover and is not a Community GA claim. Do not flip `s2_go_tenant_gateway` without a mounted probe.

---

## Inventory (this repo)

Searched the tree for Go sources, Go modules, and Go deployables. Result: **S2 library only** (`go/tenant-gateway/`). Not in compose/Helm. Not live.

### What exists (Python / ops)

| Area | Path | Role |
|---|---|---|
| Worker fabric | `apps/worker/boss_worker/{main,runner,control_plane,config}.py` | Lease loop; capabilities include `code.task`, `webhook.outbound.deliver`, docs/github/A2A |
| Job leases | `engine/boss_engine/services/jobs.py` | Durable queue, TTL, heartbeat, reclaim, deadletter |
| Worker registry | `engine/boss_engine/services/worker_registry.py` | Capability/liveness inventory (not an execution authority) |
| Autoscaling | `engine/boss_engine/services/worker_autoscaling.py` + `/v1/worker-autoscaling/*` | Queue depth → desired workers via elastic fabric |
| Elastic fabric | `engine/boss_engine/services/elastic_fabric.py` + `/v1/elastic/*` | local/container/vm/cloud slots; real cloud writes stay ChangeSet-gated |
| Concurrency governance | `engine/boss_engine/services/concurrency_governance.py` + `/v1/concurrency/{policy,status}` | Queue, lease contention, model rate limits, VM-host density (BOSS-100) |
| Load harness | `engine/boss_engine/services/concurrency_harness.py` + `POST /v1/concurrency-load` | Python stub soak (BOSS-103). Not a Go pool. |
| Sandbox contract | `engine/boss_engine/services/sandbox_contract.py` | Versioned submit/poll/release envelope (BOSS-99) |
| Sandbox providers | `engine/boss_engine/services/sandbox_provider.py` | `LocalSubprocessProvider` (default) + `MicroVMProvider` HTTP client |
| In-repo sandbox host | `sandbox-host/sandbox_agent.py`, `sandbox-host/guest/` | Python FastAPI + ContainerBackend stand-in |
| Goldeneye host (out of tree) | `/srv/sandbox/bin/sandbox-service.py` on `:8077` | Python Firecracker service; see [`BOSS-340_SANDBOX_HOST_DIAGNOSIS.md`](BOSS-340_SANDBOX_HOST_DIAGNOSIS.md) |
| Event dispatcher | `engine/boss_engine/services/event_dispatch.py` | Allowlisted same-transaction fan-out to jobs |
| Webhooks | `engine/boss_engine/services/webhooks.py` | Signed inbound + Receipt-backed outbound |
| Edge | `docker-compose.yaml`, `deploy/goldeneye.compose.yaml` | **Traefik** labels for API/web hosts |
| Helm | `deploy/helm/boss/templates/{api,web,worker}.yaml` | API + web + worker only — no Go chart |
| S2 library (optional) | `go/tenant-gateway/` | Tenant/project routing, auth pass-through, fail-closed unknown tenant, `/health`. **Not mounted.** Cutover notes: [`TENANT_GATEWAY.md`](TENANT_GATEWAY.md) |

### What does **not** exist

- No Go Dockerfile, Helm chart, or compose service for the gateway
- No live Envoy (and no Traefik replacement on goldeneye)
- No Go event/webhook process (S3)
- No Go Firecracker pool (S1) — do not unfreeze from S2
- No API field that may report `go_runtime_shipped: true` or `s2_go_tenant_gateway: true` today (library ≠ live mount)

### Do not confuse these with S2

| Surface | What it actually is |
|---|---|
| `/v1/model-gateway/*` | Model Entitlement Broker (SecretRef BYOK, leases, receipts). Cloud “managed multi-tenant model gateway” is a different paid claim. |
| Preview `open_url` “gateway URL” | API-gateway path for a preview (`/v1/previews/{id}/web/`), not a tenant Go proxy. |
| `sandbox_contract.py` “Python ⇄ Go” | Historical contract name. The live host that conforms is Python. |

---

## Why Community does not need Go

Spec 10 splits two problems:

1. **Orchestration concurrency** — agent loops waiting on models. Language is not the bottleneck; **horizontal Python workers + leases** are.
2. **Execution concurrency** — many isolated code runs. That is a **sandbox-host** problem. The seam is already HTTP (`MicroVMProvider`). The engine must run **without** any scale substrate (`LocalSubprocessProvider`).

Community GA is a self-hosted single-node / small-fleet control plane. The Python fabric already provides:

- durable jobs, leases, heartbeats, deadletters
- budget-gated spend
- Receipts + dedupe for external effects
- event subscriptions + HMAC webhooks
- optional Firecracker host behind the same contract
- backpressure status so operators can see queue/lease/rate/VM pressure

A second language in-tree would add a toolchain without unblocking Community GA.

---

## Next smallest slice **if** Enterprise scale starts

Do **not** start a Go rewrite. Prove a bottleneck first.

1. **Measure the Python path.** Use `POST /v1/concurrency-load` (stub) plus a real soak against worker leases and, if needed, webhook deliveries. Record saturation (throughput stops rising) and the failing layer (queue, workers, sandbox host, outbound HTTP).
2. **If sandbox density is the limiter:** harden the **existing Python Firecracker host** and the versioned HTTP contract (BOSS-98 / BOSS-340 residual). Adopt/extend Sean’s service. Do not fork a new Go S1 in this repo.
3. **If outbound webhook fan-out is the limiter (BOSS-102):** add a *delivery sidecar* that consumes existing `webhook.outbound.deliver` jobs and the spec-00 ledger. Python keeps event semantics, subscriptions, HMAC keys, Receipts, and deadletters. The sidecar must not become a second event model.
4. **If TLS / tenant routing / edge RPS is the limiter (BOSS-101):** the static binary now lives at `go/tenant-gateway/`. It is **not** a Traefik replacement until a measured soak + shadow + explicit cutover (see [`TENANT_GATEWAY.md`](TENANT_GATEWAY.md)). No business logic, no budget/lease authority in the proxy. Do not change live goldeneye routing.
5. **Only then** consider Go, and only for the measured resource-bound layer. Engine stays Python.

Exit criteria for *starting* S2/S3 code: a linked soak report showing the Python equivalent saturates below the required Enterprise SLO, plus an explicit owner approval that this is not a Community GA claim.

---

## Honesty contract (API)

`GET /v1/concurrency/policy` and `POST /v1/concurrency-load` include `go_scale_track`:

| Field | Community value |
|---|---|
| `community_ga_required` | `false` |
| `go_runtime_shipped` | `false` |
| `s1_go_sandbox_service` | `false` |
| `s2_go_tenant_gateway` | `false` |
| `s3_go_event_dispatcher` | `false` |
| `doc` | `docs/operations/GO_SCALE_TRACK.md` |

`POST /v1/concurrency-load` also returns `runner: python_stub`. Do not flip any `*_shipped` / `s2_*` / `s3_*` flag to true without a real in-repo Go binary *and* a live probe. An unmounted library is not a probe. Tests pin the fail-closed defaults.

---

## Related

- Claims freeze: [`COMMUNITY_GA_CLAIMS_FREEZE.md`](COMMUNITY_GA_CLAIMS_FREEZE.md)
- Living plan: [`GA_LIVING_PLAN.md`](GA_LIVING_PLAN.md)
- Sandbox GA: [`SANDBOX_GA_STATUS.md`](../product/SANDBOX_GA_STATUS.md)
- Worker autoscaling: [`../product/WORKER_AUTOSCALING.md`](../product/WORKER_AUTOSCALING.md)
- Elastic fabric: [`../product/ELASTIC_FABRIC.md`](../product/ELASTIC_FABRIC.md)
- Spec 00 (event model S3 must not duplicate): [`../planning/oss-readiness/00-event-and-webhook-foundation.md`](../planning/oss-readiness/00-event-and-webhook-foundation.md)
- S2 gateway (library only): [`TENANT_GATEWAY.md`](TENANT_GATEWAY.md)
