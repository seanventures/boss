# Quickstart

Get BOSS running and create your first task in about ten minutes. This guide installs on a single machine, such as a laptop, dev box, or VM, and opens Mission Control on that machine. When you are ready to share it with your team, see [Deploy](hostname.md).

## Before you begin

You need one of the following hosts:

- Ubuntu 22.04 or 24.04, or Debian 12 or 13, with `curl` and `sudo`. The installer sets up `git`, Python 3, and Docker for you.
- macOS with Docker Desktop running and Apple's command line tools (`xcode-select --install`).
- Any other Linux host that already has `git`, `curl`, Python 3, and Docker Engine with Compose.

A model key is optional. The control plane starts without one, and model work becomes available once you [connect a model](MODEL_CONNECTIONS.md).

## Install

Paste one command:

```bash
curl -fsSL https://boss.sean.network/install.sh | bash
```

It installs the newest published release into `~/boss`. The only setup question is an optional OpenAI key, asked when no provider key is exported. Exported `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY`, `XAI_API_KEY`, or `OPENROUTER_API_KEY` values are saved to the private `.env` and never printed.

Models are part of the install. After the stack is healthy, the installer looks for a Codex, Claude Code, or Grok login, the coding CLIs, and Ollama, and asks once before connecting each one. A Codex login is copied into BOSS. Claude Code and Grok sign in again inside the BOSS container with the provider's own login, so login files on your machine are never copied. If no model is usable, it offers to sign you in to a subscription or to set up a free local model: Ollama with `qwen2.5-coder:7b`. That model then becomes the default for every role. Add `| bash -s -- --yes` instead of `| bash` to skip every question. With `--yes`, exported keys are still saved, but nothing is copied, signed in, or installed; add `--connect-found` to connect what was found, or `--install-ollama` to set up the local model. The summary card lists each model source with the command that fixes it, and [Model connections](MODEL_CONNECTIONS.md) has the details.

Prefer git? Clone the release and install it in place:

```bash
git clone --branch v1.0-rc1 https://github.com/seanventures/boss.git
cd boss
./install.sh --dir "$PWD" --ref v1.0-rc1
```

Either way, the installer records the release in `.boss-release`, and reruns and upgrades stay on it instead of moving to `main`. Run the rest of this guide from the install directory.

When the installer finishes, you see a summary card confirming that BOSS Community is running. It lists Mission Control at `http://127.0.0.1:3000`, how to get the console password, the release, the models it connected, the seeded local project `boss-dogfood`, and the install directory. The API key line tells you the key is stored in `.env` rather than printed.

The installer also puts a `boss` command on your `PATH` that works from any directory:

```bash
boss open        # console address
boss password    # console password, in a terminal only
boss doctor      # check the install can do its job
boss status      # release, services, and health
boss connect     # the Models step again: keys, logins, Ollama, a local model
boss upgrade     # back up, then move to the newest release
boss uninstall   # stop BOSS and keep data
```

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
boss password
```

Without the `boss` command, run `python3 scripts/install/console_password.py show` from the install directory.

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

## Install without git

On a machine without a checkout, the website installer does the same thing in one command. It installs the `v1.0-rc1` release into `~/boss` and keeps reruns on that release:

```bash
curl -fsSL https://boss.sean.network/install.sh | bash
```

Pass installer flags after `bash -s --`, for example `| bash -s -- --skip-model-key`.

## Next steps

None of these are required for a first run.

- **Connect a model you already have.** See [model connections](MODEL_CONNECTIONS.md). Saving a key does not call the provider.
- **Connect a subscription you already have** with `boss connect`. It asks before copying a Codex login, and `boss connect claude` or `boss connect grok` signs those CLIs in inside the BOSS container. To use an existing Grok subscription, see [Grok CLI](GROK_CLI.md).
- **Install host copies of Claude Code, Codex CLI, and Grok Build** with `boss update --install-coding-clis`. This installs the tools but does not sign you in.
- **Explore the [five example workflows](https://github.com/seanventures/boss/blob/main/examples/README.md).** Installing them does not start any work.
- **Upgrade or uninstall** with `boss upgrade` and `boss uninstall`. The [installer guide](https://github.com/seanventures/boss/blob/main/scripts/install/README.md#upgrade--recover) covers backups and recovery.

The [guide index](README.md) lists every page. To report a problem, see [feedback](FEEDBACK_LOOP.md). Report security vulnerabilities by email only, as described in [SECURITY.md](https://github.com/seanventures/boss/blob/main/SECURITY.md).
