# The path

Install [v1.0-rc10.11](https://github.com/seanventures/boss/releases/tag/v1.0-rc10.11), claim the install, connect one repository, connect one model, and open one pull request.

This is the short path on a machine you administer. [Your hostname](hostname.md) covers HTTPS. [Sign in](CONSOLE_AUTH.md) covers passkeys, passwords, and an SSH tunnel. The [first change](FIRST_USEFUL_CHANGE.md) is what you do with the pull request after it exists.

This is a release candidate. The bounded installed checks behind it are an owned Linux arm64 machine that already had Docker. A clean-host install and useful provider work are separate checks. The [release notes](RELEASE_NOTES_v1.0-rc10.11.md) say what this tag changed and what was not run.

## Before you begin

The installer is written for one of these hosts:

- Ubuntu 22.04 or 24.04, or Debian 12 or 13, with `curl` and `sudo`. It can install Git, Python 3, and Docker.
- macOS with Docker Desktop running and Apple's command line tools (`xcode-select --install`).
- Another Linux host that already has Git, `curl`, Python 3, and Docker Engine with Compose.

A model key is optional. The control plane starts without one.

The installer warns when free disk is under 80 GB. Docker Desktop keeps images on its own disk, which can fill while the host still looks fine. If Postgres fails to initialize, the installer prints the database log. Free space in Docker and run the installer again. A rerun keeps credentials it has already written. Sizing is in the [installer guide](https://github.com/seanventures/boss/blob/main/scripts/install/README.md).

## Install

Install this tag from its checkout. You need Git. If GitHub asks you to sign in, use an account that can read the repository. On Ubuntu or Debian without Git, first run `sudo apt-get update && sudo apt-get install -y git`.

```bash
(
  set -e
  git clone --branch v1.0-rc10.11 https://github.com/seanventures/boss.git boss-rc10.11
  cd boss-rc10.11
  BOSS_RELEASE_COMMIT="$(git rev-parse HEAD)"
  ./install.sh --dir "$PWD" --ref "$BOSS_RELEASE_COMMIT"
)
```

Use a new directory named `boss-rc10.11`. The command stops if the clone fails. It records the checked-out commit and stays on that source when you rerun it. Continue from `cd boss-rc10.11`. To move later, on purpose, use `boss upgrade --to REF`.

The script at `https://boss.sean.network/install.sh` is published separately. Do not treat it as this tag.

Add `--console-origin https://boss.example.com` (your hostname, not this example) when anyone will open the console from another machine. Set that before you claim. The [hostname guide](hostname.md) is the proxy, certificates, and private-IP case. Omit the flag to open `http://127.0.0.1:3000` on the install machine only.

The installer asks once about models it finds. `--yes` skips those questions. With `--yes`, exported keys are still saved, and nothing is signed in or installed unless you also pass `--connect-found` or `--install-ollama`.

What the install does:

- The API, console, and database listen on `127.0.0.1` only.
- Installer keys go into `.env` in the install directory, mode `0600`. The installer does not print them. Console connections use the selected secret backend.
- It seeds a local project, `boss-dogfood`.
- It puts a `boss` command on your `PATH`. Open a new terminal if the command is not found, or run `~/.local/bin/boss`.
- It does not add your user to the `docker` group. If Docker needs an administrator, later manual Compose commands are `sudo docker compose`.

Then, on the install machine:

```bash
boss status
boss doctor
```

`boss doctor` checks configuration. It does not ask a model to answer unless you pass `--probe`. A healthy API is not a working repository or a working model.

## Claim the install

Nobody owns a new install until you claim it. On the install machine:

```bash
boss identity setup-link
```

The command prints a link that works once, for one hour. Open it in the browser you will sign in with. Enter your name and email. Create a passkey. On an IP address, where the browser will not create a passkey, choose a password and an authenticator app. Save the recovery codes.

`boss open`, `boss doctor`, and the sign-in page say the install is not claimed until you do this. `boss password` prints the one-time console password in a terminal, if you would rather claim with that than with the link. It is not written to a log.

On the install machine, open `http://127.0.0.1:3000`. From another computer, use the hostname you set, or the tunnel in [sign-in](CONSOLE_AUTH.md). An install that already has an Owner follows [upgrade to named accounts](IDENTITY_UPGRADE.md).

## Connect a repository

BOSS opens on **Setup** until a repository and a model are connected.

Under **Repos**, choose **My GitHub account** or **An organisation**. Click **Connect GitHub**. On GitHub, create the app, tick the repositories BOSS may use, and install it. Back in BOSS, pick one repository and click **Connect**.

The default path does not ask you to paste a token. Access is the GitHub App on your install. BOSS does not push to your default branch. Use a non-production repository for the first attempt. Fixes, and the cases that do need a token, are in [Connect GitHub](CONNECT_GITHUB.md).

## Connect a model

Under **Model**, add a provider key or sign a coding subscription in inside BOSS. `boss connect` does the same from a terminal. The row turns green when the connection answers a test question.

That test can be a real call. It is budgeted, and it is not proof that a later build will finish. Importing keys with `boss connect keys` stores them without a completion. The first governed check is what proves them. Claude Code and Grok sign in again inside BOSS. Each provider, and what a local model cannot do, is in [model connections](MODEL_CONNECTIONS.md).

`boss doctor --probe` asks a model to answer. That uses model credit or local compute.

## Open one pull request

When the header says **Next: open your first PR**, open **Try it**. Describe one small change and press **Open a PR**. The first time, BOSS asks you to allow its agents to act for you on that repository. Allow it only if you mean that.

The pull request appears on GitHub for you to review. This can spend model credit, and it can write a branch. It is not a merge, and it is not evidence that the result is useful. Follow the run before you treat it as finished. The [first change](FIRST_USEFUL_CHANGE.md) is how to read the plan, the revision, and the receipt.

Installing an agent definition, or one of the [fifteen example workflows](https://github.com/seanventures/boss/blob/main/examples/README.md), does not start work.

## If you get stuck

Run `boss status` and `boss doctor`. Keep the release and the error. Leave credentials and private task data out of the report. The [feedback page](FEEDBACK_LOOP.md#stuck-during-your-first-run) is the next step. Security reports go by email, as [SECURITY.md](https://github.com/seanventures/boss/blob/main/SECURITY.md) says.

`boss upgrade` backs up, then moves release. `boss uninstall` stops BOSS and keeps the data. Deleting data is a separate confirmation. See the [installer guide](https://github.com/seanventures/boss/blob/main/scripts/install/README.md#upgrade--recover).

## Optional: store one task without a model

This checks that the API can store a task. It does not call a model, send email, or deploy.

From the directory you passed to `--dir`:

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

| Request | What you should see |
| --- | --- |
| `/health` | A health response with no secrets in it. |
| `/v1/edition` | The edition for the local project in `BOSS_PROJECT_ID`. |
| `/v1/tasks` | The new task, titled `Local note`, created by the **Host tools** service account. |

Run the task command a second time. The API rejects the repeated `dedupe_key` instead of creating another task. That is this API. A dedupe key does not make every provider idempotent.
