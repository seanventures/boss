# BOSS Community installer

One-line Community setup for a supported Linux VM.

## One-liner

The public Community repository has not been published yet. After publication,
replace this placeholder with the reviewed release commit's full SHA and run:

```bash
BOSS_RELEASE_COMMIT=REPLACE_WITH_REVIEWED_40_CHARACTER_SHA bash -c 'set -u; [[ $BOSS_RELEASE_COMMIT =~ ^[0-9a-f]{40}$ ]] || { echo "Set the reviewed 40-character release SHA" >&2; exit 2; }; tmp=$(mktemp) || exit; curl -fsSL "https://raw.githubusercontent.com/seanventures/boss/$BOSS_RELEASE_COMMIT/install.sh" -o "$tmp" && BOSS_REPO_REF="$BOSS_RELEASE_COMMIT" bash "$tmp" "$@"; status=$?; rm -f "$tmp"; exit "$status"' _
```

The HTTP download and Git checkout use the same immutable commit, and the
installer verifies that commit. If the download fails, the installer does not
run. Until publication, use an authorized checkout.

The installer reads prompts from the terminal even when its downloaded file is
run by `bash`. Paste a model key at the hidden prompt, or press Enter and
connect one later. Do not place API keys in shell arguments, which can appear
in process listings and shell history.

For unattended automation, pass the key to the installer process through its
environment and add `--yes` after the final `_` in the pinned command. Or run
the authorized checkout directly:

```bash
OPENAI_API_KEY="$OPENAI_API_KEY" ./install.sh --dir "$PWD" --yes
```

Or start without a model key and add one to `.env` later:

Add `--yes --skip-model-key` after the final `_` in the pinned command, or run
`./install.sh --dir "$PWD" --yes --skip-model-key` from the checkout.

## What it does

1. On a fresh Ubuntu 22.04/24.04 or Debian 12/13 VM, installs `git`, `curl`, OpenSSL, Python 3, Docker Engine, Buildx and Compose from the signed Docker apt repository; leaves existing runtimes untouched if they conflict
2. Prompts for install directory and **BYOK** model key (optional skip)  
3. Clones or fast-forwards the BOSS repo without deleting a nonempty directory or overwriting tracked local changes
4. Writes a private `.env` (separate owner and worker API keys generated, mode 600)
5. `docker compose` builds and starts **Postgres + API + worker** (+ Mission Control if selected)  
6. Waits for `/health`, runs DB seed, and fails visibly if seeding or edition verification fails
7. Verifies Community edition + ProviderOps surface

On rerun, both API credentials and any operator-added API keys, the database password,
model keys, secret-store encryption key, console login, public URL, port settings,
Mission Control choice, and secret-store choice are retained. Use the lifecycle
upgrade command below to back up state before updating a long-lived install.
Pasting the installer line again also backs up an existing running install
before switching to a newer source revision. A stopped database container is
started from its existing image ID for that snapshot, then returned to its
stopped state. If that container is missing, cannot start, or backup fails,
the source and configuration stay untouched. The backup path is printed; keep it
private.
For an older headless install, pass `--no-web` once to record that choice;
use `--web` to enable it later.
If you choose Infisical and it cannot be provisioned, the installer reports an
incomplete install. The next rerun keeps that choice and retries it rather than
silently switching to database credential storage.

The owner key (`BOSS_API_KEY`) is bound to the `ceo` member and powers Mission
Control and local CLI commands. The worker key (`BOSS_WORKER_API_KEY`) is bound
to the seeded `system` member. Keep it private to the worker container; it can
claim and settle leased jobs. Existing single-key installs receive a new worker
key on the next installer rerun or upgrade without rotating the owner key.

The installer does not add your account to the root-equivalent Docker group.
On a Linux host where only the administrator can access Docker, run Compose
with `sudo` or grant Docker access deliberately through your host policy.

Mission Control has a separate single-operator password and signed session secret,
generated on first install and stored only in the private `.env`. The password is
never the BOSS API key. On the VM, retrieve or rotate it deliberately:

```bash
cd ~/boss
python3 scripts/install/console_password.py show
python3 scripts/install/console_password.py reset
# After reset, run the web recreate command printed by the helper.
```

