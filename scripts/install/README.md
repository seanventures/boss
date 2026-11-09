# BOSS Community installer

Install BOSS Community on a machine you administer. This page is the installer reference: the one-line install, the `boss` command, the commit-pinned GitHub command, local checkouts, network and secret defaults, upgrade, recovery, and uninstall. For a single guided path from install to the console and a first task, use the [quickstart](../../docs/community/QUICKSTART.md).

## Install

For v1.0-rc10.12, use the [commit-pinned GitHub checkout](#straight-from-github). You need Git and repository access; use an account with access if GitHub asks you to sign in. The website installer and hosted manual are deployed separately, so source publication alone does not verify their version.

### One command

```bash
curl -fsSL https://boss.sean.network/install.sh | bash
```

That is the whole install on a fresh Ubuntu 22.04/24.04 or Debian 12/13 machine, or on macOS with Docker Desktop running. It needs no answers:

- It finds the newest published release (`v*` tag), downloads that release's `install.sh` from GitHub over HTTPS, and installs that release into `~/boss`. A failed download runs nothing.
- On Ubuntu and Debian it installs what is missing: `git`, `curl`, Python 3, and Docker Engine with Compose from Docker's signed apt repository. At a terminal, `sudo` asks for your password once; with `--yes` it never prompts.
- It uses the database secret store, turns Mission Control on, and keeps every listener on `127.0.0.1`.
- The only question is an optional OpenAI key, asked only when no provider key is exported and a person is at the terminal. `--yes` asks nothing.
- After the stack is healthy it runs the [Models](#models) step: it finds the model keys, subscriptions, coding CLIs, and Ollama already on the machine, connects them with your consent, and offers a free local model when nothing else is usable. Then it installs the [`boss` command](#the-boss-command).

Pass installer flags after `bash -s --`:

```bash
curl -fsSL https://boss.sean.network/install.sh | bash -s -- --yes
curl -fsSL https://boss.sean.network/install.sh | bash -s -- --ref v1.0-rc10.12 --console-origin https://boss.example.com
```

`BOSS_REF=main` (or `--ref main`) installs the development branch instead of a release.

### Models

After the stack is healthy, the installer runs one **Models** step. `boss connect` runs the same step again at any time. It works in three parts.

**1. Detect.** Each source is reported as `found` or `not found`, and no value is ever printed. `boss connect --list` prints this report and changes nothing:

- provider keys exported in this shell (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY`, `XAI_API_KEY`, `OPENROUTER_API_KEY`)
- a Codex ChatGPT login (`codex login status`), a Claude Code login (`claude auth status`), and a Grok Build login file (existence only)
- the `claude`, `codex`, and `grok` programs on `PATH`
- Ollama, whether it is running and answering on `127.0.0.1:11434` or installed but stopped, and the models it has already pulled

**2. Connect what was found.** Each connection needs its own yes at a terminal:

| Source | What happens |
|---|---|
| Exported provider keys | Saved to the private `.env`. Exporting the key for this command is the consent. |
| Codex, Claude Code or Grok Build login | The login on this machine is never copied. Asks once whether to sign that subscription in inside the BOSS API container, as the `boss` user, through the provider's own login: BOSS prints the link and code, you finish it in a browser, and BOSS proves the connection with one test turn. There is no question when the CLI is already signed in there. `--yes` signs nothing in unless you add `--connect-found`. Even then a sign-in needs a person at a terminal; without one, the card says to run `boss connect codex`, `boss connect claude` or `boss connect grok`. |
| Ollama running on this machine | Asks once whether to connect it. `--yes` connects it only with `--connect-found`. See [Ollama](#ollama). |

**3. When no model is usable, offer one.** "Usable" means a connected API key, subscription, or Ollama. These offers come only at a terminal, one question each:

- Sign in to Codex, then Claude Code, then Grok Build, inside BOSS. Each question defaults to no.
- Set up a free local model, with the download size in the question. This one defaults to **yes**, because without it BOSS has no model; it is still a question because the download is several GB. See [The local model](#the-local-model). `--no-local-model` turns this offer off.
- The installer only: install the official coding CLIs on this machine, through the same path as `--install-coding-clis`. Codex needs its CLI here to sign in; Claude Code and Grok Build already run inside BOSS. This question defaults to no.

`--yes` asks nothing and installs nothing extra. Only explicit flags install: `--install-ollama` (with `--local-model NAME`) and `--install-coding-clis`.

The summary card ends with one row per source: API keys, Codex, Claude Code, Grok Build, Ollama, and the default model. Each row says connected, found but not connected, or not found, with the command that fixes it. `boss connect --summary` prints the same rows. A connection that fails is shown with `✗`, and BOSS itself stays installed; retry with `boss connect`. `--no-discover` skips detection.

A Claude Code sign-in is refused while `.env` holds `ANTHROPIC_API_KEY` or a legacy `CLAUDE_CODE_OAUTH_TOKEN`, because Claude Code would use that key instead of the subscription. Remove the line, run `boss update`, then run `boss connect claude`.

### Ollama

Connecting an Ollama that is already running does the following:

1. The API and worker containers reach the host as `host.docker.internal`. The Compose file maps that name with `extra_hosts: host-gateway`; Docker Desktop resolves it already. No port is published.
2. The API container checks that it can read Ollama's model list at `http://host.docker.internal:11434`. If it cannot, nothing is changed, and the fix is printed.
3. BOSS registers Ollama as an OpenAI-compatible model (`POST /v1/setup/model`, kept for one release beside Repo and models' `POST /v1/setup/models`), with:
   - the base URL `http://host.docker.internal:11434/v1`
   - the first model Ollama lists, or `--local-model` or `BOSS_OLLAMA_MODEL` when that model is pulled
   - the placeholder key `ollama`, because the form requires a key and Ollama ignores it
4. When no hosted model is connected (no provider key in `.env`, no recorded subscription, and no API-key connection), Ollama becomes the model for every role: implementer, reviewer, hard problems, and routine work. This goes through the project's role settings. A role you already chose is left alone. Chat, meetings, and the other surfaces that read `OPENAI_*` then use it too: BOSS sets `OPENAI_API_KEY=ollama`, `OPENAI_BASE_URL` to the local address, and `OPENAI_MODEL` in `.env`, but only when `.env` holds no hosted OpenAI key. With a hosted model connected, chat is untouched. When a hosted model is connected, Ollama is added but not made the default. The summary card's `Default` and `Chat` rows show which applies.
5. The model is priced at zero for the local endpoint only: the `BOSS_MODEL_PRICES_JSON` key is `<local base URL>#<model>`, for example `http://host.docker.internal:11434/v1#qwen2.5-coder:7b`, so the same model name on a hosted provider keeps its normal price. Other entries are kept, and only api and worker are recreated so they read `.env`.
6. BOSS asks the model one tiny question before it records the connection — reserved against the budget (as a $0 gate: a local model costs nothing) and receipted, like every model check. A model that does not answer is not connected, and the reason is printed.

An existing **Anything OpenAI-compatible** connection to another address is never replaced; replace it in Mission Control if you want Ollama instead. A local Ollama never takes the native (OpenAI-compatible) lane from a hosted connection: with an OpenAI key saved in the setup screen or in `.env`, that key is used whichever was saved last; Ollama is used there only when nothing hosted is connected.

When a hosted model is connected later, `boss connect` hands back the roles the fallback set (lanes that still say native with the Ollama model return to their defaults), and `boss connect keys` with a new `OPENAI_API_KEY` moves chat from Ollama to that key. An installer rerun with an exported OpenAI key does the same for chat. Ollama stays connected.

### The local model

`--install-ollama` (for the installer, or `boss connect --install-ollama`) does the whole local setup without asking. A yes to the offer above does the same:

1. **Install Ollama if it is missing.** On Linux, BOSS downloads the official installer from `https://ollama.com/install.sh` and runs it; a failed download runs nothing. On macOS, BOSS runs `brew install ollama` when Homebrew is present. Without Homebrew, BOSS tells you to install the app from https://ollama.com/download and stops.
2. **Start it if it is stopped.** On Linux this uses `systemctl enable --now ollama`. On macOS it uses `brew services start ollama`, or opens the app. Otherwise BOSS runs `ollama serve` in the background, which does not survive a reboot.
3. **Make it reachable from the containers.** On Linux, Ollama listens on `127.0.0.1`, which containers cannot reach. BOSS therefore binds the Ollama service to the Docker bridge address (the `host-gateway` address, usually `172.17.0.1`), which this machine and its containers can reach and your network cannot. It never binds to `0.0.0.0`.
   - An Ollama that was already installed or running before this run belongs to you, and other programs may use it on `127.0.0.1`, so BOSS asks before moving it (default no). An unattended run never moves it unless you pass `--rebind-ollama`; without that, it stops with the fix. An Ollama that this run installed is moved without asking.
   - The change is a systemd drop-in, `/etc/systemd/system/ollama.service.d/boss-docker-bridge.conf`, with `OLLAMA_HOST=<bridge address>:11434` and `After=docker.service`. It needs `sudo`.
   - BOSS records the new address as `BOSS_OLLAMA_URL` in `.env`.
   - Afterwards the `ollama` command needs `export OLLAMA_HOST=<bridge address>:11434`. To undo the change, delete the drop-in and run `sudo systemctl daemon-reload && sudo systemctl restart ollama`.
4. **Pull the model.** The default is `qwen2.5-coder:7b`; `--local-model NAME` picks another. Before pulling, BOSS prints the download size (from the model's manifest on the Ollama registry) and the free disk space where Ollama keeps models. It stops when there is less free space than the download plus 2 GB, or less than 10 GB when the size is unknown. A model that is already pulled is not pulled again.
5. **Connect it** as described in [Ollama](#ollama): registered, made the default when nothing hosted is connected, priced at zero, and checked from the container.

Each step that fails stops the setup with `✗` and the command to retry; BOSS itself keeps running.

### The `boss` command

The installer puts `boss` in `~/.local/bin` (or in `/usr/local/bin` when that is already writable and `~/.local/bin` is not on `PATH`; `BOSS_BIN_DIR` overrides both). It never needs `sudo` and never replaces another program's `boss`. It remembers the install directory, so it works from any directory:

| Command | What it does |
|---|---|
| `boss status` | Install directory, release, services, and API health |
| `boss open` | Prints the Mission Control address |
| `boss password` | Shows the console password, in a terminal only: it claims a new install, then signs teammates in as Operator for 30 days after the claim. `boss password reset` rotates it |
| `boss doctor` | Runs `checkup.sh` (`--probe` also asks a model a question) |
| `boss logs [-f] [api\|worker\|web\|db]` | Recent service logs |
| `boss connect` | Runs the [Models](#models) step again. It saves newly exported provider keys into `.env` and restarts only api and worker. It then asks once before each sign-in or connection, offers a subscription sign-in or a local model when nothing is usable, and ends with the Models table. `--connect-found` answers yes for what was found. `--yes` asks nothing and installs nothing |
| `boss connect --list` / `--summary` | Prints what this machine has, or the Models table, and changes nothing |
| `boss connect --install-ollama [--local-model NAME]` | Sets up [the local model](#the-local-model) end to end without asking |
| `boss connect keys` | Saves `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY`, `XAI_API_KEY`, or `OPENROUTER_API_KEY` exported in this shell into the private `.env` (mode `0600`), then recreates the api and worker containers so they read it. Names only are printed; a key is never accepted as an argument. No `boss update` needed |
| `boss connect codex\|claude\|grok\|ollama` | Connects one. `codex`, `claude` and `grok` sign in inside the BOSS container through the provider's own login (open the link it prints), then prove it; they need a terminal. `ollama` connects a running Ollama |
| `boss upgrade [--to REF]` | Backs up, then moves to the newest release, or to `REF`. `--dry-run` shows the move first |
| `boss update` | Re-applies configuration on the installed release: reruns the installer so newly exported keys and flags such as `--web` take effect. It never changes the release |
| `boss uninstall [--purge]` | Stops BOSS and keeps data and the `boss` command. `--purge` deletes data after a backup and a typed confirmation, then removes the `boss` command the installer wrote for this install (one that belongs to another program or another install is left alone) |
| `boss version` | Installed release and commit |
| `boss identity CMD` | Named accounts from the host shell: `setup-link` (a one-hour claim link), `claim-status`, `recover-owner --email E`, `host-key`, `legacy status`, `posture`, `audit verify`. Runs in the api container as the `boss` user. See [Identity administration](../../docs/community/IDENTITY_ADMIN.md#host-shell-commands) |

### Which release, and staying on it

The installer records the ref it installed in `.boss-release` in the install directory (not secret, and listed in the checkout's `.git/info/exclude` so it never shows as a local change). Reruns and `boss upgrade` read it, so a release install never moves to `main` by itself:

| Channel | Installed with | `boss upgrade` without `--to` |
|---|---|---|
| `release` | a `v*` tag, the default | the newest stable release tag. A prerelease install may also move to a newer prerelease. Never an older tag |
| `branch` | `--ref main` or another branch | fast-forwards that branch |
| `pinned` | `--ref` with a full commit SHA | refuses; pass `--to REF` |

An install made before this record existed stays where it is: a checkout at a release tag keeps that tag, a detached commit stays on that commit, and a branch checkout follows `main` as before. `--ref` always wins and is recorded.

### Release one-liner

```bash
curl -fsSL https://boss.sean.network/install.sh | bash
```

This is the same command as [Install](#install): it installs the newest release into `~/boss` and records that release, so reruns and `boss upgrade` stay on releases. Pass installer flags after `bash -s --`. To read the script first, download it with `curl -fsSL https://boss.sean.network/install.sh -o install.sh`.

### Straight from GitHub

Install v1.0-rc10.12 without the website. This uses Git authentication if the repository requires access, checks out the published commit for that tag, and installs that same commit:

```bash
(
  set -e
  BOSS_RELEASE_COMMIT=ae15726290b2044de556e4f9166de0d38690009c
  git clone --branch v1.0-rc10.12 https://github.com/seanventures/boss.git boss-rc10.12
  cd boss-rc10.12
  git checkout --detach "$BOSS_RELEASE_COMMIT"
  ./install.sh --dir "$PWD" --ref "$BOSS_RELEASE_COMMIT"
)
```

The `boss-rc10.12` directory must not already exist. Read the script in that checkout before running it if you want to inspect it. This records a pinned commit; a later upgrade needs `boss upgrade --to REF`. Add installer flags to the `./install.sh` line, for example `--console-origin https://boss.example.com`.

### From a local checkout

Clone the release and install it in place:

```bash
git clone --branch v1.0-rc10.12 https://github.com/seanventures/boss.git
cd boss
./install.sh --dir "$PWD" --ref v1.0-rc10.12
```

A checkout at a release tag stays on that release. A checkout on a branch follows `main` unless you pass `--ref`, and the ref is then [recorded](#which-release-and-staying-on-it). From a checkout you already have on the branch you want:

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
2. Chooses defaults without asking (`~/boss`, database secret store, Mission Control on). The only prompt is an optional **BYOK** OpenAI key, and only when no provider key is exported.
3. Clones or fast-forwards the BOSS repo at the recorded or newest release, without deleting a nonempty directory or overwriting tracked local changes, and records the ref in `.boss-release`.
4. Writes a private `.env` (separate owner and worker API keys generated, mode 600).
5. Runs `docker compose` to build and start **Postgres + API + worker** (+ Mission Control if selected).
6. Waits for `/health`, runs the DB seed, and fails visibly if seeding or edition verification fails.
7. Verifies the Community edition and ProviderOps surface.
8. Reports the model keys and subscriptions it finds on this machine and connects only what you agree to.
9. Installs the `boss` command and prints a summary card: console address, how to get the password, the release, what was connected, and the next commands.

The health wait allows up to 60 attempts within 180 seconds, including requests
and retry sleeps. Each request has a 2-second connection limit and at most
5 seconds total, reduced to the remaining time. On timeout, check the printed
health URL, API/database logs and whether another service occupies the API port.
HTTP success alone does not verify that an unrelated listener is BOSS. Log
collection and other installation phases are outside this wait budget.

### Reruns

A rerun retains both API credentials and any operator-added API keys, the database password, model keys, the secret-store encryption key, the console login, public URL, console origin, API bind, port settings, the Mission Control choice, and the secret-store choice, unless this run sets the console origin or API bind again.

- Use `boss upgrade` (the lifecycle [upgrade command](#upgrade--recover)) to back up state before moving a long-lived install to a newer release.
- A rerun remembers which release you installed. Without `--ref`, it stays on the ref recorded in `.boss-release`, so changing a setting never moves a release install to `main`: `boss update --install-coding-clis` or `./install.sh --dir "$PWD" --install-coding-clis`.
- Pasting the installer line again also backs up an existing running install before switching to a newer source revision. A stopped database container is started from its existing image ID for that snapshot, then returned to its stopped state. If that container is missing, cannot start, or backup fails, the source and configuration stay untouched. The backup path is printed; keep it private.
- For an older headless install, pass `--no-web` once to record that choice; use `--web` to enable the console later.
- The local test inbox (Mailpit) is off unless you pass `--with-mailpit` (or set `BOSS_EMAIL_DEV_INBOX=1`). A rerun keeps that choice; `--no-mailpit` turns it off. Real mail goes through the Email (SMTP) connector: see [Email](../../docs/community/EMAIL.md).
- If you choose Infisical and it cannot be provisioned, the installer reports an incomplete install. The next rerun keeps that choice and retries it rather than silently switching to database credential storage.

### API keys

| Key | Belongs to | Used by |
|---|---|---|
| `BOSS_HOST_API_KEY` | Service account **Host tools** | The `boss` command, `boss doctor`, `make metatron`, `remote.sh` and the model helpers. `boss` issues it when missing or refused; `boss identity host-key` makes a new one |
| `BOSS_MCP_API_KEY` | Service account **MCP (project)** | `make mcp`, after `make mcp-setup` |
| `BOSS_WORKER_API_KEY` (worker) | Seeded `system` member | The worker container only; it can claim and settle leased jobs, so never expose it elsewhere |
| `BOSS_API_KEY` (shared, legacy) | Legacy console key | Older tools. It loses people-and-access changes when the install is claimed and stops 30 days later |

People use their own keys: **My account › API keys** in the console.

Existing single-key installs receive a new worker key on the next installer rerun or upgrade, without rotating the owner key.

### Docker access

The installer does not add your account to the root-equivalent Docker group. On a Linux host where only the administrator can access Docker, run Compose with `sudo` or grant Docker access deliberately through your host policy.

Default local Linux installs can use sudo. Custom project names, image tags, or Docker connection settings need Docker access as your user account. The helpers stop when sudo cannot safely preserve your selection. They never forward Docker credentials or preserve the full environment.

Restore uses the target project you specify and the validated archived images. Running apps in a custom restored project still needs direct Docker access. Keep the same project and image-tag settings for each lifecycle command.

## Mission Control access

Mission Control has its own single-operator password and signed session secret, generated on first install and stored only in the private `.env`. The password is never the BOSS API key. On the VM, retrieve or rotate it deliberately:

```bash
boss password          # or: cd ~/boss && python3 scripts/install/console_password.py show
boss password reset    # then run the web recreate command printed by the helper
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

The Community API and worker images already contain pinned Claude Code, Codex CLI, and Grok Build binaries, and each subscription signs in **inside** the BOSS API container, as the `boss` user, through the provider's own login. A login on your laptop or on the VM's own account is never copied into BOSS:

```bash
boss connect codex     # or: boss connect claude, boss connect grok
```

BOSS prints the link and one-time code that the provider's login shows. Open the link in any browser and enter the code; Claude Code instead shows a code on its page for you to paste at the hidden prompt, which its own login reads inside BOSS. BOSS then connects the subscription and proves it with one governed test turn (`BOSS_OK`). A refusal prints as BOSS's own "headline — fix". The link expires after 15 minutes; Ctrl-C cancels. The console's **+ Add model › Subscription › Sign in** and `remote.sh subscription --provider codex|claude_code|grok` run the same sign-in.

| Flag | What it does |
|---|---|
| `--connect-codex` | Signs Codex in inside BOSS as above, then proves it. Needs a terminal; without one the installer says to run `boss connect codex` and still finishes |
| `--connect-claude` | The same for Claude Code |
| `--connect-claude-local` | Records a Claude Code login already made inside the API container |
| `--connect-grok-local` | Records a Grok device login already made inside the API container |
| `--install-coding-clis` | Installs missing official CLIs for the VM user (for your own use on the VM; BOSS does not need them) |
| `--discover-subscriptions` | Checks the VM user's login status only; sends no credentials |

- Claude Code is refused while `.env` holds `ANTHROPIC_API_KEY` or a legacy `CLAUDE_CODE_OAUTH_TOKEN`, because headless work would use that instead of the subscription.
- The CLI homes (`/home/boss/.codex`, `.claude`, `.grok`) are private volumes shared by the API and worker. Safe uninstalls and upgrades keep them; a data purge removes them.

### Grok Build

Grok's device session is tied to the Unix account and home that runs BOSS, which is why it signs in inside the container: a login under the SSH admin account does not authenticate the containers. BOSS never copies or parses `~/.grok/auth.json`.

- Once the Grok harness is available and its BOSS readiness proof passes, set `BOSS_CODE_AGENT_ENGINE=grok` in `.env` and recreate the API and worker. This selects Grok for governed coding work, not the console's chat model.
- To run governed coding work on an Anthropic API key as Agent SDK usage (metered on your Anthropic account, never the Claude subscription), store the key in Repo and models -> Anthropic API, then pick **Claude Agent SDK** for Build, Review or Chat there, or set `BOSS_CODE_AGENT_ENGINE=claude_agent_sdk` in `.env` for the whole install and recreate the API and worker. `BOSS_AGENT_SDK_MODEL` sets its model (default `claude-sonnet-5`).

> [!NOTE]
> A CLI installed on the host does not by itself mean the BOSS containers are connected.

## Repository binding

The BOSS source URL the installer uses is not an operator repository binding. Fresh Community installs leave `BOSS_GITHUB_OWNER` and `BOSS_GITHUB_REPO` blank, so the legacy default-project webhook route does not point at the BOSS source repository. Register your repository in the project setup flow. If you intentionally use the legacy default webhook route, set both variables in the private `.env` and rerun the installer; reruns retain those explicit values.

## Files

| Path | Role |
|---|---|
| `scripts/install/bootstrap.sh` | Release bootstrap: finds the newest release and runs its `install.sh`; imported by the separate website |
| `install.sh` | The installer (repo root) |
| `scripts/install/boss.sh` | The `boss` command; the launcher on `PATH` runs it from the install |
| `scripts/install/discover_models.py` | Read-only report of model keys, subscriptions, and Ollama on this machine |
| `scripts/install/connect_ollama.py` | Registers the Ollama on this machine through `POST /v1/setup/model` and applies the default-model rule (used by `boss connect`) |
| `scripts/install/model_status.py` | The Models table: one row per model source, read from BOSS and this machine |
| `scripts/install/local_model.py` | A model's download size from the Ollama registry, and its zero price in `.env` |
| `scripts/install/coding_subscriptions.py` | Read-only VM CLI discovery and explicit Claude/Codex binding |
| `.boss-release` (generated) | The ref this install follows; not secret |
| `scripts/install/community-compose.yml` | Self-contained Community stack |
| `.env` (generated) | Secrets + ports — never commit |

## Requirements

- Fresh VM: Ubuntu 22.04/24.04 or Debian 12/13, 64-bit, with `sudo` or root access
- Existing Linux/macOS host: `git`, `curl`, running Docker Engine/Desktop and Compose
- Existing distro Docker packages are not removed automatically; upgrade those manually

### Sizing

| | Need | Why |
|---|---|---|
| **Disk** | **About 100 GB recommended working capacity** | Images, build cache and agent repositories need space; the amount varies by selected services and workload. The installer reports the install filesystem separately from an accessible local Linux Docker image store, with an advisory below about 80 GB free. This is not a hard minimum. |
| **RAM** | **8 GB minimum, 16 GB comfortable** | The six containers idle at about 1.6 GB. Coding work runs inside the API container and adds whatever that task installs. Less than 8 GB will swap. |
| **CPU** | 4 cores | Reviewers run in parallel. |

If the host starts swapping, runs stall and BOSS records `host.stalled` on that machine. Add memory or free disk, then retry; retrying on a swapping host does not fix the stall.

The installer builds and loads one selected service image at a time to reduce overlapping disk use. This does not guarantee a build will fit. Docker Desktop and remote daemons have separate storage limits; when their free space cannot be measured locally, the installer reports it as unknown. Check the selected Docker host or Docker Desktop disk settings as well as the install volume.

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
boss doctor                             # free checks only (runs ./scripts/install/checkup.sh)
boss doctor --probe                     # also ask a model a question
./scripts/install/checkup.sh            # the same checks, from the install directory
```

Without `--probe`, the checks do not call a model. `--probe` asks a model a question and spends money. The script is `checkup.sh`; `boss doctor` and `boss checkup` both run it.

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
# boss-community.env.sh exports BOSS_API_URL, BOSS_API_KEY, and BOSS_PROJECT_ID.
source ~/boss/boss-community.env.sh
curl -fsS -H "Authorization: Bearer $BOSS_API_KEY" \
  "$BOSS_API_URL/v1/projects/$BOSS_PROJECT_ID/preflight"
```

`{"ready": true}` means no unresolved blockers from the last run and no recent host stalls. Warnings name what to fix first; running anyway spends real money to reach a known answer.

### When a run is slower than expected

```bash
source ~/boss/boss-community.env.sh
curl -fsS -H "Authorization: Bearer $BOSS_API_KEY" \
  "$BOSS_API_URL/v1/code-tasks/${RUN_ID:?the code task run id, from the console}/retrospective"
```

Returns three separate grades (delivery, efficiency, host) deterministically, with no model call. If `host` is `degraded` or `critical`, the time was spent on the machine and re-running will not improve it.

## After install

```bash
source ~/boss/boss-community.env.sh
curl -sS "$BOSS_API_URL/health"
# Mission Control: http://127.0.0.1:3000 — the first visit claims it (boss password proves it is you)
```

## Upgrade & recover

```bash
boss upgrade --dry-run                                      # show the release it would move to
boss upgrade                                                # back up, then move to the newest release
boss upgrade --to v1.1                                      # back up, then move to one ref
boss upgrade --no-rollback                                  # keep a failed release in place to inspect it
```

If the new release fails to start after its source is in place, `boss upgrade` puts the previous release back and starts it again, then says **Rolled back**. Data volumes are kept. Source rollback alone does not undo database migrations; compatibility must be verified for the two revisions. If that compatibility is unknown or rollback cannot start, restore the complete backup into a fresh stack (below).

### The database image

The database runs `boss-community-db:pg16.12-vector0.8.1-age1.6.0`, built by the installer from `scripts/install/db-image/Dockerfile`: PostgreSQL 16 on Alpine (the same base and libc as the `postgres:16-alpine` image earlier releases ran) with pgvector 0.8.1 and Apache AGE 1.6.0 compiled in. Upgrading to it is an image swap: the existing volume opens unchanged, and on its first boot the schema step creates the `vector` and `age` extensions. Nothing is dumped or restored. `boss upgrade` takes its protected backup before the swap, as for every upgrade. `engine/tests/test_db_image_upgrade_real_docker.py` proves the swap on a populated volume (row counts, `amcheck` on every B-tree index, both extensions loading) and a fresh install.

`boss upgrade` runs `scripts/install/upgrade.sh` from the install. Without `--to`, a release install moves to the newest published release tag and never to `main`; see [which release](#which-release-and-staying-on-it). The script works directly too:

```bash
./scripts/install/upgrade.sh --dir "$PWD"                   # back up, then move along the recorded channel
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
- artifacts and the identity volume containing the master key needed for Owner TOTP and encrypted connections
- the local repositories volume (`boss_community_repos`, `/var/lib/boss/repos`): the bare git repository of every local project and the GitHub mirrors. **Until a local project is published to GitHub, this is the only copy of its code.** It is archived in the same pause of writers as the database dump, so its commits match the change sets in the database
- a versioned completion manifest binding the source revision, database dumps, keys, artifacts and local repositories with checksums, and listing every repository archived

All running application writers, including egress, pause during the snapshot and resume even if it fails. CLI session volumes are excluded, and configured Claude/Codex session values are blanked from the archived `.env` and checked against decompressed content as described below. Their named volumes are retained during upgrade, and a data-purge recovery requires provider sign-in again.

> [!WARNING]
> Keep the backup outside the checkout and protect it like `.env`.

### Recover an older safe uninstall

Older versions of safe uninstall used `docker compose down`. That removed the database container but kept its named data volume. The current backup refuses to guess which database image (`postgres:16-alpine` before this release, `boss-community-db` since) should reopen that data, so if `backup.sh` reports a missing Postgres container, use this recovery path before upgrading.

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

Recovery must restore the database **and its matching identity key**. A database-only backup cannot recover encrypted TOTP factors and connections after their key volume is lost. Older archives without a complete recovery manifest are incomplete: preserve the original volumes and obtain a reviewed recovery plan. They cannot authorize a data purge.

Use `restore.sh` from the current recovery tools against a **fresh checkout at the backed-up source revision**, a new Compose project name, and no existing target `.env`, containers or volumes. Keep the existing installation intact. Restoring older data can erase the receipt or claim of an already accepted provider action, so application startup remains held independently of the restored database.

`BOSS_BACKUP` is the private backup path `backup.sh` or `upgrade.sh` printed (a `boss-…` directory under `~/boss-backups/`, or under `BOSS_BACKUP_DIR` if you set it). `BOSS_FRESH` is a new checkout at the revision in that backup's `source-commit` file. Run `restore.sh` from your current checkout:

```bash
BOSS_FRESH="$HOME/boss-recovered"
git clone https://github.com/seanventures/boss.git "$BOSS_FRESH"
git -C "$BOSS_FRESH" checkout "$(cat "${BOSS_BACKUP:?set BOSS_BACKUP to the backup path backup.sh or upgrade.sh printed}/source-commit")"
./scripts/install/restore.sh --backup "$BOSS_BACKUP" \
  --dir "$BOSS_FRESH" --project boss-recovered
```

The helper validates the completion manifest, file checksums, safe archive paths, master-key format and database key check value before creating target resources. It uses locally cached immutable database/API image IDs recorded by the backup; it never pulls or builds images. The target source revision must match the archive. It restores the identity, artifact and local repositories volumes before application startup, checks that every repository the manifest lists is in the restored volume, restores each database with `pg_restore --exit-on-error --single-transaction`, then checks the restored database/key pairing. On any failure it reports an incomplete recovery, leaves application services stopped, and retains the new target for inspection; it never deletes the original stack.

Normal recovery also requires a matching source/image pair containing the reviewed startup guard. Guard interface, image revision and guard/entrypoint file hashes are checked before target data resources. A separate recovery volume holds API, worker, egress and MCP startup before application imports; API checks it before privileged sandbox initialization. The exclusive held marker exists before any usable target `.env` or recovery base is published, closing the concurrent-start window. The complete environment appears atomically with the new project and image pins; owned volumes retain their Compose project/logical-volume labels. Base Compose retains the shared read-only mount and immutable image pins, so leaving out the generated image override does not remove the hold. MCP receives no identity-vault mount. The exact archived API/Python image is reused for the four held roles with explicit role commands; this does not qualify automatic readiness or resumption.

Pre-guard archives, including RC8, refuse normal recovery. Use explicit offline data-only mode with current recovery tools:

```bash
BOSS_FRESH="$HOME/boss-recovered-offline"
git clone https://github.com/seanventures/boss.git "$BOSS_FRESH"
git -C "$BOSS_FRESH" checkout "$(cat "${BOSS_BACKUP:?set BOSS_BACKUP to the backup path backup.sh or upgrade.sh printed}/source-commit")"
./scripts/install/restore.sh --backup "$BOSS_BACKUP" \
  --dir "$BOSS_FRESH" --project boss-recovered-offline \
  --offline-data-only
```

Offline mode persists a **DB-only base Compose** in the fresh target and retains the archived static Compose separately as `.boss-archived-compose.yml`. It declares no application roles even without the saved override. Identity/artifact restoration uses bounded network-disabled utility containers, not application startup. This intentionally modifies the target recovery workspace; rerunning an archived installer or replacing this base is outside the reviewed recovery procedure.

The recovered database uses an ephemeral loopback port so the original can keep its port. No application-start command is printed and there is no generic clear/acknowledge/resume operation. Preserve the recovery volume, original data, provider receipts and accepted-action evidence. Offline archive/key metadata, local cryptographic checks and history inspection do not establish a real Owner password/MFA sign-in, working `/health`, provider reconciliation or permission to resume. Provider-specific reconciliation and an explicitly reviewed migration/recovery plan must resolve outstanding liability before any restart. Moving source, images or schema requires separate qualification. See [Restored state hold](../../docs/community/RESTORED_STATE_HOLD.md) and [Provider recovery](../../docs/community/PROVIDER_RECOVERY.md).

Backups contain private `.env` and identity keys and must be protected together. The identity key must be a regular file under the Compose identity volume; custom external key locations require a separate reviewed backup procedure and are refused by the automatic backup. CLI session volumes are excluded; the archived `.env` blanks `CLAUDE_CODE_OAUTH_TOKEN` and `CODEX_AUTH_JSON`, including supported single-line quoted, whitespace and `export` forms. Interpolated, multiline or ambiguous session assignments are refused. The scan checks their decoded values and nested Codex token strings in decompressed database text and artifact contents. Encrypted durable BOSS/Infisical connection credentials are retained for recovery; this scan covers configured `.env` session values and does not claim to remove every credential from encrypted records. Unsafe archive members (including symlinks), missing volumes, incompatible keys and excluded CLI session material fail closed. If either excluded value is found in decompressed database or artifact content, backup fails and discards the incomplete archive without changing the original data. Remove the unexpected persisted session material through an appropriate reviewed data procedure, then retry. After moving machines or purging session volumes, sign in through the official provider CLI again.

### Remote (archive) installs

A remote archive install uses `.boss-shipped-commit` rather than Git.

- **Update:** the local `upgrade.sh` requires a Git checkout, so use `remote.sh update` from your machine instead; it backs up on the VM first.
- **Recover:** prepare a fresh checkout at the backed-up commit on the VM, then use current recovery tools and the complete database, identity-key, artifact and local repositories archive. Pre-guard source requires explicit offline data-only recovery. Success retains the startup hold; it does not authorize starting the archived API.
- **Failed update:** the new source is at the install path while the prior source remains at `PATH.prev` until installer/recreation succeeds. A failed build leaves the old containers running; after diagnosing the failure, you can move `PATH.prev` back to `PATH` before retrying. If recreation started, a database migration may have run: inspect the backup on that VM and use the complete recovery procedure above instead of only moving source. A failed source swap itself restores the prior source path, and the remote script refuses to discard a leftover `PATH.prev` on a later attempt.

`user@your-vm` is a machine you administer:

```bash
scripts/install/remote.sh --host user@your-vm update --ref main
scripts/install/remote.sh --host user@your-vm uninstall --yes
# Intentional data purge only:
scripts/install/remote.sh --host user@your-vm uninstall --yes --purge-data --confirm-purge DELETE-BOSS-DATA
```

## Uninstall / reinstall

```bash
boss uninstall                     # safe: stops BOSS, keeps data and credentials (asks first)
boss uninstall --purge             # backs up, asks you to type DELETE-BOSS-DATA, removes the boss command
```

`boss uninstall` runs `scripts/install/uninstall.sh`. The script works directly too:

```bash
./scripts/install/uninstall.sh --dir "$PWD" --dry-run
./scripts/install/uninstall.sh --dir "$PWD" --yes           # safe: keeps data and credentials
./install.sh --dir "$PWD" --yes --skip-model-key             # reuses the same volumes and keys
./scripts/install/uninstall.sh --dir "$PWD" --purge-data     # backs up, then asks for DELETE-BOSS-DATA
```

| Mode | Removes | Keeps |
|---|---|---|
| Safe uninstall (default) | Stops this Compose stack | Stopped containers and their exact image IDs, source, `.env`, named volumes, images, and backups, so a later upgrade can start the same database image for backup. Rerunning is safe. |
| `--purge-data` | This stack's named volumes (including Postgres, identity keys, artifacts, local repositories, and CLI sessions), then `.env` and the generated shell shim | Backups and the checkout |

`--purge-data` first requires a successful private backup unless you explicitly add `--skip-backup`. `--yes` alone never authorizes a data purge; for a scripted purge, add `--confirm-purge DELETE-BOSS-DATA`.

To stop:

```bash
cd ~/boss
./scripts/install/uninstall.sh --dir "$PWD" --yes
```

> [!WARNING]
> A manual `docker compose down` removes the stopped container needed for a later no-recreation backup. If an older uninstall already did that, use [Recover an older safe uninstall](#recover-an-older-safe-uninstall).

After a safe uninstall the `boss` command stays on `PATH`, so you can reinstall with `boss update`. `boss uninstall --purge` removes it once the purge has succeeded, but only when it is the launcher the installer wrote for this install directory: a `boss` that belongs to another program or manages another install is left alone. `uninstall.sh --purge-data` run directly does not touch the launcher. `--dry-run` with `--purge` names the launcher it would remove.

## Flags

Every flag is optional. See `./install.sh --help` for the full list. Added for the one-command install:

| Flag | What it does |
|---|---|
| `--yes`, `-y` | Never ask. Starts without a model key when none is exported |
| `--ref REF` | Install a release tag, branch, or full commit SHA, and record it |
| `--connect-found` | Connect what was found without asking: a Codex login, a Claude Code or Grok Build sign-in inside the BOSS container (only with a person at a terminal), and Ollama. The only way `--yes` connects anything |
| `--install-ollama` | Set up the local model without asking: install Ollama if missing, start it, make it reachable from the containers, pull the model, and connect it. See [The local model](#the-local-model) |
| `--local-model NAME` | The Ollama model to pull and connect (default `qwen2.5-coder:7b`) |
| `--no-local-model` | Never offer the local model |
| `--rebind-ollama` | Allow `--install-ollama` to move an Ollama you already had from `127.0.0.1` to the Docker bridge address (Linux) |
| `--no-discover` | Skip looking for model keys and subscriptions |
| `--no-boss-command` | Do not install `boss` on `PATH` |

`BOSS_BIN_DIR` chooses where `boss` goes. `BOSS_INSTALL_DIR` points `boss` at another install.
