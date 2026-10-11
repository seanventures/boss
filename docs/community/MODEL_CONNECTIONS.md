# Connect a model you already have

Attach an API key, a coding subscription, or a local model to your BOSS install, so governed jobs can use it. The control plane runs without a model, so you can connect one whenever you are ready. BOSS does not create provider accounts. It asks before importing a Codex login; Claude Code and Grok Build sign in separately inside BOSS.

## The Models step

The installer sets up models as part of the install, and `boss connect` repeats the same step whenever you run it:

1. **Detect.** BOSS reports what this machine has: exported provider keys; Codex, Claude Code, and Grok Build logins; the coding CLIs on `PATH`; and Ollama, whether it is running or installed but stopped, with the models it has pulled. No value is printed. `boss connect --list` shows only this report.
2. **Connect what was found.** BOSS asks once for each source at a terminal. A Codex login is copied into BOSS; a Claude Code or Grok Build login is never copied, and the CLI signs in again inside the BOSS container instead. A running Ollama is connected as described in [Ollama](#ollama). `--yes` connects nothing found unless you add `--connect-found`.
3. **When no model is usable, offer one.** At a terminal, BOSS offers to sign in to Claude Code or Grok Build inside BOSS, and then to set up a free [local model](#a-local-model-with-ollama). The local model question defaults to yes, because without it BOSS has no model. `--yes` installs nothing; `--install-ollama` sets up the local model unattended, and `--no-local-model` turns the offer off.

The step ends with one row per source (API keys, Codex, Claude Code, Grok Build, Ollama, and the default model). Each row says connected, found but not connected, or not found, with the command that fixes it. The installer's summary card shows the same rows, and `boss connect --summary` prints them.

## Connect from the console

Open **Mission Control → Settings → Repo and models**. **+ Add model** offers three kinds: **Subscription** (Claude Code, Codex, Grok Build, Cursor), **API key** (OpenRouter, OpenAI, Anthropic, Gemini, xAI, or any OpenAI-compatible endpoint as **Other**), and **Local** (Ollama, or any local OpenAI-compatible URL). **Connect** asks an API-key, local model, or already authenticated coding CLI one tiny governed question. An API key is saved only after the outcome is known; a row says **Working** after an answer, not merely after a login. In RC9 source, pasted subscription tokens and auth files are held before storage or a model call: sign the CLI in as the BOSS service user, then Connect without pasted credential material. Candidate-isolated subscription import is still pending.

**Who does what** on the same page assigns models to jobs: **Build**, **Review**, **Chat**, **Decisions**, and under **More jobs**, **Hard problems** and **Routine work**. Each job defaults to **Auto**, which picks your choice, else a working subscription, else a local model, else a metered key, else a key in the install's settings file.

The same setup is available through the API:

| Endpoint | Purpose |
| --- | --- |
| `GET /v1/setup?project_id=boss-dogfood` | The whole Repo and models view: repository checks, models, who does what, budget. Never calls a provider. `boss-dogfood` is the local project seeded on your machine. |
| `POST /v1/setup/models` | Connect a model (`kind`: `subscription`, `api_key` or `local`) and prove it with one governed question. |
| `POST /v1/setup/checks` | Re-check the repository and models now (`target`: `all`, `repository`, `model:<source_id>`). |
| `PUT /v1/setup/jobs/{job}` | Choose the engine and model for one job, or `null` for Auto. |

The older `/v1/setup/status`, `/v1/setup/model`, `/v1/setup/model-providers` and `/v1/setup/coding-agents*` routes retain compatibility responses with a `Deprecation` header. In RC9 source, the deprecated HTTP credential-import route refuses supplied material; use an existing authenticated CLI session instead.

## Supported providers

The table lists every model path BOSS currently supports, with its present limits.

| Model path | How to connect | Current limit |
| --- | --- | --- |
| OpenAI API | **API key → OpenAI** in the console, or `OPENAI_API_KEY` through the installer or `boss connect keys` | Console **Connect** runs a governed proof. Environment imports store the key without a completion and remain unverified until checked or used. |
| Anthropic API | **API key → Anthropic** in the console, or `ANTHROPIC_API_KEY` in the install environment | Console **Connect** runs a governed proof. An environment-only key is a separate source; check the source and model selected for the job. |
| OpenRouter API | **API key → OpenRouter**, or `OPENROUTER_API_KEY` through `boss connect keys` | Console **Connect** runs a governed proof; the installer has no separate OpenRouter key prompt. |
| Google Gemini API | **API key → Gemini**, or `GOOGLE_API_KEY` in the install environment | Uses Google's OpenAI-compatible chat endpoint. Console **Connect** runs a governed proof; provider-specific features outside that interface may need a separate adapter. |
| xAI Grok API | **API key → xAI**, or `XAI_API_KEY` in the install environment | Console **Connect** runs a governed proof. This API-key connection is separate from a Grok Build subscription. |
| OpenAI-compatible API | **API key → Other** with a reachable base URL, model and key | Console **Connect** runs a governed proof. For an unauthenticated local server, use **Local** instead. |
| Ollama or a local compatible model | **Local** in the console; `boss connect ollama` for a running Ollama; `boss connect --install-ollama` to install one | Console **Connect** runs a governed proof. The CLI Ollama path checks reachability and model presence without a completion. Its local endpoint is priced at zero, and fallback role assignment is described below. |
| Codex ChatGPT subscription | **Subscription → Codex** for an existing authenticated CLI session, or `boss connect codex` / `--connect-codex` | RC9 console Connect does not import an auth file. Explicit terminal import remains an intentional storage/replacement action; the subsequent governed test turn verifies the recorded session. |
| Claude Code subscription, local CLI login | `boss connect claude` at a terminal, or `--connect-claude-local` after signing in inside the container | Claude Code owns its session in the shared API/worker config volume. BOSS proves the login with one governed turn before recording it as connected. |
| Claude Code subscription, setup-token import | `--connect-claude`, or `store_subscription_token.py claude_code --stdin` | Terminal storage is an intentional authentication replacement and alone makes no model call. Connecting runs a separate governed proof. RC9 console/HTTP import is held; console Connect uses an existing authenticated session. Database credential storage requires the explicit fallback acceptance described below. |
| Grok Build subscription | `boss connect grok` at a terminal, then `--connect-grok-local`; see [Grok CLI](GROK_CLI.md) | BOSS does not copy `auth.json`. Recording the container login runs one governed test turn; a missing login is an error. |

**What research needs.** Discovery web research, including the research step that opens Build's default workflow, runs on the Claude Code subscription: the Claude CLI does the web search, and each research stage is a governed call with its own budget reservation, model lease, and receipt. A local Ollama model or an API key cannot stand in. Without a usable Claude Code subscription, Build's research step is recorded as skipped with the reason, and the run goes on to the panel. See [Getting started](GETTING_STARTED.md#6-follow-the-governed-path).

The Gemini and xAI entries use each provider's documented OpenAI-compatible chat endpoint, so provider-specific features outside that compatibility layer may not be available. See the [Gemini compatibility guide](https://ai.google.dev/gemini-api/docs/openai) and the [xAI API reference](https://docs.x.ai/developers/rest-api-reference/inference).

To see what BOSS has recorded, use the setup screen or `GET /v1/setup/coding-agents?project_id=boss-dogfood`. That endpoint returns the installed CLI's sanitized status and the recorded connection. It is a local status check and does not call a model.

## How BOSS handles your keys

!!! warning "Never pass keys as arguments"
    BOSS refuses API keys and tokens as command-line arguments, because the shell would keep them in history and process listings. Paste an OpenAI key at the installer's hidden prompt, or export `OPENAI_API_KEY` or `ANTHROPIC_API_KEY` for the installer process.

    To add a key after installing, export it and run `boss connect keys` (plain `boss connect` does the same as part of its search). It saves `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY`, `XAI_API_KEY`, or `OPENROUTER_API_KEY` into the install's private `.env`, prints only the names, and recreates the api and worker containers so they read it. The database and console keep running, and no `boss update` is needed.

Credentials are stored on the install machine. A hosted provider receives authentication and the prompt or repository context needed for the call; connect only accounts and repositories you intend BOSS to use. Local persistence means:

- The generated `.env` has mode `0600`. It is never uploaded; do not commit it.
- Keys you save in the setup screen go to the secret backend you selected for this install. Explicit terminal subscription imports use that backend too; RC9 console/HTTP subscription import is held. Neither is written to git or printed by the installer.
- The local Claude Code session lives in a volume owned by the CLI. BOSS backups do not include it.
- The Grok CLI session stays in the service user's Grok home. BOSS does not copy `auth.json`.

For a dedicated local secret store, choose `--secret-store infisical` during installation. With the default database backend, storing a subscription credential in Postgres requires the explicit flag `--accept-database-fallback`.

## Verification and live checks

The connection method determines when BOSS calls the model:

- **Console Connect** makes one small governed call for API keys, existing authenticated CLI sessions and local models in every edition. It reserves budget and records a receipt before the call. A refused budget makes no call. **Working** means that check received an answer; it does not prove a larger build can finish.
- **Environment key import**, including `boss connect keys`, stores a key without a completion. A later governed check or job tests it. A saved or detected source is not the same as a working model.
- **Subscription recording** through the installer, `boss connect`, or `remote.sh subscription` runs one trivial governed turn (`BOSS_OK`). A failed proof reports why rather than declaring it working.
- **CLI Ollama setup** checks reachability and installed models without asking for a completion. Use the console check or a small governed job to verify an answer.
- A rejected credential is not made available to jobs by **Connect**. An explicit rate-limit rejection can leave a saved source with that status. An unknown provider outcome or unknown paid cost requires reconciliation rather than declaring a new connection working. A failed or uncertain replacement does not overwrite a previously working credential.

A proof's receipt records what is known. A successful paid proof settles from typed cost or priced usage evidence. A known rejection or cancellation before contact releases its unused reservation; an uncertain submission or cost retains its financial hold for reconciliation. A quote controls BOSS admission, not the provider's eventual invoice. API keys and coding subscriptions remain distinct sources. The console's **Re-check** also calls models: use it when you are ready for a small test, and inspect the resulting status and receipt.

An earlier uncertain attempt can also hold new paid work after an older release discarded its reservation. The [historical financial hold guide](LEGACY_FINANCIAL_HOLD.md) explains the separate budget warning, its labelled estimate and why raising a cap or repeating a connection test does not reconcile that attempt.

### RC9 source: durable proof authority

The RC9 source commits a worker job lease, model lease, reservation and receipt before a connection proof reaches the provider. It heartbeats both leases during the proof, pins the actual source, and pins the candidate API credential or allowlisted environment reference. An official CLI continues to own its existing authenticated session; this does not freeze its OAuth material or prove internal CLI retry behavior. A refused budget or capacity makes no model call. The proof shares capacity with ordinary work using that connection; it does not create an extra concurrent slot.

An encrypted candidate credential is restricted to its proof. It cannot serve ordinary jobs or count as a ready model. Environment-key checks use an allowlisted environment reference without copying the value into the database. After the proof, its temporary credential binding is revoked; a successful connection is recorded through the chosen store. Receipt and cost history remain. Unknown outcomes keep their hold and a visible reconciliation decision, while the previous working connection stays available.

The Connect request's `idempotency_key` binds the same actor, candidate and receipt across API processes and restarts. Repeating the same intent replays its saved result and original proof timestamp without another model call. It cannot restore a credential after a later replacement. Changing its credential or scope requires a new explicit intent. An unresolved intent is held for receipt review. A deliberate Re-check is a new small proof, not a free retry of an unknown outcome.

Modern Connect and the deprecated HTTP credential-import route refuse supplied subscription material without changing the working store. Existing authenticated CLI-session Connect remains available. The legacy terminal credential-import/storage helpers remain intentional configuration actions for installer compatibility: they store or replace authentication material and do not validate a connection. They are separate from this candidate-isolation boundary; candidate-only OAuth import/replacement is still unresolved.

This describes RC9 source behavior, not a published RC9 qualification or a successful useful build. Provider responses, supported-host lifecycle and real useful-task acceptance require their own evidence.

## Coding CLIs

The API and worker images already include Claude Code, Codex CLI, and Grok Build. To also install host copies for your user, run:

```bash
./install.sh --dir "$PWD" --yes --skip-model-key --install-coding-clis
```

This installs the tools only. It does not sign you in or purchase a subscription.

### Record a Claude Code login

`boss connect claude` does both steps below at a terminal: it signs Claude Code in inside the API container as the `boss` user, then records the login. When the installer finds a Claude Code login on your machine, it offers the same thing once. Your machine's login is never copied; Claude Code signs in again inside the container with its own login. Without a terminal, nothing is signed in and BOSS tells you to run `boss connect claude`. The sign-in is refused while `.env` holds `ANTHROPIC_API_KEY` or a legacy `CLAUDE_CODE_OAUTH_TOKEN`.

By hand, for Claude Code, sign in inside the API container as the `boss` user that runs the API and worker, then record the login:

```bash
docker compose -f scripts/install/community-compose.yml --env-file .env \
  exec -u boss api claude auth login
./install.sh --dir "$PWD" --yes --skip-model-key --connect-claude-local
```

The CLI config directory is a named volume shared by the API and worker. Safe uninstalls and upgrades keep it, while an explicit data purge removes it. Moving to another machine requires signing in with the provider again. This local path never asks you to submit a Claude token.

### Record a Grok Build login

Grok device login follows the same pattern, as `boss` inside the API container. `boss connect grok` runs it at a terminal and records the login. See [Grok CLI](GROK_CLI.md) for the steps and limits. Credential import, the database fallback, and recovery are covered in the [installer guide](https://github.com/seanventures/boss/blob/main/scripts/install/README.md).

## Ollama

`boss connect ollama` connects an Ollama that is already running on the install machine. The installer and `boss connect` offer it when they find Ollama on `127.0.0.1:11434`; `--yes` connects it only with `--connect-found`.

1. The API and worker containers reach the host as `host.docker.internal`, which the Compose file maps with `host-gateway`. No port is published.
2. BOSS checks from inside the API container that Ollama answers at `http://host.docker.internal:11434`. If it does not, nothing is changed and the fix is printed.
3. BOSS saves an **Anything OpenAI-compatible** connection with:
   - the base URL `http://host.docker.internal:11434/v1`
   - the first model Ollama lists, or the one named by `--local-model` or `BOSS_OLLAMA_MODEL`
   - the placeholder key `ollama`
4. When no hosted model is connected (no provider key in `.env`, no recorded subscription, and no API-key connection), Ollama becomes the model for every role: implementer, reviewer, hard problems, and routine work. A role you already chose is left alone. Chat, meetings, and the other surfaces that read `OPENAI_*` then use it too: BOSS sets `OPENAI_API_KEY=ollama`, `OPENAI_BASE_URL` to the local address, and `OPENAI_MODEL` in `.env`, but only when `.env` holds no hosted OpenAI key. With a hosted model connected, chat is untouched. When a hosted model is connected, Ollama is added but not made the default.
5. The model is priced at zero for the local endpoint only: the `BOSS_MODEL_PRICES_JSON` key is `<local base URL>#<model>`, for example `http://host.docker.internal:11434/v1#qwen2.5-coder:7b`, so the same model name on a hosted provider keeps its normal price. Other entries are kept. Only api and worker are recreated.
6. BOSS checks from the API container that the model is there. This CLI Ollama path does not run a completion. A console check or the first governed job verifies an answer; see [Verification and live checks](#verification-and-live-checks).

An existing **Anything OpenAI-compatible** connection to another address is left alone. A local Ollama never takes the native (OpenAI-compatible) lane from a hosted connection: with an OpenAI key saved in the setup screen or in `.env`, that key is used whichever was saved last; Ollama is used there only when nothing hosted is connected.

When a hosted model is connected later, `boss connect` hands back the roles the fallback set (lanes that still say native with the Ollama model return to their defaults), and `boss connect keys` with a new `OPENAI_API_KEY` moves chat from Ollama to that key. An installer rerun with an exported OpenAI key does the same for chat. Ollama stays connected.

Switching to a different Ollama model later, with `boss connect ollama --local-model NAME`, moves chat and every role BOSS gave the previous local model to the new one. A job you chose yourself in **Mission Control → Settings → Repo and models → Who does what** stays as you set it, and the output counts it: `The 3 role(s) BOSS had given the previous local model now use Ollama NAME; 1 role(s) you chose were left alone`.

BOSS knows which roles it set from `.env.boss-local-roles` in the install directory. The file holds the role names and the model, never a key. An install from before that file existed is read by comparison: a role still set to native with exactly the previously connected Ollama model counts as one BOSS set. When a hosted model is connected and BOSS hands its roles back, the file is removed.

## A local model with Ollama

`boss connect --install-ollama`, or `--install-ollama` for the installer, sets up a free local model end to end. A yes to the local model offer does the same:

1. **Install Ollama if it is missing.** On Linux, BOSS uses the official installer from `https://ollama.com/install.sh`. On macOS, it runs `brew install ollama` when Homebrew is present; otherwise it asks you to install the app from https://ollama.com/download.
2. **Start it if it is stopped.**
3. **Make it reachable from the containers.** On Linux, Ollama listens on `127.0.0.1`, which containers cannot reach. BOSS binds the Ollama service to the Docker bridge address, usually `172.17.0.1`, which this machine and its containers can reach and your network cannot. It never binds to `0.0.0.0`.
   - An Ollama that was already installed or running before this run belongs to you, and other programs may use it on `127.0.0.1`, so BOSS asks before moving it (default no). An unattended run never moves it unless you pass `--rebind-ollama`; without that, it stops with the fix. An Ollama that this run installed is moved without asking.
   - The change is a systemd drop-in, `/etc/systemd/system/ollama.service.d/boss-docker-bridge.conf`, and it needs `sudo`.
   - BOSS records the new address as `BOSS_OLLAMA_URL` in `.env`.
   - Afterwards, run the `ollama` command with `export OLLAMA_HOST=<bridge address>:11434`.
4. **Pull the model.** The default is `qwen2.5-coder:7b`; `--local-model NAME` picks another. BOSS prints the download size and the free disk space first, and stops when there is not enough room.
5. **Connect it** as described in [Ollama](#ollama).

See the [installer guide](https://github.com/seanventures/boss/blob/main/scripts/install/README.md) for each step's details and how to undo the Linux binding.

## Next steps

- [Quickstart](QUICKSTART.md), if the console is not running yet.
- [Console access](CONSOLE_AUTH.md), for claiming this install and signing in as yourself.
- [Guide index](README.md), for the terminal, feedback, and security.
