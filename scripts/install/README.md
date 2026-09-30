# BOSS Community installer

Install BOSS Community on a machine you administer. This page is the installer reference: the pinned `v1.0-rc1` command, local checkouts, network and secret defaults, upgrade, recovery, and uninstall. For a single guided path from a checkout to the console and a first task, use the [quickstart](../../docs/community/QUICKSTART.md).

## Install

### One command

```bash
curl -fsSL https://boss.sean.network/install.sh | bash
```

This installs release `v1.0-rc1` (commit `7b88a18a2b4cd0b14b09a47fc415f7ffd4580066`) into `~/boss`, asking before it uses another directory. Pass installer flags after `bash -s --`, for example `| bash -s -- --console-origin https://boss.example.com`. The script only downloads `install.sh` from that release commit on GitHub and runs it with the same commit pinned, so reruns stay on the release. To read it first, download it with `curl -fsSL https://boss.sean.network/install.sh -o install.sh`.

### Straight from GitHub

The same install without the website. The download and the Git checkout use the same commit, the installer verifies it, and a failed download stops before anything runs:

```bash
BOSS_RELEASE_COMMIT=7b88a18a2b4cd0b14b09a47fc415f7ffd4580066 bash -c 'set -u; [[ $BOSS_RELEASE_COMMIT =~ ^[0-9a-f]{40}$ ]] || { echo "Set the reviewed 40-character release SHA" >&2; exit 2; }; tmp=$(mktemp) || exit; curl -fsSL "https://raw.githubusercontent.com/seanventures/boss/$BOSS_RELEASE_COMMIT/install.sh" -o "$tmp" && BOSS_REPO_REF="$BOSS_RELEASE_COMMIT" bash "$tmp" "$@"; status=$?; rm -f "$tmp"; exit "$status"' _
```

Add installer flags after the final `_`, for example `_ --console-origin https://boss.example.com`.

### From a local checkout

Clone the release and install it in place:

```bash
git clone --branch v1.0-rc1 https://github.com/seanventures/boss.git
cd boss
./install.sh --dir "$PWD" --ref v1.0-rc1
```

Without `--ref`, the installer fast-forwards the checkout to `main`. From a checkout you already have on the branch you want:

```bash
./install.sh --dir "$PWD"
```

This installs into the checkout itself, from the local tree: it does not clone the release URL or connect to any other machine over SSH.

- Passing an empty directory as `--dir` fills that directory from this tree instead.
- A checkout that is ahead of `main`, or whose history has diverged from it, stays on that source.
- Pass `--ref` to require a different commit. A diverged ref then stops with the checkout unchanged.

## Security defaults

### Loopback and the private `.env`

The installer binds web, Postgres, and Infisical to `127.0.0.1`. The API binds to `127.0.0.1` unless you pass `--api-bind`.

Credentials are written only on the machine where the installer runs, into a private `.env` that is never uploaded. That file is yours: keep it private and never commit it. Nothing in this guide requires an ops token, a private registry, or a hosted BOSS account. In examples, `user@your-vm` is a VM you administer.

### Secrets stay off the command line

The installer reads prompts from the terminal even when `bash` runs the downloaded file. Paste a model key at the hidden prompt, or press Enter and connect one later.

