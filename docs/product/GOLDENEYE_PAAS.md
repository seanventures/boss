# Goldeneye — bare-metal PaaS as a function of BOSS

**Status:** Active (modular kit + optional BOSS control plane)  
**Kit path:** `packages/goldeneye/` (self-contained; extractable OSS project)  
**Runtime (BOSS):** `engine/boss_engine/services/goldeneye_paas.py`  
**APIs (BOSS):** `/v1/goldeneye/*`  
**Installer:** `packages/goldeneye/install.sh`  
**Module API:** `1` — see `packages/goldeneye/docs/MODULE_CONTRACT.md`

---

## What it is

**Goldeneye** is a **modular bare-metal PaaS kit** (standalone product path):
installer, versioned modules, edge proxy, private mesh, registry, deploy CLI,
ops handshake, and proven backups. It is designed to become its own open-source
repository; BOSS is an **optional** control plane that registers nodes, plans
module installs/app deploys, emits receipts, and keeps fleet truth durable.

It is **not** a managed multi-tenant Cloud fleet. Community/self-hosted editions
run the kit on **your** metal. Sean’s production host named `goldeneye` is the
dogfood instance of the kit.

### Modular units

| Unit | Role |
|---|---|
| `core/` | `ge` CLI + package manager (stable) |
| `modules/<id>/` | Versioned installable units (`package.yaml`) |
| `integrations/boss/` | Optional BOSS bridge docs |
| `python/goldeneye_kit/` | Catalog assembly library |

Upgrade: `ge upgrade <new-tree>` and `ge module upgrade <id>`.  
Extract plan: `packages/goldeneye/docs/OPEN_SOURCE.md`.

---

## Product surfaces

| Surface | Role |
|---|---|
| **Kit installer** | Turn a fresh Ubuntu bare-metal (or VM) box into a Goldeneye node |
| **Package manager** | Layered packages: host baseline → platform core → control → data → optional services |
| **Deploy CLI** | Ship containerized apps with auto HTTPS, env injection, rollback |
| **BOSS APIs** | Register nodes, install packages, deploy/list apps, fleet status |
| **Resource plane** | Bare-metal effects are receipted; real SSH/provision stays gated |

---

## Design principles (from production kit)

1. **Tailnet by default, public by exception.** Admin and inter-node traffic stay private; only end-user apps get public hostnames.
2. **One canonical public A record per node (or fleet apex); everything else CNAMEs.** Hardware/IP moves stay cheap.
3. **Name things, never hard-code addresses.** Registry, peers, and services resolve by DNS.
4. **Agents are first-class operators.** The platform is self-describing (ops handshake, secrets API, deploy contract).
5. **Prove it, don’t assert it.** Backups restore-drill; exposure audits; drift detection.
6. **BOSS hard rules apply.** Every external effect has a `Receipt` + `dedupe_key`. Spendy/real host writes stay policy-gated. Failures are visible.

---

## Layer model

```
0 host      OS, storage mounts, Tailscale, ufw, sshd, unattended-upgrades
1 runtime   Docker + address pools, platform-dns, external networks (web/storage/ops)
2 edge      Registry, hub pull-through, Traefik (public + tailnet), CrowdSec
3 control   ops-agent, health/functional checks, exposure-audit, infra-drift
4 data      pg dumps, volume/config backup, restic, peer/offsite, restore-drill
5 observe   uptime, host metrics, logs, errors, portal
6 apps      per-app compose via deploy pipeline
* optional  Keycloak, secrets vault, LLM gateway, Plane, MinIO, Meili, Temporal, …
```

Bootstrap order is fixed; the package manager refuses out-of-order installs
unless `--force` is set for operators who know the risk.

---

## Kit usage (standalone)

```bash
# On a fresh Ubuntu 24.04 host (as root), after Tailscale is up:
curl -fsSL https://raw.githubusercontent.com/seanheiney/boss/main/packages/goldeneye/install.sh | bash

# Or from a BOSS checkout:
sudo packages/goldeneye/install.sh --site /etc/goldeneye/site.yaml

# Package manager
ge pkg list
ge pkg install layer1-runtime
ge pkg install layer2-edge
ge deploy myapp --image registry.local.example:5000/myapp:abc1234
ge node status
```

Site config parameterizes domains, public IP, tailnet name, operator CIDRs,
ACME email, and alert sink — nothing Sean-specific is required for a clean
install.

---

## BOSS control plane usage

```http
POST /v1/goldeneye/nodes
{
  "project_id": "boss-dogfood",
  "actor": "ceo",
  "name": "node-1",
  "roles": ["control", "edge", "apps"],
  "public_ip": "203.0.113.10",
  "tailnet_ip": "100.64.0.10",
  "ssh_host": "node-1.tailnet.ts.net"
}

GET  /v1/goldeneye/packages
POST /v1/goldeneye/packages/install
POST /v1/goldeneye/apps/deploy
GET  /v1/goldeneye/status?project_id=…
GET  /v1/goldeneye/apps?project_id=…
POST /v1/goldeneye/apps/{name}/rollback
```

### Real vs simulated host effects

| Gate | Behavior |
|---|---|
| `BOSS_SSH_HOST_DEPLOY_ALLOWED=false` (default) | Package install / deploy create **planned** or **simulated** records + receipts; no remote shell |
| `BOSS_SSH_HOST_DEPLOY_ALLOWED=true` + SSH key | Real remote package/deploy via SSH host adapter path |

This matches ProviderOps honesty: Community can model fleet and prove receipts
without pretending remote metal was mutated.

---

## Relationship to other BOSS products

| Product | Boundary |
|---|---|
| **Community installer** (`install.sh` at repo root) | Runs BOSS itself (API/worker/web) on Docker — not the full PaaS kit |
| **Elastic fabric** | Spins workers/VMs for jobs; Goldeneye runs **apps** on bare metal PaaS nodes |
| **BOSS Cloud** | Multi-tenant hosted control plane; Goldeneye remains customer/self-hosted metal |
| **Resource plane / ssh_host** | Low-level single-container SSH deploys; Goldeneye is the opinionated multi-package PaaS |

---

## Package catalog (shipped)

See `packages/goldeneye/catalog.yaml`. Core packages:

- `layer0-host` — sysctl, ufw templates, unattended-upgrades policy
- `layer1-runtime` — Docker daemon pools, external networks, platform-dns recipe
- `layer2-edge` — Traefik + registry + hubcache recipes
- `layer3-control` — ops-agent skeleton, health timers stubs
- `layer4-data` — backup + restore-drill stubs
- `layer5-observe` — homepage / dozzle / uptime placeholders
- `deploy-cli` — `deploy` / `ge` command surface
- Optional: `keycloak`, `minio`, `meilisearch`, `llm-proxy`, `plane`, …

---

## Done means

- Fresh box can be bootstrapped with the kit installer to a working edge + deploy path
- BOSS APIs register nodes and plan package installs / app deploys with receipts
- Tests cover catalog, node register, simulated install/deploy, rollback record
- Sean dogfood continues on the production goldeneye host; kit stays parameterized

---

## Operator docs

- Kit README: `packages/goldeneye/README.md`
- BOSS-on-Goldeneye app deploy: `docs/operations/GOLDENEYE_DEPLOYMENT.md`
- Production skill (dogfood host): `goldeneye-deploy` skill
