# Python ⇄ Go sandbox job contract (v1.0)

> Plane #99, module "Runtime & Concurrency (Go services)". Spec: `docs/planning/oss-readiness/10-go-concurrency-services.md` §4.
> Canonical Python side: `apps/api/app/services/sandbox_contract.py`. **The Go sandbox-service (S1) conforms to this.**

## Model

The Python engine dispatches a **whole coding task** to an external **sandbox-service** (the Go
Firecracker service) and never blocks a worker on the execution: **submit → poll → collect**. The whole
`clone → agent → test → diff` pipeline runs guest-side; only governance metadata + the diff cross back.

**Golden rule:** the engine runs with **no** sandbox-service present — it falls back to the in-process
`LocalSubprocessProvider`. The Go service is the scale/enterprise substrate, **never a hard dependency**.
`available(settings)` (true iff `BOSS_SANDBOX_AGENT_URL` is set) gates the whole contract.

- **Transport:** HTTP/JSON. **Auth:** shared secret header `X-Sandbox-Token` (from the ops secret store; rotate there, never in code).
- **Ownership:** the Python side is canonical; the Go side conforms. Changes are **additive** and bump `contract_version`.
- **`contract_version`:** `1.0` (sent on every submit; the service SHOULD reject an unknown major version).

## Operations

### 1. Submit — `POST /v1/sandbox/tasks`
Request body (built by `build_submit_request`):
```json
{
  "contract_version": "1.0",
  "task_id": "crun_...",
  "tenant_id": "default",
  "repo": { "owner": "paramg", "name": "boss-sandbox", "base_branch": "main", "work_branch": "boss/code/crun_..." },
  "git_token": "<short-lived repo token>",
  "instructions": "…", "acceptance_criteria": ["…"], "test_commands": ["pytest -q"],
  "engine": "claude_code|openai|native|…",
  "engine_env": { "CLAUDE_CODE_OAUTH_TOKEN": "…", "OPENAI_API_KEY": "…" },
  "run_mutation": false,
  "resources": { }
}
```
`engine_env` carries the **per-task** model credentials by env-var name — the guest exports them for the
agent; they are **never baked into the guest image**. Response: `{ "sandbox_task_id": "<id>" }`.

### 2. Poll — `GET /v1/sandbox/tasks/{sandbox_task_id}`
Response (normalized by `parse_status` / `parse_result`):
```json
{
  "status": "queued|running|done|failed|timeout",
  "phase": "clone|implement|test|review|…",
  "result": {
    "diff": [ { "path": "solution.py", "content": "…" } ],
    "tests_passed": true, "summary": "…", "cost_usd_micros": 0, "error": null
  }
}
```
- **Terminal statuses:** `done`, `failed`, `timeout`, `cancelled`. `queued`/`running` → keep polling. An unknown/empty
  status is treated as `running` (never a false terminal). `result` is present on a terminal status.
- Poll interval `BOSS_SANDBOX_POLL_INTERVAL_SECONDS` (≥3s); overall wait `BOSS_SANDBOX_AGENT_TIMEOUT_SECONDS`.
  Timeouts are **fail-closed** (never treated as success).

### 3. Cancel — `POST /v1/sandbox/tasks/{sandbox_task_id}/cancel`
Additive (BOSS-97). Body: `{ "contract_version": "1.0", "op": "cancel", "job_id": "<id>", "reason": "…" }`.
Terminal status becomes `cancelled`.

### 4. Heartbeat — `POST /v1/sandbox/tasks/{sandbox_task_id}/heartbeat`
Additive (BOSS-97). Body: `{ "contract_version": "1.0", "op": "heartbeat", "job_id": "<id>" }`.
A missed heartbeat / exceeded timeout is fail-closed (`timeout`), never a silent success.

### 5. Release — `DELETE /v1/sandbox/tasks/{sandbox_task_id}`
Called on **success** to free capacity. **Failed VMs are intentionally NOT released** — the service
retains the rootfs for inspection. Bounded retention for a rootfs-backed preview:
`POST /v1/sandbox/tasks/{id}/hold {ttl_seconds, reason}` and `POST /v1/sandbox/tasks/{id}/preview {ttl_seconds, command}`.

JSON Schema for the shared job envelope: `engine/boss_engine/contracts/go_job_v1.schema.json`.
Python client: `boss_engine.services.go_job_contract.GoJobClient` (raises `GoServiceUnset` when the URL is empty).

## Conformance

`validate_submit_request(body)` returns the contract violations in a submit body (empty = valid) — a Go
implementation can be checked against the same fixtures. Tests: `apps/api/tests/test_sandbox_contract.py`.

## What's Python vs Go

Python owns the contract + the fallback local provider (OSS single-node). The Go service owns VM-pool
density, warm-pool/snapshot-resume, per-repo rootfs cache, and success-release/failure-retain (the scale
substrate, S1). The **async submit/poll shape here is what makes a single Python worker non-blocking** —
it submits and polls rather than holding a connection open for the whole multi-minute execution.