The installer refuses API keys and tokens as arguments, because the shell would save them in history and process listings. Export `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, or `BOSS_GITHUB_TOKEN` for the installer process instead.

For a scripted install, pass the key through the environment and add `--yes`:

```bash
curl -fsSL https://boss.sean.network/install.sh | OPENAI_API_KEY="$OPENAI_API_KEY" bash -s -- --yes
```

Or, from the checkout you already have:

```bash
OPENAI_API_KEY="$OPENAI_API_KEY" ./install.sh --dir "$PWD" --yes
```

To start without a model key, add `--yes --skip-model-key` to either command, for example:

```bash
./install.sh --dir "$PWD" --yes --skip-model-key
```

You can add a key to the private `.env` later. See [model connections](../../docs/community/MODEL_CONNECTIONS.md).

## What the installer does

1. On a fresh Ubuntu 22.04/24.04 or Debian 12/13 VM, installs `git`, `curl`, OpenSSL, Python 3, Docker Engine, Buildx, and Compose from the signed Docker apt repository. Existing runtimes that conflict are left untouched.
2. Prompts for the install directory and a **BYOK** model key (optional skip).
3. Clones or fast-forwards the BOSS repo without deleting a nonempty directory or overwriting tracked local changes.
4. Writes a private `.env` (separate owner and worker API keys generated, mode 600).
5. Runs `docker compose` to build and start **Postgres + API + worker** (+ Mission Control if selected).
6. Waits for `/health`, runs the DB seed, and fails visibly if seeding or edition verification fails.
7. Verifies the Community edition and ProviderOps surface.

### Reruns

A rerun retains both API credentials and any operator-added API keys, the database password, model keys, the secret-store encryption key, the console login, public URL, console origin, API bind, port settings, the Mission Control choice, and the secret-store choice, unless this run sets the console origin or API bind again.

- Use the lifecycle [upgrade command](#upgrade--recover) to back up state before updating a long-lived install.
- A rerun does not remember which release you installed. Without `--ref`, it moves the checkout forward to `main`. To change a setting and stay on a release, pass the same ref again, for example `./install.sh --dir "$PWD" --ref v1.0-rc1 --install-coding-clis`.
- Pasting the installer line again also backs up an existing running install before switching to a newer source revision. A stopped database container is started from its existing image ID for that snapshot, then returned to its stopped state. If that container is missing, cannot start, or backup fails, the source and configuration stay untouched. The backup path is printed; keep it private.
- For an older headless install, pass `--no-web` once to record that choice; use `--web` to enable the console later.
- If you choose Infisical and it cannot be provisioned, the installer reports an incomplete install. The next rerun keeps that choice and retries it rather than silently switching to database credential storage.

### API keys

| Key | Bound to | Used by |
|---|---|---|
| `BOSS_API_KEY` (owner) | `ceo` member | Mission Control and local CLI commands |
| `BOSS_WORKER_API_KEY` (worker) | Seeded `system` member | The worker container only; it can claim and settle leased jobs, so never expose it elsewhere |

Existing single-key installs receive a new worker key on the next installer rerun or upgrade, without rotating the owner key.

### Docker access

The installer does not add your account to the root-equivalent Docker group. On a Linux host where only the administrator can access Docker, run Compose with `sudo` or grant Docker access deliberately through your host policy.

## Mission Control access

Mission Control has its own single-operator password and signed session secret, generated on first install and stored only in the private `.env`. The password is never the BOSS API key. On the VM, retrieve or rotate it deliberately:

```bash
cd ~/boss
python3 scripts/install/console_password.py show
python3 scripts/install/console_password.py reset
# After reset, run the web recreate command printed by the helper.
```

`show` requires a terminal so the password is not piped into logs.

### Serve the console on a hostname

The worked path for a hostname is [docs/community/hostname.md](../../docs/community/hostname.md). Pass `--console-origin https://console.example` on install, or, on an install that is already running, set `BOSS_CONSOLE_PUBLIC_ORIGIN=https://console.example` in `.env` and recreate web without rebuilding:

```bash
docker compose -f scripts/install/community-compose.yml --env-file .env --profile web up -d --force-recreate --no-deps web
```

> [!IMPORTANT]
> The TLS proxy on this machine must **overwrite** `Host`, `X-Forwarded-Host`, and `X-Forwarded-Proto` with the external HTTPS host and scheme; passing through client-supplied values is unsafe.

The Next listener stays on loopback, and this setting does not replace TLS. The HTTPS origin answers only after the proxy forwards to `127.0.0.1` and those headers match. Check a real login before you rely on the proxy. An SSH tunnel works without a proxy.

A hosted BOSS account is not required. Connect a model you already have only if you want model work.

## Optional coding subscriptions

The Community API and worker images already contain pinned Claude Code, Codex CLI, and Grok Build binaries. This option installs separate host copies for the VM user, for when you want to inspect or import that user's subscription login.

