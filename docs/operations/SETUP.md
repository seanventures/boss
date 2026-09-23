# Setting up BOSS

Two parts: get it running, then give it a model. BOSS ships with no model of its own.

You need **Docker** and **Node 18+**, and a clone of this repository. All commands run from its
root.

---

## Where to start

| I want to | Go to |
|---|---|
| Run BOSS on my own machine | [1A — On this machine](#1a--on-this-machine) |
| Run BOSS on a server, driven from my laptop | [1B — On a server](#1b--on-a-server-from-your-laptop) |
| Know whether my OS is supported | [Will it run on my machine?](#will-it-run-on-my-machine) |
| Give BOSS an API key | [2A — API key](#2a--api-key) |
| Use my Claude or Codex subscription | [Which path is yours?](#which-path-is-yours) |
| Choose which model does what | [Choose a model per role](#choose-a-model-per-role) |
| Check the install actually works | [Part 3 — Prove it works](#part-3--prove-it-works) |
| Change something after installing | [Making changes](#making-changes) |
| Fix an install that went wrong | [When the install goes wrong](#when-the-install-goes-wrong) |
| Remove it completely | [Taking it off again](#taking-it-off-again) |
| Know what to set `$BOSS_API` / `$BOSS_API_KEY` to | [Setting the variables](#setting-boss_api-and-boss_api_key) |
| Know where my credentials are stored | [Where secrets live](#reference--where-secrets-live) |

## Will it run on my machine?

| OS | Supported | What you need |
|---|---|---|
| **macOS** — Apple Silicon or Intel | Yes | Docker Desktop |
| **Linux** — x86_64 or arm64 | Yes | Docker Engine + Compose v2 |
| **Windows** | **Through WSL2 only** | Docker Desktop with the WSL2 backend, then install *inside* your WSL2 distribution |

The installer is a bash script that branches on `uname`. Inside WSL2 that reports `Linux`, so
it takes the Linux path and behaves like any other Linux install — clone into the WSL2
filesystem rather than `/mnt/c`, or Docker's file watching will be slow. **Native PowerShell,
CMD and Git Bash are not supported**: Git Bash rewrites the container paths the compose file
depends on, and there is no PowerShell installer.

Whatever you run BOSS on, you can drive a *server* install from any of the three — see
[1B](#1b--on-a-server-from-your-laptop). That only needs `ssh`, `git` and `tar` locally.

---

# Part 1 — Install

## Where will BOSS run?

Pick one. Everything after this reads the same either way, but the two differ in how you reach
the API and where your credentials end up.

| | On this machine | On a server |
|---|---|---|
| **For** | trying it, developing against it | real work, a shared install, anything long-running |
| **Install with** | `./install.sh` | `./scripts/install/remote.sh … install` from your laptop |
| **Console at** | `http://127.0.0.1:3000` | the server's address, or an SSH tunnel |
| **Credentials land in** | this machine's secret store | **the server's** secret store |
| **Go to** | [1A](#1a--on-this-machine) | [1B](#1b--on-a-server-from-your-laptop) |

## 1A — On this machine

```bash
node packages/create-boss/index.js
```

That is the whole setup. It picks ports that are free on your machine, starts the containers,
seeds the database, provisions a secret store, and prints where everything is:

```
  Console       http://127.0.0.1:3000/?org=org_boss
  API           http://127.0.0.1:8000
  Secret store  http://127.0.0.1:8080   (admin@boss.local)
```

Your ports will differ if the defaults were taken — it says which it chose.

Credentials are written to `.env`. **Back that file up**: `INFISICAL_ENCRYPTION_KEY` is the only
way to read what the secret store holds. Re-running is safe; it never replaces a key that
already exists.

Now go to [Part 2](#part-2--connect-a-model).

## 1B — On a server, from your laptop

Install it from here. Do not copy the repository up and SSH in — that is three steps, each easy
to get subtly wrong, and it leaves a tree on the server that nobody can identify afterwards.

**You still clone on your own machine.** `remote.sh` lives in this repository and drives the
server over SSH; you are not installing locally, you are running the tooling that installs
remotely.

```bash
git clone https://github.com/seanheiney/boss.git ~/boss
cd ~/boss
```

**On the server you need** SSH access and Docker with Compose. Nothing else — not git, not a
deploy key, not access to this repository.

`--key` is the **private** key on your own machine — the file *without* `.pub`, exactly what
`ssh -i` takes. The `.pub` half is what sits in the server's `~/.ssh/authorized_keys`. Omit
`--key` entirely if plain `ssh root@YOUR.SERVER.IP` already works.

No keypair yet, or building the VM now? The README has the full sequence — generate, install
the public half, prove it — under
[**No SSH key yet?**](../../README.md#install-on-a-server). It is kept in one place so the two
cannot drift apart.

```bash
./scripts/install/remote.sh \
  --host root@YOUR.SERVER.IP \
  --key ~/.ssh/id_ed25519 \
  install --yes
```

That is the whole remote install. It streams a `git archive` of your current commit over the
SSH connection and runs the same `install.sh` on the far side, so:

* the server needs **no git, no deploy key and no access to this repository** — only Docker;
* what lands there is a **named commit**, written to `.boss-shipped-commit`, so "which version
  is running" has an answer;
* `--public-url` is derived from `--host` for you (see the warning further down about why
  `127.0.0.1` is the failure that looks like success).

Ship a specific version with `--ref`, and pass anything else straight through to the installer
after `--`:

```bash
./scripts/install/remote.sh --host root@YOUR.SERVER.IP --ref v1.2.0 install --yes
./scripts/install/remote.sh --host root@YOUR.SERVER.IP install --yes -- --anthropic-key "$ANTHROPIC_API_KEY"
```

### Did the remote install work?

```bash
./scripts/install/remote.sh --host root@YOUR.SERVER.IP smoke    # API, database, console
./scripts/install/remote.sh --host root@YOUR.SERVER.IP check    # the full checkup
./scripts/install/remote.sh --host root@YOUR.SERVER.IP status   # containers, ports, capacity
./scripts/install/remote.sh --host root@YOUR.SERVER.IP logs --service api
```

`smoke` is the one to run first. It proves four things the installer's own "done" message does
not: the containers are up, `/health` answers, the schema actually applied (it counts tables),
and the console was built for an address a browser can reach.

### Taking it off again

```bash
./scripts/install/remote.sh --host root@YOUR.SERVER.IP uninstall --yes
```

`--yes` is required — there is no prompt over a non-interactive SSH session to catch a mistake,
and this destroys the server's database and its secret-store key. It then verifies what "clean"
means rather than trusting its own exit code: no containers, no volumes, no BOSS images, no
`.env`. Upstream base images like `postgres:16-alpine` are deliberately left, so the next
install does not re-download them.

The source tree at `--dir` is left in place. Remove it yourself if you want the machine bare.

---

## Making changes

The containers are built from this repository, not mounted from it, so editing a file does not
change what is running. Rebuild the service you changed:

```bash
docker compose -f scripts/install/community-compose.yml --env-file .env \
  --profile secrets --profile web up -d --build api worker
```

`api worker` for Python changes, `web` for the console. About a minute with warm layers.

While actually writing code, skip the containers instead:

```bash
pnpm dev
```

Uvicorn with reload, Next in dev mode, worker alongside — save a file and it is live, against
the same database and secret store.

## Stopping and starting

```bash
docker compose -f scripts/install/community-compose.yml --env-file .env --profile secrets --profile web stop
docker compose -f scripts/install/community-compose.yml --env-file .env --profile secrets --profile web start
```

To throw everything away, including every stored secret:

```bash
docker compose -f scripts/install/community-compose.yml --env-file .env --profile secrets --profile web down -v
```

## Installing by hand

The installer runs these, in order. Use them if it fails, or somewhere it cannot reach.

```bash
# 1. settings — an API key, and the two keys the secret store will not start without
printf 'BOSS_API_KEY=%s\n' "$(openssl rand -hex 16)" >> .env
sh scripts/install/gen_infisical_keys.sh .env

# 2. start — set BOSS_POSTGRES_PORT / INFISICAL_PORT / BOSS_API_PORT / BOSS_WEB_PORT in .env
#    first if 5432, 8080, 8000 or 3000 are taken on your machine
docker compose -f scripts/install/community-compose.yml --env-file .env \
  --profile secrets --profile web up -d --build

# 3. database contents — schema, plus the default org, workspace and project
docker compose -f scripts/install/community-compose.yml --env-file .env \
  exec -T api python -m boss_engine bootstrap

# 4. secret store — admin, organization, project and machine identity
sh scripts/install/bootstrap_infisical.sh .env

# 5. restart BOSS so it reads what step 4 wrote
docker compose -f scripts/install/community-compose.yml --env-file .env \
  --profile secrets --profile web up -d api worker
```

## When the install goes wrong

| What you see | What it means |
|---|---|
| `required variable INFISICAL_ENCRYPTION_KEY is missing` | Step 1 was not run, or `--env-file .env` was left off. |
| `port is already allocated` | The installer avoids this; by hand, set the four `*_PORT` values yourself. |
| `violates foreign key constraint … projects` | Step 3 was not run. |
| `the secret store cannot read its own data` | A volume from an earlier install, encrypted with a different key. Re-run with `--reset-secret-store` to rebuild it — everything it held is discarded. |
| `bootstrap returned no token` | Already provisioned. Infisical issues that token once; the existing one is in `.env`. |
| The console's organization picker does nothing | Known issue after a restart. Go straight to `/?org=org_boss`. |
| `Connected, but the check failed` with a 404 | The token cannot see that repository. For a PRIVATE repository GitHub returns 404 rather than 403, so this is a scope answer, not a missing repository — BOSS names the repositories the token *can* reach. Widen **Repository access** on the token and press Connect and test again. |
| Connecting again after fixing a token | Just press **Connect and test** again. The second attempt re-points the existing connection at the new credential and keeps its repository binding; it is a retry, not a second connection. |

---

---

# Part 2 — Connect a model

## Before anything else: what a model has to cover

BOSS spends models in three places, and they are configured separately:

| | What it is | Where it is set |
|---|---|---|
| **Builds** | writing and reviewing code | Implementer / reviewer roles, Step two |
| **The panel** | the meeting that writes your PRD | Console chat, agents and meetings |
| **The chat** | asking BOSS questions in the console | the same row |

A subscription covers all three. One CLI, one login, no key. An API key covers all three too.
What does NOT work is assuming the build model answers the chat — they are different settings
and an install that sets only the first has a chat and a panel that cannot answer.

## Setting `$BOSS_API` and `$BOSS_API_KEY`

Every `curl` in Part 2 and [Part 3](#part-3--prove-it-works) uses these two. Set them once here
and the rest of the page works unchanged.

**You may not need them at all.** The console does everything below, and a server takes
`remote.sh model` / `remote.sh subscription`, which use the server's own credentials and need
no variables. This is for driving it from a shell.

**BOSS on this machine:**

```bash
export BOSS_API=http://127.0.0.1:8000     # the API port Part 1 printed
export BOSS_API_KEY=$(grep '^BOSS_API_KEY=' .env | cut -d= -f2)
export PROJECT=boss-dogfood
```

**BOSS on a server** — one command, and it uses the server's real address and the server's key:

```bash
eval "$(./scripts/install/remote.sh --host root@YOUR.SERVER.IP --key ~/.ssh/id_ed25519 env)"
```

That prints the server's API key to your terminal, which is the point but worth knowing.

> **The key is the server's, not yours,** and the address is its IP — not `127.0.0.1`, which
> on your laptop means your laptop. The only time `127.0.0.1` is right for a server is when you
> are forwarding its ports with `remote.sh … tunnel`, which is optional and only worth it if
> you would rather `:8000` were not reachable from the internet.

**Why it matters which one you point at:** a credential lands in the secret store of whichever
install you are talking to. Aim at the wrong endpoint and the key is stored on the wrong
machine, where it reports connected and does nothing for the install you meant.

## Which path is yours?

| You have | Where BOSS runs | Go to |
|---|---|---|
| An API key | anywhere — this machine or a server | [2A — API key](#2a--api-key) |
| A Claude or Codex subscription | **directly on your host**, sharing the CLI's login | [2B — same filesystem](#2b--subscription-boss-on-this-machine) |
| A Claude or Codex subscription | in containers, or on a server | [2C — send the credential](#2c--subscription-boss-on-a-server) |

**If you installed with Part 1, you want 2C — even on your own laptop.** BOSS runs in
containers, and a container does not share your home directory, so the login you did on this
machine is invisible to it. "Same machine" is not the question; *same filesystem as the CLI's
login* is. That is why 2B and 2C split on where BOSS runs rather than on which machine you are
sitting at.

Path B is for BOSS running directly on your host (`pnpm dev`), or a container you `exec` into
and sign in inside.

## 2A — API key

| Provider | Example model | Get a key |
|---|---|---|
| `openrouter` | `anthropic/claude-sonnet-4.5` | <https://openrouter.ai/keys> |
| `openai` | `gpt-4o` | <https://platform.openai.com/api-keys> |
| `anthropic` | `claude-sonnet-4-5` | <https://console.anthropic.com/settings/keys> |
| `custom` | yours | your endpoint — also pass `"base_url"` |

**Console:** Setup → Model → pick provider → paste key → Verify and save. **Or:**

```bash
curl -X POST "${BOSS_API:?}/v1/setup/model" \
  -H "x-boss-api-key: ${BOSS_API_KEY:?}" -H 'Content-Type: application/json' -d '{
  "project_id": "boss-dogfood", "actor": "ceo",
  "provider": "openrouter", "api_key": "sk-or-your-key-here",
  "model": "anthropic/claude-sonnet-4.5"
}'
```

The key is verified before it is stored. Nothing is saved when it fails, so fix and retry.

| You get | Means |
|---|---|
| `422` · that key was rejected | Key wrong or truncated. |
| `422` · not allowed to use that model | Try the example model for that provider. |
| `422` · no model called … | Wrong model name or base URL. |
| `422` · rate-limiting right now | The key works. Retry shortly. |
| `409` · is a subscription, not an API key | Go to B or C. |
| `400` · a base URL is required | Add `"base_url"` for `custom`. |

## 2B — Subscription, BOSS on this machine

### Codex

```bash
npm install -g @openai/codex && codex login
```

**Console:** Setup → Model → Codex subscription → **Connect**. Or:

```bash
boss-engine call connect_coding_agent --args '{"provider":"codex"}'
```

Then point BOSS at it with `BOSS_CODE_AGENT_ENGINE=codex`, or
[one provider everywhere](#use-one-provider-everywhere). Verify:

```bash
boss-engine call detect_coding_agents --args '{}'
```

You want codex with `"ready": true`. BOSS stores no credential; the CLI keeps its own session.

If `next_step` says `sign in with: codex login`, the CLI is installed but signed out — or signed
in as a different OS user than BOSS runs as. If it says `installed: false`, you are on Path C.

### Claude Code

```bash
npm install -g @anthropic-ai/claude-code && claude auth login
```

`claude_code` is BOSS's default engine and uses the CLI's own session. Press **Connect** in the
console to record it, or check it from the shell:

```bash
boss-engine call detect_coding_agents --args '{}'
```

## 2C — Subscription, BOSS on a server

BOSS cannot see your CLI's login, so the credential has to travel.

| Subscription | What travels | Key |
|---|---|---|
| Codex | the whole `~/.codex/auth.json` | `CODEX_AUTH_JSON` |
| Claude Code | the token from `claude setup-token` | `CLAUDE_CODE_OAUTH_TOKEN` |
| Cursor | nothing — not supported here | — |

Do **not** set `CODEX_ACCESS_TOKEN`. Codex reads that variable itself and errors on it.

### Codex — sign BOSS in directly (recommended)

If you can reach the container, give BOSS **its own** Codex session rather than a copy of
yours. It prints a code to enter in a browser:

```bash
docker exec -it boss-community-api-1 codex login --device-auth
```

That session belongs to BOSS: your laptop can keep using Codex without disturbing it, there is
nothing to re-send, and it survives rebuilds because the credential lives in a shared volume.
Press **Connect** in the console afterwards.

> **Before you do this, make sure `CODEX_AUTH_JSON` is not set in your `.env`.** BOSS restores
> a configured credential over whatever the CLI holds, so a value left there by an earlier
> attempt will overwrite the session you just created, and every turn will fail `401
> invalidated oauth token` on a credential BOSS put there itself. Check with
> `grep -c '^CODEX_AUTH_JSON=' .env` — you want `0`.

### Codex — send the credential instead

For a machine where you cannot run an interactive login. Run this where you are signed in; it
reads the credential file and hands it to BOSS, which stores it wherever this installation
keeps secrets. Nothing is printed but the size.

```bash
python3 scripts/store_subscription_token.py codex --dry-run
python3 scripts/store_subscription_token.py codex
```

This route shares **one session** between two machines, and Codex rotates on use: each refresh
revokes the other copy. Expect to re-send whenever you use Codex locally. Prefer the device
login above wherever you can.

Then **Connect** in the console, or:

```bash
curl -X POST "${BOSS_API:?}/v1/setup/coding-agents/connect" \
  -H "x-boss-api-key: ${BOSS_API_KEY:?}" -H 'Content-Type: application/json' \
  -d '{"project_id":"boss-dogfood","actor":"ceo","provider":"codex"}'
```

Success runs a real Codex turn before saying yes:

```json
{
  "ok": true,
  "detected": { "authenticated": true, "auth_detail": "Logged in using ChatGPT" },
  "proof": { "ok": true, "replied": "BOSS_OK" }
}
```

If this installation has no secret store, BOSS stops and asks before putting a live credential
in its database — add `--accept-database-fallback` to say yes deliberately.

| Message | Fix |
|---|---|
| `no CODEX_AUTH_JSON available to this install` | It did not reach this BOSS. Check `BOSS_API` points at the same deployment. |
| `agent identity JWT payload is not valid JSON` | `CODEX_ACCESS_TOKEN` is set somewhere. Unset it. |
| `the stored credential file did not sign codex in` | Expired or invalidated. Run `codex login status` on your own machine to refresh it, then send it again. |
| A turn fails `401 … invalidated oauth token` | Either `CODEX_AUTH_JSON` is set in `.env` and is overwriting the session (check first — `grep -c '^CODEX_AUTH_JSON=' .env`), or you used Codex locally and rotated the token out from under BOSS's copy. |
| Connect succeeds, then fails again later | You are sharing one session with another machine. Use the device login instead. |
| `the codex credential is not valid JSON` | Your `~/.codex/auth.json` is unreadable. Sign in again. |

### Claude Code

On your own machine:

```bash
claude setup-token
```

That prints a long-lived token made for headless use. It is a separate credential from the
session you sign in with, so BOSS holding it does not disturb your own login.

**In the console:** Setup → Step two → Provider **Claude Code subscription** → paste it into
*Token from `claude setup-token`* → **Store and connect**. BOSS writes it to this install's
secret store, keeps a reference rather than the value, and proves it with one real turn. No
restart and nothing to put in a file.

**BOSS on a server — one command.** This is the whole thing; it stores the token on the server
and proves it with a real turn:

```bash
claude setup-token | ./scripts/install/remote.sh \
  --host root@YOUR.SERVER.IP --key ~/.ssh/id_ed25519 \
  subscription --provider claude_code
```

The token is read from the pipe, so it never reaches your shell history or the server's process
list. If that install has no secret store, BOSS stops rather than putting a live credential in
its database — add `--accept-database-fallback` to say yes deliberately.

**BOSS on this machine — from the shell:**

```bash
claude setup-token | python3 scripts/store_subscription_token.py claude_code --stdin

curl -sS -X POST "$BOSS_API/v1/setup/coding-agents/connect" \
  -H "x-boss-api-key: $BOSS_API_KEY" -H 'Content-Type: application/json' \
  -d '{"project_id":"boss-dogfood","actor":"ceo","provider":"claude_code"}'
```

`$BOSS_API` and `$BOSS_API_KEY` come from
[Setting `$BOSS_API` and `$BOSS_API_KEY`](#setting-boss_api-and-boss_api_key) — and for
a server they are **not** `127.0.0.1` unless you opened a tunnel. `remote.sh … env` prints the
right pair.

---

## Choose a model per role

Connect as many models as you like — each Connect records its own, and none replaces another.
Which one fills which role is then set per project in the console: **Setup → Step two → Choose
a model per role**. It takes effect on the next run. No restart, no `.env`.

| Role | What it does | Unset means |
|---|---|---|
| Implementer | writes the code | a connected harness BOSS picks, preferring one it has proven |
| Adversarial reviewer | reviews it and writes the line-by-line findings | the implementer reviews its own work |
| Hard problems | takes over when a task escalates | the implementer |
| Routine work | small, well-specified changes | the implementer |
| Console chat, agents and meetings | answers in the chat, runs the panel that writes your PRD, speaks in meetings | a connected subscription, or the API key this install has |

**The last row is not optional on an install with no API key.** The panel that produces the
PRD is a model call like any other: leave it unset with nothing else configured and the
meeting fails. It resolves to a connected subscription by itself — Claude first, because its
token is made for unattended use — so an install that connected one is already answered.

Worth making the reviewer a different model from the implementer: one that is the same model
rarely disagrees with itself.

**The options are the models you have connected**, not everything BOSS can drive — a picker
offering eleven providers on an install with one credential is a picker where ten of the
choices fail inside a build. Connect another model and it appears in every role immediately;
a model you chose and later disconnected stays listed, so the setting is never rewritten
behind you. A project that chooses nothing keeps using the deployment's defaults.

## Use one provider everywhere

```bash
BOSS_PROVIDER_LOCK=codex        # or openrouter, groq, deepseek, ollama, vllm, claude_code, cursor
```

Moves the coding pipeline, reviewers, agent chat, meetings and discovery in one setting. This
one is still environment-only and needs a restart, because it is applied when settings are
constructed rather than read per run — unlike the per-role choices above, which are database
values the console writes. Unset changes nothing. `cursor` moves the pipeline only — it has no
chat adapter.

Check what actually ran, from the ledger rather than the config:

```bash
boss-engine call provider_audit --args '{"expect":"openai","dimension":"vendor"}'
```

`vendor` = whose model. `transport` = whose endpoint. Claude via OpenRouter is vendor
`anthropic`, transport `openrouter`.

---

# Part 3 — Prove it works

Before you trust a fresh server, check these four. Each one is a failure we have actually had:

```bash
# 1. the delivery catalogue installed itself (12 workflows, seeded at boot)
curl -s "$BOSS_API/v1/delivery-workflows?view=installed&project_id=$PROJECT" \
  -H "Authorization: Bearer $BOSS_API_KEY" | head -c 200

# 2. a model answers the chat — not just builds
curl -sS -X POST "$BOSS_API/v1/engine/agent/chat" \
  -H "Authorization: Bearer $BOSS_API_KEY" -H 'Content-Type: application/json' \
  -d '{"project_id":"'"$PROJECT"'","actor":"executive_console","message":"Reply with OK."}'

# 3. the repository is connected and pushable
curl -s "$BOSS_API/v1/setup/status?project_id=$PROJECT" \
  -H "Authorization: Bearer $BOSS_API_KEY"

# 4. the coding agents are signed in where BOSS runs, not where you are
curl -s "$BOSS_API/v1/setup/coding-agents?project_id=$PROJECT" \
  -H "Authorization: Bearer $BOSS_API_KEY"
```

```bash
# 5. the machine is fit to run — no unresolved blockers, no recent host stalls
curl -s "$BOSS_API/v1/projects/$PROJECT/preflight" \
  -H "Authorization: Bearer $BOSS_API_KEY"

# 6. a preview link actually resolves from where the BROWSER is, not from the VM
#    (run this in the browser, not on the server — that is the whole point)
#    $BOSS_API/v1/previews/${PREVIEW_ID:?from the console}/web/  -> expect 200 and a page
```

Then press Build. If the panel stalls, the answer is in the run's step error rather than in a
container log — BOSS records the reason it stopped.

**Afterwards, ask the run how it went.** The retrospective is deterministic and costs nothing:

```bash
curl -s "$BOSS_API/v1/code-tasks/${RUN_ID:?the run id, from the console}/retrospective" \
  -H "Authorization: Bearer $BOSS_API_KEY"
```

Three separate grades — delivery, efficiency, host. They are separate on purpose: a slow run on
a thrashing machine is a `host` problem, and reading it as the pipeline degrading is a day
spent fixing something that was never broken.

## Extra checks for a server

Everything above works the same on a remote install, with three differences that are worth
knowing before you are looking at a failure.

**A subscription still travels, but not the same way for both.**

| | On a server |
|---|---|
| Claude Code | `claude setup-token` on your own machine, paste the value into Step two. It is a separate long-lived credential — your own login is untouched. |
| Codex | `docker exec -it <api-container> codex login --device-auth` and follow the URL. The container gets its own session, because one ChatGPT session cannot serve two machines. |

**Nothing needs to be in the environment.** Credentials go through the console into this
install's secret store, and every lane reads them from there — builds, the chat, and the panel.
An install that keeps `.env` empty is fully configured; setting `CLAUDE_CODE_OAUTH_TOKEN` or
`OPENAI_API_KEY` there still works and takes precedence, which is what you want while
debugging and not what you want as the permanent arrangement.

**The repository comes from the console, not from the environment.** `BOSS_TARGET_GITHUB_*` is
a fallback for a project with no repository connected. Connect one in Step one and it wins —
including on a project that used to have a different one, which is moved rather than refused.

**The console's API address is baked in at IMAGE BUILD TIME, and it must be the address the
BROWSER will use.** This is the one that bites hardest on a remote VM. `NEXT_PUBLIC_BOSS_API_URL`
is compiled into the web bundle, so a value of `127.0.0.1` means the browser tries to reach the
API on the machine it is running on — yours, not the server. Every preview link then points at
nothing, and a preview that is running perfectly looks broken.

```bash
# on the VM, before building web
NEXT_PUBLIC_BOSS_API_URL=https://boss.example.internal:8000   # what YOUR BROWSER will type
docker compose -f scripts/install/community-compose.yml --profile web build web
```

It defaults to `http://127.0.0.1:${BOSS_API_PORT}`, which is right for a laptop and wrong for
every server. Changing `BOSS_API_PORT` alone is not enough: rebuild `web`, or the old address
stays compiled in.

**Size the box so it never swaps.** A host that swaps stops scheduling BOSS for minutes at a
time, and the symptom is an 85-minute build that should have taken 20, with nothing in any log
saying why. 8 GB RAM minimum and 100 GB disk — see the installer README for the measured
figures.

---

# Reference

## Reference — where secrets live

Part 1 gives you Infisical. BOSS otherwise uses the best store the installation has, trying
`infisical → platform_secret_store → vault → local_vault → database` and taking the first that
answers.

To use an Infisical you already run instead of the bundled one, set `BOSS_INFISICAL_URL`,
`BOSS_INFISICAL_PROJECT_ID`, and either `BOSS_INFISICAL_TOKEN` or `BOSS_INFISICAL_CLIENT_ID`
with `BOSS_INFISICAL_CLIENT_SECRET`.

Any service speaking four verbs works in place of it, authenticated with an `X-Ops-Token`
header: `GET /secrets` · `GET /secret?key=` · `POST /secret` · `DELETE /secret?key=`. Values
must be single-line.

## Reference — other lanes

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

## Known limits

- **One Codex session cannot serve two machines.** Codex rotates its credential on use and each
  refresh revokes the previous one, so a copied session dies the moment the other machine runs
  anything. Measured: a token valid for another ten days was refused within three minutes.
  Signing BOSS in directly with `--device-auth` avoids this entirely — it gets its own session
  and its own refresh chain. Copying is the fallback for machines that cannot do an interactive
  login, and there you should expect to re-send.
- **A credential set in the environment wins over everything.** That is deliberate, so an
  operator can override while working something out — but a value left in `.env` by an earlier
  attempt will silently overwrite a working session, including one you just created. BOSS now
  refuses to replace a newer credential with an older one, and says so in its logs; a value with
  no timestamp is still applied as before.
- A Claude token expires too, but is not invalidated this way — mint a new one when it lapses.
- Claude Code is not covered by `detect_coding_agents`; use `list_harnesses` to confirm it.
- Cursor works only where its own login lives, and no governed task has completed on it yet.
- `list_harnesses` shows `verified: false` until a full governed task has completed on a
  harness. Wired is not proven.
