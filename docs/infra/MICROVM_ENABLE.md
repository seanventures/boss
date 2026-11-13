# Enabling the Firecracker microVM lane (BOSSAPP-823/824)

How an operator turns the microVM execution lane ON for one BOSS install, what
the diagnostics must show at each step, and how to roll back. The freeze
(`BOSS_MICROVM_CLAIMS_FROZEN=true`) **stays the default**: nothing in this
runbook changes defaults — enabling is always an explicit, reversible act.

The lane has two halves:

1. **Host side** — the Go `sandbox-service` (`services/sandbox-service/`) on a
   Linux x86_64 KVM host, with the `firecracker` backend. Proven standalone by
   `scripts/sandbox/microvm-e2e.sh`.
2. **Engine side** — the BOSS control plane dispatching tasks to it over the
   v1.0 contract (`engine/boss_engine/services/sandbox_contract.py`).

## 0. What the firecracker backend needs on the host

| Artifact | How it gets there | Config |
|---|---|---|
| `/dev/kvm` (rw for the service user) | host kernel / `usermod -aG kvm <user>` | — |
| `firecracker` binary (pinned release, sha256-verified) | `scripts/sandbox/microvm-e2e.sh` step 2 | `BOSS_SANDBOX_FC_BIN` |
| Uncompressed kernel `vmlinux` (Firecracker CI 5.10 line) | `scripts/sandbox/microvm-setup.sh` | `BOSS_SANDBOX_FC_KERNEL` |
| Rootfs `rootfs.ext4` — Alpine + python3/git/node + `/opt/boss/in_guest_agent.py` + `/sbin/boss-init` | `scripts/sandbox/microvm-setup.sh` (docker export + `mke2fs -d`, no sudo) | `BOSS_SANDBOX_FC_ROOTFS` |
| e2fsprogs (`mke2fs`, `debugfs`) | distro package | — |

Per task the service copies the rootfs (sparse), builds a small ext4 "work"
drive carrying `spec.json`, boots one VM (`firecracker --no-api`), and reads
`result.json` back with `debugfs` — the host never mounts guest images and
needs no root. Guest networking (needed only for real clones / `claude_code`;
`echo` diagnostics run offline) is opt-in via `BOSS_SANDBOX_FC_TAP` +
`BOSS_SANDBOX_FC_GUEST_IP` + `BOSS_SANDBOX_FC_GATEWAY_IP` and a host tap/NAT
you provision. The `claude` CLI is **not** baked into the rootfs yet; guest
engines `echo` and `openai` work today, `claude_code` needs a rootfs bake with
the CLI first.

## 1. Prove the host side (no BOSS config touched)

On the KVM host (e.g. boss-1), from a checkout of this repo:

```bash
scripts/sandbox/microvm-e2e.sh            # artifacts land in ~/.boss/sandbox
```

All 7 steps must print `PASS`, ending with a real microVM task:
`submit → done → SANDBOX_OK.txt → release → 404`. The script sets
`BOSS_MICROVM_CLAIMS_FROZEN=false` **only for the host service process it
starts**; the engine default is untouched.

Then run it as a persistent service (same env the e2e used):

```bash
BOSS_SANDBOX_LISTEN=0.0.0.0:8078 \
BOSS_SANDBOX_TOKEN=<strong shared secret> \
BOSS_SANDBOX_BACKEND=firecracker \
BOSS_SANDBOX_FC_BIN=~/.boss/sandbox/firecracker-v1.10.1 \
BOSS_SANDBOX_FC_KERNEL=~/.boss/sandbox/vmlinux \
BOSS_SANDBOX_FC_ROOTFS=~/.boss/sandbox/rootfs.ext4 \
BOSS_SANDBOX_WORK_ROOT=~/.boss/sandbox/work \
BOSS_MICROVM_CLAIMS_FROZEN=false \
  ~/.boss/sandbox/sandbox-service
```

