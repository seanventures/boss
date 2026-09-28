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
| OpenAI-compatible API | Choose **Anything OpenAI-compatible** and provide a reachable base URL, model, and credential | Local Ollama needs a container-reachable address; the setup form currently requires a nonempty key, even for a local server that does not authenticate. |
| Codex ChatGPT subscription | Optionally install the official CLI, sign in on this VM, then rerun with `--connect-codex` | Explicit import stores the CLI auth file in BOSS's selected secret backend and records the connection without a live proof turn. It remains unverified. |
| Claude Code subscription | Optionally install the official CLI, obtain its setup token on this VM, then rerun with `--connect-claude` | Explicit import stores the token and records the connection without a live proof turn. It remains unverified. |
| Grok Build subscription | Sign in as `boss` inside the API container; see [Grok CLI](GROK_CLI.md) | A host login does not reach BOSS. Community currently pauses the live proof required to record this CLI through setup. |
| Gemini or Grok API key | No guided Community installer or setup-screen path yet | Their configuration fields are not forwarded by the Community Compose file. Do not assume adding a key to `.env` connects them. |

The official CLI install option installs binaries, not subscriptions:

```bash
cd ~/boss
./install.sh --dir "$PWD" --yes --skip-model-key --install-coding-clis
```

Sign in through each provider's own CLI on the VM before requesting an import.
For the default database secret backend, explicit
`--accept-database-fallback` is required to store a subscription credential in
Postgres. Choose `--secret-store infisical` during installation if you want a
dedicated local secret store. The installer does not print imported credentials.

Use the setup screen or the read-only status endpoint to confirm what BOSS
recorded. This is a configuration check, not a model call. Community model
probe and subscription proof turns are paused until their budget reservation,
worker lease, and cost receipt are in place.
