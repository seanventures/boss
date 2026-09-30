# Shared-server architecture (modular)

```
┌─────────────────────────────────────────────────────────────┐
│  Control plane (optional)                                   │
│  BOSS /v1/shared-server/*  ·  other orchestrators               │
└───────────────────────────┬─────────────────────────────────┘
                            │ catalog + planned/real effects
┌───────────────────────────▼─────────────────────────────────┐
│  core/          ge CLI · module manager · site loader       │
│  catalog.yaml   assembled view of modules                   │
└───────────────────────────┬─────────────────────────────────┘
          ┌─────────────────┼─────────────────┐
          ▼                 ▼                 ▼
   modules/layer*     modules/optional    modules.d/* (external)
   host→runtime→      minio, keycloak,    third-party modules
   edge→control…      plane, …            (survive kit upgrade)
          │
          ▼
   host: Docker · Traefik · registry · ops-agent · apps
```

## Stability boundaries

| Layer | Stability | Upgrade |
|---|---|---|
| `core/` | High — CLI + module API | Kit upgrade replaces carefully |
| `modules/*/package.yaml` | Contract `api_version: 1` | Per-module semver |
| `modules/*/install.sh` | Module-owned | Module upgrade hook |
| `integrations/*` | Optional | Independent of runtime |
| site.yaml + state | Operator data | Never wiped by kit upgrade |

## Why modular

1. **Independent versioning** — edge can move without touching host baseline.  
2. **Extractable OSS** — no BOSS imports inside modules.  
3. **Third-party modules** — same contract under `modules.d/`.  
4. **Safe upgrades** — state under `/var/lib/shared-server` is outside the kit tree.  
5. **Control-plane agnostic** — BOSS is a consumer of the catalog, not the package manager.

## Data on disk

| Path | Contents |
|---|---|
| `/opt/shared-server` | Kit tree (replaceable) |
| `/etc/shared-server/site.yaml` | Site parameters |
| `/var/lib/shared-server/modules/*.json` | Installed module versions |
| `/var/lib/shared-server/apps/*.json` | Kit-recorded app deploys |
| `/var/lib/shared-server/modules.d/` | External modules |
| `/srv/apps` | App compose/env (per site.yaml) |
