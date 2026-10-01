# Connect a model you already have

Attach an API key or a coding subscription you already have to your BOSS install, so governed jobs can use it. The control plane runs without a model, so you can connect one whenever you are ready. BOSS does not create provider accounts for you or copy logins from your laptop.

## Connect from the console

Open **Mission Control → Settings → Repo and model** to attach a model to the seeded project. From there you can assign connected models to implementation, review, and escalation roles.

The same setup is available through the API:

| Endpoint | Purpose |
| --- | --- |
| `GET /v1/setup/model-providers` | List the providers you can connect. |
| `POST /v1/setup/model` | Save a model connection. |
| `GET /v1/setup/status?project_id=boss-dogfood` | Read setup status for a project. `boss-dogfood` is the local project seeded on your machine. |

## Supported providers

The table lists every model path BOSS currently supports, with its present limits.

| Model path | How to connect | Current limit |
| --- | --- | --- |
| OpenAI API | Hidden installer prompt, `OPENAI_API_KEY`, or the setup screen | The key is saved without a live test. |
| Anthropic API | `ANTHROPIC_API_KEY` through the installer environment, or the setup screen | Environment-only Anthropic setup is not counted by the setup checklist; use the setup screen to see a project connection. |
| OpenRouter API | Add OpenRouter on the setup screen with its own key and model | The installer does not prompt for this key; the saved model is unverified. |
| Google Gemini API | Choose **Google Gemini API** in the setup screen and enter its key and model, or set `GOOGLE_API_KEY` on the VM | BOSS uses Google's OpenAI-compatible chat endpoint. Provider-specific tools may need a separate adapter; the connection is unverified until a governed job succeeds. |
| xAI Grok API | Choose **xAI Grok API** in the setup screen and enter its key and model, or set `XAI_API_KEY` on the VM | This is an API-key path, separate from a Grok Build subscription. The connection is unverified until a governed job succeeds. |
| OpenAI-compatible API | Choose **Anything OpenAI-compatible** and provide a reachable base URL, model, and credential | The setup form requires a nonempty key, even for a local server that does not authenticate. |
| Ollama on the install machine | `boss connect ollama`, or yes at the installer's question | Uses the **Anything OpenAI-compatible** route with the placeholder key `ollama`. The containers must be able to reach Ollama; see [Ollama](#ollama). Unverified until a governed job uses it. |
| Codex ChatGPT subscription | Optionally install the official CLI, sign in on this VM, then rerun with `--connect-codex` | Explicit import stores the CLI auth file in BOSS's selected secret backend and records the connection without a live proof turn. It remains unverified. |
| Claude Code subscription, local CLI login | `boss connect claude` (needs a terminal), or yes at the installer's question when a Claude Code login is found. Both run `claude auth login` as `boss` inside the API container, then the `--connect-claude-local` recording | Claude Code owns its private session in the shared API/worker config volume. BOSS records status without receiving OAuth bytes. The first governed job still needs to verify model use. |
| Claude Code subscription, setup-token import | Operator runs `claude setup-token` and passes the token to `--connect-claude`, `store_subscription_token.py claude_code --stdin`, or the setup screen | Stored only in the local secret store. Not written to git, logs, or Postgres. A missing token is an error. No model call during setup. |
| Grok Build subscription | `boss connect grok` (needs a terminal), which signs in as `boss` inside the API container with `grok login --device-auth`, then records it with `--connect-grok-local`; see [Grok CLI](GROK_CLI.md) | BOSS does not copy `auth.json`. Recording checks CLI status and does not start a model call. A missing login is an error. |

The Gemini and xAI entries use each provider's documented OpenAI-compatible chat endpoint, so provider-specific features outside that compatibility layer may not be available. See the [Gemini compatibility guide](https://ai.google.dev/gemini-api/docs/openai) and the [xAI API reference](https://docs.x.ai/developers/rest-api-reference/inference).

