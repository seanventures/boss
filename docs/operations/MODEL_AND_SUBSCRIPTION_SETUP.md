# Connect a model to BOSS

Pick your row. Follow that section. Ignore the rest.

| You have | BOSS runs | Go to |
|---|---|---|
| An API key | anywhere | [A — API key](#a--api-key) |
| A Claude or Codex subscription | on the machine you signed in on | [B — subscription, same machine](#b--subscription-same-machine) |
| A Claude or Codex subscription | on a server | [C — subscription, server](#c--subscription-server) |

Set these first; every command below uses them.

```bash
export BOSS_API=https://your-boss-api
export BOSS_API_KEY=your-boss-api-key
```

---

## A — API key

| Provider | Example model | Get a key |
|---|---|---|
| `openrouter` | `anthropic/claude-sonnet-4.5` | <https://openrouter.ai/keys> |
| `openai` | `gpt-4o` | <https://platform.openai.com/api-keys> |
| `anthropic` | `claude-sonnet-4-5` | <https://console.anthropic.com/settings/keys> |
| `custom` | yours | your endpoint |

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

| Status | Fix |
|---|---|
| 401 | Key wrong or truncated. Re-copy it. |
| 403 | Your account cannot use that model. Try the example model. |
| 404 | Wrong model name or base URL. |
| 429 | Rate limited. Retry. |
| 409 | You picked a subscription. Go to B or C. |

---

## B — subscription, same machine

Install and sign in:

```bash
npm install -g @openai/codex && codex login
# Claude Code: npm install -g @anthropic-ai/claude-code && claude login
```

**Console:** Setup → Model → Codex subscription → Connect.

**CLI:**

```bash
boss-engine call connect_coding_agent --args '{"provider":"codex"}'
```

BOSS stores no credential here. Verify:

```bash
boss-engine call detect_coding_agents --args '{}'
```

You want `"ready": true`.

**If it fails** — read `next_step`; it is the command to run. `npm install …` means the CLI is
not on `PATH` for the user BOSS runs as. `sign in with: codex login` means it is not signed in,
or you signed in as a different OS user.

---

## C — subscription, server

Your login is on your laptop; BOSS is elsewhere. Send the credential.

| Subscription | What to send | Key |
|---|---|---|
| Codex | the whole `~/.codex/auth.json` | `CODEX_AUTH_JSON` |
| Claude Code | the token from `claude setup-token` | `CLAUDE_CODE_OAUTH_TOKEN` |
| Cursor | not supported on a server | — |

Do **not** set `CODEX_ACCESS_TOKEN`. Codex reads that variable itself and errors on it.

Both CLIs are already in the BOSS image. You supply only the credential.

### Step 1 — send it

Run on the machine where you are signed in. Nothing is printed but the size.

```bash
export OPS_BASE_URL=http://127.0.0.1:8099
python3 scripts/store_subscription_token.py codex --dry-run
python3 scripts/store_subscription_token.py codex
```

The `export` matters — without it the script cannot see the variable.

No secret store? Skip this step — Step 2 covers setting the value directly.

### Step 2 — make BOSS see it

Storing a secret does not put it in front of the running process. Pick the row that matches how
your deployment gets its configuration.

**Your platform writes stored secrets into the app's environment at deploy time.** This is the
common case. Render the key and recreate the containers:

```bash
python3 ops.py render CODEX_AUTH_JSON --recreate api worker
```

Nothing else to configure. Skip to Step 3.

**You set environment variables by hand.** Set `CODEX_AUTH_JSON` in the deployment's
environment — `docker run -e`, compose, a Kubernetes secret, your platform's UI. Nothing else
to configure. Skip to Step 3.

**You want BOSS to read the store itself, so the credential never enters the environment.**
Point BOSS at the store:

| Your store | Set on BOSS |
|---|---|
| HTTP secret service | `BOSS_PLATFORM_SECRET_STORE_URL` + `BOSS_PLATFORM_SECRET_STORE_TOKEN_FILE` |
| Vault / OpenBao | `BOSS_VAULT_ADDR` + `BOSS_VAULT_TOKEN` |
| Encrypted local files | `BOSS_KMS_MASTER_KEY` |

BOSS reads **one** store — the first of those that answers — so put the credential in that one.

### Step 3 — connect

If Step 2 did not already recreate the containers, restart BOSS first. Then:

```bash
curl -X POST "${BOSS_API:?}/v1/setup/coding-agents/connect" \
  -H "x-boss-api-key: ${BOSS_API_KEY:?}" -H 'Content-Type: application/json' \
  -d '{"project_id":"boss-dogfood","actor":"ceo","provider":"codex"}'
```

Success looks like this — BOSS runs a real turn before saying yes:

```
"authenticated": true, "auth_detail": "Logged in using ChatGPT",
"proof": { "ok": true, "replied": "BOSS_OK" }
```

**If it fails**

| Message | Fix |
|---|---|
| `no CODEX_AUTH_JSON available to this install` | Not in the environment or in the store BOSS selected. Check Step 2. |
| `agent identity JWT payload is not valid JSON` | `CODEX_ACCESS_TOKEN` is set. Unset it. |
| `the stored credential file did not sign codex in` | Expired. Re-run Step 1 from a machine where `codex login status` says you are signed in. |
| `not valid JSON` | Stored value is truncated. Re-run Step 1. |

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

| Lane | Key | Base URL |
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
  re-run Step 1 when it does.
- Cursor works only on the machine holding its login.
- `list_harnesses` shows `verified: false` until a full governed task has completed on a
  harness. Wired is not proven.
