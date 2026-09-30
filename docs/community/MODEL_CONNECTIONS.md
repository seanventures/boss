# Connect a model you already have

The installer starts the control plane even when no model is connected. Use an API key or a coding subscription you already have. BOSS does not open a provider account for you, and it does not copy a login from your laptop.

Open **Mission Control → Settings → Repo and model** to attach a model to the seeded project. You can assign connected models to implementation, review, and escalation roles. The same setup is available as `GET /v1/setup/model-providers`, `POST /v1/setup/model`, and `GET /v1/setup/status?project_id=boss-dogfood`. The id `boss-dogfood` is the project seeded on your machine, not a shared server.

## What stays on this machine

- The generated `.env` stays on the install machine, mode `0600`. It is not uploaded, and it should not be committed.
- A key you save in the setup screen goes to the secret backend you selected for this install.
- An imported subscription credential is stored there too. It is not written to git or printed by the installer.
- The local Claude Code session stays in a volume owned by the CLI. BOSS backups do not include that session.
- Grok's CLI session stays in the service user's Grok home. BOSS does not copy `auth.json`.

Choose `--secret-store infisical` during installation if you want a dedicated local secret store. For the default database backend, storing a subscription credential in Postgres requires the explicit flag `--accept-database-fallback`.

## What BOSS will not do with a key

- It refuses API keys and tokens as command-line arguments. The shell would keep them in history and process listings. Paste an OpenAI key at the installer's hidden prompt, or export `OPENAI_API_KEY` or `ANTHROPIC_API_KEY` for the installer process.
- **Save key without test** stores the key and marks the model **unverified**. It does not send the key to the provider and does not run a test request. A key that arrived through `.env` is unverified too.
- A green setup step is not a model response. The key can still be invalid or lack access to the model you named. A governed model job is what checks that.
- Community model probes and subscription proof turns stay paused until a budget reservation, a worker lease, and a cost receipt are in place. Setup does not run those turns.
- BOSS does not treat an API key as a coding subscription, or a subscription as an API key. Recording a CLI login does not start a model call.
- The installer does not print imported credentials.

## Supported paths

Do not expect a provider that is not in this table. The limits are current, not a promise that every provider feature works.

| Model path | First-run connection | Current limit |
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

The Gemini and xAI setup entries use those providers' documented OpenAI-compatible chat endpoints. BOSS does not infer that every provider-specific feature works through that compatibility layer. See the [Gemini compatibility guide](https://ai.google.dev/gemini-api/docs/openai) and the [xAI API reference](https://docs.x.ai/developers/rest-api-reference/inference).

`GET /v1/setup/coding-agents?project_id=boss-dogfood` reads the installed CLI's sanitized status and the connection BOSS recorded. That is a local status check, not a model call. Use the setup screen or that status endpoint to see what was recorded.

## Coding CLIs

The API and worker images already include Claude Code, Codex CLI, and Grok Build. This command installs host copies for your user. It does not sign you in and it does not buy a subscription:

```bash
./install.sh --dir "$PWD" --yes --skip-model-key --install-coding-clis
```

Sign in with each provider's own CLI on this machine before you ask BOSS to record the login. For Claude Code, sign in inside the API container as the same `boss` user that runs the API and worker:

```bash
docker compose -f scripts/install/community-compose.yml --env-file .env \
  exec -u boss api claude auth login
./install.sh --dir "$PWD" --yes --skip-model-key --connect-claude-local
```

The CLI config directory is a named volume shared by the API and worker. Safe uninstall and upgrades keep it. An explicit data purge removes it, and a move to another machine needs a new login with the provider. This local path does not ask you to submit a Claude token.

Grok device login is the same shape, as `boss` inside the API container. The steps and the limits are in [Grok CLI](GROK_CLI.md). Credential import, the database fallback, and recovery are in the [installer guide](https://github.com/seanventures/boss/blob/main/scripts/install/README.md).

## Next

- [Quickstart](QUICKSTART.md) if the console is not up yet.
- [Console access](CONSOLE_AUTH.md) for the owner password.
- [Guide index](README.md) for the terminal, feedback, and security.
