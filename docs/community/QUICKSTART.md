# BOSS Community — quickstart

Goal: a working self-hosted control plane with BYO keys on a VM or developer machine.

> Invited onto the **private** repo as a design partner? Start at
> [`docs/operations/DESIGN_PARTNER_ONBOARDING.md`](../operations/DESIGN_PARTNER_ONBOARDING.md)
> (`make onboard`). Access is a human GitHub invite; this quickstart is not a
> public-clone substitute.  
> For production goldeneye-style deploy, see `docs/operations/GOLDENEYE_DEPLOYMENT.md`.  
> For the full product contract, see [BOSS_COMMUNITY_EDITION_SPEC.md](../product/BOSS_COMMUNITY_EDITION_SPEC.md).

## One-line install (recommended)

The planned public one-liner will install missing VM dependencies, start the Community
stack, prompt for keys, seed a project, and verify the API:

```bash
curl -fsSL https://raw.githubusercontent.com/seanventures/boss/main/install.sh | bash
```

**Public launch pending:** `seanventures/boss` is not published yet, so the raw URL
above is a preview of the command, not a working public download. Invited contributors
can install from their authorized checkout:

```bash
cd /path/to/authorized/boss
./install.sh --dir "$PWD"
```

Supported fresh VMs: Ubuntu 22.04/24.04 and Debian 12/13 with `sudo` or root.
The installer provisions `git`, `curl`, OpenSSL, Python 3, Docker Engine,
Buildx, and Compose as needed.
It binds the API and console to loopback. To use the console from your laptop:

```bash
ssh -L 3000:127.0.0.1:3000 -L 8000:127.0.0.1:8000 user@your-vm
```

Open `http://127.0.0.1:3000` locally. For a permanent public hostname, use a TLS
reverse proxy, pass `--public-url https://boss.example.com` to the installer,
and set `BOSS_CONSOLE_PUBLIC_ORIGIN=https://boss.example.com` in `.env`. The proxy
must overwrite the forwarded host and scheme headers; see the installer guide.
Details and flags: [installer guide](../../scripts/install/README.md) · [install.sh](../../install.sh).
After installation, update with `./scripts/install/upgrade.sh --dir "$PWD"` from
the checkout. It makes a private database, artifact, and configuration backup
before rerunning the installer. To stop BOSS while keeping its data and keys,
run `./scripts/install/uninstall.sh --dir "$PWD" --yes`; rerun the installer to
resume. To remove data, use `--purge-data`, which backs up first and requires
a separate typed confirmation. See [upgrade and recovery](../../scripts/install/README.md#upgrade--recover).
Mission Control asks for its own password. On the VM, run
`python3 scripts/install/console_password.py show` from the install directory
to display it privately; `reset` rotates it and prints the web restart command.

To add the official Claude Code, Codex CLI, and Grok Build binaries on the VM,
pass `--install-coding-clis`. BOSS then reports local login status without
transferring credentials. Authenticate with each provider on the VM; a laptop
subscription session does not automatically follow an SSH connection. Explicit
`--connect-claude` and `--connect-codex` import the supported CLI credential
through BOSS and prove it works. Grok requires `grok login --device-auth` as
the `boss` user inside the API container; its login volume is shared with the
worker. See the
[coding subscriptions guide](../../scripts/install/README.md#optional-coding-subscriptions).

## What you will run

| Process | Role |
|---|---|
| Postgres | Durable state |
| `apps/api` | Control plane |
| `apps/worker` | Leased job execution |
| `apps/web` (optional) | Mission Control |
| CLI / MCP client | Operator surfaces |

## 1. Prerequisites

- Docker (or local Postgres 16+)  
- Python 3.11+ for engine/API (or use compose images)  
- A model API key **you** control (OpenAI-compatible, Anthropic, etc.)  

## 2. Configure

```bash
export BOSS_EDITION=community
export DATABASE_URL=postgresql+psycopg://boss:boss@127.0.0.1:5432/boss
# Bring your own key — never commit it
export OPENAI_API_KEY=sk-...
# Optional: local OpenAI-compatible endpoint
# export OPENAI_BASE_URL=http://127.0.0.1:11434/v1
```

Leave `BOSS_ACCOUNT_CONNECT_URL` **unset** for pure Community.

## 3. Start services

From the repository root (see `Makefile` / `docker-compose.yaml` for the current recommended path):

```bash
make dev          # when available in your checkout
# or: docker compose up -d
# or: run API + worker processes per docs/operations
```

Health check:

```bash
curl -sS "$BOSS_API_URL/health"
```

## 4. Seed a project

```bash
# Example — use the project’s seed path for your version
make seed
# Project id is often boss-dogfood in dogfood installs
```

## 5. Drive the loop

**CLI / MCP:** submit an outcome (command) and watch tasks, jobs, and receipts.

**Mission Control:** open the web console → Command Center → Approvals → Task Board.

**Prove it:** every external effect should appear as a receipt; budget pressure should show on the Budget surface.

## 6. Optional: Software Factory

1. Connect GitHub (project GitHub / easy-connect in Mission Control).  
2. Submit a code-oriented outcome.  
3. Require tests/evidence before treating the task as complete.  

## 7. Optional: ProviderOps

1. Add provider keys as SecretRefs (never plain agent-visible secrets).  
2. Sync inventory.  
3. Plan ChangeSets — approve before apply.  

## 8. Optional: Easy Connect to BOSS Cloud

Only when you want hosted team / marketplace / managed capacity:

```http
POST /v1/cloud/easy-connect
{ "project_id": "...", "method": "device", "actor": "ceo" }
```

Or use **Editions → Connect with device code** in Mission Control.  
Community remains fully usable if Cloud is not configured.

## Verify independence

| Check | Expected |
|---|---|
| Account Connect URL unset | Community work still succeeds |
| `GET /v1/edition` | `edition_id=community`, `account_connect_required=false` |
| `GET /v1/cloud/status` | `independent=true` when cloud APIs present |
| Missing model key | Fail closed with visible decision/event — not a mock success |

## Next reading

- [FEATURES.md](./FEATURES.md)  
- [BOSS_COMMUNITY_EDITION_SPEC.md](../product/BOSS_COMMUNITY_EDITION_SPEC.md)  
- `docs/product/PRD-v2.0.md`  
- https://boss.sean.network/community/  
