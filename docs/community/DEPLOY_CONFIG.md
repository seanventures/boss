# Deploying configuration

A deploy in BOSS ships a built app to a host. Some systems are changed by **configuration alone**: an API gateway that hot-reloads `tenants/<tenant>.yaml`, a feature-flag file, a routing table. `Deploy config` ships files, not an app, with the same governance as a deploy: a ChangeSet, a budget reservation (zero), a person's approval or the environment's policy, a receipt, and a rollback.

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
3. On apply, the previous version of every file is kept under `<directory>/.boss-versions/<run>`, each file is written to a temporary name and renamed into place, the reload runs, and the health URL is probed for up to 30 seconds. A failed reload or health check **restores the kept versions and reloads again**; the run says `rolled_back`.

`promote_config` plans the **same bundle** — the exact bytes staging passed, by sha256 — to the production target, under production's rules. It refuses a source deployment that did not succeed.

In a workflow the two are the `Deploy config` and `Promote config` steps (captain: the deployment manager). Wire `Deploy config`'s `produced_artifacts` from the CLI step that validated or fixed the file; the step waits on the deployment run like `Deploy` does, so a failed health check fails the step.