On the VM, add `--install-coding-clis` to install any missing official Claude Code, Codex CLI, and Grok Build binaries for the VM user. Existing working binaries are left alone, and each new binary must pass `--version`. The option also reports which VM logins can be verified. The installer downloads each provider's native install script over HTTPS before running it; review the upstream install methods for [Claude Code](https://code.claude.com/docs/en/setup), [Codex CLI](https://github.com/openai/codex#installing-and-running-codex-cli), and [Grok Build](https://docs.x.ai/build/overview).

```bash
# After BOSS is installed (or on a rerun):
./install.sh --dir "$PWD" --yes --skip-model-key --install-coding-clis
```

The VM cannot discover a subscription login on your laptop. Sign in with the provider's official CLI **on the VM**, under the account you intend to use:

```bash
codex login --device-auth
grok login --device-auth
```

`--discover-subscriptions` only checks status and sends no credentials.

| Flag | What it does |
|---|---|
| `--install-coding-clis` | Installs missing official CLIs for the VM user and reports verifiable logins |
| `--discover-subscriptions` | Checks login status only; sends no credentials |
| `--connect-claude-local` | Records a Claude Code CLI-owned local login made inside the API container |
| `--connect-claude` | Imports a setup token you supply into the local secret store |
| `--connect-codex` | Imports the VM user's Codex CLI auth JSON into BOSS's secret backend |
| `--connect-grok-local` | Records a Grok device login made inside the API container |

### Claude Code

Claude Code has a separate **CLI-owned local login**. Sign in through the official CLI as the `boss` user inside the API container, then tell BOSS to record that connection. The API and worker share the same private Claude config volume, including `.claude.json`, across container recreates:

```bash
docker compose -f scripts/install/community-compose.yml --env-file .env \
  exec -u boss api claude auth login
./install.sh --dir "$PWD" --yes --skip-model-key --connect-claude-local
```

The login happens in Anthropic's browser/device flow. BOSS reads the official CLI's sanitized `auth status` result and records connection metadata; this path does not send OAuth bytes to a BOSS endpoint or database.

- Recording is refused if `ANTHROPIC_API_KEY`, `CLAUDE_CODE_OAUTH_TOKEN`, or a previously stored setup token could make the status misleading.
- Community does not run a live proof turn during setup, so model use remains unverified until a governed job succeeds.
- `--connect-claude` imports a setup token the operator supplies and stores it only in the local secret store. It does not call a model. A missing token fails before anything is stored.

### Codex CLI

`--connect-codex` imports the VM user's CLI auth JSON into BOSS's secret backend. A database-backed install asks for `--accept-database-fallback` before that credential can be stored in Postgres; `--secret-store infisical` provisions a dedicated store. See [model connections](../../docs/community/MODEL_CONNECTIONS.md) for each path's limits.

### Grok Build

Grok's device session is tied to the Unix account and home that runs BOSS, so a login under the SSH admin account does not authenticate the containers. The API and worker images include Grok and share an owner-only `.grok` volume; run the device login as `boss` **inside the API container**:

```bash
docker compose -f scripts/install/community-compose.yml --env-file .env \
  exec -u boss api grok login --device-auth
./install.sh --dir "$PWD" --yes --skip-model-key --connect-grok-local
```

- The installer does not copy or parse `~/.grok/auth.json`, and `--connect-grok-local` does not call the model. If the login is missing, setup stops and records nothing.
- Once the Grok harness is available and its BOSS readiness proof passes, set `BOSS_CODE_AGENT_ENGINE=grok` in `.env` and recreate the API and worker. This selects Grok for governed coding work, not the console's chat model.

> [!NOTE]
> A CLI installed on the host does not by itself mean the BOSS containers are connected.

## Repository binding

The BOSS source URL the installer uses is not an operator repository binding. Fresh Community installs leave `BOSS_GITHUB_OWNER` and `BOSS_GITHUB_REPO` blank, so the legacy default-project webhook route does not point at the BOSS source repository. Register your repository in the project setup flow. If you intentionally use the legacy default webhook route, set both variables in the private `.env` and rerun the installer; reruns retain those explicit values.

## Files

| Path | Role |
|---|---|
| `install.sh` | One-line entry (repo root) |
| `scripts/install/coding_subscriptions.py` | Read-only VM CLI discovery and explicit Claude/Codex binding |
| `scripts/install/community-compose.yml` | Self-contained Community stack |
| `.env` (generated) | Secrets + ports — never commit |