`GET /v1/sandbox/health` must show:

```json
{ "ok": true, "backend": "firecracker", "firecracker_live": true,
  "execution_ready": true, "claims_frozen": false, "reason": "" }
```

If a piece is missing, `ok` is `false` and `reason` **names it** (e.g.
`firecracker backend: kernel image missing at …`). Fix what it names; never
force past it.

## 2. Point the engine at it (one install / project)

In the BOSS API/worker environment (`app.env` on the install):

```bash
BOSS_MICROVM_CLAIMS_FROZEN=false            # the explicit unfreeze — engine side
BOSS_GOVERNED_SANDBOX_PROVIDER=microvm      # governed pipeline working stages
BOSS_SANDBOX_PROVIDER=microvm               # classic code.task path (optional, same lane)
BOSS_SANDBOX_AGENT_URL=http://<host>:8078   # the sandbox-service above
BOSS_SANDBOX_TOKEN=<same shared secret>     # sent as X-Sandbox-Token (Bearer also accepted)
```

Recreate the containers (`docker compose up -d --force-recreate api worker` —
a plain restart keeps stale env). Note the committed-choice comment in
`governed_code_task_service._resolve_use_microvm` still applies: a per-run
`spec.substrate` override always wins, so a broken microVM path can be
retried onto `local` without any config change.

## 3. Verify through the API before trusting it

1. `GET /v1/diag/host-capabilities` — for microVMs **on the API host itself**
   the verdict needs `kvm.openable_rw=true` + `has_CAP_SYS_ADMIN=true`; when
   the sandbox-service runs on a **separate** KVM host (the normal shape,
   e.g. boss-1), this endpoint documents that the API container itself cannot
   boot VMs — that is expected and fine.
2. `GET /v1/diag/sandbox-roundtrip` — must return
   `{"reached": true, "final_status": "done", "result_diff_files": ["SANDBOX_OK.txt"]}`.
   While frozen it refuses with 422 `microvm_claims_frozen` (by design);
   missing host URL is a 503.
3. Console → sandbox host panel (`list_sandboxes`): `host.connected=true`,
   `firecracker_live=true`, `claims_frozen=false`.

Only after all three: run one governed build and watch the stages execute on
the microVM (the run records the `sandbox_task_id`; `fs`/`logs` on
`/v1/sandbox/tasks/{id}` browse the retained workspace of a failed task,
which is kept ~2h — extend with `hold`).

## 4. Rollback

Set the freeze back and recreate:

```bash
BOSS_MICROVM_CLAIMS_FROZEN=true
# optionally also: BOSS_GOVERNED_SANDBOX_PROVIDER=local, BOSS_SANDBOX_PROVIDER=local
```

With the freeze on, `make_sandbox_provider` routes new runs to the local
subprocess substrate even if the provider still says `microvm`, direct microVM
calls 422 with `microvm_claims_frozen`, and the host service (if left running)
also refuses coding submits while its own freeze flag is true. In-flight
sandbox tasks finish or time out on the host; nothing needs manual cleanup
(`DELETE /v1/sandbox/tasks/{id}` frees a retained one early).

## Troubleshooting map

| Symptom | Meaning | Fix |
|---|---|---|
| 422 `microvm_claims_frozen` | freeze still on (engine or host service) | step 2 env + recreate; host service env |
| 422 `firecracker_not_enabled` + message | backend piece missing — message names it | step 0 table |
| 503 `sandbox_host_unavailable` | URL unset / service down / network | start service, check `BOSS_SANDBOX_AGENT_URL` |
| 401 on submit | token mismatch | same `BOSS_SANDBOX_TOKEN` both sides |
| task `failed`, error "clone failed" | guest has no network for a real repo | tap/NAT (step 0) or run `test_mode` diagnostics |
| task `timeout` | wall-clock budget hit | raise `timeout` in the submit / `BOSS_SANDBOX_MAX_TIMEOUT_SECONDS` |
