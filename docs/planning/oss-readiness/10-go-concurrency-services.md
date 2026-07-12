# 10 — Runtime & Concurrency: Go services for high-scale task execution

> **Module:** Runtime & Concurrency (Go services) · **Epic:** #— · **Priority:** High
> **Boundary:** the engine stays **Python** (orchestration, I/O-bound); the **resource-bound
> execution substrate goes to Go** (goroutine/VM concurrency). This is polyglot **by layer**, not a
> rewrite. See [DECISION-engine-language](DECISION-oss-licensing-strategy.md) sibling reasoning.

## 1. Why (the scale argument)

At **100s of concurrent agents** there are two different concurrency problems, in two different layers:

1. **Orchestration concurrency** — each agent is a loop (`think → call model → decide → call tool`)
   that spends ~99% of its wall-clock *awaiting an LLM API* (measured directly in VAG Phase 3). 100s of
   these are 100s of mostly-idle `await`s; a few Python async worker pods absorb them. **Language is not
   the bottleneck here** — horizontal workers are.
2. **Execution concurrency** — when an agent actually *runs code* (tests, build, git), that does **not**
   run in the engine. It is dispatched to a **sandbox provider**. Running 100s of isolated, untrusted
   executions concurrently — CPU, RAM, process/VM isolation, lifecycle, scheduling density — is the
   **resource-bound** problem. This is where Go (goroutines + Firecracker) earns its place.

So "should the engine be Go for scale?" is aimed at the wrong layer. The engine (Python) is the thin
orchestration brain; the **execution substrate** is where thread/VM density lives, and that is what we
put in Go.

## 2. Current state (grounded)

Execution substrate is already abstracted behind a provider seam — `apps/api/app/services/sandbox_provider.py`:

- **`SandboxProvider` / `Sandbox` Protocols** (`sandbox_provider.py:26–53`) — the pipeline asks a provider
  for a sandbox and uses one surface; *"swapping execution substrate is a provider swap, not a rewrite."*
- **`LocalSubprocessProvider`** (`:56`) — per-task `EphemeralSandbox` (temp clone + allowlisted
  subprocess) **on the API host**. *No isolation, no pooling* — fine for dev / single-node, **does not
  scale** to 100s of concurrent untrusted executions. This is the OSS single-node default.
- **`MicroVMProvider`** (`:70`) — dispatches the **whole** pipeline to an external **sandbox-service**
  over HTTP: `POST/DELETE {sandbox_agent_url}/v1/sandbox/tasks/…` with `X-Sandbox-Token`
  (`:89–108`). Selected via `BOSS_GOVERNED_SANDBOX_PROVIDER=microvm` (`config.py:427`,
  `governed_code_task_service.py:480`). Docstring: single-tenant ephemeral Firecracker/Kata microVM,
  warm-pool / snapshot-resume / per-repo cache — **the Go service is what implements this side.**

**Key finding: the Python⇄Go seam already exists as a stable HTTP contract.** Sean is already building
the Firecracker sandbox-service (per project memory: security hardening in progress, hybrid split +
async API agreed). This module is therefore mostly **standardize + harden + scale** the Go side of a
contract that's already defined, plus two more Go edge services.

## 3. The three Go services

### S1 — Sandbox execution service (Firecracker) — the scale substrate  *(flagship)*
Implements the `MicroVMProvider` HTTP contract. Runs each coding task in an ephemeral, isolated
Firecracker (or Kata/gVisor) microVM. Owns the hard part: **VM pool density** (how many VMs per host),
warm-pool + snapshot-resume for cold-start, per-repo rootfs cache, per-task teardown, and retaining
failed VMs for inspection (the contract already distinguishes `release` on success vs retain on failure,
`sandbox_provider.py:92`). Go's goroutine model + tight memory footprint is the right fit for scheduling
100s of concurrent VM lifecycles. **Coordinate with Sean's in-flight service — adopt/extend, don't fork.**

### S2 — Tenant gateway / proxy (consolidate the edge)
The tenant Envoy edge already exists (per infra memory). Consolidate/replace the per-tenant proxy layer
with a Go gateway: TLS termination, tenant routing, auth passthrough, rate-limit enforcement, and
backpressure toward the sandbox pool. High-concurrency network service — Go's sweet spot; single static
binary simplifies self-host/enterprise deploy.