To see what BOSS has recorded, use the setup screen or `GET /v1/setup/coding-agents?project_id=boss-dogfood`. That endpoint returns the installed CLI's sanitized status and the recorded connection. It is a local status check and does not call a model.

## How BOSS handles your keys

!!! warning "Never pass keys as arguments"
    BOSS refuses API keys and tokens as command-line arguments, because the shell would keep them in history and process listings. Paste an OpenAI key at the installer's hidden prompt, or export `OPENAI_API_KEY` or `ANTHROPIC_API_KEY` for the installer process.

    To add a key after installing, export it and run `boss connect keys` (plain `boss connect` does the same as part of its search). It saves `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY`, `XAI_API_KEY`, or `OPENROUTER_API_KEY` into the install's private `.env`, prints only the names, and recreates the api and worker containers so they read it. The database and console keep running, and no `boss update` is needed.

Everything stays on the install machine:

- The generated `.env` has mode `0600`. It is never uploaded; do not commit it.
- Keys you save in the setup screen go to the secret backend you selected for this install. Imported subscription credentials are stored there too, and are never written to git or printed by the installer.
- The local Claude Code session lives in a volume owned by the CLI. BOSS backups do not include it.
- The Grok CLI session stays in the service user's Grok home. BOSS does not copy `auth.json`.

For a dedicated local secret store, choose `--secret-store infisical` during installation. With the default database backend, storing a subscription credential in Postgres requires the explicit flag `--accept-database-fallback`.

## Verification and live checks

Saving a connection and proving it works are separate steps.

- **Save key without test** stores the key and marks the model **unverified**. It does not send the key to the provider or run a test request. Keys that arrive through `.env` are also unverified.
- A green setup step is not a model response. The key may still be invalid or lack access to the model you named. A governed model job is what verifies it.
- API keys and coding subscriptions are distinct. BOSS never treats one as the other, and recording a CLI login does not start a model call.

!!! note "Paused: model probes and subscription proof turns"
    Community model probes and subscription proof turns are paused until a budget reservation, a worker lease, and a cost receipt are in place for them. Setup does not run those turns.

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

`boss connect ollama` connects an Ollama running on the install machine. The installer offers the same thing when it finds Ollama on `127.0.0.1:11434`; `--yes` connects it only with `--connect-found`.

1. The API and worker containers reach the host as `host.docker.internal`, mapped in the Compose file with `host-gateway`. No port is published.
2. BOSS checks from inside the API container that Ollama answers at `http://host.docker.internal:11434`. If it does not, nothing is changed.
3. BOSS saves an **Anything OpenAI-compatible** connection: base URL `http://host.docker.internal:11434/v1`, the first model Ollama lists (set `BOSS_OLLAMA_MODEL` to pick another), and the placeholder key `ollama`. No model is called, and the connection stays unverified until a governed job uses it.

An existing **Anything OpenAI-compatible** connection to another address is left alone. When the setup screen also holds an OpenAI key, the native lane uses whichever connection was saved last.

!!! warning "Ollama on Linux"
    Ollama listens on `127.0.0.1` by default, which a container cannot reach on Linux. BOSS does not change Ollama's settings. To connect it, have Ollama listen on an address the Docker bridge can reach, such as `OLLAMA_HOST=0.0.0.0:11434` in its service settings, and block port 11434 from other machines with your firewall, because Ollama has no authentication. The check in step 2 tells you whether the containers can reach it.

BOSS prices a model it does not know at its default token rate, which overstates a local model's cost. To record Ollama usage at zero, add `BOSS_MODEL_PRICES_JSON='{"llama3.2:latest":[0,0]}'` (with your model's name) to `.env` and run `boss update`.

## Next steps

- [Quickstart](QUICKSTART.md), if the console is not running yet.
- [Console access](CONSOLE_AUTH.md), for the owner password.
- [Guide index](README.md), for the terminal, feedback, and security.
