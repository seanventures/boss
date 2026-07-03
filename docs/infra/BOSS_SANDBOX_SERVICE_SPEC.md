# BOSS Sandbox Service — Integration Spec (goldeneye)

**Audience:** Sean (goldeneye platform) · **From:** BOSS control‑plane team
**Date:** 2026‑07‑02 · **Status:** security foundation done (3/6); this doc unblocks the remaining 3 stages.

---

## 0. Context & status

- **goldeneye is bare‑metal with KVM** — confirmed via boss‑api's `/v1/diag/host-capabilities` probe. (boss‑api itself is intentionally zero‑privilege: uid 100, `CapEff=0x0`, no `/dev/kvm`, no `docker.sock`. The sandbox privilege belongs to a **separate service**, never boss‑api.)
- **Sean's security foundation is complete and proven (3/6 stages), all on goldeneye:**
  1. ✅ **Boot** — Firecracker micro‑VMs run.
  2. ✅ **Isolation** — untrusted guest gets internet‑only egress; hard‑blocked from tailnet, LLM gateway, docker networks, host, and every internal service (verified from *inside* a guest).
  3. ✅ **Jailer + quotas** — VMs run jailed as an unprivileged user, chrooted, with cgroup CPU/mem/pids caps + seccomp.
- **Remaining 3 stages** (management service, rootfs bake, BOSS wiring) — Sean builds the service + rootfs; **BOSS does the wiring** (see §7). This doc provides the specs Sean asked for.

There are **two workloads** for the sandbox (see §2 and §6):
- **A. Code‑task execution** — run BOSS's coding pipeline against a repo in isolation, return a diff.
- **B. Persistent app previews** — long‑lived, routable sandboxes hosting BOSS‑built customer apps.

---

## 1. Architecture — the hybrid split

The **guest writes code and runs tests**; **BOSS keeps governance + PRs on the control plane.** This keeps the DB and the LLM gateway on the control plane (which the guest is hard‑blocked from) and keeps *source* inside the guest.

```
  Control plane (boss-api + worker, existing box)        goldeneye host
  ┌───────────────────────────────────────────┐  HTTPS  ┌──────────────────────────────┐
  │ MicroVMProvider ──────────────────────────┼────────►│ sandbox-service (privileged) │
  │  submit task / poll / release              │  mTLS   │  → boot jailed Firecracker VM│
  │                                            │         │  → dispatch spec via vsock   │
  │ Governance (on the RETURNED diff):         │◄────────┤  → collect result via vsock  │
  │  risk-tier · review/security panel ·       │ result  │  micro-VM guest:             │
  │  quality gates · Decision · open PR        │         │   clone→agent→tests→mutation │
  └───────────────────────────────────────────┘         └──────────────────────────────┘
```

**Guest responsibilities (per code task):** clone → run the code‑agent edit/test loop → run the authoritative test commands → (optional) mutation testing → return `{summary, diff, tests, mutation, cost}`.
**Guest does NOT:** push commits, open PRs, or write to any BOSS database.
**Control‑plane responsibilities:** risk‑tiering, the adversarial review/security panel, quality‑gate evaluation, the merge Decision, and opening the PR **from the returned diff**.

**Security consequence:** the guest's git token can be **clone/read‑only**. The PR‑write credential never leaves the control plane.

---

## 2. Workload A — code‑task API contract (async + poll)

Code tasks run ~10 minutes, so the contract is **async submit + poll**, not a long sync call. VMs are **single‑tenant** (one VM only ever serves one tenant; destroyed after).

### Submit — `POST /v1/sandbox/tasks`  (Acquire + Run)
```jsonc
{
  "task_id": "crun_...",              // BOSS-provided idempotency key
  "tenant_id": "project_id",          // single-tenant pin
  "repo": { "owner": "...", "name": "...",
            "base_branch": "...", "work_branch": "boss/code/..." },
  "git_token": "<short-lived, CLONE/READ scope only>",
  "instructions": "...",              // the agent prompt/spec
  "acceptance_criteria": ["..."],
  "test_commands": ["pytest -q", "..."],
  "engine": "claude_code|openai|azure",
  "engine_env": {                     // passed per-task, never baked into the image
    "CLAUDE_CODE_OAUTH_TOKEN": "...",  // for engine=claude_code
    "OPENAI_API_KEY": "...",           // or AZURE_* for native
    "OPENAI_BASE_URL": "..."           // model-agnostic (OpenAI/Grok/Together/…)
  },
  "run_mutation": true,
  "resources": { "vcpu": 2, "mem_mb": 4096, "disk_gb": 10, "timeout_s": 2700 }
}
```
→ `202 { "sandbox_task_id": "...", "status": "queued" }`

### Poll — `GET /v1/sandbox/tasks/{sandbox_task_id}`
```jsonc
{
  "status": "queued|running|done|failed|timeout",
  "phase":  "provisioning|writing|testing|mutation",   // drives the BOSS UI stepper
  "result": {                                          // present when status=done
    "summary": "...",
    "diff": [ { "path": "app.py", "content": "<full file>" } ],  // FileChange[]
    "tests": { "passed": true, "report": "..." },
    "mutation": { "score": 0.0, "survivors": ["..."] },
    "cost_usd_micros": 0,
    "logs_tail": "..."
  },
  "error": "..."                                       // present when failed/timeout
}
```

