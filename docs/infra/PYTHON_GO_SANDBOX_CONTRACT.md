# Python ⇄ Go sandbox job contract (v1.1)

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
- **`contract_version`:** `1.1` (sent on every submit; the service rejects an unknown version). `1.0` is the **frozen legacy lane** (below).
- **In-repo Go S1:** `services/sandbox-service/` implements this contract as a **fail-closed scaffold**. It does not boot Firecracker. When `BOSS_MICROVM_CLAIMS_FROZEN=true`, coding submit/preview return **422** `{reason: "microvm_claims_frozen"}`. Unfrozen-but-no-backend returns **422** `{reason: "firecracker_not_enabled"}`. Health never sets `ok: true` unless a live Firecracker backend exists (this scaffold never does).
- **Lease / heartbeat (additive):** `POST /v1/sandbox/tasks/{id}/heartbeat` extends the job lease. Expired leases become `status=timeout` with `reason=lease_expired` — never a silent drop. Success `DELETE` releases; failed jobs are retained.

## Operations

### 1. Submit — `POST /v1/sandbox/tasks`
Request body (built by `build_submit_request`):
```json
{
  "contract_version": "1.1",
  "task_id": "crun_...",
  "tenant_id": "default",
  "repo": { "owner": "paramg", "name": "boss-sandbox", "base_branch": "main", "work_branch": "boss/code/crun_..." },
  "instructions": "…", "acceptance_criteria": ["…"], "test_commands": ["pytest -q"],
  "engine": "claude_code|openai|native|…",
  "handles": { "egr": "boss_egr_…", "mdl": "boss_mdl_…", "mcpgw": "boss_mcpgw_…", "run": "boss_run_…" },
  "endpoints": { "model": "http://127.0.0.1:7101", "git": "http://127.0.0.1:7102", "mcp": "http://127.0.0.1:7103",
                 "run_plane": "http://127.0.0.1:7104", "connect": "http://127.0.0.1:7105" },
  "mcp": { "servers": { "jira": "http://127.0.0.1:7103/mcp/jira" } },
  "run_mutation": false,
  "resources": { }
}
```
Response: `{ "sandbox_task_id": "<id>" }`.

## v1.1 — no credential in the body (P8b S9, BOSSAPP-1070)

**What changed.** `git_token` and `engine_env` are gone. A v1.0 body carried the run's GitHub token
and model credential INTO the guest (boss-agent-hosts-lld.md §1.2, defects 1–3); P8-pre closed the
worst leaks and P8b removes the fields. The guest now holds **handles only**:

| Field | What it is | Who issues it | Redeemed at |
|---|---|---|---|
| `handles.egr` | `boss_egr_…` | `identity/runs.issue_egress_credential` | boss-egress (git smart-HTTP, REST) through `boss-hostd` port 7102 |
| `handles.mdl` | `boss_mdl_…` bound to `(run, fence_epoch, vm_id)` | `streaming_model_gateway.issue_handle` (`guest_handles`) | the central model gateway through `boss-hostd` port 7101 (HOST_API.md §4.6) |
| `handles.mcpgw` | `boss_mcpgw_…` | the MCP gateway | the MCP gateway through port 7103 |
| `handles.run` | the run handle | the run plane | port 7104 |
| `endpoints` | the guest-loopback 71xx URLs `boss-vsockd` bridges (LLD §3.8); fixed by the host image, sent so a guest never guesses a port | `DEFAULT_ENDPOINTS` | — |
| `mcp.servers` | `{name: url}` of the MCP gateway routes the run may use; the credential is `handles.mcpgw`, never here | the control plane | — |

A handle is worthless off its VM: the only path that redeems it is that VM's own vsock socket on
its own host, and the gateway at the far end binds it to the run, its lease epoch and the VM.

**What both validators refuse** (`validate_submit_request` in Python; `ValidateSubmitWith` in Go,
same reasons and wording, field NAMES only, never a value):

