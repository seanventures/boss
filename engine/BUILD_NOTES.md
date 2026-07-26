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

## Phase 0 — DONE ✅
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

## Phase 1 — DONE ✅
- `create_code_task` now EXTENDS a task's existing app branch (`boss/code/{root}`)
  when the task already has a completed PR — build-on-existing, not rebuild.
- Added `retry_code_task`. 15 tools (10 read, 5 write).
- **LIVE agent verified**: `EngineAgent` answered "how many tasks running + recent
  builds" by calling `list_running_work` + `list_code_tasks` against the real
  engine/DB via the Claude CLI adapter — no API, no console.

## Phase 2 — DONE ✅
Physically relocated the engine into `boss_engine/`: all service modules +
`config/db/schemas/bootstrap`. **Pure git mv (zero content edits)** — 126 renames;
main hotfixes editing a service merge onto the renamed file via git rename
detection with no conflict. The internal API + worker keep working UNCHANGED via
lazy compatibility shims:
- `apps/api/app/{config,db,schemas,bootstrap}.py` → alias `boss_engine.<mod>`.
- `apps/api/app/services/__init__.py` → lazy meta-path finder aliasing every
  `apps.api.app.services.X` onto the SAME object as `boss_engine.services.X`
  (main.py's 90 inline imports untouched; single module identity).
- `_compat` flipped to `boss_engine.*` — the engine no longer needs the monorepo.

Verified (via `engine/scripts/verify_engine.py --with-api 397 --live`):
engine imports standalone (no apps/); apps.api.app.main == 397 routes (baseline);
`apps.api.app.services.X is boss_engine.services.X`; 17/17 tests; live read ok.

## Phase 3 — DONE ✅ (standalone image builds + runs live)
- `engine/requirements.txt` (api deps minus fastapi/uvicorn/dev), `engine/Dockerfile`
  (standalone image, copies only `boss_engine/` — no apps/), `engine/scripts/verify_engine.py`.
- **Image builds (441MB) and runs standalone**: `docker run boss-engine:dev tools`
  lists all 15 tools; `docker run -e DATABASE_URL=… boss-engine:dev status` returned
  live `project_status` from Postgres — NO API, NO console in the image. Req #1 proven.
- The release gate builds and exercises the standalone package and container
  locally. Registry publication and target-environment deployment remain
  separately governed provider effects.

## Not done / next candidates
- Repoint API + worker to import `boss_engine.*` directly and delete the shims
  (optional; shims are permanent-safe, so this is cleanliness not necessity).
- Wire the console to the engine-native agent via a thin `/v1/engine/agent/chat`.
- Broaden tools: deploy status, plan propose/dispatch, budget/entitlements detail.
- Lean the engine deps (make playwright/reportlab optional at import).
- Phase 4: lift `engine/` into its own OSS repo.

## How to run it (reviewer quickstart)
```bash
export DATABASE_URL='postgresql+psycopg://boss:boss@localhost:5432/boss'
# engine standalone (only the engine on the path):
PYTHONPATH=engine python -m boss_engine tools
PYTHONPATH=engine python -m boss_engine ask "how many tasks are running?"
PYTHONPATH=engine python -m boss_engine serve-mcp            # MCP stdio for GitHub/Claude
# full regression gate (engine + API graph + live):
PYTHONPATH=.:engine python engine/scripts/verify_engine.py --with-api 397 --live
```
