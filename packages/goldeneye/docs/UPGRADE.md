# Upgrading Goldeneye

## Kit upgrade (recommended)

1. Obtain the new tree (git tag, release tarball, monorepo path).  
2. As root:

```bash
ge upgrade /path/to/goldeneye-0.x.y
```

What it does:

1. Records current kit VERSION.  
2. rsyncs the new tree over `/opt/goldeneye` (or `paths.infra_root`).  
3. Reinstalls `/usr/local/bin/ge`.  
4. Rebuilds `catalog.yaml` from `modules/*/package.yaml`.  
5. For each **installed** module, if catalog version > state version, runs
   `upgrade` hook (or `install.sh` with `GOLDENEYE_MODULE_ACTION=upgrade`).

**Preserved:** `/etc/goldeneye/site.yaml`, `/var/lib/goldeneye/*`, `/srv/apps/*`.

## Module-only upgrade

```bash
ge module upgrade layer2-edge
ge module check layer2-edge
```

## From git (development)

```bash
cd /path/to/checkout
sudo GOLDENEYE_KIT_ROOT=/opt/goldeneye ge upgrade "$(pwd)"
```

When vendored in BOSS:

```bash
sudo ge upgrade /path/to/boss/packages/goldeneye
```

## Rollback

1. Keep the previous tree or git tag.  
2. `ge upgrade /path/to/previous-version`.  
3. Module hooks should be idempotent; if a module broke, fix or pin version in
   state JSON only as last resort (`/var/lib/goldeneye/modules/<id>.json`).

## Version policy

| Artifact | Scheme |
|---|---|
| Kit `VERSION` | Semver — release of core + default module set |
| Module `package.yaml` version | Semver — independent |
| Module `api_version` | Integer — breaking install API only |
| `catalog.yaml` `version` | Document format (currently `2`) |

## Breaking changes

If `package.yaml` `api_version` must bump, ship a migration note under
`docs/migrations/` and refuse install when kit is too old (`min_kit_version`).