## Requirements

- Fresh VM: Ubuntu 22.04/24.04 or Debian 12/13, 64-bit, with `sudo` or root access
- Existing Linux/macOS host: `git`, `curl`, running Docker Engine/Desktop and Compose
- Existing distro Docker packages are not removed automatically; upgrade those manually

### Sizing

| | Need | Why |
|---|---|---|
| **Disk** | **100 GB** | Images are about 11.5 GB (api 4.1, infisical 3.8, web 1.6, worker 1.5, postgres 0.4). Building them uses tens of GB of layer cache. A run can also clone a repository and install browsers. The installer warns below 80 GB. |
| **RAM** | **8 GB minimum, 16 GB comfortable** | The six containers idle at about 1.6 GB. Coding work runs inside the API container and adds whatever that task installs. Less than 8 GB will swap. |
| **CPU** | 4 cores | Reviewers run in parallel. |

If the host starts swapping, runs stall and BOSS records `host.stalled` on that machine. Add memory or free disk, then retry; retrying on a swapping host does not fix the stall.

### Ports

Everything derives from these variables; set them once in `.env`:

| Variable | Default | Notes |
|---|---|---|
| `BOSS_API_PORT` | 8000 | The console's preview links are built from this **at image build time**. Change it and rebuild `web`, or every preview link points at a port with nothing behind it. |
| `BOSS_API_BIND` | 127.0.0.1 | Address that accepts API connections. `127.0.0.1`, `0.0.0.0`, or one IPv4/IPv6 address. Web and Postgres stay on loopback. Authentication stays required. |
| `BOSS_WEB_PORT` | 3000 | Mission Control. Always bound to `127.0.0.1`. |
| `BOSS_POSTGRES_PORT` | 5432 | Always bound to `127.0.0.1`. |
| `INFISICAL_PORT` | 8080 | Always bound to `127.0.0.1`. |

`NEXT_PUBLIC_BOSS_API_URL` derives from `BOSS_API_PORT` automatically. Set it explicitly only when the API is reached at a different hostname than the browser uses, such as a remote VM, where it must be the address **the browser** will use, not `127.0.0.1`.

### Installing on a server

The worked path for a console hostname is [docs/community/hostname.md](../../docs/community/hostname.md). On the VM:

1. Pass `--console-origin https://console.example`, plus `--public-url` when the browser must call the API at a name other than loopback.
2. Point DNS or a hosts entry at this machine.
3. Run a TLS proxy here that forwards to `127.0.0.1:3000` and overwrites `Host`, `X-Forwarded-Host`, and `X-Forwarded-Proto`.

The web listener and Postgres stay on loopback. The API stays on `127.0.0.1` unless you pass `--api-bind`. The API key stays in `.env` and is never a URL parameter.

To skip the proxy, use an SSH tunnel to a VM you administer:

```bash
ssh -L 3000:127.0.0.1:3000 -L 8000:127.0.0.1:8000 user@your-vm
```

Then open `http://127.0.0.1:3000` on the computer that opened the tunnel. The public URL is baked into the web build. An older `.env` that set `BOSS_BIND_ADDRESS=0.0.0.0` is moved back to loopback on rerun. The only way to publish the API is `--api-bind` / `BOSS_API_BIND`, and authentication stays required.

## Verify the install

```bash
./scripts/install/checkup.sh            # free checks only
./scripts/install/checkup.sh --probe    # also ask a model a question
```

Without `--probe`, the checks do not call a model. `--probe` asks a model a question and spends money. The command is `checkup`, not `doctor`.

The checks cover: containers running, the API answering, the address compiled into the console, a model connected, a model that answers (`--probe` only), the repository pushable, and whether this machine can schedule work. The script exits 0 when those checks pass.

```
  ok    containers: 6 running
  ok    api: /health answers on http://127.0.0.1:8000
  ok    console: http://boss.example.internal:8000
  ok    models connected: claude_code
  ok    chat: a model answered
  ok    repository: connected
  ok    host: scheduling BOSS normally

  BOSS is ready.  Mission Control: http://127.0.0.1:3000
```