`show` requires a terminal so the password is not piped into logs. Keep the
console on loopback or behind an HTTPS reverse proxy; the web login refuses
non-HTTPS requests outside loopback. For a public console hostname, set
`BOSS_CONSOLE_PUBLIC_ORIGIN=https://boss.example.com` in `.env` and recreate
web. The reverse proxy must **overwrite** `Host`, `X-Forwarded-Host`, and
`X-Forwarded-Proto` with the external HTTPS host and scheme; passing through
client-supplied values is unsafe. Keep the Next listener bound to loopback.
This setting does not replace TLS. The proxy configuration needs a live login
and CSRF check before public access is enabled.

No BOSS Cloud / Account Connect required.

### Optional coding subscriptions

The Community API and worker images already contain pinned Claude Code, Codex
CLI, and Grok Build binaries. This option installs separate host copies for the
VM user when you want to inspect or import that user's subscription login.

On the VM, add `--install-coding-clis` to install missing official Claude Code,
Codex CLI, and Grok Build binaries for the VM user. Existing working binaries are
left alone; each new binary must pass `--version`. This option also reports which
VM logins can be verified. The installer downloads each provider's native install
script over HTTPS before running it. Review the upstream install methods at
[Claude Code](https://code.claude.com/docs/en/setup),
[Codex CLI](https://github.com/openai/codex#installing-and-running-codex-cli), and
[Grok Build](https://docs.x.ai/build/overview).

```bash
# After BOSS is installed (or on a rerun):
./install.sh --dir "$PWD" --yes --skip-model-key --install-coding-clis
```

The VM cannot discover a subscription login on your laptop. Sign in using the
provider's official CLI **on the VM**, under the account you intend to use:

```bash
codex login --device-auth
claude login
grok login --device-auth
```

`--discover-subscriptions` only checks status; it sends no credentials. For
Claude or Codex, an explicit `--connect-claude` or `--connect-codex` stores the
official CLI credential through BOSS's credential endpoint and records the
connection. Community does not run a live proof turn during setup; model use
remains unverified until a governed job succeeds. For Claude, run `claude setup-token`
in a VM terminal so its official prompts remain visible; the installer accepts the
token through a hidden terminal prompt or `CLAUDE_CODE_OAUTH_TOKEN` environment
variable. Codex uses its CLI auth JSON, read only from the current VM user's
private file. BOSS never prints either credential. A database-backed install asks you to add
`--accept-database-fallback` before a subscription credential can be stored in
Postgres; the `--secret-store infisical` option provisions a dedicated store.
See [model connections](../../docs/community/MODEL_CONNECTIONS.md) for the supported paths
and limits of each provider.

Grok's device session is tied to the Unix account and home that runs BOSS.
A login under the SSH admin account does not authenticate the containers.
The API and worker images include Grok and share an owner-only `.grok` volume;
run the device login as `boss` **inside the API container**:

```bash
docker compose -f scripts/install/community-compose.yml --env-file .env \
  exec -u boss api grok login --device-auth
```

The installer does not copy or parse `~/.grok/auth.json`. Once the Grok
harness is available and its BOSS readiness proof passes, set
`BOSS_CODE_AGENT_ENGINE=grok` in `.env` and recreate the API and worker.
This selects Grok for governed coding work; it does not select the console's
chat model. A host CLI being installed does not by itself mean the BOSS
containers are connected.

The BOSS source URL used by the installer is not an operator repository
binding. Fresh Community installs leave `BOSS_GITHUB_OWNER` and
`BOSS_GITHUB_REPO` blank, so the legacy default-project webhook route does
not point at the private development repository. Register the repository in
the project setup flow; if you intentionally use the legacy default webhook
route, set both variables in the private `.env` and rerun the installer. Reruns
retain those explicit values.

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

### Sizing — measured, not estimated

These are real figures from a working install, not guesses. The previous "~5GB free disk" was
wrong by an order of magnitude and would strand a build half-way.

| | Need | Why |
|---|---|---|
| **Disk** | **100 GB** | Images are ~11.5 GB (api 4.1, infisical 3.8, web 1.6, worker 1.5, postgres 0.4). Building them generates tens of GB of layer cache — a real build reached 58 GB before pruning. Every agent run also clones the target repo and may `npm install` Playwright browsers. The installer warns below 80 GB. |
| **RAM** | **8 GB minimum, 16 GB comfortable** | The six containers idle at ~1.6 GB. The coding agent runs INSIDE the api container, so a build adds a Node process plus whatever the task installs. A 7.6 GB allocation ran a 39-file build successfully; less than 8 GB will swap. |
| **CPU** | 4 cores | Reviewers run in parallel. |

**Do not let it swap.** A host that starts swapping stops scheduling BOSS for minutes at a
time, and the symptom is a build that takes 85 minutes instead of 20 with nothing in any log
to explain it. BOSS now detects this itself (`host.stalled` events, and the per-run
retrospective attributes the lost time), but the cure is capacity, not detection.

### Ports

Everything derives from these; set them once in `.env`:

| Variable | Default | Notes |
|---|---|---|
| `BOSS_API_PORT` | 8000 | The console's preview links are built from this **at image build time**. Change it and rebuild `web`, or every preview link points at a port with nothing behind it. |
| `BOSS_WEB_PORT` | 3000 | Mission Control. |
| `BOSS_POSTGRES_PORT` | 5432 | |
| `INFISICAL_PORT` | 8080 | |

`NEXT_PUBLIC_BOSS_API_URL` derives from `BOSS_API_PORT` automatically. Set it explicitly only
when the API is reached at a different hostname than the browser uses — a remote VM, for
instance, where it must be the address **the browser** will use, not `127.0.0.1`.

### Installing on a server (not a laptop)

The default VM install binds API and web to loopback. Reach it immediately through an SSH
tunnel from your laptop:

```bash
ssh -L 3000:127.0.0.1:3000 -L 8000:127.0.0.1:8000 user@your-vm
```

Then open `http://127.0.0.1:3000` locally. For a permanent public site, put HTTPS and an
access policy in front of the loopback ports and pass `--public-url https://boss.example.com`
when installing or updating. Set `BOSS_CONSOLE_PUBLIC_ORIGIN` to the console's
HTTPS origin as described above. The public URL is baked into the web build. Community
keeps API and web ports on loopback even if an older `.env` had a public bind;
remote access goes through an SSH tunnel or a TLS reverse proxy.

### Did it work?

```bash
./scripts/install/checkup.sh            # free checks only
./scripts/install/checkup.sh --probe    # also ask a model a question
```

`--probe` behaves as `consensus doctor --probe` does — free checks by default, and a real model
call only when you ask for one, because that call costs money. The name is `checkup` rather than
`doctor` because several other tools now ship a `doctor` that means something slightly
different in each; a command whose meaning changes per product is one nobody trusts.

Seven checks, each one a failure that has actually happened here: containers running, the API
answering, the address compiled into the console, a model connected, **a model that actually
answers** (`--probe`), the repository pushable, and whether this machine can schedule BOSS at
all. Exits 0 when BOSS can do its job.

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

"Connected" and "working" are different claims, and the chat check is the one that separates
them: a subscription can be connected and out of quota, and only asking it a question finds out.

### Before the first run

```bash
# Is the machine fit to start an expensive run?
curl -s "$BOSS_API_URL/v1/projects/$BOSS_DEFAULT_PROJECT_ID/preflight"
```

`{"ready": true}` means no unresolved blockers from the last run and no recent host stalls.
Warnings name what to fix first — running anyway wastes real money reaching a known answer.

### When a run is slower than expected

```bash
curl -s "$BOSS_API_URL/v1/code-tasks/<run_id>/retrospective"
```

Returns three separate grades — delivery, efficiency, host — deterministically, with no model
call. If `host` is `degraded` or `critical`, the time was the machine's and re-running will not
improve it.

## After install

```bash
source ~/boss/boss-community.env.sh
curl -sS -H "Authorization: Bearer $BOSS_API_KEY" "$BOSS_API_URL/health"
# Mission Control: http://127.0.0.1:3000
```

### Upgrade / recover

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

Upgrades write a private backup under `~/boss-backups/` before changing source or
containers. It contains the previous source commit, `.env`, BOSS and optional
Infisical Postgres dumps, and artifacts. The API, worker, web and Infisical
services pause during the snapshot and resume even if it fails. The backup
does not contain Codex/Grok CLI sessions; their named volumes are retained
during upgrade, and a data-purge recovery requires provider sign-in again.
Keep the backup outside the checkout and protect it like `.env`.

### Recover an older safe uninstall

Older versions of safe uninstall used `docker compose down`. That removed the
database container but kept its named data volume. The current backup refuses
to guess which mutable `postgres:16-alpine` image should reopen that data. If
`backup.sh` reports a missing Postgres container, use this recovery path before
upgrading. Do not run the new installer or `compose up` against that volume yet.
Confirm that the old stack is stopped and no other container is writing to its
database volume; the offline copy below is valid only while the volume is idle.

First identify a **local, reviewed Postgres 16 image ID** from the host's
deployment records or cached image inventory. Prefer the image ID the old
container used. If that ID is unavailable, select a compatible cached
Postgres 16 image only after retaining an offline copy of the volume; this is
a recovery decision, not an automatic tag update. The commands below make that
copy before starting Postgres, then create only the database container from
the explicit immutable image ID. Replace the image-ID placeholder; if you
cannot identify a reviewed local image, stop and get a recovery review.
The command resolves the actual database volume from the current Compose
project, checks that it already exists, and verifies the offline archive has
PostgreSQL 16 control data. A missing volume or invalid archive stops before
any database container is created.

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

The subshell leaves the recovered database container **stopped**. Only after
it succeeds, make and inspect a logical backup from that container:

```bash
cd ~/boss && ./scripts/install/backup.sh --dir "$PWD"
```

Then run the upgrade. It takes its own fresh backup before changing source:

```bash
cd ~/boss && ./scripts/install/upgrade.sh --dir "$PWD"
```

Both commands print private backup paths. If the local
`boss-community-api:local` image is missing, backup
also stops before any build or pull; restore the reviewed image first. For a
host where Docker requires administrator access, run the Docker commands
through your host's approved `sudo` policy.

If a build fails, the backup path is printed. Inspect Compose logs and retry
`upgrade.sh` once the cause is fixed. A database migration may already have
run, so the script deliberately does not reset source or database behind your
back. To return to the backed-up version, stop app writers, restore the source
commit and `.env`, restore the dumps, and rebuild that version. Run this from
the install directory, replacing the backup path first:

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

The final command is for the default database-backed install with Mission
Control. Omit `--profile web` if the console was disabled. Add
`--profile secrets` only if the previous install used Infisical; wait for its
health before restarting the API and worker. Restoring an upgraded database
into an older source revision may require a migration-aware recovery plan.
Run these commands during a maintenance window and confirm `/health`, the
project list, and an artifact before reopening the console.

The artifact restore overlays the archive. Files created after the backup
remain unless removed separately; review the current volume first. A remote archive
install uses `.boss-shipped-commit` rather than Git; recover it by shipping
that commit from the operator machine with `remote.sh --ref COMMIT install`,
then restore the database and artifacts from the private host backup.
The local `upgrade.sh` requires a Git checkout; for archive installs use
`remote.sh update` from the operator machine, which now backs up first.
For remote installs, the new source is at the install path while the prior
source remains at `PATH.prev` until installer/recreation succeeds. A failed
build leaves the old containers running; after diagnosing the failure, you can
move `PATH.prev` back to `PATH` before retrying. If recreation started, a
database migration may have run: inspect the private backup and use the
database/artifact recovery steps above instead of only moving source. A failed
source swap itself restores the prior source path. The remote script refuses to
discard a leftover `PATH.prev` on a later attempt.
For example:

```bash
scripts/install/remote.sh --host user@your-vm update --ref main
scripts/install/remote.sh --host user@your-vm uninstall --yes
# Intentional data purge only:
scripts/install/remote.sh --host user@your-vm uninstall --yes --purge-data --confirm-purge DELETE-BOSS-DATA
```

### Uninstall / reinstall

```bash
./scripts/install/uninstall.sh --dir "$PWD" --dry-run
./scripts/install/uninstall.sh --dir "$PWD" --yes           # safe: keeps data and credentials
./install.sh --dir "$PWD" --yes --skip-model-key             # reuses the same volumes and keys
./scripts/install/uninstall.sh --dir "$PWD" --purge-data     # backs up, then asks for DELETE-BOSS-DATA
```

The default safe uninstall stops this Compose stack. It keeps the stopped
containers and their exact image IDs, source, `.env`, named volumes, images,
and backups so a later upgrade can start the same database image for backup.
Rerunning it is safe. `--purge-data` removes this stack's named volumes,
including Postgres, artifacts and CLI sessions, then deletes `.env` and the
generated shell shim.
It first requires a successful private backup unless you explicitly add
`--skip-backup`. `--yes` alone never authorizes a data purge; for scripted
purge add `--confirm-purge DELETE-BOSS-DATA`. Backups and checkout stay on disk.

Stop:

```bash
cd ~/boss
./scripts/install/uninstall.sh --dir "$PWD" --yes
```

Manual `docker compose down` removes the stopped container needed for a later
no-recreation backup. Use the recovery procedure above if an older uninstall
already did that.

## Flags

See `./install.sh --help`.
