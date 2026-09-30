# Shared-server

**Modular bare-metal PaaS kit** — turn one or more servers into a self-hosted
platform: installer, package manager, edge (Traefik), registry, deploy CLI,
ops handshake, and proven backup drills.

Shared-server is designed as an **independent open-source project**. It is currently
vendored under the BOSS monorepo at `packages/shared-server/` and can be extracted
without rewriting module recipes. BOSS is an *optional* control-plane
integration, not the runtime foundation.

| | |
|---|---|
| **Version** | see `VERSION` |
| **License** | Apache-2.0 |
| **Module API** | `1` (`schemas/package.schema.json`) |
| **CLI** | `ge` |

## Quick start

```bash
sudo mkdir -p /etc/shared-server
sudo cp site.example.yaml /etc/shared-server/site.yaml
# edit domains, public_ip, acme_email

curl -fsSL https://tailscale.com/install.sh | sh && sudo tailscale up

sudo ./install.sh --site /etc/shared-server/site.yaml --with-core
# or step modules yourself:
#   ge module install layer0-host
#   ge module install layer1-runtime
#   …

ge deploy hello --image registry.example/hello:v1
ge node status
```

## Modular layout

```
shared-server/
  VERSION                 kit release
  install.sh              host bootstrap
  site.example.yaml
  catalog.yaml            GENERATED from modules (do not hand-edit)
  core/                   CLI + package manager (stable)
    bin/ge
    lib/{common,module}.sh
  modules/<id>/           installable units (upgrade independently)
    package.yaml          module contract
    install.sh | upgrade.sh | check.sh
  schemas/                package.yaml + site.yaml JSON Schema
  integrations/           optional control planes (boss/, …)
  python/shared_server_kit/   catalog assembly library
  docs/                   architecture, contract, upgrade, extract
```

Back-compat symlinks: `lib` → `core/lib`, `bin` → `core/bin`, `packages` → `modules`.

## Module commands

```bash
ge module list
ge module install <id> [--force]
ge module upgrade <id>
ge module check [id]
ge catalog build          # regenerate catalog.yaml
ge upgrade /path/to/new   # replace kit tree + bump installed modules
```

Contract: [`docs/MODULE_CONTRACT.md`](docs/MODULE_CONTRACT.md).

## Upgrade

```bash
# From a new checkout / release tree:
sudo ge upgrade /tmp/shared-server-0.3.0
```

Details: [`docs/UPGRADE.md`](docs/UPGRADE.md).

## Integrations

| Integration | Path | Role |
|---|---|---|
| **None** | — | Kit runs fully standalone |
| **BOSS** | `integrations/boss/` | Control plane: nodes, receipts, ChangeSets via `/v1/shared-server/*` |

## Design principles

1. Tailnet by default; public only for end-user apps  
2. One canonical public A record; CNAMEs for everything else  
3. Name services, never hard-code node IPs in apps  
4. Agents rediscover credentials via ops handshake / secrets API  
5. Backups proven by restore drills  
6. **Modules are the unit of upgrade** — kit core stays small  

## Extract as its own repo

See [`docs/OPEN_SOURCE.md`](docs/OPEN_SOURCE.md). Short version: this directory
*is* the project root; move it out, keep `VERSION` / modules / core / LICENSE.

## Docs

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)  
- [`docs/MODULE_CONTRACT.md`](docs/MODULE_CONTRACT.md)  
- [`docs/UPGRADE.md`](docs/UPGRADE.md)  
- [`docs/OPEN_SOURCE.md`](docs/OPEN_SOURCE.md)  
- [`KIT.md`](KIT.md) — system inventory & hard-won lessons  
