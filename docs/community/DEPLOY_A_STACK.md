# Deploy an app with BOSS

BOSS deploys a repository to a VM you added under **Infra**, at a branch, a commit, or a pull request's head. The repository says how it is installed and checked in two files at its root:

- `boss.deploy.yaml` says how to install, update, health-check and open the app on a host.
- `boss.verify.yaml` says which tests the code-task gate runs for a change.

BOSS deploys itself this way. Both files are at the root of this repository, and they make a working example.

## From the console

1. **Infra → Add a VM.** Enter its address and SSH user. BOSS generates a key pair, keeps the private half in its secret store, and shows the public key. Add it to `~/.ssh/authorized_keys` for that user on the VM, then select **Verify connection**. The check only runs `echo` over SSH and changes nothing on the host.
2. **Setup → Repositories.** Register the GitHub repository, for example `seanventures/boss`.
3. **Add a deployment target** on that VM for the repository's stack. Give it the public URL people will open, such as `https://boss.example.com`. If the manifest is not at the repository root, give its path as well.
4. **Deploy.** Choose a ref in Infra, or let a workflow's deploy step deploy the pull request it built. A stack target needs no preview first.

BOSS resolves the ref to a commit and reads `boss.deploy.yaml` at that commit. It then checks the host without changing it: SSH works, the directory is writable, and `sudo` and Docker are present when the manifest requires them. The plan becomes a governed change that waits in **Decisions** for your approval. Nothing is written to the host until you approve.

After approval, a worker job runs the deployment, which can take ten minutes or more. Each step's status and the tail of its log appear on the deployment. If it succeeds, **Open** goes to the manifest's `open` URL. If it fails, the deployment shows what failed and what to do next. BOSS never rolls back by itself. **Roll back** plans a new deployment of the last healthy commit, through the same approval.

The same steps work over the API:

| Call | Does |
|---|---|
| `POST /v1/deployments/stack/plan` `{project_id, target_id, repository, ref}` | Plans a deployment and returns it with its `decision_id` |
| `GET /v1/deployments/stack/{id}` | Returns one deployment: status, steps, log tail, `open_url`, `error` |
| `GET /v1/deployments/stack?project_id=&target_id=` | Lists deployments |
| `POST /v1/deployments/stack/{id}/rollback` | Plans a deployment of the previous healthy commit |

Approve a deployment by resolving its `decision_id` through the decisions API. A deployment's status is one of `planned`, `awaiting_approval`, `running`, `healthy`, `failed`, or `rejected`.

## boss.deploy.yaml

```yaml
version: 1
name: shop                      # [a-z0-9-]+; host directory defaults to /opt/boss-apps/shop
kind: commands                  # or: compose
env:                            # written to <dir>/.boss/deploy.env (mode 600), exported to every step
  APP_URL: "{target.public_url}"
  DB_PASSWORD: {generate: password}   # generated once per target and stack, then reused
  STRIPE_KEY: {secret: stripe}        # a secret you stored in BOSS
install: ["./scripts/setup.sh", "docker compose up -d --build"]   # the first deploy to this target
update:  ["docker compose up -d --build"]                         # later deploys; defaults to install
health:  [{url: "http://127.0.0.1:8080/health", expect: 200, timeout_seconds: 600}]
open:    {url: "{target.public_url}"}
teardown: ["docker compose down"]
requires: {sudo: false, docker: true}
metrics: {path: /metrics}       # optional: BOSS scrapes it after deploy (below)
```

`kind: compose` with `compose: {file: compose.yml, project: shop}` is shorthand. Install and update then run `docker compose -f <file> -p <project> --env-file .boss/deploy.env up -d --build`.

**Placeholders.** Steps, env values and `open` may use `{dir}`, `{commit}`, `{ref}`, `{target.host}`, `{target.public_url}` and `{target.user}`. Any other `{name}` is a manifest error, so write shell variables as `$NAME`, not `${NAME}`.

**On the host.** Each commit is extracted from a `git archive` into `<dir>/releases/<commit>`. `<dir>/current` switches to it only after extraction completes. Every step runs from the release's own directory, `<dir>/releases/<commit>` (not through the `current` link, so tools such as Docker Compose see that the release changed and recreate what changed), with `deploy.env` exported, under a time limit. Its output goes to the deployment log, with secret values redacted. Health URLs are probed on the host itself, so a service bound to `127.0.0.1` can be checked. The last three releases are kept. The GitHub token that fetches the code never reaches the host.

