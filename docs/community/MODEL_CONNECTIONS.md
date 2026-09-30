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
| OpenAI-compatible API | Choose **Anything OpenAI-compatible** and provide a reachable base URL, model, and credential | Local Ollama needs a container-reachable address; the setup form currently requires a nonempty key, even for a local server that does not authenticate. |
| Codex ChatGPT subscription | Optionally install the official CLI, sign in on this VM, then rerun with `--connect-codex` | Explicit import stores the CLI auth file in BOSS's selected secret backend and records the connection without a live proof turn. It remains unverified. |
| Claude Code subscription, local CLI login | Run `claude auth login` as `boss` inside the API container, then rerun the installer with `--connect-claude-local` | Claude Code owns its private session in the shared API/worker config volume. BOSS records status without receiving OAuth bytes. The first governed job still needs to verify model use. |
| Claude Code subscription, setup-token import | Operator runs `claude setup-token` and passes the token to `--connect-claude`, `store_subscription_token.py claude_code --stdin`, or the setup screen | Stored only in the local secret store. Not written to git, logs, or Postgres. A missing token is an error. No model call during setup. |
| Grok Build subscription | Sign in as `boss` inside the API container with `grok login --device-auth`, then `--connect-grok-local`; see [Grok CLI](GROK_CLI.md) | BOSS does not copy `auth.json`. Recording checks CLI status and does not start a model call. A missing login is an error. |

The Gemini and xAI entries use each provider's documented OpenAI-compatible chat endpoint, so provider-specific features outside that compatibility layer may not be available. See the [Gemini compatibility guide](https://ai.google.dev/gemini-api/docs/openai) and the [xAI API reference](https://docs.x.ai/developers/rest-api-reference/inference).

To see what BOSS has recorded, use the setup screen or `GET /v1/setup/coding-agents?project_id=boss-dogfood`. That endpoint returns the installed CLI's sanitized status and the recorded connection. It is a local status check and does not call a model.

## How BOSS handles your keys

!!! warning "Never pass keys as arguments"
    BOSS refuses API keys and tokens as command-line arguments, because the shell would keep them in history and process listings. Paste an OpenAI key at the installer's hidden prompt, or export `OPENAI_API_KEY` or `ANTHROPIC_API_KEY` for the installer process.

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

Sign in with each provider's own CLI on this machine before asking BOSS to record the login. For Claude Code, sign in inside the API container as the `boss` user that runs the API and worker, then record the login:

```bash
docker compose -f scripts/install/community-compose.yml --env-file .env \
  exec -u boss api claude auth login
./install.sh --dir "$PWD" --yes --skip-model-key --connect-claude-local
```

The CLI config directory is a named volume shared by the API and worker. Safe uninstalls and upgrades keep it, while an explicit data purge removes it. Moving to another machine requires signing in with the provider again. This local path never asks you to submit a Claude token.

### Record a Grok Build login

Grok device login follows the same pattern, as `boss` inside the API container. See [Grok CLI](GROK_CLI.md) for the steps and limits. Credential import, the database fallback, and recovery are covered in the [installer guide](https://github.com/seanventures/boss/blob/main/scripts/install/README.md).

## Next steps

- [Quickstart](QUICKSTART.md), if the console is not running yet.
- [Console access](CONSOLE_AUTH.md), for the owner password.
- [Guide index](README.md), for the terminal, feedback, and security.
