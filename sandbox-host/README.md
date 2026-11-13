# BOSS Sandbox Host

The execution-plane side of the coding-agent (architecture §5.3/§5.4). The
**control plane** (BOSS API/worker) dispatches a task spec here; this host
launches an **isolated, single-tenant sandbox**, runs the coding pipeline inside
it, and returns **metadata + the diff only** — source never leaves the sandbox.

## Layout
- `guest/Dockerfile` — the sandbox image. Container today (`docker run`); the
  **same image** becomes the microVM rootfs under Kata/Firecracker later.
- `guest/in_guest_agent.py` — runs **inside** the sandbox: clone → coding agent
  (`echo` stand-in or `claude_code`) → authoritative tests → `git diff` → result.
- `sandbox_agent.py` — runs **on the host**: an HTTP API the control plane calls;
  a pluggable backend launches the sandbox (`ContainerBackend` now).

## Backend swap (no control-plane changes)
| Backend | How | When |
|---|---|---|
| `ContainerBackend` (`docker run`) | today — works on macOS Docker | stand-in / dev |
| same + `--runtime kata-fc` | real microVM via Kata + containerd | Phase D, on a Linux KVM host |
| `FirecrackerBackend` | raw Firecracker + jailer + snapshots | Phase D, perf/warm-pool |

The control plane only ever calls `POST /v1/sandbox/run-task`; swapping the
backend (container → microVM) is a host-side change.

## Run locally (stand-in)
```bash
# 1) build the guest image
docker build -t boss-sandbox-guest:latest -f guest/Dockerfile guest/

# 2) start the sandbox agent (host service)
BOSS_SANDBOX_IMAGE=boss-sandbox-guest:latest \
  uvicorn sandbox_agent:app --host 0.0.0.0 --port 8088

# 3) the control plane calls it (MicroVMProvider) — or test directly:
curl -s localhost:8088/v1/sandbox/run-task -H 'content-type: application/json' \
  -d '{"tenant_id":"boss-dogfood","spec":{"engine":"echo","test_mode":true,
       "test_commands":["python3 -c \"print(1)\""]}}'
```

## Status
- ✅ Round-trip proven: control plane → container → in-guest agent → result.
- ⬜ Next: toolchains (JDK/Maven, Node…) + the `claude` agent in the guest image;
  `MicroVMProvider` HTTP client + `code_task_service` guest-dispatch path;
  per-VM egress allowlist + scrubbing proxy; then the Kata backend on a KVM host.

## Deploy target
On a Linux KVM host (Kata + containerd). Control plane stays wherever BOSS runs;
it reaches this agent over HTTPS/mTLS.