| Reason | When |
|---|---|
| `raw_credential_field` | a v1.1 body carries `git_token`, `engine_env`, `token`, `claude_oauth_token`, `anthropic_api_key`, `openai_api_key`, `azure_key`, `github_token`, `gh_token`, `api_key` or `password` (present at all, even empty), or a `handles.<kind>` that does not carry its `boss_<kind>_` prefix |
| `secret_shaped_value` | **strict hosts only:** a field named like a secret (`env_refs._SECRET_NAME`'s shape — TOKEN, SECRET, PASSWORD, PASSPHRASE, `_PAT`, API_KEY, ACCESS_KEY, PRIVATE_KEY, CREDENTIAL — minus the bare `_KEY` that ids such as `dedupe_key` use) holds a value, or a well-known key shape (`sk-ant-…`, `sk-…`, `ghp_…`/`github_pat_…`, `glpat-…`, `xox…-`, `AKIA…`/`ASIA…`, a JWT) appears anywhere in the body |
| `contract_legacy_refused` | a v1.0 body outside the legacy lane, or on a `strict_secrets` host |
| `contract_deprecated` | not a refusal: the log line a v1.0 body earns on the legacy lane |

Go answers **422** with the reason (`contract.RefusalReason`); Python raises `RawCredentialRefused`
before submitting (`ensure_no_raw_credential`, called by `MicroVMProvider._submit_body`).

**Switches.** Every microVM host is `strict_secrets` by default (Go `contract.OptionsFromEnv`:
`BOSS_STRICT_SECRETS` defaults on). The **legacy lane** is `BOSS_SANDBOX_CONTRACT_LEGACY=1` on both
sides: Python then builds a v1.0 body (`build_submit_request(..., legacy=True)`, with the P8-pre
`engine_env` narrowing and refusals) and logs `contract_deprecated`; Go accepts it only with strict
secrets off. The lane is frozen: nothing new lands on it.

**Where the model handle comes from.** `MicroVMProvider._submit_body` asks
`streaming_model_gateway.guest_handles` for a `boss_mdl_` when the spec names a project (the key
is read by the gateway through a *reference* — `secret_ref:<id>`, `llm_gateway:<id>` or
`env:<NAME>` — never carried). For a deployed agent's run the run plane issues it when the host
claims the run (`run_plane._lease_one` → the work item's `handles`, HOST_API.md §4.5). The
`RELAY_URL_VARS` exemption in `env_refs` is gone with v1.1: no BOSS relay carries a credential any
more, so a host credential leaving for the sandbox-service URL is refused like any other misdirection.

**Guest spec (Go `GuestSpec`).** A v1.1 spec carries `handles`, `endpoints`, `mcp` and
`contract_version`, and none of `token` / `claude_oauth_token` / `openai_api_key` / `azure_key`.
The in-guest agent points `ANTHROPIC_BASE_URL` / `OPENAI_BASE_URL` at `endpoints.model` with
`handles.mdl` as the key, and git's `url.insteadOf` at `endpoints.git` with `handles.egr`
(the guest-side wiring is the Go/guest agent's work: HOST_API.md §4.6, "what the guest does").

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

`validate_submit_request(body, strict_secrets=True, allow_legacy=None)` returns the contract violations in a
submit body (empty = valid) — a Go implementation can be checked against the same fixtures
(`services/sandbox-service/testdata/submit_valid.json` is v1.1; `submit_legacy_v10.json` the frozen lane).
Tests: `apps/api/tests/test_sandbox_contract.py`, `engine/tests/test_p8pre_microvm_credential_fixes.py`,
`engine/tests/test_p8b_model_gateway.py` (the Python⇄Go rule parity), Go `internal/contract`.

## What's Python vs Go

Python owns the contract + the fallback local provider (OSS single-node). The Go service owns VM-pool
density, warm-pool/snapshot-resume, per-repo rootfs cache, and success-release/failure-retain (the scale
substrate, S1). The **async submit/poll shape here is what makes a single Python worker non-blocking** —
it submits and polls rather than holding a connection open for the whole multi-minute execution.

## Execution backends (BOSSAPP-824, additive to v1.0)

`services/sandbox-service` now EXECUTES admitted submits when started with a backend
(`BOSS_SANDBOX_BACKEND=process|firecracker`); with no backend it remains the fail-closed scaffold
described above. Both backends run the same in-guest pipeline (`sandbox-host/guest/in_guest_agent.py`,
embedded in the binary with a sync test):

- **process** — the guest pipeline as a local subprocess in a per-task workdir. No isolation claim; it
  exists so this contract is drivable on any machine/CI (`engine/tests/test_sandbox_service_conformance.py`
  builds the binary and drives the real Python client through submit → status → result → release).
- **firecracker** — one microVM per task: kernel + per-task rootfs copy + an ext4 work drive
  (`mke2fs -d` in, `debugfs` out — no host mounts). Readiness failures 422 with a message naming the
  missing piece. Operator runbook: `docs/infra/MICROVM_ENABLE.md`; host proof: `scripts/sandbox/microvm-e2e.sh`.

Additive surface shipped with the backends (all conforming to the Python side):

- `test_mode` is now forwarded by `build_submit_request` (the Go `SubmitRequest` always typed it);
  `engine=echo` always runs as an offline test_mode diagnostic — no repo, network, or credentials.
- `cancelled` is a terminal status on the Go side too (`POST /v1/sandbox/tasks/{id}/cancel`).
- Inspection of a retained workspace: `GET /v1/sandbox/tasks/{id}/fs?path=/work/...` (tree + file reads)
  and `GET /v1/sandbox/tasks/{id}/logs` (serial console / guest agent log) — what `sandbox_console.py`
  proxies for the console; `hold` accepts both `{ttl_seconds}` and the console's `{hours}`.
- Auth: `X-Sandbox-Token` (canonical) or the same secret as `Authorization: Bearer`.
- Poll `result` additionally carries the guest's full shape (`status`, `diff_summary`, `tests`, `usage`,
  `turns`, `engine`) that `MicroVMProvider.run_code_task` maps into a `CodeAgentResult`.
