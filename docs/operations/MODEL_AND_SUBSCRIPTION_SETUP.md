# Connect a model to BOSS

Pick your row. Follow that section. Ignore the rest.

| You have | BOSS runs | Go to |
|---|---|---|
| An API key | anywhere | [A — API key](#a--api-key) |
| A Claude or Codex subscription | on the machine you signed in on | [B — subscription, same machine](#b--subscription-same-machine) |
| A Claude or Codex subscription | on a server | [C — subscription, server](#c--subscription-server) |

Most of this can be done from the console: **Setup → Model**. The commands below do the same
thing for anyone scripting it.

Where the commands run:

- `curl …` — anywhere that can reach your BOSS API.
- `boss-engine …` — where the engine is installed (`pip install boss-engine`). From a clone,
  use `PYTHONPATH=engine python3 -m boss_engine …` instead.
- `python3 scripts/…` — from the root of a clone of the BOSS repository.

If you use the commands, set these two first:

```bash
export BOSS_API=https://boss-api.example.com   # your BOSS API address, the one the console talks to
export BOSS_API_KEY=your-boss-api-key          # from your deployment's BOSS_API_KEY / BOSS_CONSOLE_API_KEY
```

---

## A — API key

| Provider | Example model | Get a key |
|---|---|---|
| `openrouter` | `anthropic/claude-sonnet-4.5` | <https://openrouter.ai/keys> |
| `openai` | `gpt-4o` | <https://platform.openai.com/api-keys> |
| `anthropic` | `claude-sonnet-4-5` | <https://console.anthropic.com/settings/keys> |
| `custom` | yours | your endpoint — also pass `"base_url"` |

**Console:** Setup → Model → pick provider → paste key → Verify and save.

**API:**

```bash
curl -X POST "${BOSS_API:?}/v1/setup/model" \
  -H "x-boss-api-key: ${BOSS_API_KEY:?}" -H 'Content-Type: application/json' -d '{
  "project_id": "boss-dogfood", "actor": "ceo",
  "provider": "openrouter", "api_key": "sk-or-your-key-here",
  "model": "anthropic/claude-sonnet-4.5"
}'
```

Done. The key is verified before it is stored.

**If it fails**

Nothing is stored when it fails, so just fix and retry.

| You get | Means |
|---|---|
| `422` · that key was rejected — check it was copied whole | Key wrong or truncated. |
| `422` · the key is valid but not allowed to use that model | Try the example model for that provider. |
| `422` · no model called … — check the model name | Wrong model name or base URL. |
| `422` · the provider is rate-limiting right now | The key works. Retry shortly. |
| `422` · could not reach … | Network or base URL problem, not the key. |
| `409` · is a subscription, not an API key | You picked `codex`/`cursor`/Claude. Go to B or C. |
| `400` · a base URL is required for a custom provider | Add `"base_url"` to the request. |

---

## B — subscription, same machine

BOSS runs where you signed in — your laptop, a dev box, a container you log in from.

### Codex

```bash
npm install -g @openai/codex && codex login
```

**Console:** Setup → Model → Codex subscription → **Connect**. Or:

```bash
boss-engine call connect_coding_agent --args '{"provider":"codex"}'
```

Then point BOSS at it — `BOSS_CODE_AGENT_ENGINE=codex`, or
[one provider everywhere](#use-one-provider-everywhere). Verify:

```bash
boss-engine call detect_coding_agents --args '{}'
```

You want codex with `"ready": true`. BOSS stores no credential; the CLI keeps its own session.

**If it fails** — read `next_step`; it is the command to run. `npm install …` means the CLI is
not on `PATH` for the user BOSS runs as. `sign in with: codex login` means it is not signed in,
or you signed in as a different OS user.

### Claude Code

```bash
npm install -g @anthropic-ai/claude-code && claude login
```

That is the whole setup. `claude_code` is BOSS's default engine and it uses the CLI's own
session, so there is no Connect step, no credential to store, and nothing to configure. If you
previously switched engines, switch back with `BOSS_CODE_AGENT_ENGINE=claude_code`.

Verify:

```bash
boss-engine call list_harnesses --args '{}'
```

You want `claude_code` listed. Claude is not part of the coding-agent **detection** list, so it
will not appear in `detect_coding_agents` — that command covers Codex, Cursor and Aider only.

**If it fails** — the CLI must be on `PATH` for the user BOSS runs as, and signed in as that
same OS user.

### Cursor

Detected and connected exactly like Codex, but unverified and usable only here — see
[Known limits](#known-limits).

---

## C — subscription, server

Your login is on your laptop; BOSS is elsewhere. Send the credential.

| Subscription | What to send | Key | Go to |
|---|---|---|---|
| Codex | the whole `~/.codex/auth.json` | `CODEX_AUTH_JSON` | [Codex](#codex-on-a-server) |
| Claude Code | the token from `claude setup-token` | `CLAUDE_CODE_OAUTH_TOKEN` | [Claude Code](#claude-code-on-a-server) |
| Cursor | not supported on a server | — | — |

Both CLIs are already in the BOSS image. You supply only the credential.

### Codex on a server

Do **not** set `CODEX_ACCESS_TOKEN`. Codex reads that variable itself and errors on it.

#### Step 1 — put the credential where BOSS runs

Run these on the machine where you are signed in. Nothing is printed but the size.
Pick **one**.

**A — write it into the deployment's env file.** Simplest, when you can reach that file.

Which file: the one BOSS already reads for its other settings.

| How BOSS runs | The file |
|---|---|
| Docker Compose | `.env` beside your compose file (the same one holding `BOSS_API_KEY`) |
| From a clone | `.env` in the repository root |
| Kubernetes | no env file — use a Secret, or route B |

Run from the root of your BOSS clone. `.env` is then the repository's own file; pass a full
path instead if the file you need is elsewhere (`--write-env /srv/boss/.env`):

```bash
python3 scripts/store_subscription_token.py codex --write-env .env
```

Restart BOSS. **You are done — skip to Step 3.**

If you cannot reach the machine's filesystem, use route B.

**B — send it to an HTTP secret store:**

```bash
export OPS_BASE_URL=http://127.0.0.1:8099   # your secret store's address
python3 scripts/store_subscription_token.py codex --dry-run
python3 scripts/store_subscription_token.py codex
```

The `export` matters — without it the script cannot see the variable.

Storing it is not enough on its own; continue to Step 2.

Add `--dry-run` to either to see what would be sent without sending it.

#### Step 2 — make BOSS read it (route B only)

A stored secret is not yet in front of the running process. Pick **one**.

**B1 — have your platform put it in the app's environment.** If your deployment already
applies stored secrets at deploy time, trigger that (a deploy, a config sync, a restart), then
recreate the containers. Nothing to configure on BOSS.

**B2 — have BOSS read the store directly**, so the credential never enters the environment.
Set these on BOSS, then restart:

| Your store | Set on BOSS |
|---|---|
| HTTP secret service | `BOSS_PLATFORM_SECRET_STORE_URL` + `BOSS_PLATFORM_SECRET_STORE_TOKEN_FILE` |
| Vault / OpenBao | `BOSS_VAULT_ADDR` + `BOSS_VAULT_TOKEN` |
| Encrypted local files | `BOSS_KMS_MASTER_KEY` |

BOSS reads **one** store — the first of those that answers — so put the credential in that one.

#### Step 3 — connect

If Step 2 did not already recreate the containers, restart BOSS first.

**Console:** Setup → Model → Codex subscription → **Connect**.

**Or with the API:**

```bash
curl -X POST "${BOSS_API:?}/v1/setup/coding-agents/connect" \
  -H "x-boss-api-key: ${BOSS_API_KEY:?}" -H 'Content-Type: application/json' \
  -d '{"project_id":"boss-dogfood","actor":"ceo","provider":"codex"}'
```

Success looks like this — BOSS runs a real Codex turn before saying yes:

```json
{
  "ok": true,
  "provider": "codex",
  "detected": { "authenticated": true, "auth_detail": "Logged in using ChatGPT",
                "ready": true },
  "proof": { "ok": true, "exit_code": 0, "replied": "BOSS_OK" }
}
```

**If it fails**

| Message | Fix |
|---|---|
| `no CODEX_AUTH_JSON available to this install` | Not in the environment or in the store BOSS selected. Check Step 2. |
| `agent identity JWT payload is not valid JSON` | `CODEX_ACCESS_TOKEN` is set. Unset it. |
| `the stored credential file did not sign codex in` | Expired. Re-run Step 1 from a machine where `codex login status` says you are signed in. |
| `not valid JSON` | Stored value is truncated. Re-run Step 1. |

### Claude Code on a server

Claude mints a token for exactly this, so there is no file to move.

**Step 1 — mint it**, on a machine where you are signed in:

```bash
claude setup-token
```

**Step 2 — set it on the deployment** as `CLAUDE_CODE_OAUTH_TOKEN`, by whichever route suits
you. Both are the same routes Codex uses above:

- the env file BOSS reads (`.env` beside your compose file, or in the repository root), or
- your secret store, with `BOSS_PLATFORM_SECRET_STORE_URL` / `BOSS_VAULT_ADDR` /
  `BOSS_KMS_MASTER_KEY` set so BOSS can read it back.

**Step 3 — restart BOSS.** There is no Connect step: `claude_code` is the default engine and
picks the token up on its next run.

Verify:

```bash
boss-engine call list_harnesses --args '{}'
```

**If it fails**

| You see | Fix |
|---|---|
| the task runs but authentication is refused | The token expired. Mint a new one and set it again. |
| `claude: not found` in a run's output | The image or host is missing the CLI. |

### Cursor on a server

Not supported. Cursor has no headless token, so it only works where its login lives.

---

## Use one provider everywhere

```bash
BOSS_PROVIDER_LOCK=codex        # or openrouter, groq, deepseek, ollama, vllm, claude_code, cursor
```

Moves the coding pipeline, reviewers, agent chat, meetings and discovery. Unset changes nothing.

`cursor` moves the pipeline only — it has no chat adapter.

Check what actually ran:

```bash
boss-engine call provider_audit --args '{"expect":"openai","dimension":"vendor"}'
```

`vendor` = whose model. `transport` = whose endpoint. Claude via OpenRouter is vendor
`anthropic`, transport `openrouter`.

---

## Reference

### Other lanes

| Lane | Key setting | Base URL setting |
|---|---|---|
| `openrouter` | `OPENROUTER_API_KEY` | `OPENROUTER_BASE_URL` |
| `groq` | `GROQ_API_KEY` | `GROQ_BASE_URL` |
| `deepseek` | `DEEPSEEK_API_KEY` | `DEEPSEEK_BASE_URL` |
| `ollama` | none | `OLLAMA_BASE_URL` (default `http://localhost:11434/v1`) |
| `vllm` | none | `VLLM_BASE_URL` (required) |
| `azure` | `AZURE_OPENAI_KEY` | `AZURE_OPENAI_ENDPOINT` + `AZURE_OPENAI_DEPLOYMENT` |

Local models are free — price them at zero or the ledger will invent a bill:

```bash
BOSS_MODEL_PRICES_JSON='{"qwen2.5-coder:14b":[0,0]}'
```

### Run the secret store yourself

Implement four endpoints and BOSS uses it unchanged, authenticated with an `X-Ops-Token` header:

`GET /secrets` · `GET /secret?key=` · `POST /secret` · `DELETE /secret?key=`

Values must be single-line.

### Known limits

- A copied Codex credential does not get the refreshes your laptop's CLI performs. It expires;
  re-send it when it does. A Claude token expires too — mint a new one.
- Claude Code is not covered by `detect_coding_agents`; use `list_harnesses` to confirm it.
- Cursor works only on the machine holding its login, and no governed task has been completed
  on it yet.
- `list_harnesses` shows `verified: false` until a full governed task has completed on a
  harness. Wired is not proven.
