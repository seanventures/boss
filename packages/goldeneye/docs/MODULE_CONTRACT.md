# Shared-server module contract

Every installable unit is a **module** under `modules/<id>/` with a
`package.yaml` manifest. The kit core never special-cases a module beyond
reading this contract and running hooks.

## Layout

```
modules/<id>/
  package.yaml     # required — schema: schemas/package.schema.json
  install.sh       # required unless hooks.install points elsewhere
  upgrade.sh       # optional — defaults to re-running install.sh
  uninstall.sh     # optional
  check.sh         # optional — exit 0 healthy, non-zero drift/broken
  README.md        # optional
  … assets …
```

## package.yaml (api_version: 1)

| Field | Required | Meaning |
|---|---|---|
| `api_version` | yes | Contract version (`1`) |
| `id` | yes | Stable id (`layer0-host`, `minio`) |
| `name` | yes | Human title |
| `version` | yes | Module semver (independent of kit `VERSION`) |
| `layer` | yes | Bootstrap layer 0–9 |
| `kind` | yes | host / runtime / edge / control / data / observe / tool / optional / integration |
| `depends_on` | no | Module ids that must be installed first |
| `optional` | no | Omit from default bootstrap |
| `provides` | no | Capability tags |
| `conflicts` | no | Mutually exclusive module ids |
| `hooks` | no | Override script names |
| `min_kit_version` | no | Require kit VERSION ≥ this |

## Hooks

All hooks run as root with env:

| Env | Value |
|---|---|
| `SHARED_SERVER_KIT_ROOT` | Kit install root |
| `SHARED_SERVER_SITE` | Path to site.yaml |
| `SHARED_SERVER_STATE_ROOT` | `/var/lib/shared-server` (or override) |
| `SHARED_SERVER_MODULE_ID` | Module id |
| `SHARED_SERVER_MODULE_VERSION` | Version from package.yaml |
| `SHARED_SERVER_MODULE_ACTION` | `install` \| `upgrade` \| `uninstall` \| `check` |

Exit codes: `0` success; non-zero fail (package manager aborts).

## Upgrade semantics

1. **Kit upgrade** (`ge upgrade`): replace kit tree (core + modules manifests), then upgrade each installed module whose `package.yaml` version > recorded state.
2. **Module upgrade** (`ge module upgrade <id>`): run `upgrade.sh` if present else `install.sh` with `SHARED_SERVER_MODULE_ACTION=upgrade`.
3. State records live in `$STATE/modules/<id>.json` (`version`, `installed_at`, `updated_at`). Legacy `$STATE/packages/<id>.installed` is still recognized.

## Catalog

`catalog.yaml` is the **assembled** view for control planes (e.g. BOSS).
Generate with:

```bash
ge catalog build   # writes catalog.yaml from modules/*/package.yaml
```

Do not hand-edit `catalog.yaml` as source of truth — edit module `package.yaml`.

## External modules (future)

`ge module add <source>` will clone/copy a module into
`modules.d/<id>/` (outside the core kit tree) so third-party modules survive
kit upgrades. Contract is the same `package.yaml`.
