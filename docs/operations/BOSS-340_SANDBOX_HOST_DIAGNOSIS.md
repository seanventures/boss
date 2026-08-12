# BOSS-340 — Sandbox agent host unreachable (diagnosis + fix)

**Date:** 2026-08-12  
**Plane:** BOSS-340  
**Host:** goldeneye  
**Status:** **Control-plane connectivity restored** (`host.connected: true`). Guest rootfs still missing for real microVM task execution.

---

## Symptom

```http
GET /v1/sandboxes
→ 200 {"sandboxes":[],"host":{"connected":false},"unavailable":"[Errno 111] Connection refused"}
```

Console Sandboxes surface and any path that needs the sandbox agent host could not inspect/exec live sandboxes.

---

## Root cause

`BOSS_SANDBOX_PROVIDER=microvm` and the API were correctly configured to call Sean’s **host Firecracker sandbox-service**, but that service was **not running** and its runtime tree was **gone**.

| Check | Finding |
|---|---|
| `BOSS_SANDBOX_AGENT_URL` | `http://10.100.9.1:8077` (no secrets) |
| Meaning of host | `10.100.9.1` = docker bridge gateway on goldeneye (`championsgate_internal` today). Service binds `0.0.0.0:8077`, so **any** host bridge IP works once the daemon listens. |
| Port 8077 | **Nothing listening** before fix (`Connection refused` from host and from `boss-app-api-1`) |
| systemd | `sandbox-service.service` **disabled / inactive (dead)**; journal empty |
| Runtime tree | `/srv/sandbox` **missing** (unit expects `/srv/sandbox/bin/sandbox-service.py`) |
| Source still present | `/root/projects/infra/sandbox/` (RUNBOOK, `sandbox-service.py`, bake/setup scripts) |
| Containers | No `sandbox-host` / `sandbox-agent` container; boss-app compose has api/worker/web only |
| UFW | Already allowed `8077/tcp` from `10.100.0.0/14` — **not** the firewall |
| boss-app network | `boss-app_boss` gateway is now `10.100.47.1` (URL still uses `10.100.9.1`; both work once service binds all interfaces) |

This is **not** a boss-app Docker network membership bug and not a wrong secret. The privileged host daemon simply was not installed/running.

Canonical design (from `/root/projects/infra/sandbox/RUNBOOK.md`):

- Control plane (unprivileged api/worker) → HTTP `X-Sandbox-Token` → host `sandbox-service` on **:8077**
- From containers: host bridge IP (documented as `http://10.100.9.1:8077`)
- From tailnet/ops: `http://100.66.255.78:8077`
- Runtime under `/srv/sandbox` (binaries, kernel, rootfs, token, jails)

---

## Fix applied (reversible, non-destructive)

On goldeneye as root:

1. Recreated `/srv/sandbox/{bin,kernels,rootfs,vms,jail}`.
2. Installed `/srv/sandbox/bin/sandbox-service.py` from `/root/projects/infra/sandbox/`.
3. Patched `EXT_IF` `nic1` → `ens10f0np0` (public NIC renamed; keeps public-ingress DROP for :8077).
4. Wrote `/srv/sandbox/.sandbox-token` from existing `BOSS_SANDBOX_TOKEN` in `/srv/apps/boss-app/.env` (chmod 600).
5. Created system user `sandboxvm` (uid 999) for jailer.
6. Installed Firecracker **v1.16.1** + jailer under `/srv/sandbox/bin/`.
7. Fetched guest kernel `vmlinux-6.1.102`.
8. `systemctl enable --now sandbox-service` → **active (running)** on `0.0.0.0:8077`.

**Rollback:**

```bash
systemctl disable --now sandbox-service
# optional full teardown:
# rm -rf /srv/sandbox
# userdel sandboxvm
```

**No** boss-app `.env` change, **no** compose recreate, **no** force-push, **no** `--env-template`.

---

## Live proof (2026-08-12)

