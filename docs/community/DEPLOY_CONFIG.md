# Deploying configuration

A deploy in BOSS ships a built app to a host. Some systems are changed by **configuration alone**: an API gateway that hot-reloads `tenants/<tenant>.yaml`, a feature-flag file, a routing table. `Deploy config` ships files, not an app, with the same governance as a deploy: a ChangeSet, a budget reservation (zero), a person's approval or the environment's policy, a receipt, and a rollback.

## Turning it on

Configuration goes only where the operator says it may. In `.env`:

```
BOSS_CONFIG_DEPLOY_ROOTS=/srv/northgate              # comma-separated; a target's directory must sit beneath one
BOSS_CONFIG_RELOAD_COMMANDS=kill -HUP $(cat gateway.pid)   # one per line (or a JSON list); exact matches only
```

With no roots set, no config target can be made. A target outside the roots is refused when it is created, when a deployment is planned and again on the machine, against the directory's resolved path (a symlink cannot walk it out). A reload command must be one of the listed commands; a reload URL must be loopback, the target's own host, or listed in `BOSS_CONFIG_RELOAD_URLS`; and a target with any reload is created by a person, never an agent. Config targets are made only with `create_config_target`: `POST /v1/deployments` and the `deployment.target.register` skill refuse `config_dir`. A deployment to production always waits for a person, whatever production's policy says.

## The target

A `config_dir` deployment target is a directory on a host, plus how it is reloaded and how it is checked:

```
create_config_target  app_name="Northgate Gateway"  environment=staging
                      directory=/srv/northgate/staging/config/tenants
                      reload_url=http://127.0.0.1:8081/-/reload      # or reload_command, or neither
                      health_url=http://127.0.0.1:8081/readyz
```

`host_id` names a verified Fleet host (files travel over SSH under the host's pinned key). Without one the directory is on the machine BOSS runs on, which is what a demo gateway in the same compose project needs. One target per app and environment, like any other.

## Deploy, then promote

`deploy_config` takes the files — a map of destination path to content, or a CLI step's `produced_artifacts` (the fixed YAML a `sandboxed_command` skill wrote) — and plans a deployment run:

1. The files are stored as one **bundle** artifact: every byte and every sha256. The ChangeSet and the approval card list the file names and hashes.
2. Staging runs without asking when a person set its policy so (Infra › Deploys › Rules, *Deploy without asking*); otherwise, and always for production unless set otherwise, a decision waits in Approvals. Dual control is honoured.
3. On apply, the previous version of every file is kept beside the directory, under `<parent>/.boss-versions/<directory name>/<run>` (never inside the watched directory), each file is written to a temporary name and renamed into place, the reload runs, and the health URL is probed for up to 30 seconds. A failed reload or health check **restores the kept versions and reloads again**; the run says `rolled_back`.

`promote_config` plans the **same bundle** — the exact bytes staging passed, checked against that deployment's sha256 — to the production target. A promotion to production always waits for a person in Approvals, whatever production's policy says. It refuses a source deployment that did not succeed.

In a workflow the two are the `Deploy config` and `Promote config` steps (captain: the deployment manager). Wire `Deploy config`'s `produced_artifacts` from the CLI step that validated or fixed the file; the step waits on the deployment run like `Deploy` does, so a failed health check fails the step.
