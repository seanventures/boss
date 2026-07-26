# Engine-split build notes

Running log of the OSS engine-split work on branch `feat/oss-engine-split`
(isolated worktree; `main` stays hotfix-able). Autonomous build.

## Decisions locked
- **Agent backend:** Engine-native + MCP (user pick). Ship a self-contained agent
  inside the OSS engine AND expose the same tools over MCP so GitHub/Claude
  agents can drive the engine.
- **Sequencing:** new files first (zero merge conflict with main hotfixes), the
  disruptive 127-file physical move LAST (Phase 2).
- **Engine has no web-framework dependency.** MCP protocol logic is pure; stdio is
  the default transport; FastAPI is an optional `[mcp-http]` extra.

## Phase 0 — DONE ✅ (this commit)
Tool layer + engine-native agent + MCP + standalone CLI. All new files under
`engine/`; they import today's services in place via `boss_engine/_compat.py`
(no service files moved yet).

- `boss_engine/_compat.py` — locates the monorepo root; single switch point for
  the Phase-2 move (`SERVICES_PKG`).
- `boss_engine/context.py` — `EngineContext`: wires Settings + DB engine +
  service singletons exactly as `apps/api/app/main.py` did, no FastAPI.
- `boss_engine/tools/` — the tool layer. 14 tools (10 read, 4 write):
  - read: project_status, list_code_tasks, get_code_task, list_running_work,
    list_discovery, list_meetings, list_previews, get_task_preview,
    microvm_status, query_db (read-only, guarded)
  - write: create_code_task, create_discovery, request_meeting, start_preview
- `boss_engine/agent/` — `EngineAgent`, a tool-calling loop; write tools are
  confirmation-gated unless `allow_writes=True`.
- `boss_engine/mcp/` — `MCPServer`; JSON-RPC (initialize/tools/list/tools/call);
  stdio transport (stdlib) + optional HTTP transport (lazy FastAPI); `read_only`
  mode hides write tools.
- `boss_engine/__main__.py` — `python -m boss_engine {tools,status,call,ask,chat,serve-mcp}`.
- `pyproject.toml`, `README.md`, `tests/`.

### Verified
- 17/17 unit tests pass (registry, MCP protocol, agent write-gating) — no DB.
- Live read-only run against local Postgres (infra-db-1:5432):
  `EngineContext.create()` OK, DB reachable; project_status / list_code_tasks /
  list_running_work / query_db / microvm_status return real data.
- `query_db` rejects non-SELECT; `microvm_status` degrades past local schema drift.

### Known local-only quirks (not bugs)
- Local Postgres is on an OLDER schema (no `app_previews.sandbox_task_id`), so the
  existing `preview_service.list_previews` SQL errors locally. The deployed DB
  self-migrates and has the column. `microvm_status` was made resilient to it.

## Next
- Phase 1 — broaden tool coverage (retry/deploy task, plan propose/dispatch,
  budget/entitlements, discovery/meeting detail); wire the engine-native agent's
  create_code_task to the build-on-existing logic in `intent_router`.
- Phase 2 — relocate the ~103 pure service modules under `boss_engine/`; repoint
  API + worker; leave re-export shims. Flip `_compat.SERVICES_PKG`.
- Phase 3 — standalone packaging + engine Dockerfile; run with no API/console.