**App metrics.** With `metrics: {path: /metrics}`, BOSS scrapes the app's Prometheus page on the health sweep's cadence (every 120 s unless `interval` says otherwise, 15 to 3600 s). Observability's app drawer then shows request rate, error rate (5xx out of all requests), p50/p95 latency and the last scrape in words, such as "Metrics endpoint returned 404". The path must be a path only, with no scheme or host. BOSS asks the app's own origin, the one its health probe uses (`via: public`), or the origin of the first `health` URL over the host's SSH channel when the app is reachable only from the host (`via: host`). The default, `via: auto`, tries public first. Redirects are not followed. BOSS reads `http_requests_total` (with a `status`, `code` or `status_code` label) and `http_request_duration_seconds`, plus the prometheus-fastapi-instrumentator and starlette-exporter names. When more than 5% of requests fail over 5 minutes with at least 20 requests, BOSS opens an incident. The incident resolves once the error rate recovers. To change the rule, set `alert: {error_rate_percent, window_minutes, min_requests, enabled}` under `metrics`.

**Generated secrets** are created on the first deploy and stored in BOSS's secret store. Later deploys reuse them. They are never logged or returned by the API.

### How BOSS declares itself

BOSS's `boss.deploy.yaml` runs `scripts/deploy/boss_stack.sh`. That script calls the repository's own `install.sh` with the unattended flags from the README: `--yes --skip-model-key --console-origin {target.public_url}`. Then it puts Caddy in front for HTTPS, as in README step 3. It writes the Caddyfile only if the file is missing, is the package default, or holds the block the script wrote earlier. A Caddyfile that serves other sites is left unchanged, and the step prints the block for you to add yourself.

The script also handles the one thing the release layout changes. The install's `.env` holds the database credentials, and each new release directory starts without one. The script keeps `.env` in `<dir>/.boss/boss` and copies it into each release before the installer runs. It copies it back afterwards, even when the installer fails. Updates run `scripts/install/backup.sh` first.

The VM needs passwordless `sudo`, because the installer installs Docker itself. The target's public URL must be `https`. The install and update steps may run for up to 90 minutes, since the first build of every image can take longer than the default 30-minute step limit on a small VM.

A host runs one BOSS. If the VM already runs a BOSS installed another way, such as the README install in `~/boss`, the deployment stops before installing anything and says so. Remove that install first, or deploy to another VM. The same applies when the VM still holds an earlier install's database volume that this deployment has no credentials for.

The HTTPS step installs Caddy if it is missing. It refreshes the package lists first. On a release whose archive has no `caddy` package, such as Ubuntu 22.04, it adds Caddy's own apt repository as Caddy's install guide documents.

The installer's `boss` command is pointed at `<dir>/current`, so `boss status`, `boss upgrade` and `boss backup` on the host keep working after older releases are removed.

## boss.verify.yaml

A code task's gate normally runs one default command for the whole repository. In a monorepo, `boss.verify.yaml` lets the gate run only the suites a change touches:

```yaml
version: 1
setup: []                       # optional; runs once, from the repo root, before any suite
suites:
  - name: engine
    paths: ["engine/**"]        # the suite runs when a changed file matches
    run: ["python3 -m pytest -q {tests}"]
    select: {tests_glob: "engine/tests/test_*.py", by: imports}
  - name: web
    paths: ["apps/web/**"]
    cwd: apps/web               # optional; steps run here, and {tests} paths are relative to it
    setup: ["pnpm install --frozen-lockfile"]   # optional; only when this suite runs
    run: ["npx vitest run {tests}"]
    select: {tests_glob: "apps/web/tests/**/*.test.*", by: imports, fallback: skip}
timeout_seconds: 900            # per command
```

- `by: imports` selects the test files that import a changed file or name its path in a string. A changed test file is always selected. `by: all` selects the whole glob every time. An import of a workspace package, such as `@boss/api-client`, counts as an import of every file in the directory whose `package.json` declares that name.
- If no test in a touched suite matches, `fallback: skip` (the default) runs nothing for that suite. `fallback: all` runs the whole glob.
- Every changed code file must be covered by a test the gate runs. A changed file that no suite claims, or that no selected test imports or names, fails the gate with `no test covers <files>`. Add a test for it, or get a human waiver. Only documentation and other non-code files (`.md`, `.txt`, images, anything under `docs/`) and deleted files may go untested, so a docs-only change passes.
- The gate reads `boss.verify.yaml` once, from the base commit, before any agent runs. A code task that creates, edits or deletes the file fails that attempt as tampering, and the file is put back. Change the verify declaration in its own reviewed pull request.
- If the code task cannot list the changed files, every suite runs its whole glob.
- Commands run **without a shell**, one command per entry. Each one must start with an executable on the sandbox allowlist (`BOSS_SANDBOX_VALIDATION_ALLOWED_COMMANDS`). A manifest that does not validate fails the gate and shows its error.
- A task's explicit `test_commands` override the manifest.

Without a manifest, a Python repository with a `conftest.py`, a `pytest.ini`, or pytest configured in `pyproject.toml`, `setup.cfg` or `tox.ini` is verified with `python3 -m pytest -q`. Other Python repositories use `unittest`. Web defaults run in the package that contains the change.
