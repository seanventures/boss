# Goldeneye — bare-metal PaaS as a function of BOSS

**Status:** Active (modular kit + optional BOSS control plane)  
**Kit path:** `packages/goldeneye/` (self-contained; extractable OSS project)  
**Runtime (BOSS):** `engine/boss_engine/services/goldeneye_paas.py`  
**APIs (BOSS):** `/v1/goldeneye/*`  
**Installer:** `packages/goldeneye/install.sh`  
**Module API:** `1` — see `packages/goldeneye/docs/MODULE_CONTRACT.md`

---

## What it is

**Goldeneye** is a **modular bare-metal PaaS kit** you run on hardware or a VM
you administer: installer, versioned modules, edge proxy, a mesh you operate,
a registry you run, deploy CLI, and backups. It is designed to become its own
open-source repository. BOSS is an **optional** control plane that can register
nodes, plan module installs and app deploys, emit receipts, and store fleet
records. The kit does not require BOSS.

It is **not** a managed multi-tenant Cloud fleet, and it is **not** access to a
server operated by the maintainers. There is no shared host to SSH into.
Community and self-hosted installs run the kit on **your** metal. No step
requires Plane, an ops token, or a private registry.

### Community vs platform-only

| Surface | Edition |
|---|---|
| Kit installer + `ge` CLI + modules (`packages/goldeneye/`) | **Community** — you run it on your metal |
| BOSS control plane `/v1/goldeneye/*` (register, plan, receipts) | **Community** — optional; simulated by default |
| Real SSH actuation (`BOSS_SSH_HOST_DEPLOY_ALLOWED`) | **Paid editions only**; Community/local rejects the API request when the flag is on |
| Managed multi-tenant Goldeneye fleet / hosted metal as a service | **Platform-only (Cloud)** |
| Cloud Easy Connect as the node source | **Platform-only (Cloud)** |

`GET /v1/goldeneye/status` is honest: no registered nodes → `status=not_configured`,
`host.connected=false`, `metrics=null`. It never invents a host as connected
and it does not fabricate fleet metrics. Missing `goldeneye_nodes` is repaired via
`ensure_tables` (empty-200), not a 500.

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
4. **Agents are first-class operators.** Software on your machine can read its own deploy contract. That local handshake is not an ops token from the maintainers.
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
unless `--force` is set for operators who know the risk. Plane in the optional
row is a package you may install on your machine. The BOSS Community installer
does not require this kit, Plane, an ops token, or a registry you do not run.

---

## Kit usage (standalone)

The Community GitHub one-liner is not usable until a reviewed commit exists.
Do not fetch this kit from an unreviewed branch, and do not SSH to a host you
do not administer. From a checkout you already have, on Ubuntu you control:

```bash
sudo packages/goldeneye/install.sh --site /etc/goldeneye/site.yaml

# Package manager, on that same machine
ge pkg list
ge pkg install layer1-runtime
ge pkg install layer2-edge
ge deploy myapp --image registry.local.example:5000/myapp:abc1234
ge node status
```

`registry.local.example` is a name you configure. The kit can run a registry
on your machine. It does not log in to a maintainer registry.

Site config parameterizes domains, public IP, your own tailnet name, the CIDRs
you administer, ACME email, and alert sink. Those values describe your machine.
Tailscale, if you install it, is your tailnet. It is not a network operated
for you. The generated site file and any env files stay on that machine.

---

## BOSS control plane usage

Example only. `boss-dogfood` is the project id a local install seeds. The
addresses are documentation values, not a host to contact.

```http
POST /v1/goldeneye/nodes
{
  "project_id": "boss-dogfood",
  "actor": "ceo",
  "name": "node-1",
  "roles": ["control", "edge", "apps"],
  "public_ip": "203.0.113.10",
  "tailnet_ip": "100.64.0.10",
  "ssh_host": "node-1.example"
}

GET  /v1/goldeneye/packages
GET  /v1/goldeneye/catalog/bootstrap
POST /v1/goldeneye/packages/install
POST /v1/goldeneye/apps/deploy
GET  /v1/goldeneye/status?project_id=…
GET  /v1/goldeneye/apps?project_id=…
POST /v1/goldeneye/apps/{name}/rollback
```

`GET /v1/goldeneye/catalog/bootstrap` returns the kit's `default_bootstrap` order
(e.g. `layer0-host` → `layer1-runtime` → … → `deploy-cli`) so operators and
agents can walk a fresh node without hard-coding layer sequence.

### Real vs simulated host effects

| Gate | Behavior |
|---|---|
| `BOSS_SSH_HOST_DEPLOY_ALLOWED=false` (default) | Package install / deploy create **simulated** records + receipts; no remote shell |
| Community/local + `BOSS_SSH_HOST_DEPLOY_ALLOWED=true` | Package install returns **409** before a record, receipt, or SSH call |
| Paid edition + gate on but node has **no** `ssh_host` | Package install is **planned** only (hint: run `ge module install <id>` on the node) |
| Paid edition + gate on + node `ssh_host` + SSH key | **Real actuation** via SSH host adapter |

This matches ProviderOps honesty: Community can model fleet and prove receipts
without pretending remote metal was mutated.

### Actuation path (package install)

In a paid edition, when the gate is open **and** the node has `ssh_host` set,
`POST /v1/goldeneye/packages/install` attempts a remote:

```bash
ge module install <package_id>
```

over SSH using `SSHHostAdapter` patterns (`BOSS_SSH_HOST_PRIVATE_KEY` or
`BOSS_SSH_HOST_PRIVATE_KEY_FILE`, BatchMode, key material written to a 0600 temp
file only for the call).

| Outcome | Install `status` | Receipt | Notes |
|---|---|---|---|
| Remote exit 0 | `installed` | `succeeded` | `metadata.actuation` records host/command/via |
| Remote non-zero / adapter error | `failed` | `failed` | `error` jsonb: returncode, stderr, host, command |
| Gate off | `simulated` | `succeeded` | Default; safe for CI and Community |
| Paid edition, gate on, no `ssh_host` | `planned` | `succeeded` | Operator/agent must run `ge` on the box |
| Community/local, gate on | No row | No receipt | API returns 409 before any remote call |

Every allowed attempt emits a `Receipt` with a unique `dedupe_key` and a
`goldeneye.package.install` event. Failures are never silent.

App deploy real actuation remains planned for a follow-on slice; package install
is the first closed remote loop.

Dependencies: installs refuse missing `depends_on` unless `force=true`. Only
`installed` / `simulated` rows satisfy dependency checks (`failed` does not).

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

- A machine you administer can be bootstrapped with the kit installer to a working edge + deploy path
- BOSS APIs register nodes and plan package installs / app deploys with receipts
- Tests cover catalog, node register, simulated install/deploy, rollback record
- A clean install uses only the parameters in your site file and does not call a maintainer host

---

## Operator docs

- Kit README: `packages/goldeneye/README.md`
- Run the kit from a checkout on your machine. Do not follow a deploy guide that names a host you do not administer.
