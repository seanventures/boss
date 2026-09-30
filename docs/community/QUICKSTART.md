# Quickstart

This is one path from a checkout you already have to Mission Control on `127.0.0.1`, then one task. It does not contact a maintainer server.

## What you need

- A checkout of this repository on the machine where BOSS will run.
- Ubuntu 22.04 or 24.04, or Debian 12 or 13, with `sudo`. An existing Linux or macOS host also works when `git`, `curl`, and Docker Engine or Docker Desktop with Compose are already installed.
- Nothing else. A model key is optional. Skipping it still starts the control plane. Model work stays unavailable until you connect one.

This repository has not been published. Do not guess a commit.

## Install

From the checkout:

```bash
./install.sh --dir "$PWD"
```

The installer binds the API, console, and database to `127.0.0.1`. It writes `.env` in that directory with mode `0600`, does not upload it, and does not print the API key or the console password. No step needs Plane, an ops token, a private registry, or SSH to a machine you do not administer.

If this checkout is ahead of the default branch, or the histories have diverged, the installer keeps this source. It does not rewind the tree. Pass `--ref` only when you mean a different commit. A diverged ref then stops instead of installing the wrong source.

The installer does not add your user to the Docker group. If Docker on this host requires an administrator, later manual Compose commands need `sudo docker compose`. The installer detects that access for its own commands.

You should see a success card that says BOSS Community is running. It names Mission Control at `http://127.0.0.1:3000`, the seeded project `boss-dogfood`, and the install directory. The API key line says the key is in `.env` and was not printed. `boss-dogfood` is that local project, not a shared server.

Docker Desktop stores images on its own virtual disk. That disk can fill while the host still has free space. If Postgres then fails to initialize, the installer prints the database log, including `No space left on device`. Free space in Docker and run the installer again. A rerun keeps credentials already written. The installer also warns when free disk is under 80 GB. Sizing is in the [installer guide](https://github.com/seanventures/boss/blob/main/scripts/install/README.md).

## Open the console

On the install machine, open `http://127.0.0.1:3000`.

If BOSS is on another machine, that machine must be a VM you administer. `user@your-vm` means that VM:

```bash
ssh -L 3000:127.0.0.1:3000 -L 8000:127.0.0.1:8000 user@your-vm
```

Open `http://127.0.0.1:3000` on the computer that opened the tunnel. The tunnel reaches the console. It does not deploy an app through BOSS.

On the install machine, show the console password in a terminal. It is not printed into logs, and it is not the API key:

```bash
python3 scripts/install/console_password.py show
```

You should see a sign-in page for Mission Control. After you sign in, open **Command Center**. On a new install, **Team structure**'s planned layout starts empty. **Agent Studio** already has starter definitions. Installing one does not start work. Sign-in details are in [console access](CONSOLE_AUTH.md).

## Create one task

This checks that the control plane can store work. It does not call a model, send email, or deploy.

From the directory you passed to `--dir`:

```bash
source ./boss-community.env.sh
curl -fsS "$BOSS_API_URL/health"
curl -fsS -H "Authorization: Bearer $BOSS_API_KEY" \
  "$BOSS_API_URL/v1/edition?project_id=$BOSS_PROJECT_ID"
curl -fsS -H "Authorization: Bearer $BOSS_API_KEY" \
  -H "Content-Type: application/json" \
  -d "{\"project_id\":\"$BOSS_PROJECT_ID\",\"title\":\"Local note\",\"dedupe_key\":\"local-note-1\",\"actor\":\"ceo\"}" \
  "$BOSS_API_URL/v1/tasks"
```

The owner key is the `ceo` actor. You should see each command finish without an error. Health does not print a secret. The edition response is for `BOSS_PROJECT_ID` on this machine. The task response includes the title `Local note`. Run the same task command again. The API rejects that `dedupe_key` instead of creating a second task.

## Optional

Skip all of these on the first path.

- A model you already have: [model connections](MODEL_CONNECTIONS.md). Saving a key does not call the provider.
- Host copies of Claude Code, Codex CLI, and Grok Build: `./install.sh --dir "$PWD" --install-coding-clis`. That does not sign you in. A Grok subscription you already have is in [Grok CLI](GROK_CLI.md).
- The [Shared-server kit](https://github.com/seanventures/boss/blob/main/docs/product/SHARED_SERVER_PAAS.md). It is optional software you run locally. It is not access to a server operated by someone else.
- The [five example workflows](https://github.com/seanventures/boss/blob/main/examples/README.md). Installing them starts no work.
- Upgrade and uninstall: [installer guide](https://github.com/seanventures/boss/blob/main/scripts/install/README.md#upgrade--recover).

The release one-liner installs `v1.0-rc1`. The checkout path above does not use it:

```bash
BOSS_RELEASE_COMMIT=7b88a18a2b4cd0b14b09a47fc415f7ffd4580066 bash -c 'set -u; [[ $BOSS_RELEASE_COMMIT =~ ^[0-9a-f]{40}$ ]] || { echo "Set the reviewed 40-character release SHA" >&2; exit 2; }; tmp=$(mktemp) || exit; curl -fsSL "https://raw.githubusercontent.com/seanventures/boss/$BOSS_RELEASE_COMMIT/install.sh" -o "$tmp" && BOSS_REPO_REF="$BOSS_RELEASE_COMMIT" bash "$tmp" "$@"; status=$?; rm -f "$tmp"; exit "$status"' _
```

Do not invent a SHA. The [guide index](README.md) lists the other pages. Report a problem through [feedback](FEEDBACK_LOOP.md). Report a vulnerability only by email, as [SECURITY.md](https://github.com/seanventures/boss/blob/main/SECURITY.md) describes.