> [!NOTE]
> A passing checkup is not a promise that every preview or deploy will succeed. A connected subscription can still be out of quota; only the `--probe` question distinguishes "saved" from "answered."

### Before the first run

```bash
# Is the machine fit to start an expensive run?
curl -s "$BOSS_API_URL/v1/projects/$BOSS_DEFAULT_PROJECT_ID/preflight"
```

`{"ready": true}` means no unresolved blockers from the last run and no recent host stalls. Warnings name what to fix first; running anyway spends real money to reach a known answer.

### When a run is slower than expected

```bash
curl -s "$BOSS_API_URL/v1/code-tasks/${RUN_ID:?the code task run id, from the console}/retrospective"
```

Returns three separate grades (delivery, efficiency, host) deterministically, with no model call. If `host` is `degraded` or `critical`, the time was spent on the machine and re-running will not improve it.

## After install

```bash
source ~/boss/boss-community.env.sh
curl -sS -H "Authorization: Bearer $BOSS_API_KEY" "$BOSS_API_URL/health"
# Mission Control: http://127.0.0.1:3000
```

## Upgrade & recover

```bash
./scripts/install/upgrade.sh --dir "$PWD"                   # fast-forward and back up first
./scripts/install/upgrade.sh --dir "$PWD" --dry-run         # inspect source change
./scripts/install/upgrade.sh --dir "$PWD" --no-web          # record older headless installs once
./scripts/install/upgrade.sh --dir "$PWD" --web             # deliberately enable the console
```

A default `~/boss` install can run this from its checkout:

```bash
cd ~/boss && ./scripts/install/upgrade.sh --dir "$PWD"
```

Before changing source or containers, an upgrade writes a private backup under `~/boss-backups/` containing:

- the previous source commit
- `.env`
- BOSS and optional Infisical Postgres dumps
- artifacts

The API, worker, web, and Infisical services pause during the snapshot and resume even if it fails. The backup does not contain Codex/Grok CLI sessions; their named volumes are retained during upgrade, and a data-purge recovery requires provider sign-in again.

> [!WARNING]
> Keep the backup outside the checkout and protect it like `.env`.

### Recover an older safe uninstall

Older versions of safe uninstall used `docker compose down`. That removed the database container but kept its named data volume. The current backup refuses to guess which mutable `postgres:16-alpine` image should reopen that data, so if `backup.sh` reports a missing Postgres container, use this recovery path before upgrading.

> [!CAUTION]
> Do not run the new installer or `compose up` against that volume yet. Confirm that the old stack is stopped and no other container is writing to its database volume; the offline copy below is valid only while the volume is idle.

1. **Identify a local, reviewed Postgres 16 image ID** from the host's deployment records or cached image inventory. Prefer the image ID the old container used. If that ID is unavailable, select a compatible cached Postgres 16 image only after retaining an offline copy of the volume; this is a recovery decision, not an automatic tag update. If you cannot identify a reviewed local image, stop and get a recovery review.
2. **Run the recovery subshell** below after replacing the image-ID placeholder. It resolves the actual database volume from the current Compose project, checks that it already exists, makes an offline copy of the volume, and verifies the archive has PostgreSQL 16 control data before starting Postgres. It then creates only the database container, from the explicit immutable image ID. A missing volume or invalid archive stops before any database container is created.

