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
3. **MCP server** (`boss_engine.mcp`) — the same tools over MCP JSON-RPC, so an
   external agent (GitHub Copilot, Claude, Cursor) can be "the BOSS Agent".

## Quickstart

```bash
# from the monorepo root, using the app venv + a reachable Postgres
export DATABASE_URL='postgresql+psycopg://boss:boss@localhost:5432/boss'
export PYTHONPATH=engine

python -m boss_engine tools                       # list the tool layer
python -m boss_engine status --project boss-dogfood
python -m boss_engine call list_code_tasks --args '{"limit":5}'
python -m boss_engine ask "how many tasks are running right now?"
python -m boss_engine chat --allow-writes          # interactive agent
python -m boss_engine serve-mcp                     # MCP over stdio (default)
python -m boss_engine serve-mcp --http --port 8791  # MCP over HTTP (needs [mcp-http])
```

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
