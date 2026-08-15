# Cert — BOSS-97 parent seam (not a close)

**Probed:** 2026-08-15  
**Branch:** `ai/grok/boss-97-go-scale-seam` (from `main` @ `bd4cc36`)  
**Plane:** `8f5cdade-ed66-4c51-8f4f-c07689e68bd0` — **left In Progress**. Do not close 97 until 98/101/102 are actually shipped.  
**Spend / Firecracker:** none. Dry-run harness refuses `--live` / `--firecracker` / `--spend`.  
**GA:** not stamped.

Parent ticket is the umbrella. This cert covers the **Python ⇄ Go seam only** (spec 10 tickets #2, #3, #6 as stubs). Children stay open:

| Child | Ticket | This slice |
|---|---|---|
| S1 sandbox binary | BOSS-98 | **Not shipped.** Contract + client only. |
| S2 tenant gateway | BOSS-101 | **Not shipped.** |
| S3 webhook dispatcher | BOSS-102 | **Not shipped.** |

---

## What shipped (parent seam)

### 1. Async job API contract (enqueue / status / cancel / heartbeat)

- Typed JSON Schema: `engine/boss_engine/contracts/go_job_v1.schema.json`
- Python surface: `engine/boss_engine/services/go_job_contract.py`
  - `GoJobClient.enqueue|status|cancel|heartbeat`
  - Fail-closed timeouts (`GoJobTimeout`) — never treated as success
  - `GoServiceUnset` when `BOSS_SANDBOX_AGENT_URL` is empty
  - Receipt/dedupe keys `go-job:enqueue:{tenant}:{task}[:idempotency]`
- Additive S1 routes on the existing contract (`cancelled` status, `.../cancel`, `.../heartbeat`)
- Routes do **not** collide with the engine's durable `/v1/jobs` queue

### 2. Concurrency / scheduling hooks

- `admit_or_reject(in_flight, max_in_flight, backpressure, rate_limited)`
  - Overflow → `429` + `max_in_flight_overflow` / `backpressure_reject` / `rate_limit_reject`
  - `dropped_silently=false` always
- Policy knob `max_in_flight` / `BOSS_CONCURRENCY_MAX_IN_FLIGHT` (default 16)
- `evaluate_backpressure` emits `max_in_flight_overflow` at the ceiling

### 3. Observability / load-harness stub

- Metric names: `boss_go_queue_depth`, `boss_go_vm_count`, `boss_go_exec_latency_seconds`, `boss_go_in_flight`, `boss_go_overflow_rejected_total`, `boss_go_job_{enqueue,status,cancel,heartbeat,timeout}_total`
- `scripts/go_scale_dry_run.py` — stub only, `spent_usd_micros=0`, `runner=python_stub`
- Honesty block `go_scale_track` on `GET /v1/concurrency/policy` and `POST /v1/concurrency-load`:
  - `go_runtime_shipped=false`, `s1/s2/s3=false`, `engine_requires_go=false`

### 4. Engine without Go

Default `BOSS_SANDBOX_PROVIDER=local` → `LocalSubprocessProvider`.  
`GoJobClient` raises `GoServiceUnset` when the URL is empty. Engine does not require any Go service.

---

## Tests (noconftest, no DB, no Firecracker)

`PYTHONPATH=.:engine .venv/bin/python -m pytest --noconftest`

| Suite | Result |
|---|---|
| `engine/tests/test_go_job_contract.py` | **PASS** (Go unset, JSON Schema, fail-closed timeout, cancel/heartbeat) |
| `engine/tests/test_go_scale_seam.py` | **PASS** (overflow reject, metrics names, dry-run script) |
| `apps/api/tests/test_sandbox_contract.py` | **PASS** |
| `apps/api/tests/test_concurrency_governance_unit.py` | **PASS** |
| `engine/tests/test_microvm_claims_freeze.py` | **PASS** |
| `engine/tests/test_sandbox_echo.py` + `test_config.py` | **PASS** |

**66 passed, 0 failed** in the isolated worktree.  
`scripts/go_scale_dry_run.py --live` exits 2 and refuses spend.

---

## What remains for children

### BOSS-98 (S1 sandbox)

- Implement the Go Firecracker service that **conforms** to `go_job_v1.schema.json` + `sandbox_contract.py`
- Warm-pool, per-repo cache, success-release / failure-retain
- Do not rewrite the engine. Adopt/extend Sean’s host; do not fork a second S1
- Do **not** unfreeze `BOSS_MICROVM_CLAIMS_FROZEN`

### BOSS-101 (S2 gateway)

- Single Go binary: TLS + tenant routing + auth passthrough + rate-limit
- Honor `admit_or_reject` / `max_in_flight` backpressure toward the sandbox pool
- No budget/lease authority in the proxy
- Reuse the shared schema; do not invent a second job envelope

### BOSS-102 (S3 dispatcher)

- Fan-out worker behind spec-00 (do not re-implement the event model)
- Consume existing `webhook.outbound.deliver` jobs; Receipts + HMAC stay in Python
- Reuse enqueue/status/cancel/heartbeat types if a sidecar job API is needed

### Parent 97 close rule

Close 97 only when 98 **and** 101 **and** 102 are actually shipped (live probe, not a scaffold flag). This cert is **not** that close.
