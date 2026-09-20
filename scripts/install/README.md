# BOSS Community installer

Idiot-proof, one-line Community edition setup — OpenClaw style.

## One-liner

```bash
curl -fsSL https://raw.githubusercontent.com/seanheiney/boss/main/install.sh | bash
```

With a model key already in the environment:

```bash
curl -fsSL https://raw.githubusercontent.com/seanheiney/boss/main/install.sh | bash -s -- \
  --openai-key "$OPENAI_API_KEY"
```

Non-interactive (CI / automation):

```bash
curl -fsSL https://raw.githubusercontent.com/seanheiney/boss/main/install.sh | bash -s -- \
  -y --openai-key "$OPENAI_API_KEY" --dir ~/boss
```

From a local checkout:

```bash
./install.sh --dir "$(pwd)" --openai-key "$OPENAI_API_KEY"
```

## What it does

1. Checks OS tools (`git`, `docker`, Compose, disk, ports)  
2. Prompts for install directory and **BYOK** model key (optional skip)  
3. Clones/updates the BOSS repo  
4. Writes a private `.env` (API key generated, mode 600)  
5. `docker compose` builds and starts **Postgres + API + worker** (+ Mission Control if selected)  
6. Waits for `/health`, runs DB seed  
7. Verifies Community edition + ProviderOps surface  

No BOSS Cloud / Account Connect required.

## Files

| Path | Role |
|---|---|
| `install.sh` | One-line entry (repo root) |
| `scripts/install/community-compose.yml` | Self-contained Community stack |
| `.env` (generated) | Secrets + ports — never commit |

## Requirements

- Docker Desktop (macOS/Windows) or Docker Engine + Compose (Linux)
- `git`, `curl`

### Sizing — measured, not estimated

These are real figures from a working install, not guesses. The previous "~5GB free disk" was
wrong by an order of magnitude and would strand a build half-way.

| | Need | Why |
|---|---|---|
| **Disk** | **100 GB** | Images are ~11.5 GB (api 4.1, infisical 3.8, web 1.6, worker 1.5, postgres 0.4). Building them generates tens of GB of layer cache — a real build reached 58 GB before pruning. Every agent run also clones the target repo and may `npm install` Playwright browsers. |
| **RAM** | **8 GB minimum, 16 GB comfortable** | The six containers idle at ~1.6 GB. The coding agent runs INSIDE the api container, so a build adds a Node process plus whatever the task installs. A 7.6 GB allocation ran a 39-file build successfully; less than 8 GB will swap. |
| **CPU** | 4 cores | Reviewers run in parallel. |

**Do not let it swap.** A host that starts swapping stops scheduling BOSS for minutes at a
time, and the symptom is a build that takes 85 minutes instead of 20 with nothing in any log
to explain it. BOSS now detects this itself (`host.stalled` events, and the per-run
retrospective attributes the lost time), but the cure is capacity, not detection.

### Ports

Everything derives from these; set them once in `.env`:

| Variable | Default | Notes |
|---|---|---|
| `BOSS_API_PORT` | 8000 | The console's preview links are built from this **at image build time**. Change it and rebuild `web`, or every preview link points at a port with nothing behind it. |
| `BOSS_WEB_PORT` | 3000 | Mission Control. |
| `BOSS_POSTGRES_PORT` | 5432 | |
| `INFISICAL_PORT` | 8080 | |

`NEXT_PUBLIC_BOSS_API_URL` derives from `BOSS_API_PORT` automatically. Set it explicitly only
when the API is reached at a different hostname than the browser uses — a remote VM, for
instance, where it must be the address **the browser** will use, not `127.0.0.1`.

### Installing on a server (not a laptop)

One extra flag, and it is the one that decides whether the console works:

```bash
curl -fsSL https://raw.githubusercontent.com/seanheiney/boss/main/install.sh | bash -s -- \
  --public-url boss.example.internal      # the address YOUR BROWSER will use
```

`NEXT_PUBLIC_BOSS_API_URL` is compiled into the web bundle at image build time. Without this
flag it is `127.0.0.1`, which is right on a laptop and wrong on every server: the browser then
tries to reach the API on the machine it is running on — yours — and every preview link points
at nothing while the previews themselves are fine. A bare host, `host:port`, or a full URL are
all accepted.

### Did it work?

```bash
./scripts/install/doctor.sh
```

Seven checks, each one a failure that has actually happened here: containers running, the API
answering, the address compiled into the console, a model connected, **a model that actually
answers**, the repository pushable, and whether this machine can schedule BOSS at all. Exits 0
when BOSS can do its job.

```
  ok    containers: 6 running
  ok    api: /health answers on http://127.0.0.1:8000
  ok    console: http://boss.example.internal:8000
  ok    models connected: claude_code
  ok    chat: a model answered
  ok    repository: connected
  ok    host: scheduling BOSS normally

  BOSS is ready.  Mission Control: http://127.0.0.1:3000
```

"Connected" and "working" are different claims, and the chat check is the one that separates
them: a subscription can be connected and out of quota, and only asking it a question finds out.

### Before the first run

```bash
# Is the machine fit to start an expensive run?
curl -s "$BOSS_API_URL/v1/projects/$BOSS_DEFAULT_PROJECT_ID/preflight"
```

`{"ready": true}` means no unresolved blockers from the last run and no recent host stalls.
Warnings name what to fix first — running anyway wastes real money reaching a known answer.

### When a run is slower than expected

```bash
curl -s "$BOSS_API_URL/v1/code-tasks/<run_id>/retrospective"
```

Returns three separate grades — delivery, efficiency, host — deterministically, with no model
call. If `host` is `degraded` or `critical`, the time was the machine's and re-running will not
improve it.

## After install

```bash
source ~/boss/boss-community.env.sh
curl -sS -H "X-API-Key: $BOSS_API_KEY" "$BOSS_API_URL/health"
# Mission Control: http://127.0.0.1:3000
```

Stop:

```bash
cd ~/boss
docker compose -f scripts/install/community-compose.yml --env-file .env down
```

## Flags

See `./install.sh --help`.
