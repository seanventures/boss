# Connecting a model to BOSS

**Status:** for review (Sean + Param). Describes what is in `main` today, and says plainly where
something is unproven.

BOSS runs on whichever provider you point it at. There are three ways a model reaches it, and
they are genuinely different — not three spellings of the same thing:

| | What it is | Where it works | What BOSS stores |
|---|---|---|---|
| **API key** | A key you buy and paste | Anywhere | The key, in a secret store |
| **Subscription (local)** | A CLI you are signed into | Only where that login lives | Nothing |
| **Subscription (remote)** | The headless token that CLI mints | Anywhere | The token, in a secret store |

The distinction that catches people: **a Codex or Claude subscription is an OAuth session the
CLI holds on your machine.** There is no credential to paste, so a form asking for one is asking
for something that does not exist. BOSS finds the CLI instead — which works beautifully on a
laptop and not at all on a server, because your login is not on the server. That is what the
third row exists for.

---

## 1. API keys

The providers BOSS ships a catalogue entry for. Every one of them is verified before it is
stored: BOSS spends one token proving the key works, because a wizard that stores a key and
reports success has only verified that you can type.

| Provider | Base URL | Example model | Get a key |
|---|---|---|---|
| OpenRouter | `https://openrouter.ai/api/v1` | `anthropic/claude-sonnet-4.5` | <https://openrouter.ai/keys> |
| OpenAI | `https://api.openai.com/v1` | `gpt-4o` | <https://platform.openai.com/api-keys> |
| Anthropic | `https://api.anthropic.com/v1` | `claude-sonnet-4-5` | <https://console.anthropic.com/settings/keys> |
| Anything OpenAI-compatible | you supply | you supply | — |

**Console:** Setup → Model → pick a provider → paste key → *Verify and save*.

**API:**

```bash
curl -X POST "$BOSS_API/v1/setup/model" \
  -H "x-boss-api-key: $BOSS_API_KEY" -H 'Content-Type: application/json' -d '{
  "project_id": "boss-dogfood", "actor": "ceo",
  "provider": "openrouter", "api_key": "sk-or-…", "model": "anthropic/claude-sonnet-4.5"
}'
```

It verifies **before** it stores, then records the key as a `SecretRef` and registers the
entitlement source. Storing first and verifying afterwards leaves a broken key recorded as
configured, and the operator finds out an hour later when a build fails — by which point the key
is the last thing anyone suspects. A rejected key says *which* kind of wrong: a bad key, a model
your account cannot reach, or a rate limit — rather than "setup failed".

Post a **subscription** provider here and you get a 409 that redirects rather than a generic
rejection: a subscription is authorised through its own login, so the wizard says where that
flow lives instead of showing a key box that would be refused on submit.

Discover what this build supports rather than trusting this table:

```bash
curl -H "x-boss-api-key: $BOSS_API_KEY" "$BOSS_API/v1/setup/model-providers"
```

### Direct provider lanes

These are separate credential pairs so a cheap lane can never bill the platform-managed
`OPENAI_*` key. Set the pair and the lane is available:

| Lane | Key | Base URL (default) |
|---|---|---|
| `openrouter` | `OPENROUTER_API_KEY` | `OPENROUTER_BASE_URL` — `https://openrouter.ai/api/v1` |
| `groq` | `GROQ_API_KEY` | `GROQ_BASE_URL` — `https://api.groq.com/openai/v1` |
| `deepseek` | `DEEPSEEK_API_KEY` | `DEEPSEEK_BASE_URL` — `https://api.deepseek.com/v1` |
| `ollama` | none needed | `OLLAMA_BASE_URL` — `http://localhost:11434/v1` |
| `vllm` | none needed | `VLLM_BASE_URL` — **required**, e.g. `http://gpubox:8000/v1` |
| `azure` | `AZURE_OPENAI_KEY` | `AZURE_OPENAI_ENDPOINT` + `AZURE_OPENAI_DEPLOYMENT` |

`ollama` and `vllm` are the local/self-hosted lanes and need no cloud credential at all — the
OpenAI client refuses an empty API key, so BOSS supplies a placeholder the server ignores.
Local inference is $0, so zero-price the models or cost accounting will invent a bill:

```bash
BOSS_MODEL_PRICES_JSON='{"qwen2.5-coder:14b":[0,0]}'
```

DeepSeek's first-party API is China-hosted. Fine for evals; think about it before customer code
goes through it.

---

## 2. Subscriptions, on the machine you are sitting at

For a **self-hosted BOSS running where you are signed in** — a laptop, a dev box, a container
you `exec` into and sign in.

### Codex

```bash
npm install -g @openai/codex
codex login                      # browser, ChatGPT account
```

Then in the console: Setup → Model → **Codex subscription** → *Connect*.

BOSS checks the binary, reads whether it is signed in, and **makes it complete one real turn in
its read-only sandbox** before recording anything. Signed in is not the same as working: a CLI
reports "logged in" from a token on disk, which says somebody signed in once — not that the
session still works or that the plan is active.

Equivalent without the console:

```bash
boss-engine call detect_coding_agents --args '{}'
boss-engine call connect_coding_agent --args '{"provider":"codex"}'
```

