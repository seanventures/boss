# BOSS Engine

The **OSS-shippable core** of the BOSS autonomous software factory. This package
is the workflow engine — it orchestrates coding tasks, discovery runs, PRD
meetings, previews, sandboxes/microVMs and the governance gates that turn a
request into shipped software. It has **no dependency on the HTTP API or the web
console**; those live in `../apps/` and are the internal/enterprise layer that
sits on top of this engine.

The engine now ships on its own as a Python package and container. The API and
console remain separate consumers of the same engine core.

## What you get

```
boss_engine/
  context.py     EngineContext — wires Settings + DB + services, no FastAPI
  services/      the complete workflow and policy service graph
  tools/         the tool layer: every engine helper as a schema-typed tool
  agent/         EngineAgent — the engine-native BOSS Agent (tool-calling loop)
  mcp/           MCP server exposing the same tools to GitHub/Claude/etc.
  __main__.py    `python -m boss_engine` — run the engine standalone
```

### Three ways to drive the engine — all in-process, never over HTTP

1. **Tool layer** (`boss_engine.tools`) — the single source of truth. Read tools
   give an overall view (what's running, previews, discovery, meetings, the
   microVM fleet, governed Postgres reads); write tools create workflow tasks.
2. **Engine-native agent** (`boss_engine.agent`) — a batteries-included
   BOSS Agent that reasons over the tool layer. Ships with the OSS engine.
3. **MCP server** (`boss_engine.mcp`) — project-scoped tools over MCP JSON-RPC, so an
   external agent (GitHub Copilot, Claude, Cursor) can be "the BOSS Agent".

Private design-partner week-1 (monorepo + Mission Control): see
[`docs/operations/DESIGN_PARTNER_ONBOARDING.md`](../docs/operations/DESIGN_PARTNER_ONBOARDING.md)
or run `make onboard` from the repository root. Engine-only Docker/MCP steps
are in [`DESIGN_PARTNER_ONBOARDING.md`](./DESIGN_PARTNER_ONBOARDING.md). This
package is not a public-release announcement.

## Quickstart

```bash
# from the monorepo root, using the app venv + a reachable Postgres
export DATABASE_URL='postgresql+psycopg://boss:boss@localhost:5432/boss'
export PYTHONPATH=engine

python -m boss_engine bootstrap                   # FIRST RUN: schema + seed defaults
python -m boss_engine tools                       # list the tool layer
python -m boss_engine --project boss-dogfood status
python -m boss_engine call list_code_tasks --args '{"limit":5}'
python -m boss_engine ask "how many tasks are running right now?"
python -m boss_engine chat --allow-writes          # interactive agent
python -m boss_engine serve-mcp                     # MCP over stdio (default; writes OK)
python -m boss_engine serve-mcp --http --port 8791  # HTTP + Mission Control (read-only)
# To enable authenticated HTTP writes for a local MCP client:
# export BOSS_MCP_HTTP_TOKEN="$(openssl rand -hex 32)"
# python -m boss_engine serve-mcp --http --allow-writes --host 127.0.0.1
```

Raw control-plane SQL is held out of chat, workflow agents, investigations, and
MCP because those sessions are pinned to a project and the SQL connection has
no database-enforced project boundary. In paid editions, a local administrator
can invoke `call query_db` directly; `call write_db` also needs `--confirm`.
Community uses the project-scoped read and operation tools.

A fresh database MUST be bootstrapped first — `bootstrap` creates the schema
(`db/schema.sql`) and seeds the default org/project, budget policy, the
`core_v1` role pack and the default agent modules. Without it every DB-backed
tool fails with `relation "code_task_runs" does not exist`.

### Durable-workspace requirement

The community engine has no hosted dependency: a self-hosted PostgreSQL
database is its durable workspace. It is required before BOSS can create a
task, Receipt, approval, lease, meeting, or other governed workflow record.
Without `DATABASE_URL`, `boss-engine tools` remains useful for inspecting the
catalog, and the MCP endpoint is read-only, but write tools fail closed with
setup instructions. In-memory ports are an internal composition seam for
isolated pipeline tests; they are not a substitute for the durable audit trail
required by a running BOSS installation.

### Docker