### Release — `DELETE /v1/sandbox/tasks/{sandbox_task_id}`
Snapshot/hibernate or destroy the VM. (Lifecycle is task‑scoped: clean per task, reused across that task's iterations, destroyed on close.)

**Live log‑tail:** nice‑to‑have, not required for v1. If cheap, `GET /v1/sandbox/tasks/{id}/logs` (SSE). Otherwise the `phase` field + `logs_tail` on poll is enough.

**Auth:** mTLS **or** a shared `X-Sandbox-Token` header — control‑plane → service only.

---

## 3. Guest runtime (rootfs) + per‑task sizing

We already have the exact rootfs as an OCI image — **`sandbox-host/guest/Dockerfile` → registry `goldeneye.tail95d980.ts.net:5000/boss-sandbox-guest`.** Bake the micro‑VM rootfs from it.

**Contents:** Debian slim + **Python 3.12, Node 20, JDK 17 / Maven 3.9**, `git`, `curl`, `ca-certificates`, package managers (`pip`, `npm`, `mvn`), and the **`claude` CLI** (`@anthropic-ai/claude-code`, authenticated via `CLAUDE_CODE_OAUTH_TOKEN` **passed per task, never baked**). The native engine (`engine=openai|azure`) works with no CLI — it calls the model API directly over HTTPS. Add Go / .NET here as coverage grows.

**Per‑task sizing (defaults, overridable via `resources`):** **2 vCPU · 4 GB RAM · 10 GB disk · 2700 s (45 min) wall‑clock.**

**Optional (v2):** per‑`(tenant, repo)` cache volume for `~/.m2`, `~/.gradle`, npm/pip caches → warm builds.

---

## 4. Egress — internet‑only is fine for code tasks

Code tasks need only **internet** endpoints:
- **GitHub** (clone) — internet ✓
- **Model API** — **public** endpoints only: `api.anthropic.com` (claude CLI) or the Azure/OpenAI public endpoint. **Not** BOSS's internal LLM gateway. ✓
- **Package mirrors** — pip / npm / Maven Central — internet ✓

They do **not** need to reach any internal BOSS service. Results return over the host↔guest channel (vsock) in the poll response. **No allowlist hole required.** (If we ever route models through an internal gateway, we'll request a narrow proxied allowlist for just that endpoint — not needed now.)

---

## 5. Reference design in the repo

Match these (they are the concrete contract, already written):
- `sandbox-host/sandbox_agent.py` — the host/management service (has a pluggable backend: `ContainerBackend` today → **Firecracker/Kata** for real micro‑VMs).
- `sandbox-host/guest/in_guest_agent.py` — the in‑guest agent (clone → agent loop → tests → diff).
- `sandbox-host/guest/Dockerfile` — the guest rootfs (see §3).
- `apps/api/app/services/sandbox_provider.py` → `MicroVMProvider` — the control‑plane client that calls the service.

The security‑critical **VM boot** (Firecracker + jailer + TAP/nftables + vsock) is the layer Sean has built and proven — the service just needs that boot primitive behind the `RunTask` path.

---

## 6. Workload B — persistent app previews

Separate from code tasks. Today BOSS‑built customer apps run as **subprocesses inside boss‑api** → they overload the container and their ports are TTL‑evicted, so we can't keep demo apps up. We need **long‑lived, routable sandboxes**:

- `POST /v1/sandbox/apps { app_id, image_or_build, run_cmd, env, port } → { url }`
- `DELETE /v1/sandbox/apps/{app_id}`

**Requirements:** stays up (no auto‑destroy), **stable routable URL per app** (Traefik or the service exposing a port), **reservable** (not evicted mid‑demo).

**Isolation:** these are *our* built apps (semi‑trusted), so isolation can be **lighter** than code tasks — a plain container may suffice vs a full micro‑VM. Your call.

---

## 7. Division of work & handoff

**Sean builds (needs host root + the proven security layer — cannot be done off‑host):**
1. The **management service** exposing §2 (and §6) — wrapping the proven Firecracker + jailer + TAP/nftables + vsock boot.
2. **Bake the rootfs** from `boss-sandbox-guest` (§3).
3. Provide the endpoint URL + auth (mTLS cert or `X-Sandbox-Token`).

**BOSS picks up once the service is live (see §8):** the remaining wiring on our side.

---

## 8. Remaining wiring — BOSS side (do this once Sean's service is up)

The moment the sandbox‑service endpoint is reachable, remaining work is small and self‑serve:

1. **Config flip (self‑serve via ops API):** `BOSS_SANDBOX_PROVIDER=microvm` + `BOSS_SANDBOX_AGENT_URL=<service-url>` (+ the `X-Sandbox-Token`/mTLS material).
2. **`MicroVMProvider` → the async contract:** submit (`POST /tasks`) → poll (`GET /tasks/{id}`) → release (`DELETE`), mapping the result into BOSS's `CodeTaskResult`.
3. **Hybrid‑split refactor of the governed pipeline:** governance stages (review/security/quality‑gate) read the **returned diff** instead of a live host sandbox; **mutation runs in‑guest** and its evidence comes back in `result.mutation`.
4. **Previews → `/v1/sandbox/apps`:** point `preview_service` at the persistent‑app endpoint (Workload B) instead of in‑container subprocesses.
5. **Validate end‑to‑end:** run a real code task through a micro‑VM, confirm the diff returns, governance runs on the control plane, and the PR opens through the existing merge gate — then cut previews over.

> Nothing in §8 needs Sean. It's provider config + a control‑plane refactor we own. We'll pick it up as soon as the service is deployed and reachable.
