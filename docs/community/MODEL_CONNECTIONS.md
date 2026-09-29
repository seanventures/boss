# Connect models after a Community install

The one-line installer starts the control plane even when no model is connected.
It accepts an OpenAI key at a hidden prompt. `OPENAI_API_KEY` and the optional
`ANTHROPIC_API_KEY` can also be supplied to the installer through its environment.
The installer does not silently sign in to other providers or copy credentials
from a laptop. Keep keys out of shell arguments and history.

Open **Mission Control → Settings → Repo and model** to add more than one model
to the seeded project. The setup screen lets you assign connected models to
implementation, review, and escalation roles. Its API equivalents are
`GET /v1/setup/model-providers`, `POST /v1/setup/model`, and
`GET /v1/setup/status?project_id=boss-dogfood`. Community saves API keys without
a provider request. A saved key is **unverified** until a governed model job
uses it; a green setup step is not a successful model response.

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
| Claude Code subscription, legacy token import | Disabled. `--connect-claude`, `store_subscription_token.py claude_code`, and the console paste box refuse to run | No provider or counsel clearance for copying a Claude session token is recorded in this repo. Counsel reference `01049940` covers first-party Apache licensing only. Use the CLI-owned login above. |
| Grok Build subscription | Sign in as `boss` inside the API container; see [Grok CLI](GROK_CLI.md) | A host login does not reach BOSS. Community currently pauses the live proof required to record this CLI through setup. |

The Gemini and xAI setup entries use their providers' documented OpenAI-compatible
chat endpoints. BOSS does not infer that every provider-specific feature works
through that compatibility layer. The setup screen stores each key in the
selected secret backend; the corresponding environment variables are optional
for operators who manage `.env` directly. See the providers'
[Gemini compatibility guide](https://ai.google.dev/gemini-api/docs/openai)
and [xAI API reference](https://docs.x.ai/developers/rest-api-reference/inference).

The official CLI install option installs binaries, not subscriptions:

```bash
cd ~/boss
./install.sh --dir "$PWD" --yes --skip-model-key --install-coding-clis
```

Sign in through each provider's own CLI on the VM before requesting an import.
For the local Claude path, use the CLI **inside** the API container, under the
same `boss` account that runs the API and worker:

```bash
docker compose -f scripts/install/community-compose.yml --env-file .env \
  exec -u boss api claude auth login
./install.sh --dir "$PWD" --yes --skip-model-key --connect-claude-local
```

`GET /v1/setup/coding-agents?project_id=boss-dogfood` reads the installed
CLI's sanitized status and the connection BOSS recorded. This is a local status
check, not a model call. The CLI config directory is a named volume shared by
the API and worker; safe uninstall and upgrades retain it. BOSS backups exclude
the Claude CLI session. An explicit data purge removes the volume, and a move to
another machine requires another provider-owned login. This local path does not
ask for or submit a Claude token.

For the default database secret backend, explicit
`--accept-database-fallback` is required to store a subscription credential in
Postgres. Choose `--secret-store infisical` during installation if you want a
dedicated local secret store. The installer does not print imported credentials.

Use the setup screen or the read-only status endpoint to confirm what BOSS
recorded. This is a configuration check, not a model call. Community model
probe and subscription proof turns are paused until their budget reservation,
worker lease, and cost receipt are in place.
