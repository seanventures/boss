# Connect a model you already have

Attach an API key, a coding subscription, or a local model to your BOSS install, so governed jobs can use it. The control plane runs without a model, so you can connect one whenever you are ready. BOSS does not create provider accounts. A subscription (Codex, Claude Code, Grok Build) is never copied, pasted or imported: it signs in inside BOSS through the provider's own login, and BOSS then proves it.

## The Models step

The installer sets up models as part of the install, and `boss connect` repeats the same step whenever you run it:

1. **Detect.** BOSS reports what this machine has: exported provider keys; Codex, Claude Code, and Grok Build logins; the coding CLIs on `PATH`; and Ollama, whether it is running or installed but stopped, with the models it has pulled. No value is printed. `boss connect --list` shows only this report.
2. **Connect what was found.** BOSS asks once for each source at a terminal. No login is ever copied. For a Codex, Claude Code or Grok Build login found here, BOSS offers to [sign that subscription in inside BOSS](#sign-a-subscription-in-inside-boss). A running Ollama is connected as described in [Ollama](#ollama). `--yes` connects nothing found unless you add `--connect-found`.
3. **When no model is usable, offer one.** At a terminal, BOSS offers to sign in to Codex, Claude Code or Grok Build inside BOSS, and then to set up a free [local model](#a-local-model-with-ollama). The local model question defaults to yes, because without it BOSS has no model. `--yes` installs nothing; `--install-ollama` sets up the local model unattended, and `--no-local-model` turns the offer off.

The step ends with one row per source (API keys, Codex, Claude Code, Grok Build, Ollama, and the default model). Each row says connected, found but not connected, or not found, with the command that fixes it. The installer's summary card shows the same rows, and `boss connect --summary` prints them.

## Connect from the console

Open **Mission Control → Settings → Repo and models**. **+ Add model** offers three kinds: **Subscription** (Claude Code, Codex, Grok Build), **API key** (OpenRouter, OpenAI, Anthropic, Gemini, xAI, or any OpenAI-compatible endpoint as **Other**), and **Local** (Ollama, or any local OpenAI-compatible URL). Cursor is not offered: its CLI is not in the BOSS image. **Local → Ollama** with the URL left empty uses the host's Ollama as the containers see it, `http://host.docker.internal:11434/v1` (`OLLAMA_BASE_URL` in `.env` changes it), and **Test all** looks there too. A local model that does not answer shows **Can't reach**, and **Auto** does not pick it. **Connect** asks an API-key, local model, or already authenticated coding CLI one tiny governed question. An API key is saved only after the outcome is known; a row says **Working** after an answer, not merely after a login. A subscription has nothing to paste: **Sign in** starts the provider's own login inside BOSS and shows its link and code, and **Connect** then proves it.

**Models** also shows source status and existing sign-in sessions. Open **Add source**, choose a source, then use its **Add key** or **Sign in** action. Choosing a source does not connect it. Sources with unknown credentials or unreadable session history remain visible.

**Who does what** on the same page assigns models to jobs: **Build**, **Review**, **Chat**, **Decisions**, and under **More jobs**, **Hard problems** and **Routine work**. Each job defaults to **Auto**, which picks your choice, else a working subscription, else a local model, else a metered key, else a key in the install's settings file.

The same setup is available through the API:

| Endpoint | Purpose |
| --- | --- |
| `GET /v1/setup?project_id=boss-dogfood` | The whole Repo and models view: repository checks, models, who does what, budget. Never calls a provider. `boss-dogfood` is the local project seeded on your machine. |
| `POST /v1/setup/models` | Connect a model (`kind`: `subscription`, `api_key` or `local`) and prove it with one governed question. |
| `POST /v1/setup/sign-in` | Start a subscription's own login inside BOSS (`provider`: `codex`, `claude_code` or `grok`). Answers the link and code it printed; `GET /v1/setup/sign-in/{sign_in_id}` polls it, `POST …/code` hands Claude Code the code its page shows, `DELETE` cancels. |
| `POST /v1/setup/checks` | Test the repository and models now (`target`: `all`, `repository`, `model:<source_id>`). |
| `PUT /v1/setup/jobs/{job}` | Choose the engine and model for one job, or `null` for Auto. |

The older `/v1/setup/status`, `/v1/setup/model`, `/v1/setup/model-providers`, `/v1/setup/coding-agents` and `/v1/setup/coding-agents/connect` routes retain compatibility responses with a `Deprecation` header. The old subscription credential-import route is gone: sign the subscription in inside BOSS instead.

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
| Codex ChatGPT subscription | `boss connect codex`, or **Subscription → Codex → Sign in** in the console | Codex's own device login runs inside BOSS: open the link it prints and enter the code. BOSS then proves it with one governed turn. |
| Claude Code subscription | `boss connect claude`, or **Subscription → Claude Code → Sign in** | Claude Code's own login runs inside BOSS: open the link, sign in, and paste back the code the page shows (Claude Code reads it; BOSS never keeps it). BOSS then proves it with one governed turn. |
| Grok Build subscription | `boss connect grok`, or **Subscription → Grok Build → Sign in**; see [Grok CLI](GROK_CLI.md) | Grok's own device login runs inside BOSS. BOSS does not copy `auth.json`. Connecting runs one governed test turn. |

**What research needs.** Discovery web research, including the research step that opens Build's default workflow, runs on the Claude Code subscription: the Claude CLI does the web search, and each research stage is a governed call with its own budget reservation, model lease, and receipt. A local Ollama model or an API key cannot stand in. Without a usable Claude Code subscription, Build's research step is recorded as skipped with the reason, and the run goes on to the panel. See [Getting started](GETTING_STARTED.md#6-follow-the-governed-path).

The Gemini and xAI entries use each provider's documented OpenAI-compatible chat endpoint, so provider-specific features outside that compatibility layer may not be available. See the [Gemini compatibility guide](https://ai.google.dev/gemini-api/docs/openai) and the [xAI API reference](https://docs.x.ai/developers/rest-api-reference/inference).

To see what BOSS has recorded, use the setup screen or `GET /v1/setup/coding-agents?project_id=boss-dogfood`. That endpoint returns the installed CLI's sanitized status and the recorded connection. It is a local status check and does not call a model.

## How BOSS handles your keys

!!! warning "Never pass keys as arguments"
    BOSS refuses API keys and tokens as command-line arguments, because the shell would keep them in history and process listings. Paste an OpenAI key at the installer's hidden prompt, or export `OPENAI_API_KEY` or `ANTHROPIC_API_KEY` for the installer process.

    To add a key after installing, export it and run `boss connect keys` (plain `boss connect` does the same as part of its search). It saves `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY`, `XAI_API_KEY`, or `OPENROUTER_API_KEY` into the install's private `.env`, prints only the names, and recreates the api and worker containers so they read it. The database and console keep running, and no `boss update` is needed.

Credentials are stored on the install machine. A hosted provider receives authentication and the prompt or repository context needed for the call; connect only accounts and repositories you intend BOSS to use. Local persistence means:

- The generated `.env` has mode `0600`. It is never uploaded; do not commit it.
- Keys you save in the setup screen go to the secret backend you selected for this install. They are not written to git or printed by the installer.
- A subscription's session (Codex, Claude Code, Grok Build) lives in the CLI's own home inside BOSS, a volume shared by the api and worker. BOSS never reads it out, and BOSS backups do not include it.
- The Grok CLI session stays in the service user's Grok home. BOSS does not copy `auth.json`.

For a dedicated local secret store, choose `--secret-store infisical` during installation.

### Agents never hold your API key

A coding agent runs commands, and those commands can read the agent's environment. So when an agent session runs on an API key, BOSS keeps the key and gives the session something else:

- **A run token and a local address.** The process that starts the session runs a small model proxy on `127.0.0.1`. The session gets `ANTHROPIC_BASE_URL` pointing at it and `ANTHROPIC_API_KEY` set to a `boss_mdl_…` run token. The proxy adds your real key to each request on its way to the provider and streams the answer back.
- **Model calls only.** The proxy forwards only the provider's model API (for Anthropic: `/v1/messages`, `/v1/messages/count_tokens` and `/v1/models`) to a fixed upstream. It refuses any other path, a token issued for another provider, and an unknown, expired or revoked token, before anything leaves BOSS.
- **Short-lived.** A token belongs to one session and its run. It is revoked when that session ends and expires on its own after the longest session BOSS can start. Run tokens are removed from logs like every other BOSS credential.
- **Metered once.** Spend is still recorded from the session's own result in the usual per-run records. The proxy only counts what crossed it, so the two can be compared; it never adds a second charge.

| Engine and credential | Covered |
| --- | --- |
| `claude_code` on an Anthropic API key (stored in the console or `ANTHROPIC_API_KEY`) | Yes |
| `claude_agent_sdk` (always an Anthropic API key) | Yes. It no longer needs the Claude CLI's own Linux sandbox (bubblewrap), which cannot start inside BOSS's containers |
| Aider, with `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` or `OPENROUTER_API_KEY` | Yes: each key becomes a token for its own provider, with that provider's base-URL variable set |
| Native, OpenAI and provider-lane engines | Not needed: these call the model from BOSS's own process, and the commands they run never receive a provider key |
| A subscription login: Claude Code, Codex ChatGPT, Grok Build | No. These are the CLIs' own sign-ins, not API keys. They stay in the session as before |
| A micro-VM build | No. The guest gets its credential through its own launch spec and shares no loopback with BOSS |

No setting turns the proxy off. If it cannot start, the session does not start, and the engine pre-check reports the proxy's own error. It never falls back to handing over the key.

## Verification and live checks

The connection method determines when BOSS calls the model:

- **Console Connect** makes one small governed call for API keys, existing authenticated CLI sessions and local models in every edition. It reserves budget and records a receipt before the call. A refused budget makes no call. **Working** means that check received an answer; it does not prove a larger build can finish.
- **Environment key import**, including `boss connect keys`, stores a key without a completion. A later governed check or job tests it. A saved or detected source is not the same as a working model.
- **Subscription sign-in** through the console, the installer, `boss connect`, or `remote.sh subscription` ends with one trivial governed turn (`BOSS_OK`). A failed proof reports why rather than declaring it working.
- **CLI Ollama setup** checks reachability and installed models without asking for a completion. Use the console check or a small governed job to verify an answer.
- A rejected credential is not made available to jobs by **Connect**. An explicit rate-limit rejection can leave a saved source with that status. An unknown provider outcome or unknown paid cost requires reconciliation rather than declaring a new connection working. A failed or uncertain replacement does not overwrite a previously working credential.

A proof's receipt records what is known. A successful paid proof settles from typed cost or priced usage evidence. A known rejection or cancellation before contact releases its unused reservation; an uncertain submission or cost retains its financial hold for reconciliation. A quote controls BOSS admission, not the provider's eventual invoice. API keys and coding subscriptions remain distinct sources. The console's **Test** and **Test all** also call models: use it when you are ready for a small test, and inspect the resulting status and receipt.

An earlier uncertain attempt can also hold new paid work after an older release discarded its reservation. The [historical financial hold guide](LEGACY_FINANCIAL_HOLD.md) explains the separate budget warning, its labelled estimate and why raising a cap or repeating a connection test does not reconcile that attempt.

### RC9 source: durable proof authority

The RC9 source commits a worker job lease, model lease, reservation and receipt before a connection proof reaches the provider. It heartbeats both leases during the proof, pins the actual source, and pins the candidate API credential or allowlisted environment reference. An official CLI continues to own its existing authenticated session; this does not freeze its OAuth material or prove internal CLI retry behavior. A refused budget or capacity makes no model call. The proof shares capacity with ordinary work using that connection; it does not create an extra concurrent slot.

An encrypted candidate credential is restricted to its proof. It cannot serve ordinary jobs or count as a ready model. Environment-key checks use an allowlisted environment reference without copying the value into the database. After the proof, its temporary credential binding is revoked; a successful connection is recorded through the chosen store. Receipt and cost history remain. The proof's temporary source is retired, not deleted: Setup → Repo and models no longer lists it, and a `connection.proof_retired` event names it by what it proved: the provider, the model and the outcome. Unknown outcomes keep their hold and a visible reconciliation decision, while the previous working connection stays available.

The Connect request's `idempotency_key` binds the same actor, candidate and receipt across API processes and restarts. Repeating the same intent replays its saved result and original proof timestamp without another model call. It cannot restore a credential after a later replacement. Changing its credential or scope requires a new explicit intent. An unresolved intent is held for receipt review. A deliberate Test is a new small proof, not a free retry of an unknown outcome.

Connect refuses supplied subscription material without changing the working store, and nothing in BOSS imports a subscription credential any more: a subscription signs in inside BOSS through its own login, then Connect proves that session.

This describes RC9 source behavior, not a published RC9 qualification or a successful useful build. Provider responses, supported-host lifecycle and real useful-task acceptance require their own evidence.

## Coding CLIs

The API and worker images already include Claude Code, Codex CLI, and Grok Build. To also install host copies for your user, run:

```bash
./install.sh --dir "$PWD" --yes --skip-model-key --install-coding-clis
```

This installs the tools only. It does not sign you in or purchase a subscription.

### Sign a subscription in inside BOSS

Codex, Claude Code and Grok Build each run their own login inside the BOSS API container, as the `boss` user that runs the API and worker. Nothing from your machine is copied, and you never paste a token or drop a file:

```bash
boss connect codex     # or: boss connect claude, boss connect grok
```

1. BOSS starts the provider's own login inside the container and prints the link and one-time code it shows.
2. Open the link in any browser, sign in, and enter the code. Claude Code works the other way round: its page shows a code, and you paste it at the hidden prompt. Claude Code reads it inside BOSS; BOSS never keeps it.
3. BOSS connects the subscription and proves it with one governed test turn. The command ends with `✓ Codex is signed in inside BOSS and connected: it answered one test turn`, or with BOSS's own reason and fix.

An unused link expires after 15 minutes, and Ctrl-C cancels the sign-in. If the subscription is already signed in inside BOSS, step 1 is skipped. A login whose last test was refused (for example, an expired Codex login) signs in again. Without a terminal, nothing is started and BOSS says to run `boss connect <provider>`. The console's **+ Add model › Subscription › Sign in** runs the same sign-in, and `remote.sh subscription --provider codex` runs it on a server over SSH. Claude Code is refused while `.env` holds `ANTHROPIC_API_KEY` or a legacy `CLAUDE_CODE_OAUTH_TOKEN`.

The CLI homes are named volumes shared by the API and worker. Safe uninstalls and upgrades keep them, while an explicit data purge removes them. Moving to another machine requires signing in with the provider again.

See [Grok CLI](GROK_CLI.md) for Grok Build's limits, and the [installer guide](https://github.com/seanventures/boss/blob/main/scripts/install/README.md) for recovery.

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
