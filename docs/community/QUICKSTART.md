# The path

Install [v1.0-rc10.31](https://github.com/seanventures/boss/releases/tag/v1.0-rc10.31), claim the install, connect one repository, connect one model, and open one pull request.

This is the short path on a machine you administer. [Your hostname](hostname.md) covers HTTPS. [Sign in](CONSOLE_AUTH.md) covers passkeys, passwords, and an SSH tunnel. The [first change](FIRST_USEFUL_CHANGE.md) is what you do with the pull request after it exists.

This is a release candidate. This tag was installed from this page, word for word, on an Ubuntu 24.04 x86_64 host before it was published, and walked through claim, Setup, a sample project and the model screen. The [release notes](RELEASE_NOTES_v1.0-rc10.31.md) say what changed and what was not checked.

You need about 30 minutes, and for the last step a GitHub account (or code kept on the install machine) and one model: a Claude, ChatGPT or Grok subscription, an API key (OpenRouter, OpenAI, Anthropic, Gemini or xAI), or a local model.

## Before you begin

The installer is written for one of these hosts:

- Ubuntu 22.04 or 24.04, or Debian 12 or 13, with `curl` and `sudo`. It can install Git, Python 3, and Docker.
- macOS with a Docker runtime running ([Colima](COLIMA.md) or Docker Desktop) and Apple's command line tools (`xcode-select --install`).
- Another Linux host that already has Git, `curl`, Python 3, and Docker Engine with Compose.

A model key is optional. The control plane starts without one.

On a company network (an outbound proxy, TLS inspection, or package registries that must come from an internal mirror), read [Enterprise network](ENTERPRISE_NETWORK.md) before you install. The installer checks for TLS inspection and tells you what to set.

The installer warns when free disk is under 80 GB. The images include Java and Go by default (about 0.75 GB; [Toolchains](TOOLCHAINS.md) says how to turn them off). Docker Desktop keeps images on its own disk, which can fill while the host still looks fine. If Postgres fails to initialize, the installer prints the database log. Free space in Docker and run the installer again. A rerun keeps credentials it has already written. Sizing is in the [installer guide](https://github.com/seanventures/boss/blob/v1.0-rc10.31/scripts/install/README.md).

## Install

Install this tag from its checkout. You need Git. If GitHub asks you to sign in, use an account that can read the repository. On Ubuntu or Debian without Git, first run `sudo apt-get update && sudo apt-get install -y git`.

```bash
(
  set -e
  git clone --branch v1.0-rc10.31 https://github.com/seanventures/boss.git boss-rc10.31
  cd boss-rc10.31
  BOSS_RELEASE_COMMIT="$(git rev-parse HEAD)"
  ./install.sh --dir "$PWD" --ref "$BOSS_RELEASE_COMMIT"
)
```

Use a new directory named `boss-rc10.31`. The command stops if the clone fails. It records the checked-out commit and stays on that source when you rerun it. Continue from `cd boss-rc10.31`. To move later, on purpose, use `boss upgrade --to REF`.

The script at `https://boss.sean.network/install.sh` is published separately. Do not treat it as this tag.

Add `--console-origin https://boss.example.com` (your hostname, not this example) when anyone will open the console from another machine. Set that before you claim. The [hostname guide](hostname.md) is the proxy, certificates, and private-IP case. Omit the flag to open `http://localhost:3000` on the install machine only.

The installer asks about model keys and sign-ins it can use, and offers a local model (about 4.4 GB; the default is No). `--yes` skips those questions. With `--yes`, exported keys are still saved, and nothing is signed in or installed unless you also pass `--connect-found` or `--install-ollama`.

What the install does:

- The API, console, and database listen on `127.0.0.1` only.
- Installer keys go into `.env` in the install directory, mode `0600`. The installer does not print them. Console connections use the selected secret backend.
- It seeds one local project, `boss-dogfood`, for BOSS's own housekeeping. Your projects are the ones you add.
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

The command prints a link that works once, for one hour. Open it in the browser you will sign in with. The installer's last screen and `boss doctor` print the same next step.

1. **Claim this BOSS**: enter your name, email and a password of 12 characters or more. Click **Claim and continue**.
2. **Protect your account**: choose **Passkey on this device** (recommended) or **Authenticator app (code)**. For the app, scan the QR code (or type the key) and enter the six-digit code.
3. **Save your recovery codes**: download, print or copy them, tick **I've stored them somewhere safe**, and click **Continue setup**.

`boss open`, `boss doctor`, and the sign-in page say the install is not claimed until you do this. `boss password` prints the one-time console password in a terminal, if you would rather claim with that than with the link. It is not written to a log.

Use the same address the link printed (`http://localhost:3000` on the install machine) every time you sign in: a passkey belongs to the exact address it was made on, so `127.0.0.1` and `localhost` are different sites to your browser. From another computer, use the hostname you set, or the tunnel in [sign-in](CONSOLE_AUTH.md). An install that already has an Owner follows [upgrade to named accounts](IDENTITY_UPGRADE.md).

## Connect a repository

After the claim, BOSS opens on **Setup**, with **① Repos** and **② Models** and a count of what is left. **Get started** (top of Setup) is the same three steps as a guided page.

You can keep code on the install machine, or connect GitHub:

- **On this machine:** **Projects › Add project › Start a new app › This machine**, or **Try a sample**. BOSS makes a local repository and Setup shows Repos as **On this machine**. Pull requests stay inside BOSS. GitHub is optional.
- **GitHub:** under **Repos**, click **Connect GitHub**. GitHub opens with BOSS's app filled in: click **Create**, pick the repositories BOSS may work on, click **Install**. Back in BOSS, pick your repository. **Other ways** offers a token instead.

The default path does not ask you to paste a token. Access is the GitHub App on your install. BOSS does not push to your default branch. Use a non-production repository for the first attempt. Fixes, and the cases that do need a token, are in [Connect GitHub](CONNECT_GITHUB.md).

## Connect a model

Under **② Models**, click **Add model**. Three tabs:

- **Subscription:** Claude Code, Codex or Grok Build. Click **Sign in**, approve on the provider's page, copy the code it shows, paste it back and submit.
- **API key:** OpenRouter, OpenAI, Anthropic, Gemini, xAI or another OpenAI-compatible endpoint. Paste the key, pick a model, click **Connect**.
- **Local:** Ollama, or another URL on your network.

`boss connect` does the same from a terminal. The row turns green when the connection answers a test question.

That test can be a real call. It is budgeted, and it is not proof that a later build will finish. Importing keys with `boss connect keys` stores them without a completion. The first governed check is what proves them. Claude Code and Grok sign in again inside BOSS. Each provider, and what a local model cannot do, is in [model connections](MODEL_CONNECTIONS.md).

`boss doctor --probe` asks a model to answer. That uses model credit or local compute.

## Open one pull request

With a repository and a model connected, pick one way to start:

- **Command Center:** pick a template (for example **CSV quality desk**) or describe an app, and press **Easy Mode**. Templates build web apps, browser games, maps, 3D/XR pages, data tools and small services in Python, Node or Go (Python, Node, Java and Go are all built in). If someone turned a toolchain off, its cards say **Needs Go** or **Needs Java** with the command to turn it back on (`boss toolchains enable go`). BOSS does not build Android or iOS apps.
- **Try a sample** on **Projects › Add project**: a small sample app, kept on this machine. If something is still missing, the **Setup needed** box lists it up front; **Start anyway** lets you add it before the step that needs it.
- **On a project you already have:** open it, press **New work**, describe one small change, and start it.

The first time, BOSS asks **Let … act for you?** and lists the agents, the permissions and the repository. Choose **For this run only** (Easy Mode says **For this build only**) or **For 30 days**. Allow it only if you mean that.

The pull request appears on GitHub (or, for code on this machine, in BOSS) for you to review. This can spend model credit, and it can write a branch. It is not a merge, and it is not evidence that the result is useful. Follow the run before you treat it as finished. The [first change](FIRST_USEFUL_CHANGE.md) is how to read the plan, the revision, and the receipt.

Installing an agent definition, or one of the [fifteen example workflows](https://github.com/seanventures/boss/blob/v1.0-rc10.31/examples/README.md), does not start work.

## If you get stuck

Run `boss status` and `boss doctor`. Keep the release and the error. Leave credentials and private task data out of the report. The [feedback page](FEEDBACK_LOOP.md#stuck-during-your-first-run) is the next step. Security reports go by email, as [SECURITY.md](https://github.com/seanventures/boss/blob/v1.0-rc10.31/SECURITY.md) says.

`boss upgrade` backs up, then moves release. `boss uninstall` stops BOSS and keeps the data. Deleting data is a separate confirmation. `boss stop` and `boss start` stop and start this install. See the [installer guide](https://github.com/seanventures/boss/blob/v1.0-rc10.31/scripts/install/README.md#upgrade--recover).

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
