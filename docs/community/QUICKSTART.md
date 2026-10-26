# Quickstart

Get BOSS running and create your first stored task. Allow several minutes for installation; first-time container builds and local model downloads can take longer. This guide installs on a single machine, such as a laptop, dev box, or VM, and opens Mission Control on that machine. When you are ready to share it with your team, see [Deploy](hostname.md).

## Before you begin

You need one of the following hosts:

- Ubuntu 22.04 or 24.04, or Debian 12 or 13, with `curl` and `sudo`. The installer sets up `git`, Python 3, and Docker for you.
- macOS with Docker Desktop running and Apple's command line tools (`xcode-select --install`).
- Any other Linux host that already has `git`, `curl`, Python 3, and Docker Engine with Compose.

A model key is optional. The control plane starts without one, and model work becomes available once you [connect a model](MODEL_CONNECTIONS.md).

## Install

For RC10.5, install from its release checkout. You need Git and a GitHub account with access if the repository asks you to sign in. On Ubuntu or Debian without Git, first run `sudo apt-get update && sudo apt-get install -y git`.

```bash
(
  set -e
  git clone --branch v1.0-rc10.10 https://github.com/seanventures/boss.git boss-rc10.10
  cd boss-rc10.10
  BOSS_RELEASE_COMMIT="$(git rev-parse HEAD)"
  ./install.sh --dir "$PWD" --ref "$BOSS_RELEASE_COMMIT"
)
```

Use a new directory named `boss-rc10.10`; the command stops if cloning fails. Continue from `cd boss-rc10.10` after installation. The installer records the exact checked-out commit. To select a later release deliberately, use `boss upgrade --to REF`.

The website installer is updated separately from source releases. Once its RC10.5 parity is verified, the short installation path is:

```bash
curl -fsSL https://boss.sean.network/install.sh | bash -s -- --ref v1.0-rc10.10
```

The website path installs into `~/boss`. The only setup question is an optional OpenAI key, asked when no provider key is exported. Exported `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY`, `XAI_API_KEY`, or `OPENROUTER_API_KEY` values are saved to the private `.env` and never printed.

Models are part of the install. After the stack is healthy, the installer looks for a Codex, Claude Code, or Grok login, the coding CLIs, and Ollama, and asks once before connecting each one. A Codex login is copied into BOSS. Claude Code and Grok sign in again inside the BOSS container with the provider's own login, so login files on your machine are never copied. If no model is usable, it offers to sign you in to a subscription or to set up a free local model: Ollama with `qwen2.5-coder:7b`. That model then becomes the default for every role and for chat. After you connect a hosted model later, run `boss connect` to hand the roles back to it. Add `| bash -s -- --yes` instead of `| bash` to skip every question. With `--yes`, exported keys are still saved, but nothing is copied, signed in, or installed; add `--connect-found` to connect what was found, or `--install-ollama` to set up the local model. The summary card lists each model source with the command that fixes it, and [Model connections](MODEL_CONNECTIONS.md) has the details.

The installer records the selected release in `.boss-release`. Reruns stay on that source instead of moving to `main`. Run the rest of this guide from the install directory.

When the installer finishes, you see a summary card confirming that BOSS Community is running. It lists Mission Control at `http://127.0.0.1:3000`, how to get the console password (you use it once, to claim the install), the release, the models it connected, the seeded local project `boss-dogfood`, and the install directory. The API key line tells you the key is stored in `.env` rather than printed.

The installer also puts a `boss` command on your `PATH` that works from any directory:

```bash
boss open        # console address
boss password    # console password (proves it is you when you claim), terminal only
boss doctor      # check the install can do its job
boss status      # release, services, and health
boss connect     # the Models step again: keys, logins, Ollama, a local model
boss upgrade     # back up, then move to the newest release
boss uninstall   # stop BOSS and keep data
boss identity setup-link   # a one-hour link to claim BOSS, instead of the password
```

What the installer sets up:

