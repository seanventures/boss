# 15 — OSS Engine Docker Verification Report (2026-07-28)

**Plane:** GTM & Marketing #294 (verification) · follow-up gap issues filed in the same module
**Scope:** standalone `engine/Dockerfile` image + fresh postgres:16, as a first-time OSS user would run it. No repo files modified.

## Verdict

The engine core is **real and works end-to-end** — build → schema bootstrap → CLI tools → tool-grounded agent answer → MCP stdio — but **three gaps block a clean public release**: the image can't self-bootstrap a fresh DB, `serve-mcp --http` is broken by an annotation bug, and the zero-Postgres quickstart is aspirational.

## What worked

1. **Image build:** `docker build -t boss-engine:oss .` from `engine/` — 27.4 s, 446 MB.
2. **Postgres:** postgres:16 container (db/user/pass `boss`) on a dedicated network.
3. **Schema bootstrap:** engine-native `python -m boss_engine.bootstrap` exists (full `ensure_schema` + org/workspace/project + budget/specpack/MCP/A2A seeding) and completes — but only with host mounts (GAP 1).
4. **CLI vs live DB:** `tools` (15 tools: 10 read/5 write), `status` (`boss-dogfood`, ok:true), `call query_db` (SELECT works; `DELETE` correctly rejected — read-only guard holds), `call list_code_tasks`.
5. **MCP stdio (default transport):** `initialize` → `boss-engine 0.9.1`, protocol 2024-11-05; `tools/list` → 15; `tools/call project_status` → correct payload, `isError:false`.
6. **Agent chat E2E:** one live `ask "how many code tasks exist?"` → correct tool-grounded answer against the fresh DB. Note: repo `.env` has no ANTHROPIC/OPENAI key (`BOSS_LLM_ENGINE=claude_cli`, unusable in-container); the Azure OpenAI credential set was used via the adapter's `auto` mode. The plain-`OPENAI_API_KEY` path is expected to work but was not exercised.

## Release-blocking gaps

### GAP 1 — standalone image cannot bootstrap a fresh DB
`Settings.repo_root = Path(__file__).resolve().parents[2]` (`engine/boss_engine/config.py:839`) resolves to `/` inside the image; bootstrap dies on `FileNotFoundError: '/db/schema.sql'`, then `'/docs/product/TRACEABILITY.md'`. The Dockerfile copies only `boss_engine/`. Verified minimal workaround: mount monorepo `db/`, `packages/`, `docs/` read-only. **Fix:** COPY (or vendor as package resources) `db/schema.sql`, `packages/specpack`, `docs/product/TRACEABILITY.md` into the image; document bootstrap in `engine/README.md` (currently unmentioned — a new user hits `relation "code_task_runs" does not exist`).

### GAP 2 — `serve-mcp --http` broken (never live-tested)
(a) Image lacks the `[mcp-http]` extra → `ModuleNotFoundError: uvicorn` (documented, minor). (b) Real bug: `engine/boss_engine/mcp/server.py` has `from __future__ import annotations` at module top while `Request` is lazily imported inside `asgi_app()` (~line 134); FastAPI can't resolve the string annotation, treats `request` as a query param, and **every JSON-RPC POST returns 422**. With a wrapper that rebuilds the identical app with a resolvable annotation, HTTP transport works perfectly (initialize / tools/list / tools/call all correct). **Fix:** import `Request` at module scope (or drop future-annotations in that file) + ship the http extra in the image.

### GAP 3 — zero-Postgres quickstart is aspirational (confirms gap issue #301)
`InMemoryPersistence` / `EnginePorts.oss_defaults()` exist (`ports/composition.py`, `defaults/persistence.py`) but nothing in the CLI/`EngineContext` path calls `configure_engine_ports()` — `oss_defaults` is only a fallback inside `software_factory.py:54`. Read tools issue raw SQL at `DATABASE_URL`; SQLite connects but fails (`schema.sql` is Postgres-only: jsonb). Only `tools` works DB-less. **Fix:** wire `oss_defaults` into the CLI, or remove the zero-Postgres claim from spec 01.

**Minor:** no `docker-compose.yaml` under `engine/` (root compose is the enterprise stack); `engine/README.md` quickstart assumes the monorepo venv, not the container.

## Verified new-user command sequence (feeds YouTube Ep1 / quickstart docs)

```bash
cd boss/engine
docker build -t boss-engine:oss .                       # ~30 s, 446 MB
docker network create boss-oss
docker run -d --name boss-oss-db --network boss-oss -p 55432:5432 \
  -e POSTGRES_DB=boss -e POSTGRES_USER=boss -e POSTGRES_PASSWORD=boss postgres:16
export DB='postgresql+psycopg://boss:boss@boss-oss-db:5432/boss'
# bootstrap (mounts required until GAP 1 is fixed; run from monorepo root)
cd .. && docker run --rm --network boss-oss -e DATABASE_URL=$DB \
  -v $PWD/db:/db:ro -v $PWD/packages:/packages:ro -v $PWD/docs:/docs:ro \
  --entrypoint python boss-engine:oss -m boss_engine.bootstrap
# use it
docker run --rm boss-engine:oss tools
docker run --rm --network boss-oss -e DATABASE_URL=$DB boss-engine:oss status
docker run -i --rm --network boss-oss -e DATABASE_URL=$DB boss-engine:oss serve-mcp   # stdio MCP
docker run --rm --network boss-oss -e DATABASE_URL=$DB -e OPENAI_API_KEY=sk-... \
  boss-engine:oss ask "how many code tasks exist?"
```

**Definition of done for the public quickstart:** the sequence above with the `cd .. && docker run … bootstrap` step reduced to `docker run --rm … boss-engine:oss bootstrap` (no mounts), plus a working `serve-mcp --http`.
