# BOSS Community installer

Install BOSS Community on a machine you administer. This page is the installer reference: the one-line install, the `boss` command, the commit-pinned GitHub command, local checkouts, network and secret defaults, upgrade, recovery, and uninstall. For a single guided path from install to the console and a first task, use the [quickstart](../../docs/community/QUICKSTART.md).

## Install

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
curl -fsSL https://boss.sean.network/install.sh | bash -s -- --ref v1.0-rc2 --console-origin https://boss.example.com
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
| Codex ChatGPT login | Asks once before copying `~/.codex/auth.json` into BOSS's secret store. `--yes` copies nothing unless you add `--connect-found`. On the database store, that also needs `--accept-database-fallback`. |
| Claude Code or Grok Build login | The login on this machine is never copied. Asks once whether to sign the CLI in inside the BOSS API container, as the `boss` user, with the provider's own login (`claude auth login`, `grok login --device-auth`). The login is then recorded through the existing `--connect-claude-local` / `--connect-grok-local` path. There is no question when the CLI is already signed in there. `--yes` signs nothing in unless you add `--connect-found`. Even then a sign-in needs a person at a terminal; without one, the card says to run `boss connect claude` or `boss connect grok`. |
| Ollama running on this machine | Asks once whether to connect it. `--yes` connects it only with `--connect-found`. See [Ollama](#ollama). |

**3. When no model is usable, offer one.** "Usable" means a connected API key, subscription, or Ollama. These offers come only at a terminal, one question each:

- Sign in to Claude Code, then to Grok Build, inside BOSS. Each question defaults to no.
- Set up a free local model, with the download size in the question. This one defaults to **yes**, because without it BOSS has no model; it is still a question because the download is several GB. See [The local model](#the-local-model). `--no-local-model` turns this offer off.
- The installer only: install the official coding CLIs on this machine, through the same path as `--install-coding-clis`. Codex needs its CLI here to sign in; Claude Code and Grok Build already run inside BOSS. This question defaults to no.

`--yes` asks nothing and installs nothing extra. Only explicit flags install: `--install-ollama` (with `--local-model NAME`) and `--install-coding-clis`.

The summary card ends with one row per source: API keys, Codex, Claude Code, Grok Build, Ollama, and the default model. Each row says connected, found but not connected, or not found, with the command that fixes it. `boss connect --summary` prints the same rows. A connection that fails is shown with `✗`, and BOSS itself stays installed; retry with `boss connect`. `--no-discover` skips detection.

A Claude Code sign-in is refused while `.env` holds `ANTHROPIC_API_KEY` or a legacy `CLAUDE_CODE_OAUTH_TOKEN`, because Claude Code would use that key instead of the subscription. Remove the line, run `boss update`, then run `boss connect claude`.

### Ollama

Connecting an Ollama that is already running does the following:

1. The API and worker containers reach the host as `host.docker.internal`. The Compose file maps that name with `extra_hosts: host-gateway`; Docker Desktop resolves it already. No port is published.
2. The API container checks that it can read Ollama's model list at `http://host.docker.internal:11434`. If it cannot, nothing is changed, and the fix is printed.
3. BOSS registers Ollama through the same route as the setup screen's **Anything OpenAI-compatible** form (`POST /v1/setup/model`), with:
   - the base URL `http://host.docker.internal:11434/v1`
   - the first model Ollama lists, or `--local-model` or `BOSS_OLLAMA_MODEL` when that model is pulled
   - the placeholder key `ollama`, because the form requires a key and Ollama ignores it
4. When no hosted model is connected (no provider key in `.env`, no recorded subscription, and no API-key connection), Ollama becomes the model for every role: implementer, reviewer, hard problems, and routine work. This goes through the project's role settings. A role you already chose is left alone. Chat, meetings, and the other surfaces that read `OPENAI_*` then use it too: BOSS sets `OPENAI_API_KEY=ollama`, `OPENAI_BASE_URL` to the local address, and `OPENAI_MODEL` in `.env`, but only when `.env` holds no hosted OpenAI key. With a hosted model connected, chat is untouched. When a hosted model is connected, Ollama is added but not made the default. The summary card's `Default` and `Chat` rows show which applies.
5. The model is priced at zero for the local endpoint only: the `BOSS_MODEL_PRICES_JSON` key is `<local base URL>#<model>`, for example `http://host.docker.internal:11434/v1#qwen2.5-coder:7b`, so the same model name on a hosted provider keeps its normal price. Other entries are kept, and only api and worker are recreated so they read `.env`.
6. The API container checks that it sees the model in Ollama. No completion is run, because Community setup makes no model calls until model spend has its reservation, lease, and receipt path. The first governed job is what verifies the model.

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
| `boss password` | Shows the console password, in a terminal only. `boss password reset` rotates it |
| `boss doctor` | Runs `checkup.sh` (`--probe` also asks a model a question) |
| `boss logs [-f] [api\|worker\|web\|db]` | Recent service logs |
| `boss connect` | Runs the [Models](#models) step again. It saves newly exported provider keys into `.env` and restarts only api and worker. It then asks once before each copy, sign-in, or connection, offers a subscription sign-in or a local model when nothing is usable, and ends with the Models table. `--connect-found` answers yes for what was found. `--yes` asks nothing and installs nothing |
| `boss connect --list` / `--summary` | Prints what this machine has, or the Models table, and changes nothing |
| `boss connect --install-ollama [--local-model NAME]` | Sets up [the local model](#the-local-model) end to end without asking |
| `boss connect keys` | Saves `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY`, `XAI_API_KEY`, or `OPENROUTER_API_KEY` exported in this shell into the private `.env` (mode `0600`), then recreates the api and worker containers so they read it. Names only are printed; a key is never accepted as an argument. No `boss update` needed |
| `boss connect codex\|claude\|grok\|ollama` | Connects one. `claude` and `grok` sign in inside the BOSS container and need a terminal. `ollama` connects a running Ollama |
| `boss upgrade [--to REF]` | Backs up, then moves to the newest release, or to `REF`. `--dry-run` shows the move first |
| `boss update` | Re-applies configuration on the installed release: reruns the installer so newly exported keys and flags such as `--web` take effect. It never changes the release |
| `boss uninstall [--purge]` | Stops BOSS and keeps data and the `boss` command. `--purge` deletes data after a backup and a typed confirmation, then removes the `boss` command the installer wrote for this install (one that belongs to another program or another install is left alone) |
| `boss version` | Installed release and commit |

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

The same install without the website. The download and the Git checkout use the same commit, the installer verifies it, and a failed download stops before anything runs:

```bash
BOSS_RELEASE_COMMIT=a2a2dea7f4936c7dba83ffdca84934eefa4efc3c bash -c 'set -u; [[ $BOSS_RELEASE_COMMIT =~ ^[0-9a-f]{40}$ ]] || { echo "Set the reviewed 40-character release SHA" >&2; exit 2; }; tmp=$(mktemp) || exit; curl -fsSL "https://raw.githubusercontent.com/seanventures/boss/$BOSS_RELEASE_COMMIT/install.sh" -o "$tmp" && BOSS_REPO_REF="$BOSS_RELEASE_COMMIT" bash "$tmp" "$@"; status=$?; rm -f "$tmp"; exit "$status"' _
```

> [!WARNING]
> Do not point the download URL at another host.

The installer uses `~/boss` unless you pass `--dir`. Add installer flags after the final `_`, for example `_ --console-origin https://boss.example.com`.

### From a local checkout

Clone the release and install it in place:

```bash
git clone --branch v1.0-rc2 https://github.com/seanventures/boss.git
cd boss
./install.sh --dir "$PWD" --ref v1.0-rc2
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

### Reruns

A rerun retains both API credentials and any operator-added API keys, the database password, model keys, the secret-store encryption key, the console login, public URL, console origin, API bind, port settings, the Mission Control choice, and the secret-store choice, unless this run sets the console origin or API bind again.

- Use `boss upgrade` (the lifecycle [upgrade command](#upgrade--recover)) to back up state before moving a long-lived install to a newer release.
- A rerun remembers which release you installed. Without `--ref`, it stays on the ref recorded in `.boss-release`, so changing a setting never moves a release install to `main`: `boss update --install-coding-clis` or `./install.sh --dir "$PWD" --install-coding-clis`.
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
| `docs/landing/install.sh` | Website bootstrap: finds the newest release and runs its `install.sh` |
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
boss upgrade --dry-run                                      # show the release it would move to
boss upgrade                                                # back up, then move to the newest release
boss upgrade --to v1.1                                      # back up, then move to one ref
```

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
| `--purge-data` | This stack's named volumes (including Postgres, artifacts, and CLI sessions), then `.env` and the generated shell shim | Backups and the checkout |

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
