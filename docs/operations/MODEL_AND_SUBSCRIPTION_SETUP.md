# Setting up a model for BOSS

BOSS does not ship with a model. You connect one, and BOSS runs on whatever you connect.

This page is task-first: find your situation in the table below, go to that one section, and
follow it start to finish. Each path is complete on its own — you should never need to assemble
steps from two places. Reference material is at the end for when you want it.

## Which path is yours?

| What you have | Where BOSS runs | Follow |
|---|---|---|
| An **API key** (OpenRouter, OpenAI, Anthropic, or any OpenAI-compatible endpoint) | Anywhere | [Path A](#path-a--connect-an-api-key) |
| A **Claude or Codex subscription** | On the same machine where you ran `claude login` / `codex login` | [Path B](#path-b--connect-a-subscription-on-your-own-machine) |
| A **Claude or Codex subscription** | On a server, container, or anywhere that is not your laptop | [Path C](#path-c--connect-a-subscription-to-boss-on-a-server) |
| Nothing yet, and you want to try BOSS for free | Anywhere with a GPU or a local model | [Path A](#path-a--connect-an-api-key), lane `ollama` |

You can do more than one. Many installations run an API key for the cheap lanes and a
subscription for the expensive ones.

**Why the subscription paths differ.** A Claude or Codex subscription is not a key — it is an
OAuth session the CLI holds on the machine where you signed in. There is nothing to paste, so
BOSS looks for the CLI instead. That works perfectly where your login lives (Path B) and not at
all anywhere else (Path C), which needs a token that can travel.

---

## Path A — connect an API key

### What you need

- An API key from one of the providers below.
- The BOSS console, or the API and your `BOSS_API_KEY`.

| Provider | Base URL | Example model | Get a key |
|---|---|---|---|
| OpenRouter | `https://openrouter.ai/api/v1` | `anthropic/claude-sonnet-4.5` | <https://openrouter.ai/keys> |
| OpenAI | `https://api.openai.com/v1` | `gpt-4o` | <https://platform.openai.com/api-keys> |
| Anthropic | `https://api.anthropic.com/v1` | `claude-sonnet-4-5` | <https://console.anthropic.com/settings/keys> |
| Custom | whatever you supply | whatever you supply | your own endpoint |

### Steps

**In the console:** Setup → Model → choose a provider → paste the key → **Verify and save**.

**Or with the API:**

```bash
curl -X POST "${BOSS_API:?your BOSS base URL}/v1/setup/model" \
  -H "x-boss-api-key: ${BOSS_API_KEY:?your BOSS API key}" \
  -H 'Content-Type: application/json' -d '{
  "project_id": "boss-dogfood", "actor": "ceo",
  "provider": "openrouter", "api_key": "sk-or-your-key-here",
  "model": "anthropic/claude-sonnet-4.5"
}'
```

BOSS verifies the key with one real call **before** storing it, then records it as a `SecretRef`
and registers the entitlement source. You are done — there is no separate storage step.

### Verify

```bash
curl -H "x-boss-api-key: ${BOSS_API_KEY:?your BOSS API key}" \
     "${BOSS_API:?your BOSS base URL}/v1/setup/model-providers"
```

### If it fails

| Status | What BOSS tells you | What to do |
|---|---|---|
| 401 | that key was rejected — check it was copied whole | Re-copy the key; they are long and easy to truncate. |
| 403 | the key is valid but not allowed to use that model | Your account cannot reach that model. Try the example model above. |
| 404 | no model called *X* at *URL* — check the model name | The model name or base URL is wrong. |
| 429 | the key works; the provider is rate-limiting right now | Wait and retry. |
| 0 | could not reach *URL* | Network or base URL problem, not a key problem. |
| 409 | …is a subscription, not an API key | You picked `claude_code`, `codex` or `cursor`. Go to Path B or C. |

Nothing is stored in any of these cases, so you can simply retry.

---

## Path B — connect a subscription on your own machine

Use this when BOSS runs where you are signed in — your laptop, a dev box, or a container you
`exec` into and log in from.

### What you need

| Subscription | Install | Sign in |
|---|---|---|
| Codex (ChatGPT plan) | `npm install -g @openai/codex` | `codex login` |
| Claude Code | `npm install -g @anthropic-ai/claude-code` | `claude login` |

```bash
npm install -g @openai/codex
codex login
```

### Steps

**In the console:** Setup → Model → **Codex subscription** → **Connect**.

**Or from the command line:**

```bash
boss-engine call detect_coding_agents --args '{}'
boss-engine call connect_coding_agent --args '{"provider":"codex"}'
```

BOSS checks the binary runs, asks the CLI whether it is signed in, and then **makes it complete
one real turn in a read-only sandbox** before recording anything. This is deliberate: a CLI
reports "logged in" from a token on disk, which tells you somebody signed in once — not that
the session still works or that the plan is active.

BOSS stores **no credential** on this path. The CLI keeps its own session; BOSS records only an
entitlement row noting that the subscription exists and is owner-only.

### Verify

```bash
boss-engine call detect_coding_agents --args '{}'
```

You want your provider listed with `"ready": true`.

### If it fails

Read the `next_step` field — it is written to be the command you run next.

| `next_step` | What it means |
|---|---|
| `npm install -g @openai/codex` | The CLI is not on `PATH` **for the user BOSS runs as**, which is often a different user than yours. |
| `sign in with: codex login` | Installed, not signed in — or signed in as a different OS user. |
| `set CODEX_ACCESS_TOKEN on this install (…)` | BOSS decided this machine has no browser to sign in from. You want Path C. |

The response also carries `"installed"`, `"authenticated"` and `"version"` per agent, so you can
see which of the three stages failed rather than inferring it.

---

## Path C — connect a subscription to BOSS on a server

Your subscription is authorised on your laptop; BOSS is somewhere else. Detection will correctly
report `installed: false` on the server, because the CLI genuinely is not there.

The bridge is a **travelling token** — a headless credential the CLI mints, which you put
somewhere the server can read.

### Step 1 — check your CLI can mint one

| Subscription | Can it travel? | How the token is minted |
|---|---|---|
| Claude Code | Yes | `claude setup-token` prints one |
| Codex | Yes | read from the Codex CLI's own credential file (the script in Step 3 does this) |
| Cursor | **No** | No token route exists. Cursor only works on Path B. |

Both CLIs ship inside the BOSS image, so you install nothing on the server. The image carries
binaries and no credentials; you supply the token.

To check programmatically rather than by table, each agent reports a `portable` field — `true`
means it has a token route and can be used from a server at all:

```bash
curl -H "x-boss-api-key: ${BOSS_API_KEY:?your BOSS API key}" \
     "${BOSS_API:?your BOSS base URL}/v1/setup/coding-agents"
```

### Step 2 — choose where the token will live

This is the decision the rest of the path depends on. All four options work; they differ in what
you must already run and what rotating the token later costs you.

| Option | Choose it when | You configure on BOSS | Rotating the token later |
|---|---|---|---|
| **1. Environment variable** | You can set env vars on the deployment and want the fewest moving parts | nothing | Set the new value and **restart** |
| **2. Secret-store service (HTTP)** | You already run one, or cannot edit files on the box at all | `BOSS_PLATFORM_SECRET_STORE_URL` + a store token | Just write the new value — no restart |
| **3. Vault / OpenBao** | You already run Vault | `BOSS_VAULT_ADDR` + `BOSS_VAULT_TOKEN` | Just write the new value — no restart |
| **4. Local encrypted vault** | One box, no external secret service, and you want the token off the environment | `BOSS_KMS_MASTER_KEY` | Just write the new value — no restart |

BOSS looks for the token in the environment **first**, then in the secret backend. So Option 1
always wins if the variable is set — which is useful while you are working something out, and a
trap if you forget you set it.

> **The one thing to get right for Options 2–4.** BOSS uses a *single* secret backend: the first
> one of `platform_secret_store → vault → local_vault → database` that is configured and
> answering. It does not search all of them. If you configure a secret-store service **and**
> Vault, BOSS reads the secret-store service, and a token you put in Vault will not be found.
> Put the token in the backend BOSS will actually select, and confirm with the check in Step 5.

### Step 3 — put the token there

#### Option 1 — environment variable

Set `CODEX_ACCESS_TOKEN` (or `CLAUDE_CODE_OAUTH_TOKEN`) however your deployment sets environment
variables — `docker run -e`, a compose file, a Kubernetes secret, a systemd unit, your platform's
config UI. For Claude:

```bash
claude setup-token     # prints the token; put it in your deployment's environment
```

For Codex, get the value with the helper script's dry run, which reports the token without
printing it, then set that variable through your deployment's normal mechanism:

```bash
python3 scripts/store_subscription_token.py codex --dry-run
```

Then restart BOSS.

#### Option 2 — secret-store service over HTTP

Run this **on the machine where you are signed in**. It reads the token from the CLI's own
credential file and posts it straight to the store, so it never passes through your terminal,
clipboard or shell history:

```bash
export OPS_BASE_URL=http://127.0.0.1:8099
python3 scripts/store_subscription_token.py codex --dry-run   # confirm first, sends nothing
python3 scripts/store_subscription_token.py codex
```

It prints the token's length and destination, never the token itself. It reads the store's own
auth token from `~/.config/boss/ops-token`; use `--ops-token-file` for another path.

For Claude Code, `claude setup-token` prints the token and you write it to the same store under
the key `CLAUDE_CODE_OAUTH_TOKEN`.

#### Option 3 — Vault / OpenBao

Write the token to your Vault KV mount under the key `CODEX_ACCESS_TOKEN`, using your normal
Vault tooling and policies.

#### Option 4 — local encrypted vault

The token is encrypted with AES-256-GCM into files under `BOSS_SECRETS_DIR`. Set a master key
first, or BOSS generates one and stores it in the same database as the secrets — which survives
redeploys but means anyone with the database has both halves.

### Step 4 — tell BOSS where to look

Skip this for Option 1; the environment needs no configuration.

| Option | Set on the BOSS install |
|---|---|
| 2 | `BOSS_PLATFORM_SECRET_STORE_URL`, plus `BOSS_PLATFORM_SECRET_STORE_TOKEN_FILE` (preferred) or `BOSS_PLATFORM_SECRET_STORE_TOKEN` |
| 3 | `BOSS_VAULT_ADDR`, `BOSS_VAULT_TOKEN`, optionally `BOSS_VAULT_KV_MOUNT` and `BOSS_VAULT_NAMESPACE` |
| 4 | `BOSS_KMS_MASTER_KEY` (or `BOSS_KMS_MASTER_KEY_FILE`), optionally `BOSS_SECRETS_DIR` |

Prefer the `_FILE` variants where they exist. A token in a file is not in the process
environment, where anything that dumps env — a crash reporter, a subprocess, a log line — would
carry it along.

Restart BOSS after changing any of these, since they are read at startup.

### Step 5 — verify

```bash
curl -H "x-boss-api-key: ${BOSS_API_KEY:?your BOSS API key}" \
     "${BOSS_API:?your BOSS base URL}/v1/setup/coding-agents"
```

On the first run that needs it, BOSS authorises the CLI from the stored token by piping it on
**stdin** — never argv, which is visible in `ps` to every process on the box.

### If it fails

| What you see | What it means |
|---|---|
| `no CODEX_ACCESS_TOKEN available to this install` | BOSS found nothing in the environment or in the selected backend. Re-check Step 2's warning about which backend gets selected. |
| `token rejected (exit N)` | A value was found and the CLI refused it: expired, or not actually a token. Mint it again. BOSS deliberately logs only the exit status, because the CLI's own error can echo the token back. |
| `platform secret store is not configured` | `BOSS_PLATFORM_SECRET_STORE_URL` and its token are not both set — see Step 4. |
| The store accepted your write but BOSS still cannot see it | Either the value is wrong (a store will happily accept a placeholder and report success) or BOSS is selecting a different backend. |

---

## Optional — make every surface use one provider

By default the coding pipeline, its reviewers, and the chat surfaces can each be pointed
separately. One setting points them together:

```bash
BOSS_PROVIDER_LOCK=codex        # or openrouter, groq, deepseek, ollama, vllm, claude_code, cursor
```

Leave it unset and nothing changes.

**How far it reaches depends on whether the provider has an API:**

| Lock | Coding pipeline, reviewers, strong/low-cost lanes | Agent chat, meetings, discovery |
|---|---|---|
| `openrouter`, `groq`, `deepseek`, `ollama`, `vllm` | moved | moved, credentials rebound |
| `claude_code` | moved | moved |
| `codex`, `cursor` | moved | **not moved** |

Codex and Cursor are CLIs BOSS shells out to rather than APIs it can call, and only Claude Code
has a chat adapter today. So a `codex` lock puts the whole coding pipeline on Codex and leaves
chat, meetings and discovery on whatever `OPENAI_*` credentials are configured.

### Checking what actually happened

Configuration says what you asked for; the ledger says what ran.

```bash
boss-engine call provider_audit --args '{"expect":"openai","dimension":"vendor"}'
boss-engine call list_harnesses --args '{}'
```

```
transport=openrouter  vendor=anthropic  calls=1
transport=openrouter  vendor=deepseek   calls=2
claim "every model is openai" -> VIOLATED by ['anthropic', 'deepseek']
```

`transport` is the endpoint BOSS called; `vendor` is who trained the weights. Routing
`anthropic/claude-opus-4.5` through OpenRouter means the endpoint was OpenRouter and your prompt
still reached Anthropic. Ask for `dimension: vendor` when the question is whose model saw your
code, and `transport` when it is whose endpoint you called.

---

## Reference — direct provider lanes

Beyond the four catalogue providers in Path A, these lanes take their own credentials, so a cheap
lane can never bill your main key.

| Lane | Key | Base URL |
|---|---|---|
| `openrouter` | `OPENROUTER_API_KEY` | `OPENROUTER_BASE_URL`, default `https://openrouter.ai/api/v1` |
| `groq` | `GROQ_API_KEY` | `GROQ_BASE_URL`, default `https://api.groq.com/openai/v1` |
| `deepseek` | `DEEPSEEK_API_KEY` | `DEEPSEEK_BASE_URL`, default `https://api.deepseek.com/v1` |
| `ollama` | none needed | `OLLAMA_BASE_URL`, default `http://localhost:11434/v1` |
| `vllm` | none needed | `VLLM_BASE_URL`, **required** |
| `azure` | `AZURE_OPENAI_KEY` | `AZURE_OPENAI_ENDPOINT` + `AZURE_OPENAI_DEPLOYMENT` |

`ollama` and `vllm` need no cloud credential. Local inference is free, so zero-price the models
or cost accounting will invent a bill:

```bash
BOSS_MODEL_PRICES_JSON='{"qwen2.5-coder:14b":[0,0]}'
```

DeepSeek's first-party API is China-hosted. Fine for evaluation; consider it before customer code
goes through it.

## Reference — secret backends

| Backend | Configure with |
|---|---|
| `platform_secret_store` | `BOSS_PLATFORM_SECRET_STORE_URL` + `BOSS_PLATFORM_SECRET_STORE_TOKEN_FILE` or `_TOKEN` |
| `vault` | `BOSS_VAULT_ADDR` + `BOSS_VAULT_TOKEN` (`VAULT_ADDR` / `VAULT_TOKEN` also read) |
| `local_vault` | `BOSS_KMS_MASTER_KEY` or `BOSS_KMS_MASTER_KEY_FILE`, `BOSS_SECRETS_DIR` |
| `database` | nothing — the fallback |
| `environment`, `file` | explicit references only |

BOSS selects the first of `platform_secret_store → vault → local_vault → database` that is
configured and healthy. Postgres stores **pointers**, never values: a `SecretRef` names a backend
and a key.

`database` is last on purpose and is not a good place for a token. It exists so that setup can
always complete on an installation that has nothing else configured, because the alternative
people fall back to is a plaintext environment variable anyway.

### Running the secret store yourself

The `platform_secret_store` backend speaks a small HTTP contract, authenticated with an
`X-Ops-Token` header:

| Verb | Meaning |
|---|---|
| `GET /secrets` | list keys, values withheld |
| `GET /secret?key=` | read one value |
| `POST /secret` | `{"key": ..., "value": ...}` |
| `DELETE /secret?key=` | remove one |

Any service implementing those four verbs works with BOSS unmodified, so you can run your own
container and keep every credential inside your infrastructure. If you are evaluating this,
three things are worth deciding up front: how callers are authorised (BOSS sends one token per
application, which is all-or-nothing), how you back the store up, and that the store should
**not** be reachable from the coding sandbox — that environment is deny-by-default precisely so
a sandboxed process cannot reach credentials.

## Status

Honest state of each path, so you know what has been proven rather than merely built.

| Path | State |
|---|---|
| Path A, all providers and lanes | Working; keys verified before they are stored |
| Path B, Codex | Working — detected, proved with a real turn, entitlement recorded |
| Path B, Claude Code | Works, but configured by environment variable rather than detection |
| Path C, Option 1 (environment variable) | Implemented; **not yet proven end to end** |
| Path C, Options 2–4 (secret backends) | Implemented; **not yet proven end to end**. Option 2 also needs a build that has `BOSS_PLATFORM_SECRET_STORE_URL` — before it was added, that backend could not be selected and BOSS silently used the next one down |
| `BOSS_PROVIDER_LOCK`, coding pipeline | Working; proven on OpenRouter and DeepSeek with zero Anthropic calls |
| `BOSS_PROVIDER_LOCK`, chat under a `codex` lock | Does not move — see the table above |
| Cursor | Path B only |

`list_harnesses` reports `verified: false` for any harness where a full governed code task has
not yet completed — build, independent review, gate, PR. Wired is not proven.