```bash
(
  set -euo pipefail
  cd ~/boss
  docker image ls --no-trunc postgres
  DB_IMAGE_ID=sha256:REPLACE_WITH_REVIEWED_LOCAL_POSTGRES_16_IMAGE_ID
  [[ "$DB_IMAGE_ID" =~ ^sha256:[0-9a-f]{64}$ ]]
  PG_VOLUME="$(docker compose -f scripts/install/community-compose.yml --env-file .env \
    config --format json | python3 -c 'import json,sys; c=json.load(sys.stdin); m=[v for v in c["services"]["db"]["volumes"] if v.get("type")=="volume" and v.get("target")=="/var/lib/postgresql/data"]; assert len(m)==1; print(c["volumes"][m[0]["source"]]["name"])')"
  test -n "$PG_VOLUME"
  docker volume inspect "$PG_VOLUME" >/dev/null
  docker image inspect "$DB_IMAGE_ID" >/dev/null
  docker run --rm --pull never --entrypoint postgres "$DB_IMAGE_ID" --version | grep -E 'PostgreSQL\) 16\.'
  umask 077
  mkdir -p "$HOME/boss-backups"
  OFFLINE_DIR="$(mktemp -d "$HOME/boss-backups/legacy-pg-offline.XXXXXX")"
  OFFLINE_DB_BACKUP="$OFFLINE_DIR/postgres-volume.tar"
  ARCHIVE_VALID=0
  OVERRIDE=""
  cleanup() {
    if [[ -n "$OVERRIDE" ]]; then
      docker compose -f scripts/install/community-compose.yml -f "$OVERRIDE" --env-file .env stop db >/dev/null 2>&1 || true
      rm -f "$OVERRIDE"
    fi
    if [[ "$ARCHIVE_VALID" -ne 1 ]]; then
      rm -f "$OFFLINE_DB_BACKUP"
      rmdir "$OFFLINE_DIR" 2>/dev/null || true
    fi
  }
  trap cleanup EXIT
  docker run --rm --pull never \
    --mount "type=volume,source=$PG_VOLUME,target=/data,readonly" \
    --entrypoint tar "$DB_IMAGE_ID" -C /data -cf - . > "$OFFLINE_DB_BACKUP"
  test -s "$OFFLINE_DB_BACKUP"
  test "$(tar -xOf "$OFFLINE_DB_BACKUP" ./PG_VERSION)" = 16
  test "$(tar -xOf "$OFFLINE_DB_BACKUP" ./global/pg_control | wc -c)" -gt 0
  ARCHIVE_VALID=1
  echo "Offline database volume copy: $OFFLINE_DB_BACKUP"
  OVERRIDE="$(mktemp)"
  printf 'services:\n  db:\n    image: %s\n' "$DB_IMAGE_ID" > "$OVERRIDE"
  docker compose -f scripts/install/community-compose.yml -f "$OVERRIDE" --env-file .env \
    up -d --no-deps --no-build --pull never db
  ready=0
  for attempt in {1..60}; do
    if docker compose -f scripts/install/community-compose.yml -f "$OVERRIDE" --env-file .env \
      exec -T db pg_isready -U boss -d boss >/dev/null 2>&1; then ready=1; break; fi
    sleep 2
  done
  test "$ready" -eq 1
)
```

3. **Back up.** The subshell leaves the recovered database container **stopped**. Only after it succeeds, make and inspect a logical backup from that container:

```bash
cd ~/boss && ./scripts/install/backup.sh --dir "$PWD"
```

4. **Upgrade.** The upgrade takes its own fresh backup before changing source:

```bash
cd ~/boss && ./scripts/install/upgrade.sh --dir "$PWD"
```

Both commands print private backup paths. If this install's `boss-community-api` image is missing, backup also stops before any build or pull; restore the reviewed image first. The installer tags that image with the Compose project name, so a second project does not replace the first. On a host where Docker requires administrator access, run the Docker commands through your host's approved `sudo` policy.

### Roll back to a backup

If a build fails, the backup path is printed. Inspect Compose logs and retry `upgrade.sh` once the cause is fixed. A database migration may already have run, so the script deliberately does not reset source or database behind your back.

To return to the backed-up version, stop app writers, restore the source commit and `.env`, restore the dumps, and rebuild that version. Run this from the install directory, replacing the backup path first:

```bash
BACKUP=/absolute/path/from-upgrade
docker compose -f scripts/install/community-compose.yml --env-file .env --profile web --profile secrets stop worker api web infisical
git switch --detach "$(cat "$BACKUP/source-commit")"
cp "$BACKUP/.env" .env && chmod 600 .env
docker compose -f scripts/install/community-compose.yml --env-file .env up -d db
for attempt in {1..60}; do
  docker compose -f scripts/install/community-compose.yml --env-file .env exec -T db pg_isready -U boss -d boss >/dev/null && break
  sleep 1
done
docker compose -f scripts/install/community-compose.yml --env-file .env exec -T db pg_isready -U boss -d boss
docker compose -f scripts/install/community-compose.yml --env-file .env exec -T db pg_restore -U boss -d boss --clean --if-exists --no-owner < "$BACKUP/boss.dump"
if [ -f "$BACKUP/infisical.dump" ]; then
  docker compose -f scripts/install/community-compose.yml --env-file .env exec -T db pg_restore -U boss -d infisical --clean --if-exists --no-owner < "$BACKUP/infisical.dump"
fi
docker compose -f scripts/install/community-compose.yml --env-file .env run --rm --no-deps -T --entrypoint tar api -C /var/lib/boss/artifacts -xf - < "$BACKUP/artifacts.tar"
docker compose -f scripts/install/community-compose.yml --env-file .env --profile web up -d --build
```