or `GET /v1/setup/coding-agents` and `POST /v1/setup/coding-agents/connect`.

### Claude Code

```bash
npm install -g @anthropic-ai/claude-code
claude login
```

Claude is currently wired through an environment variable rather than detection (see §3), so the
console still asks you to mint a token. **That asymmetry is not deliberate** — Claude predates
the detection path and has not been moved onto it. Worth closing.

### What BOSS stores

Nothing. The CLI holds its own session; BOSS shells out to it and records an entitlement row
saying the subscription exists, is owner-only, and is not licensed for multi-tenant work.

---

## 3. Subscriptions, on a server

**This is the case that matters for a shipped product**, and the one that is easy to get wrong.

Your subscription is authorised on your laptop. The pipeline runs in a container. Detection
correctly reports `installed: false` there, because the CLI genuinely is not on that machine.
Measured on the live deployment: the goldeneye API said `codex installed=False` while the same
check on the laptop said ready. Both were right.

The bridge is the **headless token the CLI mints for exactly this**:

| CLI | Mint it | BOSS reads |
|---|---|---|
| Claude Code | `claude setup-token` | `CLAUDE_CODE_OAUTH_TOKEN` |
| Codex | read the access token from the signed-in machine's `~/.codex/auth.json` | `CODEX_ACCESS_TOKEN` |
| Cursor | **no token route** | — |

Both CLIs ship in the BOSS image. The image carries binaries and **no credentials**; the
deployment supplies the token.

Store it through the secret store — **no filesystem access to the deployment required**, which
matters because the operator setting this up generally cannot edit `.env` on the box:

```bash
OPS=$(cat ~/.config/boss/ops-token)
curl -X POST -H "X-Ops-Token: $OPS" -H 'Content-Type: application/json' \
     -d '{"key":"CODEX_ACCESS_TOKEN","value":"<token>"}' \
     "$OPS_BASE_URL/secret"
```

On first use BOSS runs `codex login --with-access-token`, piping the token on **stdin** — never
argv, which is visible in `ps` to every process on the box. A rejected token logs the exit status
and nothing else, because the CLI's own error can quote back what it was given.

**Known rough edge.** `claude setup-token` is a first-class command; Codex has no equivalent, so
its step is "open a JSON file and copy a field" — a manual credential handling step in the middle
of an otherwise automated flow, and the one place this design asks a human to touch a live token.
The fix is for `connect_coding_agent` to read it and write it to the secret store itself, so the
token goes machine-to-machine and is never on a clipboard. Not built. **Recommend deciding on
this before the doc goes to customers** — it is the weakest point in the flow.

> **Cursor cannot be used on a hosted BOSS today.** It has no documented token route, so it only
> works where its login lives. Using it properly needs a different plan — parked deliberately,
> not overlooked.

---

## 4. Pointing everything at one provider

```bash
BOSS_PROVIDER_LOCK=codex        # or openrouter, groq, deepseek, ollama, vllm, claude_code, cursor
```

Before this it took six settings in three vocabularies, and an operator who missed one was
indistinguishable from one who set them all. Leave it unset and nothing changes — an
installation that already tuned its per-surface settings is not re-pointed by an upgrade.

**How far the lock reaches depends on whether the provider has an API:**

| Lock | Coding pipeline + reviewers + strong/low-cost lanes | Agent chat, meetings, discovery |
|---|---|---|
| `openrouter`, `groq`, `deepseek`, `ollama`, `vllm` | moved | moved — credentials rebound to that lane |
| `claude_code` | moved | moved (`claude_cli`) |
| `codex`, `cursor` | moved | **not moved** |

That last row is the one to read carefully. Codex and Cursor are CLIs BOSS shells out to, not
APIs it can call, and only `claude_code` has a chat adapter. So a `codex` lock moves the whole
coding pipeline onto Codex and leaves chat/meetings/discovery on whatever `OPENAI_*` is
configured. **That is a real remaining coupling** — the pipeline is provider-agnostic, the chat
surfaces are not yet, for Codex specifically. It is visible rather than hidden: the audit below
will show the other vendor's calls.

### Proving it

Config says what was asked for. The ledger says what happened.

```bash
boss-engine call provider_audit --args '{"expect":"openai","dimension":"vendor"}'
boss-engine call list_harnesses --args '{}'
```

```
transport=openrouter  vendor=anthropic  calls=1   ← Claude via OpenRouter still reached Anthropic
transport=openrouter  vendor=deepseek   calls=2
claim "every model is openai" -> VIOLATED by ['anthropic', 'deepseek']
```

**Transport and vendor are different questions.** Routing `anthropic/claude-opus-4.5` through
OpenRouter means the endpoint was OpenRouter and the weights were Anthropic's — your prompt
still reached Anthropic. A report that collapsed the two would let someone claim "no Anthropic"
while sending Anthropic every token.

---

## 5. Where secrets live

BOSS does not grow its own vault. It uses the best store the installation actually has, trying
the most protected first and falling back until one answers:

| Backend | Configure with | Notes |
|---|---|---|
| `platform_secret_store` | `BOSS_PLATFORM_SECRET_STORE_URL` + `_TOKEN` or `_TOKEN_FILE` | An ops API speaking the four verbs below |
| `vault` | `BOSS_VAULT_ADDR` + `BOSS_VAULT_TOKEN` (`VAULT_*` also read) | HashiCorp Vault / OpenBao; `BOSS_VAULT_KV_MOUNT`, `BOSS_VAULT_NAMESPACE` |
| `local_vault` | `BOSS_KMS_MASTER_KEY` (or `_FILE`), `BOSS_SECRETS_DIR` | AES-256-GCM files on disk |
| `database` | nothing | Last resort |
| `environment`, `file` | — | Explicit refs only |

Prefer `_TOKEN_FILE` over `_TOKEN`: a token in a file is not in the process environment, where
anything that dumps env — a crash reporter, a subprocess, a log line — carries it along.

`database` is last deliberately and is **not a good place for a token**. It is there because a
wizard that refuses to complete is worse than one that completes and can be improved, and
because the alternative an operator falls back to is a plaintext env var anyway. When no master
key is configured, the local vault generates one and stores it in the same database as the
secrets — durable, not readable by casual inspection, but anyone with the database has both
halves. Set `BOSS_KMS_MASTER_KEY` to separate them.

Postgres holds **pointers**, never values: a `SecretRef` names a backend and a key.

> **Fixed while writing this doc.** `platform_secret_store` is the first backend the fallback
> chain tries, but it read settings fields that were never defined — and `Settings` ignores
> unknown env vars — so it raised "not configured" no matter what the operator configured, and
> every installation silently fell through to a lesser store. The three settings above now
> exist, with tests pinning that the backend can be selected. A backend nothing can select is
> not a backend.

### Open question for Sean — the secret service as its own thing

Sean's proposal: ship the secret service as open source so a customer runs it in their own
container, and BOSS has no file, config or database holding a secret.

**The seam already exists and is load-bearing.** `PlatformSecretStoreBackend` speaks a four-verb
HTTP contract, authenticated by an `X-Ops-Token` header:

| Verb | Meaning |
|---|---|
| `GET /secrets` | list keys, values withheld |
| `GET /secret?key=` | one value |
| `POST /secret` | `{"key": ..., "value": ...}` |
| `DELETE /secret?key=` | remove one |

Any service implementing those four verbs drops in with **no change to BOSS** — this is already
how the goldeneye deployment works. It would also let `database` be removed from the fallback
chain rather than defended.

Worth deciding at the same time:

- **What authorises a caller?** Today it is a single ops token per app, all-or-nothing. Per-project
  scoping is what would make one service safe for several BOSS tenants.
- **Does it need to be reachable from the sandbox?** It currently is not, and that is a feature.
  The coding agent's environment is a deny-by-default allow-list precisely so a sandboxed process
  cannot reach credentials. On 2026-09-02 an agent's test run inherited a production
  `DATABASE_URL` and cleared 204 tables; that is the failure mode this guards.
- **Where do subscription tokens fit?** They are not API keys. They expire, they are minted per
  machine, and re-minting is a routine operation rather than an incident — so the service needs
  a story for rotation that does not involve a human noticing a pipeline has started failing.
- **Does it mirror?** The current store is backed up and mirrored off-site. A customer-run
  container that loses its volume loses every credential BOSS holds.

## 6. Status, honestly

| Path | State |
|---|---|
| API keys — openrouter, openai, anthropic, custom | Working; verified before storing |
| Direct lanes — groq, deepseek, ollama, vllm, azure | Working |
| Subscription on a local install (Codex) | Working — detected, proved with a real turn, entitlement recorded |
| Subscription on a local install (Claude) | Works, but via env var rather than detection |
| Subscription on a server (token) | Implemented; **not yet proven end to end** |
| Codex token export | Manual — no `setup-token` equivalent; see §3 |
| `BOSS_PROVIDER_LOCK` — coding pipeline | Working; proven on OpenRouter/DeepSeek with zero Anthropic calls |
| `BOSS_PROVIDER_LOCK` — chat surfaces under `codex` | **Not moved.** Real remaining coupling (§4) |
| `platform_secret_store` | Fixed in this change; previously unselectable |
| Cursor | Registered, unverified, hosted-BOSS blocked |

The `codex` harness is registered `verified: false` and stays that way until a full governed code
task completes on it — build, independent review, gate, PR. Wired is not proven, and a registry
that says otherwise stops meaning anything. `boss-engine call list_harnesses --args '{}'` reports
the current split rather than asking you to trust this table.

### What to decide with Sean

1. **Is the open-source secret service the right shape?** The four-verb contract in §5 already
   exists and already works; the open questions are multi-tenant scoping, rotation, and mirroring
   — not the protocol.
2. **Does Codex's manual token step ship as-is?** It is the only point where a human handles a
   live credential. Automating it is small.
3. **Do the chat surfaces need to follow the lock for CLI providers?** Today "provider agnostic"
   is true of the coding pipeline and not of agent chat under a Codex lock. Either build the
   adapter or say plainly that CLI subscriptions govern code and not conversation.
