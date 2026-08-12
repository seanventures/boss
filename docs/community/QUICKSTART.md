# BOSS Community — quickstart

Goal: a working self-hosted control plane with BYO keys in minutes on a developer machine.

> For production goldeneye-style deploy, see `docs/operations/GOLDENEYE_DEPLOYMENT.md`.  
> For the full product contract, see [BOSS_COMMUNITY_EDITION_SPEC.md](../product/BOSS_COMMUNITY_EDITION_SPEC.md).

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