The final command is for the default database-backed install with Mission Control:

- Omit `--profile web` if the console was disabled.
- Add `--profile secrets` only if the previous install used Infisical; wait for its health before restarting the API and worker.

> [!WARNING]
> Restoring an upgraded database into an older source revision may require a migration-aware recovery plan. Run these commands during a maintenance window and confirm `/health`, the project list, and an artifact before reopening the console.

Notes on what a restore covers:

- The artifact restore overlays its archive. Files created after the backup remain unless removed separately; review the current volume first.
- BOSS does not archive the Claude CLI's session volume. The backup's `.env` copy blanks `CLAUDE_CODE_OAUTH_TOKEN` and `CODEX_AUTH_JSON`, and those values are removed from the dump and artifact archive if they appear there. A normal upgrade or safe uninstall keeps its named volume; after an explicit volume purge or a move to another machine, run `claude auth login` inside the API container again.

### Remote (archive) installs

A remote archive install uses `.boss-shipped-commit` rather than Git.

- **Update:** the local `upgrade.sh` requires a Git checkout, so use `remote.sh update` from your machine instead; it backs up on the VM first.
- **Recover:** ship the backed-up commit from your own machine with `remote.sh --ref COMMIT install`, then restore the database and artifacts from the backup written on that VM.
- **Failed update:** the new source is at the install path while the prior source remains at `PATH.prev` until installer/recreation succeeds. A failed build leaves the old containers running; after diagnosing the failure, you can move `PATH.prev` back to `PATH` before retrying. If recreation started, a database migration may have run: inspect the backup on that VM and use the database/artifact recovery steps above instead of only moving source. A failed source swap itself restores the prior source path, and the remote script refuses to discard a leftover `PATH.prev` on a later attempt.

`user@your-vm` is a machine you administer:

```bash
scripts/install/remote.sh --host user@your-vm update --ref main
scripts/install/remote.sh --host user@your-vm uninstall --yes
# Intentional data purge only:
scripts/install/remote.sh --host user@your-vm uninstall --yes --purge-data --confirm-purge DELETE-BOSS-DATA
```

## Uninstall / reinstall

```bash
./scripts/install/uninstall.sh --dir "$PWD" --dry-run
./scripts/install/uninstall.sh --dir "$PWD" --yes           # safe: keeps data and credentials
./install.sh --dir "$PWD" --yes --skip-model-key             # reuses the same volumes and keys
./scripts/install/uninstall.sh --dir "$PWD" --purge-data     # backs up, then asks for DELETE-BOSS-DATA
```

| Mode | Removes | Keeps |
|---|---|---|
| Safe uninstall (default) | Stops this Compose stack | Stopped containers and their exact image IDs, source, `.env`, named volumes, images, and backups, so a later upgrade can start the same database image for backup. Rerunning is safe. |
| `--purge-data` | This stack's named volumes (including Postgres, artifacts, and CLI sessions), then `.env` and the generated shell shim | Backups and the checkout |

`--purge-data` first requires a successful private backup unless you explicitly add `--skip-backup`. `--yes` alone never authorizes a data purge; for a scripted purge, add `--confirm-purge DELETE-BOSS-DATA`.

To stop:

```bash
cd ~/boss
./scripts/install/uninstall.sh --dir "$PWD" --yes
```

> [!WARNING]
> A manual `docker compose down` removes the stopped container needed for a later no-recreation backup. If an older uninstall already did that, use [Recover an older safe uninstall](#recover-an-older-safe-uninstall).

## Flags

See `./install.sh --help`.