```bash
# Build from the REPO ROOT (the image vendors db/schema.sql + the spec pack):
docker build -f engine/Dockerfile -t boss-engine .

docker network create boss && docker run -d --name boss-db --network boss \
  -e POSTGRES_DB=boss -e POSTGRES_USER=boss -e POSTGRES_PASSWORD=boss postgres:16
export DB='postgresql+psycopg://boss:boss@boss-db:5432/boss'

docker run --rm --network boss -e DATABASE_URL=$DB boss-engine bootstrap
docker run --rm --network boss -e DATABASE_URL=$DB boss-engine status
docker run -i  --rm --network boss -e DATABASE_URL=$DB boss-engine serve-mcp   # stdio
export BOSS_MCP_HTTP_TOKEN="$(openssl rand -hex 32)"
docker run -d --name boss-mcp --network boss -p 127.0.0.1:8791:8791 \
  -e DATABASE_URL=$DB -e BOSS_MCP_HTTP_TOKEN \
  boss-engine serve-mcp --http --host 0.0.0.0                                  # HTTP (read-only)
```

### Mission Control (community, read-only)

`serve-mcp --http` also serves a zero-dependency, read-only **Mission Control**
page at `http://host:8791/` — project status, running work, code tasks,
previews, discovery and meetings, rendered straight from the engine's read
tools over the same `/mcp` JSON-RPC endpoint. HTTP mode defaults to
**read-only** (loopback needs no transport auth; non-loopback requires a token);
write tools stay on stdio MCP or require
`--allow-writes` on loopback with `BOSS_MCP_HTTP_TOKEN`. Authenticated HTTP
clients send `Authorization: Bearer <token>`; remote clients can use an SSH
tunnel. The Mission Control page prompts for that token in write-capable mode
and keeps it in browser memory for the current page session. It is a snapshot
viewer by design; approvals remain explicit operator actions.

MCP exposes project-scoped engine tools, not every control
plane HTTP route. See [Community MCP guide](../docs/community/MCP.md) for the
capability map and security boundary.

### Local / open models (Ollama, vLLM, any OpenAI-compatible endpoint)

The chat lane honors an OpenAI-compatible base URL, and the code-agent lane has
first-class local providers:

```bash
# Ollama on the same box (zero config beyond the model name):
export BOSS_LLM_ENGINE=openai OPENAI_API_KEY=ollama \
       OPENAI_BASE_URL=http://localhost:11434/v1 OPENAI_MODEL=qwen2.5-coder:14b
python -m boss_engine ask "what's running?"

# Code tasks on local lanes: BOSS_CODE_AGENT_ENGINE=ollama (daemon default
# http://localhost:11434/v1, override OLLAMA_BASE_URL) or =vllm (set
# VLLM_BASE_URL, e.g. http://gpubox:8000/v1).
# Local inference is $0 — zero-price your models for honest cost accounting:
export BOSS_MODEL_PRICES_JSON='{"qwen2.5-coder:14b":[0,0]}'
```

### Standalone installs

`pip install boss-engine` outside the monorepo: point the engine at a spec pack
with `BOSS_SPECPACK_ROOT=/path/to/specpack` (roles, skills, packs, policies) —
otherwise it is resolved from the monorepo layout relative to the package.

### Private design partners and public release

Before the public release, use the
[design-partner onboarding pack](./DESIGN_PARTNER_ONBOARDING.md) to establish a
safe local/MCP proof, the canonical sample-repository boundary, and
evidence-backed feedback. It deliberately does not imply that the engine is
publicly released or that hosted services are configured. The
[community readiness record](./COMMUNITY_READINESS.md) tracks the legal,
security, extraction, and approval gates required before a public flip.

## Status: Phase 3 release-ready

The service graph is physically owned by `boss_engine/services`. The API and
worker use compatibility shims that preserve module identity while allowing a
gradual import cleanup. The package and standalone container do not require the
API or console source trees.

| Phase | What |
|-------|------|
| 0 ✅ | Tool layer + engine-native agent + MCP + CLI |
| 1 ✅ | Broaden read/write tool coverage and live read-only verification |
| 2 ✅ | Relocate pure services under `boss_engine/`; preserve API/worker compatibility shims |
| 3 ✅ | Standalone package + Dockerfile; install and run with no API/console |
| 4 | Lift `engine/` into its own OSS repo |

See `BUILD_NOTES.md` for the running log.