```bash
# Host
curl -sS http://127.0.0.1:8077/v1/sandbox/health
# {"ok": true, "capacity": 8, "available": 8, "running": 0}

curl -sS http://10.100.9.1:8077/v1/sandbox/health
# same

# From api container
docker exec boss-app-api-1 python -c \
  'import urllib.request; print(urllib.request.urlopen("http://10.100.9.1:8077/v1/sandbox/health").read().decode())'

# Control plane
# Authorization: Bearer $BOSS_API_KEY
curl -sS -H "Authorization: Bearer $BOSS_API_KEY" \
  https://boss-api.local.sean.network/v1/sandboxes
```

**Observed:**

```json
{
  "sandboxes": [],
  "host": {
    "connected": true,
    "kind": "Firecracker micro-VM",
    "capacity": 8,
    "available": 8,
    "running": 0,
    "url": "http://10.100.9.1:8077"
  }
}
```

Also confirmed via `docker exec boss-app-api-1` → `http://127.0.0.1:8000/v1/sandboxes`.

---

## Remaining blocker (task *execution*, not connectivity)

| Asset | Status |
|---|---|
| `sandbox-service` listening | ✅ |
| Firecracker + jailer | ✅ v1.16.1 |
| Guest kernel | ✅ |
| **`/srv/sandbox/rootfs/guest-base.ext4`** | ❌ **missing** |
| Registry `boss-sandbox-guest` | Repo exists but **tags: null** (no pullable image) |
| Registry `boss-sandbox-host` | Same (unused for this path; docker.sock stand-in) |

Health/`host.connected` does **not** require a rootfs. Submitting `POST /v1/sandbox/tasks` will fail until the base rootfs is baked:

```bash
# After a tagged guest image is in the registry:
bash /root/projects/infra/sandbox/bake-rootfs.sh \
  goldeneye.tail95d980.ts.net:5000/boss-sandbox-guest:<tag>
systemctl restart sandbox-service   # optional; service already up
```

Until then, microVM code-tasks should be expected to fail at provision; console host status can still show connected.

---

## Proof commands (operator cheat sheet)

```bash
# Config (host:port only)
grep BOSS_SANDBOX_AGENT_URL /srv/apps/boss-app/.env

# Service
systemctl status sandbox-service --no-pager
ss -lntp | grep 8077
journalctl -u sandbox-service -n 50 --no-pager

# Health
curl -sS http://10.100.9.1:8077/v1/sandbox/health
docker exec boss-app-api-1 python -c \
  'import urllib.request; print(urllib.request.urlopen("http://10.100.9.1:8077/v1/sandbox/health", timeout=5).read())'

# BOSS surface
curl -sS -H "Authorization: Bearer $BOSS_API_KEY" \
  https://boss-api.local.sean.network/v1/sandboxes | jq .
```

---

## Recommendations

1. **Bake + publish** `boss-sandbox-guest` with a real tag and run `bake-rootfs.sh` so task execution works.
2. Optionally set `BOSS_SANDBOX_AGENT_URL=http://10.100.47.1:8077` (current `boss-app_boss` gateway) for clarity — not required while the service binds `0.0.0.0`.
3. Add `host.docker.internal:host-gateway` + documented URL as a longer-term compose improvement so bridge renumbering cannot stale the URL.
4. Keep `sandbox-service` **enabled** so reboots restore connectivity; include `/srv/sandbox` in host config/runtime backup (RUNBOOK already notes this).
5. Do **not** mark BOSS-340 fully “execution ready” until a test task returns `done` with a diff; connectivity acceptance for this ticket is met by `host.connected: true`.

---

## Files / units involved (host, not this repo)

- `/etc/systemd/system/sandbox-service.service`
- `/srv/sandbox/bin/sandbox-service.py`
- `/srv/sandbox/.sandbox-token`
- `/root/projects/infra/sandbox/RUNBOOK.md`
- boss-app: `/srv/apps/boss-app/.env` (`BOSS_SANDBOX_*` only; unchanged)
- Control plane probe: `engine/boss_engine/services/sandbox_console.py` → `GET {url}/v1/sandbox/health`