- The API, console, and database listen on `127.0.0.1` only.
- Installer keys go into `.env` in the install directory with mode `0600`; console connections use the selected secret backend. The installer does not upload the file or print keys. Hosted calls send the authentication and task data they need to that provider.
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

The first time, the console says **This BOSS has no Owner yet. Claim it.** Claiming makes you the first Owner:

1. Show the console password in a terminal on the install machine. It is never written to logs:

    ```bash
    boss password
    ```

    Without the `boss` command, run `python3 scripts/install/console_password.py show` from the install directory. Or run `boss identity setup-link` for a link that works once, for one hour.

2. On the claim screen, enter the password (or open the link), your name and email.
3. Add a passkey, or a password and an authenticator app.

From then on you sign in as yourself. Invite your team in **Identity › People** (see [People and access](PEOPLE_AND_ACCESS.md)).

After you sign in, BOSS opens on **Repo and models** until a repository and a model are connected: connect a GitHub repository and a model there. **Try it** starts a small build; it can spend model credit and write a branch and pull request to the repository you select. Follow the run and its checks before treating it as finished. Then open **Command Center**. On a new install, the planned layout in **Agent Studio → Team** is empty, and **Agent Studio → + New agent → From template** offers the starter definitions. Installing a definition does not start any work. For session details and remote access, see [Sign in to Mission Control](CONSOLE_AUTH.md).

## Create your first task

This step confirms that the control plane can store work. It does not call a model, send email, or deploy anything.

From the directory you passed to `--dir`, load the API address and the host tools' key into your shell (the key is never printed):

```bash
grep -q '^BOSS_HOST_API_KEY=.' .env || boss identity host-key
source ./boss-community.env.sh
export BOSS_API_KEY="$(sed -n 's/^BOSS_HOST_API_KEY=//p' .env)"
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
| `/v1/tasks` | The new task, titled `Local note`, created by the **Host tools** service account. |

Now run the task command a second time. The API rejects the repeated `dedupe_key` instead of creating a duplicate task. Every effect in BOSS carries a dedupe key for exactly this reason.

## Install without git

On a machine without a checkout, the website installer does the same thing in one command. It selects the newest published release candidate on a fresh install and keeps reruns on the recorded release. To select `v1.0-rc10.10` explicitly:

```bash
curl -fsSL https://boss.sean.network/install.sh | bash -s -- --ref v1.0-rc10.10
```

Pass installer flags after `bash -s --`, for example `| bash -s -- --skip-model-key`.

## If you get stuck

Start with `boss status` and `boss doctor`, then follow the [first-run troubleshooting steps](FEEDBACK_LOOP.md#stuck-during-your-first-run). Keep the release and error message; leave credentials and private data out of reports.

## Next steps

None of these are required for a first run.

- **Connect a model you already have.** See [model connections](MODEL_CONNECTIONS.md). **Connect** in the console makes one small budgeted, receipted model call. Importing keys through `boss connect keys` stores them without a completion; their first governed check or job proves them.
- **Connect a subscription you already have** with `boss connect`. It asks before copying a Codex login, and `boss connect claude` or `boss connect grok` signs those CLIs in inside the BOSS container. To use an existing Grok subscription, see [Grok CLI](GROK_CLI.md).
- **Install host copies of Claude Code, Codex CLI, and Grok Build** with `boss update --install-coding-clis`. This installs the tools but does not sign you in.
- **Explore the [fifteen example workflows](https://github.com/seanventures/boss/blob/main/examples/README.md).** Installing them does not start any work.
- **Upgrade or uninstall** with `boss upgrade` and `boss uninstall`. The [installer guide](https://github.com/seanventures/boss/blob/main/scripts/install/README.md#upgrade--recover) covers backups and recovery.

The [guide index](README.md) lists every page. To report a problem, see [feedback](FEEDBACK_LOOP.md). Report security vulnerabilities by email only, as described in [SECURITY.md](https://github.com/seanventures/boss/blob/main/SECURITY.md).
