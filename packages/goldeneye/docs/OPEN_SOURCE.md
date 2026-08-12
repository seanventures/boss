# Extracting Goldeneye as its own open-source project

This directory is structured as a **self-contained project root**. Extracting it
from the BOSS monorepo should not require rewriting modules.

## What stays in Goldeneye (this tree)

- `core/`, `modules/`, `schemas/`, `install.sh`, `VERSION`, `LICENSE`
- `python/goldeneye_kit/`
- `docs/`, `KIT.md`, `site.example.yaml`
- Optional `integrations/*` adapters that talk *to* control planes

## What stays in BOSS

- `engine/boss_engine/services/goldeneye_paas.py` — control plane + receipts  
- `/v1/goldeneye/*` API routes  
- Mission Control UI (if any)  
- DB tables `goldeneye_*`  
- Policy gates (`BOSS_SSH_HOST_DEPLOY_ALLOWED`, budgets)

BOSS depends on Goldeneye as a **library + node agent**, not the reverse.

## Extract checklist

1. `git subtree split` or copy `packages/goldeneye/` → new repo root.  
2. Confirm root contains `VERSION`, `install.sh`, `core/`, `modules/`, `LICENSE`.  
3. Add CI: `ge catalog build`, shellcheck on `core/bin/ge`, pytest for
   `python/goldeneye_kit`.  
4. Publish release tarballs named `goldeneye-<version>.tar.gz`.  
5. In BOSS: either  
   - git submodule `packages/goldeneye`, or  
   - pin a release artifact in install docs, or  
   - keep a vendored snapshot and sync on release.  
6. Update BOSS `goldeneye_paas._kit_roots()` to also look at
   `/opt/goldeneye` and `GOLDENEYE_KIT_ROOT`.  
7. Point one-liner installer at the new raw GitHub URL.

## Public API surface (stable)

| Surface | Stability |
|---|---|
| `modules/*/package.yaml` contract `api_version: 1` | Stable |
| `ge module|catalog|upgrade|deploy|node` CLI | Stable (additive) |
| `catalog.yaml` document `version: 2` | Stable |
| `python/goldeneye_kit.load_catalog` | Stable |
| Hook env vars `GOLDENEYE_*` | Stable |
| Internal shell helpers in `core/lib/*` | Best-effort |

## Naming

- Project: **Goldeneye**  
- CLI: **ge**  
- Config: `/etc/goldeneye/site.yaml`  
- State: `/var/lib/goldeneye`  
- Default install root: `/opt/goldeneye`

Avoid embedding BOSS product language in module `install.sh` files.
