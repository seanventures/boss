# Sandbox / microVM — Community GA status

**Status date:** 2026-08-13  
**Plane:** BOSS-340 (host), BOSS-337 Phase A4  
**Code path:** `engine/boss_engine/services/sandbox_provider.py`, `sandbox_console.py`, `sandbox-host/`

---

## Verdict

| Claim | GA status | Notes |
|---|---|---|
| Sandbox **host connectivity** (health probe) | **Allowed** when live host is up | BOSS-340 restored `sandbox-service` on goldeneye. Live `647c742`: host connected, 8/8 free, 0 running. |
| microVM **echo exercise** (test_mode + engine=echo) | **Proven live** 2026-08-13 | Rootfs baked (`guest-base.ext4` 289M); task `sbx-865940e15a70440a` **done**, diff `SANDBOX_OK.txt`. Coding/claude_code still **not claimed**. |
| **Local coding** while freeze is on | **Allowed (GA path) — live** | `make_sandbox_provider` returns `LocalSubprocessProvider` when `BOSS_MICROVM_CLAIMS_FROZEN=true` even if env says `microvm`. Live `GET /v1/factory/wip`: `coding_substrate=local`, `coding_substrate_configured=microvm`, `microvm_claims_frozen=true`. Replay BugBox used this path. |
| Retained-rootfs preview from microVM build | **Not claimed** | Depends on successful execution + rootfs |
| Local subprocess coding (`BOSS_SANDBOX_PROVIDER=local`) | **Allowed** | Weaker isolation; needs git/tools in worker image. Effective live path while freeze is on. |
| Firecracker **coding** GA | **Not claimed** | Configured `microvm` + freeze → factory advertises `local`. `GET /v1/diag/sandbox-roundtrip` **422**. |

**Do not market “sandboxes always work” or “Firecracker coding is GA” until a live provision succeeds after rootfs bake.**

### Live dogfood (2026-08-13, shared tag `647c742`)

- Worker/API env still says `BOSS_SANDBOX_PROVIDER=microvm` with `BOSS_MICROVM_CLAIMS_FROZEN=true`.
- Factory advertises the **local** coding substrate (frozen-microVM fallback). This is the working-software path.
- New canary `crun_27b5a74441fc4c0d8ace` is running on that local fallback — not a Firecracker coding proof.
- Do **not** unfreeze claims based on this tag.

---

## Architecture (where the code lives)

| Layer | Location | Role |
|---|---|---|
| Control plane client | `engine/boss_engine/services/sandbox_provider.py` (`MicroVMProvider`) | Submit / poll / release / hold / serve_preview over HTTP |
| Console proxy | `engine/boss_engine/services/sandbox_console.py` | Health, retained-rootfs tree/logs/hold |
| Contract | `engine/boss_engine/services/sandbox_contract.py` | Versioned Python⇄Go submit envelope |
| Repo stand-in host | `sandbox-host/sandbox_agent.py` + `guest/` | ContainerBackend + guest agent (dev/sim) |
| In-repo Go S1 scaffold | `services/sandbox-service/` | Typed job API + lease/TTL/heartbeat. **Fail-closed.** Not live Firecracker. Not GA. |
| Production host | Host `sandbox-service` (Firecracker) on goldeneye `:8077` | Not part of boss-app compose |

Settings:

- `BOSS_SANDBOX_PROVIDER` — `local` \| `microvm`
- `BOSS_SANDBOX_AGENT_URL` — host base URL
- `BOSS_SANDBOX_TOKEN` — `X-Sandbox-Token`
- **`BOSS_MICROVM_CLAIMS_FROZEN`** — GA honesty gate (this doc)

---

## Claim freeze mode (`BOSS_MICROVM_CLAIMS_FROZEN=true`)

When the guest rootfs cannot be prepared or proven in-tree for a release, operators set:

```bash
BOSS_MICROVM_CLAIMS_FROZEN=true
```

Effects:

| Surface | Behavior |
|---|---|
| `MicroVMProvider.run_code_task` / `serve_preview` | Raises `MicroVMClaimsFrozen` → failed code run / error (never silent) |
| `GET /v1/diag/sandbox-roundtrip` | **422** with clear “not claimed for Community GA” message |
| `POST /v1/sandboxes/{id}/exec` | **422** when frozen |
| `GET /v1/sandboxes` | **200** with `host.claims_frozen: true` + message (connectivity still reported) |
| Inspection (tree/logs/hold) | Allowed if host is up; **503** if host missing (never 500) |

Unfreeze only after:

1. `/srv/sandbox/rootfs/guest-base.ext4` (or equivalent) is baked from a tagged guest image  
2. `POST /v1/sandbox/tasks` → terminal `done` with a non-empty diff on dogfood  
3. Control-plane `GET /v1/diag/sandbox-roundtrip` reports `reached: true` with freeze **false**

---

## Fail-closed (no host)

Missing or unreachable host must never surface as HTTP **500**:

| Condition | Status |
|---|---|
| `BOSS_SANDBOX_AGENT_URL` empty | **503** on host-dependent routes; list returns `connected: false` |
| Connection refused / HTTP error to host | **503** with `sandbox_host_unavailable` |
| Claims frozen | **422** on execution claims |

---

## Rootfs bake (ops, not control-plane)

Host-side (example from live diagnosis):

```bash
bash /root/projects/infra/sandbox/bake-rootfs.sh \
  goldeneye.tail95d980.ts.net:5000/boss-sandbox-guest:<tag>
# ensures /srv/sandbox/rootfs/guest-base.ext4
systemctl restart sandbox-service   # optional
```

In-repo guest image source: `sandbox-host/guest/Dockerfile` → image `boss-sandbox-guest`.  
There is **no** deterministic rootfs prepare in the API container; bake stays on the privileged sandbox host.

---

## Recommended Community defaults

```bash
BOSS_SANDBOX_PROVIDER=local          # coding without microVM claim
BOSS_MICROVM_CLAIMS_FROZEN=true      # until rootfs proven
# After bake + live proof:
# BOSS_SANDBOX_PROVIDER=microvm
# BOSS_MICROVM_CLAIMS_FROZEN=false
# BOSS_SANDBOX_AGENT_URL=http://host.docker.internal:8077
```

---

## Related

- `docs/operations/BOSS-340_SANDBOX_HOST_DIAGNOSIS.md`
- `docs/operations/ga-readiness-2026-08-12/panels/05-sandbox-delivery.md`
- `docs/infra/PYTHON_GO_SANDBOX_CONTRACT.md`
- `docs/product/PREVIEW_GA_CHECKLIST.md` (preview View reachability)
