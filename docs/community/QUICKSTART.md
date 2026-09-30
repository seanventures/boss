# Quickstart

Go from a checkout of this repository to a running Mission Control console on `127.0.0.1`, then create your first task through the API. The whole path runs on your own machine.

## Before you begin

You need:

- A checkout of this repository on the machine where BOSS will run.
- One of the following hosts:
    - Ubuntu 22.04 or 24.04, or Debian 12 or 13, with `sudo`. The installer sets up what it needs.
    - Any other Linux or macOS host that already has `git`, `curl`, and Docker Engine or Docker Desktop with Compose.

A model key is optional. The control plane starts without one, and model work becomes available once you [connect a model](MODEL_CONNECTIONS.md).

## Install

From the checkout, run:

```bash
./install.sh --dir "$PWD"
```

When the installer finishes, you see a success card confirming that BOSS Community is running. It lists Mission Control at `http://127.0.0.1:3000`, the seeded local project `boss-dogfood`, and the install directory. The API key line tells you the key is stored in `.env` rather than printed.

What the installer sets up:

- The API, console, and database listen on `127.0.0.1` only.
- Credentials go into `.env` in the install directory with mode `0600`. The file never leaves the machine, and neither the API key nor the console password is printed.
- Your checkout is installed as-is. If it is ahead of the default branch or has diverged from it, the installer keeps your source rather than rewinding the tree. Pass `--ref` only when you want a different commit; a ref that has diverged stops the install instead of installing the wrong source.

!!! note "Docker permissions"
    The installer does not add your user to the `docker` group. It detects whether Docker needs an administrator for its own commands, but if it does, run any manual Compose commands later as `sudo docker compose`.

!!! warning "Docker Desktop disk space"
    Docker Desktop stores images on its own virtual disk, which can fill up while the host still has free space. If Postgres fails to initialize, the installer prints the database log, including `No space left on device`. Free space in Docker and run the installer again; a rerun keeps the credentials already written. The installer also warns when free disk is under 80 GB. See the [installer guide](https://github.com/seanventures/boss/blob/main/scripts/install/README.md) for sizing.

## Open the console

On the install machine, open `http://127.0.0.1:3000`.

If BOSS runs on a remote VM you administer, forward the ports over SSH. Here and throughout these docs, `user@your-vm` stands for your own account on that VM:

```bash
ssh -L 3000:127.0.0.1:3000 -L 8000:127.0.0.1:8000 user@your-vm
```

Then open `http://127.0.0.1:3000` on the computer that opened the tunnel.

To sign in, display the console password in a terminal on the install machine. The password is separate from the API key and is never written to logs:

```bash
python3 scripts/install/console_password.py show
```

After you sign in, open **Command Center**. On a new install, the planned layout in **Team structure** is empty, and **Agent Studio** contains starter definitions. Installing a definition does not start any work. For session details and remote access, see [Sign in to Mission Control](CONSOLE_AUTH.md).

## Create your first task

This step confirms that the control plane can store work. It does not call a model, send email, or deploy anything.

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

Each command should complete without an error:

| Request | What you should see |
| --- | --- |
| `/health` | A health response with no secrets in it. |
| `/v1/edition` | The edition for the local project in `BOSS_PROJECT_ID`. |
| `/v1/tasks` | The new task, titled `Local note`, created by the `ceo` actor (the owner key). |

Now run the task command a second time. The API rejects the repeated `dedupe_key` instead of creating a duplicate task. Every effect in BOSS carries a dedupe key for exactly this reason.

## Install from a release

The release one-liner installs `v1.0-rc1` from a pinned commit. It downloads `install.sh` at that exact commit and refuses to run unless `BOSS_RELEASE_COMMIT` is a full 40-character SHA:

```bash
BOSS_RELEASE_COMMIT=7b88a18a2b4cd0b14b09a47fc415f7ffd4580066 bash -c 'set -u; [[ $BOSS_RELEASE_COMMIT =~ ^[0-9a-f]{40}$ ]] || { echo "Set the reviewed 40-character release SHA" >&2; exit 2; }; tmp=$(mktemp) || exit; curl -fsSL "https://raw.githubusercontent.com/seanventures/boss/$BOSS_RELEASE_COMMIT/install.sh" -o "$tmp" && BOSS_REPO_REF="$BOSS_RELEASE_COMMIT" bash "$tmp" "$@"; status=$?; rm -f "$tmp"; exit "$status"' _
```

!!! tip
    Only use a commit SHA published for a release; never substitute one you have not reviewed. If you are installing from a checkout, you do not need this command.

## Next steps

None of these are required for a first run.

- **Connect a model you already have.** See [model connections](MODEL_CONNECTIONS.md). Saving a key does not call the provider.
- **Install host copies of Claude Code, Codex CLI, and Grok Build** with `./install.sh --dir "$PWD" --install-coding-clis`. This installs the tools but does not sign you in. To use an existing Grok subscription, see [Grok CLI](GROK_CLI.md).
- **Explore the [five example workflows](https://github.com/seanventures/boss/blob/main/examples/README.md).** Installing them does not start any work.
- **Upgrade or uninstall** with the [installer guide](https://github.com/seanventures/boss/blob/main/scripts/install/README.md#upgrade--recover).

The [guide index](README.md) lists every page. To report a problem, see [feedback](FEEDBACK_LOOP.md). Report security vulnerabilities by email only, as described in [SECURITY.md](https://github.com/seanventures/boss/blob/main/SECURITY.md).
