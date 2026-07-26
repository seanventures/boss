# BOSS Engine

The **OSS-shippable core** of the BOSS autonomous software factory. This package
is the workflow engine — it orchestrates coding tasks, discovery runs, PRD
meetings, previews, sandboxes/microVMs and the governance gates that turn a
request into shipped software. It has **no dependency on the HTTP API or the web
console**; those live in `../apps/` and are the internal/enterprise layer that
sits on top of this engine.

This directory is being extracted so the engine can ship on its own (and, later,
move to its own OSS repo). The API + console stay behind as the commercial layer.

## What you get

```
boss_engine/
  context.py     EngineContext — wires Settings + DB + services, no FastAPI
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

## Status: Phase 0

The engine currently **imports the service modules in place** from
`apps/api/app/services` (via `boss_engine/_compat.py`), so it is runnable and
testable with zero file moves. The staged plan:

| Phase | What |
|-------|------|
| 0 ✅ | Tool layer + engine-native agent + MCP + CLI (new files; import in place) |
| 1 | Broaden read/write tool coverage |
| 2 | Physically relocate the pure service modules under `boss_engine/`; repoint API + worker; leave re-export shims |
| 3 | Standalone packaging + Dockerfile; `pip install boss-engine` and run with no API/console |
| 4 | Lift `engine/` into its own OSS repo |

See `BUILD_NOTES.md` for the running log.
