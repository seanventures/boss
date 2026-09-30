# Extracting Shared-server as its own open-source project

This directory is structured as a **self-contained project root**. Extracting it
from the BOSS monorepo should not require rewriting modules.

## What stays in Shared-server (this tree)

- `core/`, `modules/`, `schemas/`, `install.sh`, `VERSION`, `LICENSE`
- `python/shared_server_kit/`
- `docs/`, `KIT.md`, `site.example.yaml`
- Optional `integrations/*` adapters that talk *to* control planes

## What stays in BOSS

- `engine/boss_engine/services/shared_server_paas.py` — control plane + receipts  
- `/v1/shared-server/*` API routes  
- Mission Control UI (if any)  
- DB tables `shared_server_*`  
- Policy gates (`BOSS_SSH_HOST_DEPLOY_ALLOWED`, budgets)

BOSS depends on Shared-server as a **library + node agent**, not the reverse.

## Extract checklist

1. `git subtree split` or copy `packages/shared-server/` → new repo root.  
2. Confirm root contains `VERSION`, `install.sh`, `core/`, `modules/`, `LICENSE`.  
3. Add CI: `ge catalog build`, shellcheck on `core/bin/ge`, pytest for
   `python/shared_server_kit`.  
4. Publish release tarballs named `shared-server-<version>.tar.gz`.  
5. In BOSS: either  
   - git submodule `packages/shared-server`, or  
   - pin a release artifact in install docs, or  
   - keep a vendored snapshot and sync on release.  
6. Update BOSS `shared_server_paas._kit_roots()` to also look at
   `/opt/shared-server` and `SHARED_SERVER_KIT_ROOT`.  
7. Point one-liner installer at the new raw GitHub URL.

## Public API surface (stable)

| Surface | Stability |
|---|---|
| `modules/*/package.yaml` contract `api_version: 1` | Stable |
| `ge module|catalog|upgrade|deploy|node` CLI | Stable (additive) |
| `catalog.yaml` document `version: 2` | Stable |
| `python/shared_server_kit.load_catalog` | Stable |
| Hook env vars `SHARED_SERVER_*` | Stable |
| Internal shell helpers in `core/lib/*` | Best-effort |

## Naming

- Project: **Shared-server**  
- CLI: **ge**  
- Config: `/etc/shared-server/site.yaml`  
- State: `/var/lib/shared-server`  
- Default install root: `/opt/shared-server`

Avoid embedding BOSS product language in module `install.sh` files.
