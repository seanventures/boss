# BOSS Sandbox — Inspection Session Spec (micro‑VM browse/exec console)

**Goal:** restore the old Sandboxes console experience (browse the working tree, view files, run governed commands, see telemetry) — but for the **Firecracker micro‑VM** substrate.

---

## 0. Why this needs a new design (not just re‑pointing the old console)

The old console browsed **persistent containers** that lived a while, with a rich agent API (`/v1/sandboxes`, `/tree`, `/file`, `/exec`, `/stats`). Micro‑VMs are different:
- **Ephemeral & single‑tenant** — booted per task, destroyed on completion (45‑min TTL). There's usually **no live VM** sitting around to browse.
- **One‑shot channel** — the service injects `/work/spec.json`, runs the guest agent, reads `/work/result.json` back over serial. There is **no interactive exec channel** today.
- **Hard‑isolated** — internet‑only egress; blocked from tailnet/LLM‑gateway/host/internal. Any inspection must preserve that.

So "same experience" = a real, small feature: an **on‑demand inspection session**.

---

## 1. The model — an on‑demand inspection session

From the console, **"Inspect run X"** (or "Inspect a branch"):
1. The management service **boots an ephemeral inspection VM**, clones the run's branch into it (the same rootfs image), and holds it open (no agent runs — it's for humans).
2. BOSS opens a **governed browse/exec session** against it: tree, file view, `exec` (helper allowlist + break‑glass), telemetry.
3. The VM is **single‑tenant, TTL'd (e.g. 15 min idle), destroyed on close** — same isolation as a task VM.

Two entry points:
- **Inspect a run** — clone `boss/code/{root_run_id}` (or a run's branch) for post‑hoc debugging ("why did create‑ticket break?").
- **Attach to a live task** *(optional, later)* — if a task VM is currently running, attach a read‑mostly session to it. v1 can skip this; the "boot a fresh inspection VM on the branch" path covers the debugging need.

---

## 2. API contract — management service (Sean adds these)

All under the existing base (`http://10.100.9.1:8077`), `X‑Sandbox‑Token` gated, control‑plane‑only.

```
POST   /v1/sandbox/inspect
  body: { tenant_id, repo:{owner,name,base_branch}, branch, git_token, ttl_s?:900,
          resources?:{vcpu,mem_mb,disk_gb} }
  → 201 { inspect_id, status:"booting|ready", branch }

GET    /v1/sandbox/inspect/{id}
  → { inspect_id, status:"booting|ready|expired|destroyed", tenant_id, branch,
      age_seconds, resources }

GET    /v1/sandbox/inspect/{id}/tree            → { tree: string[] }          # rel paths, dirs end with /
GET    /v1/sandbox/inspect/{id}/file?path=...   → { content, truncated?:bool } # UTF-8, size-capped
POST   /v1/sandbox/inspect/{id}/exec
  body: { command, timeout_s?:60 }
  → { stdout, stderr, exit_code, duration_ms, timed_out }

GET    /v1/sandbox/inspect/{id}/stats           → { cpu_pct, mem_usage, mem_pct, pids, block_io,
                                                    oom_killed, container_status, exit_code }
DELETE /v1/sandbox/inspect/{id}                 → { destroyed:true }
```

**Notes for Sean:**
- `exec` runs the command **inside the inspection VM's working tree** (`cwd=/work/repo`), returns captured output. The VM keeps its egress isolation — a command *can* reach the internet (pip/git) but **not** any internal BOSS/host service (unchanged from task VMs).
- The service is a **dumb executor** here — it does **not** enforce the command allowlist or break‑glass. **BOSS enforces governance on the control plane** (see §3) and only forwards commands it has authorized. This keeps the security policy in one place (BOSS) and the VM simple.
- `tree`/`file` can be implemented as canned `exec` calls (`find`, `cat`) if that's easier than dedicated handlers — your call.

---

## 3. Governance — enforced on the BOSS control plane (I own this)

The old console's safety model is preserved, and it lives in BOSS, not the VM:
- **Helper allowlist** — read‑only/diagnostic commands (`git status`, `ls`, `cat`, `mvn -q test`, …) run freely.
- **Break‑glass** — anything outside the allowlist (raw shell, `env`, network mutation) requires an explicit **reason** and emits an **elevated audit event** (`sandbox.exec.break_glass`).
- Every `exec`, file read, and session open/close is **audited** (`emit_event`) with actor + tenant + inspect_id.
- The control plane checks the command against the allowlist **before** forwarding to the service's `/exec`. The VM never sees a command BOSS didn't authorize.

So: **Sean's `/exec` is unconditional; BOSS gates it.** (If Sean prefers, we can *also* pass a `break_glass` flag through for defense‑in‑depth, but BOSS is the enforcement point.)

---

## 4. Guest / rootfs (small addition — I write it, ships in `boss-sandbox-guest`)

The inspection VM boots the same rootfs but runs **no agent** — it just needs to stay up and accept exec. Two ways, Sean picks what fits his boot flow:
- **(a) Serial/vsock exec loop** — a tiny `in_guest_inspect.py` that reads a command from the channel, runs it in `/work/repo`, returns `{stdout,stderr,exit_code}`. I'll add this to the guest image.
- **(b) SSH‑less exec via the host** — if the service can already `exec` into the VM (e.g. via its VMM/agent channel), no guest change is needed and (a) is skipped.

Either way the guest change is minimal and I own it; it does not affect the task‑execution path.

---

## 5. Control‑plane + UI (I own this)

- `apps/api/app/services/sandbox_console.py` — repoint `get_sandbox/tree/file/exec/stats/destroy` at the new `/v1/sandbox/inspect/*` endpoints, **with the `X‑Sandbox‑Token`**, plus a `create_inspection(run_id)` that resolves the run's branch and calls `POST /inspect`. Enforce the allowlist + break‑glass + audit here.
- `apps/api/app/main.py` — a `POST /v1/code-tasks/{run_id}/inspect` (and the existing `/v1/sandboxes/{id}/…` routes repointed) so the console can start a session from a run.
- `apps/web/app/sandboxes-ui.tsx` — **the browse/file/console/telemetry UI already exists** (`SandboxDetail`, `Console`, `Telemetry`). Wire it to the session lifecycle: "Inspect" button on a completed run → session → the existing detail view → "Close" destroys it.

---

## 6. Division of work

| Piece | Owner |
|---|---|
| Boot an inspection VM on a branch; hold it (TTL); destroy | **Sean** (VM lifecycle) |
| `/v1/sandbox/inspect/*` endpoints (tree/file/exec/stats) | **Sean** (service) |
| Exec channel into the guest (or reuse his VMM exec) | **Sean** (+ my tiny guest helper if needed) |
| Guest inspect helper (`in_guest_inspect.py`) — optional | **BOSS/me** |
| Control‑plane proxy + allowlist + break‑glass + audit | **BOSS/me** |
| "Inspect" wiring in the console UI (view already exists) | **BOSS/me** |

---

## 7. Scope / caveats for v1
- Inspection VMs are **throwaway** — edits made in an exec session are **not** persisted or turned into PRs (this is a *debugger*, not an editor). Use Iterate‑with‑Feedback to change code.
- Boot cost ~a few seconds; a session stays warm for its TTL so subsequent commands are instant.
- "Attach to a *live* running task VM" is **out of scope for v1** (boot‑fresh‑on‑branch covers debugging). Can add later.
- Concurrency counts against the host's capacity (8) like task VMs.

---