### S3 — Event + webhook dispatcher (high-fanout delivery)
Spec [00](00-event-and-webhook-foundation.md) defines the event **model** (Python: the ledger, the job
enqueue, subscription registry). Split the **high-fanout delivery** out as a Go service: fan a single
event to many outbound webhooks with retries/HMAC, and receive signed inbound webhooks at volume.
Python owns semantics; Go owns the fan-out I/O concurrency. **Cross-reference spec 00** — do not
duplicate; this is the delivery worker behind that foundation.

## 4. The Python ⇄ Go contract (the seam)

- **Transport:** HTTP/JSON today (already live for S1); add async job semantics — submit → poll/callback
  → collect — so a single request never blocks the Python worker (matches the agreed async API).
- **Auth:** shared-secret header (`X-Sandbox-Token` today); rotate via the ops secret store, never in code.
- **Contract ownership:** the Python side (`sandbox_provider.py`, spec 00 registry) is canonical; the Go
  services conform to it. Version the contract; changes are additive.
- **Golden rule:** the engine must run **without** any Go service present (falls back to
  `LocalSubprocessProvider` single-node). The Go services are the **scale/enterprise** substrate, not a
  hard dependency — this keeps the OSS single-node story intact (ties to fork #1: thin community control
  plane, scale features paid).

## 5. OSS vs paid boundary

| Layer | Language | OSS? |
|---|---|---|
| Engine (orchestration, VAG, pipeline) | Python | **OSS** (Apache-2.0) |
| Single-node execution (`LocalSubprocessProvider`) | Python | **OSS** — credible self-host |
| Sandbox-service (Firecracker, S1) | Go | Contract is open; the scale/pooled service is **enterprise** |
| Gateway (S2), high-fanout dispatcher (S3) | Go | **Enterprise/control-plane** |

The HTTP contracts are public (so anyone *could* implement a substrate); the *managed, dense, pooled*
Go services are where the paid operational value sits — consistent with the operations-are-the-moat thesis.

## 6. Tickets

1. **S1 — Sandbox execution service (Firecracker): standardize + harden the Go substrate.** *(high)*
   AC: conforms to the `MicroVMProvider` HTTP contract; runs a governed code task end-to-end in an
   isolated VM; warm-pool + per-repo cache; success-release / failure-retain; documented capacity model.
   Coordinate with Sean's existing service (adopt, don't fork).
2. **Async job API contract: Python engine ⇄ Go services.** *(high)*
   AC: submit/poll/callback semantics so a Python worker never blocks on a long execution; versioned
   JSON contract doc; token via ops secret store; engine falls back to local provider when absent.
3. **Concurrency & scheduling: VM-pool density, backpressure, rate-limit governance.** *(high)*
   AC: N concurrent VMs per host with a documented ceiling; backpressure to callers at capacity; honors
   budget/rate governance so provider limits (not the host) bind first.
4. **S2 — Go tenant gateway / proxy (consolidate the Envoy edge).** *(medium)*
   AC: TLS + tenant routing + auth passthrough + rate limiting as a single Go binary; parity with the
   current edge; deploy doc.
5. **S3 — Go event + webhook dispatcher (high-fanout delivery).** *(medium)*
   AC: consumes spec-00 events; fan-out outbound webhooks with retry + HMAC; signed inbound receiver at
   volume. Does **not** re-implement the spec-00 event model.
6. **Observability + load/soak harness for 100s of concurrent tasks.** *(medium)*
   AC: per-service metrics (queue depth, VM count, p50/p90 exec latency); a load test that drives 100s of
   concurrent code tasks and reports throughput, saturation point, and failure modes.

## 7. Sequencing

1. Lock the **async contract** (#2) with Sean — everything else conforms to it.
2. **S1 + scheduling** (#1, #3) — the actual scale substrate; the load harness (#6) proves it.
3. **S2 / S3** (#4, #5) — edge and fan-out; S3 lands *after* spec-00's event model exists.

## 8. Risks

- **Contract drift** between Python engine and Go services → version the contract, additive changes only,
  contract tests on both sides.
- **Duplicating Sean's work** → this is *adopt + harden*, not a greenfield build; confirm ownership first.
- **Two languages = two toolchains** → justified only for the resource-bound layer; resist scope-creeping
  Go up into the engine (the whole point of §1).
- **S3 vs spec 00 overlap** → S3 is delivery only; the event model stays in spec 00.
